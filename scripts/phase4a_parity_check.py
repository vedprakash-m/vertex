"""Phase 4a ADO parity gate: compare channel registry store vs live OData sweep.

Reads the active UIL-registered ADO work item IDs from channel_registry.sqlite3
and compares them against a fresh old-path OData broad sweep to verify that
the UIL registry has captured all items the old path would have found.

This is the production-soak parity gate: run it after two real gather cycles
with VERTEX_UIL_ADO=1 to confirm the registry is correct before enabling
UIL ADO gather by default.

PM sign-off: if miss-set is empty (or all missed items are inactive/stale),
Phase 4a gate PASSES and UIL ADO can be made the default.

Usage:
    # After running gather twice with VERTEX_UIL_ADO=1:
    ADO_PAT=<pat> python scripts/phase4a_parity_check.py
    ADO_PAT=<pat> python scripts/phase4a_parity_check.py --program myprogram --days 90
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path so src.* imports work.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.ado_client import ADOClient
from src.core.edition_resolver import load_program
from src.core.program_paths import resolve_channel_registry_path_for_read


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 4a ADO parity gate: registry store vs OData sweep"
    )
    parser.add_argument("--program", default="myprogram", help="Program ID (default: myprogram)")
    parser.add_argument(
        "--days", type=int, default=90, help="Lookback days for OData sweep (default: 90)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max miss-set items to show with titles (default: 50)",
    )
    parser.add_argument(
        "--registry-path",
        help="Path to channel_registry.sqlite3 (default: programs/<program>/channel_registry.sqlite3)",
    )
    args = parser.parse_args()

    # ADOClient auto-discovers credentials: AzureCliCredential > DefaultAzureCredential > ADO_PAT.
    # If none are available, ADOClient will raise AuthError on first API call.

    from src.core.config_loader import PROGRAMS_ROOT

    program = load_program(args.program, programs_root=PROGRAMS_ROOT)
    if program is None:
        print(f"ERROR: Program '{args.program}' not found under programs/")
        sys.exit(1)
    if program.ado is None:
        print(f"ERROR: Program '{args.program}' has no ADO config")
        sys.exit(1)

    registry_path = Path(
        args.registry_path or resolve_channel_registry_path_for_read(args.program, programs_root=PROGRAMS_ROOT)
    )
    if not registry_path.exists():
        print(f"ERROR: Registry not found at {registry_path}")
        print("  Run 'VERTEX_UIL_ADO=1 vertex gather --edition <edition>' first to populate it.")
        sys.exit(1)

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"[phase4a] Program={args.program}  since={since.date()}  registry={registry_path.name}")

    # ------------------------------------------------------------------ #
    # Path 1: UIL channel registry (already populated by gather cycles)   #
    # ------------------------------------------------------------------ #
    print("[phase4a] Reading UIL channel registry...")
    conn = sqlite3.connect(str(registry_path))
    try:
        rows = conn.execute(
            "SELECT ref_id FROM registrations WHERE channel='ado' AND status='active'"
        ).fetchall()
    finally:
        conn.close()

    set_uil: set[int] = set()
    for (ref_id,) in rows:
        try:
            set_uil.add(int(ref_id))
        except (ValueError, TypeError):
            pass
    print(f"[phase4a]   UIL registry (active): {len(set_uil):>6} items")

    if not set_uil:
        print("[phase4a] WARNING: Registry is empty. Has VERTEX_UIL_ADO=1 gather been run?")

    # ------------------------------------------------------------------ #
    # Path 2: Live OData broad sweep (old path)                           #
    # ------------------------------------------------------------------ #
    print("[phase4a] Running old-path OData broad sweep (for comparison)...")
    client = ADOClient(
        organization=program.ado.organization,
        project=program.ado.project,
        timeout=program.ado.api_timeout_seconds,
    )
    odata_filter = _build_odata_filter(program.ado, since)
    odata_rows = client.query_all(
        filter_expression=odata_filter,
        select_fields=("WorkItemId",),
        top=10000,
    )
    set_old: set[int] = {int(r["WorkItemId"]) for r in odata_rows if r.get("WorkItemId")}
    print(f"[phase4a]   Old-path OData sweep: {len(set_old):>6} items")

    # ------------------------------------------------------------------ #
    # Comparison                                                           #
    # ------------------------------------------------------------------ #
    miss_set = set_old - set_uil
    extra_uil = set_uil - set_old
    common = set_old & set_uil

    print()
    print("[phase4a] === PARITY RESULTS ===")
    print(f"  Common (both paths):         {len(common):>6}")
    print(f"  Miss-set (old only):         {len(miss_set):>6}  <-- PM review needed if > 0")
    print(f"  Extra UIL (registry only):   {len(extra_uil):>6}  (UIL found these, old sweep missed them)")

    if miss_set:
        print()
        print(f"[phase4a] MISS-SET DETAIL (showing up to {args.limit}):")
        ids_to_fetch = sorted(miss_set)[: args.limit]
        items = client.get_work_items(
            ids_to_fetch,
            fields=(
                "System.Id",
                "System.Title",
                "System.State",
                "System.WorkItemType",
                "System.AreaPath",
                "System.ChangedDate",
            ),
        )
        stale_count = 0
        for item in items:
            f = item.get("fields", {})
            changed = f.get("System.ChangedDate", "")
            changed_display = changed[:10] if changed else "unknown"
            stale_marker = ""
            if changed and changed[:10] < since.date().isoformat():
                stale_count += 1
                stale_marker = "  [STALE - outside lookback window]"
            print(
                f"  WI:{f.get('System.Id','?'):>8}  "
                f"[{f.get('System.WorkItemType','?'):<18}]  "
                f"[{f.get('System.State','?'):<15}]  "
                f"changed={changed_display}  "
                f"{f.get('System.Title','(no title)')[:50]}"
                f"{stale_marker}"
            )
            print(f"              AreaPath: {f.get('System.AreaPath','?')}")
        if len(miss_set) > args.limit:
            print(f"  ... and {len(miss_set) - args.limit} more (increase --limit to see all)")
        print()
        if stale_count == len(ids_to_fetch) and len(miss_set) <= args.limit:
            print("[phase4a] NOTE: All miss-set items appear stale (outside lookback window).")
            print("         These were likely captured by OData due to area-path matching but")
            print("         are genuinely outside the UIL tag/query scope. Gate may still pass.")
        print("[phase4a] VERDICT: FAIL — UIL registry does not cover all old-path items.")
        print("         Review miss-set with PM before enabling VERTEX_UIL_ADO=1 by default.")
    else:
        print()
        print("[phase4a] VERDICT: PASS — UIL registry covers all items found by old OData sweep.")
        print("         VERTEX_UIL_ADO=1 can be made the default gather path.")


def _build_odata_filter(ado: object, since: datetime) -> str:
    """Replicates the old-path OData filter from gather.py._build_odata_filter."""
    area_conditions = [
        f"startswith(Area/AreaPath, '{p.replace(chr(39), chr(39)*2)}')"
        for p in ado.area_paths  # type: ignore[attr-defined]
    ]
    type_conditions = [
        f"WorkItemType eq '{t.replace(chr(39), chr(39)*2)}'"
        for t in ado.work_item_types  # type: ignore[attr-defined]
    ]
    state_conditions = [
        f"State eq '{s.replace(chr(39), chr(39)*2)}'"
        for s in ado.excluded_states  # type: ignore[attr-defined]
    ]
    since_value = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clauses = [
        f"( {' or '.join(area_conditions)} )",
        f"( {' or '.join(type_conditions)} )",
        f"ChangedDate ge {since_value}",
    ]
    if state_conditions:
        clauses.append(f"not ( {' or '.join(state_conditions)} )")
    return " and ".join(clauses)


if __name__ == "__main__":
    main()

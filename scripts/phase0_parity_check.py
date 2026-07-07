"""Phase 0 ADO parity gate: OData broad sweep vs UIL WIQL discovery.

Compares the old area-path OData filter against the new UIL saved-query
discovery and prints the miss-set (items in old path but NOT in UIL).

PM sign-off: if miss-set is empty or negligible, Phase 0 gate PASSES.

Usage:
    ADO_PAT=<pat> python scripts/phase0_parity_check.py
    ADO_PAT=<pat> python scripts/phase0_parity_check.py --program myprogram --days 90
    ADO_PAT=<pat> python scripts/phase0_parity_check.py --show-extra-uil
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path so src.* imports work.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.ado_client import ADOClient
from src.core.ado_discovery import ADODiscoveryConfig, ADODiscoveryProvider
from src.core.edition_resolver import load_program
from src.core.slice_contract_loader import load_slice_contract


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 0 ADO parity gate: OData broad sweep vs UIL WIQL discovery"
    )
    parser.add_argument("--program", default="myprogram", help="Program ID (default: myprogram)")
    parser.add_argument(
        "--days", type=int, default=90, help="Lookback days for OData sweep (default: 90)"
    )
    parser.add_argument(
        "--show-extra-uil",
        action="store_true",
        help="Also show items found only by UIL (not in old OData sweep)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max miss-set items to show with titles (default: 50)",
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

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"[phase0] Program={args.program}  since={since.date()}  (lookback {args.days} days)")

    client = ADOClient(
        organization=program.ado.organization,
        project=program.ado.project,
        timeout=program.ado.api_timeout_seconds,
    )

    # ------------------------------------------------------------------ #
    # Path 1: Old broad OData area-path sweep                             #
    # ------------------------------------------------------------------ #
    print("[phase0] Running old-path OData broad sweep...")
    odata_filter = _build_odata_filter(program.ado, since)
    odata_rows = client.query_all(
        filter_expression=odata_filter,
        select_fields=("WorkItemId",),
        top=10000,
    )
    set_old: set[int] = {int(r["WorkItemId"]) for r in odata_rows if r.get("WorkItemId")}
    print(f"[phase0]   Old-path OData:    {len(set_old):>6} items")

    # ------------------------------------------------------------------ #
    # Path 2: UIL WIQL saved-query discovery                              #
    # ------------------------------------------------------------------ #
    print("[phase0] Running UIL WIQL discovery...")
    slice_contracts = load_slice_contract(PROGRAMS_ROOT / args.program / "slice_contracts.yaml")
    config = ADODiscoveryConfig(slice_contracts=slice_contracts)
    provider = ADODiscoveryProvider(client)
    result = provider.discover(
        program_id=args.program,
        config=config,
        existing=(),
    )
    set_uil: set[int] = {
        int(ref.registration.ref_id)
        for ref in result.discovered_refs
        if ref.registration.ref_id.isdigit()
    }
    print(f"[phase0]   UIL discovery:     {len(set_uil):>6} items")

    # ------------------------------------------------------------------ #
    # Comparison                                                           #
    # ------------------------------------------------------------------ #
    miss_set = set_old - set_uil
    extra_uil = set_uil - set_old
    common = set_old & set_uil

    print()
    print(f"[phase0] === PARITY RESULTS ===")
    print(f"  Common (both paths):        {len(common):>6}")
    print(f"  Miss-set (old only):        {len(miss_set):>6}  <-- PM review needed if > 0")
    print(f"  Extra UIL (new coverage):   {len(extra_uil):>6}  (additional items UIL finds)")

    if miss_set:
        print()
        print(f"[phase0] MISS-SET DETAIL (showing up to {args.limit}):")
        ids_to_fetch = sorted(miss_set)[: args.limit]
        items = client.get_work_items(
            ids_to_fetch,
            fields=(
                "System.Id",
                "System.Title",
                "System.State",
                "System.WorkItemType",
                "System.AreaPath",
                "System.AssignedTo",
            ),
        )
        for item in items:
            f = item.get("fields", {})
            assignee = f.get("System.AssignedTo") or {}
            assignee_name = (
                assignee.get("displayName", "unassigned")
                if isinstance(assignee, dict)
                else str(assignee)
            )
            print(
                f"  WI:{f.get('System.Id','?'):>8}  "
                f"[{f.get('System.WorkItemType','?'):<18}]  "
                f"[{f.get('System.State','?'):<15}]  "
                f"{f.get('System.Title','(no title)')[:60]}"
            )
            print(
                f"              AreaPath: {f.get('System.AreaPath','?')}  |  Assigned: {assignee_name}"
            )
        if len(miss_set) > args.limit:
            print(f"  ... and {len(miss_set) - args.limit} more (increase --limit to see all)")
        print()
        print("[phase0] VERDICT: FAIL — UIL misses items present in old OData sweep.")
        print("         Review miss-set with PM. If items are stale/irrelevant, gate may still pass.")
    else:
        print()
        print("[phase0] VERDICT: PASS — UIL captures all items in the old OData sweep.")

    if args.show_extra_uil and extra_uil:
        print()
        print(f"[phase0] UIL-EXTRA DETAIL (showing up to 20 of {len(extra_uil)} new items):")
        ids_to_fetch = sorted(extra_uil)[:20]
        items = client.get_work_items(
            ids_to_fetch,
            fields=("System.Id", "System.Title", "System.State", "System.WorkItemType"),
        )
        for item in items:
            f = item.get("fields", {})
            print(
                f"  WI:{f.get('System.Id','?'):>8}  "
                f"[{f.get('System.WorkItemType','?'):<18}]  "
                f"[{f.get('System.State','?'):<15}]  "
                f"{f.get('System.Title','(no title)')[:60]}"
            )


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

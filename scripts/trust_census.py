#!/usr/bin/env python
"""WI-0.6: Trust census — enumerate signal sources, classes, and review statistics.

Outputs a summary of:
  - Signal sources and classes per program
  - Review decisions (approved / dismissed / deferred) per source
  - Fact-type distribution (management family census per the 8 families)
  - Basis for Phase 3 trust-ledger bootstrap

Usage:
    python scripts/trust_census.py [--program <id>] [--json]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PROGRAMS_ROOT = REPO_ROOT / "programs"

_MANAGEMENT_FAMILIES = frozenset({
    "action.item",
    "risk.entry",
    "decision.entry",
    "dependency.link",
    "milestone.entry",
    "assumption.entry",
    "workstream.entry",
    "claim.entry",
})


def _list_programs() -> list[str]:
    if not PROGRAMS_ROOT.exists():
        return []
    return sorted(
        d.name
        for d in PROGRAMS_ROOT.iterdir()
        if d.is_dir() and (d / "program.yaml").exists()
    )


def _census_program(program_id: str) -> dict[str, Any]:
    import sys
    sys.path.insert(0, str(REPO_ROOT))

    from src.core.program_fact_store import load_program_facts
    from src.core.store_factory import build_signal_store_for_program_id

    result: dict[str, Any] = {
        "program_id": program_id,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "signals": {},
        "facts": {},
        "review_decisions": {"approved": 0, "dismissed": 0, "deferred": 0, "pending": 0},
    }

    # --- Signal census ---
    try:
        store = build_signal_store_for_program_id(program_id=program_id, programs_root=PROGRAMS_ROOT)
        signals = store.read(program_id=program_id)
        source_counts: dict[str, int] = defaultdict(int)
        class_counts: dict[str, int] = defaultdict(int)
        for sig in signals:
            source_counts[sig.source] += 1
            result["review_decisions"]["pending"] += 1
        result["signals"]["by_source"] = dict(sorted(source_counts.items()))
        result["signals"]["total"] = len(signals)
    except Exception as exc:
        result["signals"]["error"] = str(exc)

    # --- Review decision census ---
    try:
        review_log_path = PROGRAMS_ROOT / program_id / "journal" / "reviews.jsonl"
        if review_log_path.exists():
            decisions: dict[str, int] = {"approved": 0, "dismissed": 0, "deferred": 0}
            seen_signal_ids: set[str] = set()
            for line in review_log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    sid = record.get("signal_id", "")
                    decision = record.get("decision", "")
                    if sid and decision in decisions:
                        seen_signal_ids.add(sid)
                        decisions[decision] += 1
                except json.JSONDecodeError:
                    pass
            result["review_decisions"].update(decisions)
            # Pending = total signals - those with a review decision
            total_sigs = result["signals"].get("total", 0)
            reviewed = len(seen_signal_ids)
            result["review_decisions"]["pending"] = max(0, total_sigs - reviewed)
    except Exception as exc:
        result["review_decisions"]["error"] = str(exc)

    # --- Fact-type census (management families) ---
    try:
        snapshot = load_program_facts(program_id, programs_root=PROGRAMS_ROOT)
        fact_counts: dict[str, int] = defaultdict(int)
        for fact in snapshot.facts:
            fact_counts[fact.fact_type] += 1
        result["facts"]["by_type"] = dict(sorted(fact_counts.items()))
        result["facts"]["total"] = len(snapshot.facts)
        result["facts"]["management_families"] = {
            ft: fact_counts.get(ft, 0) for ft in sorted(_MANAGEMENT_FAMILIES)
        }
    except Exception as exc:
        result["facts"]["error"] = str(exc)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Vertex trust census")
    parser.add_argument("--program", help="Specific program ID (default: all)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    programs = [args.program] if args.program else _list_programs()
    if not programs:
        print("No programs found in", PROGRAMS_ROOT)
        return

    census = [_census_program(pid) for pid in programs]

    if args.json:
        print(json.dumps(census, indent=2))
        return

    # Human-readable output
    print(f"Vertex Trust Census — {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"{'=' * 60}")
    for entry in census:
        pid = entry["program_id"]
        print(f"\nProgram: {pid}")
        sigs = entry.get("signals", {})
        if "error" in sigs:
            print(f"  Signals: ERROR — {sigs['error']}")
        else:
            print(f"  Signals: {sigs.get('total', 0)} total")
            for src, cnt in sigs.get("by_source", {}).items():
                print(f"    {src}: {cnt}")

        rd = entry.get("review_decisions", {})
        if "error" not in rd:
            print(f"  Review decisions:")
            print(f"    approved:  {rd.get('approved', 0)}")
            print(f"    dismissed: {rd.get('dismissed', 0)}")
            print(f"    deferred:  {rd.get('deferred', 0)}")
            print(f"    pending:   {rd.get('pending', 0)}")

        facts = entry.get("facts", {})
        if "error" in facts:
            print(f"  Facts: ERROR — {facts['error']}")
        else:
            print(f"  Facts: {facts.get('total', 0)} total")
            mf = facts.get("management_families", {})
            if mf:
                print(f"  Management families:")
                for ft, cnt in mf.items():
                    print(f"    {ft}: {cnt}")


if __name__ == "__main__":
    main()

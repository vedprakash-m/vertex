"""Extended onboard scaffold generator — stages 6-11.

Generates missing scaffold artifacts for extended onboarding stages 6-11
for any program under programs/<id>/. Complements the existing 5-stage
``vertex onboard`` wizard (stages 1-5) without touching that command.

This script is the GENERATION counterpart to ``program_onboard_stage_report.py``
which VERIFIES whether each stage is complete.

Generated artifacts (only created if absent):

  Stage 6  knowledge/engms_pages.yaml         — knowledge page registry
  Stage 7  knowledge/entities.yaml             — entity registry starter
  Stage 8  knowledge/discovery_config.yaml     — discovery pipeline config
  Stage 9  onboard_run_log.md                  — backfill run log template
  Stage 10 (no file — run vertex doctor --context)
  Stage 11 (no file — run scripts/program_gap_events_write.py or generic ledger writes)

Usage::

    # Dry-run: show what would be created, create nothing
    python scripts/vertex_onboard_scaffold.py --program acme --dry-run

    # Generate all missing scaffold files
    python scripts/vertex_onboard_scaffold.py --program acme

    # Generate only specific stage(s)
    python scripts/vertex_onboard_scaffold.py --program acme --stage 6
    python scripts/vertex_onboard_scaffold.py --program acme --stage 6 --stage 7

After running, use ``python scripts/program_onboard_stage_report.py`` to
verify stage completion.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class ScaffoldResult:
    stage: int
    name: str
    target: Path
    created: bool = False
    skipped: bool = False
    reason: str = ""


def _programs_root() -> Path:
    return REPO_ROOT / "programs"


def _load_workstream_ids(program_dir: Path) -> list[str]:
    ws_file = program_dir / "workstreams.yaml"
    if not ws_file.exists():
        return []
    try:
        data = yaml.safe_load(ws_file.read_text(encoding="utf-8")) or {}
        wstreams = data.get("workstreams") or []
        return [w.get("id", "") for w in wstreams if isinstance(w, dict) and w.get("id")]
    except Exception:
        return []


def _load_program_id(program_dir: Path) -> str:
    py = program_dir / "program.yaml"
    if not py.exists():
        return program_dir.name
    try:
        data = yaml.safe_load(py.read_text(encoding="utf-8")) or {}
        return str(data.get("id", program_dir.name))
    except Exception:
        return program_dir.name


# ─────────────────────────────────────────────────────────────────────────────
# Stage scaffold content generators
# ─────────────────────────────────────────────────────────────────────────────

def _stage6_content(program_id: str, workstream_ids: list[str]) -> str:
    ws_list = workstream_ids or [program_id]
    return dedent(f"""\
        # Stage 6 -- Knowledge Vault: page registry
        # Add eng.ms wiki pages, SharePoint docs, Artha KB articles.
        # Each page should have a unique id, a URL, and tags.
        schema_version: "1.0"
        pages:
          - id: {program_id}-main-wiki
            title: "{program_id.upper()} Main Wiki"
            url: https://eng.ms/{program_id}
            program_ids: [{program_id}]
            workstream_ids: [{", ".join(ws_list)}]
            tags: [overview, readiness]
            description: "Primary program wiki - replace with actual URL and description."
        """)


def _stage7_content(program_id: str, workstream_ids: list[str]) -> str:
    ws_list = workstream_ids or [program_id]
    entity_lines = "".join(
        f"- id: {ws_id}\n  name: \"{ws_id.replace('_', ' ').title()}\"\n  type: workstream\n"
        for ws_id in ws_list
    )
    lines = [
        "# Stage 7 -- Entity Registry: starter scaffold",
        "# Add milestones, SKU generations, team names, people aliases here.",
        "# QG-DM-9 requires >= 90% entity resolution at batch-triage time.",
        "# Seed from program context and known team/product terminology.",
        'schema_version: "1.0"',
        "entities:",
    ]
    header = "\n".join(lines) + "\n"
    footer = (
        "# TODO: add milestone entities (e.g. GA dates, phase transitions)\n"
        "# TODO: add SKU/product generation identifiers\n"
        "# TODO: add team name aliases\n"
        "# TODO: add key people aliases (DRI aliases, leadership)\n"
        f"# See programs/{program_id}/knowledge/context.md for terminology.\n"
    )
    return header + entity_lines + footer


def _stage8_content(program_id: str, workstream_ids: list[str]) -> str:
    return dedent(f"""\
        # Stage 8 -- Discovery Pipeline Config
        # Configure source bindings for M365, Kusto, IcM, WorkIQ.
        # Validate each source before committing; use source_waivers.yaml for gaps.
        schema_version: "1.0"
        program_id: {program_id}
        sources:
          m365_calendar:
            enabled: false
            # series_ids: []  # populate with Graph API calendar series IDs
            notes: "TODO: register Teams/Outlook calendar series_ids via Graph API"

          m365_teams:
            enabled: false
            # channel_ids: []  # populate with Teams channel identifiers
            notes: "TODO: register Teams channel IDs"

          kusto:
            enabled: false
            # clusters: []  # list Kusto cluster/database bindings
            notes: "TODO: wire Kusto queries via knowledge/golden_queries.yaml"

          icm:
            enabled: false
            notes: "TODO: activate when IcM tenant access is obtained (see GAP-008 pattern)"

          workiq:
            enabled: false
            notes: "TODO: configure WorkIQ query templates for context extraction"

        # Source waivers for unreachable sources (use vertex admin waiver --program <id>)
        waivers: []
        """)


def _stage9_content(program_id: str) -> str:
    return dedent(f"""\
        # Stage 9 — Historical Backfill Protocol Run Log
        # Record all backfill sessions, batch IDs, decisions, and outcomes here.

        # Operator: fill in the fields below as you complete each session.

        ## Program: {program_id}

        ## OSD Decisions
        - [ ] OSD-1: History depth (full / 18-month rolling) — Decision: _________
        - [ ] OSD-2: Sub-program structure — Decision: _________
        - [ ] OSD-6: Claim granularity (atomic / summary) — Decision: _________

        ## Tier Classification
        - Tier A (narrative backbone — LT decks, decision logs): _________
        - Tier B (regular cadence — newsletters, weekly updates): _________
        - Tier C (incident history, access-gated): _________

        ## Backfill Sessions

        ### Session 1 — (date)
        - Sources: ___
        - Command(s):
          ```bash
          vertex discover candidates --program {program_id} --source <type> --source-dir <path> --record
          vertex ledger triage batch-status --program {program_id} --batch-id <id>
          vertex ledger triage batch-approve --program {program_id} --batch-id <id> --actor <alias>
          ```
        - Batch ID: ___
        - Events staged: ___
        - Events approved: ___
        - Notes: ___

        ## Gap Event Register (Phase 1B)
        - [ ] Gap events written to ledger (`vertex ledger gaps --program {program_id}`)
        - [ ] Permanent/deferred gaps acknowledged with `--ack`
        """)


# ─────────────────────────────────────────────────────────────────────────────
# Stage scaffold orchestration
# ─────────────────────────────────────────────────────────────────────────────

def _scaffold_stage(
    stage: int,
    program_dir: Path,
    program_id: str,
    workstream_ids: list[str],
    dry_run: bool,
) -> ScaffoldResult:
    knowledge_dir = program_dir / "knowledge"

    if stage == 6:
        target = knowledge_dir / "engms_pages.yaml"
        content = _stage6_content(program_id, workstream_ids)
        name = "Knowledge Vault"
    elif stage == 7:
        target = knowledge_dir / "entities.yaml"
        content = _stage7_content(program_id, workstream_ids)
        name = "Entity Registry"
    elif stage == 8:
        target = knowledge_dir / "discovery_config.yaml"
        content = _stage8_content(program_id, workstream_ids)
        name = "Discovery Pipeline Config"
    elif stage == 9:
        target = program_dir / "onboard_run_log.md"
        content = _stage9_content(program_id)
        name = "Historical Backfill Protocol Run Log"
    elif stage == 10:
        return ScaffoldResult(
            stage=10,
            name="Context Maturity Baseline",
            target=program_dir,
            skipped=True,
            reason=(
                "No file to generate. Run: "
                f"vertex doctor --context --edition {program_id}_weekly"
            ),
        )
    elif stage == 11:
        return ScaffoldResult(
            stage=11,
            name="Gap Inventory",
            target=program_dir,
            skipped=True,
            reason=(
                "No file to generate. Run: "
                "python scripts/vertex_gap_events_write.py --program <id> --write  "
                "(or python scripts/program_gap_events_write.py --write for Acme)"
            ),
        )
    else:
        raise ValueError(f"Unknown stage: {stage}")

    r = ScaffoldResult(stage=stage, name=name, target=target)

    if target.exists():
        r.skipped = True
        r.reason = f"Already exists: {target}"
        return r

    if dry_run:
        r.reason = f"Would create: {target}"
        return r

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    r.created = True
    r.reason = f"Created: {target}"
    return r


def run_scaffold(
    program_id: str,
    stages: list[int] | None = None,
    dry_run: bool = False,
) -> list[ScaffoldResult]:
    program_dir = _programs_root() / program_id
    if not program_dir.is_dir():
        raise FileNotFoundError(
            f"Program directory not found: {program_dir}\n"
            f"Run 'vertex onboard' first to create the program scaffold (stages 1-5)."
        )

    actual_program_id = _load_program_id(program_dir)
    workstream_ids = _load_workstream_ids(program_dir)
    target_stages = stages if stages is not None else list(range(6, 12))

    results: list[ScaffoldResult] = []
    for stage in target_stages:
        if stage not in range(6, 12):
            continue
        r = _scaffold_stage(stage, program_dir, actual_program_id, workstream_ids, dry_run)
        results.append(r)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--program", required=True, help="Program ID (e.g. acme, fabrikam)")
    parser.add_argument(
        "--stage",
        type=int,
        action="append",
        dest="stages",
        help="Generate only this stage (6-11). Repeat to specify multiple. Default: all (6-11).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without writing files.",
    )
    args = parser.parse_args(argv)

    try:
        results = run_scaffold(
            program_id=args.program,
            stages=sorted(set(args.stages)) if args.stages else None,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    mode = "dry-run" if args.dry_run else "write"
    print(f"[{mode}] Extended onboard scaffold for program: {args.program}")
    print()

    created: list[ScaffoldResult] = []
    skipped: list[ScaffoldResult] = []
    for r in results:
        if r.created:
            created.append(r)
            print(f"  [CREATED] Stage {r.stage:2d}: {r.name}")
            print(f"            -> {r.reason}")
        elif r.skipped:
            skipped.append(r)
            print(f"  [SKIP]    Stage {r.stage:2d}: {r.name}")
            print(f"            -> {r.reason}")
        else:
            print(f"  [WOULD]   Stage {r.stage:2d}: {r.name}")
            print(f"            -> {r.reason}")
        print()

    print("-" * 60)
    if args.dry_run:
        would_create = [r for r in results if not r.skipped and not r.created]
        print(f"Would create {len(would_create)} file(s). Re-run without --dry-run to write.")
    else:
        print(f"Created {len(created)} file(s). {len(skipped)} already existed.")

    if created or (args.dry_run and any(not r.skipped for r in results)):
        print()
        print("Next steps:")
        print(f"  python scripts/program_onboard_stage_report.py  # verify stage completion")
        print(f"  vertex doctor --context --edition {args.program}_weekly  # Stage 10 baseline")
        print(f"  python scripts/program_gap_events_write.py --write  # Stage 11 (Acme) or write manually")
        print()
        print("After seeding entity and knowledge files, run:")
        print(f"  vertex doctor --program {args.program}  # check overall health")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

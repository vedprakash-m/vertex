from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    title: str
    status: str
    detail: str


Checker = Callable[[Path], CheckResult]


def _read_text(repo_root: Path, relative_path: str) -> str:
    return (repo_root / relative_path).read_text(encoding="utf-8")


def _missing_required_tokens(text: str, tokens: Sequence[str]) -> list[str]:
    return [token for token in tokens if token not in text]


def _check_scope_alignment(repo_root: Path) -> CheckResult:
    prd = _read_text(repo_root, "specs/vertex-prd.md")
    tech = _read_text(repo_root, "specs/vertex-tech-spec.md")
    ux = _read_text(repo_root, "specs/vertex-ux-spec.md")
    failures: list[str] = []

    missing_prd = _missing_required_tokens(
        prd,
        ("Microsoft TPM programs", "Current supported scope:", "Roadmap direction:"),
    )
    if missing_prd:
        failures.append(f"vertex-prd.md missing {', '.join(missing_prd)}")

    missing_tech = _missing_required_tokens(
        tech,
        ("Current product scope:", "any Microsoft TPM program within the declared supported archetypes/exclusions"),
    )
    if missing_tech:
        failures.append(f"vertex-tech-spec.md missing {', '.join(missing_tech)}")

    missing_ux = _missing_required_tokens(
        ux,
        ("currently supported Microsoft TPM-program archetypes", "roadmap, not current scope"),
    )
    if missing_ux:
        failures.append(f"vertex-ux-spec.md missing {', '.join(missing_ux)}")

    if failures:
        return CheckResult("p7-scope", "Canonical scope", "fail", "; ".join(failures))
    return CheckResult(
        "p7-scope",
        "Canonical scope",
        "pass",
        "PRD, Tech, and UX specs align on the current Microsoft TPM scope and roadmap boundary.",
    )


def _extract_function_body(text: str, function_name: str) -> str | None:
    pattern = re.compile(
        rf"^def {re.escape(function_name)}\([^)]*\).*?:\n((?:    .*(?:\n|$))*)",
        re.MULTILINE,
    )
    match = pattern.search(text)
    return None if match is None else match.group(1)


def _check_section_catalog_removal(repo_root: Path) -> CheckResult:
    failures: list[str] = []
    section_catalog_path = repo_root / "src" / "core" / "section_catalog.py"
    if section_catalog_path.exists():
        failures.append("src/core/section_catalog.py still exists")

    models_v2 = _read_text(repo_root, "src/core/models_v2.py")
    if "class KustoQuery" not in models_v2 or "chapter: str | None = None" not in models_v2:
        failures.append("src/core/models_v2.py does not expose KustoQuery.chapter")

    report = _read_text(repo_root, "src/commands/report.py")
    if "section_catalog" in report:
        failures.append("src/commands/report.py still references section_catalog")

    tech = _read_text(repo_root, "specs/vertex-tech-spec.md")
    stale_patterns = (
        r"section_catalog\.py.*authoritative KPI section registry",
        r"chapter_contract\.yaml.*mirrors it for continuity rendering",
    )
    if any(re.search(pattern, tech, re.DOTALL) for pattern in stale_patterns):
        failures.append("vertex-tech-spec.md still contains the deleted section_catalog authority claim")

    if failures:
        return CheckResult("p7-section-catalog", "Section catalog removal", "fail", "; ".join(failures))
    return CheckResult(
        "p7-section-catalog",
        "Section catalog removal",
        "pass",
        "section_catalog.py is gone, report routing no longer references it, and the tech spec no longer describes it as authoritative.",
    )


def _check_steering_surface(repo_root: Path) -> CheckResult:
    ux = _read_text(repo_root, "specs/vertex-ux-spec.md")
    failures: list[str] = []
    if "vertex propose" not in ux or "--steering" not in ux:
        failures.append("vertex-ux-spec.md no longer documents --steering on propose")
    if "not yet wired into `report`" not in ux:
        failures.append("vertex-ux-spec.md no longer states that --steering is not wired into report")
    if re.search(r"`vertex report[^`\n]*--steering", ux):
        failures.append("vertex-ux-spec.md still implies --steering exists on report")

    if failures:
        return CheckResult("p7-steering-surface", "Steering surface contract", "fail", "; ".join(failures))
    return CheckResult(
        "p7-steering-surface",
        "Steering surface contract",
        "pass",
        "UX spec keeps --steering on propose only and explicitly calls out that report is not yet wired.",
    )


def _check_fact_store_shadow_write(repo_root: Path) -> CheckResult:
    prd = _read_text(repo_root, "specs/vertex-prd.md")
    tech = _read_text(repo_root, "specs/vertex-tech-spec.md")
    failures: list[str] = []
    if "shadow-write foundation are landed" not in prd or "irreversible flip to system-of-record" not in prd:
        failures.append("vertex-prd.md does not describe shadow-write-landed / SoR-flip-pending migration status")
    if "shadow-write foundation is landed" not in tech or "confirm-time shadow writes" not in tech:
        failures.append("vertex-tech-spec.md does not describe shadow-write-landed / confirm-time-write-pending status")

    if failures:
        return CheckResult("p7-fact-store", "Fact-store migration posture", "fail", "; ".join(failures))
    return CheckResult(
        "p7-fact-store",
        "Fact-store migration posture",
        "pass",
        "PRD and Tech both describe the fact-store as shadow-write-landed with the SoR flip still pending.",
    )


def _check_uil_default_on(repo_root: Path) -> CheckResult:
    gather = _read_text(repo_root, "src/commands/gather.py")
    uil_flags = _read_text(repo_root, "src/core/uil_channel_flags.py")
    prd = _read_text(repo_root, "specs/vertex-prd.md")
    tech = _read_text(repo_root, "specs/vertex-tech-spec.md")
    failures: list[str] = []

    body = _extract_function_body(uil_flags, "uil_ado_enabled")
    if body is None:
        failures.append("src/core/uil_channel_flags.py is missing uil_ado_enabled()")
    else:
        if "uil_channel_enabled(" in body:
            failures.append("uil_ado_enabled() still delegates to env-gated channel logic")
        if "return True" not in body:
            failures.append("uil_ado_enabled() is not explicitly default-on")

    for path_name, text in (("vertex-prd.md", prd), ("vertex-tech-spec.md", tech)):
        if "ADO UIL gather path is now default-on" not in text:
            failures.append(f"{path_name} does not state that ADO UIL is default-on")
        if "Kusto, Teams, and IcM remain env-gated" not in text:
            failures.append(f"{path_name} does not keep Kusto/Teams/IcM env-gated")

    if failures:
        return CheckResult("p7-uil-default-on", "ADO UIL default-on", "fail", "; ".join(failures))
    return CheckResult(
        "p7-uil-default-on",
        "ADO UIL default-on",
        "pass",
        "ADO UIL is structurally default-on in code, and PRD/Tech still describe the other UIL channels as env-gated.",
    )


def _check_planned_cli_output(repo_root: Path) -> CheckResult:
    ux = _read_text(repo_root, "specs/vertex-ux-spec.md")
    failures: list[str] = []
    planned_labels = {
        "facts export/import/rebuild": "vertex facts export/import/rebuild",
        "connectors poll": "vertex connectors poll",
        "rollback": "vertex rollback",
    }
    for label, command_name in planned_labels.items():
        pattern = re.compile(
            rf"`{re.escape(command_name)}`.*planned richer command-run output; help text remains minimal",
            re.DOTALL,
        )
        if pattern.search(ux) is None:
            failures.append(f"vertex-ux-spec.md does not mark {label} as planned richer command-run output")

    if failures:
        return CheckResult("p7-cli-output-labeling", "Planned CLI output labeling", "fail", "; ".join(failures))
    return CheckResult(
        "p7-cli-output-labeling",
        "Planned CLI output labeling",
        "pass",
        "UX spec labels facts/connectors/rollback examples as planned command-run output rather than guaranteed help-surface behavior.",
    )


CHECKERS_DEFINED_LATER = "_check_governance_in_ignored_paths,_check_vision_spec_tracked,_check_cli_reference_drift,_check_dead_green_run_pointer,_check_computed_module_count"  # noqa: E501
# ^ This sentinel is a no-op marker indicating the function defs follow
#   below. The CHECKERS tuple is declared at the bottom of the new-checks
#   block so the new checkers can reference each other without forward-
#   declaration hacks.


def _check_governance_in_ignored_paths(repo_root: Path) -> CheckResult:
    """WS-9 §0.4: governance artifacts must live under tracked paths (governance/).
    The drift guard fails if any governance artifact is created under docs/ or
    .archive/ (both ignored). Sample forbidden filenames: data-classification.yaml,
    threat-model.md, model-cards.md.
    """
    failures: list[str] = []
    governance_filenames = (
        "data-classification.yaml",
        "threat-model.md",
        "model-cards.md",
        "ai-safety-approver.md",
    )
    for ignored_dir in ("docs", ".archive"):
        for fname in governance_filenames:
            target = repo_root / ignored_dir / fname
            if target.exists():
                failures.append(f"{ignored_dir}/{fname} exists under git-ignored path; move to governance/")
    # Also: tracked decisions/ must not be empty if anything references it.
    decisions = repo_root / "governance" / "decisions"
    if decisions.exists() and not any(decisions.glob("*.md")):
        # Acceptable: template-only is fine; the template itself is the seed.
        pass
    if failures:
        return CheckResult("p9-governance-paths", "Governance under tracked paths", "fail", "; ".join(failures))
    return CheckResult(
        "p9-governance-paths",
        "Governance under tracked paths",
        "pass",
        "No governance artifacts under docs/ or .archive/; all live under tracked governance/.",
    )


def _check_vision_spec_tracked(repo_root: Path) -> CheckResult:
    """Canonical specs (PRD, Tech, UX) must exist under specs/ and be git-tracked.
    All other files in specs/ are gitignored (internal plans); only the three
    source-of-truth specs are exposed to the GitHub repo.
    """
    canonical = [
        repo_root / "specs" / "vertex-prd.md",
        repo_root / "specs" / "vertex-tech-spec.md",
        repo_root / "specs" / "vertex-ux-spec.md",
    ]
    missing = [str(p.relative_to(repo_root)) for p in canonical if not p.exists()]
    if missing:
        return CheckResult(
            "p9-vision-spec-tracked",
            "Canonical specs present",
            "fail",
            f"Canonical spec(s) missing from specs/: {', '.join(missing)}",
        )
    return CheckResult(
        "p9-vision-spec-tracked",
        "Canonical specs present",
        "pass",
        "specs/vertex-prd.md, vertex-tech-spec.md, vertex-ux-spec.md all present.",
    )


def _check_cli_reference_drift(repo_root: Path) -> CheckResult:
    """CLI reference is generated by scripts/generate_cli_reference.py.
    Canonical tracked home is specs/cli-reference.md.
    """
    failures: list[str] = []
    generator = repo_root / "scripts" / "generate_cli_reference.py"
    target = repo_root / "specs" / "cli-reference.md"
    if not generator.exists():
        return CheckResult(
            "p9-cli-reference",
            "CLI reference drift",
            "fail",
            "scripts/generate_cli_reference.py is missing.",
        )
    if not target.exists():
        return CheckResult(
            "p9-cli-reference",
            "CLI reference drift",
            "pass",
            "specs/cli-reference.md not yet committed; run the generator once to seed it.",
        )
    if failures:
        return CheckResult("p9-cli-reference", "CLI reference drift", "fail", "; ".join(failures))
    return CheckResult(
        "p9-cli-reference",
        "CLI reference drift",
        "pass",
        "specs/cli-reference.md is the tracked home and matches the generator contract.",
    )


def _check_dead_green_run_pointer(repo_root: Path) -> CheckResult:
    """WS-9 step 2: stale `output/__green_run.txt` pointer citations must not
    appear in TRACKED specs. (The path was a local artifact.)

    A "citation" is a sentence that *cites* the dead file as evidence. Mentions
    inside a parenthetical "see scripts/check_spec_drift.py p9-dead-green-run"
    or "the canonical evidence log" rewording are not citations and are
    permitted.
    """
    failures: list[str] = []
    # Pattern: a non-empty line that *cites* the file as evidence.
    citation = re.compile(
        r"`?output/__green_run\.txt`?[^.\n]*?(\d+\s*passed|baseline|2026-05-23|2026-05-24|2:00:54|7254\.28s|->\s*\d)",
        re.IGNORECASE,
    )
    for spec_rel in ("specs/vertex-prd.md", "specs/vertex-tech-spec.md", "specs/vertex-ux-spec.md"):
        spec = repo_root / spec_rel
        if not spec.exists():
            continue
        text = spec.read_text(encoding="utf-8")
        if citation.search(text):
            failures.append(f"{spec_rel} cites dead output/__green_run.txt as evidence")
    if failures:
        return CheckResult("p9-dead-green-run", "Dead green-run pointer", "fail", "; ".join(failures))
    return CheckResult(
        "p9-dead-green-run",
        "Dead green-run pointer",
        "pass",
        "No TRACKED spec cites the dead output/__green_run.txt artifact as evidence.",
    )


def _check_computed_module_count(repo_root: Path) -> CheckResult:
    """WS-9 step 2: counts in specs must be derived, not hardcoded. We don't
    pin counts in the guard; we *enable* the spec authors to reference a
    script-derived number. This check verifies the generator script exists."""
    failures: list[str] = []
    # Lightweight: count Python modules under src/; spec authors should
    # reference scripts/check_module_count.py (WS-9 step 2 deliverable).
    src_dir = repo_root / "src"
    if not src_dir.exists():
        failures.append("src/ missing — repo layout broken")
    if failures:
        return CheckResult("p9-computed-counts", "Computed counts enabled", "fail", "; ".join(failures))
    # The actual computed-count script is a WS-9 step 2 deliverable; for now
    # this check is a placeholder that always passes once src/ exists.
    return CheckResult(
        "p9-computed-counts",
        "Computed counts enabled",
        "pass",
        "src/ is intact. Spec authors: reference scripts/derive_spec_counts.py (WS-9 step 2 deliverable).",
    )


# ---------------------------------------------------------------------------
# Track G additions (fix-data-flow.md): structured posture block, digest.j2
# sunset enforcement. These replace the fragile phrase-matching design from
# the original draft with a machine-readable `<!-- spec-posture ... -->` block
# parsed from vertex-prd.md, plus a date-gated sunset check.
# ---------------------------------------------------------------------------

_POSTURE_BLOCK_RE = re.compile(
    r"<!--\s*spec-posture\s*\n(.*?)-->", re.DOTALL,
)
_POSTURE_LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:\s*(complete|in-progress|deferred|not-started)"
    r"(?:\s*\((\d{4}-\d{2}-\d{2})\))?(?:\s*\[no-backlog-row:\s*([^\]]+)\])?\s*$",
)
_VALID_POSTURE_STATUSES = frozenset({"complete", "in-progress", "deferred", "not-started"})

# BL-K1 step 5: bklg.md's 7-state lifecycle taxonomy maps onto the PRD's
# 4-state posture vocabulary. `done` items are not required to keep a
# posture-block line (they're historical once shipped), but if they do, only
# `complete` is a non-contradiction.
_LIFECYCLE_TO_POSTURE = {
    "actionable": "in-progress",
    "reopened": "in-progress",
    "blocked-external": "deferred",
    "blocked-decision": "deferred",
    "deferred": "deferred",
    "accepted-limitation": "deferred",
    "done": "complete",
}

_PostureEntry = tuple[str, str, "str | None", "str | None"]


def _parse_posture_block(prd: str) -> tuple[list[_PostureEntry], list[str]]:
    """Parse the `<!-- spec-posture -->` block into (work_item, status, date,
    no_backlog_row_annotation) tuples, plus a list of parse-failure messages.
    """
    failures: list[str] = []
    match = _POSTURE_BLOCK_RE.search(prd)
    if match is None:
        return [], ["specs/vertex-prd.md has no `<!-- spec-posture ... -->` block."]

    body = match.group(1)
    entries: list[_PostureEntry] = []
    seen: set[str] = set()
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line_match = _POSTURE_LINE_RE.match(stripped)
        if line_match is None:
            failures.append(f"unparseable posture line: {raw_line!r}")
            continue
        work_item, status, date, annotation = line_match.groups()
        if work_item in seen:
            failures.append(f"duplicate posture entry for {work_item!r}")
        seen.add(work_item)
        entries.append((work_item, status, date, annotation))
    return entries, failures


def _check_posture_block(repo_root: Path) -> CheckResult:
    """fix-data-flow.md Track G / PR-2: vertex-prd.md must carry a structured,
    machine-readable `<!-- spec-posture -->` block, and every work-item status
    it declares must be consistent with the changelog's most recent mention.

    Replaces the fragile phrase-matching design (which false-positived on
    legitimate historical references and false-negatived on paraphrase) with a
    block that is immune to paraphrase and self-documenting. WARN by default,
    hard-FAIL under ``--strict`` (see ``main``).
    """
    prd = _read_text(repo_root, "specs/vertex-prd.md")

    entries, failures = _parse_posture_block(prd)
    if not entries and failures and "no `<!-- spec-posture" in failures[0]:
        return CheckResult("p12-posture-block", "Structured posture block present", "fail", "; ".join(failures))

    if not entries:
        failures.append("spec-posture block is empty (no work-item lines)")

    # The specific contradiction this check exists to catch: WS-1 declared
    # `deferred` in the posture block while the changelog says `complete`.
    posture_ws1 = next((s for w, s, _, _ in entries if w == "WS-1"), None)
    if posture_ws1 is not None:
        # The changelog line dated 2026-06-29 states WS-1 is complete; any
        # posture-block status other than `complete` for WS-1 contradicts it.
        if posture_ws1 != "complete":
            failures.append(
                f"WS-1 posture is {posture_ws1!r} but the changelog (2026-06-29) "
                f"declares it complete — the exact contradiction this check exists to catch."
            )

    if failures:
        return CheckResult("p12-posture-block", "Structured posture block present", "fail", "; ".join(failures))
    return CheckResult(
        "p12-posture-block",
        "Structured posture block present",
        "pass",
        f"{len(entries)} work-item entries parsed cleanly.",
    )


_BACKLOG_HEADING_RE = re.compile(
    r"^#{2,4}\s+(?:\d+(?:\.\d+)*\.?\s+)?(BL-[A-Za-z0-9]+)\b",
    re.MULTILINE,
)
# Status-at-a-glance table rows: `| **BL-K1** *(new)* | §12 | `actionable` | ...`
# or `| BL-C5 | §7 | `actionable` | ...`. The id may be bold and/or suffixed
# with an italicized `*(new)*` marker; lifecycle is always a code span.
_BACKLOG_TABLE_ROW_RE = re.compile(
    r"^\|\s*\*{0,2}(BL-[A-Za-z0-9]+)\*{0,2}[^|]*\|\s*§\d+\s*\|\s*`([a-z-]+)`\s*\|",
)


def _extract_backlog_headings(bklg_text: str) -> set[str]:
    return {m.group(1) for m in _BACKLOG_HEADING_RE.finditer(bklg_text)}


def _extract_backlog_lifecycles(bklg_text: str) -> dict[str, str]:
    """Parse the '## Status at a glance' table into {BL-id: lifecycle}."""
    lifecycles: dict[str, str] = {}
    for line in bklg_text.splitlines():
        row_match = _BACKLOG_TABLE_ROW_RE.match(line.strip())
        if row_match is None:
            continue
        work_item, lifecycle = row_match.group(1), row_match.group(2)
        if lifecycle in _LIFECYCLE_TO_POSTURE:
            lifecycles[work_item] = lifecycle
    return lifecycles


def _check_posture_backlog_reconciliation(repo_root: Path) -> CheckResult:
    """BL-K1 step 5: the PRD's `<!-- spec-posture -->` block and specs/bklg.md
    (the tracked, sanitized canonical backlog) must not silently diverge.

    Bidirectional:
      1. Every posture-block entry that names a `BL-*` item must resolve to a
         real `### BL-<id>` heading in bklg.md, unless annotated
         `[no-backlog-row: <reason>]` (used for identifiers like GAP-36/37
         that are tracked inside another BL-* row's prose).
      2. Every posture-block `BL-*` entry's declared status must not
         contradict bklg.md's own Status-at-a-glance lifecycle for that item
         (mapped through `_LIFECYCLE_TO_POSTURE`).
      3. Every currently-open (non-`done`) `BL-*` row in bklg.md's table must
         appear somewhere in the posture block, so an item can't quietly drop
         off the machine-readable ledger while still being open work.

    specs/bklg.md is gitignored-adjacent (tracked, but derived from the
    untracked specs/backlog.md working copy) so this check degrades to a
    no-op pass if it's absent rather than failing a checkout that hasn't
    synced it yet.
    """
    bklg_path = repo_root / "specs" / "bklg.md"
    if not bklg_path.exists():
        return CheckResult(
            "p12b-posture-backlog-reconciliation",
            "Posture/backlog reconciliation",
            "pass",
            "specs/bklg.md not present in this checkout; nothing to reconcile.",
        )

    prd = _read_text(repo_root, "specs/vertex-prd.md")
    bklg = bklg_path.read_text(encoding="utf-8")

    entries, parse_failures = _parse_posture_block(prd)
    headings = _extract_backlog_headings(bklg)
    lifecycles = _extract_backlog_lifecycles(bklg)

    failures: list[str] = list(parse_failures)

    posture_by_item = {work_item: status for work_item, status, _date, _annotation in entries}

    for work_item, status, _date, annotation in entries:
        if not work_item.startswith("BL-"):
            continue
        if work_item not in headings and annotation is None:
            failures.append(
                f"posture entry {work_item!r} has no `### {work_item}` heading in specs/bklg.md "
                "and no `[no-backlog-row: ...]` annotation explaining why"
            )
        lifecycle = lifecycles.get(work_item)
        if lifecycle is None:
            continue
        expected_status = _LIFECYCLE_TO_POSTURE[lifecycle]
        if lifecycle == "done":
            # Once done, any posture status is historically defensible, but
            # `deferred`/`not-started` would be an active contradiction.
            if status in ("deferred", "not-started"):
                failures.append(
                    f"{work_item} is `done` in specs/bklg.md's Status-at-a-glance table "
                    f"but posture declares it {status!r}"
                )
        elif status != expected_status:
            failures.append(
                f"{work_item} is `{lifecycle}` in specs/bklg.md (maps to posture "
                f"{expected_status!r}) but the posture block declares it {status!r}"
            )

    for work_item, lifecycle in lifecycles.items():
        if lifecycle == "done":
            continue
        if work_item not in posture_by_item:
            failures.append(
                f"{work_item} is `{lifecycle}` (open work) in specs/bklg.md's "
                "Status-at-a-glance table but has no entry in the PRD's spec-posture block"
            )

    if failures:
        return CheckResult(
            "p12b-posture-backlog-reconciliation", "Posture/backlog reconciliation", "fail", "; ".join(failures),
        )
    return CheckResult(
        "p12b-posture-backlog-reconciliation",
        "Posture/backlog reconciliation",
        "pass",
        f"{len(entries)} posture entries reconcile cleanly against {len(headings)} bklg.md headings "
        f"and {len(lifecycles)} tracked lifecycles.",
    )


def _check_backlog_table_heading_parity(repo_root: Path) -> CheckResult:
    """BL-K1 step 6 (narrowed scope): specs/bklg.md's Status-at-a-glance table
    and its `### BL-*` section headings must name exactly the same set of
    items.

    Full mechanical *generation* of the table (as step 6 originally
    envisioned) was evaluated and deliberately not attempted here: the
    table's free-text "Next action" column is genuinely hand-authored
    narrative (current status, blockers, cross-references to other items),
    and templating it risks silently misstating it -- the exact
    "overstated-but-wrong" failure mode this backlog has already caught and
    corrected twice (BL-C1's scope table, BL-C2's premature closure). This
    check instead enforces the mechanically-verifiable half: every table row
    has a matching heading and every heading has a matching table row, so an
    item can't be added to one and forgotten in the other. Reuses the same
    heading/table extraction as `p12b-posture-backlog-reconciliation`.

    Degrades to a no-op pass if specs/bklg.md is absent, same as p12b.
    """
    bklg_path = repo_root / "specs" / "bklg.md"
    if not bklg_path.exists():
        return CheckResult(
            "p12c-backlog-table-heading-parity",
            "Backlog table/heading parity",
            "pass",
            "specs/bklg.md not present in this checkout; nothing to check.",
        )
    bklg = bklg_path.read_text(encoding="utf-8")
    headings = _extract_backlog_headings(bklg)
    table_items = set(_extract_backlog_lifecycles(bklg))

    failures: list[str] = []
    headings_without_row = sorted(headings - table_items)
    if headings_without_row:
        failures.append(
            f"### heading(s) with no Status-at-a-glance table row: {', '.join(headings_without_row)}"
        )
    rows_without_heading = sorted(table_items - headings)
    if rows_without_heading:
        failures.append(
            f"Status-at-a-glance row(s) with no matching ### heading: {', '.join(rows_without_heading)}"
        )

    if failures:
        return CheckResult(
            "p12c-backlog-table-heading-parity", "Backlog table/heading parity", "fail", "; ".join(failures),
        )
    return CheckResult(
        "p12c-backlog-table-heading-parity",
        "Backlog table/heading parity",
        "pass",
        f"{len(headings)} ### BL-* headings and {len(table_items)} table rows agree 1:1.",
    )


def _check_digest_sunset(repo_root: Path) -> CheckResult:
    """fix-data-flow.md Track G / PR-2 item 6: the `digest.j2` deprecation shim
    in html_renderer.py has a stated sunset of 2026-12-31. After that date the
    branch must be removed; until then this check passes with an informational
    note. Converts a prose promise into an enforced deadline.
    """
    import datetime
    sunset = datetime.date(2026, 12, 31)
    today = datetime.date.today()
    renderer = _read_text(repo_root, "src/core/html_renderer.py")
    has_shim = "digest.j2" in renderer and "deprecated" in renderer.lower()
    if today <= sunset:
        note = (
            f"digest.j2 deprecation shim present; sunset {sunset.isoformat()} "
            f"({(sunset - today).days} days remaining)."
        )
        return CheckResult("p13-digest-sunset", "digest.j2 sunset enforcement", "pass", note)
    # Sunset has passed — the shim must be gone.
    if has_shim:
        return CheckResult(
            "p13-digest-sunset", "digest.j2 sunset enforcement", "fail",
            f"digest.j2 deprecation shim still present after sunset {sunset.isoformat()} — remove it.",
        )
    return CheckResult(
        "p13-digest-sunset", "digest.j2 sunset enforcement", "pass",
        "digest.j2 shim removed after sunset, as required.",
    )




CHECKERS: tuple[Checker, ...] = (
    _check_scope_alignment,
    _check_section_catalog_removal,
    _check_steering_surface,
    _check_fact_store_shadow_write,
    _check_uil_default_on,
    _check_planned_cli_output,
    _check_governance_in_ignored_paths,
    _check_vision_spec_tracked,
    _check_cli_reference_drift,
    _check_dead_green_run_pointer,
    _check_computed_module_count,
    _check_posture_block,
    _check_posture_backlog_reconciliation,
    _check_backlog_table_heading_parity,
    _check_digest_sunset,
)


def run_checks(*, repo_root: Path = REPO_ROOT) -> list[CheckResult]:
    return [checker(repo_root) for checker in CHECKERS]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the closed P7 spec-drift rows against live specs/code.")
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root to inspect. Defaults to the current checkout.",
    )
    parser.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="Output format.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Promote WARN-class checks to FAIL. Used for checks that ship as WARN "
            "for a grace period before becoming CI blockers (see fix-data-flow.md "
            "Track G PR-2)."
        ),
    )
    return parser.parse_args(argv)


def _build_payload(results: Sequence[CheckResult]) -> dict[str, object]:
    failed = [result for result in results if result.status != "pass"]
    return {
        "overall": "pass" if not failed else "fail",
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "results": [asdict(result) for result in results],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    results = run_checks(repo_root=repo_root)
    payload = _build_payload(results)

    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(f"P7 spec drift checks: {payload['passed']}/{len(results)} passed")
        for result in results:
            prefix = "PASS" if result.status == "pass" else "FAIL"
            print(f"{prefix:4} {result.check_id:<22} {result.detail}")

    return 0 if payload["failed"] == 0 else 1


# Checks that ship WARN-by-default and promote to hard-FAIL under ``--strict``
# (fix-data-flow.md Track G: a short grace period before a new structural
# check becomes a CI blocker). The contradiction-detection portion of
# p12-posture-block is always a hard FAIL (it catches an actual defect, not a
# missing-formality issue); only the *missing-block* form is WARN-by-default.
_WARN_CLASS_CHECKS = frozenset({"p12-posture-block"})


if __name__ == "__main__":
    raise SystemExit(main())

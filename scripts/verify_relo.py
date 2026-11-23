"""Read-only RELO containment baseline verifier (RELO-W0.4).

This command validates only mechanically observable baseline safeguards. It
does not create evidence, call providers, or claim a certification result.
Human approvals, ratified budgets, and representative operational evidence
remain outside its authority.

Select an evidence root and specification with ``--repo-root`` and ``--spec``.
``--budgets`` selects a separate operator registry; otherwise the selected root's
governance registry is used. Program configuration and telemetry checks run only
when ``--program-config`` and ``--telemetry`` are supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Callable

import yaml


_SECTION_RE = re.compile(r"^#{2,6}\s+(\d+(?:\.\d+)*(?:[a-z])?)(?:\.\s|\s)", re.MULTILINE)
_CITATION_RE = re.compile(r"§(\d+(?:\.\d+)*(?:[a-z])?)")
_VERSION_RE = re.compile(r"^\*\*Version:\*\*\s+([\d.]+)", re.MULTILINE)
_CHANGELOG_VERSION_RE = re.compile(r"^- \*\*v([\d.]+)\s+—", re.MULTILINE)
_HEADING_ID_RE = re.compile(r"^#{3,6}\s+(RELO-[A-Z]\d+(?:\.\d+)?)\b", re.MULTILINE)
_CARD_ID_RE = re.compile(r"^id:\s+(RELO-W\d+\.\d+[a-z])\s*$", re.MULTILINE)
_PARENT_CARD_ID_RE = re.compile(r"^RELO-W\d+\.\d+$")
_CHILD_CARD_ID_RE = re.compile(r"^RELO-W\d+\.\d+[a-z]$")
_PHASE_TABLE_ERROR = "phase work-item definition table is missing or malformed"
_WORK_PACKAGE_STATUSES = frozenset({"planned", "partial", "blocked", "completed", "deferred"})
_W0_STARTABILITY = frozenset({
    "code_now", "completed_local", "blocked_evidence", "blocked_judgment",
})
_EVIDENCE_DECISIONS = ("RELO-D6d", "RELO-D7", "RELO-D13", "RELO-D14")
_EVIDENCE_NUMERIC_CLAIMS: dict[str, dict[str, tuple[float, bool]]] = {
    "RELO-D6d": {
        "shadow_run_count": (4, True),
        "cost_per_certified_artifact_usd": (0, False),
        "cost_per_completed_cycle_usd": (0, False),
        "cost_per_active_program_month_usd": (0, False),
        "unattributed_cost_usd": (0, False),
    },
    "RELO-D7": {
        "baseline_count": (5, True),
        "matched_cycle_count": (8, True),
        "matched_cycle_span_days": (49, True),
        "workflow_class_count": (1, True),
        "confidence_95_lower_bound": (0.8, False),
    },
    "RELO-D14": {
        "cost_per_certified_artifact_ceiling_usd": (0, False),
        "cost_per_completed_cycle_ceiling_usd": (0, False),
        "cost_per_active_program_month_ceiling_usd": (0, False),
    },
}
_EVIDENCE_PROFILE_KEYS = frozenset({
    "program_count",
    "entity_count",
    "relation_count",
    "document_count",
    "history",
    "compiler",
    "storage",
    "queue",
    "latency",
})
_OPEN_ACCEPTANCE_RE = re.compile(
    r"\b(?:not\s+completion\b|(?:the\s+)?acceptance\s+remains?\s+open\b|"
    r"reconcile\b.{0,100}\bacceptance\b.{0,100}\b(?:before|prior to)\b.{0,60}\b(?:closure|completion)\b|"
    r"remains an owner decision\b|nullable[- ]time expansion remains deferred\b|"
    r"no anti[- ]entropy or reseed added;\s*status unchanged\b|"
    r"older[- ]history replay.{0,100}source qualification remain open\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    id: str
    passed: bool
    detail: str


def verify(
    repo_root: Path, *, spec_path: Path, program_config: Path | None = None,
    telemetry_paths: tuple[Path, ...] = (), budgets_path: Path | None = None,
    evidence_packet_paths: tuple[Path, ...] = (), as_of: str | None = None,
) -> tuple[CheckResult, ...]:
    """Evaluate explicit evidence; program and telemetry checks are opt-in."""
    pyproject = repo_root / "pyproject.toml"
    results = []
    if program_config is not None:
        results.append(_check_deployment_vars(program_config))
    results.append(
        _check("RELO-W0.7a", lambda: "not perf" in pyproject.read_text(encoding="utf-8") and '"perf:' in pyproject.read_text(encoding="utf-8"), "The perf lane is named and deselected by default."),
    )
    if telemetry_paths:
        results.append(_check("RELO-W0.10a", lambda: all(_synthetic_count(path) == 0 for path in telemetry_paths), "Selected telemetry sidecars contain no synthetic records."))
    results.extend((
        lint_specification(spec_path),
        lint_work_packages(repo_root, spec_path=spec_path),
        _check_spec_size_budget(spec_path, repo_root=repo_root, budgets_path=budgets_path),
    ))
    if evidence_packet_paths:
        results.extend(_check_evidence_packets(
            repo_root, evidence_packet_paths, as_of=as_of, budgets_path=budgets_path,
        ))
    return tuple(results)


def lint_specification(path: Path) -> CheckResult:
    """Validate RELO document identity and references without editing it."""
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return CheckResult("RELO-SPEC-LINT", False, "Unable to read specification: invalid UTF-8.")
    except OSError:
        return CheckResult("RELO-SPEC-LINT", False, "Unable to read specification: file read failed.")
    findings: list[str] = []
    version_match = _VERSION_RE.search(content)
    changelog_match = _CHANGELOG_VERSION_RE.search(content)
    if version_match is None or changelog_match is None or version_match.group(1) != changelog_match.group(1):
        findings.append("header version does not match latest changelog entry")

    section_ids = _SECTION_RE.findall(content)
    duplicate_sections = _duplicates(section_ids)
    if duplicate_sections:
        findings.append(f"duplicate section number(s): {', '.join(duplicate_sections)}")
    # Historical changelog prose may describe a citation that was repaired in
    # a prior version; only normative body references are resolvable links.
    normative_body = content.split("## Table of Contents", maxsplit=1)[-1]
    unresolved = sorted(set(_CITATION_RE.findall(normative_body)) - set(section_ids))
    if unresolved:
        findings.append(f"unresolved section citation(s): {', '.join(unresolved)}")

    defined_ids = _HEADING_ID_RE.findall(content) + _CARD_ID_RE.findall(content)
    duplicates = _duplicates(defined_ids)
    if duplicates:
        findings.append(f"duplicate defined RELO ID(s): {', '.join(duplicates)}")
    visible_content = _markdown_visible_content(content)
    work_definitions, malformed_phase = _phase_work_item_definitions(visible_content)
    compacted_work_ids = _compacted_work_item_ids(_spec_repo_root(path), set(work_definitions))
    if malformed_phase:
        findings.append(_PHASE_TABLE_ERROR)
    work_ids = set(work_definitions)
    work_ids.update(compacted_work_ids)
    if len(work_definitions) != len(set(work_definitions)):
        findings.append("duplicate work-item definition(s)")
    traceability = re.search(
        r"^### 19\.2\s[^\n]*\n(.*?)(?=^#{1,3}\s|\Z)", visible_content,
        flags=re.MULTILINE | re.DOTALL,
    )
    if traceability is not None:
        rows = _markdown_table_rows(
            traceability.group(1),
            ("Requirement", "Work items", "Gate", "Primary test/evidence", "Phase"),
            exact=True,
        )
        if rows is None:
            findings.append("requirement traceability table is missing or malformed")
            rows = []
        definitions = set(re.findall(
            r"^\|\s*(?:\*\*)?(RELO-(?:FR-\d+|NFR-\d+|W\d+\.\d+[a-z]?|QG-\d+))(?=\s|\*\*|\|)",
            visible_content[:traceability.start()] + visible_content[traceability.end():],
            flags=re.MULTILINE,
        ))
        definitions.update(compacted_work_ids)
        declared_requirements: set[str] = set()
        for line in rows:
            if not re.match(r"^\|\s*RELO-(?:FR|NFR)-\d+\s*\|", line):
                continue
            declared_requirements.add(line.split("|")[1].strip())
            columns = [cell.strip() for cell in line.strip().split("|")[1:-1]]
            if not line.rstrip().endswith("|") or len(columns) != 5 or not all(columns):
                identifier = line.split("|")[1].strip()
                findings.append(f"{identifier}: declared traceability row requires five nonempty columns")
                continue
            phase = re.fullmatch(r"RELO-([0-6])(?:(?:/[0-6])+|[-–]([0-6]))?", columns[4])
            if phase is None or (phase[2] is not None and phase[1] > phase[2]):
                findings.append(f"{columns[0]}: requirement mapping requires valid phase references")
            references = {columns[0]}
            try:
                references.update(_traceability_references(" ".join(columns[1:3]), definitions))
                work_references = _traceability_references(columns[1], definitions)
                if not any(reference.startswith("RELO-W") for reference in work_references):
                    findings.append(f"{columns[0]}: requirement mapping requires a work-item reference")
            except ValueError as error:
                findings.append(f"{columns[0]}: {error}")
            unresolved_references = sorted(references - definitions)
            if unresolved_references:
                findings.append(f"{columns[0]}: unresolved traceability reference(s): {', '.join(unresolved_references)}")
        missing_requirements = sorted(
            identifier for identifier in definitions
            if identifier.startswith(("RELO-FR-", "RELO-NFR-")) and identifier not in declared_requirements
        )
        if missing_requirements:
            findings.append(f"missing requirement traceability row(s): {', '.join(missing_requirements)}")
    findings.extend(_goal_traceability_findings(content, work_ids))
    findings.extend(_complete_traceability_findings(path, visible_content, work_ids))
    findings.extend(_problem_registry_findings(path, visible_content))
    findings.extend(_compaction_archive_findings(path, visible_content))
    findings.extend(_semantic_traceability_findings(path, visible_content))
    return CheckResult(
        "RELO-SPEC-LINT",
        not findings,
        "Specification header, sections, citations, and defined IDs are structurally consistent."
        if not findings
        else "; ".join(findings),
    )


def _has_invalid_traceability_candidates(value: str, *, goal_targets: bool) -> bool:
    if goal_targets:
        candidates = r"W\d+\.\d+\w*|(?:FR|NFR)-\d+\w*|O\d+\w*|F\d+\w*"
        valid = r"W\d+\.\d+[a-z]?|(?:FR|NFR)-\d+|O\d+|F\d+"
    else:
        candidates = r"W\d+\.\d+\w*|QG-\d+\w*"
        valid = r"W\d+\.\d+[a-z]?|QG-\d+"
    return any(
        re.fullmatch(rf"(?:RELO-)?(?:{valid})", match.group()) is None
        for match in re.finditer(rf"(?<![\w-])(?:RELO-)?(?:{candidates})(?:\.+\w+)*", value)
    )


def _traceability_references(
    value: str, definitions: set[str], *, goal_targets: bool = False,
) -> set[str]:
    if not goal_targets and _has_invalid_traceability_candidates(value, goal_targets=False):
        raise ValueError("unsupported traceability reference")
    references: set[str] = set()
    families = (
        r"(?:W\d+\.\d+[a-z]?|(?:FR|NFR)-\d+|O\d+|F\d+)(?![\w])"
        if goal_targets else r"W\d+\.\d+[a-z]?|QG-\d+"
    )
    pattern = rf"(?<![\w-])(?:RELO-)?({families})(?:\s*[-–]\s*(?:RELO-)?({families}))?"
    for match in re.finditer(pattern, value):
        start, end = match.groups()
        if goal_targets and re.match(r"\s*[-–—]", value[match.end():]):
            raise ValueError(f"invalid traceability range after: {match.group(0)}")
        if end is None:
            references.add(f"RELO-{start}")
            continue
        first = re.fullmatch(r"(W\d+\.|QG-|FR-|NFR-|O|F)(\d+)", start)
        last = re.fullmatch(r"(W\d+\.|QG-|FR-|NFR-|O|F)(\d+)", end)
        if first is None or last is None or first[1] != last[1] or int(first[2]) > int(last[2]):
            raise ValueError(f"invalid traceability range: {match.group(0)}")
        for number in range(int(first[2]), int(last[2]) + 1):
            reference = (
                f"RELO-{first[1]}{number:02d}"
                if first[1] in {"QG-", "FR-", "NFR-"} else f"RELO-{first[1]}{number}"
            )
            if reference not in definitions:
                raise ValueError(f"unresolved traceability reference: {reference}")
            references.add(reference)
    return references


def _markdown_visible_content(specification: str) -> str:
    visible_lines = []
    fence: str | None = None
    in_comment = False
    for line in specification.splitlines(keepends=True):
        if fence is not None:
            closing = re.fullmatch(r" {0,3}(`+|~+)[ \t]*\n?", line)
            if closing and closing[1][0] == fence[0] and len(closing[1]) >= len(fence):
                fence = None
            visible_lines.append("\n" if line.endswith("\n") else "")
            continue
        if not in_comment:
            opening = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
            if opening:
                fence = opening[1]
                visible_lines.append("\n" if line.endswith("\n") else "")
                continue
        fragments = []
        remainder = line
        while remainder:
            if in_comment:
                _, marker, remainder = remainder.partition("-->")
                if not marker:
                    break
                fragments.append(" ")
                in_comment = False
            else:
                text, marker, remainder = remainder.partition("<!--")
                fragments.append(text)
                if not marker:
                    break
                fragments.append(" ")
                in_comment = True
        visible_line = "".join(fragments)
        if line.endswith("\n") and not visible_line.endswith("\n"):
            visible_line += "\n"
        visible_lines.append(visible_line)
    return "".join(visible_lines)


def _traceability_section(content: str, number: str, *, phase_heading: bool = False) -> str | None:
    parent, _, child = number.partition(".")
    section = re.search(
        rf"^##[ \t]+{re.escape(parent)}(?:\.[ \t]|[ \t])[^\n]*\n(.*?)(?=^#{{1,2}}[ \t]|\Z)",
        content, re.MULTILINE | re.DOTALL,
    )
    if section is None:
        return None
    if not child:
        return section.group(1)
    heading_suffix = r"\.?(?:[ \t]+[^\n]*)?" if phase_heading else r"[ \t][^\n]*"
    section = re.search(
        rf"^###[ \t]+{re.escape(number)}{heading_suffix}\n(.*?)(?=^#{{1,3}}[ \t]|\Z)",
        section.group(1), re.MULTILINE | re.DOTALL,
    )
    return section.group(1) if section is not None else None


def _markdown_table_rows(
    section: str | None, header: tuple[str, ...], *, exact: bool = False, strict_rows: bool = False,
) -> list[str] | None:
    if section is None:
        return None
    lines = section.splitlines()
    for index, line in enumerate(lines):
        columns = _markdown_table_cells(line)
        if columns is None or tuple(columns[:len(header)]) != header:
            continue
        if (exact and len(columns) != len(header)) or (strict_rows and not all(columns)):
            return None
        if index + 1 >= len(lines):
            return None
        separator = _markdown_table_cells(lines[index + 1])
        if separator is None or len(separator) != len(columns) or any(
            not re.fullmatch(r":?-+:?", cell) for cell in separator
        ):
            return None
        rows = []
        for row in lines[index + 2:]:
            if not row.lstrip().startswith("|"):
                if strict_rows and "|" in row:
                    return None
                break
            if strict_rows:
                row_cells = _markdown_table_cells(row)
                if row_cells is None or len(row_cells) != len(columns) or not all(row_cells):
                    return None
            rows.append(row)
        return rows
    return None


def _markdown_table_cells(line: str) -> list[str] | None:
    text = line.strip()
    if not text.startswith("|") or not text.endswith("|"):
        return None
    return [cell.strip() for cell in text.split("|")[1:-1]]


def _traceability_definition_ids(rows: list[str] | None, pattern: str) -> list[str]:
    if rows is None:
        return []
    identifiers = []
    for row in rows:
        columns = _markdown_table_cells(row)
        match = re.match(rf"(?:\*\*)?({pattern})(?=\s|\*\*|$)", columns[0]) if columns else None
        if match is not None:
            identifiers.append(match.group(1))
    return identifiers


def _phase_work_item_definitions(visible_content: str) -> tuple[list[str], bool]:
    identifiers: list[str] = []
    malformed = False
    for section_number in range(1, 8):
        number = f"11.{section_number}"
        headings = re.findall(
            rf"^###[ \t]+{re.escape(number)}(?:[ \t]|\.(?:[ \t]|$)|$)",
            visible_content, re.MULTILINE,
        )
        if not headings:
            continue
        section = _traceability_section(visible_content, number, phase_heading=True)
        if len(headings) != 1 or section is None:
            malformed = True
            continue
        rows = _markdown_table_rows(section, ("ID", "Work item"), strict_rows=True)
        if rows is None:
            malformed = True
            continue
        for row in rows:
            columns = _markdown_table_cells(row)
            if not columns or len(columns) < 2 or not columns[1]:
                malformed = True
                continue
            match = re.fullmatch(r"(?:\*\*(RELO-W\d+\.\d+[a-z]?)\*\*|(RELO-W\d+\.\d+[a-z]?))", columns[0])
            if match is None:
                malformed = True
                continue
            identifiers.append(match.group(1) or match.group(2))
    return identifiers, malformed


def _goal_traceability_findings(content: str, work_ids: set[str]) -> list[str]:
    visible = _markdown_visible_content(content)
    if re.search(r"^###[ \t]+19\.1[ \t]", visible, re.MULTILINE) is None:
        return []
    trace = _traceability_section(visible, "19.1")
    if trace is None:
        return ["goal traceability table is missing or malformed"]
    rows = _markdown_table_rows(
        trace, ("Goal", "Objectives", "Primary requirements/workstreams"), exact=True,
    )
    if rows is None:
        return ["goal traceability table is missing or malformed"]

    findings: list[str] = []
    goal_rows = _markdown_table_rows(_traceability_section(visible, "3.2"), ("ID", "Goal"))
    objective_rows = _markdown_table_rows(
        _traceability_section(visible, "3.3"), ("ID", "Objective", "Required evidence"),
    )
    goals = _traceability_definition_ids(goal_rows, r"RELO-G\d+")
    objectives = _traceability_definition_ids(objective_rows, r"RELO-O\d+")
    for label, identifiers, definition_rows in (
        ("goal", goals, goal_rows), ("objective", objectives, objective_rows),
    ):
        if not identifiers or definition_rows is None or len(identifiers) != len(definition_rows):
            findings.append(f"{label} definition table is missing or malformed")
        if len(identifiers) != len(set(identifiers)):
            findings.append(f"duplicate {label} definition(s)")

    requirement_ids: set[str] = set()
    requirement_definition_counts: list[str] = []
    for section, pattern in (("5.5", r"RELO-FR-\d+"), ("5.6", r"RELO-NFR-\d+")):
        section_ids = _traceability_definition_ids(
            _markdown_table_rows(_traceability_section(visible, section), ("ID", "Requirement")), pattern,
        )
        requirement_ids.update(section_ids)
        requirement_definition_counts.extend(section_ids)
    duplicate_requirements = _duplicates(requirement_definition_counts)
    if duplicate_requirements:
        findings.append(
            "duplicate requirement definition(s): "
            + ", ".join(duplicate_requirements)
        )
    features = _traceability_section(visible, "8")
    feature_ids = set(re.findall(
        r"^###\s+8\.\d+\s+(RELO-F\d+)\b", features or "", re.MULTILINE,
    ))
    definitions = set(objectives) | requirement_ids | work_ids | feature_ids
    declared_goals: set[str] = set()
    referenced_objectives: set[str] = set()
    referenced_requirements: set[str] = set()
    goal_row_counts: dict[str, int] = {}
    token = r"(?:W\d+\.\d+[a-z]?|(?:FR|NFR)-\d+|O\d+|F\d+)(?![\w])"
    for row in rows:
        columns = _markdown_table_cells(row)
        if columns is None or len(columns) != 3 or not all(columns):
            findings.append("goal traceability row requires three nonempty columns")
            continue
        match = re.fullmatch(r"(?:\*\*(RELO-G\d+)\*\*|(RELO-G\d+))", columns[0])
        if match is None:
            findings.append("goal traceability row has an invalid goal ID")
            continue
        goal = match.group(1) or match.group(2)
        goal_row_counts[goal] = goal_row_counts.get(goal, 0) + 1
        if goal not in goals:
            findings.append(f"{goal}: unresolved goal traceability reference")
        else:
            declared_goals.add(goal)
        for index, label in ((1, "objective"), (2, "requirement")):
            value = columns[index]
            if (
                any(re.match(token, value[marker.end():]) is None for marker in re.finditer(r"\bRELO-", value))
                or _has_invalid_traceability_candidates(value, goal_targets=True)
            ):
                findings.append(f"{goal}: unsupported {label} traceability reference")
            try:
                references = _traceability_references(value, definitions, goal_targets=True)
            except ValueError as error:
                findings.append(f"{goal}: {error}")
                continue
            unresolved = sorted(references - definitions)
            if unresolved:
                findings.append(f"{goal}: unresolved traceability reference(s): {', '.join(unresolved)}")
            if index == 1:
                if not references or any(not ref.startswith("RELO-O") for ref in references):
                    findings.append(f"{goal}: objective mapping requires objective references")
                if goal in goals:
                    referenced_objectives.update(references & set(objectives))
            elif not any(ref.startswith(("RELO-FR-", "RELO-NFR-")) for ref in references):
                findings.append(f"{goal}: requirement mapping requires a requirement reference")
            elif any(ref.startswith("RELO-O") for ref in references):
                findings.append(f"{goal}: requirement mapping contains an objective reference")
            if index == 2:
                referenced_requirements.update(
                    references & requirement_ids
                )

    duplicate_goal_rows = sorted(
        goal for goal, count in goal_row_counts.items() if count > 1
    )
    if duplicate_goal_rows:
        findings.append(
            f"duplicate goal traceability row(s): {', '.join(duplicate_goal_rows)}"
        )
    missing_goals = sorted(set(goals) - declared_goals)
    if missing_goals:
        findings.append(f"missing goal traceability row(s): {', '.join(missing_goals)}")
    missing_objectives = sorted(set(objectives) - referenced_objectives)
    if missing_objectives:
        findings.append(f"unreferenced objective(s): {', '.join(missing_objectives)}")
    missing_requirements = sorted(requirement_ids - referenced_requirements)
    if missing_requirements:
        findings.append(
            "missing goal requirement traceability row(s): "
            + ", ".join(missing_requirements)
        )
    return findings


def _phase_references(value: str) -> set[str]:
    single = re.fullmatch(r"RELO-([0-6])", value)
    if single is not None:
        return {f"RELO-{single[1]}"}
    listed = re.fullmatch(r"RELO-([0-6])((?:/[0-6])+)", value)
    if listed is not None:
        return {
            f"RELO-{number}"
            for number in (listed[1], *listed[2][1:].split("/"))
        }
    ranged = re.fullmatch(r"RELO-([0-6])[-–]([0-6])", value)
    if ranged is None or int(ranged[1]) > int(ranged[2]):
        return set()
    return {
        f"RELO-{number}"
        for number in range(int(ranged[1]), int(ranged[2]) + 1)
    }


def _work_item_phases(spec_path: Path, visible_content: str) -> dict[str, str]:
    phases: dict[str, str] = {}
    for section_number in range(1, 8):
        rows = _markdown_table_rows(
            _traceability_section(
                visible_content, f"11.{section_number}", phase_heading=True,
            ),
            ("ID", "Work item"),
        )
        for row in rows or []:
            cells = _markdown_table_cells(row)
            match = re.fullmatch(
                r"(?:\*\*(RELO-W\d+\.\d+[a-z]?)\*\*|"
                r"(RELO-W\d+\.\d+[a-z]?))",
                cells[0] if cells else "",
            )
            if match is not None:
                phases[match.group(1) or match.group(2)] = f"RELO-{section_number - 1}"
    try:
        ledger = yaml.safe_load(
            (_spec_repo_root(spec_path) / "governance" / "relo-work-packages.yaml")
            .read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return phases
    cards = ledger.get("work_items") if isinstance(ledger, dict) else None
    if isinstance(cards, list):
        for card in cards:
            if not isinstance(card, dict):
                continue
            identifier = card.get("id")
            phase = card.get("phase")
            if (
                isinstance(identifier, str)
                and isinstance(phase, str)
                and re.fullmatch(r"RELO-[0-6]", phase)
            ):
                phases.setdefault(identifier, phase)
    return phases


def _traceability_mapping_missing(value: str) -> bool:
    normalized = re.sub(r"[*`]", "", value).strip().lower().rstrip(".")
    return normalized in {
        "", "-", "—", "n/a", "na", "none", "no gate", "no evidence",
        "pending", "tbd", "unassigned",
    }


def _stale_evidence_paths(
    spec_path: Path, evidence: str,
) -> bool:
    repo_root = _spec_repo_root(spec_path).resolve()
    for match in re.finditer(r"`((?:tests|scripts)/[^`]+?\.(?:py|yaml|json))(?:::[^`]+)?`", evidence):
        prefix = evidence[max(0, match.start() - 24):match.start()]
        if re.search(r"\bplanned\b", prefix, re.IGNORECASE):
            continue
        target = (repo_root / match.group(1)).resolve()
        if not target.is_relative_to(repo_root) or not target.is_file():
            return True
    return False


def _complete_traceability_findings(
    spec_path: Path, visible_content: str, work_ids: set[str],
) -> list[str]:
    strict_complete_traceability = (
        "### 3.2 Goals" in visible_content
        and "### 3.3 Objectives" in visible_content
        and "### 19.1 Goal and objective traceability" in visible_content
        and "### 19.2 Requirement-to-delivery traceability" in visible_content
    )
    if not strict_complete_traceability:
        return []
    goal_rows = _markdown_table_rows(
        _traceability_section(visible_content, "19.1"),
        ("Goal", "Objectives", "Primary requirements/workstreams"),
        exact=True,
    )
    requirement_rows = _markdown_table_rows(
        _traceability_section(visible_content, "19.2"),
        ("Requirement", "Work items", "Gate", "Primary test/evidence", "Phase"),
        exact=True,
    )
    goal_definition_rows = _markdown_table_rows(
        _traceability_section(visible_content, "3.2"), ("ID", "Goal"),
    )
    objective_definition_rows = _markdown_table_rows(
        _traceability_section(visible_content, "3.3"),
        ("ID", "Objective", "Required evidence"),
    )
    missing_tables = [
        name
        for name, rows in (
            ("goal mapping", goal_rows),
            ("requirement mapping", requirement_rows),
            ("goal definition", goal_definition_rows),
            ("objective definition", objective_definition_rows),
        )
        if rows is None
    ]
    if missing_tables:
        return [
            f"{name} traceability table is missing or malformed"
            for name in missing_tables
        ]
    assert goal_rows is not None
    assert requirement_rows is not None
    assert goal_definition_rows is not None
    assert objective_definition_rows is not None

    goals = set(_traceability_definition_ids(goal_definition_rows, r"RELO-G\d+"))
    objectives = set(
        _traceability_definition_ids(objective_definition_rows, r"RELO-O\d+"),
    )
    requirements: set[str] = set()
    for section_number, pattern in (
        ("5.5", r"RELO-FR-\d+"), ("5.6", r"RELO-NFR-\d+"),
    ):
        requirements.update(_traceability_definition_ids(
            _markdown_table_rows(
                _traceability_section(visible_content, section_number),
                ("ID", "Requirement"),
            ),
            pattern,
        ))
    missing_definitions = [
        name
        for name, values in (
            ("canonical goal", goals),
            ("canonical objective", objectives),
            ("canonical requirement", requirements),
        )
        if not values
    ]
    if missing_definitions:
        return [
            f"{name} traceability definitions are missing"
            for name in missing_definitions
        ]

    features_section = _traceability_section(visible_content, "8") or ""
    features = set(re.findall(
        r"^###\s+8\.\d+\s+(RELO-F\d+)\b",
        features_section,
        re.MULTILINE,
    ))
    work_phases = _work_item_phases(spec_path, visible_content)
    findings: list[str] = []
    complete_requirements: set[str] = set()
    row_counts: dict[str, int] = {}
    for row in requirement_rows:
        cells = _markdown_table_cells(row)
        if cells is None or len(cells) != 5 or not all(cells):
            continue
        requirement = cells[0]
        if requirement not in requirements:
            continue
        row_counts[requirement] = row_counts.get(requirement, 0) + 1
        complete = True
        if _traceability_mapping_missing(cells[2]):
            findings.append(f"{requirement}: requirement mapping is missing gate")
            complete = False
        if _traceability_mapping_missing(cells[3]):
            findings.append(f"{requirement}: requirement mapping is missing evidence")
            complete = False
        if _stale_evidence_paths(spec_path, cells[3]):
            findings.append(f"{requirement}: requirement mapping has stale test/evidence path")
            complete = False
        try:
            references = _traceability_references(cells[1], work_ids)
        except ValueError:
            complete = False
            references = set()
        declared_phases = _phase_references(cells[4])
        actual_phases = {
            work_phases[reference]
            for reference in references
            if reference in work_phases
        }
        if (
            not declared_phases
            or not actual_phases
            or any(reference not in work_phases for reference in references)
            or declared_phases != actual_phases
        ):
            findings.append(f"{requirement}: requirement mapping has work-item phase mismatch")
            complete = False
        if complete:
            complete_requirements.add(requirement)
    for requirement in sorted(requirements - complete_requirements):
        if requirement not in row_counts:
            findings.append(f"{requirement}: canonical requirement has no complete delivery mapping")
    for requirement, count in sorted(row_counts.items()):
        if count != 1:
            findings.append(f"{requirement}: duplicate canonical traceability requirement row")
            complete_requirements.discard(requirement)

    goal_requirements: dict[str, set[str]] = {}
    objective_goals: dict[str, set[str]] = {}
    for row in goal_rows:
        cells = _markdown_table_cells(row)
        if cells is None or len(cells) != 3 or not all(cells):
            continue
        goal_match = re.fullmatch(
            r"(?:\*\*(RELO-G\d+)\*\*|(RELO-G\d+))", cells[0],
        )
        if goal_match is None:
            continue
        goal = goal_match.group(1) or goal_match.group(2)
        if goal not in goals:
            continue
        try:
            goal_references = _traceability_references(
                " ".join(cells[1:]),
                requirements | objectives | work_ids | features,
                goal_targets=True,
            )
        except ValueError:
            continue
        mapped_requirements = goal_references & requirements
        goal_requirements.setdefault(goal, set()).update(mapped_requirements)
        for objective in goal_references & objectives:
            objective_goals.setdefault(objective, set()).add(goal)
    for goal in sorted(goals):
        if not goal_requirements.get(goal):
            findings.append(f"{goal}: canonical goal is not mapped to a delivery requirement")
        elif goal_requirements[goal] - complete_requirements:
            findings.append(f"{goal}: canonical goal has an incomplete delivery mapping")
    for objective in sorted(objectives):
        owners = objective_goals.get(objective, set())
        reachable = {
            requirement
            for goal in owners
            for requirement in goal_requirements.get(goal, set())
            if requirement in complete_requirements
        }
        if not owners or not reachable:
            findings.append(
                f"{objective}: canonical objective has no complete bidirectional mapping",
            )
    return findings


def _decision_table_ids(specification: str) -> set[str]:
    section = re.search(
        r"^##[ \t]+20\.[ \t]+[^\n]*\n(.*?)(?=^#{1,2}[ \t]+|\Z)",
        _markdown_visible_content(specification), re.MULTILINE | re.DOTALL,
    )
    if section is None:
        return set()
    table = re.search(
        r"^\|[ \t]*ID[ \t]*\|[ \t]*Decision[ \t]*\|[^\n]*\n"
        r"\|[ \t]*:?-+:?[ \t]*\|[ \t]*:?-+:?[ \t]*\|(?:[ \t]*:?-+:?[ \t]*\|)*[ \t]*\n"
        r"((?:\|[^\n]*(?:\n|\Z))*)",
        section.group(1), re.MULTILINE,
    )
    if table is None:
        return set()
    return {
        bold or plain
        for bold, plain in re.findall(
            r"^\|[ \t]*(?:\*\*(RELO-D\d+[a-z]?)\*\*|(RELO-D\d+[a-z]?))[ \t]*\|",
            table.group(1), re.MULTILINE,
        )
    }


def _spec_repo_root(spec_path: Path) -> Path:
    return spec_path.parent.parent if spec_path.parent.name == "specs" else spec_path.parent


def _validated_compacted_definition(
    card: dict, *, parent_ids: set[str],
) -> bool:
    identifier = card.get("id")
    definition = card.get("definition")
    if not isinstance(identifier, str) or not _CHILD_CARD_ID_RE.fullmatch(identifier):
        return False
    if not isinstance(definition, dict):
        return False
    parent_id = definition.get("parent_id")
    row = definition.get("row")
    row_hash = definition.get("row_sha256")
    if (
        not isinstance(parent_id, str)
        or parent_id != identifier[:-1]
        or (
            parent_id not in parent_ids
            and definition.get("parent_status") != "rootless"
        )
        or not isinstance(row, str)
        or not isinstance(row_hash, str)
    ):
        return False
    cells = _markdown_table_cells(row)
    return (
        cells is not None
        and 2 <= len(cells) <= 4
        and all(cells)
        and cells[0] == identifier
        and hashlib.sha256(row.encode("utf-8")).hexdigest() == row_hash
    )


def _compacted_work_item_ids(repo_root: Path, parent_ids: set[str]) -> set[str]:
    try:
        document = yaml.safe_load(
            (repo_root / "governance" / "relo-work-packages.yaml").read_text(encoding="utf-8"),
        )
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return set()
    cards = document.get("work_items") if isinstance(document, dict) else None
    if not isinstance(cards, list):
        return set()
    return {
        card["id"] for card in cards
        if isinstance(card, dict) and _validated_compacted_definition(card, parent_ids=parent_ids)
    }


def _problem_registry_findings(spec_path: Path, visible_content: str) -> list[str]:
    marker = "Canonical text: `governance/relo-problem-statements.yaml`."
    if marker not in visible_content:
        return []
    registry_path = _spec_repo_root(spec_path) / "governance" / "relo-problem-statements.yaml"
    try:
        document = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return ["problem registry is not valid UTF-8"]
    except OSError:
        return ["problem registry is unavailable"]
    except yaml.YAMLError:
        return ["problem registry is not valid YAML"]
    if not isinstance(document, dict) or document.get("schema_version") != "1":
        return ["problem registry schema_version must be '1'"]
    expected_count = document.get("expected_count")
    problems = document.get("problems")
    if not isinstance(expected_count, int) or expected_count < 1:
        return ["problem registry expected_count must be a positive integer"]
    if not isinstance(problems, list):
        return ["problem registry problems must be a list"]
    findings: list[str] = []
    identifiers: list[str] = []
    for position, problem in enumerate(problems, start=1):
        if not isinstance(problem, dict):
            findings.append(f"problem registry entry {position} must be a mapping")
            continue
        identifier = problem.get("id")
        markdown = problem.get("markdown")
        markdown_hash = problem.get("markdown_sha256")
        if not isinstance(identifier, str) or re.fullmatch(r"RELO-P\d+", identifier) is None:
            findings.append(f"problem registry entry {position} has invalid id")
            continue
        identifiers.append(identifier)
        if (
            not isinstance(markdown, str)
            or not isinstance(markdown_hash, str)
            or not markdown.startswith(f"#### {identifier} ")
        ):
            findings.append(f"{identifier}: problem registry definition is malformed")
            continue
        if hashlib.sha256(markdown.encode("utf-8")).hexdigest() != markdown_hash:
            findings.append(f"{identifier}: problem registry definition hash mismatch")
    duplicates = _duplicates(identifiers)
    if duplicates:
        findings.append(f"duplicate problem registry ID(s): {', '.join(duplicates)}")
    if len(problems) != expected_count or len(set(identifiers)) != expected_count:
        findings.append("problem registry expected_count does not match unique definitions")
    defined = set(identifiers)
    if sorted(int(identifier.removeprefix("RELO-P")) for identifier in identifiers) != list(
        range(1, expected_count + 1),
    ):
        findings.append("problem registry IDs must be contiguous from RELO-P1")
    referenced = set(re.findall(r"\bRELO-P\d+\b", visible_content))
    unresolved = sorted(referenced - defined)
    if unresolved:
        findings.append(f"unresolved problem registry reference(s): {', '.join(unresolved)}")
    return findings


def _compaction_archive_findings(spec_path: Path, visible_content: str) -> list[str]:
    marker = "`.archive/specs/relo-history-through-v3.247.md`"
    if marker not in visible_content:
        return []
    archive_path = _spec_repo_root(spec_path) / ".archive" / "specs" / "relo-history-through-v3.247.md"
    try:
        content = archive_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ["compaction archive is not valid UTF-8"]
    except OSError:
        return ["compaction archive is unavailable"]
    lines = content.splitlines()
    metadata = {
        key: (int(match.group(1)) if key.endswith("_lines") else match.group(1))
        for key, pattern in {
            "history_lines": r"^\*\*History lines:\*\* (\d+)$",
            "history_sha256": r"^\*\*History SHA-256:\*\* ([0-9a-f]{64})$",
            "toc_lines": r"^\*\*TOC lines:\*\* (\d+)$",
            "toc_sha256": r"^\*\*TOC SHA-256:\*\* ([0-9a-f]{64})$",
        }.items()
        if (match := re.search(pattern, content, re.MULTILINE)) is not None
    }
    if set(metadata) != {"history_lines", "history_sha256", "toc_lines", "toc_sha256"}:
        return ["compaction archive metadata is missing or malformed"]
    try:
        history_heading = lines.index("## Changelog through v3.247")
        toc_heading = lines.index("## Previous table of contents")
    except ValueError:
        return ["compaction archive section headings are missing"]
    history = lines[history_heading + 1:toc_heading]
    toc = lines[toc_heading + 1:]
    if history and history[0] == "":
        history = history[1:]
    if history and history[-1] == "":
        history = history[:-1]
    if toc and toc[0] == "":
        toc = toc[1:]
    findings: list[str] = []
    if len(history) != metadata["history_lines"]:
        findings.append("archived history line count mismatch")
    elif hashlib.sha256(("\n".join(history) + "\n").encode("utf-8")).hexdigest() != metadata["history_sha256"]:
        findings.append("archived history hash mismatch")
    if len(toc) != metadata["toc_lines"]:
        findings.append("archived table-of-contents line count mismatch")
    elif hashlib.sha256(("\n".join(toc) + "\n").encode("utf-8")).hexdigest() != metadata["toc_sha256"]:
        findings.append("archived table-of-contents hash mismatch")
    return findings


def lint_work_packages(repo_root: Path, *, spec_path: Path) -> CheckResult:
    """Validate RELO readiness records without interpreting their design.

    This enforces the issuance rules that are mechanically knowable: coverage,
    unique IDs, valid classes and acyclic dependencies, resolving AGENT_READY
    paths, and named §20 blockers for NEEDS_DECISION work. It never upgrades a card.
    """
    work_packages_path = repo_root / "governance" / "relo-work-packages.yaml"
    try:
        document = yaml.safe_load(work_packages_path.read_text(encoding="utf-8"))
        specification = spec_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return CheckResult("RELO-WORK-PACKAGES", False, "Unable to read work-package evidence: invalid UTF-8.")
    except yaml.YAMLError:
        return CheckResult("RELO-WORK-PACKAGES", False, "Unable to read work-package evidence: invalid YAML.")
    except OSError:
        return CheckResult("RELO-WORK-PACKAGES", False, "Unable to read work-package evidence: file read failed.")
    if not isinstance(document, dict) or document.get("schema_version") != "1":
        return CheckResult("RELO-WORK-PACKAGES", False, "work-package schema_version must be '1'")
    cards = document.get("work_items")
    if not isinstance(cards, list):
        return CheckResult("RELO-WORK-PACKAGES", False, "work_items must be a list")

    work_definitions, malformed_phase = _phase_work_item_definitions(_markdown_visible_content(specification))
    if malformed_phase:
        return CheckResult("RELO-WORK-PACKAGES", False, _PHASE_TABLE_ERROR)
    parent_ids = set(work_definitions)
    expected = set(parent_ids)
    ids = [card.get("id") for card in cards if isinstance(card, dict)]
    findings: list[str] = []
    for position, card in enumerate(cards, start=1):
        if not isinstance(card, dict):
            continue
        identifier = card.get("id")
        if isinstance(identifier, str) and _CHILD_CARD_ID_RE.fullmatch(identifier):
            if _validated_compacted_definition(card, parent_ids=parent_ids):
                expected.add(identifier)
            else:
                findings.append(f"{identifier}: invalid compacted definition at entry {position}")
    duplicates = set(_duplicates([identifier for identifier in ids if isinstance(identifier, str)]))
    if duplicates:
        positions = [
            str(position) for position, card in enumerate(cards, start=1)
            if isinstance(card, dict) and isinstance(card.get("id"), str) and card["id"] in duplicates
        ]
        findings.append(f"duplicate card ID(s) at entries: {', '.join(positions)}")
    actual = {identifier for identifier in ids if isinstance(identifier, str)}
    missing = sorted(expected - actual)
    extra = actual - expected
    if missing:
        findings.append(f"missing work-package card(s): {', '.join(missing)}")
    if extra:
        positions = [
            str(position) for position, card in enumerate(cards, start=1)
            if isinstance(card, dict) and isinstance(card.get("id"), str) and card["id"] in extra
        ]
        findings.append(f"unknown work-package card(s) at entries: {', '.join(positions)}")

    decision_ids = _decision_table_ids(specification)
    allowed_classes = {"AGENT_READY", "NEEDS_DECISION", "NEEDS_JUDGMENT"}
    dependency_graph: dict[str, list[str]] = {}
    for position, card in enumerate(cards, start=1):
        if not isinstance(card, dict):
            findings.append("work-package card is not a mapping")
            continue
        identifier = card.get("id")
        dependencies = card.get("depends_on")
        if isinstance(identifier, str) and identifier and isinstance(dependencies, list):
            dependency_graph[identifier] = [
                dependency for dependency in dependencies
                if isinstance(dependency, str) and dependency in actual
            ]
        if not isinstance(identifier, str) or not identifier:
            findings.append("work-package card has invalid id")
            identifier = "<invalid id>"
        elif identifier not in expected:
            identifier = f"<unknown card at entry {position}>"
        status = card.get("status")
        if not isinstance(status, str) or status not in _WORK_PACKAGE_STATUSES:
            findings.append(f"{identifier}: invalid status (must be one of {', '.join(sorted(_WORK_PACKAGE_STATUSES))})")
        elif status == "completed":
            completion_text = " ".join(
                value for field in ("evidence", "rationale")
                if isinstance((value := card.get(field)), str)
            )
            if _OPEN_ACCEPTANCE_RE.search(completion_text):
                findings.append(f"{identifier}: completed status contradicts open acceptance evidence")
        if card.get("current_parent") is True and not (
            isinstance(identifier, str)
            and re.fullmatch(r"RELO-W0\.\d+", identifier)
        ):
            findings.append(f"{identifier}: current_parent must identify an unsuffixed RELO-0 parent")
        if card.get("current_slice") is True:
            startability = card.get("startability")
            if not isinstance(startability, str) or startability not in _W0_STARTABILITY:
                findings.append(f"{identifier}: current_slice requires valid startability")
            definition = card.get("definition")
            if (
                not isinstance(definition, dict)
                or definition.get("parent_id") not in {
                    "RELO-W0.12", "RELO-W0.13", "RELO-W0.14",
                }
            ):
                findings.append(f"{identifier}: current_slice requires an assigned W0 parent")
        card_class = card.get("class")
        if not isinstance(card_class, str) or card_class not in allowed_classes:
            detail = "(not an allowed class)" if isinstance(card_class, str) else "(must be a string)"
            findings.append(f"{identifier}: invalid class {detail}")
            continue
        if card.get("current_slice") is True:
            startability = card.get("startability")
            compatible = isinstance(startability, str) and {
                "code_now": card_class == "AGENT_READY" and status == "planned",
                "completed_local": card_class == "AGENT_READY" and status == "completed",
                "blocked_evidence": (
                    card_class == "NEEDS_DECISION"
                    and status in {"partial", "blocked"}
                ),
                "blocked_judgment": (
                    card_class == "NEEDS_JUDGMENT"
                    and status in {"planned", "partial", "blocked", "deferred"}
                ),
            }.get(startability, False)
            if not compatible:
                findings.append(f"{identifier}: startability is incompatible with class/status")
        if not isinstance(dependencies, list) or any(
            not isinstance(dependency, str) or dependency not in actual for dependency in dependencies
        ):
            findings.append(f"{identifier}: unresolved depends_on")
        if card_class == "NEEDS_DECISION":
            decisions = card.get("blocking_decisions")
            if not isinstance(decisions, list) or not decisions or any(
                not isinstance(decision, str) or decision not in decision_ids for decision in decisions
            ):
                findings.append(f"{identifier}: NEEDS_DECISION must name existing §20 decision(s)")
        if card_class == "AGENT_READY":
            for required in ("read_first", "change", "acceptance", "forbidden", "rollback"):
                if not card.get(required):
                    findings.append(f"{identifier}: AGENT_READY is missing {required}")
            read_first = card.get("read_first", [])
            if not isinstance(read_first, list) or len(read_first) > 3:
                findings.append(f"{identifier}: read_first must contain one to three paths")
            if not isinstance(read_first, list):
                read_first = []
            change = card.get("change", [])
            if not isinstance(change, list):
                findings.append(f"{identifier}: change must be a list of paths")
                change = []
            root = Path(os.path.abspath(repo_root))
            resolved_root = Path(os.path.realpath(root))
            for field, paths in (("read_first", read_first), ("change", change)):
                for position, relative_path in enumerate(paths, start=1):
                    unresolved = f"{identifier}: unresolved file path in {field} at entry {position}"
                    if not isinstance(relative_path, str):
                        findings.append(unresolved)
                        continue
                    try:
                        candidate = root / relative_path
                        lexical = Path(os.path.abspath(candidate))
                        if not lexical.is_relative_to(root) and not lexical.is_relative_to(resolved_root):
                            findings.append(f"{identifier}: {field} path is outside selected repository")
                            continue
                        resolved = Path(os.path.realpath(candidate, strict=True))
                        if not resolved.is_relative_to(resolved_root):
                            findings.append(f"{identifier}: {field} path is outside selected repository")
                        elif not resolved.is_file():
                            findings.append(unresolved)
                    except (OSError, ValueError):
                        findings.append(unresolved)
    if not duplicates:
        try:
            TopologicalSorter(dependency_graph).prepare()
        except CycleError:
            findings.append("work-package dependency graph contains a cycle")
    return CheckResult(
        "RELO-WORK-PACKAGES",
        not findings,
        "All RELO-0/1 work items have a valid readiness record with mechanically checkable blockers and paths."
        if not findings
        else "; ".join(findings),
    )


def _duplicates(values: list[str]) -> list[str]:
    return sorted({value for value in values if values.count(value) > 1})


def _check(identifier: str, predicate: Callable[[], bool], success: str) -> CheckResult:
    try:
        passed = predicate()
    except UnicodeDecodeError:
        return CheckResult(identifier, False, "Unable to read baseline evidence: invalid UTF-8.")
    except OSError:
        return CheckResult(identifier, False, "Unable to read baseline evidence: file read failed.")
    return CheckResult(identifier, passed, success if passed else f"{success} Check failed.")


def _check_deployment_vars(program_path: Path) -> CheckResult:
    """RELO-W0.9a: the selected `ai:` block must reference the documented
    `VERTEX_AI_DEPLOYMENT` / `VERTEX_EXEC_DEPLOYMENT` variables, not a
    hardcoded deployment name. A prior plain-text substring check missed a
    YAML indentation defect that broke parsing entirely; this parses the
    file so a structurally invalid or silently-wrong `ai:` block fails loudly.
    """
    identifier = "RELO-W0.9a"
    success = "Selected program uses documented VERTEX deployment variables."
    try:
        document = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return CheckResult(identifier, False, f"{success} Check failed: selected program is not valid YAML.")
    except UnicodeDecodeError:
        return CheckResult(identifier, False, f"{success} Check failed: selected program has invalid UTF-8.")
    except OSError:
        return CheckResult(identifier, False, f"{success} Check failed: Unable to read selected program configuration.")
    ai_block = document.get("ai") if isinstance(document, dict) else None
    if not isinstance(ai_block, dict):
        return CheckResult(identifier, False, f"{success} Check failed: no `ai:` mapping found.")
    expected = {
        "blurb_deployment": "${VERTEX_AI_DEPLOYMENT}",
        "exec_summary_deployment": "${VERTEX_EXEC_DEPLOYMENT}",
    }
    mismatches = [key for key, want in expected.items() if ai_block.get(key) != want]
    if mismatches:
        return CheckResult(identifier, False, f"{success} Check failed: unexpected values for {', '.join(mismatches)}.")
    return CheckResult(identifier, True, success)


def _spec_obligation_count(content: str) -> int:
    """RELO-W0.12b: count RELO-P/F/W/D obligation IDs the same way the
    ratified `relo-spec-obligation-count-budget` was measured. Uses the
    original non-bold table-row pattern used for baseline measurement.
    """
    w_ids = set(re.findall(r"^\|\s+(RELO-W\d+\.\d+[a-z]?)\s+\|", content, re.MULTILINE))
    p_ids = set(re.findall(r"^#### (RELO-P\d+)", content, re.MULTILINE))
    d_ids = set(re.findall(r"^\|\s+\*\*(RELO-D\d+)\*\*", content, re.MULTILINE))
    f_ids = set(re.findall(r"^### \d+\.\d+ (RELO-F\d+)", content, re.MULTILINE))
    return len(w_ids) + len(p_ids) + len(d_ids) + len(f_ids)


def _check_spec_size_budget(spec_path: Path, *, repo_root: Path, budgets_path: Path | None = None) -> CheckResult:
    """RELO-W0.12b: enforce the ratified line-count/obligation-count ceilings
    from the selected registry. Missing, candidate or valueless ceilings are
    not assessed; a read-only verifier cannot itself ratify a budget.
    """
    identifier = "RELO-SPEC-SIZE-BUDGET"
    try:
        content = spec_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return CheckResult(identifier, False, "Unable to read specification: invalid UTF-8.")
    except OSError:
        return CheckResult(identifier, False, "Unable to read specification: file read failed.")

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from src.core.nfr_budgets import load_nfr_budgets
    except ImportError:
        return CheckResult(identifier, False, "Unable to load NFR budget registry: loader unavailable.")

    budgets_path = budgets_path if budgets_path is not None else repo_root / "governance" / "nfr-budgets.yaml"
    if not budgets_path.exists():
        return CheckResult(identifier, False, "No NFR budget registry present; spec size ceilings cannot be assessed.")
    try:
        budgets = {b.id: b for b in load_nfr_budgets(budgets_path)}
    except UnicodeDecodeError:
        return CheckResult(identifier, False, "Unable to load NFR budget registry: invalid UTF-8.")
    except yaml.YAMLError:
        return CheckResult(identifier, False, "Unable to load NFR budget registry: invalid YAML.")
    except OSError:
        return CheckResult(identifier, False, "Unable to load NFR budget registry: file read failed.")
    except Exception:  # noqa: BLE001 - surface any registry load failure as a finding
        return CheckResult(identifier, False, "Unable to load NFR budget registry: check failed.")

    line_count = len(content.splitlines())
    obligation_count = _spec_obligation_count(content)

    findings: list[str] = []
    line_budget = budgets.get("relo-spec-line-count-budget")
    obligation_budget = budgets.get("relo-spec-obligation-count-budget")
    if any(budget is None or not budget.is_ratified or budget.value is None for budget in (line_budget, obligation_budget)):
        return CheckResult(identifier, False, "Spec size not assessed: both ratified size ceilings with values are required.")
    if line_budget is not None and line_budget.is_ratified and line_budget.value is not None:
        if line_count > line_budget.value:
            findings.append(f"line count {line_count} exceeds ratified ceiling {line_budget.value}")
    if obligation_budget is not None and obligation_budget.is_ratified and obligation_budget.value is not None:
        if obligation_count > obligation_budget.value:
            findings.append(f"obligation count {obligation_count} exceeds ratified ceiling {obligation_budget.value}")

    detail = f"Selected specification is {line_count} lines with {obligation_count} obligation IDs, within the selected ratified ceilings."
    return CheckResult(identifier, not findings, detail if not findings else "; ".join(findings))


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed.astimezone(timezone.utc)


def _load_evidence_packet(path: Path) -> object:
    verifier_repo = Path(__file__).resolve().parents[1]
    if str(verifier_repo) not in sys.path:
        sys.path.insert(0, str(verifier_repo))
    from src.core.yaml_utils import UniqueKeySafeLoader

    return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeySafeLoader)


def _load_evidence_cards(repo_root: Path) -> dict[str, set[str]] | None:
    try:
        document = yaml.safe_load(
            (repo_root / "governance" / "relo-work-packages.yaml").read_text(encoding="utf-8"),
        )
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    cards = document.get("work_items") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != "1"
        or not isinstance(cards, list)
    ):
        return None
    linked: dict[str, set[str]] = {
        decision: set() for decision in _EVIDENCE_DECISIONS
    }
    for card in cards:
        if not isinstance(card, dict):
            return None
        identifier = card.get("id")
        blocking = card.get("blocking_decisions", [])
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(blocking, list)
            or any(not isinstance(decision, str) for decision in blocking)
        ):
            return None
        for decision in blocking:
            if decision in linked:
                linked[decision].add(identifier)
    return linked


def _load_ratified_budget_ids(
    repo_root: Path, *, budgets_path: Path | None,
) -> set[str] | None:
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from src.core.exceptions import ConfigError
        from src.core.nfr_budgets import load_nfr_budgets
    except ImportError:
        return None
    try:
        selected = budgets_path or repo_root / "governance" / "nfr-budgets.yaml"
        return {
            budget.id for budget in load_nfr_budgets(selected) if budget.is_ratified
        }
    except (ConfigError, OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def _claim_value_findings(decision: str, claims: dict[str, object]) -> list[str]:
    findings: list[str] = []
    for claim, (minimum, integer_only) in _EVIDENCE_NUMERIC_CLAIMS.get(decision, {}).items():
        value = claims.get(claim)
        if integer_only:
            valid = isinstance(value, int) and not isinstance(value, bool) and value >= minimum
        else:
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= minimum
            )
        if claim == "confidence_95_lower_bound" and valid:
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value > minimum
                and value <= 1.0
            )
        if not valid:
            findings.append(f"claim {claim} is missing or invalid")
    if decision == "RELO-D13":
        for claim in ("two_program_profile", "maximum_profile"):
            profile = claims.get(claim)
            if not isinstance(profile, dict) or not _EVIDENCE_PROFILE_KEYS.issubset(profile):
                findings.append(f"claim {claim} is missing required scale dimensions")
                continue
            if isinstance(profile, dict):
                count_keys = ("program_count", "entity_count", "relation_count", "document_count")
                valid_counts = all(
                    isinstance(profile.get(key), int)
                    and not isinstance(profile.get(key), bool)
                    and profile[key] >= 0
                    for key in count_keys
                )
                valid_complex = all(
                    isinstance(profile.get(key), dict) and bool(profile[key])
                    for key in ("history", "compiler", "storage", "queue", "latency")
                )
                expected_programs = profile.get("program_count") == 2 if claim == "two_program_profile" else (
                    isinstance(profile.get("program_count"), int)
                    and profile.get("program_count", 0) >= 2
                )
            if not valid_counts or not valid_complex:
                findings.append(f"claim {claim} is missing required scale dimensions")
            elif not expected_programs:
                findings.append("claim two_program_profile must describe exactly two programs")
        for claim in ("next_program_admission_tripwire", "separate_architecture_tripwire"):
            tripwire = claims.get(claim)
            if (
                not isinstance(tripwire, dict)
                or not tripwire
                or any(value is None for value in tripwire.values())
            ):
                findings.append(f"claim {claim} is missing or invalid")
    return findings


def _validate_evidence_packet(
    repo_root: Path,
    document: object,
    *,
    expected_cards: set[str],
    as_of: datetime,
) -> tuple[str | None, str | None, list[str]]:
    if not isinstance(document, dict) or document.get("schema_version") != "1":
        return None, None, ["packet schema_version must be '1'"]
    packet_id = document.get("packet_id")
    decision = document.get("decision_id")
    if not isinstance(packet_id, str) or not packet_id.strip():
        return None, decision if isinstance(decision, str) else None, ["packet_id is missing"]
    if decision not in _EVIDENCE_DECISIONS:
        return packet_id, None, ["decision_id is unsupported"]
    findings: list[str] = []
    card_ids = document.get("card_ids")
    if (
        not isinstance(card_ids, list)
        or any(not isinstance(identifier, str) for identifier in card_ids)
        or len(card_ids) != len(set(card_ids))
    ):
        findings.append("card_ids must be a unique string list")
    elif set(card_ids) != expected_cards:
        findings.append("card_ids do not match current ledger blockers")
    captured_at = _parse_utc_timestamp(document.get("captured_at"))
    valid_until = _parse_utc_timestamp(document.get("valid_until"))
    observed_from = _parse_utc_timestamp(document.get("observed_from"))
    observed_through = _parse_utc_timestamp(document.get("observed_through"))
    if (
        captured_at is None
        or valid_until is None
        or observed_from is None
        or observed_through is None
    ):
        findings.append("packet timestamps must be explicit UTC values")
    elif (
        observed_from > observed_through
        or observed_through > captured_at
        or captured_at >= valid_until
        or as_of < captured_at
        or as_of >= valid_until
    ):
        findings.append("packet timestamps are inconsistent or stale")

    artifacts = document.get("artifacts")
    artifact_ids: set[str] = set()
    if not isinstance(artifacts, list) or not artifacts:
        findings.append("artifacts must be a non-empty list")
        artifacts = []
    root = Path(os.path.realpath(repo_root))
    for position, artifact in enumerate(artifacts, start=1):
        if not isinstance(artifact, dict):
            findings.append(f"artifact entry {position} is malformed")
            continue
        artifact_id = artifact.get("id")
        relative = artifact.get("path")
        expected_hash = artifact.get("sha256")
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or artifact_id in artifact_ids
            or not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or not isinstance(expected_hash, str)
        ):
            findings.append(f"artifact entry {position} is malformed or duplicated")
            continue
        artifact_ids.add(artifact_id)
        try:
            resolved = Path(os.path.realpath(root / relative, strict=True))
            if not resolved.is_relative_to(root) or not resolved.is_file():
                findings.append(f"artifact entry {position} is outside or unavailable")
                continue
            actual_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except (OSError, ValueError):
            findings.append(f"artifact entry {position} is unavailable")
            continue
        if actual_hash != expected_hash:
            findings.append(f"artifact entry {position} hash mismatch")

    claims = document.get("claims")
    claim_values: dict[str, object] = {}
    claim_ids: set[str] = set()
    if not isinstance(claims, list) or not claims:
        findings.append("claims must be a non-empty list")
        claims = []
    for position, claim in enumerate(claims, start=1):
        if not isinstance(claim, dict):
            findings.append(f"claim entry {position} is malformed")
            continue
        claim_id = claim.get("id")
        sources = claim.get("source_artifact_ids")
        if (
            not isinstance(claim_id, str)
            or not claim_id
            or claim_id in claim_ids
            or not isinstance(sources, list)
            or not sources
            or any(not isinstance(source, str) for source in sources)
            or not set(sources).issubset(artifact_ids)
            or "value" not in claim
        ):
            findings.append(f"claim entry {position} is malformed, duplicated or unlinked")
            continue
        claim_ids.add(claim_id)
        claim_values[claim_id] = claim["value"]
    findings.extend(_claim_value_findings(decision, claim_values))

    decision_record = document.get("decision")
    if not isinstance(decision_record, dict) or decision_record.get("state") not in {
        "pending", "submitted", "ratified",
    }:
        findings.append("decision state is missing or invalid")
    elif decision_record["state"] == "ratified":
        if (
            _parse_utc_timestamp(decision_record.get("ratified_at")) is None
            or not isinstance(decision_record.get("ratified_by"), str)
            or not decision_record["ratified_by"].strip()
        ):
            findings.append("ratified decision lacks UTC time or named approver")
    return packet_id, decision, findings


def _budget_reconciliation_findings(
    document: dict, *, expected_ids: set[str],
) -> list[str]:
    reconciliations = document.get("budget_reconciliations")
    if not isinstance(reconciliations, list):
        return ["budget reconciliation matrix is missing or malformed"]
    artifact_ids = {
        artifact.get("id")
        for artifact in document.get("artifacts", [])
        if isinstance(artifact, dict) and isinstance(artifact.get("id"), str)
    }
    reconciled_ids: list[str] = []
    findings: list[str] = []
    for position, row in enumerate(reconciliations, start=1):
        if not isinstance(row, dict):
            findings.append(f"budget reconciliation row {position} is malformed")
            continue
        budget_id = row.get("budget_id")
        disposition = row.get("disposition")
        evidence_refs = row.get("evidence_refs")
        if not isinstance(budget_id, str) or not budget_id:
            findings.append(f"budget reconciliation row {position} has invalid id")
            continue
        reconciled_ids.append(budget_id)
        if "proposed_value" in row:
            findings.append(f"budget reconciliation row {position} cannot invent a value")
        if disposition not in {"pending", "retain", "tighten", "supersede_by_adr", "retire"}:
            findings.append(f"budget reconciliation row {position} has invalid disposition")
        if (
            not isinstance(evidence_refs, list)
            or not evidence_refs
            or any(not isinstance(reference, str) for reference in evidence_refs)
            or not set(evidence_refs).issubset(artifact_ids)
        ):
            findings.append(f"budget reconciliation row {position} lacks linked evidence")
    if len(reconciled_ids) != len(set(reconciled_ids)):
        findings.append("budget reconciliation contains duplicate budget ids")
    if set(reconciled_ids) != expected_ids:
        findings.append("budget reconciliation ids do not match selected ratified budgets")
    return findings


def _check_evidence_packets(
    repo_root: Path,
    packet_paths: tuple[Path, ...],
    *,
    as_of: str | None,
    budgets_path: Path | None = None,
) -> tuple[CheckResult, ...]:
    as_of_value = _parse_utc_timestamp(as_of)
    if as_of_value is None:
        return tuple(
            CheckResult(
                f"RELO-EVIDENCE-{decision.removeprefix('RELO-')}",
                False,
                "An explicit UTC --as-of is required.",
            )
            for decision in _EVIDENCE_DECISIONS
        )
    linked_cards = _load_evidence_cards(repo_root)
    if linked_cards is None or any(not linked_cards[decision] for decision in _EVIDENCE_DECISIONS):
        return tuple(
            CheckResult(
                f"RELO-EVIDENCE-{decision.removeprefix('RELO-')}",
                False,
                "Current ledger decision/card linkage is unavailable.",
            )
            for decision in _EVIDENCE_DECISIONS
        )

    findings_by_decision: dict[str, list[str]] = {
        decision: [] for decision in _EVIDENCE_DECISIONS
    }
    packet_ids: dict[str, str] = {}
    packet_documents: dict[str, dict] = {}
    packet_id_owners: dict[str, str] = {}
    general_findings: list[str] = []
    for position, path in enumerate(packet_paths, start=1):
        try:
            document = _load_evidence_packet(path)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            general_findings.append(f"packet entry {position} cannot be read or parsed")
            continue
        if not isinstance(document, dict):
            general_findings.append(f"packet entry {position} is not a mapping")
            continue
        decision_hint = document.get("decision_id")
        if not isinstance(decision_hint, str) or decision_hint not in findings_by_decision:
            general_findings.append(f"packet entry {position} has an unsupported decision")
            continue
        packet_id, decision, findings = _validate_evidence_packet(
            repo_root,
            document,
            expected_cards=linked_cards[decision_hint],
            as_of=as_of_value,
        )
        if decision is None or decision != decision_hint or packet_id is None:
            findings_by_decision[decision_hint].extend(findings)
            continue
        prior_decision = packet_id_owners.get(packet_id)
        if prior_decision is not None:
            findings_by_decision[prior_decision].append(
                "packet_id is duplicated across decisions",
            )
            findings_by_decision[decision].append("packet_id is duplicated across decisions")
        else:
            packet_id_owners[packet_id] = decision
        if decision in packet_documents:
            findings_by_decision[decision].append("multiple packets supplied for one decision")
        else:
            packet_ids[decision] = packet_id
            packet_documents[decision] = document
            findings_by_decision[decision].extend(findings)
    for decision in _EVIDENCE_DECISIONS:
        if decision not in packet_documents:
            findings_by_decision[decision].append("required packet is missing")

    d14 = packet_documents.get("RELO-D14")
    if d14 is not None:
        dependencies = d14.get("depends_on_packets")
        if (
            not isinstance(dependencies, dict)
            or dependencies.get("RELO-D6d") != packet_ids.get("RELO-D6d")
        ):
            findings_by_decision["RELO-D14"].append(
                "D14 packet does not resolve the supplied D6d packet",
            )
        if findings_by_decision["RELO-D6d"]:
            findings_by_decision["RELO-D14"].append(
                "D14 cannot depend on an inadmissible D6d packet",
            )
        ratified_ids = _load_ratified_budget_ids(
            repo_root, budgets_path=budgets_path,
        )
        if ratified_ids is None:
            findings_by_decision["RELO-D14"].append(
                "budget reconciliation registry is unavailable",
            )
        else:
            findings_by_decision["RELO-D14"].extend(
                _budget_reconciliation_findings(d14, expected_ids=ratified_ids),
            )
    results = [
        CheckResult(
            f"RELO-EVIDENCE-{decision.removeprefix('RELO-')}",
            not findings_by_decision[decision],
            (
                "Evidence packet is mechanically admissible; decision and card completion remain unassessed."
                if not findings_by_decision[decision]
                else "; ".join(findings_by_decision[decision])
            ),
        )
        for decision in _EVIDENCE_DECISIONS
    ]
    if general_findings:
        results.insert(0, CheckResult(
            "RELO-EVIDENCE-PREFLIGHT",
            False,
            "; ".join(general_findings),
        ))
    return tuple(results)


def _synthetic_count(path: Path) -> int:
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and (payload.get("deployment_id") == "trace-model" or payload.get("feature") == "unit-test"):
            count += 1
    return count


_TERMINAL_SECTIONS = ("work_ledger", "source_authority", "budgets", "decisions")
_TERMINAL_MISSING_INPUTS = (
    ("evidence records for the authority gates and certification exit conditions", "§3.6, §3.6a"),
    ("RELO_PROFILE_COMPLETE Definition of Done evidence", "§18, §18.6"),
    ("frozen certified profile ID", "§13.6a"),
    ("CONTAINED entry predicate", "§13.6"),
    ("HALTED owner decision and disposition inventory", "§13.6b"),
    ("staffing and funding records", "§11.0.2, RELO-A25"),
    ("WorkIQ waiver compatibility with full source completeness", "ADR-0020 Outstanding, RVX-D03"),
    ("product-vision gate", "§20 RELO-D16"),
)
_TERMINAL_LIMITATIONS = (
    "Missing inputs have no supported machine-readable format here; evidence held elsewhere is not assessed, not declared absent.",
    "Completed cards are code-complete only; counts, source modes and budgets are not certification or runtime health.",
    "Implementation authorization is separate from terminal certification; no state is inferred from document status or version.",
    "No RELO terminal state is product-vision completion (INV-RELO-29).",
)
_CLOSEOUT_WORK_ITEM_SCOPE = ("RELO-W0.12", "RELO-W0.13", "RELO-W0.14")
_CLOSEOUT_LIMITATIONS = (
    "Generated traceability is a mechanical inverse of the selected specification; it does not prove acceptance or execution evidence.",
    "Staffing, economics and NFR packets report selected evidence shape only; they do not certify currentness, truth or owner authority.",
    "The public NFR registry is a candidate template, not deployment approval or measured evidence.",
)


def _in_closeout_scope(identifier: str) -> bool:
    return any(identifier == prefix or identifier.startswith(prefix) for prefix in _CLOSEOUT_WORK_ITEM_SCOPE)


def _closeout_traceability(spec_path: Path) -> dict:
    try:
        content = spec_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return _unavailable(spec_path.name, f"read failed ({type(error).__name__})")
    visible = _markdown_visible_content(content)
    work_definitions, malformed_phase = _phase_work_item_definitions(visible)
    if malformed_phase:
        return _unavailable(spec_path.name, _PHASE_TABLE_ERROR)
    scope_work = sorted({identifier for identifier in work_definitions if _in_closeout_scope(identifier)})
    missing_scope = [
        prefix
        for prefix in _CLOSEOUT_WORK_ITEM_SCOPE
        if not any(identifier == prefix or identifier.startswith(prefix) for identifier in scope_work)
    ]

    trace = re.search(
        r"^### 19\.2\s[^\n]*\n(.*?)(?=^#{1,3}\s|\Z)",
        visible,
        flags=re.MULTILINE | re.DOTALL,
    )
    if trace is None:
        return _unavailable(spec_path.name, "no '### 19.2' traceability section")
    rows = _markdown_table_rows(
        trace.group(1),
        ("Requirement", "Work items", "Gate", "Primary test/evidence", "Phase"),
        exact=True,
    )
    if rows is None:
        return _unavailable(spec_path.name, "requirement traceability table is missing or malformed")

    outside_trace = visible[:trace.start()] + visible[trace.end():]
    definitions = set(re.findall(
        r"^\|\s*(?:\*\*)?(RELO-(?:FR-\d+|NFR-\d+|W\d+\.\d+[a-z]?|QG-\d+))(?=\s|\*\*|\|)",
        outside_trace,
        flags=re.MULTILINE,
    ))
    requirement_to_work_items: dict[str, set[str]] = {}
    work_item_to_requirements: dict[str, set[str]] = {}
    for row in rows:
        cells = _markdown_table_cells(row)
        if cells is None or len(cells) != 5 or not all(cells):
            return _unavailable(spec_path.name, "requirement traceability row is missing or malformed")
        requirement = cells[0]
        if not re.fullmatch(r"RELO-(?:FR|NFR)-\d+", requirement):
            continue
        try:
            references = _traceability_references(cells[1], definitions)
        except ValueError:
            return _unavailable(spec_path.name, "requirement traceability work references are invalid")
        scoped_references = sorted(
            reference
            for reference in references
            if _in_closeout_scope(reference) and reference in set(scope_work)
        )
        if not scoped_references:
            continue
        requirement_to_work_items.setdefault(requirement, set()).update(scoped_references)
        for work_item in scoped_references:
            work_item_to_requirements.setdefault(work_item, set()).add(requirement)

    missing_backlinks = sorted(work for work in scope_work if work not in work_item_to_requirements)
    return {
        "scope": list(_CLOSEOUT_WORK_ITEM_SCOPE),
        "defined_work_items": scope_work,
        "requirement_to_work_items": {
            key: sorted(value) for key, value in sorted(requirement_to_work_items.items())
        },
        "work_item_to_requirements": {
            key: sorted(value) for key, value in sorted(work_item_to_requirements.items())
        },
        "missing_work_item_definitions": missing_scope,
        "work_items_without_requirement_backlinks": missing_backlinks,
        "complete": not missing_scope and not missing_backlinks,
    }


def _load_closeout_budgets(repo_root: Path, budgets_path: Path | None) -> tuple[tuple, dict | None]:
    relative = "governance/nfr-budgets.yaml"
    import sys

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from src.core.exceptions import ConfigError
        from src.core.nfr_budgets import load_nfr_budgets
    except ImportError as error:
        return (), _unavailable(relative, f"loader import failed ({type(error).__name__})")
    try:
        return load_nfr_budgets(budgets_path if budgets_path is not None else repo_root / relative), None
    except (ConfigError, OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        return (), _unavailable(relative, f"load failed ({type(error).__name__})")


def _closeout_evidence_requirements(budgets: tuple) -> list[dict]:
    requirements = [
        {
            "id": "RELO-W0.13.staffing-current",
            "work_item": "RELO-W0.13",
            "category": "staffing",
            "required_fields": ["as_of", "evidence_refs", "approval"],
            "requires_real_data": True,
            "requires_owner_approval": True,
        },
        {
            "id": "RELO-W0.13.economics-current",
            "work_item": "RELO-W0.13",
            "category": "economics",
            "required_fields": [
                "as_of",
                "window_start",
                "window_end",
                "workload_profile",
                "evidence_refs",
                "approval",
            ],
            "requires_real_data": True,
            "requires_owner_approval": True,
        },
    ]
    for budget in budgets:
        result_fields = (
            ["observed_value", "unit"]
            if budget.direction in {"ceiling", "floor"}
            else ["policy_result"]
        )
        requirements.append({
            "id": f"RELO-W0.14.nfr.{budget.id}",
            "work_item": "RELO-W0.14",
            "category": "nfr-measurement",
            "budget_id": budget.id,
            "required_fields": [
                "as_of",
                "workload_profile",
                "verification_method",
                *result_fields,
                "evidence_refs",
                "approval",
            ],
            "requires_real_data": True,
            "requires_owner_approval": True,
        })
    return requirements


def _valid_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _approval_missing(value: object) -> bool:
    if not isinstance(value, dict):
        return True
    return (
        value.get("status") != "approved"
        or not isinstance(value.get("approved_by"), str)
        or not value["approved_by"].strip()
        or not _valid_date(value.get("approved_at"))
    )


def _evidence_refs_missing(value: object) -> bool:
    return (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    )


def _staffing_missing(section: object) -> list[str]:
    if not isinstance(section, dict):
        return ["as_of", "evidence_refs", "approval"]
    missing = []
    if not _valid_date(section.get("as_of")):
        missing.append("as_of")
    if _evidence_refs_missing(section.get("evidence_refs")):
        missing.append("evidence_refs")
    if _approval_missing(section.get("approval")):
        missing.append("approval")
    return missing


def _economics_missing(section: object) -> list[str]:
    if not isinstance(section, dict):
        return [
            "as_of",
            "window_start",
            "window_end",
            "workload_profile",
            "evidence_refs",
            "approval",
        ]
    missing = []
    for field in ("as_of", "window_start", "window_end"):
        if not _valid_date(section.get(field)):
            missing.append(field)
    if missing == [] and section["window_start"] > section["window_end"]:
        missing.append("window_end")
    workload_profile = section.get("workload_profile")
    if not isinstance(workload_profile, str) or not workload_profile.strip():
        missing.append("workload_profile")
    if _evidence_refs_missing(section.get("evidence_refs")):
        missing.append("evidence_refs")
    if _approval_missing(section.get("approval")):
        missing.append("approval")
    return missing


def _nfr_measurement_missing(entry: object, budget: object) -> list[str]:
    if not isinstance(entry, dict):
        return ["measurement record"]
    missing = []
    if not _valid_date(entry.get("as_of")):
        missing.append("as_of")
    for field in ("workload_profile", "verification_method"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            missing.append(field)
    direction = getattr(budget, "direction", None)
    if direction in {"ceiling", "floor"}:
        observed = entry.get("observed_value")
        if isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isfinite(observed):
            missing.append("observed_value")
        unit = entry.get("unit")
        if (
            not isinstance(unit, str)
            or not unit.strip()
            or unit != getattr(budget, "unit", None)
        ):
            missing.append("unit")
    else:
        result = entry.get("policy_result")
        if not isinstance(result, str) or not result.strip():
            missing.append("policy_result")
    if _evidence_refs_missing(entry.get("evidence_refs")):
        missing.append("evidence_refs")
    if _approval_missing(entry.get("approval")):
        missing.append("approval")
    return missing


def _remaining_evidence(requirement: dict, missing: list[str]) -> dict:
    return {
        "id": requirement["id"],
        "work_item": requirement["work_item"],
        "category": requirement["category"],
        "requires_real_data": requirement["requires_real_data"],
        "requires_owner_approval": requirement["requires_owner_approval"],
        "missing": missing,
    }


def _closeout_evidence_packet(
    repo_root: Path,
    *,
    budgets_path: Path | None,
    evidence_packet_path: Path | None,
) -> dict:
    budgets, failure = _load_closeout_budgets(repo_root, budgets_path)
    if failure is not None:
        return {**failure, "selected": evidence_packet_path is not None, "complete": False}
    requirements = _closeout_evidence_requirements(budgets)
    if evidence_packet_path is None:
        return {
            "selected": False,
            "complete": False,
            "requirements": requirements,
            "remaining": [
                _remaining_evidence(requirement, ["evidence packet not selected"])
                for requirement in requirements
            ],
            "findings": [],
        }
    try:
        document = yaml.safe_load(evidence_packet_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        return {
            **_unavailable(evidence_packet_path.name, f"read failed ({type(error).__name__})"),
            "selected": True,
            "complete": False,
        }
    except yaml.YAMLError:
        return {
            **_unavailable(evidence_packet_path.name, "YAML parse failed"),
            "selected": True,
            "complete": False,
        }

    findings: list[str] = []
    if not isinstance(document, dict):
        document = {}
        findings.append("evidence packet must be a mapping")
    if document.get("schema_version") != "1":
        findings.append("schema_version must be '1'")

    staffing = document.get("staffing")
    economics = document.get("economics")
    staffing_missing = _staffing_missing(staffing)
    economics_missing = _economics_missing(economics)
    if not isinstance(staffing, dict) or staffing.get("status") not in {"pending", "supplied"} or (
        staffing.get("status") == "supplied" and staffing_missing
    ):
        findings.append("staffing evidence is incomplete")
    if not isinstance(economics, dict) or economics.get("status") not in {"pending", "supplied"} or (
        economics.get("status") == "supplied" and economics_missing
    ):
        findings.append("economics evidence is incomplete")

    measurements = document.get("nfr_measurements")
    measurement_by_budget: dict[str, dict] = {}
    if not isinstance(measurements, list):
        measurements = []
        findings.append("nfr_measurements must be a list")
    else:
        for position, entry in enumerate(measurements, start=1):
            if not isinstance(entry, dict) or entry.get("status") not in {"pending", "supplied"}:
                findings.append(f"NFR evidence entry {position} is incomplete")
                continue
            budget_id = entry.get("budget_id")
            if not isinstance(budget_id, str) or not budget_id:
                findings.append(f"NFR evidence entry {position} is incomplete")
                continue
            if budget_id in measurement_by_budget:
                findings.append("NFR evidence contains a duplicate budget_id")
                continue
            measurement_by_budget[budget_id] = entry

    budgets_by_id = {budget.id: budget for budget in budgets}
    unknown_measurements = sorted(set(measurement_by_budget) - set(budgets_by_id))
    if unknown_measurements:
        findings.append("NFR evidence contains unknown budget_id entries")

    remaining: list[dict] = []
    if staffing is None or not isinstance(staffing, dict) or staffing.get("status") != "supplied":
        remaining.append(_remaining_evidence(requirements[0], staffing_missing or ["supplied status"]))
    elif staffing_missing:
        remaining.append(_remaining_evidence(requirements[0], staffing_missing))
    if economics is None or not isinstance(economics, dict) or economics.get("status") != "supplied":
        remaining.append(_remaining_evidence(requirements[1], economics_missing or ["supplied status"]))
    elif economics_missing:
        remaining.append(_remaining_evidence(requirements[1], economics_missing))

    for requirement in requirements[2:]:
        budget_id = requirement["budget_id"]
        entry = measurement_by_budget.get(budget_id)
        budget = budgets_by_id[budget_id]
        missing = _nfr_measurement_missing(entry, budget)
        if entry is None or entry.get("status") != "supplied" or missing:
            remaining.append(_remaining_evidence(
                requirement,
                missing if missing else ["supplied status"],
            ))
            if entry is not None and entry.get("status") == "supplied" and missing:
                findings.append("NFR evidence contains an incomplete supplied measurement")

    return {
        "selected": True,
        "complete": not findings and not remaining and len(measurement_by_budget) == len(budgets),
        "requirements": requirements,
        "remaining": remaining,
        "findings": sorted(set(findings)),
    }


def assess_terminal_state(repo_root: Path, *, spec_path: Path, budgets_path: Path | None = None) -> dict:
    """RELO-W6.12a: read-only evidence-gap projection; never reports a terminal state."""
    return {
        "terminal_state": None,
        "assessment": "not_assessed",
        "reason": "No supported evidence format exists for the §13.6 terminal-state predicates.",
        "work_ledger": _ledger_projection(repo_root),
        "source_authority": _source_authority_projection(repo_root),
        "budgets": _budget_projection(repo_root, budgets_path=budgets_path),
        "decisions": _decision_projection(spec_path),
        "w6_readiness": _w6_readiness_projection(repo_root, spec_path=spec_path),
        "missing_inputs": [{"input": item, "spec": spec} for item, spec in _TERMINAL_MISSING_INPUTS],
        "limitations": list(_TERMINAL_LIMITATIONS),
    }


def assess_closeout(
    repo_root: Path,
    *,
    spec_path: Path,
    budgets_path: Path | None = None,
    evidence_packet_path: Path | None = None,
) -> dict:
    """RELO-W0.12/13/14 mechanical evidence-gap assessment."""
    return {
        "terminal_state": None,
        "assessment": "not_assessed",
        "reason": (
            "RELO-W0.12/W0.13/W0.14 mechanical completeness does not establish "
            "operational qualification."
        ),
        "traceability": _closeout_traceability(spec_path),
        "evidence_packet": _closeout_evidence_packet(
            repo_root,
            budgets_path=budgets_path,
            evidence_packet_path=evidence_packet_path,
        ),
        "limitations": list(_CLOSEOUT_LIMITATIONS),
    }


def _unavailable(label: str, detail: str) -> dict:
    return {"unavailable": f"{label}: {detail}"}


def _load_governance_yaml(repo_root: Path, relative: str) -> tuple[object, dict | None]:
    try:
        text = (repo_root / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return None, _unavailable(relative, f"read failed ({type(error).__name__})")
    try:
        return yaml.safe_load(text), None
    except yaml.YAMLError as error:
        return None, _unavailable(relative, f"parse failed ({type(error).__name__})")


def _ledger_projection(repo_root: Path) -> dict:
    relative = "governance/relo-work-packages.yaml"
    document, failure = _load_governance_yaml(repo_root, relative)
    if failure:
        return failure
    if not isinstance(document, dict) or document.get("schema_version") != "1":
        return _unavailable(relative, "schema_version must be '1'")
    cards = document.get("work_items")
    if not isinstance(cards, list):
        return _unavailable(relative, "work_items must be a list")
    by_status: dict[str, int] = {}
    by_phase: dict[str, dict[str, int]] = {}
    by_class: dict[str, int] = {}
    parent_by_status: dict[str, int] = {}
    relo6: list[dict[str, str]] = []
    ids: list[str] = []
    for position, card in enumerate(cards, start=1):
        if not isinstance(card, dict):
            return _unavailable(relative, f"work item {position} must be a mapping")
        for field in ("id", "phase", "class", "status"):
            if not isinstance(card.get(field), str) or not card[field].strip():
                return _unavailable(relative, f"work item {position} needs non-empty string {field}")
        ids.append(card["id"])
        status = card["status"]
        by_status[status] = by_status.get(status, 0) + 1
        if _PARENT_CARD_ID_RE.fullmatch(card["id"]):
            parent_by_status[status] = parent_by_status.get(status, 0) + 1
        phase = by_phase.setdefault(card["phase"], {})
        phase[status] = phase.get(status, 0) + 1
        by_class[card["class"]] = by_class.get(card["class"], 0) + 1
        if card["phase"] == "RELO-6":
            relo6.append({"id": card["id"], "class": card["class"], "status": status})
    duplicates = _duplicates(ids)
    if duplicates:
        return _unavailable(relative, f"duplicate work item ID(s): {', '.join(duplicates)}")
    return {
        "by_status": by_status,
        "by_phase": by_phase,
        "by_class": by_class,
        "code_complete": by_status.get("completed", 0),
        "parent_by_status": parent_by_status,
        "parent_total": sum(parent_by_status.values()),
        "parent_code_complete": parent_by_status.get("completed", 0),
        "w0_readiness": _w0_readiness_projection(cards),
        "is_certification": False,
        "relo6": relo6,
    }


def _w0_readiness_projection(cards: list[dict]) -> dict:
    parents = sorted(
        (
            card for card in cards
            if card.get("current_parent") is True
        ),
        key=lambda card: card["id"],
    )
    slices = sorted(
        (
            card for card in cards
            if card.get("current_slice") is True
        ),
        key=lambda card: card["id"],
    )
    by_parent: dict[str, dict] = {}
    for parent in parents:
        parent_id = parent["id"]
        child_slices = [
            {
                "id": card["id"],
                "status": card.get("status"),
                "class": card.get("class"),
                "startability": card.get("startability"),
                "depends_on": card.get("depends_on", []),
                "blocking_decisions": card.get("blocking_decisions", []),
            }
            for card in slices
            if isinstance(card.get("definition"), dict)
            and card["definition"].get("parent_id") == parent_id
        ]
        by_parent[parent_id] = {
            "status": parent["status"],
            "class": parent["class"],
            "blocking_decisions": parent.get("blocking_decisions", []),
            "slices": child_slices,
        }
    buckets = {
        startability: [
            card["id"] for card in slices if card.get("startability") == startability
        ]
        for startability in sorted(_W0_STARTABILITY)
    }
    return {
        "open_parents": [parent["id"] for parent in parents if parent["status"] != "completed"],
        "by_parent": by_parent,
        **buckets,
    }


def _source_authority_projection(repo_root: Path) -> dict:
    relative = "governance/relo-authority-matrix.yaml"
    document, failure = _load_governance_yaml(repo_root, relative)
    if failure:
        return failure
    sources = document.get("sources") if isinstance(document, dict) else None
    if not isinstance(sources, list):
        return _unavailable(relative, "sources must be a list")
    mandatory: list[str] = []
    modes: dict[str, str] = {}
    names: list[str] = []
    for position, row in enumerate(sources, start=1):
        if not isinstance(row, dict) or not isinstance(row.get("source"), str) or not row["source"].strip():
            return _unavailable(relative, f"source {position} needs a non-empty string source")
        if not isinstance(row.get("mandatory"), bool):
            return _unavailable(relative, f"source {position} needs boolean mandatory")
        scheduled = row.get("scheduled")
        if not isinstance(scheduled, dict) or not isinstance(scheduled.get("principal_mode"), str) or not scheduled["principal_mode"].strip():
            return _unavailable(relative, f"source {position} needs scheduled.principal_mode")
        names.append(row["source"])
        if row["mandatory"]:
            mandatory.append(row["source"])
        modes[row["source"]] = scheduled["principal_mode"]
    duplicates = _duplicates(names)
    if duplicates:
        return _unavailable(relative, f"duplicate source(s): {', '.join(duplicates)}")
    return {"mandatory_sources": mandatory, "scheduled_principal_modes": modes}


def _budget_projection(repo_root: Path, *, budgets_path: Path | None = None) -> dict:
    relative = "governance/nfr-budgets.yaml"
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from src.core.exceptions import ConfigError
        from src.core.nfr_budgets import load_nfr_budgets
    except ImportError as error:
        return _unavailable(relative, f"loader import failed ({type(error).__name__})")
    try:
        budgets = load_nfr_budgets(budgets_path if budgets_path is not None else repo_root / relative)
    except (ConfigError, OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        return _unavailable(relative, f"load failed ({type(error).__name__})")
    return {
        "ratified": sum(1 for budget in budgets if budget.is_ratified),
        "unratified": [budget.id for budget in budgets if not budget.is_ratified],
    }


def _decision_projection(spec_path: Path) -> dict:
    label = spec_path.name
    try:
        content = spec_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return _unavailable(label, f"read failed ({type(error).__name__})")
    heading = re.search(r"^## 20\.", content, re.MULTILINE)
    if heading is None:
        return _unavailable(label, "no '## 20.' decisions section")
    section = re.split(r"^## ", content[heading.end():], maxsplit=1, flags=re.MULTILINE)[0]
    rows = re.findall(r"^\|\s+\*\*(RELO-D\w+)\*\*\s+\|(.*)$", section, re.MULTILINE)
    if not rows:
        return _unavailable(label, "no decision rows")
    return {
        "open": [identifier for identifier, rest in rows if "**RESOLVED" not in rest],
        "interim": [identifier for identifier, rest in rows if "**RESOLVED" in rest and "(interim)" in rest],
    }


def _print_terminal_state(report: dict) -> None:
    if "w6_terminal_evidence" in report:
        evidence = report["w6_terminal_evidence"]
        print(
            f"EXACT terminal state: {evidence['terminal_state']} "
            f"({evidence['assessment']}; not product-vision completion)"
        )
    print(f"NOT ASSESSED terminal state: {report['reason']}")
    for key in _TERMINAL_SECTIONS:
        print(f"{key}: {json.dumps(report[key], sort_keys=True)}")
    for item in report["missing_inputs"]:
        print(f"MISSING {item['input']} ({item['spec']})")
    for limitation in report["limitations"]:
        print(f"LIMITATION {limitation}")


def _print_closeout(report: dict) -> None:
    print(f"NOT ASSESSED RELO-W0 closeout: {report['reason']}")
    print(f"traceability: {json.dumps(report['traceability'], sort_keys=True)}")
    print(f"evidence_packet: {json.dumps(report['evidence_packet'], sort_keys=True)}")
    for item in report["evidence_packet"].get("remaining", []):
        print(f"MISSING {item['id']}: {', '.join(item['missing'])}")
    for limitation in report["limitations"]:
        print(f"LIMITATION {limitation}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True, help="Explicit evidence repository root.")
    parser.add_argument("--spec", type=Path, required=True, help="Explicit specification to lint.")
    parser.add_argument("--budgets", type=Path, help="Optional explicit operator budget registry instead of the selected root's governance registry.")
    parser.add_argument("--program-config", type=Path, help="Optional program configuration to check for deployment variables.")
    parser.add_argument("--telemetry", type=Path, action="append", default=[], help="Optional telemetry sidecar to check; repeatable.")
    parser.add_argument("--evidence-packet", type=Path, action="append", default=[], help="Optional RELO evidence packet; repeat to supply D6d, D7, D13 and D14.")
    parser.add_argument("--as-of", help="Explicit UTC timestamp used for reproducible evidence-packet freshness checks.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable results.")
    parser.add_argument("--terminal-state", action="store_true", help="Report the read-only terminal-state evidence gap (never certifies).")
    parser.add_argument(
        "--w6-evidence",
        type=Path,
        help="Optional explicit W6.12 evidence packet used to evaluate the exact terminal-state predicates.",
    )
    parser.add_argument("--closeout", action="store_true", help="Assess RELO-W0.12/W0.13/W0.14 mechanical closeout (never certifies).")
    parser.add_argument("--packet", type=Path, help="Optional evidence packet for --closeout.")
    args = parser.parse_args(argv)
    if args.closeout and args.terminal_state:
        parser.error("--closeout and --terminal-state are mutually exclusive")
    if args.packet is not None and not args.closeout:
        parser.error("--packet requires --closeout")
    if args.w6_evidence is not None and not args.terminal_state:
        parser.error("--w6-evidence requires --terminal-state")
    if args.closeout:
        report = assess_closeout(
            args.repo_root,
            spec_path=args.spec,
            budgets_path=args.budgets,
            evidence_packet_path=args.packet,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_closeout(report)
        return 1 if any(
            "unavailable" in report[key]
            for key in ("traceability", "evidence_packet")
        ) else 6
    if args.terminal_state:
        report = assess_terminal_state(args.repo_root, spec_path=args.spec, budgets_path=args.budgets)
        if args.w6_evidence is not None:
            try:
                from src.core.relo_w6 import terminal_state_from_evidence

                selected = json.loads(args.w6_evidence.read_text(encoding="utf-8"))
                if not isinstance(selected, dict):
                    raise ValueError("terminal evidence must be an object")
                report["w6_terminal_evidence"] = terminal_state_from_evidence(selected)
            except (ImportError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
                report["w6_terminal_evidence"] = {
                    "terminal_state": None,
                    "assessment": "not_assessed",
                    "missing_fields": ["input"],
                    "invalid_fields": ["w6_evidence"],
                    "failed_gates": [],
                    "limitations": ["The selected W6 evidence packet was unavailable or invalid."],
                }
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_terminal_state(report)
        return 1 if any("unavailable" in report[key] for key in _TERMINAL_SECTIONS) else 6
    results = verify(
        args.repo_root,
        spec_path=args.spec,
        program_config=args.program_config,
        telemetry_paths=tuple(args.telemetry),
        budgets_path=args.budgets,
        evidence_packet_paths=tuple(args.evidence_packet),
        as_of=args.as_of,
    )
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        for result in results:
            print(f"{'PASS' if result.passed else 'FAIL'} {result.id}: {result.detail}")
    return 0 if all(result.passed for result in results) else 6


def _semantic_traceability_findings(spec_path: Path, visible_content: str) -> list[str]:
    requirement_rows = _markdown_table_rows(
        _traceability_section(visible_content, "19.2"),
        ("Requirement", "Work items", "Gate", "Primary test/evidence", "Phase"),
        exact=True,
    )
    if requirement_rows is None:
        return []
    repo_root = _spec_repo_root(spec_path)
    try:
        ledger = yaml.safe_load(
            (repo_root / "governance" / "relo-work-packages.yaml").read_text(encoding="utf-8"),
        )
        problems = yaml.safe_load(
            (repo_root / "governance" / "relo-problem-statements.yaml").read_text(encoding="utf-8"),
        )
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    cards = ledger.get("work_items") if isinstance(ledger, dict) else None
    problem_rows = problems.get("problems") if isinstance(problems, dict) else None
    if not isinstance(cards, list) or not isinstance(problem_rows, list):
        return []

    work_definitions, malformed_phase = _phase_work_item_definitions(visible_content)
    if malformed_phase:
        return []
    parent_ids = set(work_definitions)
    card_by_id = {
        card["id"]: card for card in cards
        if isinstance(card, dict) and isinstance(card.get("id"), str)
    }
    compacted = {
        card["id"]: card for card in cards
        if isinstance(card, dict) and _validated_compacted_definition(card, parent_ids=parent_ids)
    }
    all_work_ids = parent_ids | set(compacted)
    requirements = set()
    for section in ("5.5", "5.6"):
        requirement_pattern = r"RELO-FR-\d+" if section == "5.5" else r"RELO-NFR-\d+"
        requirements.update(_traceability_definition_ids(
            _markdown_table_rows(
                _traceability_section(visible_content, section),
                ("ID", "Requirement"),
            ),
            requirement_pattern,
        ))

    findings: list[str] = []
    parsed_rows: dict[str, list[tuple[str, set[str]]]] = {}
    for line in requirement_rows:
        cells = _markdown_table_cells(line)
        if cells is None or len(cells) != 5 or not all(cells):
            continue
        try:
            references = _traceability_references(cells[1], all_work_ids)
        except ValueError:
            continue
        parsed_rows.setdefault(cells[0], []).append((cells[3], references))

    for requirement, rows in parsed_rows.items():
        if len(rows) > 1:
            child_cards = [
                card_by_id[reference]
                for _, references in rows
                for reference in references
                if reference in card_by_id and requirement in _relo_card_requirements(card_by_id[reference])
            ]
            if child_cards:
                findings.append(
                    f"{requirement}: duplicate semantic traceability requirement row",
                )
        for reference in {
            identifier
            for _, references in rows
            for identifier in references
            if identifier in compacted
        }:
            card = card_by_id.get(reference)
            if card is None or requirement not in _relo_card_requirements(card):
                findings.append(
                    f"{reference}: orphan semantic traceability work reference for {requirement}",
                )

    scoped_cards: list[tuple[dict, list[str]]] = []
    for card in card_by_id.values():
        explicit_requirements = _relo_card_requirements(card)
        if explicit_requirements:
            scoped_cards.append((card, explicit_requirements))
    scoped_problems: dict[str, set[str]] = {}
    for card, _ in scoped_cards:
        owners = {
            card["id"],
            card.get("definition", {}).get("parent_id", card["id"]),
        }
        for problem_id in _relo_card_problems(card):
            scoped_problems.setdefault(problem_id, set()).update(owners)

    for card, explicit_requirements in scoped_cards:
        identifier = card["id"]
        visible_owners = work_definitions.count(identifier)
        compacted_owners = int(identifier in compacted)
        if visible_owners + compacted_owners == 0:
            findings.append(f"{identifier}: semantic traceability definition owner is missing")
        elif visible_owners > 1 or visible_owners + compacted_owners > 1:
            findings.append(f"{identifier}: duplicate semantic traceability definition owner")
        authority_ref = card.get("authority_ref")
        if not isinstance(authority_ref, str):
            findings.append(f"{identifier}: unresolved semantic traceability citation")
        else:
            source = authority_ref.partition("#")[0]
            try:
                citation_path = (repo_root / source).resolve(strict=True)
                if not citation_path.is_relative_to(repo_root.resolve()) or not citation_path.is_file():
                    raise ValueError
            except (OSError, ValueError):
                findings.append(f"{identifier}: unresolved semantic traceability citation")
        for requirement in explicit_requirements:
            if requirement not in requirements:
                findings.append(
                    f"{identifier}: unresolved semantic traceability requirement {requirement}",
                )
                continue
            rows = parsed_rows.get(requirement, [])
            if len(rows) > 1:
                continue
            if not rows:
                findings.append(
                    f"{identifier}: semantic traceability missing work-item coverage for {requirement}",
                )
                continue
            evidence, references = rows[0]
            parent_id = card.get("definition", {}).get("parent_id", identifier)
            if identifier not in references and parent_id not in references:
                findings.append(
                    f"{identifier}: semantic traceability missing work-item coverage for {requirement}",
                )
            acceptance = card.get("acceptance")
            if not isinstance(acceptance, list) or not any(
                test in evidence
                for entry in acceptance
                for test in (
                    (entry.get("test"),)
                    if isinstance(entry, dict)
                    else (entry,)
                )
                if isinstance(test, str) and test
            ):
                findings.append(
                    f"{identifier}: semantic traceability evidence reference is unresolved for {requirement}",
                )

    audit_rows = _markdown_table_rows(
        _traceability_section(visible_content, "19.3"),
        ("Audit finding", "Requirement/workstream", "Acceptance evidence"),
        exact=True,
    )
    if audit_rows is not None:
        problem_ids = {
            problem["id"] for problem in problem_rows
            if isinstance(problem, dict) and isinstance(problem.get("id"), str)
        }
        scoped_problem_references: set[str] = set()
        scoped_problem_owners = {
            owner for owners in scoped_problems.values() for owner in owners
        }
        for line in audit_rows:
            cells = _markdown_table_cells(line)
            if cells is None or len(cells) != 3:
                continue
            try:
                references = _traceability_references(cells[1], all_work_ids)
            except ValueError:
                continue
            if not references.intersection(scoped_problem_owners):
                continue
            for problem_id in set(re.findall(r"\bRELO-P\d+\b", cells[1])):
                scoped_problem_references.add(problem_id)
                if problem_id not in problem_ids:
                    findings.append(
                        f"{problem_id}: unresolved semantic traceability problem definition",
                    )
        for problem_id in scoped_problem_references - set(scoped_problems):
            findings.append(f"{problem_id}: orphan semantic traceability problem row")
        for problem_id, owners in scoped_problems.items():
            matching_references = 0
            for line in audit_rows:
                cells = _markdown_table_cells(line)
                if cells is None or len(cells) != 3 or problem_id not in cells[1]:
                    continue
                try:
                    references = _traceability_references(cells[1], all_work_ids)
                except ValueError:
                    continue
                if references.intersection(owners):
                    matching_references += 1
            if matching_references == 0:
                findings.append(
                    f"{problem_id}: semantic traceability missing problem row",
                )
            elif matching_references > 1:
                findings.append(f"{problem_id}: duplicate semantic traceability problem row")
            if problem_id not in problem_ids:
                findings.append(
                    f"{problem_id}: unresolved semantic traceability problem definition",
                )
            visible_owners = len(re.findall(
                rf"^####[ \t]+{re.escape(problem_id)}\b", visible_content, re.MULTILINE,
            ))
            registry_owners = sum(
                problem.get("id") == problem_id
                for problem in problem_rows if isinstance(problem, dict)
            )
            if visible_owners + registry_owners > 1:
                findings.append(f"{problem_id}: duplicate semantic traceability definition owner")
    return findings


def _relo_card_requirements(card: dict) -> list[str]:
    requirements = card.get("requirements")
    if not isinstance(requirements, list):
        return []
    return [
        requirement for requirement in requirements
        if isinstance(requirement, str)
        and re.fullmatch(r"RELO-(?:FR|NFR)-\d+", requirement)
    ]


def _relo_card_problems(card: dict) -> list[str]:
    problems = card.get("problems")
    if not isinstance(problems, list):
        return []
    return [
        problem for problem in problems
        if isinstance(problem, str) and re.fullmatch(r"RELO-P\d+", problem)
    ]


def _w6_readiness_projection(repo_root: Path, *, spec_path: Path) -> dict:
    relative = "governance/relo-work-packages.yaml"
    document, failure = _load_governance_yaml(repo_root, relative)
    if failure:
        return failure
    cards = document.get("work_items") if isinstance(document, dict) else None
    if not isinstance(cards, list):
        return _unavailable(relative, "work_items must be a list")

    by_id: dict[str, dict] = {}
    for position, card in enumerate(cards, start=1):
        if not isinstance(card, dict) or not isinstance(card.get("id"), str):
            return _unavailable(
                relative,
                f"work item {position} needs a string id",
            )
        identifier = card["id"]
        if identifier in by_id:
            return _unavailable(
                relative,
                f"duplicate work item ID(s): {identifier}",
            )
        by_id[identifier] = card

    decisions = _decision_projection(spec_path)
    if "open" not in decisions or not isinstance(decisions["open"], list):
        return _unavailable(spec_path.name, "decision projection unavailable")
    open_decisions = set(decisions["open"])

    rows: list[dict] = []
    w6_cards = sorted(
        (
            card
            for card in cards
            if isinstance(card, dict)
            and re.fullmatch(
                r"RELO-W6\.\d+",
                str(card.get("id", "")),
            )
        ),
        key=lambda card: int(card["id"].split(".")[-1]),
    )
    for card in w6_cards:
        identifier = card["id"]
        required = ("phase", "class", "status")
        if any(not isinstance(card.get(field), str) for field in required):
            return _unavailable(
                relative,
                f"{identifier} needs string phase, class and status",
            )

        declared_decisions = card.get("blocking_decisions", [])
        if not isinstance(declared_decisions, list) or any(
            not isinstance(decision, str)
            for decision in declared_decisions
        ):
            return _unavailable(
                relative,
                f"{identifier} blocking_decisions must be strings",
            )
        unresolved_decisions = [
            decision
            for decision in declared_decisions
            if decision in open_decisions
        ]

        dependencies = card.get("depends_on", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(dependency, str)
            for dependency in dependencies
        ):
            return _unavailable(
                relative,
                f"{identifier} depends_on must be strings",
            )
        unresolved_dependencies = []
        for dependency in dependencies:
            dependent = by_id.get(dependency)
            if dependent is None:
                return _unavailable(
                    relative,
                    f"{identifier} unresolved dependency {dependency}",
                )
            status = dependent.get("status")
            if status != "completed":
                unresolved_dependencies.append(
                    {
                        "id": dependency,
                        "class": dependent.get("class"),
                        "status": status,
                    }
                )

        card_class = card["class"]
        status = card["status"]
        if status == "completed":
            readiness = "completed"
        elif (
            card_class == "AGENT_READY"
            and not unresolved_decisions
            and not unresolved_dependencies
        ):
            readiness = "agent_ready"
        elif unresolved_decisions:
            readiness = "decision_blocked"
        elif card_class == "NEEDS_DECISION":
            readiness = "decision_blocked"
        elif card_class == "AGENT_READY":
            readiness = "dependency_blocked"
        else:
            readiness = "bounded_slice_required"

        rows.append(
            {
                "id": identifier,
                "class": card_class,
                "status": status,
                "readiness": readiness,
                "blocking_decisions": declared_decisions,
                "unresolved_decisions": unresolved_decisions,
                "dependencies": dependencies,
                "unresolved_dependencies": unresolved_dependencies,
            }
        )

    counts: dict[str, int] = {}
    for row in rows:
        readiness = row["readiness"]
        counts[readiness] = counts.get(readiness, 0) + 1
    return {
        "assessment": "not_qualified",
        "summary": counts,
        "agent_startable": [
            row["id"]
            for row in rows
            if row["readiness"] == "agent_ready"
        ],
        "items": rows,
    }


if __name__ == "__main__":
    raise SystemExit(main())

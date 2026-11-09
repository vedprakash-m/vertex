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
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import yaml


_SECTION_RE = re.compile(r"^#{2,6}\s+(\d+(?:\.\d+)*(?:[a-z])?)(?:\.\s|\s)", re.MULTILINE)
_CITATION_RE = re.compile(r"§(\d+(?:\.\d+)*(?:[a-z])?)")
_VERSION_RE = re.compile(r"^\*\*Version:\*\*\s+([\d.]+)", re.MULTILINE)
_CHANGELOG_VERSION_RE = re.compile(r"^- \*\*v([\d.]+)\s+—", re.MULTILINE)
_HEADING_ID_RE = re.compile(r"^#{3,6}\s+(RELO-[A-Z]\d+(?:\.\d+)?)\b", re.MULTILINE)
_WORK_ITEM_RE = re.compile(r"^\|\s+\*\*(RELO-W\d+\.\d+)\s+\|", re.MULTILINE)
_CARD_ID_RE = re.compile(r"^id:\s+(RELO-W\d+\.\d+[a-z])\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class CheckResult:
    id: str
    passed: bool
    detail: str


def verify(
    repo_root: Path, *, spec_path: Path, program_config: Path | None = None,
    telemetry_paths: tuple[Path, ...] = (), budgets_path: Path | None = None,
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

    defined_ids = (
        _HEADING_ID_RE.findall(content)
        + _WORK_ITEM_RE.findall(content)
        + _CARD_ID_RE.findall(content)
    )
    duplicates = _duplicates(defined_ids)
    if duplicates:
        findings.append(f"duplicate defined RELO ID(s): {', '.join(duplicates)}")
    visible_content = _markdown_visible_content(content)
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
    findings.extend(_goal_traceability_findings(content))
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


def _traceability_section(content: str, number: str) -> str | None:
    parent, _, child = number.partition(".")
    section = re.search(
        rf"^##[ \t]+{re.escape(parent)}(?:\.[ \t]|[ \t])[^\n]*\n(.*?)(?=^#{{1,2}}[ \t]|\Z)",
        content, re.MULTILINE | re.DOTALL,
    )
    if section is None:
        return None
    if not child:
        return section.group(1)
    section = re.search(
        rf"^###[ \t]+{re.escape(number)}[ \t][^\n]*\n(.*?)(?=^#{{1,3}}[ \t]|\Z)",
        section.group(1), re.MULTILINE | re.DOTALL,
    )
    return section.group(1) if section is not None else None


def _markdown_table_rows(
    section: str | None, header: tuple[str, ...], *, exact: bool = False,
) -> list[str] | None:
    if section is None:
        return None
    lines = section.splitlines()
    for index, line in enumerate(lines):
        columns = _markdown_table_cells(line)
        if columns is None or tuple(columns[:len(header)]) != header:
            continue
        if exact and len(columns) != len(header):
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
                break
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


def _goal_traceability_findings(content: str) -> list[str]:
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

    requirement_ids = set()
    for section, pattern in (("5.5", r"RELO-FR-\d+"), ("5.6", r"RELO-NFR-\d+")):
        requirement_ids.update(_traceability_definition_ids(
            _markdown_table_rows(_traceability_section(visible, section), ("ID", "Requirement")), pattern,
        ))
    work_ids = set()
    for phase in range(1, 8):
        work_ids.update(_traceability_definition_ids(
            _markdown_table_rows(_traceability_section(visible, f"11.{phase}"), ("ID", "Work item")),
            r"RELO-W\d+\.\d+[a-z]?",
        ))
    features = _traceability_section(visible, "8")
    feature_ids = set(re.findall(
        r"^###\s+8\.\d+\s+(RELO-F\d+)\b", features or "", re.MULTILINE,
    ))
    definitions = set(objectives) | requirement_ids | work_ids | feature_ids
    declared_goals: set[str] = set()
    referenced_objectives: set[str] = set()
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

    missing_goals = sorted(set(goals) - declared_goals)
    if missing_goals:
        findings.append(f"missing goal traceability row(s): {', '.join(missing_goals)}")
    missing_objectives = sorted(set(objectives) - referenced_objectives)
    if missing_objectives:
        findings.append(f"unreferenced objective(s): {', '.join(missing_objectives)}")
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


def lint_work_packages(repo_root: Path, *, spec_path: Path) -> CheckResult:
    """Validate RELO readiness records without interpreting their design.

    This enforces the issuance rules that are mechanically knowable: coverage,
    unique IDs, valid classes and dependencies, resolving AGENT_READY paths,
    and named §20 blockers for NEEDS_DECISION work. It never upgrades a card.
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

    expected = (
        _phase_work_item_ids(specification, "11.1", "11.2")
        | _phase_work_item_ids(specification, "11.2", "11.3")
        | _phase_work_item_ids(specification, "11.3", "11.4")
        | _phase_work_item_ids(specification, "11.4", "11.5")
        | _phase_work_item_ids(specification, "11.5", "11.6")
        | _phase_work_item_ids(specification, "11.6", "11.7")
        | _phase_work_item_ids(specification, "11.7", "12.1")
    )
    ids = [card.get("id") for card in cards if isinstance(card, dict)]
    findings: list[str] = []
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
    for position, card in enumerate(cards, start=1):
        if not isinstance(card, dict):
            findings.append("work-package card is not a mapping")
            continue
        identifier = card.get("id")
        if not isinstance(identifier, str) or not identifier:
            findings.append("work-package card has invalid id")
            identifier = "<invalid id>"
        elif identifier not in expected:
            identifier = f"<unknown card at entry {position}>"
        card_class = card.get("class")
        if not isinstance(card_class, str) or card_class not in allowed_classes:
            detail = "(not an allowed class)" if isinstance(card_class, str) else "(must be a string)"
            findings.append(f"{identifier}: invalid class {detail}")
            continue
        dependencies = card.get("depends_on")
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
    return CheckResult(
        "RELO-WORK-PACKAGES",
        not findings,
        "All RELO-0/1 work items have a valid readiness record with mechanically checkable blockers and paths."
        if not findings
        else "; ".join(findings),
    )


def _phase_work_item_ids(specification: str, phase_heading: str, next_phase_heading: str) -> set[str]:
    start = specification.find(f"### {phase_heading}")
    end = specification.find(f"### {next_phase_heading}", start + 1)
    if start < 0 or end < 0:
        return set()
    phase = specification[start:end]
    return set(re.findall(r"^\|\s+(RELO-W\d+\.\d+[a-z]?)\s+\|", phase, re.MULTILINE))


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
    non-bold table-row pattern (matching `_phase_work_item_ids`), not the
    dead bold-only `_WORK_ITEM_RE` (see governance ledger note on that gap).
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

    import sys

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

    line_count = content.count("\n") + 1
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
        "missing_inputs": [{"input": item, "spec": spec} for item, spec in _TERMINAL_MISSING_INPUTS],
        "limitations": list(_TERMINAL_LIMITATIONS),
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
        "is_certification": False,
        "relo6": relo6,
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
    import sys

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
    print(f"NOT ASSESSED terminal state: {report['reason']}")
    for key in _TERMINAL_SECTIONS:
        print(f"{key}: {json.dumps(report[key], sort_keys=True)}")
    for item in report["missing_inputs"]:
        print(f"MISSING {item['input']} ({item['spec']})")
    for limitation in report["limitations"]:
        print(f"LIMITATION {limitation}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True, help="Explicit evidence repository root.")
    parser.add_argument("--spec", type=Path, required=True, help="Explicit specification to lint.")
    parser.add_argument("--budgets", type=Path, help="Optional explicit operator budget registry instead of the selected root's governance registry.")
    parser.add_argument("--program-config", type=Path, help="Optional program configuration to check for deployment variables.")
    parser.add_argument("--telemetry", type=Path, action="append", default=[], help="Optional telemetry sidecar to check; repeatable.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable results.")
    parser.add_argument("--terminal-state", action="store_true", help="Report the read-only terminal-state evidence gap (never certifies).")
    args = parser.parse_args(argv)
    if args.terminal_state:
        report = assess_terminal_state(args.repo_root, spec_path=args.spec, budgets_path=args.budgets)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_terminal_state(report)
        return 1 if any("unavailable" in report[key] for key in _TERMINAL_SECTIONS) else 6
    results = verify(args.repo_root, spec_path=args.spec, program_config=args.program_config, telemetry_paths=tuple(args.telemetry), budgets_path=args.budgets)
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        for result in results:
            print(f"{'PASS' if result.passed else 'FAIL'} {result.id}: {result.detail}")
    return 0 if all(result.passed for result in results) else 6


if __name__ == "__main__":
    raise SystemExit(main())

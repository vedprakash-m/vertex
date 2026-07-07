from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from io import StringIO
import json
from pathlib import Path
from typing import Any, Callable

import typer

from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.continuation_contract import ContinuationContract, load_continuation_contract
from src.core.edition_resolver import EDITIONS_ROOT, PROGRAMS_ROOT, get_program_output_dir, resolve_edition
from src.core.exceptions import ConfigError
from src.core.narrative_store import load_archived_narratives
from src.core.snapshot_store import ARCHIVE_ROOT, get_archive_root
from src.core.trusted_baseline_store import load_trusted_baseline, mark_bridge_graduated


@dataclass(frozen=True, slots=True)
class BridgeIssueMeasurement:
    issue_number: int
    composition_stable: bool | None
    section_roster_stable: bool | None
    narrative_similarity: float | None
    narrative_similarity_pass: bool | None
    missing_evidence_sections: int | None
    readiness_score: int | None
    readiness_pass: bool | None


@dataclass(frozen=True, slots=True)
class BridgeCriterionStatus:
    criterion_id: str
    label: str
    threshold: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class BridgeStatusReport:
    edition: str
    display_name: str
    trusted_issue_number: int | None
    latest_confirmed_issue: int | None
    eligible_issue_numbers: tuple[int, ...]
    bridge_graduated: bool
    graduated_at: datetime | None
    graduation_issue: int | None
    graduation_ready: bool
    criteria: tuple[BridgeCriterionStatus, ...]
    data_limitations: tuple[str, ...]
    issue_measurements: tuple[BridgeIssueMeasurement, ...]

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["graduated_at"] = self.graduated_at.isoformat() if self.graduated_at is not None else None
        return payload


def bridge_status_command(
    edition: str = typer.Option(..., "--edition", help="Edition id, e.g. myprogram_weekly."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
    graduate: bool = typer.Option(False, "--graduate", help="Mark the bridge as graduated when exit criteria are met."),
    yes: bool = typer.Option(False, "--yes", help="Skip the graduation confirmation prompt."),
    export_metrics: bool = typer.Option(False, "--export-metrics", help="Write current bridge metrics to publications/<edition>/bridge_metrics.json."),
) -> None:
    if graduate and format != "human" and not yes:
        raise typer.BadParameter("--graduate with non-human output requires --yes.")

    report = build_bridge_status_report(edition)
    if export_metrics:
        metrics_path = get_program_output_dir(edition, programs_root=PROGRAMS_ROOT) / "bridge_metrics.json"
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps(report.to_payload(), indent=2, sort_keys=True), encoding="utf-8")
        typer.echo(f"Bridge metrics exported to {metrics_path}")
    if graduate:
        if report.bridge_graduated:
            _emit_bridge_status(report, format)
            raise typer.Exit(code=0)
        if not report.graduation_ready:
            _emit_bridge_status(report, format)
            raise typer.Exit(code=1)
        if yes or typer.confirm("Bridge graduation criteria met. Confirm graduation?", default=False):
            updated = mark_bridge_graduated(
                edition,
                report.latest_confirmed_issue or report.trusted_issue_number or 0,
                graduated_at=datetime.now(timezone.utc),
                graduated_by=None,
                editions_root=EDITIONS_ROOT,
                programs_root=PROGRAMS_ROOT,
            )
            if updated is None:
                raise typer.BadParameter(f"Cannot graduate bridge for {edition} without a trusted baseline.")
            report = build_bridge_status_report(edition)
            _emit_bridge_status(report, format)
            raise typer.Exit(code=0)
        _emit_bridge_status(report, format)
        raise typer.Exit(code=1)

    _emit_bridge_status(report, format)
    raise typer.Exit(code=0)


def build_bridge_status_report(
    edition_name: str,
    *,
    editions_root: Path | None = None,
    programs_root: Path | None = None,
    archive_root: Path | None = None,
) -> BridgeStatusReport:
    resolved_editions_root = editions_root or EDITIONS_ROOT
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    repo_root = resolved_editions_root.parent

    resolved = resolve_edition(
        edition_name,
        editions_root=resolved_editions_root,
        programs_root=resolved_programs_root,
    )
    if resolved is None:
        raise ConfigError(f"Edition '{edition_name}' was not found.")

    baseline = load_trusted_baseline(
        edition_name,
        editions_root=resolved_editions_root,
        programs_root=resolved_programs_root,
    )
    archive_index = read_archive_index(edition_name, archive_root=resolved_archive_root)
    latest_confirmed = find_latest_confirmed_entry(archive_index)

    if baseline is None or baseline.trusted_issue_number is None:
        return BridgeStatusReport(
            edition=edition_name,
            display_name=resolved.edition.brand_name or resolved.program.name,
            trusted_issue_number=None,
            latest_confirmed_issue=(latest_confirmed.issue_number if latest_confirmed is not None else None),
            eligible_issue_numbers=(),
            bridge_graduated=False,
            graduated_at=None,
            graduation_issue=None,
            graduation_ready=False,
            criteria=(
                BridgeCriterionStatus("BE-1", "Scorecard composition stability", ">= 5 consecutive", "unavailable", "No trusted baseline established."),
                BridgeCriterionStatus("BE-2", "Section roster continuity", ">= 5 consecutive", "unavailable", "No trusted baseline established."),
                BridgeCriterionStatus("BE-3", "Author revision scope", ">= 4 of last 5 issues", "unavailable", "No trusted baseline established."),
                BridgeCriterionStatus("BE-4", "Missing evidence rate", "<= 1 section", "unavailable", "No trusted baseline established."),
                BridgeCriterionStatus("BE-5", "Draft readiness score", ">= 80%", "unavailable", "No trusted baseline established."),
                BridgeCriterionStatus("BE-6", "Author confidence", "explicit graduation", "pending", "Run vertex bridge-status --graduate after BE-1 through BE-5 pass."),
            ),
            data_limitations=("No trusted baseline established.",),
            issue_measurements=(),
        )

    untrusted_issue_numbers = {
        history.issue
        for history in baseline.history
        if history.action == "untrusted" and history.issue > baseline.trusted_issue_number
    }
    eligible_entries = tuple(
        entry
        for entry in sorted(archive_index.issues, key=lambda item: item.issue_number)
        if entry.kind == "confirmed"
        and entry.issue_number > baseline.trusted_issue_number
        and entry.issue_number not in untrusted_issue_numbers
    )
    limitations: list[str] = []
    measurements = tuple(
        _build_issue_measurement(
            edition=edition_name,
            issue_number=entry.issue_number,
            archive_root=resolved_archive_root,
            limitations=limitations,
        )
        for entry in eligible_entries
    )
    be1_count, be1_missing = _trailing_pass_count(measurements, lambda item: item.composition_stable)
    be2_count, be2_missing = _trailing_pass_count(measurements, lambda item: item.section_roster_stable)
    be4_count, be4_missing = _trailing_pass_count(measurements, lambda item: item.missing_evidence_sections is not None and item.missing_evidence_sections <= 1)
    be5_count, be5_missing = _trailing_pass_count(measurements, lambda item: item.readiness_pass)
    window = measurements[-5:]
    be3_missing = tuple(issue.issue_number for issue in window if issue.narrative_similarity_pass is None)
    be3_pass_count = sum(1 for issue in window if issue.narrative_similarity_pass)
    be3_ratios = [issue.narrative_similarity for issue in window if issue.narrative_similarity is not None]
    passive_acceptance_issue_numbers = tuple(
        issue.issue_number
        for issue in window
        if issue.narrative_similarity is not None
        and issue.narrative_similarity >= 0.70
        and issue.readiness_pass is False
    )
    be3_average_ratio = (sum(be3_ratios) / len(be3_ratios)) if be3_ratios else None
    be3_pass = (
        len(window) == 5
        and not be3_missing
        and be3_average_ratio is not None
        and be3_average_ratio >= 0.70
        and be3_pass_count >= 4
    )

    criteria = (
        _build_consecutive_status(
            criterion_id="BE-1",
            label="Scorecard composition stability",
            count=be1_count,
            threshold=5,
            missing_issue_numbers=be1_missing,
        ),
        _build_consecutive_status(
            criterion_id="BE-2",
            label="Section roster continuity",
            count=be2_count,
            threshold=5,
            missing_issue_numbers=be2_missing,
        ),
        _build_revision_scope_status(
            window=window,
            average_ratio=be3_average_ratio,
            pass_count=be3_pass_count,
            missing_issue_numbers=be3_missing,
            passed=be3_pass,
        ),
        _build_consecutive_status(
            criterion_id="BE-4",
            label="Missing evidence rate",
            count=be4_count,
            threshold=5,
            missing_issue_numbers=be4_missing,
            suffix=f"{be4_count}/5 latest eligible issues stayed at <= 1 missing-evidence section.",
        ),
        _build_consecutive_status(
            criterion_id="BE-5",
            label="Draft readiness score",
            count=be5_count,
            threshold=5,
            missing_issue_numbers=be5_missing,
            suffix=f"{be5_count}/5 latest eligible issues stayed at >= 80% readiness.",
        ),
        _build_author_confidence_status(baseline.bridge_graduated),
    )
    if passive_acceptance_issue_numbers:
        issue_list = ", ".join(f"{issue:03d}" for issue in passive_acceptance_issue_numbers)
        limitations.append(
            "Possible passive acceptance risk: high narrative similarity paired with draft readiness below 80% for "
            f"Issue {issue_list}."
        )
    graduation_ready = all(criteria[index].status == "passed" for index in range(5)) and not baseline.bridge_graduated

    return BridgeStatusReport(
        edition=edition_name,
        display_name=resolved.edition.brand_name or resolved.program.name,
        trusted_issue_number=baseline.trusted_issue_number,
        latest_confirmed_issue=(latest_confirmed.issue_number if latest_confirmed is not None else None),
        eligible_issue_numbers=tuple(entry.issue_number for entry in eligible_entries),
        bridge_graduated=baseline.bridge_graduated,
        graduated_at=baseline.graduated_at,
        graduation_issue=baseline.graduation_issue,
        graduation_ready=graduation_ready,
        criteria=criteria,
        data_limitations=tuple(dict.fromkeys(limitations + _criteria_limitations(criteria))),
        issue_measurements=measurements,
    )


def render_bridge_status(report: BridgeStatusReport) -> str:
    trusted_baseline = (
        f"Issue {report.trusted_issue_number:03d}"
        if report.trusted_issue_number is not None
        else "not established"
    )
    latest_confirmed = (
        f"Issue {report.latest_confirmed_issue:03d}"
        if report.latest_confirmed_issue is not None
        else "none"
    )
    bridge_state = "Graduated" if report.bridge_graduated else "Active"
    graduation = "not ready"
    if report.graduation_ready and report.latest_confirmed_issue is not None:
        graduation = f"ready to graduate at Issue {report.latest_confirmed_issue:03d}"
    if report.bridge_graduated and report.graduated_at is not None and report.graduation_issue is not None:
        graduation = f"graduated at Issue {report.graduation_issue:03d} on {report.graduated_at.date().isoformat()}"
    issue_list = ", ".join(f"{issue:03d}" for issue in report.eligible_issue_numbers) or "none"
    lines = [
        f"{report.display_name} — Bridge Status",
        f"  Trusted baseline: {trusted_baseline}",
        f"  Latest confirm:   {latest_confirmed}",
        f"  Bridge state:     {bridge_state}",
        f"  Eligible issues:  {len(report.eligible_issue_numbers)} ({issue_list})",
        f"  Graduation:       {graduation}",
        "",
    ]
    for criterion in report.criteria:
        lines.append(f"  {criterion.criterion_id} {criterion.label}: {criterion.status} — {criterion.detail}")
    if report.data_limitations:
        lines.append("")
        lines.append("  Data limits:")
        for limitation in report.data_limitations:
            lines.append(f"    - {limitation}")
    return "\n".join(lines)


def render_bridge_status_csv(report: BridgeStatusReport) -> str:
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "edition",
            "display_name",
            "trusted_issue_number",
            "latest_confirmed_issue",
            "bridge_graduated",
            "graduated_at",
            "graduation_issue",
            "graduation_ready",
            "eligible_issue_numbers",
            "be1_status",
            "be2_status",
            "be3_status",
            "be4_status",
            "be5_status",
            "be6_status",
            "data_limitations",
        ],
    )
    writer.writeheader()
    criteria = {criterion.criterion_id.lower().replace("-", ""): criterion for criterion in report.criteria}
    writer.writerow(
        {
            "edition": report.edition,
            "display_name": report.display_name,
            "trusted_issue_number": report.trusted_issue_number or "",
            "latest_confirmed_issue": report.latest_confirmed_issue or "",
            "bridge_graduated": str(report.bridge_graduated).lower(),
            "graduated_at": report.graduated_at.isoformat() if report.graduated_at is not None else "",
            "graduation_issue": report.graduation_issue or "",
            "graduation_ready": str(report.graduation_ready).lower(),
            "eligible_issue_numbers": ",".join(str(issue) for issue in report.eligible_issue_numbers),
            "be1_status": criteria["be1"].status,
            "be2_status": criteria["be2"].status,
            "be3_status": criteria["be3"].status,
            "be4_status": criteria["be4"].status,
            "be5_status": criteria["be5"].status,
            "be6_status": criteria["be6"].status,
            "data_limitations": " | ".join(report.data_limitations),
        }
    )
    return output.getvalue()


def _emit_bridge_status(report: BridgeStatusReport, format: str) -> None:
    if format == "json":
        typer.echo(json.dumps(report.to_payload(), indent=2, sort_keys=True))
        return
    if format == "csv":
        typer.echo(render_bridge_status_csv(report), nl=False)
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    typer.echo(render_bridge_status(report))


def _build_issue_measurement(
    *,
    edition: str,
    issue_number: int,
    archive_root: Path,
    limitations: list[str],
) -> BridgeIssueMeasurement:
    edition_archive_root = get_archive_root(edition, archive_root)
    contract = load_continuation_contract(
        edition_archive_root / "continuation_contracts" / f"issue_{issue_number:03d}.continuation_contract.json"
    )
    if contract is None:
        limitations.append(f"Continuation contract missing for Issue {issue_number:03d}.")
    readiness_score = _load_readiness_score(
        edition_archive_root / "manifests" / f"issue_{issue_number:03d}.json",
        issue_number=issue_number,
        limitations=limitations,
    )
    narrative_similarity = _build_narrative_similarity(
        edition=edition,
        issue_number=issue_number,
        contract=contract,
        archive_root=archive_root,
        limitations=limitations,
    )
    readiness_pass = readiness_score >= 80 if readiness_score is not None else None
    narrative_similarity_pass = (
        narrative_similarity >= 0.70 and readiness_pass is True
        if narrative_similarity is not None and readiness_pass is not None
        else None
    )
    return BridgeIssueMeasurement(
        issue_number=issue_number,
        composition_stable=(
            not contract.scorecard_composition.proposed_additions and not contract.scorecard_composition.proposed_removals
            if contract is not None
            else None
        ),
        section_roster_stable=(
            not contract.section_roster.added_sections and not contract.section_roster.removed_sections
            if contract is not None
            else None
        ),
        narrative_similarity=narrative_similarity,
        narrative_similarity_pass=narrative_similarity_pass,
        missing_evidence_sections=(len(contract.section_roster.sections_missing_evidence) if contract is not None else None),
        readiness_score=readiness_score,
        readiness_pass=readiness_pass,
    )


def _load_readiness_score(
    manifest_path: Path,
    *,
    issue_number: int,
    limitations: list[str],
) -> int | None:
    if not manifest_path.exists():
        limitations.append(f"Archived manifest missing for Issue {issue_number:03d}.")
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        limitations.append(f"Archived manifest malformed for Issue {issue_number:03d}.")
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        limitations.append(f"Manifest metadata missing for Issue {issue_number:03d}.")
        return None
    readiness = metadata.get("draft_readiness")
    if not isinstance(readiness, dict) or readiness.get("score") is None:
        limitations.append(f"Draft readiness metadata missing for Issue {issue_number:03d}.")
        return None
    return int(readiness["score"])


def _build_narrative_similarity(
    *,
    edition: str,
    issue_number: int,
    contract: ContinuationContract | None,
    archive_root: Path,
    limitations: list[str],
) -> float | None:
    if contract is None:
        return None
    if not contract.narrative_seeding.seeded or contract.narrative_seeding.source_issue is None:
        return None
    current_narratives = load_archived_narratives(edition, issue_number, archive_root=archive_root)
    source_narratives = load_archived_narratives(
        edition,
        contract.narrative_seeding.source_issue,
        archive_root=archive_root,
    )
    ratios: list[float] = []
    for filename in contract.narrative_seeding.files_seeded:
        if not filename.startswith("ws_") or not filename.endswith(".md"):
            continue
        current_text = current_narratives.get(filename)
        source_text = source_narratives.get(filename)
        if current_text is None or source_text is None:
            limitations.append(f"Seeded narrative history incomplete for Issue {issue_number:03d} ({filename}).")
            return None
        ratios.append(SequenceMatcher(None, _normalize_text(source_text), _normalize_text(current_text)).ratio())
    if not ratios:
        return None
    return round(sum(ratios) / len(ratios), 3)


def _normalize_text(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def _trailing_pass_count(
    measurements: tuple[BridgeIssueMeasurement, ...],
    predicate: Callable[[BridgeIssueMeasurement], bool | None],
) -> tuple[int, tuple[int, ...]]:
    count = 0
    missing_issue_numbers: list[int] = []
    for measurement in reversed(measurements):
        result = predicate(measurement)
        if result is None:
            missing_issue_numbers.append(measurement.issue_number)
            break
        if not result:
            break
        count += 1
    return count, tuple(missing_issue_numbers)


def _build_consecutive_status(
    *,
    criterion_id: str,
    label: str,
    count: int,
    threshold: int,
    missing_issue_numbers: tuple[int, ...],
    suffix: str | None = None,
) -> BridgeCriterionStatus:
    detail = suffix or f"{count}/{threshold} consecutive eligible issues currently pass."
    if missing_issue_numbers:
        missing = ", ".join(f"{issue:03d}" for issue in missing_issue_numbers)
        return BridgeCriterionStatus(
            criterion_id,
            label,
            f">= {threshold} consecutive",
            "unavailable",
            f"Historical data missing for Issue {missing}; {count}/{threshold} consecutive passes observed before the gap.",
        )
    if count >= threshold:
        return BridgeCriterionStatus(
            criterion_id,
            label,
            f">= {threshold} consecutive",
            "passed",
            detail,
        )
    return BridgeCriterionStatus(
        criterion_id,
        label,
        f">= {threshold} consecutive",
        "pending" if count > 0 else "failed",
        detail,
    )


def _build_revision_scope_status(
    *,
    window: tuple[BridgeIssueMeasurement, ...],
    average_ratio: float | None,
    pass_count: int,
    missing_issue_numbers: tuple[int, ...],
    passed: bool,
) -> BridgeCriterionStatus:
    if len(window) < 5:
        return BridgeCriterionStatus(
            "BE-3",
            "Author revision scope",
            ">= 4 of last 5 issues at >= 0.70",
            "pending",
            f"Only {len(window)}/5 eligible issues available for the rolling window.",
        )
    if missing_issue_numbers:
        missing = ", ".join(f"{issue:03d}" for issue in missing_issue_numbers)
        return BridgeCriterionStatus(
            "BE-3",
            "Author revision scope",
            ">= 4 of last 5 issues at >= 0.70",
            "unavailable",
            f"Similarity or readiness history missing for Issue {missing} in the last-five window.",
        )
    return BridgeCriterionStatus(
        "BE-3",
        "Author revision scope",
        ">= 4 of last 5 issues at >= 0.70",
        "passed" if passed else "failed",
        f"{pass_count}/5 issues passed; average similarity ratio {average_ratio:.2f}.",
    )


def _build_author_confidence_status(bridge_graduated: bool) -> BridgeCriterionStatus:
    return BridgeCriterionStatus(
        "BE-6",
        "Author confidence",
        "explicit graduation",
        "passed" if bridge_graduated else "pending",
        (
            "Bridge graduation already recorded in trusted_baseline.yaml."
            if bridge_graduated
            else "Run vertex bridge-status --graduate after BE-1 through BE-5 pass."
        ),
    )


def _criteria_limitations(criteria: tuple[BridgeCriterionStatus, ...]) -> list[str]:
    return [criterion.detail for criterion in criteria if criterion.status == "unavailable"]
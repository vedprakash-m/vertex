from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path

from src.core.archive_store import read_scorecard_history
from src.core.models import EditionType
from src.core.narrative_store import REMOVED_SECTION_MARKER, load_archived_narratives, load_narrative_seeding_state, load_narratives
from src.core.narrative_store import NarrativeSeedingState
from src.core.overrides_store import OverridesDocument, OverridesSeedingState
from src.core.trusted_baseline_store import TrustedBaseline, load_trusted_baseline, load_trusted_baseline_issue
from src.core.view_models import WorkstreamData


@dataclass(frozen=True, slots=True)
class ContinuationContractScorecardComposition:
    frozen_from_issue: int
    inherited_dimensions: tuple[tuple[str, str], ...]
    proposed_additions: tuple[tuple[str, str], ...] = ()
    proposed_removals: tuple[tuple[str, str], ...] = ()
    removed_by_override: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ContinuationContractSectionRoster:
    inherited_sections: tuple[str, ...]
    seeded_from_prior: bool
    sections_missing_evidence: tuple[str, ...] = ()
    added_sections: tuple[str, ...] = ()
    removed_sections: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContinuationContractNarrativeSeeding:
    seeded: bool
    source_issue: int | None
    source_path: str | None
    files_seeded: tuple[str, ...] = ()
    source_hashes: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ContinuationContractOverridesSeeding:
    seeded: bool
    source_issue: int | None
    fields_carried: tuple[str, ...] = ()
    fields_cleared: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContinuationContractEvidenceQuality:
    sections_with_ado_coverage: int
    sections_with_query_only: int
    sections_with_connector_only: int
    sections_manual_only: int


@dataclass(frozen=True, slots=True)
class ContinuationContractBaselineGap:
    skipped_untrusted_issues: tuple[int, ...] = ()
    latest_untrusted_issue: int | None = None
    latest_untrusted_at: datetime | None = None
    latest_untrusted_by: str | None = None
    latest_untrusted_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ContinuationContract:
    schema_version: str
    edition: str
    issue_number: int
    prior_trusted_issue: int
    first_inherited_at: datetime
    last_refreshed_at: datetime
    scorecard_composition: ContinuationContractScorecardComposition
    section_roster: ContinuationContractSectionRoster
    narrative_seeding: ContinuationContractNarrativeSeeding
    overrides_seeding: ContinuationContractOverridesSeeding
    evidence_quality: ContinuationContractEvidenceQuality
    baseline_gap: ContinuationContractBaselineGap | None = None


def get_continuation_contract_path(output_dir: Path, issue_number: int) -> Path:
    return output_dir / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.continuation_contract.json"


def load_continuation_contract(path: Path) -> ContinuationContract | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ContinuationContract(
        schema_version=str(payload.get("schema_version", "1.0")),
        edition=str(payload["edition"]),
        issue_number=int(payload["issue_number"]),
        prior_trusted_issue=int(payload["prior_trusted_issue"]),
        first_inherited_at=datetime.fromisoformat(payload["first_inherited_at"]),
        last_refreshed_at=datetime.fromisoformat(payload["last_refreshed_at"]),
        scorecard_composition=ContinuationContractScorecardComposition(
            frozen_from_issue=int(payload["scorecard_composition"]["frozen_from_issue"]),
            inherited_dimensions=tuple(
                (str(row[0]), str(row[1]))
                for row in payload["scorecard_composition"].get("inherited_dimensions", [])
            ),
            proposed_additions=tuple(
                (str(row[0]), str(row[1]))
                for row in payload["scorecard_composition"].get("proposed_additions", [])
            ),
            proposed_removals=tuple(
                (str(row[0]), str(row[1]))
                for row in payload["scorecard_composition"].get("proposed_removals", [])
            ),
            removed_by_override=tuple(
                (str(row[0]), str(row[1]))
                for row in payload["scorecard_composition"].get("removed_by_override", [])
            ),
        ),
        section_roster=ContinuationContractSectionRoster(
            inherited_sections=tuple(str(value) for value in payload["section_roster"].get("inherited_sections", [])),
            seeded_from_prior=bool(payload["section_roster"].get("seeded_from_prior", False)),
            sections_missing_evidence=tuple(str(value) for value in payload["section_roster"].get("sections_missing_evidence", [])),
            added_sections=tuple(str(value) for value in payload["section_roster"].get("added_sections", [])),
            removed_sections=tuple(str(value) for value in payload["section_roster"].get("removed_sections", [])),
        ),
        narrative_seeding=ContinuationContractNarrativeSeeding(
            seeded=bool(payload["narrative_seeding"].get("seeded", False)),
            source_issue=(
                int(payload["narrative_seeding"]["source_issue"])
                if payload["narrative_seeding"].get("source_issue") is not None
                else None
            ),
            source_path=payload["narrative_seeding"].get("source_path"),
            files_seeded=tuple(str(value) for value in payload["narrative_seeding"].get("files_seeded", [])),
            source_hashes={
                str(key): str(value)
                for key, value in payload["narrative_seeding"].get("source_hashes", {}).items()
            },
        ),
        overrides_seeding=ContinuationContractOverridesSeeding(
            seeded=bool(payload["overrides_seeding"].get("seeded", False)),
            source_issue=(
                int(payload["overrides_seeding"]["source_issue"])
                if payload["overrides_seeding"].get("source_issue") is not None
                else None
            ),
            fields_carried=tuple(str(value) for value in payload["overrides_seeding"].get("fields_carried", [])),
            fields_cleared=tuple(str(value) for value in payload["overrides_seeding"].get("fields_cleared", [])),
        ),
        evidence_quality=ContinuationContractEvidenceQuality(
            sections_with_ado_coverage=int(payload["evidence_quality"].get("sections_with_ado_coverage", 0)),
            sections_with_query_only=int(payload["evidence_quality"].get("sections_with_query_only", 0)),
            sections_with_connector_only=int(payload["evidence_quality"].get("sections_with_connector_only", 0)),
            sections_manual_only=int(payload["evidence_quality"].get("sections_manual_only", 0)),
        ),
        baseline_gap=(
            ContinuationContractBaselineGap(
                skipped_untrusted_issues=tuple(int(value) for value in payload["baseline_gap"].get("skipped_untrusted_issues", [])),
                latest_untrusted_issue=(
                    int(payload["baseline_gap"]["latest_untrusted_issue"])
                    if payload["baseline_gap"].get("latest_untrusted_issue") is not None
                    else None
                ),
                latest_untrusted_at=(
                    datetime.fromisoformat(payload["baseline_gap"]["latest_untrusted_at"])
                    if payload["baseline_gap"].get("latest_untrusted_at") is not None
                    else None
                ),
                latest_untrusted_by=payload["baseline_gap"].get("latest_untrusted_by"),
                latest_untrusted_reason=payload["baseline_gap"].get("latest_untrusted_reason"),
            )
            if isinstance(payload.get("baseline_gap"), dict)
            else None
        ),
    )


def build_continuation_contract(
    *,
    edition_name: str,
    issue_number: int,
    started_at: datetime,
    reports_root: Path,
    archive_root: Path,
    editions_root: Path,
    programs_root: Path,
    overrides_document: OverridesDocument,
    workstream_data: tuple[WorkstreamData, ...],
    output_dir: Path,
    current_scorecard_dimensions: tuple[tuple[str, str], ...],
    current_section_ids: tuple[str, ...],
    narrative_seeding: NarrativeSeedingState | None = None,
    overrides_seeding: OverridesSeedingState | None = None,
) -> ContinuationContract | None:
    trusted_issue = load_trusted_baseline_issue(
        edition_name,
        before_issue_number=issue_number,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if trusted_issue is None:
        return None

    trusted_baseline = load_trusted_baseline(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )

    existing = load_continuation_contract(get_continuation_contract_path(output_dir, issue_number))
    inherited_dimensions = load_inherited_scorecard_dimensions(edition_name, trusted_issue, archive_root=archive_root)
    current_dimensions = tuple(sorted(current_scorecard_dimensions))
    removed_by_override = tuple(
        sorted((entry.scorecard_name, entry.dimension_name) for entry in overrides_document.removed_dimensions)
    )
    inherited_sections, source_path = _load_inherited_sections(
        edition_name,
        trusted_issue,
        reports_root=reports_root,
        archive_root=archive_root,
    )
    inherited_section_ids = tuple(sorted(
        section_id
        for filename in inherited_sections
        if (section_id := _normalize_section_id(filename)) is not None
    ))
    current_sections = tuple(sorted(current_section_ids))
    sections_missing_evidence, evidence_quality = _build_evidence_quality(workstream_data)
    resolved_narrative_seeding = narrative_seeding or load_narrative_seeding_state(
        edition_name,
        issue_number,
        reports_root=reports_root,
    )
    if resolved_narrative_seeding is None and existing is not None and existing.narrative_seeding.seeded:
        resolved_narrative_seeding = NarrativeSeedingState(
            seeded=True,
            source_issue=existing.narrative_seeding.source_issue,
            source_path=existing.narrative_seeding.source_path,
            files_seeded=existing.narrative_seeding.files_seeded,
            source_hashes=existing.narrative_seeding.source_hashes,
        )
    resolved_overrides_seeding = overrides_seeding
    if (
        existing is not None
        and existing.overrides_seeding.seeded
        and (resolved_overrides_seeding is None or not resolved_overrides_seeding.seeded)
    ):
        resolved_overrides_seeding = OverridesSeedingState(
            seeded=True,
            source_issue=existing.overrides_seeding.source_issue,
            fields_carried=existing.overrides_seeding.fields_carried,
            fields_cleared=existing.overrides_seeding.fields_cleared,
        )
    return ContinuationContract(
        schema_version="1.0",
        edition=edition_name,
        issue_number=issue_number,
        prior_trusted_issue=trusted_issue,
        first_inherited_at=(existing.first_inherited_at if existing is not None else started_at),
        last_refreshed_at=started_at,
        scorecard_composition=ContinuationContractScorecardComposition(
            frozen_from_issue=trusted_issue,
            inherited_dimensions=inherited_dimensions,
            proposed_additions=tuple(sorted(set(current_dimensions) - set(inherited_dimensions))),
            proposed_removals=tuple(sorted(set(inherited_dimensions) - set(current_dimensions) - set(removed_by_override))),
            removed_by_override=removed_by_override,
        ),
        section_roster=ContinuationContractSectionRoster(
            inherited_sections=inherited_sections,
            seeded_from_prior=bool(resolved_narrative_seeding is not None and resolved_narrative_seeding.seeded),
            sections_missing_evidence=sections_missing_evidence,
            added_sections=tuple(sorted(set(current_sections) - set(inherited_section_ids))),
            removed_sections=tuple(
                sorted(set(inherited_section_ids) - set(current_sections) - set(overrides_document.removed_sections))
            ),
        ),
        narrative_seeding=ContinuationContractNarrativeSeeding(
            seeded=bool(resolved_narrative_seeding is not None and resolved_narrative_seeding.seeded),
            source_issue=(
                resolved_narrative_seeding.source_issue
                if resolved_narrative_seeding is not None and resolved_narrative_seeding.source_issue is not None
                else trusted_issue
            ),
            source_path=(
                resolved_narrative_seeding.source_path
                if resolved_narrative_seeding is not None and resolved_narrative_seeding.source_path is not None
                else source_path
            ),
            files_seeded=(resolved_narrative_seeding.files_seeded if resolved_narrative_seeding is not None else ()),
            source_hashes=(
                resolved_narrative_seeding.source_hashes
                if resolved_narrative_seeding is not None and resolved_narrative_seeding.source_hashes is not None
                else _build_source_hashes(
                    edition_name,
                    trusted_issue,
                    reports_root=reports_root,
                    archive_root=archive_root,
                ) if source_path is not None else {}
            ),
        ),
        overrides_seeding=ContinuationContractOverridesSeeding(
            seeded=bool(resolved_overrides_seeding is not None and resolved_overrides_seeding.seeded),
            source_issue=(
                resolved_overrides_seeding.source_issue
                if resolved_overrides_seeding is not None and resolved_overrides_seeding.source_issue is not None
                else trusted_issue
            ),
            fields_carried=(resolved_overrides_seeding.fields_carried if resolved_overrides_seeding is not None else ()),
            fields_cleared=(resolved_overrides_seeding.fields_cleared if resolved_overrides_seeding is not None else ()),
        ),
        evidence_quality=evidence_quality,
        baseline_gap=_build_baseline_gap(
            trusted_baseline,
            trusted_issue=trusted_issue,
            issue_number=issue_number,
        ),
    )


def load_inherited_scorecard_dimensions(edition_name: str, trusted_issue: int, *, archive_root: Path) -> tuple[tuple[str, str], ...]:
    entries = read_scorecard_history(edition_name, archive_root=archive_root)
    inherited = {
        (str(entry.get("scorecard_name", "")), str(entry.get("dimension", entry.get("name", ""))))
        for entry in entries
        if int(entry.get("issue_number", -1)) == trusted_issue
    }
    return tuple(sorted((scorecard_name, dimension_name) for scorecard_name, dimension_name in inherited if scorecard_name and dimension_name))


def load_inherited_section_ids(
    edition_name: str,
    trusted_issue: int,
    *,
    reports_root: Path,
    archive_root: Path,
) -> tuple[str, ...]:
    inherited_sections, _source_path = _load_inherited_sections(
        edition_name,
        trusted_issue,
        reports_root=reports_root,
        archive_root=archive_root,
    )
    return tuple(
        sorted(
            section_id
            for section_id in (_normalize_section_id(filename) for filename in inherited_sections)
            if section_id is not None
        )
    )


def carried_forward_section_ids(
    loaded_narratives: dict[str, str],
    *,
    removed_section_ids: set[str],
) -> set[str]:
    carried_forward: set[str] = set()
    for filename, content in loaded_narratives.items():
        if not filename.startswith("ws_") or not filename.endswith(".md"):
            continue
        if not content.strip() or content.startswith(REMOVED_SECTION_MARKER):
            continue
        section_id = filename.removeprefix("ws_").removesuffix(".md")
        if section_id in removed_section_ids:
            continue
        carried_forward.add(section_id)
    return carried_forward


def build_bridge_section_roster_ids(
    *,
    edition_name: str,
    edition_type: EditionType | None,
    trusted_issue: int | None,
    reports_root: Path | None,
    archive_root: Path | None,
    current_section_ids: set[str],
    loaded_narratives: dict[str, str],
    removed_section_ids: set[str],
) -> tuple[set[str], set[str]]:
    carried_forward_ids = carried_forward_section_ids(
        loaded_narratives,
        removed_section_ids=removed_section_ids,
    )
    diagnostic_section_ids = set(current_section_ids) | carried_forward_ids

    if (
        edition_type != EditionType.DETAILED
        or trusted_issue is None
        or reports_root is None
        or archive_root is None
    ):
        return diagnostic_section_ids, diagnostic_section_ids

    inherited_section_ids = set(
        load_inherited_section_ids(
            edition_name,
            trusted_issue,
            reports_root=reports_root,
            archive_root=archive_root,
        )
    )
    if not inherited_section_ids:
        return diagnostic_section_ids, diagnostic_section_ids

    enforced_section_ids = (set(current_section_ids) & inherited_section_ids) | carried_forward_ids
    return enforced_section_ids, diagnostic_section_ids


def _build_baseline_gap(
    trusted_baseline: TrustedBaseline | None,
    *,
    trusted_issue: int,
    issue_number: int,
) -> ContinuationContractBaselineGap | None:
    if trusted_baseline is None:
        return None

    skipped_untrusted_issues = tuple(
        sorted(
            entry.issue
            for entry in trusted_baseline.history
            if entry.action == "untrusted" and trusted_issue < entry.issue < issue_number
        )
    )
    if not skipped_untrusted_issues:
        return None

    latest_untrusted = trusted_baseline.last_untrusted
    if latest_untrusted is None or latest_untrusted.issue not in skipped_untrusted_issues:
        return ContinuationContractBaselineGap(skipped_untrusted_issues=skipped_untrusted_issues)

    return ContinuationContractBaselineGap(
        skipped_untrusted_issues=skipped_untrusted_issues,
        latest_untrusted_issue=latest_untrusted.issue,
        latest_untrusted_at=latest_untrusted.at,
        latest_untrusted_by=latest_untrusted.by,
        latest_untrusted_reason=latest_untrusted.reason,
    )


def _load_inherited_sections(
    edition_name: str,
    trusted_issue: int,
    *,
    reports_root: Path,
    archive_root: Path,
) -> tuple[tuple[str, ...], str | None]:
    archived = load_archived_narratives(edition_name, trusted_issue, archive_root=archive_root)
    if archived:
        return tuple(sorted(archived)), "archive"
    local = load_narratives(edition_name, trusted_issue, reports_root=reports_root)
    if local:
        return tuple(sorted(local)), "program_local"
    return (), None


def _build_source_hashes(
    edition_name: str,
    trusted_issue: int,
    *,
    reports_root: Path,
    archive_root: Path,
) -> dict[str, str]:
    archived = load_archived_narratives(edition_name, trusted_issue, archive_root=archive_root)
    source = archived if archived else load_narratives(edition_name, trusted_issue, reports_root=reports_root)
    return {
        filename: f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"
        for filename, content in source.items()
    }


def _normalize_section_id(filename: str) -> str | None:
    if filename == "exec_summary.md":
        return "exec_summary"
    if filename.startswith("ws_") and filename.endswith(".md"):
        return filename[3:-3]
    if filename.startswith("chapter_") and filename.endswith(".md"):
        return filename[8:-3]
    return None


def _build_evidence_quality(
    workstream_data: tuple[WorkstreamData, ...],
) -> tuple[tuple[str, ...], ContinuationContractEvidenceQuality]:
    ado_coverage = 0
    query_only = 0
    connector_only = 0
    manual_only = 0
    sections_missing_evidence: list[str] = []
    for section in workstream_data:
        has_item_citation = any(citation.work_item_id is not None for citation in section.citations)
        has_query_citation = any(citation.work_item_id is None and citation.display_label == "ADO query" for citation in section.citations)
        if has_item_citation:
            ado_coverage += 1
            continue
        if has_query_citation:
            query_only += 1
            sections_missing_evidence.append(section.section_id)
            continue
        if section.items:
            connector_only += 1
            sections_missing_evidence.append(section.section_id)
            continue
        manual_only += 1
        sections_missing_evidence.append(section.section_id)
    return tuple(sorted(sections_missing_evidence)), ContinuationContractEvidenceQuality(
        sections_with_ado_coverage=ado_coverage,
        sections_with_query_only=query_only,
        sections_with_connector_only=connector_only,
        sections_manual_only=manual_only,
    )

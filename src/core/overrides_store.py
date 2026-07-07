from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import date
from pathlib import Path
import re
from typing import Any

import yaml

from src.core.baseline_lock import assert_issue_unlocked
from src.core.config_loader import load_bundle
from src.core.edition_resolver import resolve_edition_paths
from src.core.exceptions import ConfigError
from src.core.models import ConfirmedDimension, RiskLevel
from src.core.persona_models import PersonaOverride
from src.core.snapshot_store import ARCHIVE_ROOT, get_archive_root


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = REPO_ROOT / "reports"
PROGRAMS_ROOT = REPO_ROOT / "programs"
NEEDS_INPUT_VALUE = "❓ Needs input"


@dataclass(frozen=True, slots=True)
class Top3NowEntry:
    type: str
    text: str
    owner: str
    ado_link: str
    anchor: str
    by_date: date | None = None


@dataclass(frozen=True, slots=True)
class DecisionStripAck:
    no_leadership_ask: bool = False
    reason: str | None = None
    acknowledged_by: str | None = None
    acknowledged_at: str | None = None


@dataclass(frozen=True, slots=True)
class DimensionOverride:
    name: str
    risk: RiskLevel | None
    label: str | None = None
    note: str | None = None
    summary: str | None = None
    eta: date | None = None
    hide_details: bool = False
    hide_from_scorecard: bool = False
    # FR-SG-18 scaffolding: provenance metadata for future Judgment migration (§7.4).
    # Full lifecycle (FR-SG-58/59) is HUMAN GATE; these fields enable structured carry-forward.
    owner: str | None = None        # alias of the person who set this override
    reason: str | None = None       # why this override was applied
    review_date: date | None = None  # scheduled review date; overdue if past
    expiry_date: date | None = None  # override auto-expires after this date


@dataclass(frozen=True, slots=True)
class ScorecardOverrides:
    name: str
    dimensions: tuple[DimensionOverride, ...]
    footnote: str | None = None


@dataclass(frozen=True, slots=True)
class RemovedDimension:
    scorecard_name: str
    dimension_name: str


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    id: str                    # stable, e.g. "contoso-023-prod-gate"
    workstream: str
    type: str                  # "gate" | "commitment" | "escalation" | "architecture"
    statement: str             # human-authored canonical statement
    source_type: str           # "meeting" | "email" | "ado" | "manual"
    source_ref: str            # meeting series_id, email subject, or ADO item URL
    owner: str                 # alias
    status: str                # "active" | "superseded" | "resolved"
    effective_date: date
    resolved_date: date | None = None


@dataclass(frozen=True, slots=True)
class GovernanceState:
    dfd_date: date | None = None           # current DFD
    dfd_history: tuple[date, ...] = ()     # confirmed: 2026-03-31, 2026-04-15, 2026-06-03
    escalation_active: bool = False
    escalation_workstreams: tuple[str, ...] = ()
    lt_commitment: str | None = None       # e.g. "PF LT committed to deliver P0 items..."
    lt_commitment_date: date | None = None


@dataclass(frozen=True, slots=True)
class OverridesDocument:
    issue_number: int | None
    top_3_now: tuple[Top3NowEntry, ...]
    scorecards: tuple[ScorecardOverrides, ...]
    focused_include: tuple[str, ...] = ()
    edition_intro: str | None = None
    chapter_subtitles: dict[str, str] = field(default_factory=dict)
    chapter_owner_overrides: dict[str, str] = field(default_factory=dict)
    forwarding_context: str | None = None
    health_bluf: str | None = None
    leadership_ask: str | None = None
    show_orientation: bool = False
    decision_strip_ack: DecisionStripAck | None = None
    removed_dimensions: tuple[RemovedDimension, ...] = ()
    removed_sections: tuple[str, ...] = ()
    persona_overrides: tuple[PersonaOverride, ...] = ()
    governance: GovernanceState = field(default_factory=GovernanceState)
    decisions: tuple[DecisionRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class OverridesSeedingState:
    seeded: bool
    source_issue: int | None
    fields_carried: tuple[str, ...] = ()
    fields_cleared: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OverrideMergeStats:
    preserved_count: int
    added_count: int
    removed_count: int


def get_overrides_path(
    edition: str,
    reports_root: Path = REPORTS_ROOT,
    *,
    issue_number: int | None = None,
) -> Path:
    resolved_paths = resolve_edition_paths(
        edition,
        programs_root=reports_root.parent / "programs",
    )
    if resolved_paths is not None:
        resolved_issue_number = issue_number or _detect_latest_v2_issue_number(resolved_paths.program_dir)
        if resolved_issue_number is None:
            return resolved_paths.program_dir / "overrides" / "issue_000.yaml"
        return resolved_paths.program_dir / "overrides" / f"issue_{resolved_issue_number:03d}.yaml"
    return reports_root / edition / "overrides.yaml"


def load_overrides(
    edition: str,
    reports_root: Path = REPORTS_ROOT,
    *,
    issue_number: int | None = None,
) -> OverridesDocument | None:
    path = get_overrides_path(edition, reports_root, issue_number=issue_number)
    return _load_overrides_from_path(path)


def load_archived_overrides(
    edition: str,
    issue_number: int,
    archive_root: Path = ARCHIVE_ROOT,
) -> OverridesDocument | None:
    path = get_archive_root(edition, archive_root) / "overrides" / f"issue_{issue_number:03d}.yaml"
    return _load_overrides_from_path(path)


def load_latest_program_overrides(
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> OverridesDocument | None:
    program_dir = programs_root / program_id
    issue_number = _detect_latest_v2_issue_number(program_dir)
    if issue_number is None:
        return None
    return _load_overrides_from_path(program_dir / "overrides" / f"issue_{issue_number:03d}.yaml")


def seed_overrides_from_prior(
    edition: str,
    *,
    target_issue_number: int,
    source_issue_number: int,
    reports_root: Path = REPORTS_ROOT,
    archive_root: Path = ARCHIVE_ROOT,
    program_id: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> OverridesSeedingState:
    current_path = get_overrides_path(edition, reports_root, issue_number=target_issue_number)
    current_document = _load_overrides_from_path(current_path)
    if current_document is not None and not _is_seed_like_document(current_document):
        return OverridesSeedingState(seeded=False, source_issue=source_issue_number)

    source_document = load_overrides(edition, reports_root=reports_root, issue_number=source_issue_number)
    if source_document is None:
        source_document = load_archived_overrides(edition, source_issue_number, archive_root=archive_root)
    if source_document is None:
        return OverridesSeedingState(seeded=False, source_issue=source_issue_number)

    seeded_scorecards = _overlay_judgment_facts(source_document.scorecards, program_id, programs_root)
    seeded_document = OverridesDocument(
        issue_number=target_issue_number,
        top_3_now=(),
        scorecards=seeded_scorecards,
        focused_include=source_document.focused_include,
        edition_intro=_append_stale_review_marker(source_document.edition_intro),
        chapter_subtitles=dict(source_document.chapter_subtitles),
        chapter_owner_overrides=dict(source_document.chapter_owner_overrides),
        forwarding_context=_append_stale_review_marker(source_document.forwarding_context),
        health_bluf=None,
        leadership_ask=None,
        show_orientation=False,
        decision_strip_ack=None,
        removed_dimensions=source_document.removed_dimensions,
        removed_sections=source_document.removed_sections,
        persona_overrides=source_document.persona_overrides,
        governance=source_document.governance,
        decisions=source_document.decisions,
    )
    save_overrides(edition, seeded_document, reports_root=reports_root)
    return OverridesSeedingState(
        seeded=True,
        source_issue=source_issue_number,
        fields_carried=_seeded_fields_carried(source_document),
        fields_cleared=_seeded_fields_cleared(source_document),
    )


def _overlay_judgment_facts(
    scorecards: tuple[ScorecardOverrides, ...],
    program_id: str | None,
    programs_root: Path,
) -> tuple[ScorecardOverrides, ...]:
    """FR-SG-58: overlay fact-store judgment risk levels onto YAML scorecard overrides.

    If program_id is None or no judgment facts exist, returns scorecards unchanged (backward compat).
    Only `judgment.dimension` type facts are considered; other scorecard fields are preserved.
    """
    if program_id is None:
        return scorecards
    try:
        from src.core.program_fact_store import load_current_judgments  # deferred — no zone violation (both Zone A)
        judgments = load_current_judgments(program_id, home_root=programs_root.parent)
    except Exception:
        return scorecards
    if not judgments:
        return scorecards
    # Build dimension_name → risk_level mapping from most-recent judgment (newest decided_at wins)
    dim_risk: dict[str, str] = {}
    for j in sorted(judgments, key=lambda x: x.decided_at):
        dim_risk[j.dimension] = j.risk_level
    if not dim_risk:
        return scorecards
    updated_scorecards: list[ScorecardOverrides] = []
    for sc in scorecards:
        updated_dims: list[DimensionOverride] = []
        changed = False
        for dim in sc.dimensions:
            if dim.name in dim_risk:
                new_risk = _parse_risk_level(dim_risk[dim.name])
                if new_risk is not None and new_risk != dim.risk:
                    from dataclasses import replace as _dc_replace
                    dim = _dc_replace(dim, risk=new_risk)
                    changed = True
            updated_dims.append(dim)
        if changed:
            from dataclasses import replace as _dc_replace
            sc = _dc_replace(sc, dimensions=tuple(updated_dims))
        updated_scorecards.append(sc)
    return tuple(updated_scorecards)


def _parse_risk_level(value: str | None) -> "RiskLevel | None":
    if value is None:
        return None
    try:
        return RiskLevel(value)
    except ValueError:
        return None


def _load_overrides_from_path(path: Path) -> OverridesDocument | None:
    if not path.exists():
        return None

    raw_document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    top_3_now = tuple(
        Top3NowEntry(
            type=_string_or_default(_require_mapping(entry, field_name="top_3_now entry").get("type"), default="", field_name="top_3_now.type"),
            text=_string_or_default(_require_mapping(entry, field_name="top_3_now entry").get("text"), default="", field_name="top_3_now.text"),
            owner=_string_or_default(_require_mapping(entry, field_name="top_3_now entry").get("owner"), default="", field_name="top_3_now.owner"),
            ado_link=_string_or_default(_require_mapping(entry, field_name="top_3_now entry").get("ado_link"), default="", field_name="top_3_now.ado_link"),
            anchor=_string_or_default(_require_mapping(entry, field_name="top_3_now entry").get("anchor"), default="", field_name="top_3_now.anchor"),
            by_date=_parse_top_item_date(entry.get("by_date")),
        )
        for entry in raw_document.get("top_3_now", [])
    )
    scorecards = _parse_scorecards(raw_document.get("scorecards", {}))
    issue_number = raw_document.get("issue_number")
    decision_strip_ack = _parse_decision_strip_ack(raw_document.get("decision_strip_ack"))
    
    governance = _parse_governance_state(raw_document.get("governance"))
    decisions = tuple(
        _parse_decision_record(dec)
        for dec in (raw_document.get("decisions") or [])
    )
    
    return OverridesDocument(
        issue_number=_optional_int(issue_number, field_name="issue_number"),
        top_3_now=top_3_now,
        scorecards=scorecards,
        focused_include=_parse_focused_include(raw_document.get("focused_include")),
        edition_intro=_optional_string(raw_document.get("edition_intro")),
        chapter_subtitles=_parse_named_string_map(raw_document.get("chapter_subtitles"), field_name="chapter_subtitles"),
        chapter_owner_overrides=_parse_named_string_map(raw_document.get("chapter_owner_overrides"), field_name="chapter_owner_overrides"),
        forwarding_context=_optional_string(raw_document.get("forwarding_context")),
        health_bluf=_optional_string(raw_document.get("health_bluf")),
        leadership_ask=_optional_string(raw_document.get("leadership_ask")),
        show_orientation=_parse_optional_bool(raw_document.get("show_orientation"), default=False),
        decision_strip_ack=decision_strip_ack,
        removed_dimensions=_parse_removed_dimensions(raw_document.get("removed_dimensions")),
        removed_sections=_parse_removed_sections(raw_document.get("removed_sections")),
        persona_overrides=_parse_persona_overrides(raw_document.get("persona_overrides")),
        governance=governance,
        decisions=decisions,
    )


def merge_overrides(
    issue_number: int,
    expected_scorecards: dict[str, tuple[str, ...]],
    existing: OverridesDocument | None,
) -> tuple[OverridesDocument, OverrideMergeStats]:
    existing_scorecards = _scorecards_to_map(existing.scorecards if existing is not None else ())
    merged_scorecards: list[ScorecardOverrides] = []
    removed_dimensions: list[RemovedDimension] = []
    preserved_count = 0
    added_count = 0

    for scorecard_name, expected_dimensions in expected_scorecards.items():
        existing_dimensions = existing_scorecards.get(scorecard_name, {})
        merged_dimensions: list[DimensionOverride] = []
        expected_names = set(expected_dimensions)

        for dimension_name in expected_dimensions:
            if dimension_name in existing_dimensions:
                merged_dimensions.append(existing_dimensions[dimension_name])
                preserved_count += 1
            else:
                merged_dimensions.append(DimensionOverride(name=dimension_name, risk=None))
                added_count += 1

        for dimension_name in existing_dimensions:
            if dimension_name not in expected_names:
                removed_dimensions.append(
                    RemovedDimension(
                        scorecard_name=scorecard_name,
                        dimension_name=dimension_name,
                    )
                )

        existing_scorecard = next((s for s in (existing.scorecards if existing is not None else ()) if s.name == scorecard_name), None)
        merged_scorecards.append(
            ScorecardOverrides(
                name=scorecard_name,
                dimensions=tuple(merged_dimensions),
                footnote=existing_scorecard.footnote if existing_scorecard is not None else None,
            )
        )

    for scorecard_name, dimensions in existing_scorecards.items():
        if scorecard_name in expected_scorecards:
            continue
        removed_dimensions.extend(
            RemovedDimension(scorecard_name=scorecard_name, dimension_name=dimension_name)
            for dimension_name in dimensions
        )

    # Preserve removed_dimensions entries from the existing document that were
    # previously computed and saved (e.g. archive-inherited dimensions removed in
    # a prior run).  Without this, dimensions stripped from the scorecards section
    # on one run would silently reappear from archive inheritance on the next run.
    if existing is not None:
        expected_sets = {sn: set(dims) for sn, dims in expected_scorecards.items()}
        already_removed = {(rd.scorecard_name, rd.dimension_name) for rd in removed_dimensions}
        for entry in existing.removed_dimensions:
            if (entry.scorecard_name, entry.dimension_name) in already_removed:
                continue
            if entry.dimension_name not in expected_sets.get(entry.scorecard_name, set()):
                removed_dimensions.append(entry)

    document = OverridesDocument(
        issue_number=issue_number,
        top_3_now=existing.top_3_now if existing is not None else (),
        scorecards=tuple(merged_scorecards),
        focused_include=existing.focused_include if existing is not None else (),
        edition_intro=existing.edition_intro if existing is not None else None,
        chapter_subtitles=dict(existing.chapter_subtitles) if existing is not None else {},
        chapter_owner_overrides=dict(existing.chapter_owner_overrides) if existing is not None else {},
        forwarding_context=existing.forwarding_context if existing is not None else None,
        health_bluf=existing.health_bluf if existing is not None else None,
        leadership_ask=existing.leadership_ask if existing is not None else None,
        show_orientation=existing.show_orientation if existing is not None else False,
        decision_strip_ack=existing.decision_strip_ack if existing is not None else None,
        removed_dimensions=tuple(removed_dimensions),
        removed_sections=existing.removed_sections if existing is not None else (),
        persona_overrides=existing.persona_overrides if existing is not None else (),
        governance=existing.governance if existing is not None else GovernanceState(),
        decisions=existing.decisions if existing is not None else (),
    )
    stats = OverrideMergeStats(
        preserved_count=preserved_count,
        added_count=added_count,
        removed_count=len(removed_dimensions),
    )
    return document, stats


def save_overrides(
    edition: str,
    document: OverridesDocument,
    reports_root: Path = REPORTS_ROOT,
) -> Path:
    path = get_overrides_path(edition, reports_root, issue_number=document.issue_number)
    # Hardlock: never silently overwrite the overrides of a trusted/locked baseline issue.
    assert_issue_unlocked(document.issue_number, target_path=path, artifact="overrides")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_overrides_document(document), encoding="utf-8")
    return path


def apply_pending_overrides(
    edition_name: str,
    report_dir: Path,
    reports_root: Path = REPORTS_ROOT,
) -> Path | None:
    issue_number = _resolve_pending_override_issue_number(report_dir)
    if issue_number is None:
        return None
    bundle = load_bundle(edition_name, reports_root=reports_root)
    expected_scorecards = {
        scorecard.name: tuple(dimension.name for dimension in scorecard.dimensions)
        for scorecard in bundle.config.scorecards
    }
    overrides_document, _ = merge_overrides(
        issue_number=issue_number,
        expected_scorecards=expected_scorecards,
        existing=load_overrides(edition_name, reports_root=reports_root, issue_number=issue_number),
    )
    return save_overrides(edition_name, overrides_document, reports_root=reports_root)


def delete_seedable_overrides(
    edition: str,
    issue_number: int,
    reports_root: Path = REPORTS_ROOT,
) -> bool:
    path = get_overrides_path(edition, reports_root, issue_number=issue_number)
    document = _load_overrides_from_path(path)
    if document is None or not _is_seed_like_document(document):
        return False
    path.unlink(missing_ok=True)
    return True


def archive_overrides(
    edition: str,
    issue_number: int,
    document: OverridesDocument,
    archive_root: Path = ARCHIVE_ROOT,
) -> Path:
    archive_path = get_archive_root(edition, archive_root) / "overrides" / f"issue_{issue_number:03d}.yaml"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(_render_overrides_document(document), encoding="utf-8")
    return archive_path


def reset_overrides_for_next_issue(
    edition: str,
    next_issue_number: int,
    confirmed_dimensions: tuple[ConfirmedDimension, ...],
    reports_root: Path = REPORTS_ROOT,
) -> OverridesDocument:
    grouped_dimensions: dict[str, list[DimensionOverride]] = {}
    for dimension in confirmed_dimensions:
        grouped_dimensions.setdefault(dimension.scorecard_name, []).append(
            DimensionOverride(
                name=dimension.name,
                risk=None,
            )
        )

    document = OverridesDocument(
        issue_number=next_issue_number,
        top_3_now=(),
        scorecards=tuple(
            ScorecardOverrides(name=scorecard_name, dimensions=tuple(dimensions))
            for scorecard_name, dimensions in grouped_dimensions.items()
        ),
        focused_include=(),
        edition_intro=None,
        chapter_subtitles={},
        chapter_owner_overrides={},
        forwarding_context=None,
        health_bluf=None,
        leadership_ask=None,
        show_orientation=False,
        decision_strip_ack=None,
        removed_sections=(),
        persona_overrides=(),
    )
    save_overrides(edition, document, reports_root)
    return document


_DRAFT_REPORT_FILENAME_RE = re.compile(r"issue_(\d+)\.draft\.json$")


def _resolve_pending_override_issue_number(report_dir: Path) -> int | None:
    draft_paths = [path for path in report_dir.glob("issue_*/issue_*.draft.json") if path.is_file()]
    if not draft_paths:
        return None
    latest_draft_path = max(draft_paths, key=lambda path: path.stat().st_mtime_ns)
    match = _DRAFT_REPORT_FILENAME_RE.fullmatch(latest_draft_path.name)
    if match is None:
        return None
    return int(match.group(1))


def _detect_latest_v2_issue_number(program_dir: Path) -> int | None:
    overrides_dir = program_dir / "overrides"
    if not overrides_dir.exists():
        return None
    issue_numbers: list[int] = []
    for path in overrides_dir.glob("issue_*.yaml"):
        stem = path.stem
        _, _, suffix = stem.partition("issue_")
        if suffix.isdigit():
            issue_numbers.append(int(suffix))
    if not issue_numbers:
        return None
    return max(issue_numbers)


def _parse_scorecards(raw_scorecards: Any) -> tuple[ScorecardOverrides, ...]:
    if isinstance(raw_scorecards, dict):
        result: list[ScorecardOverrides] = []
        for scorecard_name, scorecard_dict in raw_scorecards.items():
            if not isinstance(scorecard_dict, dict):
                continue
            footnote = scorecard_dict.get("footnote")
            result.append(ScorecardOverrides(
                name=str(scorecard_name),
                footnote=str(footnote) if footnote is not None else None,
                dimensions=tuple(
                    _parse_dimension_override(dimension_name, dimension_payload)
                    for dimension_name, dimension_payload in scorecard_dict.items()
                    if dimension_name != "footnote"
                ),
            ))
        return tuple(result)

    if isinstance(raw_scorecards, list):
        parsed_scorecards: list[ScorecardOverrides] = []
        for scorecard in raw_scorecards:
            parsed_scorecards.append(
                ScorecardOverrides(
                    name=str(scorecard.get("name", "")),
                    dimensions=tuple(
                        _parse_dimension_override_from_list_entry(dimension)
                        for dimension in scorecard.get("dimensions", [])
                    ),
                )
            )
        return tuple(parsed_scorecards)

    return ()


def _parse_dimension_override(dimension_name: str, payload: Any) -> DimensionOverride:
    if isinstance(payload, dict):
        return DimensionOverride(
            name=dimension_name,
            risk=_parse_override_risk(payload.get("risk")),
            label=_optional_string(payload.get("label")),
            note=_optional_string(payload.get("note")),
            summary=_optional_string(payload.get("summary")),
            eta=_parse_override_eta(payload.get("eta")),
            hide_details=_parse_hide_details(payload.get("hide_details")),
            hide_from_scorecard=_parse_hide_details(payload.get("hide_from_scorecard")),
            owner=_optional_string(payload.get("owner")),
            reason=_optional_string(payload.get("reason")),
            review_date=_parse_override_eta(payload.get("review_date")),
            expiry_date=_parse_override_eta(payload.get("expiry_date")),
        )

    return DimensionOverride(
        name=dimension_name,
        risk=_parse_override_risk(payload),
    )


def _parse_dimension_override_from_list_entry(entry: dict[str, Any]) -> DimensionOverride:
    return DimensionOverride(
        name=str(entry.get("name", "")),
        risk=_parse_override_risk(entry.get("risk")),
        label=_optional_string(entry.get("label")),
        note=_optional_string(entry.get("note")),
        summary=_optional_string(entry.get("summary")),
        eta=_parse_override_eta(entry.get("eta")),
        hide_details=_parse_hide_details(entry.get("hide_details")),
        hide_from_scorecard=_parse_hide_details(entry.get("hide_from_scorecard")),
        owner=_optional_string(entry.get("owner")),
        reason=_optional_string(entry.get("reason")),
        review_date=_parse_override_eta(entry.get("review_date")),
        expiry_date=_parse_override_eta(entry.get("expiry_date")),
    )


def _parse_override_risk(value: Any) -> RiskLevel | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() in {"", NEEDS_INPUT_VALUE, "needs_input", "unknown"}:
        return None
    return RiskLevel.from_string(str(value))


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigError("Expected string value in overrides.yaml")
    return value


def _string_or_default(value: Any, *, default: str, field_name: str) -> str:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a string in overrides.yaml")
    return value


def _optional_int(value: Any, *, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{field_name} must be an integer in overrides.yaml")
    return value


def _require_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} must be a mapping in overrides.yaml")
    return value


def _parse_override_eta(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            return date.fromisoformat(normalized)
        except ValueError as exc:
            raise ConfigError(
                f"Unsupported eta value in overrides.yaml: {value!r}. Use YYYY-MM-DD."
            ) from exc
    raise ConfigError(f"Unsupported eta value in overrides.yaml: {value!r}. Use YYYY-MM-DD.")


def _parse_top_item_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"Unsupported by_date value in overrides.yaml: {value!r}") from exc


def _parse_optional_bool(value: Any, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "y", "1"}:
            return True
        if normalized in {"false", "no", "n", "0"}:
            return False
    raise ConfigError(f"Unsupported boolean value in overrides.yaml: {value!r}")


def _parse_focused_include(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError("focused_include must be a list in overrides.yaml")
    parsed: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ConfigError("focused_include entries must be strings in overrides.yaml")
        text = entry.strip()
        if text:
            parsed.append(text)
    return tuple(parsed)


def _parse_removed_dimensions(value: Any) -> tuple[RemovedDimension, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError("removed_dimensions must be a list in overrides.yaml")
    removed_dimensions: list[RemovedDimension] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ConfigError("removed_dimensions entries must be mappings in overrides.yaml")
        scorecard_name = str(entry.get("scorecard_name", "")).strip()
        dimension_name = str(entry.get("dimension_name", "")).strip()
        if scorecard_name and dimension_name:
            removed_dimensions.append(RemovedDimension(scorecard_name=scorecard_name, dimension_name=dimension_name))
    return tuple(removed_dimensions)


def _parse_removed_sections(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError("removed_sections must be a list in overrides.yaml")
    parsed: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ConfigError("removed_sections entries must be strings in overrides.yaml")
        text = entry.strip()
        if text:
            parsed.append(text)
    return tuple(parsed)


def _parse_persona_overrides(value: Any) -> tuple[PersonaOverride, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError("persona_overrides must be a list in overrides.yaml")
    overrides: list[PersonaOverride] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ConfigError("persona_overrides entries must be mappings in overrides.yaml")
        check_id = str(entry.get("check_id", "")).strip()
        override_severity = str(entry.get("override_severity", "")).strip().lower()
        reason = str(entry.get("reason", "")).strip()
        expires = str(entry.get("expires", "")).strip()
        approved_by = str(entry.get("approved_by", "")).strip()
        if not check_id:
            raise ConfigError("persona_overrides.check_id is required")
        if override_severity not in {"warn"}:
            raise ConfigError("persona_overrides.override_severity may only downgrade to warn")
        if not reason or not expires or not approved_by:
            raise ConfigError("persona_overrides require reason, expires, and approved_by")
        try:
            date.fromisoformat(expires)
        except ValueError as exc:
            raise ConfigError("persona_overrides.expires must use YYYY-MM-DD") from exc
        overrides.append(
            PersonaOverride(
                check_id=check_id,
                override_severity=override_severity,
                reason=reason,
                expires=expires,
                approved_by=approved_by,
                location=_optional_string(entry.get("location")),
                scope=_optional_string(entry.get("scope")),
            )
        )
    return tuple(overrides)


def _parse_named_string_map(value: Any, *, field_name: str) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} must be a mapping in overrides.yaml")
    parsed: dict[str, str] = {}
    for key, raw_value in value.items():
        normalized_key = str(key).strip()
        normalized_value = _optional_string(raw_value)
        if not normalized_key or normalized_value is None:
            continue
        parsed[normalized_key] = normalized_value
    return parsed


def _parse_decision_strip_ack(value: Any) -> DecisionStripAck | None:
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise ConfigError("decision_strip_ack must be a mapping in overrides.yaml")
    return DecisionStripAck(
        no_leadership_ask=_parse_optional_bool(value.get("no_leadership_ask"), default=False),
        reason=_optional_string(value.get("reason")),
        acknowledged_by=_optional_string(value.get("acknowledged_by")),
        acknowledged_at=_optional_string(value.get("acknowledged_at")),
    )


def _parse_date(value: Any, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            return date.fromisoformat(normalized)
        except ValueError as exc:
            raise ConfigError(
                f"Unsupported date value for {field_name} in overrides.yaml: {value!r}. Use YYYY-MM-DD."
            ) from exc
    raise ConfigError(f"Unsupported date value for {field_name} in overrides.yaml: {value!r}. Use YYYY-MM-DD.")


def _parse_decision_record(entry: Any) -> DecisionRecord:
    if not isinstance(entry, dict):
        raise ConfigError("decisions entries must be mappings in overrides.yaml")
    
    effective_date = _parse_date(entry.get("effective_date"), "decisions.effective_date")
    if effective_date is None:
        effective_date = date.today()
        
    return DecisionRecord(
        id=str(entry.get("id", "")).strip(),
        workstream=str(entry.get("workstream", "")).strip(),
        type=str(entry.get("type", "")).strip(),
        statement=str(entry.get("statement", "")).strip(),
        source_type=str(entry.get("source_type", "")).strip(),
        source_ref=str(entry.get("source_ref", "")).strip(),
        owner=str(entry.get("owner", "")).strip(),
        status=str(entry.get("status", "")).strip(),
        effective_date=effective_date,
        resolved_date=_parse_date(entry.get("resolved_date"), "decisions.resolved_date"),
    )


def _parse_governance_state(payload: Any) -> GovernanceState:
    if payload in (None, ""):
        return GovernanceState()
    if not isinstance(payload, dict):
        raise ConfigError("governance must be a mapping in overrides.yaml")
    
    dfd_history: list[date] = []
    raw_history = payload.get("dfd_history", [])
    if isinstance(raw_history, list):
        for item in raw_history:
            parsed = _parse_date(item, "governance.dfd_history")
            if parsed is not None:
                dfd_history.append(parsed)
    elif raw_history not in (None, ""):
        raise ConfigError("governance.dfd_history must be a list in overrides.yaml")
        
    return GovernanceState(
        dfd_date=_parse_date(payload.get("dfd_date"), "governance.dfd_date"),
        dfd_history=tuple(dfd_history),
        escalation_active=_parse_optional_bool(payload.get("escalation_active"), default=False),
        escalation_workstreams=_parse_string_list(payload.get("escalation_workstreams"), field_name="governance.escalation_workstreams"),
        lt_commitment=_optional_string(payload.get("lt_commitment")),
        lt_commitment_date=_parse_date(payload.get("lt_commitment_date"), "governance.lt_commitment_date"),
    )


def _parse_string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{field_name} must be a list in overrides.yaml")
    parsed: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ConfigError(f"{field_name} entries must be strings in overrides.yaml")
        text = entry.strip()
        if text:
            parsed.append(text)
    return tuple(parsed)


def _parse_hide_details(value: Any) -> bool:
    if value in (None, ""):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "y", "1"}:
            return True
        if normalized in {"false", "no", "n", "0"}:
            return False
    raise ConfigError(f"Unsupported hide_details value in overrides.yaml: {value!r}")


def _scorecards_to_map(
    scorecards: tuple[ScorecardOverrides, ...],
) -> dict[str, dict[str, DimensionOverride]]:
    return {
        scorecard.name: {dimension.name: dimension for dimension in scorecard.dimensions}
        for scorecard in scorecards
    }


def _render_overrides_document(document: OverridesDocument) -> str:
    payload: dict[str, Any] = {
        "issue_number": document.issue_number,
        "top_3_now": [
            {
                "type": entry.type,
                "text": entry.text,
                "owner": entry.owner,
                "ado_link": entry.ado_link,
                "anchor": entry.anchor,
                **({"by_date": entry.by_date.isoformat()} if entry.by_date is not None else {}),
            }
            for entry in document.top_3_now
        ],
        "scorecards": {},
    }
    
    if document.governance != GovernanceState():
        gov: dict[str, Any] = {}
        if document.governance.dfd_date is not None:
            gov["dfd_date"] = document.governance.dfd_date.isoformat()
        if document.governance.dfd_history:
            gov["dfd_history"] = [d.isoformat() for d in document.governance.dfd_history]
        if document.governance.escalation_active:
            gov["escalation_active"] = document.governance.escalation_active
        if document.governance.escalation_workstreams:
            gov["escalation_workstreams"] = list(document.governance.escalation_workstreams)
        if document.governance.lt_commitment is not None:
            gov["lt_commitment"] = document.governance.lt_commitment
        if document.governance.lt_commitment_date is not None:
            gov["lt_commitment_date"] = document.governance.lt_commitment_date.isoformat()
        payload["governance"] = gov
        
    if document.decisions:
        payload["decisions"] = [
            {
                "id": entry.id,
                "workstream": entry.workstream,
                "type": entry.type,
                "statement": entry.statement,
                "source_type": entry.source_type,
                "source_ref": entry.source_ref,
                "owner": entry.owner,
                "status": entry.status,
                "effective_date": entry.effective_date.isoformat(),
                **({"resolved_date": entry.resolved_date.isoformat()} if entry.resolved_date is not None else {}),
            }
            for entry in document.decisions
        ]
    if document.forwarding_context is not None:
        payload["forwarding_context"] = document.forwarding_context
    if document.focused_include:
        payload["focused_include"] = list(document.focused_include)
    if document.edition_intro is not None:
        payload["edition_intro"] = document.edition_intro
    if document.chapter_subtitles:
        payload["chapter_subtitles"] = document.chapter_subtitles
    if document.chapter_owner_overrides:
        payload["chapter_owner_overrides"] = document.chapter_owner_overrides
    if document.health_bluf is not None:
        payload["health_bluf"] = document.health_bluf
    if document.leadership_ask is not None:
        payload["leadership_ask"] = document.leadership_ask
    if document.show_orientation:
        payload["show_orientation"] = True
    if document.decision_strip_ack is not None:
        payload["decision_strip_ack"] = {
            "no_leadership_ask": document.decision_strip_ack.no_leadership_ask,
            **({"reason": document.decision_strip_ack.reason} if document.decision_strip_ack.reason is not None else {}),
            **({"acknowledged_by": document.decision_strip_ack.acknowledged_by} if document.decision_strip_ack.acknowledged_by is not None else {}),
            **({"acknowledged_at": document.decision_strip_ack.acknowledged_at} if document.decision_strip_ack.acknowledged_at is not None else {}),
        }
    if document.removed_dimensions:
        payload["removed_dimensions"] = [
            {
                "scorecard_name": removed.scorecard_name,
                "dimension_name": removed.dimension_name,
            }
            for removed in document.removed_dimensions
        ]
    if document.removed_sections:
        payload["removed_sections"] = list(document.removed_sections)
    if document.persona_overrides:
        payload["persona_overrides"] = [
            {
                "check_id": override.check_id,
                "override_severity": override.override_severity,
                "reason": override.reason,
                "expires": override.expires,
                "approved_by": override.approved_by,
                **({"location": override.location} if override.location is not None else {}),
                **({"scope": override.scope} if override.scope is not None else {}),
            }
            for override in document.persona_overrides
        ]
    scorecard_payload = payload["scorecards"]
    for scorecard in document.scorecards:
        dimension_payload: dict[str, Any] = {}
        if scorecard.footnote is not None:
            dimension_payload["footnote"] = scorecard.footnote
        for dimension in scorecard.dimensions:
            entry: dict[str, Any] = {
                "risk": dimension.risk.value if dimension.risk is not None else NEEDS_INPUT_VALUE,
            }
            if dimension.label is not None:
                entry["label"] = dimension.label
            if dimension.note is not None:
                entry["note"] = dimension.note
            if dimension.summary is not None:
                entry["summary"] = dimension.summary
            if dimension.eta is not None:
                entry["eta"] = dimension.eta.isoformat()
            if dimension.hide_details:
                entry["hide_details"] = True
            if dimension.owner is not None:
                entry["owner"] = dimension.owner
            if dimension.reason is not None:
                entry["reason"] = dimension.reason
            if dimension.review_date is not None:
                entry["review_date"] = dimension.review_date.isoformat()
            if dimension.expiry_date is not None:
                entry["expiry_date"] = dimension.expiry_date.isoformat()
            dimension_payload[dimension.name] = entry
        scorecard_payload[scorecard.name] = dimension_payload

    header = [
        "# Active Vertex overrides.yaml — set risk only when you need to override the derived issue risk.",
        "# Blank / ❓ Needs input risk falls back to the derived risk from the matched work items.",
        "# Set eta: YYYY-MM-DD to override the scorecard tile ETA without changing the underlying ADO dates.",
        "# Set hide_details: true to suppress that workstream detail section while keeping the scorecard row visible.",
        "# focused_include accepts detail section ids to keep in focused editions even when the section is otherwise unchanged.",
        "# top_3_now is structured data; narratives live under narratives/issue_NNN/*.md.",
        "",
    ]
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    trailer: list[str] = []
    if document.removed_dimensions:
        trailer.append("")
        for removed in document.removed_dimensions:
            trailer.append(
                f"# REMOVED — dimension no longer in config: {removed.scorecard_name} / {removed.dimension_name}"
            )
    if document.removed_sections:
        trailer.append("")
        for section_id in document.removed_sections:
            trailer.append(f"# REMOVED — section hidden by override: {section_id}")
        trailer.append("")
    return "\n".join(header) + body + ("\n".join(trailer) if trailer else "")


def _append_stale_review_marker(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    if "<!-- STALE — review -->" in value:
        return value
    return f"{value.rstrip()}\n\n<!-- STALE — review -->"


def _seeded_fields_carried(document: OverridesDocument) -> tuple[str, ...]:
    carried: list[str] = ["scorecards"]
    if document.edition_intro is not None:
        carried.append("edition_intro")
    if document.chapter_subtitles:
        carried.append("chapter_subtitles")
    if document.chapter_owner_overrides:
        carried.append("chapter_owner_overrides")
    if document.forwarding_context is not None:
        carried.append("forwarding_context")
    if document.removed_dimensions:
        carried.append("removed_dimensions")
    if document.removed_sections:
        carried.append("removed_sections")
    if document.focused_include:
        carried.append("focused_include")
    if document.governance != GovernanceState():
        carried.append("governance")
    if document.decisions:
        carried.append("decisions")
    return tuple(carried)


def _seeded_fields_cleared(document: OverridesDocument) -> tuple[str, ...]:
    cleared: list[str] = []
    if document.top_3_now:
        cleared.append("top_3_now")
    if document.health_bluf is not None:
        cleared.append("health_bluf")
    if document.leadership_ask is not None:
        cleared.append("leadership_ask")
    if document.decision_strip_ack is not None:
        cleared.append("decision_strip_ack")
    if document.show_orientation:
        cleared.append("show_orientation")
    return tuple(cleared)


def _is_seed_like_document(document: OverridesDocument) -> bool:
    if document.top_3_now:
        return False
    if document.focused_include:
        return False
    if document.edition_intro is not None:
        return False
    if document.chapter_subtitles:
        return False
    if document.chapter_owner_overrides:
        return False
    if document.forwarding_context is not None:
        return False
    if document.health_bluf is not None:
        return False
    if document.leadership_ask is not None:
        return False
    if document.show_orientation:
        return False
    if document.decision_strip_ack is not None:
        return False
    if document.removed_dimensions:
        return False
    if document.removed_sections:
        return False
    if document.governance != GovernanceState():
        return False
    if document.decisions:
        return False
    for scorecard in document.scorecards:
        for dimension in scorecard.dimensions:
            if dimension.risk is not None:
                return False
            if dimension.label is not None:
                return False
            if dimension.note is not None:
                return False
            if dimension.summary is not None:
                return False
            if dimension.eta is not None:
                return False
            if dimension.hide_details:
                return False
    return True

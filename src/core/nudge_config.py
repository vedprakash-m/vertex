"""Nudge edition config parser, validator, and backward-compat shim.

Public API:
    load_nudge_config(*, program_id, program, programs_root, templates_root=None) -> NudgeConfig
    validate_nudge_config(config, program) -> list[str]
    parse_stale_overrides(values) -> dict[str, int]
"""
from __future__ import annotations

import re
import warnings
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from src.core.exceptions import ConfigError
from src.core.nudge_models import (
    NUDGE_CANDIDATE_WORKERS_MAX,
    NUDGE_COMMENT_FETCH_LIMIT_DEFAULT,
    ActionDuePolicy,
    ExplicitActionDue,
    MilestoneRelativeActionDue,
    NudgeAudiencePolicy,
    NudgeConfig,
    NudgeDeliveryConfig,
    NudgeEvaluationConfig,
    NudgePresentationConfig,
    NudgeSectionCriteria,
    NudgeSectionSpec,
    NudgeWaiver,
    SendDateOffsetActionDue,
    WorkstreamHint,
)
from src.core.yaml_utils import load_yaml_mapping


_SECTION_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_LETTER_SEQUENCE = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# Default values per §24.3 precedence table
_DEFAULT_COOLDOWN_DAYS = 14
_DEFAULT_COMMENT_WINDOW_DAYS = 14
_DEFAULT_CADENCE_DAYS = 7
_DEFAULT_BRAND_LABEL = "Program Hygiene"
_DEFAULT_SUBJECT_LABEL = "ADO Hygiene Report"
_DEFAULT_TEMPLATE = "partials/nudge_full_hygiene.j2"
_DEFAULT_STALE_A = 2
_DEFAULT_STALE_B = 4
_DEFAULT_STALE_C = 6

# Accepted schema versions
_ACCEPTED_SCHEMA_VERSIONS = {"2.0", "2.1"}

# D-1: In schema 2.1, full_hygiene.cooldown_days/comment_window_days are canonical;
# hygiene. values are fallback-only and will emit a deprecation warning.
_SCHEMA_21 = "2.1"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_nudge_config(
    *,
    program_id: str,
    program: Any,  # Program — avoid circular import
    programs_root: Path,
    templates_root: Path | None = None,
) -> NudgeConfig:
    edition_path = programs_root / program_id / "editions" / f"{program_id}_nudge.yaml"
    raw = load_yaml_mapping(edition_path, required=True)

    errors: list[str] = []

    # Validate standard edition fields
    schema = str(raw.get("schema_version") or "")
    if schema not in _ACCEPTED_SCHEMA_VERSIONS:
        errors.append(f"schema_version must be one of {sorted(_ACCEPTED_SCHEMA_VERSIONS)!r}, got {schema!r}")

    if str(raw.get("id") or "") != f"{program_id}_nudge":
        errors.append(f"id must be '{program_id}_nudge'")

    if str(raw.get("program_id") or "") != program_id:
        errors.append(f"program_id must be '{program_id}'")

    if str(raw.get("type") or "") != "nudge":
        errors.append(f"type must be 'nudge'")

    if errors:
        raise ConfigError("Invalid nudge config:\n- " + "\n- ".join(errors))

    is_v21 = schema == _SCHEMA_21

    # D-1 config ownership:
    #  2.0: hygiene.cooldown_days / hygiene.comment_window_days are canonical
    #  2.1: full_hygiene.cooldown_days / full_hygiene.comment_window_days are canonical;
    #       hygiene. values are fallback-only (deprecation-warned if present and differ)
    hygiene_raw = raw.get("hygiene") or {}
    if not isinstance(hygiene_raw, dict):
        hygiene_raw = {}
    hygiene_cooldown = _pos_int(hygiene_raw.get("cooldown_days"), _DEFAULT_COOLDOWN_DAYS) if hygiene_raw.get("cooldown_days") is not None else None
    hygiene_comment_window = _pos_int(hygiene_raw.get("comment_window_days"), _DEFAULT_COMMENT_WINDOW_DAYS) if hygiene_raw.get("comment_window_days") is not None else None

    fh = raw.get("full_hygiene")
    if not isinstance(fh, dict):
        fh = {}

    fh_cooldown = _pos_int(fh.get("cooldown_days"), _DEFAULT_COOLDOWN_DAYS) if fh.get("cooldown_days") is not None else None
    fh_comment_window = _pos_int(fh.get("comment_window_days"), _DEFAULT_COMMENT_WINDOW_DAYS) if fh.get("comment_window_days") is not None else None

    if is_v21:
        # 2.1: full_hygiene is canonical; hygiene is fallback only
        if fh_cooldown is not None:
            global_cooldown = fh_cooldown
            if hygiene_cooldown is not None and hygiene_cooldown != fh_cooldown:
                raise ConfigError(
                    f"nudge config conflict (D-1): hygiene.cooldown_days={hygiene_cooldown} "
                    f"!= full_hygiene.cooldown_days={fh_cooldown}. They must be equal or remove hygiene.cooldown_days."
                )
            if hygiene_cooldown is not None:
                warnings.warn(
                    f"[nudge:{program_id}] schema 2.1: cooldown_days is canonical in full_hygiene; "
                    "hygiene.cooldown_days is deprecated. Remove it.",
                    DeprecationWarning, stacklevel=3,
                )
        elif hygiene_cooldown is not None:
            global_cooldown = hygiene_cooldown
            warnings.warn(
                f"[nudge:{program_id}] schema 2.1: move cooldown_days to full_hygiene (hygiene fallback used).",
                DeprecationWarning, stacklevel=3,
            )
        else:
            global_cooldown = _DEFAULT_COOLDOWN_DAYS

        if fh_comment_window is not None:
            comment_window = fh_comment_window
            if hygiene_comment_window is not None and hygiene_comment_window != fh_comment_window:
                raise ConfigError(
                    f"nudge config conflict (D-1): hygiene.comment_window_days={hygiene_comment_window} "
                    f"!= full_hygiene.comment_window_days={fh_comment_window}. They must be equal or remove hygiene.comment_window_days."
                )
            if hygiene_comment_window is not None:
                warnings.warn(
                    f"[nudge:{program_id}] schema 2.1: comment_window_days is canonical in full_hygiene; "
                    "hygiene.comment_window_days is deprecated. Remove it.",
                    DeprecationWarning, stacklevel=3,
                )
        elif hygiene_comment_window is not None:
            comment_window = hygiene_comment_window
            warnings.warn(
                f"[nudge:{program_id}] schema 2.1: move comment_window_days to full_hygiene (hygiene fallback used).",
                DeprecationWarning, stacklevel=3,
            )
        else:
            comment_window = _DEFAULT_COMMENT_WINDOW_DAYS
    else:
        # 2.0: hygiene. values are canonical (existing behavior)
        global_cooldown = hygiene_cooldown if hygiene_cooldown is not None else _DEFAULT_COOLDOWN_DAYS
        comment_window = hygiene_comment_window if hygiene_comment_window is not None else _DEFAULT_COMMENT_WINDOW_DAYS

    # Determine if this is new-format (has sections) or old-format (legacy flat keys)
    has_new_sections = isinstance(fh.get("sections"), list)
    has_legacy_keys = any(
        k in fh for k in ("ramp_p1_tag", "post_ramp_tag", "section_a_tag", "area_paths")
    )

    templates_root = templates_root or (Path(__file__).resolve().parents[2] / "templates")

    if has_new_sections:
        config = _parse_new_format(fh, program, program_id, global_cooldown, comment_window, templates_root, raw_edition=raw)
    elif has_legacy_keys:
        # Emit one deprecation warning per run
        warnings.warn(
            f"[nudge:{program_id}] Edition uses legacy full_hygiene flat keys. "
            "Migrate to the new sections: format. Legacy support will be removed in the next release.",
            DeprecationWarning,
            stacklevel=3,
        )
        config = _parse_legacy_shim(fh, program, program_id, global_cooldown, comment_window, templates_root)
    else:
        raise ConfigError(
            f"Invalid nudge config for {program_id}: full_hygiene must contain either 'sections' "
            "or legacy keys (ramp_p1_tag / post_ramp_tag)."
        )

    all_errors = validate_nudge_config(config, program)
    if all_errors:
        raise ConfigError("Invalid nudge config:\n- " + "\n- ".join(all_errors))

    return config


def validate_nudge_config(config: NudgeConfig, program: Any) -> list[str]:
    errors: list[str] = []

    # Delivery
    if not config.delivery.recipient.strip():
        errors.append("full_hygiene.recipient must be non-empty")

    if config.delivery.cadence_days < 1:
        errors.append("full_hygiene.cadence_days must be a positive integer")

    if config.delivery.delivery_mode == "per_workstream":
        errors.append("delivery_mode=per_workstream is not yet implemented (Phase 4)")

    # Evaluation
    if config.evaluation.cooldown_days < 1:
        errors.append("hygiene.cooldown_days must be a positive integer")

    if config.evaluation.comment_window_days < 1:
        errors.append("hygiene.comment_window_days must be a positive integer")

    if config.evaluation.comment_fetch_limit < 1:
        errors.append("full_hygiene.comment_fetch_limit must be a positive integer")

    # Sections
    if not config.sections:
        errors.append("At least one section is required")

    seen_ids: set[str] = set()
    seen_letters: set[str] = set()

    for sec in config.sections:
        if sec.id in seen_ids:
            errors.append(f"Duplicate section id: {sec.id!r}")
        seen_ids.add(sec.id)

        if not _SECTION_ID_RE.match(sec.id):
            errors.append(f"Section id {sec.id!r} must match ^[a-z][a-z0-9_-]{{0,63}}$")

        if sec.letter in seen_letters:
            errors.append(f"Duplicate section letter: {sec.letter!r}")
        seen_letters.add(sec.letter)

        if sec.stale_business_days < 1:
            errors.append(f"Section {sec.id!r}: stale_business_days must be positive")

        if sec.cooldown_days is not None and sec.cooldown_days < 1:
            errors.append(f"Section {sec.id!r}: cooldown_days must be positive when set")

        crit = sec.criteria
        if crit.source == "tag":
            if not crit.tags:
                errors.append(f"Section {sec.id!r}: source=tag requires at least one tag")
            if crit.required_tags and not crit.legacy_scope_override:
                errors.append(f"Section {sec.id!r}: source=tag must not have required_tags in new format")
            # area_path_filter is allowed for tag sections: it narrows the WIQL scope
        elif crit.source == "area_path":
            if not crit.area_path_filter:
                errors.append(f"Section {sec.id!r}: source=area_path requires area_path_filter")
            if crit.tags:
                errors.append(f"Section {sec.id!r}: source=area_path must not have tags")
            if crit.required_tags:
                errors.append(f"Section {sec.id!r}: source=area_path must not have required_tags")
            # Validate subset of program area paths
            ado = getattr(program, "ado", None)
            if ado is not None:
                prog_areas = frozenset(str(ap).strip() for ap in (getattr(ado, "area_paths", None) or ()))
                for ap in crit.area_path_filter:
                    if prog_areas and ap not in prog_areas:
                        errors.append(
                            f"Section {sec.id!r}: area_path_filter value {ap!r} not in program.ado.area_paths"
                        )
        elif crit.source == "registry":
            if crit.tags:
                errors.append(f"Section {sec.id!r}: source=registry must not have tags")
            # area_path_filter is allowed for registry sections: it narrows candidates to a subtree
        else:
            errors.append(f"Section {sec.id!r}: unknown criteria source {crit.source!r}")

    # ADO config required for tag/area sections
    if any(sec.criteria.source in ("tag", "area_path") for sec in config.sections):
        ado = getattr(program, "ado", None)
        if ado is None:
            errors.append("program.ado is required for tag or area_path sections")
        else:
            if not getattr(ado, "organization", "").strip():
                errors.append("program.ado.organization must be non-empty for tag/area_path sections")
            if not getattr(ado, "project", "").strip():
                errors.append("program.ado.project must be non-empty for tag/area_path sections")

    return errors


def parse_stale_overrides(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError(f"Invalid --stale-override format (expected id=days): {raw!r}")
        section_id, _, days_str = raw.partition("=")
        section_id = section_id.strip()
        if not section_id:
            raise ValueError(f"Invalid --stale-override: section id is empty in {raw!r}")
        try:
            days = int(days_str.strip())
        except ValueError:
            raise ValueError(f"Invalid --stale-override: days must be an integer in {raw!r}")
        if days < 1:
            raise ValueError(f"Invalid --stale-override: days must be positive in {raw!r}")
        if section_id in result:
            raise ValueError(f"Duplicate --stale-override for section {section_id!r}")
        result[section_id] = days
    return result


# ---------------------------------------------------------------------------
# New-format parser
# ---------------------------------------------------------------------------


def _parse_new_format(
    fh: dict[str, Any],
    program: Any,
    program_id: str,
    global_cooldown: int,
    comment_window: int,
    templates_root: Path,
    raw_edition: dict[str, Any] | None = None,
) -> NudgeConfig:
    recipient = str(fh.get("recipient") or "").strip()
    delivery_mode = str(fh.get("delivery_mode") or "broadcast").strip()
    cadence_days = _pos_int(fh.get("cadence_days"), _DEFAULT_CADENCE_DAYS)
    include_owners = bool(fh.get("include_workstream_owners", True))
    include_assignees = bool(fh.get("include_item_assignees", True))
    send_day = str((raw_edition or {}).get("send_day") or fh.get("send_day") or "").strip().lower()
    owner_roles_raw = fh.get("owner_roles") or []
    owner_roles: tuple[str, ...] = tuple(
        str(r).strip() for r in (owner_roles_raw if isinstance(owner_roles_raw, list) else [])
        if r
    ) or ("tpm_lead", "eng_lead")

    comment_fetch_limit = _pos_int(fh.get("comment_fetch_limit"), NUDGE_COMMENT_FETCH_LIMIT_DEFAULT)

    exempt_raw = fh.get("nudge_exempt_item_ids") or []
    exempt_ids: frozenset[int] = frozenset(
        int(x) for x in (exempt_raw if isinstance(exempt_raw, list) else [])
        if isinstance(x, int) and x > 0
    )

    status_kw = _str_list(fh.get("status_keywords"))
    risk_ot = _str_list(fh.get("risk_on_track_values"))

    brand = str(fh.get("brand_label") or _DEFAULT_BRAND_LABEL).strip()
    subject = str(fh.get("email_subject_label") or _DEFAULT_SUBJECT_LABEL).strip()
    template = str(fh.get("template") or _DEFAULT_TEMPLATE).strip()
    preheader = str(fh.get("preheader") or f"{brand} — ADO hygiene readiness sweep").strip()
    compress = bool(fh.get("compress_titles_with_ai", False))

    # Context-subject prefix (§8, NudgePresentationConfig)
    ctx_prefix = bool(fh.get("context_subject_prefix", False))
    ctx_tpl = str(fh.get("context_subject_prefix_template") or "[Action DUE {due} EOD]").strip()
    ctx_overdue = str(fh.get("context_subject_overdue_template") or "").strip()
    ctx_lookahead = _pos_int(fh.get("context_subject_lookahead_days"), 14)

    # Validate template path
    _validate_template_path(template, templates_root)

    raw_sections = fh.get("sections") or []
    if not isinstance(raw_sections, list):
        raise ConfigError(f"full_hygiene.sections must be a list for {program_id}")

    sections = _parse_sections(raw_sections, global_cooldown)

    # Phase 2: nudge_waivers
    waivers = _parse_waivers(fh.get("nudge_waivers") or [], program_id)

    # Phase 1: action_due_policy
    action_due_policy = _parse_action_due_policy(fh.get("action_due_policy"), program_id)

    # Phase 2: audience_policy
    audience_policy = _parse_audience_policy(fh.get("audience_policy"), program_id)

    delivery = NudgeDeliveryConfig(
        recipient=recipient,
        delivery_mode=delivery_mode,  # type: ignore[arg-type]
        cadence_days=cadence_days,
        include_workstream_owners=include_owners,
        include_item_assignees=include_assignees,
        send_day=send_day,
        owner_roles=owner_roles,
        to_leadership_rollup=bool(fh.get("to_leadership_rollup", False)),
        additional_cc=tuple(_str_list(fh.get("additional_cc"))),
    )
    evaluation = NudgeEvaluationConfig(
        comment_window_days=comment_window,
        status_keywords=tuple(status_kw),
        risk_on_track_values=tuple(risk_ot),
        cooldown_days=global_cooldown,
        nudge_exempt_item_ids=exempt_ids,
        comment_fetch_limit=comment_fetch_limit,
        nudge_waivers=tuple(waivers),
        action_due_policy=action_due_policy,
    )
    presentation = NudgePresentationConfig(
        brand_label=brand,
        email_subject_label=subject,
        template=template,
        preheader=preheader,
        compress_titles_with_ai=compress,
        context_subject_prefix=ctx_prefix,
        context_subject_prefix_template=ctx_tpl,
        context_subject_overdue_template=ctx_overdue,
        context_subject_lookahead_days=ctx_lookahead,
        audience_policy=audience_policy,
    )
    return NudgeConfig(sections=tuple(sections), delivery=delivery, evaluation=evaluation, presentation=presentation)


def _parse_sections(raw_sections: list[Any], global_cooldown: int) -> list[NudgeSectionSpec]:
    sections: list[NudgeSectionSpec] = []
    for idx, raw in enumerate(raw_sections):
        if not isinstance(raw, dict):
            raise ConfigError(f"Section at index {idx} must be a mapping")
        section_id = str(raw.get("id") or "").strip()
        if not section_id:
            raise ConfigError(f"Section at index {idx} is missing required 'id' field")
        title = str(raw.get("title") or "").strip()
        if not title:
            raise ConfigError(f"Section {section_id!r}: missing required 'title' field")
        stale = _pos_int(raw.get("stale_business_days"), 0)
        if stale < 1:
            raise ConfigError(f"Section {section_id!r}: stale_business_days must be a positive integer")
        letter_raw = raw.get("letter")
        letter = str(letter_raw).strip() if letter_raw is not None else _resolve_letter(idx)
        if not letter:
            letter = _resolve_letter(idx)
        cd_raw = raw.get("cooldown_days")
        cooldown = int(cd_raw) if cd_raw is not None else None
        desc = str(raw.get("description") or "").strip()
        deadline_raw = raw.get("deadline")
        deadline: date | None = None
        if deadline_raw is not None:
            try:
                deadline = date.fromisoformat(str(deadline_raw).strip())
            except ValueError:
                raise ConfigError(f"Section {section_id!r}: invalid deadline {deadline_raw!r}")
        stale_summary = _pos_int(raw.get("stale_summary_threshold_days"), 7)

        raw_criteria = raw.get("criteria")
        if not isinstance(raw_criteria, dict):
            raise ConfigError(f"Section {section_id!r}: missing required 'criteria' mapping")
        criteria = _parse_criteria(raw_criteria, section_id)

        # Phase 1/2 additive section fields
        deadline_milestone_id = str(raw.get("deadline_milestone_id") or "").strip() or None
        requires_milestone = bool(raw.get("requires_milestone", False))
        required = bool(raw.get("required", False))
        retire_when_milestone_done = str(raw.get("retire_when_milestone_done") or "").strip() or None
        nudge_participating_lanes = tuple(_str_list(raw.get("nudge_participating_lanes")))
        include_in_leadership_rollup = bool(raw.get("include_in_leadership_rollup", True))

        # Workstream hints: map ADO item IDs to workstream IDs for tag/area_path sections
        hints_raw = raw.get("workstream_hints") or []
        workstream_hints: list[Any] = []
        if isinstance(hints_raw, list):
            for h in hints_raw:
                if not isinstance(h, dict):
                    continue
                ws_id = str(h.get("workstream_id") or "").strip()
                ids_raw = h.get("ado_item_ids") or []
                ids = frozenset(int(x) for x in ids_raw if isinstance(x, int) and x > 0)
                if ws_id and ids:
                    workstream_hints.append(WorkstreamHint(workstream_id=ws_id, ado_item_ids=ids))

        sections.append(NudgeSectionSpec(
            id=section_id,
            title=title,
            criteria=criteria,
            stale_business_days=stale,
            letter=letter,
            cooldown_days=cooldown,
            description=desc,
            deadline=deadline,
            stale_summary_threshold_days=stale_summary,
            deadline_milestone_id=deadline_milestone_id,
            requires_milestone=requires_milestone,
            required=required,
            retire_when_milestone_done=retire_when_milestone_done,
            nudge_participating_lanes=nudge_participating_lanes,
            workstream_hints=tuple(workstream_hints),
            include_in_leadership_rollup=include_in_leadership_rollup,
        ))
    return sections


def _parse_criteria(raw: dict[str, Any], section_id: str) -> NudgeSectionCriteria:
    source = str(raw.get("source") or "").strip()
    if source not in ("registry", "tag", "area_path"):
        raise ConfigError(
            f"Section {section_id!r}: criteria.source must be one of registry/tag/area_path, got {source!r}"
        )
    tags = tuple(_str_list(raw.get("tags")))
    area_path_filter = tuple(_str_list(raw.get("area_path_filter")))
    required_tags = tuple(_str_list(raw.get("required_tags")))
    legacy = bool(raw.get("legacy_scope_override", False))
    if legacy:
        raise ConfigError(
            f"Section {section_id!r}: legacy_scope_override may not be set in authored YAML; "
            "it is reserved for the internal compatibility shim."
        )
    return NudgeSectionCriteria(
        source=source,  # type: ignore[arg-type]
        tags=tags,
        area_path_filter=area_path_filter,
        required_tags=required_tags,
        legacy_scope_override=False,
    )


# ---------------------------------------------------------------------------
# Backward-compat shim for legacy flat full_hygiene keys
# ---------------------------------------------------------------------------


def _parse_legacy_shim(
    fh: dict[str, Any],
    program: Any,
    program_id: str,
    global_cooldown: int,
    comment_window: int,
    templates_root: Path,
) -> NudgeConfig:
    """Convert old flat full_hygiene keys into NudgeConfig with generated sections."""
    recipient = str(fh.get("recipient") or "").strip()
    delivery_mode = "broadcast"
    cadence_days = _pos_int(fh.get("cadence_days"), _DEFAULT_CADENCE_DAYS)
    include_owners = True
    include_assignees = True

    comment_fetch_limit = _pos_int(fh.get("comment_fetch_limit"), NUDGE_COMMENT_FETCH_LIMIT_DEFAULT)

    exempt_raw = fh.get("nudge_exempt_item_ids") or []
    exempt_ids: frozenset[int] = frozenset(
        int(x) for x in (exempt_raw if isinstance(exempt_raw, list) else [])
        if isinstance(x, int) and x > 0
    )

    status_kw = _str_list(fh.get("status_keywords"))
    risk_ot = _str_list(fh.get("risk_on_track_values"))

    brand = str(fh.get("brand_label") or _DEFAULT_BRAND_LABEL).strip()
    subject = str(fh.get("email_subject_label") or _DEFAULT_SUBJECT_LABEL).strip()
    template = str(fh.get("template") or _DEFAULT_TEMPLATE).strip()
    preheader = str(fh.get("preheader") or f"{brand} — ADO hygiene readiness sweep").strip()
    compress = bool(fh.get("compress_titles_with_ai", False))

    # Validate template
    _validate_template_path(template, templates_root)

    # Legacy stale thresholds
    stale_cfg_raw = fh.get("stale_business_days")
    stale_cfg: dict[str, Any] = stale_cfg_raw if isinstance(stale_cfg_raw, dict) else {}
    stale_a = _pos_int(stale_cfg.get("section_a"), _DEFAULT_STALE_A)
    stale_b = _pos_int(stale_cfg.get("section_b"), _DEFAULT_STALE_B)
    stale_c = _pos_int(stale_cfg.get("section_c"), _DEFAULT_STALE_C)

    # ramp_deadline → generic deadline
    deadline_raw = fh.get("ramp_deadline")
    deadline: date | None = None
    if deadline_raw:
        try:
            deadline = date.fromisoformat(str(deadline_raw).strip())
        except ValueError:
            deadline = None

    # Legacy area paths
    area_paths_raw = fh.get("area_paths") or []
    legacy_area_paths: list[str] = (
        [str(ap).strip() for ap in area_paths_raw if str(ap).strip()]
        if isinstance(area_paths_raw, (list, tuple))
        else []
    )
    has_legacy_areas = bool(legacy_area_paths)

    # section_a_tag and ramp_p1_tag
    _section_a_tag_raw = fh.get("section_a_tag")
    _section_a_tags: list[str] = []
    if isinstance(_section_a_tag_raw, list):
        _section_a_tags = [str(t).strip() for t in _section_a_tag_raw if str(t).strip()]
    elif isinstance(_section_a_tag_raw, str) and _section_a_tag_raw.strip():
        _section_a_tags = [_section_a_tag_raw.strip()]

    _ramp_p1_raw = fh.get("ramp_p1_tag")
    ramp_p1_tags: list[str] = []
    if isinstance(_ramp_p1_raw, list):
        ramp_p1_tags = [str(t).strip() for t in _ramp_p1_raw if str(t).strip()]
    elif isinstance(_ramp_p1_raw, str) and str(_ramp_p1_raw).strip():
        ramp_p1_tags = [str(_ramp_p1_raw).strip()]

    _post_ramp_raw = fh.get("post_ramp_tag")
    post_ramp_tag = str(_post_ramp_raw).strip() if _post_ramp_raw and str(_post_ramp_raw).strip() else ""

    # Determine Section A required_tags
    if _section_a_tags:
        section_a_required = tuple(_section_a_tags)
    elif ramp_p1_tags:
        section_a_required = tuple(ramp_p1_tags)
    else:
        section_a_required = ()

    def _make_criteria_registry(required_tags: tuple[str, ...]) -> NudgeSectionCriteria:
        return NudgeSectionCriteria(
            source="registry",
            required_tags=required_tags,
            legacy_scope_override=has_legacy_areas,
            area_path_filter=tuple(legacy_area_paths) if has_legacy_areas else (),
        )

    def _make_criteria_tag(tags: list[str]) -> NudgeSectionCriteria:
        return NudgeSectionCriteria(
            source="tag",
            tags=tuple(tags),
            legacy_scope_override=has_legacy_areas,
            area_path_filter=tuple(legacy_area_paths) if has_legacy_areas else (),
        )

    sections: list[NudgeSectionSpec] = []

    # Always create Section A from registry
    sections.append(NudgeSectionSpec(
        id="priority",
        title="Active Workstream Priority",
        criteria=_make_criteria_registry(section_a_required),
        stale_business_days=stale_a,
        letter="A",
        description="",
        deadline=deadline,
        stale_summary_threshold_days=7,
    ))

    # Section B only when ramp_p1_tag is non-empty
    if ramp_p1_tags:
        sections.append(NudgeSectionSpec(
            id="remaining_ramp",
            title=f"Remaining {'/'.join(ramp_p1_tags)}",
            criteria=_make_criteria_tag(ramp_p1_tags),
            stale_business_days=stale_b,
            letter="B",
            description="",
            deadline=deadline,
            stale_summary_threshold_days=7,
        ))

    # Section C only when post_ramp_tag is non-empty
    if post_ramp_tag:
        sections.append(NudgeSectionSpec(
            id="post_ramp",
            title=f"Post-Milestone Backlog ({post_ramp_tag})",
            criteria=_make_criteria_tag([post_ramp_tag]),
            stale_business_days=stale_c,
            letter="C",
            description="",
            deadline=deadline,
            stale_summary_threshold_days=7,
        ))

    if has_legacy_areas:
        warnings.warn(
            f"[nudge:{program_id}] Legacy full_hygiene.area_paths detected; "
            "using legacy scope for candidate queries. Migrate to program.ado.area_paths.",
            DeprecationWarning,
            stacklevel=4,
        )

    delivery = NudgeDeliveryConfig(
        recipient=recipient,
        delivery_mode=delivery_mode,  # type: ignore[arg-type]
        cadence_days=cadence_days,
        include_workstream_owners=include_owners,
        include_item_assignees=include_assignees,
    )
    evaluation = NudgeEvaluationConfig(
        comment_window_days=_pos_int(fh.get("comment_window_days"), comment_window),
        status_keywords=tuple(status_kw),
        risk_on_track_values=tuple(risk_ot),
        cooldown_days=global_cooldown,
        nudge_exempt_item_ids=exempt_ids,
        comment_fetch_limit=comment_fetch_limit,
    )
    presentation = NudgePresentationConfig(
        brand_label=brand,
        email_subject_label=subject,
        template=template,
        preheader=preheader,
        compress_titles_with_ai=compress,
    )
    return NudgeConfig(
        sections=tuple(sections),
        delivery=delivery,
        evaluation=evaluation,
        presentation=presentation,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_letter(index: int) -> str:
    """Spreadsheet-column algorithm: A, B, ..., Z, AA, AB, ..."""
    n = index + 1
    result = []
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result.append(chr(65 + remainder))
    return "".join(reversed(result))


def _pos_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        v = int(value)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _parse_waivers(raw_waivers: list[Any], program_id: str) -> list[NudgeWaiver]:
    """Parse nudge_waivers list from config (§6.8, Phase 2)."""
    waivers: list[NudgeWaiver] = []
    for i, w in enumerate(raw_waivers):
        if not isinstance(w, dict):
            raise ConfigError(f"nudge_waivers[{i}] must be a mapping for {program_id}")
        try:
            wid = int(w.get("work_item_id") or 0)
        except (TypeError, ValueError):
            raise ConfigError(f"nudge_waivers[{i}].work_item_id must be an integer for {program_id}")
        if wid <= 0:
            raise ConfigError(f"nudge_waivers[{i}].work_item_id must be a positive integer for {program_id}")
        alias = str(w.get("owner_alias") or "").strip()
        reason = str(w.get("reason") or "").strip()
        if not reason:
            raise ConfigError(f"nudge_waivers[{i}] (item {wid}): reason must be non-empty for {program_id}")
        if not w.get("expires"):
            raise ConfigError(f"nudge_waivers[{i}] (item {wid}): expires must be set for {program_id}")
        try:
            expires = date.fromisoformat(str(w["expires"]).strip())
        except ValueError:
            raise ConfigError(f"nudge_waivers[{i}] (item {wid}): invalid expires date {w['expires']!r}")
        created_raw = w.get("created")
        try:
            created = date.fromisoformat(str(created_raw).strip()) if created_raw else date.today()
        except ValueError:
            created = date.today()
        waivers.append(NudgeWaiver(
            work_item_id=wid,
            owner_alias=alias,
            reason=reason,
            created=created,
            expires=expires,
        ))
    return waivers


def _parse_action_due_policy(raw: Any, program_id: str) -> "ActionDuePolicy | None":
    """Parse action_due_policy block from config (§8.3, Phase 1)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"full_hygiene.action_due_policy must be a mapping for {program_id}")
    mode = str(raw.get("mode") or "").strip()
    if mode == "explicit":
        date_raw = raw.get("date")
        if not date_raw:
            raise ConfigError(f"action_due_policy mode=explicit requires 'date' for {program_id}")
        try:
            due_date = date.fromisoformat(str(date_raw).strip())
        except ValueError:
            raise ConfigError(f"action_due_policy.date invalid: {date_raw!r} for {program_id}")
        return ExplicitActionDue(date=due_date)
    elif mode == "send_date_offset":
        bdays = _pos_int(raw.get("business_days"), 3)
        return SendDateOffsetActionDue(business_days=bdays)
    elif mode == "milestone_relative":
        mid = str(raw.get("milestone_id") or "").strip()
        # business_days_before=0 means "due on the milestone day itself"; _pos_int
        # rejects 0 (treats as default), so parse this field without the positive guard.
        bdays_raw = raw.get("business_days_before")
        if bdays_raw is not None:
            try:
                bdays = max(0, int(bdays_raw))
            except (TypeError, ValueError):
                bdays = 3
        else:
            bdays = 3
        return MilestoneRelativeActionDue(milestone_id=mid, business_days_before=bdays)
    else:
        raise ConfigError(
            f"action_due_policy.mode must be explicit|send_date_offset|milestone_relative "
            f"for {program_id}, got {mode!r}"
        )


def _parse_audience_policy(raw: Any, program_id: str) -> "NudgeAudiencePolicy | None":
    """Parse audience_policy block from config (§6.9, Phase 2)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"full_hygiene.audience_policy must be a mapping for {program_id}")
    domains = tuple(_str_list(raw.get("allowed_domains")) or ["microsoft.com"])
    max_r = _pos_int(raw.get("max_recipients"), 200)
    opt_out = frozenset(_str_list(raw.get("opt_out")))
    fallback = str(raw.get("opt_out_fallback") or "escalate").strip()
    if fallback not in ("escalate", "gap"):
        raise ConfigError(f"audience_policy.opt_out_fallback must be escalate|gap for {program_id}")
    new_approval = bool(raw.get("new_recipient_approval", True))
    unresolved = str(raw.get("unresolved_owner") or "drop").strip()
    if unresolved not in ("drop", "fail"):
        raise ConfigError(f"audience_policy.unresolved_owner must be drop|fail for {program_id}")
    delivery = str(raw.get("delivery_mode") or "to").strip()
    if delivery not in ("to", "bcc"):
        raise ConfigError(f"audience_policy.delivery_mode must be to|bcc for {program_id}")
    return NudgeAudiencePolicy(
        allowed_domains=domains,
        max_recipients=max_r,
        opt_out=opt_out,
        opt_out_fallback=fallback,  # type: ignore[arg-type]
        new_recipient_approval=new_approval,
        unresolved_owner=unresolved,  # type: ignore[arg-type]
        delivery_mode=delivery,  # type: ignore[arg-type]
    )


def _validate_template_path(template: str, templates_root: Path) -> None:
    if not template:
        raise ConfigError("full_hygiene.template must not be empty")
    if ".." in template.replace("\\", "/").split("/"):
        raise ConfigError(f"full_hygiene.template contains path traversal: {template!r}")
    resolved = (templates_root / template).resolve()
    try:
        resolved.relative_to(templates_root.resolve())
    except ValueError:
        raise ConfigError(f"full_hygiene.template resolves outside templates root: {template!r}")
    if not resolved.exists():
        raise ConfigError(f"Template file not found: templates/{template}")

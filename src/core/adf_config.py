"""Typed loader for the ``arch_data_fix`` configuration block.

Implements ADF-W0.5 of ``specs/arch-data-fix.md``. The schema is defined by
Section 10.8 (configuration contract), Appendix A.5 (governance policy types),
and the retention floors in Section 9.7. This module lives in Zone A
(``src/core``); it must not import ``src.ai``, ``src.m365``, or ``src.commands``
(INV-ADF-17).

Design rules:

- All dataclasses are ``@dataclass(frozen=True, slots=True)``.
- An absent ``arch_data_fix`` block yields ``mode=off`` defaults (Section 15.1).
- Invalid enums, negative budgets, or sub-floor retention raise ``ConfigError``.
- Program retention overrides may only *lengthen* the Section 9.7 floors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from src.core.exceptions import ConfigError
from src.core.yaml_utils import load_yaml_mapping

#: Execution mode of the closure feature (Section 15.1 / Appendix A.4).
#: Drives observe-vs-enforce gate activation across all quality gates.
SCHEMA_VERSION = "1"


# --------------------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------------------


class ExecutionMode(str, Enum):
    """off -> observe -> enforce roll-forward per program (Section 15.1)."""

    OFF = "off"
    OBSERVE = "observe"
    ENFORCE = "enforce"


class AlertDelivery(str, Enum):
    """Where materialized alert artifacts land (Section 10.8 ``alerts.delivery``)."""

    COCKPIT = "cockpit"
    DRAFT_EMAIL = "draft_email"
    DRAFT_TEAMS = "draft_teams"


class SolicitationMode(str, Enum):
    """Autonomy ceiling for the solicitation flow (Section 8.13.3)."""

    DISABLED = "disabled"
    DRAFT = "draft"
    APPROVED_BATCH = "approved_batch"
    STANDING_POLICY = "standing_policy"


#: Autonomy ladder levels serialize lowercase ``l0..l4`` (Appendix A.5).
AUTONOMY_LEVELS: frozenset[str] = frozenset({f"l{i}" for i in range(5)})


# --------------------------------------------------------------------------------------
# Retention floors (Section 9.7) -- global, may only be lengthened.
# --------------------------------------------------------------------------------------

#: Minimum raw retention in days. Keys match the Section 9.7 retention table and
#: ``retention`` config block. Programs may override to a *larger* value only.
RETENTION_FLOOR_DAYS: dict[str, int] = {
    "tier_decision_days": 45,
    "ai_telemetry_days": 90,
    "run_telemetry_days": 90,
    "channel_telemetry_days": 90,
    "context_manifest_days": 90,
    "cockpit_history_builds": 30,
    "alerts_resolved_days": 90,
    "workflow_value_events_days": 365,  # "13 months minimum" -> 365 floor
}


# --------------------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CockpitConfig:
    enabled: bool = True
    history_keep_builds: int = 30
    history_keep_weeks: int = 56
    fleet_enabled: bool = False


@dataclass(frozen=True, slots=True)
class ValueConfig:
    workflow: str = "weekly_issue"
    minimum_baseline_samples: int | None = None
    historical_baseline_allowed: bool = True


@dataclass(frozen=True, slots=True)
class ChannelBudget:
    """Per-channel execution budget (Section 10.8 ``channels.<channel>``).

    Mirrors the binding ``ChannelExecutionPolicy`` (Section 8.3.1) at the
    configuration layer; the runtime policy is materialized by Slice 1.
    """

    required: bool = False
    inline_allowed: bool = True
    per_attempt_timeout_seconds: int = 30
    total_budget_seconds: int = 60
    max_pages: int | None = None
    max_records: int | None = None
    stale_fallback_allowed: bool = False
    prefetch_required: bool = False


@dataclass(frozen=True, slots=True)
class AlertsConfig:
    delivery: AlertDelivery = AlertDelivery.COCKPIT
    cooldown_minutes: int = 1440


@dataclass(frozen=True, slots=True)
class ActuationConfig:
    enabled: bool = False
    outbox_worker_enabled: bool = False


@dataclass(frozen=True, slots=True)
class TopThreeProposalsConfig:
    enabled: bool = True
    require_human_acceptance: bool = True


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    tier_decision_days: int = RETENTION_FLOOR_DAYS["tier_decision_days"]
    ai_telemetry_days: int = RETENTION_FLOOR_DAYS["ai_telemetry_days"]
    run_telemetry_days: int = RETENTION_FLOOR_DAYS["run_telemetry_days"]
    #: Section 9.7's "Workflow/value events: 13 months minimum" -- governs
    #: proposal_audit.jsonl (ADR-0017) and adoption_telemetry.jsonl (ADF-W5.14).
    workflow_value_events_days: int = RETENTION_FLOOR_DAYS["workflow_value_events_days"]


@dataclass(frozen=True, slots=True)
class SamplingRule:
    """Review sampling policy per proposal class (Appendix A.5 ``SamplingRule``)."""

    sample_rate: float
    min_weekly_samples: int
    include_low_confidence: bool = True


@dataclass(frozen=True, slots=True)
class SolicitationPolicy:
    """Appendix A.5 ``SolicitationPolicy``."""

    mode: SolicitationMode = SolicitationMode.DISABLED
    audience_tiers: Mapping[str, str] = field(default_factory=dict)
    per_recipient_cooldown_days: int = 14
    senior_requires_named_approval: bool = True


@dataclass(frozen=True, slots=True)
class ProgramGovernancePolicy:
    """Appendix A.5 ``ProgramGovernancePolicy``.

    May tighten but never weaken platform safety floors (Section 3.6.2b).
    """

    schema_version: str = SCHEMA_VERSION
    policy_version: str = "1"
    autonomy_ceiling: Mapping[str, str] = field(default_factory=lambda: {"solicitation": "l2", "ado_create_task": "l2"})
    review_sampling: Mapping[str, SamplingRule] = field(default_factory=dict)
    source_slas_hours: Mapping[str, int] = field(default_factory=dict)
    allowed_ai_tiers: tuple[str, ...] = ()
    program_cost_ceiling_usd: float | None = None
    solicitation: SolicitationPolicy = field(default_factory=SolicitationPolicy)
    required_roles: Mapping[str, str] = field(default_factory=dict)
    retention_overrides_days: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArchDataFixConfig:
    """The full typed Section 10.8 block for one program."""

    schema_version: str = SCHEMA_VERSION
    program_id: str = ""
    mode: ExecutionMode = ExecutionMode.OFF
    cockpit: CockpitConfig = field(default_factory=CockpitConfig)
    value: ValueConfig = field(default_factory=ValueConfig)
    channels: Mapping[str, ChannelBudget] = field(default_factory=dict)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    actuation: ActuationConfig = field(default_factory=ActuationConfig)
    solicitation: SolicitationMode = SolicitationMode.DISABLED
    top_3_proposals: TopThreeProposalsConfig = field(default_factory=TopThreeProposalsConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    governance: ProgramGovernancePolicy = field(default_factory=ProgramGovernancePolicy)

    @property
    def is_off(self) -> bool:
        return self.mode is ExecutionMode.OFF

    @property
    def is_enforce(self) -> bool:
        return self.mode is ExecutionMode.ENFORCE


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _require_mapping(value: Any, *, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"arch_data_fix.{where} must be a mapping, got {type(value).__name__}")
    return dict(value)


def _coerce_bool(value: Any, *, where: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"arch_data_fix.{where} must be a boolean, got {value!r}")


def _coerce_int(value: Any, *, where: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"arch_data_fix.{where} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"arch_data_fix.{where} must be >= {minimum}, got {value}")
    return value


def _coerce_optional_int(value: Any, *, where: str, minimum: int | None = None) -> int | None:
    if value is None:
        return None
    return _coerce_int(value, where=where, minimum=minimum)


def _coerce_str(value: Any, *, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"arch_data_fix.{where} must be a string, got {value!r}")
    return value


def _coerce_enum(value: Any, enum_cls: type[Enum], *, where: str) -> Any:
    if value is None:
        return enum_cls  # caller will use default
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError as error:
            allowed = ", ".join(member.value for member in enum_cls)
            raise ConfigError(f"arch_data_fix.{where}={value!r} is not one of: {allowed}") from error
    raise ConfigError(f"arch_data_fix.{where} must be a string, got {type(value).__name__}")


def _coerce_autonomy_level(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or value not in AUTONOMY_LEVELS:
        raise ConfigError(
            f"arch_data_fix.{where}={value!r} is not a valid autonomy level (one of {sorted(AUTONOMY_LEVELS)})"
        )
    return value


def _parse_channel_budget(channel: str, raw: Any) -> ChannelBudget:
    raw = _require_mapping(raw, where=f"channels.{channel}")
    timeout = _coerce_int(raw.get("per_attempt_timeout_seconds", 30), where=f"channels.{channel}.per_attempt_timeout_seconds", minimum=0)
    total = _coerce_int(raw.get("total_budget_seconds", 60), where=f"channels.{channel}.total_budget_seconds", minimum=0)
    if total < timeout:
        raise ConfigError(
            f"arch_data_fix.channels.{channel}.total_budget_seconds ({total}) "
            f"must be >= per_attempt_timeout_seconds ({timeout})"
        )
    return ChannelBudget(
        required=_coerce_bool(raw.get("required", False), where=f"channels.{channel}.required"),
        inline_allowed=_coerce_bool(raw.get("inline_allowed", True), where=f"channels.{channel}.inline_allowed"),
        per_attempt_timeout_seconds=timeout,
        total_budget_seconds=total,
        max_pages=_coerce_optional_int(raw.get("max_pages"), where=f"channels.{channel}.max_pages", minimum=0),
        max_records=_coerce_optional_int(raw.get("max_records"), where=f"channels.{channel}.max_records", minimum=0),
        stale_fallback_allowed=_coerce_bool(
            raw.get("stale_fallback_allowed", False), where=f"channels.{channel}.stale_fallback_allowed"
        ),
        prefetch_required=_coerce_bool(raw.get("prefetch_required", False), where=f"channels.{channel}.prefetch_required"),
    )


def _parse_sampling_rule(proposal_class: str, raw: Any) -> SamplingRule:
    raw = _require_mapping(raw, where=f"governance.review_sampling.{proposal_class}")
    rate = raw.get("sample_rate")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not (0.0 <= float(rate) <= 1.0):
        raise ConfigError(
            f"arch_data_fix.governance.review_sampling.{proposal_class}.sample_rate must be in [0,1], got {rate!r}"
        )
    return SamplingRule(
        sample_rate=float(rate),
        min_weekly_samples=_coerce_int(
            raw.get("min_weekly_samples", 0), where=f"governance.review_sampling.{proposal_class}.min_weekly_samples", minimum=0
        ),
        include_low_confidence=_coerce_bool(
            raw.get("include_low_confidence", True), where=f"governance.review_sampling.{proposal_class}.include_low_confidence"
        ),
    )


def _parse_solicitation_policy(raw: Any) -> SolicitationPolicy:
    raw = _require_mapping(raw, where="governance.solicitation")
    mode_val = raw.get("mode", SolicitationMode.DISABLED.value)
    mode = _coerce_enum(mode_val, SolicitationMode, where="governance.solicitation.mode")
    audience_raw = _require_mapping(raw.get("audience_tiers"), where="governance.solicitation.audience_tiers")
    audience_tiers: dict[str, str] = {}
    for tier, level in audience_raw.items():
        audience_tiers[str(tier)] = _coerce_autonomy_level(level, where=f"governance.solicitation.audience_tiers.{tier}")
    cooldown = _coerce_int(
        raw.get("per_recipient_cooldown_days", 14),
        where="governance.solicitation.per_recipient_cooldown_days",
        minimum=0,
    )
    return SolicitationPolicy(
        mode=mode,
        audience_tiers=audience_tiers,
        per_recipient_cooldown_days=cooldown,
        senior_requires_named_approval=_coerce_bool(
            raw.get("senior_requires_named_approval", True), where="governance.solicitation.senior_requires_named_approval"
        ),
    )


def _parse_retention(raw: Any, overrides: Mapping[str, int]) -> RetentionConfig:
    raw = _require_mapping(raw, where="retention")
    values: dict[str, int] = {}
    for key, floor in RETENTION_FLOOR_DAYS.items():
        if key not in ("tier_decision_days", "ai_telemetry_days", "run_telemetry_days", "workflow_value_events_days"):
            continue
        configured = raw.get(key, floor)
        override = overrides.get(key, configured)
        final = _coerce_int(override, where=f"retention.{key}", minimum=floor)
        values[key] = final
    return RetentionConfig(**values)


#: Default autonomy ceilings applied when the governance block is absent
#: (Section 10.8 ``governance.autonomy_ceiling`` example defaults).
_DEFAULT_AUTONOMY_CEILING: dict[str, str] = {"solicitation": "l2", "ado_create_task": "l2"}


def _parse_governance(raw: Any) -> ProgramGovernancePolicy:
    raw = _require_mapping(raw, where="governance")
    policy_version = _coerce_str(raw.get("policy_version", "1"), where="governance.policy_version")

    ceiling_raw = _require_mapping(raw.get("autonomy_ceiling"), where="governance.autonomy_ceiling")
    if ceiling_raw:
        autonomy_ceiling: dict[str, str] = {}
        for proposal_class, level in ceiling_raw.items():
            autonomy_ceiling[str(proposal_class)] = _coerce_autonomy_level(
                level, where=f"governance.autonomy_ceiling.{proposal_class}"
            )
    else:
        autonomy_ceiling = dict(_DEFAULT_AUTONOMY_CEILING)

    sampling_raw = _require_mapping(raw.get("review_sampling"), where="governance.review_sampling")
    review_sampling = {cls: _parse_sampling_rule(cls, val) for cls, val in sampling_raw.items()}

    sla_raw = _require_mapping(raw.get("source_slas_hours"), where="governance.source_slas_hours")
    source_slas_hours = {
        str(ch): _coerce_int(hours, where=f"governance.source_slas_hours.{ch}", minimum=0) for ch, hours in sla_raw.items()
    }

    allowed_raw = raw.get("allowed_ai_tiers", [])
    if not isinstance(allowed_raw, (list, tuple)):
        raise ConfigError("arch_data_fix.governance.allowed_ai_tiers must be a list")
    allowed_ai_tiers = tuple(_coerce_str(t, where="governance.allowed_ai_tiers") for t in allowed_raw)

    cost_ceiling = raw.get("program_cost_ceiling_usd")
    if cost_ceiling is not None:
        cost_ceiling = _coerce_int(cost_ceiling, where="governance.program_cost_ceiling_usd", minimum=0)

    required_roles_raw = _require_mapping(raw.get("required_roles"), where="governance.required_roles")
    required_roles = {str(k): str(v) for k, v in required_roles_raw.items()}

    retention_overrides_raw = _require_mapping(raw.get("retention_overrides_days"), where="governance.retention_overrides_days")
    retention_overrides_days: dict[str, int] = {}
    for key, val in retention_overrides_raw.items():
        floor = RETENTION_FLOOR_DAYS.get(key)
        if floor is None:
            raise ConfigError(
                f"arch_data_fix.governance.retention_overrides_days.{key} is not a known retention key"
            )
        coerced = _coerce_int(val, where=f"governance.retention_overrides_days.{key}", minimum=floor)
        retention_overrides_days[key] = coerced

    return ProgramGovernancePolicy(
        schema_version=SCHEMA_VERSION,
        policy_version=policy_version,
        autonomy_ceiling=autonomy_ceiling,
        review_sampling=review_sampling,
        source_slas_hours=source_slas_hours,
        allowed_ai_tiers=allowed_ai_tiers,
        program_cost_ceiling_usd=cost_ceiling,
        solicitation=_parse_solicitation_policy(raw.get("solicitation")),
        required_roles=required_roles,
        retention_overrides_days=retention_overrides_days,
    )


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


def parse_arch_data_fix(program_id: str, raw_block: Any) -> ArchDataFixConfig:
    """Parse a raw ``arch_data_fix`` mapping into a validated ``ArchDataFixConfig``.

    ``raw_block`` is the value of the ``arch_data_fix`` key in ``program.yaml``.
    ``None`` or an empty mapping yields safe defaults with ``mode=off``.
    """
    raw = _require_mapping(raw_block, where="")
    mode = _coerce_enum(raw.get("mode", ExecutionMode.OFF.value), ExecutionMode, where="mode")

    cockpit_raw = _require_mapping(raw.get("cockpit"), where="cockpit")
    cockpit = CockpitConfig(
        enabled=_coerce_bool(cockpit_raw.get("enabled", True), where="cockpit.enabled"),
        history_keep_builds=_coerce_int(
            cockpit_raw.get("history_keep_builds", 30), where="cockpit.history_keep_builds", minimum=1
        ),
        history_keep_weeks=_coerce_int(
            cockpit_raw.get("history_keep_weeks", 56), where="cockpit.history_keep_weeks", minimum=1
        ),
        fleet_enabled=_coerce_bool(cockpit_raw.get("fleet_enabled", False), where="cockpit.fleet_enabled"),
    )

    value_raw = _require_mapping(raw.get("value"), where="value")
    value = ValueConfig(
        workflow=_coerce_str(value_raw.get("workflow", "weekly_issue"), where="value.workflow"),
        minimum_baseline_samples=_coerce_optional_int(
            value_raw.get("minimum_baseline_samples"), where="value.minimum_baseline_samples", minimum=0
        ),
        historical_baseline_allowed=_coerce_bool(
            value_raw.get("historical_baseline_allowed", True), where="value.historical_baseline_allowed"
        ),
    )

    channels_raw = _require_mapping(raw.get("channels"), where="channels")
    channels = {name: _parse_channel_budget(name, budget) for name, budget in channels_raw.items()}

    alerts_raw = _require_mapping(raw.get("alerts"), where="alerts")
    delivery = _coerce_enum(alerts_raw.get("delivery", AlertDelivery.COCKPIT.value), AlertDelivery, where="alerts.delivery")
    alerts = AlertsConfig(
        delivery=delivery,
        cooldown_minutes=_coerce_int(alerts_raw.get("cooldown_minutes", 1440), where="alerts.cooldown_minutes", minimum=0),
    )

    actuation_raw = _require_mapping(raw.get("actuation"), where="actuation")
    actuation = ActuationConfig(
        enabled=_coerce_bool(actuation_raw.get("enabled", False), where="actuation.enabled"),
        outbox_worker_enabled=_coerce_bool(
            actuation_raw.get("outbox_worker_enabled", False), where="actuation.outbox_worker_enabled"
        ),
    )

    solicitation = _coerce_enum(
        raw.get("solicitation", SolicitationMode.DISABLED.value), SolicitationMode, where="solicitation"
    )

    top3_raw = _require_mapping(raw.get("top_3_proposals"), where="top_3_proposals")
    top_3 = TopThreeProposalsConfig(
        enabled=_coerce_bool(top3_raw.get("enabled", True), where="top_3_proposals.enabled"),
        require_human_acceptance=_coerce_bool(
            top3_raw.get("require_human_acceptance", True), where="top_3_proposals.require_human_acceptance"
        ),
    )

    governance = _parse_governance(raw.get("governance"))
    retention = _parse_retention(raw.get("retention"), governance.retention_overrides_days)

    return ArchDataFixConfig(
        schema_version=SCHEMA_VERSION,
        program_id=program_id,
        mode=mode,
        cockpit=cockpit,
        value=value,
        channels=channels,
        alerts=alerts,
        actuation=actuation,
        solicitation=solicitation,
        top_3_proposals=top_3,
        retention=retention,
        governance=governance,
    )


def program_yaml_path(program_id: str, programs_root: Path) -> Path:
    """Resolve the ``program.yaml`` path for ``program_id``."""
    return Path(programs_root) / program_id / "program.yaml"


def load_arch_data_fix(
    program_id: str,
    *,
    programs_root: Path,
) -> ArchDataFixConfig:
    """Load the ``arch_data_fix`` block for a program, returning safe defaults if absent.

    Absent block -> defaults with ``mode=off``. Invalid values -> ``ConfigError``.
    """
    path = program_yaml_path(program_id, programs_root)
    if not path.exists():
        return ArchDataFixConfig(program_id=program_id)
    document = load_yaml_mapping(path, required=False)
    return parse_arch_data_fix(program_id, document.get("arch_data_fix"))

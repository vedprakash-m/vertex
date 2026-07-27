from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from src.core.exceptions import ConfigError
from src.core.yaml_utils import load_yaml_mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = REPO_ROOT / "vertex" / "policies" / "defaults.yaml"
AI_POLICY_PATH = REPO_ROOT / "vertex" / "policies" / "ai_policy.yaml"
FRESHNESS_POLICY_PATH = REPO_ROOT / "vertex" / "policies" / "freshness_policy.yaml"
POLICY_SCHEMA_VERSION = "1"

_VALID_MODEL_TIERS = frozenset({"standard", "premium", "mini"})


@dataclass(frozen=True, slots=True)
class AIModelCostPolicy:
    input_cost_per_1k_tokens: float
    output_cost_per_1k_tokens: float


@dataclass(frozen=True, slots=True)
class M365RoutingPolicy:
    agreement_boost: float
    disagreement_cap: float
    confidence_ceiling: float
    deterministic_confidence_threshold: float


@dataclass(frozen=True, slots=True)
class AIRequestRouterPolicy:
    observe_only: bool


@dataclass(frozen=True, slots=True)
class AIFeaturePolicy:
    max_tokens: int
    temperature: float
    model_tier: str
    frontier_eligible: bool
    structure_max_tokens: Optional[int] = None
    style_max_tokens: Optional[int] = None
    # D-06 tiered-routing policy (§7.6 / §10.6). All optional with safe defaults so
    # existing per-feature entries remain valid and the strict policy-wiring contract
    # (test_ai_feature_policy_wiring) does not require a new mandatory key.
    deterministic_first: bool = True
    tier0_confidence_threshold: float = 0.9
    local_router: Optional[str] = None
    max_frontier_calls_per_run: Optional[int] = None


def load_ai_model_cost_policy(deployment: str) -> AIModelCostPolicy:
    models = _required_mapping(_required_mapping(_load_policy_defaults().get("ai"), field_name="ai").get("models"), field_name="ai.models")
    deployment_key = str(deployment).strip()
    raw_policy = models.get(deployment_key) if deployment_key else None
    if raw_policy is None:
        raw_policy = models.get("default")
    model_policy = _required_mapping(raw_policy, field_name=f"ai.models.{deployment_key or 'default'}")
    return AIModelCostPolicy(
        input_cost_per_1k_tokens=_required_float(
            model_policy.get("input_cost_per_1k_tokens"),
            field_name="ai.models.*.input_cost_per_1k_tokens",
        ),
        output_cost_per_1k_tokens=_required_float(
            model_policy.get("output_cost_per_1k_tokens"),
            field_name="ai.models.*.output_cost_per_1k_tokens",
        ),
    )


def load_m365_routing_policy() -> M365RoutingPolicy:
    routing = _required_mapping(
        _required_mapping(_load_policy_defaults().get("m365"), field_name="m365").get("routing"),
        field_name="m365.routing",
    )
    return M365RoutingPolicy(
        agreement_boost=_required_probability(routing.get("agreement_boost"), field_name="m365.routing.agreement_boost"),
        disagreement_cap=_required_probability(routing.get("disagreement_cap"), field_name="m365.routing.disagreement_cap"),
        confidence_ceiling=_required_probability(routing.get("confidence_ceiling"), field_name="m365.routing.confidence_ceiling"),
        deterministic_confidence_threshold=_required_probability(
            routing.get("deterministic_confidence_threshold"),
            field_name="m365.routing.deterministic_confidence_threshold",
        ),
    )


def load_ai_request_router_policy() -> AIRequestRouterPolicy:
    ai = _required_mapping(_load_policy_defaults().get("ai"), field_name="ai")
    raw_router = ai.get("request_router")
    if raw_router is None:
        router: dict[str, Any] = {}
    elif isinstance(raw_router, dict):
        router = raw_router
    else:
        raise ConfigError(f"ai.request_router must be a mapping in {DEFAULT_POLICY_PATH}")
    return AIRequestRouterPolicy(
        observe_only=_bool_with_default(
            router.get("observe_only"),
            default=False,
            field_name="ai.request_router.observe_only",
        )
    )


def load_ai_feature_policy(feature_name: str) -> AIFeaturePolicy:
    document = _load_ai_policy_document()
    ai_features = _required_mapping(
        document.get("ai_features"),
        field_name="ai_features",
    )
    feature_key = str(feature_name or "").strip() or "default"
    raw_policy = ai_features.get(feature_key)
    if raw_policy is None:
        raw_policy = ai_features.get("default")
        if raw_policy is None:
            raise ConfigError(
                f"ai_features.{feature_key} not found and no 'default' fallback in {AI_POLICY_PATH}"
            )
    feature_policy = _required_mapping(
        raw_policy,
        field_name=f"ai_features.{feature_key if ai_features.get(feature_key) is not None else 'default'}",
    )

    structure_max_tokens_raw = feature_policy.get("structure_max_tokens")
    style_max_tokens_raw = feature_policy.get("style_max_tokens")
    structure_max_tokens: Optional[int] = (
        _required_int(
            structure_max_tokens_raw,
            field_name=f"ai_features.{feature_key}.structure_max_tokens",
        )
        if structure_max_tokens_raw is not None
        else None
    )
    style_max_tokens: Optional[int] = (
        _required_int(
            style_max_tokens_raw,
            field_name=f"ai_features.{feature_key}.style_max_tokens",
        )
        if style_max_tokens_raw is not None
        else None
    )

    raw_max_tokens = feature_policy.get("max_tokens")
    if raw_max_tokens is not None:
        max_tokens = _required_int(
            raw_max_tokens,
            field_name=f"ai_features.{feature_key}.max_tokens",
        )
    elif structure_max_tokens is not None and style_max_tokens is not None:
        # Dual-token feature (e.g. onboard_assistant): max_tokens is implicit and
        # derived by callers as max(structure_max_tokens, style_max_tokens).
        max_tokens = max(structure_max_tokens, style_max_tokens)
    else:
        raise ConfigError(
            f"ai_features.{feature_key}.max_tokens is required in {AI_POLICY_PATH} "
            "when structure_max_tokens/style_max_tokens are not both set"
        )

    temperature = _required_float(
        feature_policy.get("temperature"),
        field_name=f"ai_features.{feature_key}.temperature",
    )
    model_tier = _required_model_tier(
        feature_policy.get("model_tier"),
        field_name=f"ai_features.{feature_key}.model_tier",
    )
    frontier_eligible = _required_bool(
        feature_policy.get("frontier_eligible"),
        field_name=f"ai_features.{feature_key}.frontier_eligible",
    )
    deterministic_first = _optional_bool(
        feature_policy.get("deterministic_first"),
        default=True,
        field_name=f"ai_features.{feature_key}.deterministic_first",
    )
    tier0_confidence_threshold = _optional_probability(
        feature_policy.get("tier0_confidence_threshold"),
        default=0.9,
        field_name=f"ai_features.{feature_key}.tier0_confidence_threshold",
    )
    local_router_raw = feature_policy.get("local_router")
    local_router: Optional[str] = (
        str(local_router_raw).strip()
        if isinstance(local_router_raw, str) and local_router_raw.strip()
        else None
    )
    max_frontier_calls_raw = feature_policy.get("max_frontier_calls_per_run")
    max_frontier_calls_per_run: Optional[int] = (
        _required_int(
            max_frontier_calls_raw,
            field_name=f"ai_features.{feature_key}.max_frontier_calls_per_run",
        )
        if max_frontier_calls_raw is not None
        else None
    )

    return AIFeaturePolicy(
        max_tokens=max_tokens,
        temperature=temperature,
        model_tier=model_tier,
        frontier_eligible=frontier_eligible,
        structure_max_tokens=structure_max_tokens,
        style_max_tokens=style_max_tokens,
        deterministic_first=deterministic_first,
        tier0_confidence_threshold=tier0_confidence_threshold,
        local_router=local_router,
        max_frontier_calls_per_run=max_frontier_calls_per_run,
    )


@lru_cache(maxsize=1)
def _load_ai_policy_document() -> dict[str, Any]:
    if not AI_POLICY_PATH.exists():
        raise ConfigError(f"Missing required file: {AI_POLICY_PATH}")
    document = load_yaml_mapping(AI_POLICY_PATH)
    version = str(document.get("policy_schema_version") or "").strip()
    if version != POLICY_SCHEMA_VERSION:
        raise ConfigError(
            f"Policy schema mismatch: expected {POLICY_SCHEMA_VERSION}, got {version or '<missing>'}"
        )
    return document


@lru_cache(maxsize=1)
def _load_policy_defaults() -> dict[str, Any]:
    document = load_yaml_mapping(DEFAULT_POLICY_PATH)
    version = str(document.get("policy_schema_version") or "").strip()
    if version != POLICY_SCHEMA_VERSION:
        raise ConfigError(f"Policy schema mismatch: expected {POLICY_SCHEMA_VERSION}, got {version or '<missing>'}")
    return document


def _required_mapping(value: object, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} must be a mapping in {DEFAULT_POLICY_PATH}")
    return value


def _required_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a float in {DEFAULT_POLICY_PATH}")
    if isinstance(value, (int, float)):
        return float(value)
    raise ConfigError(f"{field_name} must be a float in {DEFAULT_POLICY_PATH}")


def _required_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field_name} must be an int in {AI_POLICY_PATH}")
    return value


def _required_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a boolean in {AI_POLICY_PATH}")
    return value


def _required_model_tier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a string in {AI_POLICY_PATH}")
    tier = value.strip()
    if tier not in _VALID_MODEL_TIERS:
        raise ConfigError(
            f"{field_name} must be one of {sorted(_VALID_MODEL_TIERS)} in {AI_POLICY_PATH}"
        )
    return tier


def _required_probability(value: object, *, field_name: str) -> float:
    probability = _required_float(value, field_name=field_name)
    if probability < 0.0 or probability > 1.0:
        raise ConfigError(f"{field_name} must be between 0.0 and 1.0 in {DEFAULT_POLICY_PATH}")
    return probability


def _optional_bool(value: object, *, default: bool, field_name: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a boolean in {AI_POLICY_PATH}")
    return value


def _optional_probability(value: object, *, default: float, field_name: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field_name} must be a float in {AI_POLICY_PATH}")
    probability = float(value)
    if probability < 0.0 or probability > 1.0:
        raise ConfigError(f"{field_name} must be between 0.0 and 1.0 in {AI_POLICY_PATH}")
    return probability


def _optional_positive_int(section: object, key: str, *, default: int | None) -> int | None:
    if not isinstance(section, dict) or key not in section:
        return default
    value = section[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer or null in {FRESHNESS_POLICY_PATH}")
    return value


def _bool_with_default(value: object, *, default: bool, field_name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{field_name} must be a boolean in {DEFAULT_POLICY_PATH}")


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    fact_type_ttl_days: dict[str, int]
    gather_cadence_hours: dict[str, float]
    #: specs/people.md §7/DIR-03: the real, governed people-registry
    #: field-staleness SLA. Ratified 2026-07-26 at 90 days -- the same
    #: number `people_query.py`'s `DEFAULT_STALE_FRESHNESS_DAYS` had
    #: already been using as an admitted "v1 placeholder"; formalizing it
    #: here (rather than picking a different number) avoids introducing a
    #: second, inconsistent threshold with no evidence to justify a change.
    people_registry_stale_after_days: int = 90
    #: BL-E4 activation, 2026-07-26: the operator explicitly rejected an
    #: OS-level (wall-clock, Task Scheduler) cadence for people-registry
    #: enrichment reminders, wanting the trigger tied to Vertex's own
    #: operational rhythm instead. `None` disables that trigger kind.
    people_registry_enrichment_nudge_every: int | None = 5
    people_registry_enrichment_report_every: int | None = 3


def load_freshness_policy(
    *,
    override_path: Path | None = None,
) -> FreshnessPolicy:
    """Load freshness TTLs and gather cadence from policy YAML.

    *override_path* allows per-program policy files to merge over the defaults.
    Unknown keys in override files are silently ignored (additive-only).
    """
    base = _load_freshness_policy_document()
    base_ttl: dict[str, int] = dict(base.get("fact_type_ttl_days") or {})
    base_cadence: dict[str, float] = {
        k: float(v)
        for k, v in (base.get("gather_cadence_hours") or {}).items()
    }
    people_registry_section = base.get("people_registry") or {}
    stale_after_days = (
        int(people_registry_section["stale_after_days"])
        if isinstance(people_registry_section, dict) and isinstance(people_registry_section.get("stale_after_days"), int)
        else 90
    )
    trigger_section = people_registry_section.get("enrichment_trigger") if isinstance(people_registry_section, dict) else None
    nudge_every = _optional_positive_int(trigger_section, "nudge_run_every", default=5)
    report_every = _optional_positive_int(trigger_section, "report_run_every", default=3)
    if override_path is not None and override_path.exists():
        try:
            import yaml as _yaml
            doc = _yaml.safe_load(override_path.read_text(encoding="utf-8")) or {}
        except Exception:
            doc = {}
        if isinstance(doc, dict):
            if isinstance(doc.get("fact_type_ttl_days"), dict):
                for k, v in doc["fact_type_ttl_days"].items():
                    if isinstance(v, int):
                        base_ttl[str(k)] = v
            if isinstance(doc.get("gather_cadence_hours"), dict):
                for k, v in doc["gather_cadence_hours"].items():
                    if isinstance(v, (int, float)):
                        base_cadence[str(k)] = float(v)
            override_people_registry = doc.get("people_registry")
            if isinstance(override_people_registry, dict):
                if isinstance(override_people_registry.get("stale_after_days"), int):
                    stale_after_days = int(override_people_registry["stale_after_days"])
                override_trigger = override_people_registry.get("enrichment_trigger")
                if isinstance(override_trigger, dict):
                    nudge_every = _optional_positive_int(override_trigger, "nudge_run_every", default=nudge_every)
                    report_every = _optional_positive_int(override_trigger, "report_run_every", default=report_every)
    return FreshnessPolicy(
        fact_type_ttl_days=base_ttl,
        gather_cadence_hours=base_cadence,
        people_registry_stale_after_days=stale_after_days,
        people_registry_enrichment_nudge_every=nudge_every,
        people_registry_enrichment_report_every=report_every,
    )


@lru_cache(maxsize=1)
def _load_freshness_policy_document() -> dict[str, Any]:
    if not FRESHNESS_POLICY_PATH.exists():
        raise ConfigError(f"Missing required file: {FRESHNESS_POLICY_PATH}")
    document = load_yaml_mapping(FRESHNESS_POLICY_PATH)
    version = str(document.get("policy_schema_version") or "").strip()
    if version != POLICY_SCHEMA_VERSION:
        raise ConfigError(
            f"Policy schema mismatch: expected {POLICY_SCHEMA_VERSION}, got {version or '<missing>'}"
        )
    return document

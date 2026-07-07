"""WI-3.0 [HARD]: truth_model.py — TruthContext, derive_truth_level, authority loader.

Core truth derivation module (§6.2). Zone A module (INV-1 applies).

**Phase-3 replaces Phase-1 static truth table in program_reality.py.**
The static-table contract test (test_ratchet_static_truth_*) is deleted when
this module is wired (WI-3.0 acceptance). Any demotion of a management fact
from HUMAN_CONFIRMED is a WI-3.0 defect.

**TruthContext rule order (§6.2.1):**
1. natural_key ∈ baseline_locked_keys                     → GOVERNANCE_LOCKED
2. confirm-loop / approved-review provenance               → HUMAN_CONFIRMED
3. (entity_id, authority_family) ∈ corroborated_keys      → CORROBORATED
4. source is primary authority AND source ∉ suspended_sources → SOURCE_VALIDATED
5. otherwise                                               → RAW_OBSERVED

**Separation rule (§6.2.1):** observations are evidence; management facts are
positions. Evidence becomes a position only via: confirm loop, explicit human
action, governed actuation (§6.11), or source-sync (§6.2.6, mirror fields,
machine-primary, primary-mode families only).

**MATERIALITY_PREDICATES** (§6.2.2): IDs map to callables; adding a
materiality rule = one function + one YAML id. NEVER expression strings.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from src.core.truth_levels import TruthLevel

# ---------------------------------------------------------------------------
# Policy path
# ---------------------------------------------------------------------------

_SOURCE_AUTHORITY_POLICY_PATH = Path("vertex/policies/source_authority.yaml")
_SOURCE_AUTHORITY_REPO_ROOT = Path(".")


# ---------------------------------------------------------------------------
# TruthContext (§6.2.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TruthContext:
    """Immutable per-command snapshot of the truth derivation context.

    Constructed once per command or gather run from already-loaded objects
    (Q:-drive rule). Used by BOTH the facade (read-time derivation) and the
    promotion stage (§6.3).

    ``suspended_sources``:
      READ PATH: latest persisted breaker verdicts from trust.source_score payloads.
      PROMOTION PATH (v3.2): includes in-run suspensions injected BEFORE
      detect_corroboration_and_conflicts runs.

    ``corroborated_keys``:
      (entity_id, authority_family) pairs covered by ACTIVE fact.corroboration events.
      (v3.2/E-1) Typed as frozenset[tuple[str, str]] for rule-3 membership test.
    """
    baseline_locked_keys: frozenset[str]
    suspended_sources: frozenset[str]
    corroborated_keys: frozenset[tuple[str, str]]


# ---------------------------------------------------------------------------
# Source authority policy loader (§6.2.2)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AuthorityEntry:
    """Authority matrix entry for one fact family."""
    primary: str
    secondary: tuple[str, ...]
    human_role: str
    mirror_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SorFlipFamilyConfig:
    """Validated SoR flip thresholds for one authority family."""
    clean_cycles_to_flip: int = 5
    divergence_tolerance: float = 0.02
    critical_zero: bool = True
    max_persistent_cycles: int = 8
    require_s0g_policy: bool = False


@dataclass(frozen=True, slots=True)
class SorFlipConfig:
    """Validated SoR flip gate configuration from source_authority.yaml."""
    defaults: SorFlipFamilyConfig = field(default_factory=SorFlipFamilyConfig)
    per_family: dict[str, SorFlipFamilyConfig] = field(default_factory=dict)

    def for_family(self, family: str) -> SorFlipFamilyConfig:
        return self.per_family.get(family, self.defaults)


@dataclass(frozen=True, slots=True)
class SourceAuthorityPolicy:
    """Loaded source authority policy (vertex/policies/source_authority.yaml)."""
    schema_version: str
    provenance_classes: dict[str, str]
    family_map: dict[str, str]
    authority: dict[str, AuthorityEntry]
    corroboration_window_hours: int
    conflict_trust_gap_threshold: float
    materiality_predicate_ids: tuple[str, ...]
    override_ttl_days: int
    sor_flip: SorFlipConfig = field(default_factory=SorFlipConfig)


@functools.lru_cache(maxsize=1)
def _load_source_authority_doc(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_source_authority_policy(
    *,
    override_path: Path | None = None,
    repo_root: Path = _SOURCE_AUTHORITY_REPO_ROOT,
) -> SourceAuthorityPolicy:
    """Load source authority policy from disk. Cached per path."""
    policy_path = override_path or (repo_root / _SOURCE_AUTHORITY_POLICY_PATH)
    raw = _load_source_authority_doc(policy_path)

    authority: dict[str, AuthorityEntry] = {}
    for family_name, entry_raw in (raw.get("authority") or {}).items():
        authority[family_name] = AuthorityEntry(
            primary=str(entry_raw.get("primary", "")),
            secondary=tuple(str(s) for s in (entry_raw.get("secondary") or [])),
            human_role=str(entry_raw.get("human_role", "")),
            mirror_fields=tuple(str(f) for f in (entry_raw.get("mirror_fields") or [])),
        )

    materiality_predicates = tuple(
        str(p["predicate"])
        for p in (raw.get("conflict", {}).get("materiality", {}).get("material_if") or [])
    )
    sor_flip = _parse_sor_flip_config(raw.get("sor_flip") or {})

    return SourceAuthorityPolicy(
        schema_version=str(raw.get("policy_schema_version", "1")),
        provenance_classes=dict(raw.get("provenance_classes") or {}),
        family_map=dict(raw.get("family_map") or {}),
        authority=authority,
        corroboration_window_hours=int(
            (raw.get("corroboration") or {}).get("base_window_hours", 72)
        ),
        conflict_trust_gap_threshold=float(
            (raw.get("conflict") or {}).get("trust_gap_threshold", 0.15)
        ),
        materiality_predicate_ids=materiality_predicates,
        override_ttl_days=int(raw.get("override_ttl_days", 90)),
        sor_flip=sor_flip,
    )


def _parse_sor_flip_config(raw: Any) -> SorFlipConfig:
    if not isinstance(raw, dict):
        raise ValueError("sor_flip must be a mapping")
    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise ValueError("sor_flip.defaults must be a mapping")
    defaults = _parse_sor_flip_family_config(defaults_raw, base=SorFlipFamilyConfig())
    per_family_raw = raw.get("per_family") or {}
    if not isinstance(per_family_raw, dict):
        raise ValueError("sor_flip.per_family must be a mapping")
    per_family: dict[str, SorFlipFamilyConfig] = {}
    for family, entry_raw in per_family_raw.items():
        if not isinstance(entry_raw, dict):
            raise ValueError(f"sor_flip.per_family.{family} must be a mapping")
        per_family[str(family)] = _parse_sor_flip_family_config(entry_raw, base=defaults)
    return SorFlipConfig(defaults=defaults, per_family=per_family)


def _parse_sor_flip_family_config(raw: dict[str, Any], *, base: SorFlipFamilyConfig) -> SorFlipFamilyConfig:
    clean_cycles = int(raw.get("clean_cycles_to_flip", base.clean_cycles_to_flip))
    divergence_tolerance = float(raw.get("divergence_tolerance", base.divergence_tolerance))
    critical_zero = _coerce_bool(raw.get("critical_zero", base.critical_zero), field_name="critical_zero")
    max_persistent = int(raw.get("max_persistent_cycles", base.max_persistent_cycles))
    require_s0g = _coerce_bool(raw.get("require_s0g_policy", base.require_s0g_policy), field_name="require_s0g_policy")

    if not 1 <= clean_cycles <= 52:
        raise ValueError("sor_flip.clean_cycles_to_flip must be between 1 and 52")
    if not 0.0 <= divergence_tolerance <= 1.0:
        raise ValueError("sor_flip.divergence_tolerance must be between 0.0 and 1.0")
    if not 1 <= max_persistent <= 104:
        raise ValueError("sor_flip.max_persistent_cycles must be between 1 and 104")
    if max_persistent < clean_cycles:
        raise ValueError("sor_flip.max_persistent_cycles must be >= clean_cycles_to_flip")

    return SorFlipFamilyConfig(
        clean_cycles_to_flip=clean_cycles,
        divergence_tolerance=divergence_tolerance,
        critical_zero=critical_zero,
        max_persistent_cycles=max_persistent,
        require_s0g_policy=require_s0g,
    )


def _coerce_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"sor_flip.{field_name} must be a boolean")


def get_authority_family(fact_type: str, policy: SourceAuthorityPolicy) -> str:
    """Return the authority_family for a fact_type from the policy."""
    return policy.family_map.get(fact_type, "unknown")


def is_primary_authority(
    source: str,
    family: str,
    policy: SourceAuthorityPolicy,
    *,
    ctx: TruthContext,
) -> bool:
    """Return True if source is the primary authority for the given family
    and is NOT currently suspended."""
    authority_entry = policy.authority.get(family)
    if authority_entry is None:
        return False
    if source in ctx.suspended_sources:
        return False
    return authority_entry.primary == source


# ---------------------------------------------------------------------------
# Digest registry (§6.2.3)
# ---------------------------------------------------------------------------

def compute_workitem_state_digest(payload: dict[str, Any]) -> str | None:
    """Digest: normalized state value."""
    state = payload.get("state") or payload.get("status")
    if state is None:
        return None
    return str(state).lower().strip()


def compute_metric_digest(payload: dict[str, Any], *, tolerance: float = 0.05) -> str | None:
    """Digest: value bucketed by tolerance band."""
    value = payload.get("value")
    if value is None:
        return None
    # Bucket into tolerance bands (e.g. 5% tolerance → floor to nearest 5%)
    try:
        v = float(value)
        bucket = int(v / tolerance) * tolerance
        return f"{bucket:.6f}"
    except (TypeError, ValueError):
        return str(value)


def compute_incident_digest(payload: dict[str, Any]) -> str | None:
    """Digest: severity + state tuple."""
    severity = payload.get("severity")
    state = payload.get("state") or payload.get("status")
    if severity is None and state is None:
        return None
    return f"{str(severity or '').lower()}:{str(state or '').lower()}"


def compute_commitment_digest(payload: dict[str, Any]) -> str | None:
    """Digest: (entity_id, promised_date, status)."""
    entity_id = payload.get("entity_ref") or payload.get("entity_id")
    promised_date = payload.get("due_date") or payload.get("promised_date")
    status = payload.get("status")
    if not entity_id and not promised_date:
        return None
    return f"{entity_id or ''}:{promised_date or ''}:{str(status or '').lower()}"


def compute_text_human_digest(
    payload: dict[str, Any],
    *,
    resolved_entity_id: str | None,
) -> str | None:
    """Digest: only for signals with a structured identifier resolving to a canonical entity.

    Unresolvable text is evidence-only (per spec §6.2.3 text.human rule).
    """
    if resolved_entity_id is None:
        return None
    structured_metadata = {
        k: v for k, v in payload.items()
        if k not in ("text", "body", "content")
    }
    if not structured_metadata:
        return None
    return f"{resolved_entity_id}:{hash(frozenset(str(v) for v in structured_metadata.values()))}"


# ---------------------------------------------------------------------------
# build_truth_context (§6.2.1)
# ---------------------------------------------------------------------------

def build_truth_context(
    program_id: str,
    *,
    fact_snapshot: Any,   # ProgramFactSnapshot
    trust_facts: tuple[Any, ...] | None = None,
) -> TruthContext:
    """Build a TruthContext from already-loaded objects (Q:-drive rule).

    ONE builder. Used by both the facade read path and the promotion stage.

    Args:
        program_id: Program identifier (for locked baseline lookup).
        fact_snapshot: ProgramFactSnapshot from load_program_facts().
        trust_facts: Optional sequence of trust.source_score facts for
                     breaker verdict injection (promotion path pre-injection).
    """
    # 1. Baseline-locked keys: from locked issue archive manifests
    baseline_locked_keys: frozenset[str] = _load_baseline_locked_keys(
        program_id, fact_snapshot=fact_snapshot
    )

    # 2. Suspended sources: from persisted trust.source_score facts
    suspended_sources: frozenset[str] = _load_suspended_sources(
        fact_snapshot=fact_snapshot,
        trust_facts=trust_facts or (),
    )

    # 3. Corroborated keys: from active fact.corroboration events
    corroborated_keys: frozenset[tuple[str, str]] = _load_corroborated_keys(
        fact_snapshot=fact_snapshot
    )

    return TruthContext(
        baseline_locked_keys=baseline_locked_keys,
        suspended_sources=suspended_sources,
        corroborated_keys=corroborated_keys,
    )


def build_trust_context_from_snapshot(fact_snapshot: Any) -> TruthContext:
    """Convenience: build TruthContext from a snapshot with no program_id.

    Used by `vertex reality status` (WI-3.9) when only the snapshot is available.
    Delegates to build_truth_context with an empty program_id.
    """
    program_id = getattr(fact_snapshot, "program_id", "")
    return build_truth_context(program_id, fact_snapshot=fact_snapshot)


def _load_baseline_locked_keys(
    program_id: str,
    *,
    fact_snapshot: Any,
) -> frozenset[str]:
    """Load natural keys that are governance-locked from archived snapshot manifests."""
    locked: set[str] = set()
    # Governance-locked facts are recorded with lifecycle_state="governance_locked"
    # in the fact store (per INV-2, only write_confirmed creates these).
    for fact in fact_snapshot.facts:
        state = getattr(fact, "lifecycle_state", None)
        if state is not None and str(state) == "governance_locked":
            locked.add(fact.natural_key)
    return frozenset(locked)


def _load_suspended_sources(
    *,
    fact_snapshot: Any,
    trust_facts: tuple[Any, ...],
) -> frozenset[str]:
    """Extract suspended sources from persisted trust.source_score facts."""
    suspended: set[str] = set()
    # From fact snapshot trust.source_score facts
    for fact in fact_snapshot.facts:
        if fact.fact_type != "trust.source_score":
            continue
        if fact.payload.get("suspended") or fact.payload.get("breaker_verdict") == "suspended":
            source = fact.payload.get("source")
            if source:
                suspended.add(str(source))
    # From freshly injected trust facts (promotion path)
    for fact in trust_facts:
        if getattr(fact, "fact_type", None) != "trust.source_score":
            continue
        payload = getattr(fact, "payload", {})
        if payload.get("suspended") or payload.get("breaker_verdict") == "suspended":
            source = payload.get("source")
            if source:
                suspended.add(str(source))
    return frozenset(suspended)


def _load_corroborated_keys(*, fact_snapshot: Any) -> frozenset[tuple[str, str]]:
    """Extract (entity_id, authority_family) pairs from active fact.corroboration events."""
    corroborated: set[tuple[str, str]] = set()
    for fact in fact_snapshot.facts:
        if fact.fact_type != "fact.corroboration":
            continue
        if str(getattr(fact, "lifecycle_state", "active") or "active").lower() != "active":
            continue
        entity_id = fact.payload.get("entity_id")
        family = fact.payload.get("family")
        if entity_id and family:
            corroborated.add((str(entity_id), str(family)))
    return frozenset(corroborated)


# ---------------------------------------------------------------------------
# derive_truth_level (§6.2.1)
# ---------------------------------------------------------------------------

_MANAGEMENT_FAMILIES: frozenset[str] = frozenset({
    "judgment",
    "commitment",
    "narrative",
})

_CONFIRM_LOOP_REVIEW_STATES: frozenset[str] = frozenset({
    "accepted",
    "confirmed",
    "approved",
    "human_confirmed",
})


def derive_truth_level(
    fact: Any,            # ProgramFactRevision
    ctx: TruthContext,
    *,
    policy: SourceAuthorityPolicy | None = None,
) -> TruthLevel:
    """Derive truth level for a fact given the current TruthContext (§6.2.1).

    Rules (in order):
    1. natural_key ∈ baseline_locked_keys → GOVERNANCE_LOCKED
    2. confirm-loop / approved-review provenance → HUMAN_CONFIRMED
    3. (entity_id, authority_family) ∈ corroborated_keys → CORROBORATED
    4. source is primary authority AND source ∉ suspended_sources → SOURCE_VALIDATED
    5. otherwise → RAW_OBSERVED

    Phase-3 (v3.0): this replaces the static table in program_reality.py.
    **Any demotion of a management fact from HUMAN_CONFIRMED is a WI-3.0 defect.**
    """
    if policy is None:
        try:
            policy = load_source_authority_policy()
        except Exception:
            # Graceful degradation if policy not available
            policy = None

    # Rule 1: Governance-locked (highest priority)
    if fact.natural_key in ctx.baseline_locked_keys:
        return TruthLevel.GOVERNANCE_LOCKED

    write_authority = str(getattr(fact, "write_authority", "human") or "human").lower()

    # Rule 2: Confirm-loop / approved-review provenance
    review_state = str(getattr(fact, "review_state", "") or "").lower()
    if review_state in _CONFIRM_LOOP_REVIEW_STATES and write_authority == "human":
        return TruthLevel.HUMAN_CONFIRMED

    # For management facts (human-primary families), default to HUMAN_CONFIRMED
    # when they have human review provenance (accepted_by is set)
    accepted_by = getattr(fact, "accepted_by", None)
    if accepted_by and write_authority == "human":
        return TruthLevel.HUMAN_CONFIRMED

    if policy is not None:
        family = get_authority_family(fact.fact_type, policy)
        authority_entry = policy.authority.get(family)
        if authority_entry and authority_entry.primary == "human":
            # Management facts (judgment, commitment, narrative) are HUMAN_CONFIRMED
            # only when they have confirmed provenance (rule 2 above).
            # Without review, they are only RAW_OBSERVED in the fact store.
            # But if they are management families (trusted by definition), we treat
            # them as SOURCE_VALIDATED at minimum.
            # NOTE: per spec §6.2.1, management facts reach HUMAN_CONFIRMED only
            # via review/confirm. Without that, they stay at RAW_OBSERVED or SOURCE_VALIDATED.
            pass

    # Rule 3: Corroborated
    if policy is not None:
        family = get_authority_family(fact.fact_type, policy)
        entity_refs = tuple(getattr(fact, "entity_refs", ()))
        if entity_refs:
            entity_id = entity_refs[0]
            if (str(entity_id), family) in ctx.corroborated_keys:
                return TruthLevel.CORROBORATED

    # Rule 4: Primary authority (source-validated)
    if policy is not None:
        source = _get_fact_source(fact)
        if source is not None:
            family = get_authority_family(fact.fact_type, policy)
            if is_primary_authority(source, family, policy, ctx=ctx):
                return TruthLevel.SOURCE_VALIDATED

    # Rule 5: RAW_OBSERVED
    return TruthLevel.RAW_OBSERVED


def _get_fact_source(fact: Any) -> str | None:
    """Extract the source identifier from a fact."""
    # Try payload first, then source_signal_ids prefix
    payload = getattr(fact, "payload", {})
    if payload.get("source"):
        return str(payload["source"])
    # Infer from source_signal_ids prefix (e.g. "ado_12345" → "ado")
    signal_ids = tuple(getattr(fact, "source_signal_ids", ()))
    if signal_ids:
        first = str(signal_ids[0])
        # Common prefix patterns: "ado_", "kusto_", "icm_", "teams_"
        for prefix in ("ado_pr_", "ado_", "kusto_", "icm_", "workiq_", "teams_", "transcript_"):
            if first.startswith(prefix):
                return prefix.rstrip("_")
    return None


# ---------------------------------------------------------------------------
# MATERIALITY_PREDICATES (§6.2.2)
# ---------------------------------------------------------------------------

def _predicate_family_is_commitment(fact: Any, program_reality: Any) -> bool:
    """Fire for commitment.entry facts."""
    return getattr(fact, "fact_type", "") == "commitment.entry"


def _predicate_severity_critical_or_high(fact: Any, program_reality: Any) -> bool:
    """Fire for risk.entry facts with critical or high severity."""
    if getattr(fact, "fact_type", "") != "risk.entry":
        return False
    payload = getattr(fact, "payload", {})
    severity = str(payload.get("risk_impact") or payload.get("impact") or "").lower()
    return severity in ("critical", "high")


def _predicate_entity_in_open_milestone_due_14d(fact: Any, program_reality: Any) -> bool:
    """Fire if the fact's entity is referenced in an open milestone due within 14 days."""
    if program_reality is None:
        return False
    entity_refs = set(str(r) for r in (getattr(fact, "entity_refs", ()) or ()))
    if not entity_refs:
        return False
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(days=14)
    try:
        milestones = program_reality.milestones()
        for assessment in milestones:
            ms = assessment.record
            # Check due_date overlap
            due_str = getattr(ms, "due_date", None) or getattr(ms, "target_date", None)
            if not due_str:
                continue
            try:
                if isinstance(due_str, str):
                    due = datetime.fromisoformat(due_str.rstrip("Z"))
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=timezone.utc)
                elif isinstance(due_str, datetime):
                    due = due_str
                else:
                    continue
            except ValueError:
                continue
            status = str(getattr(ms, "status", "") or "").lower()
            if status in ("done", "closed", "complete", "cancelled"):
                continue
            if due <= deadline:
                return True
    except Exception:
        pass
    return False


def _predicate_governance_locked_adjacent(fact: Any, program_reality: Any) -> bool:
    """Fire if the fact is adjacent to governance-locked facts (shares entity_refs)."""
    if program_reality is None:
        return False
    entity_refs = set(str(r) for r in (getattr(fact, "entity_refs", ()) or ()))
    if not entity_refs:
        return False
    try:
        for domain_assessment in program_reality._all_assessments():
            if domain_assessment.truth_level == TruthLevel.GOVERNANCE_LOCKED:
                locked_refs = set(str(r) for r in domain_assessment.evidence)
                if entity_refs & locked_refs:
                    return True
    except Exception:
        pass
    return False


MATERIALITY_PREDICATES: dict[str, Callable[[Any, Any], bool]] = {
    "family_is_commitment": _predicate_family_is_commitment,
    "severity_critical_or_high": _predicate_severity_critical_or_high,
    "entity_in_open_milestone_due_14d": _predicate_entity_in_open_milestone_due_14d,
    "governance_locked_adjacent": _predicate_governance_locked_adjacent,
}


def is_material_conflict(
    fact: Any,
    program_reality: Any,
    *,
    predicate_ids: tuple[str, ...] | None = None,
) -> bool:
    """Return True if any materiality predicate fires for this fact.

    If predicate_ids is None, evaluates all registered predicates.
    """
    ids_to_check = predicate_ids if predicate_ids is not None else tuple(MATERIALITY_PREDICATES.keys())
    for pred_id in ids_to_check:
        fn = MATERIALITY_PREDICATES.get(pred_id)
        if fn is not None and fn(fact, program_reality):
            return True
    return False


# ---------------------------------------------------------------------------
# Corroboration & conflict detection (WI-3.2b, §6.2.3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CorroborationConflictResult:
    """Result from detect_corroboration_and_conflicts (WI-3.2b).

    All event dicts are suitable for append_fact() calls by the caller.
    ``challenge_inputs`` are dicts the caller passes to
    reality_store.upsert_challenge() — kept as dicts to avoid importing
    reality_store inside Zone A truth_model.py.
    """
    corroborations: tuple[dict, ...]    # fact.corroboration payloads to emit
    conflicts: tuple[dict, ...]         # fact.conflict payloads to emit
    sync_pending_count: int             # sync-eligible deltas in legacy/shadow mode
    sync_events: tuple[dict, ...]       # fact.source_sync payloads (primary SoR mode)
    challenge_inputs: tuple[dict, ...]  # challenge dicts for reality_store.upsert_challenge


def _resolve_family_for_obs(
    obs: Any,
    fact_type: str,
    authority: SourceAuthorityPolicy,
) -> str:
    """Resolve the authority_family for an observation.

    signal.observation facts are BY_SIGNAL_CLASS — their real family is in
    the signal class payload (e.g. metric.validated → metric, icm.incident →
    incident). All other fact types look up the static family_map.
    """
    family = authority.family_map.get(fact_type, "unknown")
    if family == "BY_SIGNAL_CLASS":
        # Resolve from payload: signal_class field or payload keys
        payload = getattr(obs, "payload", {}) or {}
        signal_class = str(payload.get("signal_class") or payload.get("class") or "")
        # Map well-known signal classes to families
        if "metric" in signal_class.lower():
            return "metric"
        if "incident" in signal_class.lower() or "icm" in signal_class.lower():
            return "incident"
        if "workitem" in signal_class.lower() or "ado" in signal_class.lower():
            return "workitem.state"
        if "text" in signal_class.lower() or "human" in signal_class.lower():
            return "narrative"
        return "unknown"
    return family


def _compute_obs_digest(obs: Any, family: str, registry: Any) -> str | None:
    """Compute the comparison digest for an observation, keyed by family."""
    payload = getattr(obs, "payload", {}) or {}
    if family == "workitem.state":
        return compute_workitem_state_digest(payload)
    if family == "metric":
        return compute_metric_digest(payload)
    if family == "incident":
        return compute_incident_digest(payload)
    if family == "commitment":
        return compute_commitment_digest(payload)
    if family in ("narrative", "judgment"):
        # text.human digests only when entity resolves to canonical id
        entity_refs = tuple(getattr(obs, "entity_refs", ()) or ())
        resolved = None
        if entity_refs and registry is not None:
            resolve_fn = getattr(registry, "resolve", None)
            if resolve_fn is not None:
                try:
                    resolved = str(resolve_fn(entity_refs[0]))
                except Exception:
                    resolved = None
        return compute_text_human_digest(payload, resolved_entity_id=resolved)
    return None


def detect_corroboration_and_conflicts(
    observations: list[Any],
    authority: SourceAuthorityPolicy,
    trust_ledger: dict[str, Any] | None,
    registry: Any | None,
    *,
    ctx: TruthContext,
    now: datetime,
    program_fact_store: Any | None = None,
    family_sor_modes: dict[str, str] | None = None,
) -> CorroborationConflictResult:
    """WI-3.2b: Detect corroboration and conflicts across observations (§6.2.3).

    Run as a promotion substep. Groups observations by (entity_id, authority_family),
    computes digests, and:
    - Emits fact.corroboration for agreeing, independent observations (D-3).
    - Runs materiality-aware detection ladder for disagreements:
        (0) minor/material materiality triage
        (1) primary-authority precedence (unless suspended)
        (2) trust-gap precedence
        (3) open conflict via fact.conflict + challenge_input
    - Counts sync-pending in legacy/shadow mode (Phase 5 gate).
    - Executes source_sync in primary SoR mode when all conditions met.

    Args:
        observations: ProgramFactRevision-like objects being promoted.
        authority: Loaded SourceAuthorityPolicy.
        trust_ledger: Optional map of source → trust score (0.0–1.0).
        registry: Optional entity alias registry with .resolve(ref) → canonical_id.
        ctx: Current TruthContext (includes suspended sources, corroborated keys).
        now: Current UTC datetime.
        program_fact_store: Optional — used to load existing open conflicts (continuity).
        family_sor_modes: Optional map of family → "legacy"|"shadow"|"primary".
            Defaults all families to "legacy" (Phase 5 not yet complete).

    Returns:
        CorroborationConflictResult with events to emit and challenge inputs.
    """
    import uuid

    sor_modes: dict[str, str] = family_sor_modes or {}

    # --- Step 1: Build per-observation metadata ---
    @dataclass
    class _ObsInfo:
        obs: Any
        entity_id: str
        family: str
        prov_class: str
        digest: str | None
        source: str | None

    obs_infos: list[_ObsInfo] = []
    for obs in observations:
        entity_refs = tuple(getattr(obs, "entity_refs", ()) or ())
        if not entity_refs:
            continue
        entity_id = str(entity_refs[0])
        # Resolve canonical entity id via registry if available
        if registry is not None:
            resolve_fn = getattr(registry, "resolve", None)
            if resolve_fn is not None:
                try:
                    resolved = resolve_fn(entity_id)
                    if resolved:
                        entity_id = str(resolved)
                except Exception:
                    pass

        fact_type = str(getattr(obs, "fact_type", ""))
        family = _resolve_family_for_obs(obs, fact_type, authority)
        if not family or family == "unknown":
            continue

        source = _get_fact_source(obs)
        prov_class = authority.provenance_classes.get(source, "unknown") if source else "unknown"
        digest = _compute_obs_digest(obs, family, registry)

        obs_infos.append(_ObsInfo(obs, entity_id, family, prov_class, digest, source))

    # --- Step 2: Group by (entity_id, family) ---
    from collections import defaultdict
    groups: dict[tuple[str, str], list[_ObsInfo]] = defaultdict(list)
    for info in obs_infos:
        groups[(info.entity_id, info.family)].append(info)

    # --- Step 3: Load existing open conflicts for continuity ---
    open_conflicts: dict[tuple[str, str], str] = {}  # (entity_id, family) → conflict_id
    if program_fact_store is not None:
        try:
            snap = program_fact_store.snapshot(as_of=now)
            for fact in snap.facts:
                if fact.fact_type == "fact.conflict" and not fact.payload.get("resolved", False):
                    eid = fact.payload.get("entity_id")
                    fam = fact.payload.get("family")
                    cid = fact.payload.get("conflict_id") or fact.natural_key
                    if eid and fam:
                        open_conflicts[(str(eid), str(fam))] = str(cid)
        except Exception:
            pass

    # --- Step 4: Process each group ---
    corroborations: list[dict] = []
    conflicts: list[dict] = []
    sync_pending = 0
    sync_events: list[dict] = []
    challenge_inputs: list[dict] = []

    for (entity_id, family), group in groups.items():
        if len(group) < 2:
            continue

        authority_entry = authority.authority.get(family)

        # Compare every distinct pair
        seen_pairs: set[tuple[int, int]] = set()
        for i, a in enumerate(group):
            for j, b in enumerate(group):
                if i >= j:
                    continue
                pair = (i, j)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                # -- Independence check (D-3, INV-13) --
                # Observations corroborate ONLY if their provenance classes differ
                if a.prov_class == b.prov_class:
                    # Same provenance class → no corroboration (INV-13)
                    continue

                # Both must have digestible values to compare
                if a.digest is None or b.digest is None:
                    continue

                if a.digest == b.digest:
                    # --- Corroboration ---
                    corroborations.append({
                        "fact_type": "fact.corroboration",
                        "entity_id": entity_id,
                        "family": family,
                        "digest": a.digest,
                        "source_a": a.source or "unknown",
                        "source_b": b.source or "unknown",
                        "prov_class_a": a.prov_class,
                        "prov_class_b": b.prov_class,
                        "detected_at": now.isoformat(),
                    })
                else:
                    # --- Disagreement: detection ladder ---
                    sync_pending = _detect_conflict(
                        a=a,
                        b=b,
                        entity_id=entity_id,
                        family=family,
                        authority_entry=authority_entry,
                        authority=authority,
                        ctx=ctx,
                        trust_ledger=trust_ledger or {},
                        open_conflicts=open_conflicts,
                        now=now,
                        conflicts=conflicts,
                        challenge_inputs=challenge_inputs,
                        sync_pending_base=sync_pending,
                        sync_events=sync_events,
                        sor_modes=sor_modes,
                    )

    return CorroborationConflictResult(
        corroborations=tuple(corroborations),
        conflicts=tuple(conflicts),
        sync_pending_count=sync_pending,
        sync_events=tuple(sync_events),
        challenge_inputs=tuple(challenge_inputs),
    )


def _detect_conflict(
    *,
    a: Any,
    b: Any,
    entity_id: str,
    family: str,
    authority_entry: Any,
    authority: SourceAuthorityPolicy,
    ctx: TruthContext,
    trust_ledger: dict[str, Any],
    open_conflicts: dict[tuple[str, str], str],
    now: datetime,
    conflicts: list[dict],
    challenge_inputs: list[dict],
    sync_pending_base: int,
    sync_events: list[dict],
    sor_modes: dict[str, str],
) -> int:
    """Run the detection ladder for a conflicting observation pair.

    Returns updated sync_pending count.
    """
    import uuid

    sync_pending = sync_pending_base

    # (0) Materiality triage — use lightweight predicate check without program_reality
    # For contract purposes: use family-level materiality proxies
    material = _is_material_by_family(family, a, b)

    # (1) Primary-authority precedence (only if primary ∉ ctx.suspended_sources)
    winner_source: str | None = None
    if authority_entry is not None:
        primary = authority_entry.primary
        if primary and primary not in ctx.suspended_sources:
            if a.source == primary:
                winner_source = a.source
            elif b.source == primary:
                winner_source = b.source

    # (2) Trust-gap precedence (if no primary winner yet)
    if winner_source is None and trust_ledger:
        score_a = float(trust_ledger.get(a.source or "", 0.0))
        score_b = float(trust_ledger.get(b.source or "", 0.0))
        gap = abs(score_a - score_b)
        if gap >= authority.conflict_trust_gap_threshold:
            winner_source = a.source if score_a > score_b else b.source

    # Identify winning/losing observations
    if winner_source is not None:
        winning_obs = a if a.source == winner_source else b
        losing_obs = b if a.source == winner_source else a
    else:
        # No winner: prefer non-suspended source as nominal "winning" side
        if a.source in ctx.suspended_sources and b.source not in ctx.suspended_sources:
            winning_obs, losing_obs = b, a
        else:
            winning_obs, losing_obs = a, b  # no winner resolved

    conflict_key = (entity_id, family)

    if not material:
        # --- Minor conflict: no challenge, resolution="unresolved_minor" ---
        conflicts.append({
            "fact_type": "fact.conflict",
            "entity_id": entity_id,
            "family": family,
            "target_natural_key": getattr(losing_obs.obs, "natural_key", ""),
            "winning_source": winning_obs.source or "unknown",
            "losing_source": losing_obs.source or "unknown",
            "observed_value": winning_obs.digest,
            "expected_value": losing_obs.digest,
            "material": False,
            "resolution": "unresolved_minor",
            "resolved": False,
            "detected_at": now.isoformat(),
        })
        # No challenge for minor conflicts
    else:
        # --- Material conflict ---
        # Conflict continuity: reuse existing conflict id if one is already open
        existing_conflict_id = open_conflicts.get(conflict_key)
        conflict_id = existing_conflict_id or str(uuid.uuid4())

        if winner_source is not None:
            # (3) Open conflict only when no winner could be determined
            # Winner resolved → still record conflict for tracking, but mark with resolution
            conflict_resolution = f"precedence:{winner_source}"
        else:
            conflict_resolution = "unresolved"

        conflicts.append({
            "fact_type": "fact.conflict",
            "conflict_id": conflict_id,
            "entity_id": entity_id,
            "family": family,
            "target_natural_key": getattr(losing_obs.obs, "natural_key", ""),
            "winning_source": winning_obs.source or "unknown",
            "losing_source": losing_obs.source or "unknown",
            "observed_value": winning_obs.digest,
            "expected_value": losing_obs.digest,
            "material": True,
            "resolution": conflict_resolution,
            "resolved": winner_source is not None,
            "detected_at": now.isoformat(),
        })

        if winner_source is None:
            # Create/update challenge for open material conflicts
            challenge_inputs.append({
                "conflict_id": conflict_id,
                "entity_id": entity_id,
                "family": family,
                "target_natural_key": getattr(losing_obs.obs, "natural_key", ""),
                "source_a": a.source or "unknown",
                "source_b": b.source or "unknown",
                "value_a": a.digest,
                "value_b": b.digest,
                "detected_at": now.isoformat(),
                "is_continuation": existing_conflict_id is not None,
            })

    # Source sync opportunity (§6.2.6): check if this is sync-eligible
    # Sync-eligible: machine primary for family, primary observation (winner),
    # uncontested (no open conflict), breaker clear (primary not suspended),
    # differing fields ⊆ mirror_fields
    if (
        authority_entry is not None
        and authority_entry.primary not in ("human", "", None)
        and winner_source is not None
        and winner_source == authority_entry.primary
        and winner_source not in ctx.suspended_sources
        and authority_entry.mirror_fields
        and conflict_key not in open_conflicts
    ):
        sor_mode = sor_modes.get(family, "legacy")
        if sor_mode == "primary":
            # Execute sync: emit fact.source_sync
            winning_payload = getattr(winning_obs.obs, "payload", {}) or {}
            sync_delta = {
                k: winning_payload[k]
                for k in authority_entry.mirror_fields
                if k in winning_payload
            }
            if sync_delta:
                sync_events.append({
                    "fact_type": "fact.source_sync",
                    "entity_id": entity_id,
                    "family": family,
                    "source": winner_source,
                    "mirror_fields_updated": list(sync_delta.keys()),
                    "delta": sync_delta,
                    "target_natural_key": getattr(losing_obs.obs, "natural_key", ""),
                    "synced_at": now.isoformat(),
                })
        else:
            # legacy/shadow: count only
            sync_pending += 1

    return sync_pending


def _is_material_by_family(family: str, a: Any, b: Any) -> bool:
    """Lightweight materiality check without full program_reality context.

    Used inside detect_corroboration_and_conflicts for the detection ladder.
    Commitment and incident families are always material per the YAML predicates.
    """
    if family == "commitment":
        return True  # family_is_commitment predicate
    if family == "incident":
        # severity_critical_or_high — check if either observation has high/critical
        for obs_info in (a, b):
            payload = getattr(obs_info.obs, "payload", {}) or {}
            severity = str(payload.get("severity") or payload.get("risk_impact") or "").lower()
            if severity in ("critical", "high"):
                return True
    return False


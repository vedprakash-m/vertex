"""RELO-F1 (specs/relo.md Sec 8.1): the mandatory structured-feature boundary.

Every existing AI-adjacent primitive this composes already exists and is
independently tested: ``src.ai.tiered_router.route_through_tiers``-style
Tier 0/1/2 telemetry vocabulary (``Tier``, ``TierDecision``,
``RouteOutcome``, reused directly rather than re-invented), ``src.ai.client``
(schema-validated provider calls that already do their own atomic cost
reservation/settlement via an internal ``CostGuard`` — see below), and
``src.core.context_compiler`` (token-budgeted, overflow-safe prompt
assembly). Today every feature module wires a subset of these
independently and inconsistently (RELO-P1: 31 call sites inventoried
2026-07-29, only 3 use ``ContextCompiler``, 1 bypasses tiering entirely);
this module is the single façade that composes them the same way, every
time, so nothing can silently skip a step.

**Cost reservation is deliberately NOT duplicated here.**
``AIClient.structured()``/``.chat()`` already atomically reserve and settle
against their own internal ``CostGuard`` (built from ``trace_context`` —
see ``AIClient._reserve_budget``/``_record_usage``). A caller's
``call_provider`` is expected to invoke ``AIClient`` (or an equivalent
``LLMProvider``); this runner must never wrap that same call in a second,
independent reservation against a different ``CostGuard`` instance, or the
same spend would be double-reserved against two budgets. The one thing
this runner *does* own atomically is the Sec 8.1.5 remote/frontier
call-COUNT ceiling (``CallCeilingLedger``), which does not exist anywhere
else in the codebase (confirmed by inventory) and is orthogonal to dollar
cost.

**Deliberately out of scope for this module** (named here so a reader does
not assume silent coverage): immutable ``RunEvidenceSnapshot`` pinning and
execution-principal/authorization-profile binding (Sec 9.0's bitemporal
snapshot system is a separate, not-yet-inventoried subsystem — this runner
accepts an already-resolved ``snapshot_id``/``principal`` as opaque
identifiers, it does not do the pinning itself; nonblank identity checks
and compiled program/feature/prompt matching do not authenticate them); migrating the ~31 existing
feature call sites onto this runner (RELO-W1.3/W1.6 — a separate, reviewed,
one-at-a-time effort per feature, not a wholesale flip); the audience/
disclosure gate (Sec 8.8.4a) beyond the semantic validator hook a caller
supplies; Pydantic response models (nothing in `src/ai` uses Pydantic
today — this runner stays validator-shape-neutral, matching the existing
``AISchemaGateway.SemanticValidator`` protocol's style).

**RELO-W1.13's collaborator decomposition.** Sec 8.1.2 names ten internal
collaborators (``ExecutionPlanner``, ``SnapshotProvider``,
``ContextProvider``, ``TierExecutor``, ``BudgetReservationStore``,
``ProviderInvoker``, ``ResponseValidator``, ``ReleasePolicy``,
``ReleasedCache``, ``ExecutionRecorder``). This module does not name ten
matching classes 1:1 — several of those concerns belong to subsystems this
runner deliberately doesn't own (``SnapshotProvider``/authorization
binding, see above) or are already independently-owned, independently-
tested modules this runner composes rather than wraps in a redundant layer
(``BudgetReservationStore`` is ``AIClient``'s own internal ``CostGuard``,
by design — see above). What Sec 8.1.13's actual acceptance bar asks for
— "façade has focused testable ports" and "blocked/unavailable/partial/
abstained cannot masquerade as released" — is met by the seams that do
exist here, every one of them independently swappable via
``StructuredFeatureSpec`` fields or constructor injection, independently
covered by tests that supply a fake for exactly that seam:

- ``TierExecutor`` → ``_pre_context_fast_path`` (deterministic cache,
  ``deterministic_handler``, ``local_router_name``/``local_routers``);
- ``ContextProvider`` → ``spec.build_context``/``build_batch_context``,
  wrapping the already-separate ``DeterministicContextCompiler``;
- ``ProviderInvoker`` → ``spec.call_provider``/``call_provider_batch``;
- ``ResponseValidator`` → ``spec.semantic_validator``;
- ``ReleasedCache`` → the ``ReleasedResultCache``/``DeterministicResultCache``
  classes above, constructor-injected;
- ``ExecutionRecorder`` → ``decision_sink``, constructor-injected;
- ``ReleasePolicy`` → the closed ``FeatureOutcomeStatus`` vocabulary itself,
  enforced by ``_fallback()`` only ever relabeling a ``RELEASED`` outcome
  (to ``DETERMINISTIC_FALLBACK``); every other status
  (``BLOCKED``/``UNAVAILABLE``/``PARTIAL``/``ABSTAINED``/``EVIDENCE_GAP``)
  passes through a caller's ``fallback_handler`` untouched, so none of them
  can ever surface as ``RELEASED``.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Generic, Protocol, TypeVar

from src.ai.client import BudgetExceeded
from src.ai.tiered_router import RouteOutcome, Tier, TierDecision, TierResult
from src.core._db import open_program_db
from src.core.context_compiler import CompiledContext, ContextCompileRejected
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.exceptions import StateError
from src.core.workspace_paths import is_bare_identifier

RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")


class FeatureOutcomeStatus(str, Enum):
    """The closed status vocabulary every runner call returns (Sec 8.1.2).
    No caller receives an unqualified value — every non-``RELEASED`` status
    carries a reason, and the union covers every exit this runner has."""

    RELEASED = "released"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"
    ABSTAINED = "abstained"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    EVIDENCE_GAP = "evidence_gap"


@dataclass(frozen=True, slots=True)
class FeatureOutcome(Generic[ResponseT]):
    """The runner's one return type. ``value`` is only meaningful when
    ``status`` is ``RELEASED``, ``DETERMINISTIC_FALLBACK``, or ``PARTIAL``;
    callers must branch on ``status``, never assume ``value`` is present."""

    status: FeatureOutcomeStatus
    value: ResponseT | None
    tier: Tier
    limitations: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    retryable: bool = False
    next_action: str | None = None

    @property
    def released(self) -> bool:
        return self.status == FeatureOutcomeStatus.RELEASED


class LocalRouter(Protocol):
    """Sec 8.1.4's provider-neutral local-router contract. A concrete
    router (e.g. the existing ``M365TopicRouter``) is adapted to this
    protocol at registration time; source-specific fields never leak into
    the runner core."""

    def route(self, request: Any) -> TierResult[Any] | None:
        """Return a confident result, or ``None`` to abstain."""
        ...


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The outcome of a feature's semantic/grounding validator. Mirrors the
    shape of the existing ``AISchemaGateway.SemanticValidator`` protocol's
    non-raising findings style, generalized beyond schema bounds checks to
    grounding/privacy/audience gates (Sec 8.1.2 step 13)."""

    valid: bool
    reason: str | None = None
    limitations: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextBuildResult:
    """What a feature's ``build_context`` callable returns: the compiled
    context (already token-budgeted by ``DeterministicContextCompiler``).
    Carries no cost estimate — cost reservation belongs entirely to
    ``AIClient``/``CostGuard`` inside ``call_provider`` (see module
    docstring); duplicating it here would double-reserve."""

    compiled: CompiledContext


@dataclass(frozen=True, slots=True)
class BatchProviderResult(Generic[ResponseT]):
    """Sec 8.1.9: what one batched provider call returns. ``responses``
    maps item_id -> response for every item the provider actually produced
    a response for; ``failed_item_ids`` names items the provider explicitly
    could not process (a partial failure, per item — never silently
    dropped). An item_id absent from both is treated the same as a listed
    failure: the batch runner does not assume success by omission."""

    responses: dict[str, ResponseT]
    failed_item_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StructuredFeatureSpec(Generic[RequestT, ResponseT]):
    """Sec 8.1.2's typed feature registration. One spec per feature, reused
    across runs — it is not per-call state.

    ``call_provider`` is responsible for invoking ``AIClient`` (or an
    equivalent ``LLMProvider``) itself, including that client's own cost
    reservation/settlement and schema parsing — the runner does not touch
    cost or parsing directly, only sequencing, call-count ceilings, and
    post-call validation/outcome typing.
    """

    feature: str
    task_class: str
    prompt_version: str
    schema_version: str
    response_type: type[ResponseT]
    deterministic_handler: Callable[[RequestT], TierResult[ResponseT] | None] | None
    local_router_name: str | None
    fallback_handler: Callable[[RequestT], FeatureOutcome[ResponseT]]
    semantic_validator: Callable[[RequestT, ResponseT], ValidationResult]
    build_context: Callable[[RequestT], ContextBuildResult] | None = None
    call_provider: Callable[[RequestT, CompiledContext], ResponseT] | None = None
    max_remote_calls_per_run: int | None = None
    max_frontier_calls_per_run: int | None = None
    # Sec 8.1.7's released-model-result cache. All four are required together
    # to opt a feature into caching; a feature that supplies none of them
    # simply never checks/writes the cache (existing specs stay valid
    # without change). validator_version is a plain string, not callable —
    # it identifies which version of semantic_validator produced a result,
    # so a validator upgrade invalidates old cache identities automatically.
    request_hash: Callable[[RequestT], str] | None = None
    deployment_fingerprint: str | None = None
    validator_version: str | None = None
    serialize_response: Callable[[ResponseT], str] | None = None
    deserialize_response: Callable[[str], ResponseT] | None = None
    # Sec 8.1.7's OTHER cache -- deterministic-result, "keyed without model
    # context" (no deployment/tokenizer/schema/validator dimensions, unlike
    # the released-model cache above). Also opt-in and independent: a
    # feature can use one, both, or neither. request_hash/serialize/
    # deserialize are shared with the released-model cache fields above
    # (same request, same response shape) -- only the engine version and
    # the opt-in switch are distinct.
    deterministic_engine_version: str | None = None
    # Sec 8.1.9 batching. Opt-in: a spec that omits either callable cannot
    # be batched (run_batch() falls back to one run() per item). item_id
    # gives every item in a batch a stable identity independent of its
    # position, so partial failure never gets misattributed by index.
    item_id: Callable[[RequestT], str] | None = None
    build_batch_context: Callable[[list[RequestT]], ContextBuildResult] | None = None
    call_provider_batch: Callable[[list[RequestT], CompiledContext], BatchProviderResult[ResponseT]] | None = None
    max_batch_size: int | None = None


class CallCeilingExceeded(Exception):
    """Sec 8.1.5: a remote or frontier call-count ceiling was reached.
    Distinct from ``BudgetExceeded`` (a dollar ceiling) — this is a count,
    and it is checked and incremented BEFORE the provider is ever called,
    so an exhausted ceiling never starts a request (Sec 8.1.5: "It does not
    start a request and object afterward")."""

    def __init__(self, *, kind: str, scope_key: str, ceiling: int) -> None:
        super().__init__(f"{kind} call ceiling ({ceiling}) reached for {scope_key}")
        self.kind = kind
        self.scope_key = scope_key
        self.ceiling = ceiling


_CEILING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS call_ceiling_counters (
    scope_key   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    call_count  INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (scope_key, kind)
);
"""


class CallCeilingLedger:
    """Sec 8.1.5: ``max_remote_calls_per_run`` / ``max_frontier_calls_per_run``
    as separate hard atomic counters scoped by
    ``program_id + run_id + feature + policy_version``. Reuses the same
    SQLite ``BEGIN IMMEDIATE`` pattern RELO-W1.4 proved for
    ``CostGuard.reserve_call`` — a dedicated table so this addition cannot
    regress that already-hardened cost path."""

    def __init__(self, ledger_path: Path) -> None:
        self._ledger_path = ledger_path

    def reserve_call(self, *, scope_key: str, kind: str, ceiling: int | None) -> None:
        """Atomically increment the counter for ``(scope_key, kind)``.
        Raises :class:`CallCeilingExceeded` — without incrementing — if the
        ceiling would be exceeded. ``ceiling=None`` means unbounded."""
        if ceiling is None:
            return
        try:
            with open_program_db(self._ledger_path, durability="strict") as connection:
                connection.executescript(_CEILING_SCHEMA_SQL)
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT call_count FROM call_ceiling_counters WHERE scope_key = ? AND kind = ?",
                    (scope_key, kind),
                ).fetchone()
                current = int(row[0]) if row is not None else 0
                if current >= ceiling:
                    connection.rollback()
                    raise CallCeilingExceeded(kind=kind, scope_key=scope_key, ceiling=ceiling)
                connection.execute(
                    "INSERT INTO call_ceiling_counters (scope_key, kind, call_count, updated_at) VALUES (?, ?, 1, ?) "
                    "ON CONFLICT(scope_key, kind) DO UPDATE SET call_count = call_count + 1, updated_at = excluded.updated_at",
                    (scope_key, kind, datetime.now(timezone.utc).isoformat()),
                )
                connection.commit()
        except sqlite3.DatabaseError as error:
            raise StateError(f"Unable to update call-ceiling ledger at {self._ledger_path}") from error


def call_ceiling_ledger_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    _require_runner_program_id(program_id)
    return programs_root / program_id / "runtime" / "ai_call_ceilings.sqlite3"


def _require_runner_program_id(program_id: str) -> None:
    if not isinstance(program_id, str) or not is_bare_identifier(program_id):
        raise StateError("Runner program_id must be a bare identifier, not a path, wildcard or control value.")


@dataclass(frozen=True, slots=True)
class CacheIdentity:
    """Sec 8.1.7's full released-model-result cache key material. Every
    field is required — an incomplete identity cannot gate reuse of real
    released output. Deliberately not derived from ``ContextManifestV2``
    directly (that type does not exist as a distinct class yet — Sec 9.2
    describes it as fields *added to* the current ``ContextManifest``);
    this identity pulls the fields it needs from the existing manifest plus
    caller-supplied deployment/validator/principal identity instead of
    waiting on that rename."""

    program_id: str
    feature: str
    request_hash: str
    context_hash: str
    prompt_version: str
    policy_version: str
    compiler_policy_version: str
    deployment_fingerprint: str
    tokenizer_id: str
    response_schema_version: str
    validator_version: str
    classification: str
    execution_classification: str
    principal: str
    snapshot_id: str

    def cache_key(self) -> str:
        raw = json.dumps(
            (
                self.program_id,
                self.feature,
                self.request_hash,
                self.context_hash,
                self.prompt_version,
                self.policy_version,
                self.compiler_policy_version,
                self.deployment_fingerprint,
                self.tokenizer_id,
                self.response_schema_version,
                self.validator_version,
                self.classification,
                self.execution_classification,
                self.principal,
                self.snapshot_id,
            ),
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_RELEASED_CACHE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS released_result_cache (
    cache_key             TEXT PRIMARY KEY,
    feature               TEXT NOT NULL,
    principal             TEXT NOT NULL,
    classification        TEXT NOT NULL,
    payload               TEXT NOT NULL,
    released_content_hash TEXT NOT NULL,
    release_run_id        TEXT NOT NULL,
    cached_at             TEXT NOT NULL
);
"""


class ReleasedResultCache:
    """Sec 8.1.7: the released-model-result cache, SQLite-backed and
    content-addressed by :class:`CacheIdentity`. Only ever written after a
    response has passed semantic/grounding validation and release audit —
    "poisoned output never persists" is enforced by the *caller* only ever
    invoking :meth:`put` from that point in the runner's sequence, never
    speculatively. Authorization (principal + classification) is rechecked
    against the identity on every read, not trusted from write time."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def get(
        self,
        identity: CacheIdentity,
        *,
        deserialize: Callable[[str], ResponseT],
    ) -> ResponseT | None:
        try:
            with open_program_db(self._db_path, durability="strict") as connection:
                connection.executescript(_RELEASED_CACHE_SCHEMA_SQL)
                row = connection.execute(
                    "SELECT payload, principal, classification FROM released_result_cache WHERE cache_key = ?",
                    (identity.cache_key(),),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise StateError(f"Unable to read released-result cache at {self._db_path}") from error
        if row is None:
            return None
        payload, principal, classification = row
        # Authorization recheck (Sec 8.1.7): a cache entry written under a
        # different principal or classification is never reused, even
        # though its cache_key already encodes both -- this is a defensive
        # second check independent of key construction.
        if principal != identity.principal or classification != identity.classification:
            return None
        return deserialize(payload)

    def put(
        self,
        identity: CacheIdentity,
        value: ResponseT,
        *,
        serialize: Callable[[ResponseT], str],
        released_content_hash: str,
        release_run_id: str,
    ) -> None:
        try:
            with open_program_db(self._db_path, durability="strict") as connection:
                connection.executescript(_RELEASED_CACHE_SCHEMA_SQL)
                connection.execute(
                    "INSERT INTO released_result_cache "
                    "(cache_key, feature, principal, classification, payload, released_content_hash, release_run_id, cached_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, "
                    "released_content_hash = excluded.released_content_hash, release_run_id = excluded.release_run_id, "
                    "cached_at = excluded.cached_at",
                    (
                        identity.cache_key(),
                        identity.feature,
                        identity.principal,
                        identity.classification,
                        serialize(value),
                        released_content_hash,
                        release_run_id,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                connection.commit()
        except sqlite3.DatabaseError as error:
            raise StateError(f"Unable to write released-result cache at {self._db_path}") from error

    def evict(self, identity: CacheIdentity) -> None:
        """Invalid, revoked, privacy-excised, or obsolete entries must be
        evicted, never replayed (Sec 8.1.7) -- e.g. called by a caller that
        detects post-hoc that a previously-released entry was revoked."""
        try:
            with open_program_db(self._db_path, durability="strict") as connection:
                connection.executescript(_RELEASED_CACHE_SCHEMA_SQL)
                connection.execute("DELETE FROM released_result_cache WHERE cache_key = ?", (identity.cache_key(),))
                connection.commit()
        except sqlite3.DatabaseError as error:
            raise StateError(f"Unable to evict from released-result cache at {self._db_path}") from error


def released_result_cache_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    _require_runner_program_id(program_id)
    return programs_root / program_id / "runtime" / "ai_released_result_cache.sqlite3"


@dataclass(frozen=True, slots=True)
class DeterministicCacheIdentity:
    """Sec 8.1.7's OTHER cache key: "typed request, immutable snapshot
    subset, deterministic engine version, and authorization profile" —
    deliberately narrower than :class:`CacheIdentity` above (no deployment/
    tokenizer/schema/validator dimensions; "keyed without model context")."""

    program_id: str
    feature: str
    request_hash: str
    snapshot_id: str
    deterministic_engine_version: str
    principal: str
    classification: str

    def cache_key(self) -> str:
        raw = json.dumps(
            (
                self.program_id,
                self.feature,
                self.request_hash,
                self.snapshot_id,
                self.deterministic_engine_version,
                self.principal,
                self.classification,
            ),
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_DETERMINISTIC_CACHE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS deterministic_result_cache (
    cache_key      TEXT PRIMARY KEY,
    feature        TEXT NOT NULL,
    principal      TEXT NOT NULL,
    classification TEXT NOT NULL,
    payload        TEXT NOT NULL,
    cached_at      TEXT NOT NULL
);
"""


class DeterministicResultCache:
    """Sec 8.1.7's deterministic-result cache — a separate table/identity
    from :class:`ReleasedResultCache`, never conflated with it (Sec 8.1.2
    step 4 reads this cache before the deterministic handler even runs;
    step 8's model-result cache is a completely separate lookup after
    context compilation). Same authorization-recheck-on-read discipline as
    the released-model cache."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def get(
        self,
        identity: DeterministicCacheIdentity,
        *,
        deserialize: Callable[[str], ResponseT],
    ) -> ResponseT | None:
        try:
            with open_program_db(self._db_path, durability="strict") as connection:
                connection.executescript(_DETERMINISTIC_CACHE_SCHEMA_SQL)
                row = connection.execute(
                    "SELECT payload, principal, classification FROM deterministic_result_cache WHERE cache_key = ?",
                    (identity.cache_key(),),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise StateError(f"Unable to read deterministic-result cache at {self._db_path}") from error
        if row is None:
            return None
        payload, principal, classification = row
        if principal != identity.principal or classification != identity.classification:
            return None
        return deserialize(payload)

    def put(self, identity: DeterministicCacheIdentity, value: ResponseT, *, serialize: Callable[[ResponseT], str]) -> None:
        try:
            with open_program_db(self._db_path, durability="strict") as connection:
                connection.executescript(_DETERMINISTIC_CACHE_SCHEMA_SQL)
                connection.execute(
                    "INSERT INTO deterministic_result_cache (cache_key, feature, principal, classification, payload, cached_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, cached_at = excluded.cached_at",
                    (
                        identity.cache_key(),
                        identity.feature,
                        identity.principal,
                        identity.classification,
                        serialize(value),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                connection.commit()
        except sqlite3.DatabaseError as error:
            raise StateError(f"Unable to write deterministic-result cache at {self._db_path}") from error


def deterministic_result_cache_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    _require_runner_program_id(program_id)
    return programs_root / program_id / "runtime" / "ai_deterministic_result_cache.sqlite3"


@dataclass(frozen=True, slots=True)
class RunnerContext:
    """Per-run identity the runner needs to scope call-count ceilings and
    telemetry. Deliberately does not include snapshot pinning or
    authorization-profile resolution — callers supply an already-resolved
    ``snapshot_id``/``principal`` (see module docstring). ``classification``
    is the run's authorization/privacy classification; it is what
    :class:`DeterministicCacheIdentity` uses for its authorization profile,
    since (unlike :class:`CacheIdentity`) the deterministic cache is
    consulted *before* context compilation ever produces a manifest-derived
    classification value."""

    program_id: str
    run_id: str
    policy_version: str
    snapshot_id: str
    principal: str
    classification: str = "internal"

    def __post_init__(self) -> None:
        for field in ("program_id", "run_id", "policy_version", "snapshot_id", "principal", "classification"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise StateError(f"Runner context requires a nonblank string {field}.")
        _require_runner_program_id(self.program_id)


class StructuredFeatureRunner:
    """The Sec 8.1.2 façade. One instance per run; ``run()`` is called once
    per feature invocation."""

    def __init__(
        self,
        *,
        context: RunnerContext,
        frontier_eligible: bool,
        ceiling_ledger: CallCeilingLedger | None = None,
        released_cache: ReleasedResultCache | None = None,
        deterministic_cache: DeterministicResultCache | None = None,
        local_routers: dict[str, LocalRouter] | None = None,
        decision_sink: Callable[[TierDecision], None] | None = None,
    ) -> None:
        self._context = context
        self._frontier_eligible = frontier_eligible
        self._ceiling_ledger = ceiling_ledger or CallCeilingLedger(
            call_ceiling_ledger_path(context.program_id)
        )
        self._released_cache = released_cache or ReleasedResultCache(
            released_result_cache_path(context.program_id)
        )
        self._deterministic_cache = deterministic_cache or DeterministicResultCache(
            deterministic_result_cache_path(context.program_id)
        )
        self._local_routers = local_routers or {}
        self._decision_sink = decision_sink

    def _deterministic_cache_identity(
        self, spec: StructuredFeatureSpec[RequestT, ResponseT], request: RequestT
    ) -> DeterministicCacheIdentity | None:
        if (
            spec.request_hash is None
            or spec.deterministic_engine_version is None
            or spec.serialize_response is None
            or spec.deserialize_response is None
        ):
            return None
        return DeterministicCacheIdentity(
            program_id=self._context.program_id,
            feature=spec.feature,
            request_hash=spec.request_hash(request),
            snapshot_id=self._context.snapshot_id,
            deterministic_engine_version=spec.deterministic_engine_version,
            principal=self._context.principal,
            classification=self._context.classification,
        )

    def _cache_identity(
        self,
        spec: StructuredFeatureSpec[RequestT, ResponseT],
        request: RequestT,
        context_build: ContextBuildResult,
    ) -> CacheIdentity | None:
        # Caching is opt-in per feature: a spec that supplies none of these
        # simply never checks/writes the cache (Sec 8.1.7's key requires
        # every field; an incomplete identity cannot gate reuse).
        if (
            spec.request_hash is None
            or spec.deployment_fingerprint is None
            or spec.validator_version is None
            or spec.serialize_response is None
            or spec.deserialize_response is None
        ):
            return None
        manifest = context_build.compiled.manifest
        return CacheIdentity(
            program_id=self._context.program_id,
            feature=spec.feature,
            request_hash=spec.request_hash(request),
            context_hash=manifest.context_hash,
            prompt_version=spec.prompt_version,
            policy_version=self._context.policy_version,
            compiler_policy_version=manifest.policy_version,
            deployment_fingerprint=spec.deployment_fingerprint,
            tokenizer_id=manifest.tokenizer_id,
            response_schema_version=spec.schema_version,
            validator_version=spec.validator_version,
            classification=manifest.classification,
            execution_classification=self._context.classification,
            principal=self._context.principal,
            snapshot_id=self._context.snapshot_id,
        )

    def _context_binding_failure(
        self,
        spec: StructuredFeatureSpec[RequestT, ResponseT],
        context_build: ContextBuildResult,
    ) -> FeatureOutcome[ResponseT] | None:
        manifest = context_build.compiled.manifest
        expected = (self._context.program_id, spec.feature, spec.prompt_version)
        actual = (manifest.program_id, manifest.feature, manifest.prompt_version)
        if actual == expected:
            return None
        self._record_decision(spec.feature, Tier.NONE, RouteOutcome.SKIPPED, 0.0)
        return FeatureOutcome(
            status=FeatureOutcomeStatus.BLOCKED,
            value=None,
            tier=Tier.NONE,
            limitations=("Compiled context identity does not match the invocation.",),
            retryable=False,
            next_action="Recompile context for the current program, feature and prompt before retrying.",
        )

    def _pre_context_fast_path(
        self, spec: StructuredFeatureSpec[RequestT, ResponseT], request: RequestT
    ) -> FeatureOutcome[ResponseT] | None:
        """Sec 8.1.2 steps 4-6: the deterministic-result cache, the
        deterministic handler, and the local router -- none need a
        compiled context or a provider, so they run identically whether
        called from ``run()`` or per-item from ``run_batch()`` before any
        batch context is built. Returns ``None`` if none of them resolve
        the request (a genuine cache miss that needs a real dispatch)."""
        # Step 4: read the deterministic-result cache -- "keyed without
        # model context" (Sec 8.1.7), so this check happens before the
        # deterministic handler even runs, using only request/snapshot/
        # engine-version/authorization identity, no compiled-context or
        # model dimensions at all.
        det_cache_identity = self._deterministic_cache_identity(spec, request)
        if det_cache_identity is not None:
            assert spec.deserialize_response is not None
            cached_det_value = self._deterministic_cache.get(det_cache_identity, deserialize=spec.deserialize_response)
            if cached_det_value is not None:
                self._record_decision(spec.feature, Tier.CACHE, RouteOutcome.CACHE_HIT, 1.0)
                return FeatureOutcome(status=FeatureOutcomeStatus.RELEASED, value=cached_det_value, tier=Tier.CACHE)

        # Step 5: deterministic handler.
        if spec.deterministic_handler is not None:
            det_result = spec.deterministic_handler(request)
            if det_result is not None:
                if det_cache_identity is not None:
                    assert spec.serialize_response is not None
                    self._deterministic_cache.put(det_cache_identity, det_result.value, serialize=spec.serialize_response)
                self._record_decision(spec.feature, Tier.DETERMINISTIC, RouteOutcome.DETERMINISTIC_HIT, det_result.confidence)
                return FeatureOutcome(status=FeatureOutcomeStatus.RELEASED, value=det_result.value, tier=Tier.DETERMINISTIC)

        # Step 6: registered certified local handler.
        if spec.local_router_name is not None:
            router = self._local_routers.get(spec.local_router_name)
            if router is not None:
                local_result = router.route(request)
                if local_result is not None:
                    self._record_decision(spec.feature, Tier.LOCAL, RouteOutcome.LOCAL_HIT, local_result.confidence)
                    return FeatureOutcome(status=FeatureOutcomeStatus.RELEASED, value=local_result.value, tier=Tier.LOCAL)

        return None

    def run(self, spec: StructuredFeatureSpec[RequestT, ResponseT], request: RequestT) -> FeatureOutcome[ResponseT]:
        scope_key = f"{self._context.program_id}:{self._context.run_id}:{spec.feature}:{self._context.policy_version}"

        # Steps 4-6: deterministic cache, deterministic handler, local
        # router -- none need a compiled context, so run_batch() reuses
        # this exact logic per item before ever building a batch context.
        fast_outcome = self._pre_context_fast_path(spec, request)
        if fast_outcome is not None:
            return fast_outcome

        if not self._frontier_eligible or spec.call_provider is None or spec.build_context is None:
            self._record_decision(spec.feature, Tier.NONE, RouteOutcome.FRONTIER_BLOCKED, 0.0)
            return self._fallback(spec, request, reason="frontier_ineligible_or_unconfigured")

        # Step 7: compile bounded context (existing DeterministicContextCompiler).
        try:
            context_build = spec.build_context(request)
        except ContextCompileRejected as error:
            self._record_decision(spec.feature, Tier.NONE, RouteOutcome.SKIPPED, 0.0)
            return FeatureOutcome(
                status=FeatureOutcomeStatus.EVIDENCE_GAP,
                value=None,
                tier=Tier.NONE,
                limitations=(str(error),),
                retryable=False,
                next_action="Reduce required evidence or raise max_input_tokens for this feature.",
            )

        binding_failure = self._context_binding_failure(spec, context_build)
        if binding_failure is not None:
            return binding_failure

        # Step 8: construct the model-result cache key from the (now-built)
        # compiled context's manifest and load only a previously released
        # typed envelope. A cache hit skips the call-ceiling check and the
        # provider entirely -- it is not a call, remote or otherwise.
        cache_identity = self._cache_identity(spec, request, context_build)
        if cache_identity is not None:
            assert spec.deserialize_response is not None
            cached_value = self._released_cache.get(cache_identity, deserialize=spec.deserialize_response)
            if cached_value is not None:
                self._record_decision(spec.feature, Tier.CACHE, RouteOutcome.CACHE_HIT, 1.0)
                return FeatureOutcome(status=FeatureOutcomeStatus.RELEASED, value=cached_value, tier=Tier.CACHE)

        # Step 9/10: remote/frontier call-count ceiling — checked and
        # incremented BEFORE any provider call is attempted.
        try:
            self._ceiling_ledger.reserve_call(scope_key=scope_key, kind="remote", ceiling=spec.max_remote_calls_per_run)
            self._ceiling_ledger.reserve_call(scope_key=scope_key, kind="frontier", ceiling=spec.max_frontier_calls_per_run)
        except CallCeilingExceeded:
            self._record_decision(spec.feature, Tier.NONE, RouteOutcome.SKIPPED, 0.0)
            return self._fallback(spec, request, reason="remote_cap_exhausted")

        # Step 11: call the provider. AIClient.structured() (or equivalent)
        # already reserves/settles its own cost atomically — see module
        # docstring; this runner does not wrap that in a second reservation.
        try:
            response = spec.call_provider(request, context_build.compiled)
        except BudgetExceeded:
            # Sec 8.1.6: non-retryable, like a policy block or cap exhaustion.
            self._record_decision(spec.feature, Tier.FRONTIER, RouteOutcome.FRONTIER_BLOCKED, 0.0)
            return self._fallback(spec, request, reason="budget_exceeded")
        except Exception:
            # Uncertain provider outcome (timeout/unknown failure): AIClient's
            # own reservation is retained internally per Sec 8.1.6 — this
            # runner must not attempt to release or re-settle it.
            self._record_decision(spec.feature, Tier.FRONTIER, RouteOutcome.FRONTIER_CALL, 0.0)
            return FeatureOutcome(
                status=FeatureOutcomeStatus.UNAVAILABLE,
                value=None,
                tier=Tier.FRONTIER,
                retryable=True,
                next_action="Provider call failed or timed out; reservation retained pending reconciliation.",
            )

        # Step 13: semantic/grounding validation.
        validation = spec.semantic_validator(request, response)
        if not validation.valid:
            self._record_decision(spec.feature, Tier.FRONTIER, RouteOutcome.FRONTIER_CALL, 0.0)
            return FeatureOutcome(
                status=FeatureOutcomeStatus.BLOCKED,
                value=None,
                tier=Tier.FRONTIER,
                limitations=validation.limitations + ((validation.reason,) if validation.reason else ()),
                evidence_refs=validation.evidence_refs,
                retryable=False,
                next_action="Validation failed a release gate; not released.",
            )

        # Step 15: cache only the released typed envelope -- this is the
        # only place `put` is ever called, so an invalid/blocked response
        # (returned above) can never be cached ("poisoned output never
        # persists", Sec 8.1.7).
        if cache_identity is not None:
            assert spec.serialize_response is not None
            content_hash = hashlib.sha256(spec.serialize_response(response).encode("utf-8")).hexdigest()
            self._released_cache.put(
                cache_identity,
                response,
                serialize=spec.serialize_response,
                released_content_hash=content_hash,
                release_run_id=self._context.run_id,
            )

        self._record_decision(spec.feature, Tier.FRONTIER, RouteOutcome.FRONTIER_CALL, 1.0)
        return FeatureOutcome(
            status=FeatureOutcomeStatus.RELEASED,
            value=response,
            tier=Tier.FRONTIER,
            evidence_refs=validation.evidence_refs,
        )

    def run_batch(
        self, spec: StructuredFeatureSpec[RequestT, ResponseT], requests: list[RequestT]
    ) -> dict[str, FeatureOutcome[ResponseT]]:
        """Sec 8.1.9: batch only cache misses; every item keeps its own
        identity and its own outcome, including partial failure. A spec
        not configured for batching (missing ``item_id``/
        ``build_batch_context``/``call_provider_batch``) falls back to one
        ``run()`` per item, keyed by ``spec.request_hash`` if available or
        the item's index otherwise.

        Only the *deterministic*-result cache is consulted per item before
        batching (Sec 8.1.7's "keyed without model context" cache) — the
        released-model cache needs a per-item ``context_hash`` that a
        single shared batch context does not produce, so it is
        deliberately out of scope for this method; released-model caching
        for batched items remains a ``run()``-per-item concern until a
        batch-aware context/cache-key design is reviewed separately.
        """
        if spec.item_id is None or spec.build_batch_context is None or spec.call_provider_batch is None:
            return {
                (spec.request_hash(request) if spec.request_hash is not None else str(index)): self.run(spec, request)
                for index, request in enumerate(requests)
            }

        results: dict[str, FeatureOutcome[ResponseT]] = {}
        needs_call: list[tuple[str, RequestT]] = []
        for request in requests:
            item_id = spec.item_id(request)
            fast_outcome = self._pre_context_fast_path(spec, request)
            if fast_outcome is not None:
                results[item_id] = fast_outcome
            else:
                needs_call.append((item_id, request))

        if not needs_call:
            return results

        chunk_size = spec.max_batch_size or len(needs_call)
        for start in range(0, len(needs_call), chunk_size):
            chunk = needs_call[start : start + chunk_size]
            results.update(self._run_batch_chunk(spec, chunk))
        return results

    def _run_batch_chunk(
        self, spec: StructuredFeatureSpec[RequestT, ResponseT], chunk: list[tuple[str, RequestT]]
    ) -> dict[str, FeatureOutcome[ResponseT]]:
        if not self._frontier_eligible:
            self._record_decision(spec.feature, Tier.NONE, RouteOutcome.FRONTIER_BLOCKED, 0.0)
            return {
                item_id: self._fallback(spec, request, reason="frontier_ineligible_or_unconfigured")
                for item_id, request in chunk
            }
        scope_key = f"{self._context.program_id}:{self._context.run_id}:{spec.feature}:{self._context.policy_version}"
        chunk_requests = [request for _item_id, request in chunk]

        # No batch may exceed the same compiler/schema budgets a single
        # call would (Sec 8.1.9): ONE compiled context for the whole
        # chunk, not one per item.
        assert spec.build_batch_context is not None
        try:
            context_build = spec.build_batch_context(chunk_requests)
        except ContextCompileRejected as error:
            self._record_decision(spec.feature, Tier.NONE, RouteOutcome.SKIPPED, 0.0)
            outcome: FeatureOutcome[ResponseT] = FeatureOutcome(
                status=FeatureOutcomeStatus.EVIDENCE_GAP,
                value=None,
                tier=Tier.NONE,
                limitations=(str(error),),
                retryable=False,
                next_action="Reduce required evidence, shrink the batch, or raise max_input_tokens for this feature.",
            )
            return {item_id: outcome for item_id, _request in chunk}

        binding_failure = self._context_binding_failure(spec, context_build)
        if binding_failure is not None:
            return {item_id: binding_failure for item_id, _ in chunk}

        # ONE call-count reservation for the whole chunk (Sec 8.1.9: "the
        # same... cost... budgets" as a single call, not N).
        try:
            self._ceiling_ledger.reserve_call(scope_key=scope_key, kind="remote", ceiling=spec.max_remote_calls_per_run)
            self._ceiling_ledger.reserve_call(scope_key=scope_key, kind="frontier", ceiling=spec.max_frontier_calls_per_run)
        except CallCeilingExceeded:
            self._record_decision(spec.feature, Tier.NONE, RouteOutcome.SKIPPED, 0.0)
            return {item_id: self._fallback(spec, request, reason="remote_cap_exhausted") for item_id, request in chunk}

        assert spec.call_provider_batch is not None
        try:
            batch_result = spec.call_provider_batch(chunk_requests, context_build.compiled)
        except BudgetExceeded:
            self._record_decision(spec.feature, Tier.FRONTIER, RouteOutcome.FRONTIER_BLOCKED, 0.0)
            return {item_id: self._fallback(spec, request, reason="budget_exceeded") for item_id, request in chunk}
        except Exception:
            self._record_decision(spec.feature, Tier.FRONTIER, RouteOutcome.FRONTIER_CALL, 0.0)
            unavailable: FeatureOutcome[ResponseT] = FeatureOutcome(
                status=FeatureOutcomeStatus.UNAVAILABLE,
                value=None,
                tier=Tier.FRONTIER,
                retryable=True,
                next_action="Batched provider call failed or timed out; reservation retained pending reconciliation.",
            )
            return {item_id: unavailable for item_id, _request in chunk}

        results: dict[str, FeatureOutcome[ResponseT]] = {}
        for item_id, request in chunk:
            if item_id in batch_result.failed_item_ids or item_id not in batch_result.responses:
                # Partial failure, per item (Sec 8.1.9) -- never silently
                # dropped and never conflated with the other items' outcomes.
                self._record_decision(spec.feature, Tier.FRONTIER, RouteOutcome.FRONTIER_CALL, 0.0)
                results[item_id] = FeatureOutcome(
                    status=FeatureOutcomeStatus.UNAVAILABLE,
                    value=None,
                    tier=Tier.FRONTIER,
                    retryable=True,
                    next_action="This item was not returned by the batched provider call; other items in the batch may have succeeded.",
                )
                continue

            response = batch_result.responses[item_id]
            validation = spec.semantic_validator(request, response)
            if not validation.valid:
                self._record_decision(spec.feature, Tier.FRONTIER, RouteOutcome.FRONTIER_CALL, 0.0)
                results[item_id] = FeatureOutcome(
                    status=FeatureOutcomeStatus.BLOCKED,
                    value=None,
                    tier=Tier.FRONTIER,
                    limitations=validation.limitations + ((validation.reason,) if validation.reason else ()),
                    evidence_refs=validation.evidence_refs,
                    retryable=False,
                    next_action="Validation failed a release gate; not released.",
                )
                continue

            # Note: neither cache is written here. The deterministic-result
            # cache is for the deterministic handler's own output only (a
            # frontier-sourced response does not belong there); the
            # released-model cache needs a per-item context_hash a shared
            # batch context does not produce (see run_batch's docstring).
            # Batch-aware released-model caching is a follow-up, not a
            # silent gap papered over here.
            self._record_decision(spec.feature, Tier.FRONTIER, RouteOutcome.FRONTIER_CALL, 1.0)
            results[item_id] = FeatureOutcome(
                status=FeatureOutcomeStatus.RELEASED,
                value=response,
                tier=Tier.FRONTIER,
                evidence_refs=validation.evidence_refs,
            )
        return results

    def _fallback(self, spec: StructuredFeatureSpec[RequestT, ResponseT], request: RequestT, *, reason: str) -> FeatureOutcome[ResponseT]:
        outcome = spec.fallback_handler(request)
        if outcome.status == FeatureOutcomeStatus.RELEASED:
            return FeatureOutcome(
                status=FeatureOutcomeStatus.DETERMINISTIC_FALLBACK,
                value=outcome.value,
                tier=outcome.tier,
                limitations=outcome.limitations + (reason,),
                evidence_refs=outcome.evidence_refs,
            )
        return outcome

    def _record_decision(self, feature: str, tier: Tier, outcome: RouteOutcome, confidence: float) -> None:
        decision = TierDecision(
            feature=feature,
            tier=tier,
            outcome=outcome,
            confidence=confidence,
            frontier_called=(tier == Tier.FRONTIER),
            trace_id=self._context.run_id,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
        if self._decision_sink is not None:
            self._decision_sink(decision)


__all__ = [
    "BatchProviderResult",
    "CacheIdentity",
    "CallCeilingExceeded",
    "CallCeilingLedger",
    "ContextBuildResult",
    "DeterministicCacheIdentity",
    "DeterministicResultCache",
    "FeatureOutcome",
    "FeatureOutcomeStatus",
    "LocalRouter",
    "ReleasedResultCache",
    "RunnerContext",
    "StructuredFeatureRunner",
    "StructuredFeatureSpec",
    "ValidationResult",
    "call_ceiling_ledger_path",
    "deterministic_result_cache_path",
    "released_result_cache_path",
]

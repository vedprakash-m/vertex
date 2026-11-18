from __future__ import annotations

# Adapted from Shiproom src/ai/client.py

import json
import logging
import math
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable, Sequence, TypeVar

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.context_tokenizers import resolve_token_estimator
from src.ai.llm_trace import AITraceContext, get_current_trace_context, llm_trace
from src.ai.request_router import AIRequestRouter
from src.core.exceptions import ConfigError, StateError
from src.core.policy_loader import _load_ai_policy_document, load_ai_model_cost_policy


logger = logging.getLogger(__name__)
StructuredResponse = TypeVar("StructuredResponse")
ResultT = TypeVar("ResultT")


class AIClientError(Exception):
    """Raised when the Azure OpenAI client cannot complete a request."""


class BudgetExceeded(AIClientError):
    """Raised when an AI feature or run budget is exceeded."""


class BudgetConfigurationError(AIClientError):
    """Raised when an AI budget or model pricing configuration is invalid."""


class BudgetStateError(AIClientError, StateError):
    """Raised when the retained AI cost state cannot be safely used."""


class AIPreflightError(AIClientError):
    """Raised when local request preflight cannot safely admit another attempt."""


NON_RETRYABLE_AI_ERRORS = (BudgetExceeded, BudgetConfigurationError, BudgetStateError, AIPreflightError)


@dataclass(slots=True)
class AIUsageStats:
    """Tracks cumulative token usage across all AI calls in a run."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    call_count: int = 0

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens += prompt_tokens + completion_tokens
        self.call_count += 1


class _FeatureBudgetState:
    def __init__(self) -> None:
        self.lock = Lock()
        self.spent_usd = 0.0
        self.reservations: dict[tuple[object, int], float] = {}


class _CostChain:
    """Priced retry/fallback chain; its worst case is held with the first attempt's reservation."""

    __slots__ = ("guard", "prices", "attempts", "reservation")

    def __init__(self, guard: Any, prices: dict[str, tuple[float, float]], attempts: int) -> None:
        self.guard = guard
        self.prices = prices
        self.attempts = attempts
        self.reservation: Any = None

    def covers(self, client: AIClient) -> bool:
        guard = client._run_cost_guard
        return (
            guard is not None
            and client.deployment in self.prices
            and _guard_identity(guard) == _guard_identity(self.guard)
        )

    def worst_case_usd(self, prompt_tokens: int, max_tokens: int) -> float:
        """Sum over deployments x attempts of (input tokens x input price + max_tokens x output price)."""
        try:
            total_usd = self.attempts * math.fsum(
                (prompt_tokens / 1000) * input_per_1k + (max_tokens / 1000) * output_per_1k
                for input_per_1k, output_per_1k in self.prices.values()
            )
        except OverflowError:
            total_usd = math.inf
        if not math.isfinite(total_usd) or total_usd < 0:
            raise BudgetConfigurationError("AI token reservation cost must be finite and nonnegative.")
        return total_usd


_ACTIVE_COST_CHAIN: ContextVar[_CostChain | None] = ContextVar("vertex_ai_cost_chain", default=None)


def _guard_identity(guard: Any) -> tuple[str, str, Any]:
    return (guard._edition, guard._run_id, guard.ledger_path.resolve())


def open_cost_chain(primary: Any, deployments: Sequence[str]) -> _CostChain | None:
    """Price a retry/fallback chain before any dispatch (RELO-W1.4).

    Raises before any ledger write or dispatch when the feature ID or any
    deployment's price is unknown.  The first attempt's reservation then holds
    the worst case for every deployment x (MAX_RETRIES + 1) attempts against
    the run ceiling; later attempts draw from that hold.  Backup deployments
    are priced without constructing their clients.
    """
    if not isinstance(primary, AIClient) or primary._run_cost_guard is None:
        return None
    assert primary._trace_context is not None
    feature = primary._trace_context.metadata.get("ai_feature")
    if feature is not None:
        try:
            known_features = _load_ai_policy_document().get("ai_features") or {}
        except ConfigError:
            raise BudgetConfigurationError("AI feature policy configuration is invalid.") from None
        if str(feature).strip() not in known_features:
            raise BudgetConfigurationError(f"Unknown AI feature {feature!r}; provider call refused.")
    prices: dict[str, tuple[float, float]] = {}
    for deployment in dict.fromkeys(deployments):
        if deployment == primary.deployment:
            prices[deployment] = (primary._input_cost_per_1k_tokens, primary._output_cost_per_1k_tokens)
            continue
        try:
            pricing = load_ai_model_cost_policy(deployment)
        except ConfigError:
            raise BudgetConfigurationError("AI model pricing configuration is invalid.") from None
        prices[deployment] = (pricing.input_cost_per_1k_tokens, pricing.output_cost_per_1k_tokens)
    return _CostChain(primary._run_cost_guard, prices, primary.MAX_RETRIES + 1)


def close_cost_chain(chain: _CostChain | None, *, refresh_projection: bool = True) -> None:
    """Release the undrawn remainder; drawn attempt rows keep their own settle/retain outcome."""
    if chain is None or chain.reservation is None:
        return
    try:
        chain.guard.release_chain(chain.reservation, refresh_projection=refresh_projection)
    except Exception as error:
        raise BudgetStateError(
            f"Unable to release AI cost chain remainder ({type(error).__name__}); reconcile before retrying."
        ) from None


def run_in_cost_chain(chain: _CostChain | None, call: Callable[[], ResultT]) -> ResultT:
    """Run ``call`` with ``chain`` active, then release its undrawn remainder."""
    token = _ACTIVE_COST_CHAIN.set(chain)
    try:
        result = call()
    except BaseException as error:
        _ACTIVE_COST_CHAIN.reset(token)
        try:
            # After a cost-state failure the projection is left as the failed attempt left it.
            close_cost_chain(chain, refresh_projection=not isinstance(error, BudgetStateError))
        except BudgetStateError:
            logger.error("AI cost chain remainder could not be released after a failed request.")
        raise
    _ACTIVE_COST_CHAIN.reset(token)
    close_cost_chain(chain)
    return result


class AIClient:
    """Thin Azure OpenAI wrapper with guarded imports, retry, and per-run budget tracking."""

    DEFAULT_API_VERSION = "2024-02-01"
    TIMEOUT_SECONDS = 20
    MAX_RETRIES = 2

    def __init__(
        self,
        deployment: str,
        temperature: float,
        budget_usd: float,
        *,
        endpoint: str | None = None,
        api_key: str | None = None,
        api_version: str | None = None,
        requests_per_minute: int | None = None,
        sleep_func: Any = time.sleep,
        time_func: Callable[[], float] = time.monotonic,
        trace_context: AITraceContext | None = None,
        _feature_budget_state: _FeatureBudgetState | None = None,
    ) -> None:
        if not deployment.strip():
            raise RuntimeError("Azure OpenAI deployment name is required.")
        budget_usd = _validate_budget_usd(budget_usd, label="Feature")
        try:
            pricing = load_ai_model_cost_policy(deployment)
        except ConfigError:
            raise BudgetConfigurationError("AI model pricing configuration is invalid.") from None

        self._deployment = deployment
        self._temperature = temperature
        self._budget_usd = budget_usd
        self._input_cost_per_1k_tokens = pricing.input_cost_per_1k_tokens
        self._output_cost_per_1k_tokens = pricing.output_cost_per_1k_tokens
        self._spent_usd = 0.0
        self._feature_budget_state = _feature_budget_state or _FeatureBudgetState()
        self._reservation_namespace = object()
        self._usage_stats = AIUsageStats()
        self._sleep = sleep_func
        self._time = time_func
        # D-20: an explicit trace_context wins; otherwise fall back to the
        # process trace context bound via use_trace_context(...).
        resolved_trace_context = trace_context if trace_context is not None else get_current_trace_context()
        self._trace_context = resolved_trace_context
        self._rate_limit_scope = _resolve_rate_limit_scope(trace_context=resolved_trace_context, deployment=deployment)
        self._run_cost_guard = self._build_run_cost_guard(resolved_trace_context, budget_usd)
        self._router = AIRequestRouter(
            endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            requests_per_minute=requests_per_minute,
            sleep_func=sleep_func,
            time_func=time_func,
            rate_limit_scope=self._rate_limit_scope,
        )

    @property
    def deployment(self) -> str:
        return self._deployment

    @property
    def spent_usd(self) -> float:
        return self._spent_usd

    @property
    def usage_stats(self) -> AIUsageStats:
        return self._usage_stats

    def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> str:
        response = self._request_completion(
            system,
            user,
            max_tokens=max_tokens,
            prompt_version=prompt_version,
            mode="chat",
        )
        return self._extract_text(response)

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], StructuredResponse],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> StructuredResponse:
        response = self._request_completion(
            system,
            user,
            max_tokens=max_tokens,
            prompt_version=prompt_version,
            mode="structured",
            response_format={"type": "json_object"},
        )
        return parser(self._extract_json_object(response))

    def _reserve_budget(self, *, system: str, user: str, max_tokens: int) -> Any:
        with self._feature_budget_state.lock:
            if self._spent_usd >= self._budget_usd:
                raise BudgetExceeded(f"Spent ${self._spent_usd:.3f} of ${self._budget_usd:.2f}")
            if self._feature_budget_state.spent_usd >= self._budget_usd:
                raise BudgetExceeded(
                    f"Spent ${self._feature_budget_state.spent_usd:.3f} of "
                    f"${self._budget_usd:.2f}; the feature ceiling is exhausted."
                )
            reservation = None
            if self._run_cost_guard is not None:
                max_tokens = _validate_reservation_tokens(max_tokens)
                estimated_prompt_tokens = _validate_reservation_tokens(
                    resolve_token_estimator(self._deployment).estimate(f"{system}\n{user}")
                )
                try:
                    estimated_cost_usd = self._estimate_cost(estimated_prompt_tokens, max_tokens)
                except OverflowError:
                    raise BudgetConfigurationError("AI token reservation cost must be finite and nonnegative.") from None
                if not math.isfinite(estimated_cost_usd) or estimated_cost_usd < 0:
                    raise BudgetConfigurationError("AI token reservation cost must be finite and nonnegative.")
                committed_usd = self._feature_budget_state.spent_usd + sum(
                    self._feature_budget_state.reservations.values()
                )
                if committed_usd + estimated_cost_usd > self._budget_usd:
                    raise BudgetExceeded(
                        f"Spent/reserved ${committed_usd:.3f} of ${self._budget_usd:.2f}; "
                        f"next AI spend ${estimated_cost_usd:.3f} would exceed the feature ceiling."
                    )
                chain = _ACTIVE_COST_CHAIN.get()
                try:
                    if chain is not None and chain.covers(self) and chain.reservation is not None:
                        reservation = self._run_cost_guard.reserve_call(estimated_cost_usd, chain=chain.reservation)
                    elif chain is not None and chain.covers(self):
                        reservation = self._run_cost_guard.reserve_call(
                            estimated_cost_usd,
                            chain_total_usd=chain.worst_case_usd(estimated_prompt_tokens, max_tokens),
                        )
                        chain.reservation = reservation
                    else:
                        reservation = self._run_cost_guard.reserve_call(estimated_cost_usd)
                except (StateError, OSError, UnicodeError) as error:
                    message = str(error) if isinstance(error, StateError) else "Unable to access AI cost state."
                    raise BudgetStateError(message) from None
                self._feature_budget_state.reservations[
                    (self._reservation_namespace, reservation.reservation_id)
                ] = estimated_cost_usd
            return reservation

    def _record_usage(self, response: Any, *, reservation: Any) -> tuple[int, int, float, BudgetExceeded | None]:
        prompt_tokens, completion_tokens, cost_usd = self._validated_response_usage(response)
        with self._feature_budget_state.lock:
            total_spent_usd = self._spent_usd + cost_usd
            total_feature_spent_usd = self._feature_budget_state.spent_usd + cost_usd
            if not math.isfinite(total_spent_usd) or not math.isfinite(total_feature_spent_usd):
                raise BudgetStateError("AI provider usage cannot be accounted for; reconcile before retrying.")
            self._usage_stats.add(prompt_tokens, completion_tokens)
            self._spent_usd = total_spent_usd
            self._feature_budget_state.spent_usd = total_feature_spent_usd
            budget_error: BudgetExceeded | None = None
            if self._run_cost_guard is not None and reservation is not None:
                try:
                    run_state = self._run_cost_guard.settle_call(reservation, cost_usd)
                except (StateError, OSError, UnicodeError) as error:
                    message = str(error) if isinstance(error, StateError) else "Unable to access AI cost state."
                    raise BudgetStateError(message) from None
                # Only successful settlement removes the local estimate.
                self._feature_budget_state.reservations.pop(
                    (self._reservation_namespace, reservation.reservation_id)
                )
                run_budget_usd = self._run_cost_guard.effective_budget_usd(run_state)
                if run_state.spent_usd > run_budget_usd:
                    budget_error = BudgetExceeded(
                        f"Spent ${run_state.spent_usd:.3f} of ${run_budget_usd:.2f}; actual AI spend exceeded the run ceiling."
                    )
            if budget_error is None and total_feature_spent_usd > self._budget_usd:
                budget_error = BudgetExceeded(
                    f"Spent ${total_feature_spent_usd:.3f} of ${self._budget_usd:.2f}; "
                    "actual AI spend exceeded the feature ceiling."
                )
            return prompt_tokens, completion_tokens, cost_usd, budget_error

    def _validated_response_usage(self, response: Any) -> tuple[int, int, float]:
        usage = getattr(response, "usage", None)
        prompt_tokens = _validate_usage_tokens(getattr(usage, "prompt_tokens", None))
        completion_tokens = _validate_usage_tokens(getattr(usage, "completion_tokens", None))
        try:
            cost_usd = self._estimate_cost(prompt_tokens, completion_tokens)
        except OverflowError:
            raise BudgetStateError("AI provider usage cannot be accounted for; reconcile before retrying.") from None
        if not math.isfinite(cost_usd) or cost_usd < 0:
            raise BudgetStateError("AI provider usage cannot be accounted for; reconcile before retrying.")
        return prompt_tokens, completion_tokens, cost_usd

    def _estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            (prompt_tokens / 1000) * self._input_cost_per_1k_tokens
            + (completion_tokens / 1000) * self._output_cost_per_1k_tokens
        )

    def _request_completion(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        prompt_version: str | None,
        mode: str,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        if self._trace_context is None:
            raise AIClientError(
                "A trace context is required for every remote AI provider call; "
                "untraced calls cannot be budgeted or attributed."
            )
        if get_ai_mode() == AIMode.OBSERVE_ONLY:
            message = "AI request router is in observe-only mode; frontier calls are blocked."
            self._write_trace(
                prompt_version=prompt_version,
                latency_ms=0.0,
                error=message,
            )
            raise AIClientError(message)
        def reserve_attempt() -> tuple[Any, int | None]:
            try:
                attempt_reservation = self._reserve_budget(system=system, user=user, max_tokens=max_tokens)
                attempt_id = attempt_reservation.reservation_id if attempt_reservation is not None else None
                return attempt_reservation, attempt_id
            except (BudgetExceeded, BudgetConfigurationError, BudgetStateError):
                raise
            except Exception as error:
                raise AIPreflightError(
                    f"AI budget reservation failed ({type(error).__name__}); "
                    "reconcile retained reservations before retrying."
                ) from None

        reservation, cost_reservation_id = reserve_attempt()
        started = time.perf_counter()
        response: Any = None
        preflight_failure = False

        def reserve_retry() -> None:
            nonlocal reservation, cost_reservation_id, preflight_failure
            preflight_failure = True
            try:
                try:
                    self._write_trace(
                        prompt_version=prompt_version,
                        latency_ms=None,
                        error="AI provider attempt failed before retry.",
                        cost_reservation_id=cost_reservation_id,
                    )
                except (OSError, StateError, UnicodeError):
                    logger.error("AI trace failed while reporting a retryable provider failure.")
                reservation, cost_reservation_id = reserve_attempt()
            except (BudgetExceeded, BudgetConfigurationError, BudgetStateError, AIPreflightError):
                raise
            except Exception as error:
                raise AIPreflightError(
                    f"AI retry preflight failed ({type(error).__name__}); "
                    "reconcile retained reservations before retrying."
                ) from None
            preflight_failure = False

        def reject_before_dispatch(error: Exception) -> None:
            nonlocal preflight_failure
            preflight_failure = True
            if self._run_cost_guard is not None and reservation is not None:
                with self._feature_budget_state.lock:
                    try:
                        self._run_cost_guard._release_undispatched_call(reservation)
                        self._feature_budget_state.reservations.pop(
                            (self._reservation_namespace, reservation.reservation_id)
                        )
                    except Exception as release_error:
                        raise BudgetStateError(
                            f"Unable to release undispatched AI cost reservation ({type(release_error).__name__}); "
                            "reconcile before retrying."
                        ) from None
            raise AIPreflightError(
                f"AI local request preflight failed ({type(error).__name__}); the current attempt was not dispatched."
            ) from None

        try:
            response = self._router.route(
                deployment=self._deployment,
                system=system,
                user=user,
                temperature=self._temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                before_retry=reserve_retry,
                on_pre_dispatch_failure=reject_before_dispatch,
            )
            prompt_tokens, completion_tokens, cost_usd, budget_error = self._record_usage(
                response,
                reservation=reservation,
            )
            try:
                self._write_trace(
                    prompt_version=prompt_version,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    cost_usd=cost_usd,
                    error=str(budget_error) if budget_error is not None else None,
                    budget_exceeded=budget_error is not None,
                    cost_reservation_id=cost_reservation_id,
                )
            except (OSError, StateError, UnicodeError):
                if budget_error is None:
                    raise
                logger.error("AI trace failed while reporting a budget failure.")
            if budget_error is not None:
                raise budget_error
            logger.info(
                "AI %s completed for deployment=%s prompt_version=%s total_tokens=%s spent_usd=%.6f",
                mode,
                self._deployment,
                prompt_version or "unknown",
                self._usage_stats.total_tokens,
                self._spent_usd,
            )
            return response
        except (BudgetExceeded, BudgetConfigurationError):
            raise
        except BudgetStateError:
            if preflight_failure:
                raise
            trace_prompt_tokens: int | None = None
            trace_completion_tokens: int | None = None
            trace_cost_usd: float | None = None
            if response is not None:
                try:
                    trace_prompt_tokens, trace_completion_tokens, trace_cost_usd = self._validated_response_usage(response)
                except BudgetStateError:
                    pass
            try:
                self._write_trace(
                    prompt_version=prompt_version,
                    prompt_tokens=trace_prompt_tokens,
                    completion_tokens=trace_completion_tokens,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    cost_usd=trace_cost_usd,
                    error="Unable to settle AI cost state; reconcile before retrying.",
                    cost_reservation_id=cost_reservation_id,
                )
            except (OSError, StateError, UnicodeError):
                logger.error("AI trace failed while reporting a cost-state failure.")
            raise
        except Exception as error:  # pragma: no cover - exercised through tests with fakes
            if preflight_failure:
                raise
            wrapped_error = AIClientError(
                f"Azure OpenAI {mode} failed for deployment {self._deployment}: {error}"
            )
            self._write_trace(
                prompt_version=prompt_version,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=str(wrapped_error),
                cost_reservation_id=cost_reservation_id,
            )
            raise wrapped_error from error

    def _write_trace(
        self,
        *,
        prompt_version: str | None,
        latency_ms: float | None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_usd: float | None = None,
        error: str | None = None,
        budget_exceeded: bool = False,
        cost_reservation_id: int | None = None,
    ) -> None:
        if self._trace_context is None:
            return

        llm_trace(
            edition=self._trace_context.edition,
            run_id=self._trace_context.run_id,
            caller=self._trace_context.caller,
            model=self._deployment,
            deployment=self._deployment,
            prompt_version=prompt_version,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            error=error,
            cost_reservation_id=cost_reservation_id,
            metadata=self._trace_context.metadata,
            trace_file=self._trace_context.trace_file,
        )
        # ADF-W0.7: additionally emit the canonical per-program AI telemetry
        # record (INV-ADF-5: missing AI telemetry after a provider invocation
        # is a failure). ``feature`` is carried in the trace-context metadata
        # by call sites; the caller name is the fallback identifier.
        self._emit_ai_telemetry(
            prompt_version=prompt_version,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            error=error,
            budget_exceeded=budget_exceeded,
        )

    def _emit_ai_telemetry(
        self,
        *,
        prompt_version: str | None,
        latency_ms: float | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cost_usd: float | None,
        error: str | None,
        budget_exceeded: bool,
    ) -> None:
        """Write one canonical :mod:`src.core.ai_telemetry` row per provider call.

        Telemetry is best-effort-durable: a write failure must never mask the
        underlying result/error, so exceptions here are logged and swallowed.
        """
        from src.core.ai_telemetry import AiTelemetryRecord, AiTelemetryStatus, append_ai_telemetry

        metadata = self._trace_context.metadata if self._trace_context else {}
        feature = str(metadata.get("feature") or self._trace_context.caller if self._trace_context else "unknown")
        program_id = str(metadata.get("program_id") or self._trace_context.edition if self._trace_context else "unknown")
        if budget_exceeded:
            status = AiTelemetryStatus.BUDGET_EXCEEDED
        elif error is not None and "disabled" in error.lower():
            status = AiTelemetryStatus.FALLBACK
        elif error is not None:
            status = AiTelemetryStatus.OTHER
        else:
            status = AiTelemetryStatus.OK
        record = AiTelemetryRecord(
            ts=datetime.now(timezone.utc),
            feature=feature,
            deployment_id=self._deployment,
            status=status,
            program_id=program_id,
            latency_ms=round(latency_ms, 1) if latency_ms is not None else None,
            tokens_in=prompt_tokens,
            tokens_out=completion_tokens,
            cost_usd=round(cost_usd, 6) if cost_usd is not None else None,
            error_detail=error,
        )
        try:
            append_ai_telemetry(record)
        except Exception:  # pragma: no cover - telemetry must not break the call
            logger.warning("AI telemetry append failed.")

    def _extract_text(self, response: Any) -> str:
        choices = getattr(response, "choices", None) or ()
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", "")
        return str(content or "")

    def _extract_json_object(self, response: Any) -> dict[str, Any]:
        content = self._extract_text(response)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            raise AIClientError(f"Azure OpenAI structured response returned invalid JSON: {error}") from error
        if not isinstance(payload, dict):
            raise AIClientError("Azure OpenAI structured response returned a non-object payload.")
        return payload

    def _build_run_cost_guard(
        self,
        trace_context: AITraceContext | None,
        budget_usd: float,
    ) -> Any:
        if trace_context is None:
            return None

        run_budget_usd = _resolve_run_budget_usd(trace_context.metadata, default=budget_usd)
        if run_budget_usd == 0:
            raise BudgetExceeded("Run AI budget is zero; provider call blocked.")

        from src.ai.cost_guard import CostGuard

        return CostGuard(
            edition=trace_context.edition,
            run_id=trace_context.run_id,
            budget_usd=run_budget_usd,
        )


def _resolve_run_budget_usd(metadata: dict[str, Any], *, default: float) -> float:
    value = metadata.get("run_budget_usd")
    if value is None:
        return default
    return _validate_budget_usd(value, label="Run", allow_string=True)


def _validate_usage_tokens(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BudgetStateError("AI provider usage is invalid; reconcile before retrying.")
    return value


def _validate_reservation_tokens(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BudgetConfigurationError("AI token reservation requires nonnegative integer counts.")
    return value


def _validate_budget_usd(value: object, *, label: str, allow_string: bool = False) -> float:
    message = f"{label} AI budget must be a finite nonnegative number."
    if isinstance(value, bool) or not (
        isinstance(value, (int, float)) or (allow_string and isinstance(value, str))
    ):
        raise BudgetConfigurationError(message)
    try:
        budget = float(value)
    except (OverflowError, ValueError):
        raise BudgetConfigurationError(message) from None
    if not math.isfinite(budget) or budget < 0:
        raise BudgetConfigurationError(message)
    return budget


def _resolve_rate_limit_scope(*, trace_context: AITraceContext | None, deployment: str) -> str:
    if trace_context is not None and trace_context.run_id.strip():
        return f"run:{trace_context.run_id}"
    return f"deployment:{deployment}"

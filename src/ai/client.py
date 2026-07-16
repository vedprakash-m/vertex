from __future__ import annotations

# Adapted from Shiproom src/ai/client.py

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.llm_trace import AITraceContext, get_current_trace_context, llm_trace
from src.ai.request_router import AIRequestRouter
from src.core.policy_loader import load_ai_model_cost_policy


logger = logging.getLogger(__name__)
StructuredResponse = TypeVar("StructuredResponse")


class AIClientError(Exception):
    """Raised when the Azure OpenAI client cannot complete a request."""


class BudgetExceeded(AIClientError):
    """Raised when the per-run AI budget has already been exhausted."""


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
    ) -> None:
        if not deployment.strip():
            raise RuntimeError("Azure OpenAI deployment name is required.")
        pricing = load_ai_model_cost_policy(deployment)

        self._deployment = deployment
        self._temperature = temperature
        self._budget_usd = budget_usd
        self._input_cost_per_1k_tokens = pricing.input_cost_per_1k_tokens
        self._output_cost_per_1k_tokens = pricing.output_cost_per_1k_tokens
        self._spent_usd = 0.0
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

    def _check_budget(self) -> None:
        if self._spent_usd >= self._budget_usd:
            raise BudgetExceeded(f"Spent ${self._spent_usd:.3f} of ${self._budget_usd:.2f}")
        if self._run_cost_guard is not None:
            self._run_cost_guard.check()

    def _record_usage(self, response: Any) -> tuple[int, int, float, BudgetExceeded | None]:
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        self._usage_stats.add(prompt_tokens, completion_tokens)
        cost_usd = self._estimate_cost(prompt_tokens, completion_tokens)
        self._spent_usd += cost_usd
        budget_error: BudgetExceeded | None = None
        if self._run_cost_guard is not None:
            run_state = self._run_cost_guard.record_actual(cost_usd)
            if run_state.spent_usd > run_state.budget_usd:
                budget_error = BudgetExceeded(
                    f"Spent ${run_state.spent_usd:.3f} of ${run_state.budget_usd:.2f}; actual AI spend exceeded the run ceiling."
                )
        return prompt_tokens, completion_tokens, cost_usd, budget_error

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
        if get_ai_mode() == AIMode.OBSERVE_ONLY:
            message = "AI request router is in observe-only mode; frontier calls are blocked."
            self._write_trace(
                prompt_version=prompt_version,
                latency_ms=0.0,
                error=message,
            )
            raise AIClientError(message)
        self._check_budget()
        started = time.perf_counter()
        try:
            response = self._router.route(
                deployment=self._deployment,
                system=system,
                user=user,
                temperature=self._temperature,
                max_tokens=max_tokens,
                response_format=response_format,
            )
            prompt_tokens, completion_tokens, cost_usd, budget_error = self._record_usage(response)
            self._write_trace(
                prompt_version=prompt_version,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=(time.perf_counter() - started) * 1000,
                cost_usd=cost_usd,
                error=str(budget_error) if budget_error is not None else None,
            )
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
        except BudgetExceeded:
            raise
        except Exception as error:  # pragma: no cover - exercised through tests with fakes
            wrapped_error = AIClientError(
                f"Azure OpenAI {mode} failed for deployment {self._deployment}: {error}"
            )
            self._write_trace(
                prompt_version=prompt_version,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=str(wrapped_error),
            )
            raise wrapped_error from error

    def _write_trace(
        self,
        *,
        prompt_version: str | None,
        latency_ms: float,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_usd: float | None = None,
        error: str | None = None,
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
        )

    def _emit_ai_telemetry(
        self,
        *,
        prompt_version: str | None,
        latency_ms: float,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cost_usd: float | None,
        error: str | None,
    ) -> None:
        """Write one canonical :mod:`src.core.ai_telemetry` row per provider call.

        Telemetry is best-effort-durable: a write failure must never mask the
        underlying result/error, so exceptions here are logged and swallowed.
        """
        from src.core.ai_telemetry import AiTelemetryRecord, AiTelemetryStatus, append_ai_telemetry

        metadata = self._trace_context.metadata if self._trace_context else {}
        feature = str(metadata.get("feature") or self._trace_context.caller if self._trace_context else "unknown")
        program_id = str(metadata.get("program_id") or self._trace_context.edition if self._trace_context else "unknown")
        status = AiTelemetryStatus.FALLBACK if error is not None and "disabled" in str(error).lower() else (
            AiTelemetryStatus.OTHER if error is not None else AiTelemetryStatus.OK
        )
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
            logger.warning("AI telemetry append failed for feature=%s", feature, exc_info=True)

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
        if run_budget_usd <= 0:
            return None

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
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                return float(stripped)
            except ValueError:
                return default
    return default


def _resolve_rate_limit_scope(*, trace_context: AITraceContext | None, deployment: str) -> str:
    if trace_context is not None and trace_context.run_id.strip():
        return f"run:{trace_context.run_id}"
    return f"deployment:{deployment}"

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import portalocker

from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT


_TRACE_ENVS = ("VERTEX_LLM_TRACE",)


@dataclass(frozen=True, slots=True)
class AITraceContext:
    edition: str
    run_id: str
    caller: str
    trace_file: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# D-20: Process Trace Context. A ContextVar holds the trace context for the
# current logical run so AI clients can correlate traces/cost/rate-limit scope
# without every intermediate layer hand-threading ``trace_context=``. An
# explicit ``trace_context`` argument always wins; the ContextVar is the
# fallback set once at a command entry point via ``use_trace_context``.
_CURRENT_TRACE_CONTEXT: ContextVar[AITraceContext | None] = ContextVar(
    "vertex_current_trace_context", default=None
)


def get_current_trace_context() -> AITraceContext | None:
    """Return the trace context bound to the current execution context, if any."""
    return _CURRENT_TRACE_CONTEXT.get()


@contextmanager
def use_trace_context(trace_context: AITraceContext | None) -> Iterator[AITraceContext | None]:
    """Bind ``trace_context`` as the process trace context for the enclosed scope.

    Nesting is supported and the previous value is always restored on exit.
    """
    token = _CURRENT_TRACE_CONTEXT.set(trace_context)
    try:
        yield trace_context
    finally:
        _CURRENT_TRACE_CONTEXT.reset(token)


def is_trace_enabled() -> bool:
    return any(os.environ.get(name, "").strip() for name in _TRACE_ENVS)


def default_trace_path(edition: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_program_output_dir(edition, programs_root=programs_root) / "ai" / "llm_trace.jsonl"


def llm_trace(
    *,
    edition: str,
    run_id: str,
    caller: str,
    model: str,
    deployment: str | None = None,
    prompt_version: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    latency_ms: float | None = None,
    cost_usd: float | None = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
    trace_file: Path | None = None,
) -> None:
    if not is_trace_enabled():
        return

    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "edition": edition,
        "run_id": run_id,
        "caller": caller,
        "model": model,
    }
    if deployment is not None:
        record["deployment"] = deployment
    if prompt_version is not None:
        record["prompt_version"] = prompt_version
    if prompt_tokens is not None:
        record["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        record["completion_tokens"] = completion_tokens
        if prompt_tokens is not None:
            record["total_tokens"] = prompt_tokens + completion_tokens
    if latency_ms is not None:
        record["latency_ms"] = round(latency_ms, 1)
    if cost_usd is not None:
        record["cost_usd"] = round(cost_usd, 6)
    if error is not None:
        record["error"] = error
    if metadata:
        record["metadata"] = {str(key): value for key, value in metadata.items()}

    target = trace_file or default_trace_path(edition)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            portalocker.lock(handle, portalocker.LOCK_EX)
            try:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                portalocker.unlock(handle)
    except Exception:
        return
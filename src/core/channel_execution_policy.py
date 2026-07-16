"""ADF-W1.4 (specs/arch-data-fix.md Section 8.3.1): channel execution budgets.

Every channel provider call (discovery, hydration) must run under a bounded
attempt. A slow or hung provider degrades the channel rather than blocking
the caller indefinitely -- Section 1 names inline optional-source waits an
immediate safety blocker (the historical XPF evidence: WorkIQ p50 latency
above 65 minutes). ``run_under_channel_budget`` is the single enforcement
point; ``src/commands/gather_pipeline/channel_runtime.py`` is its only
caller today.

Known Python limitation (documented, not solved): there is no safe way to
forcibly kill a running thread. On timeout, the worker thread executing the
provider call is abandoned (not joined) so the *caller* is never blocked
past ``per_attempt_timeout_seconds`` -- but the abandoned thread keeps
running until the underlying provider call itself returns or raises (bounded
by that provider's own HTTP/client timeout) or the process exits. This
still delivers the actual safety property that matters: gather/report never
blocks on a slow channel past its configured budget.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from src.core.adf_config import ArchDataFixConfig

T = TypeVar("T")

_log = logging.getLogger(__name__)

#: Appendix A.4 ChannelExecutionRecord.degrade_reason enum values.
DEGRADE_REASON_TIMEOUT = "timeout"
DEGRADE_REASON_BUDGET_EXCEEDED = "budget_exceeded"
DEGRADE_REASON_PROVIDER_ERROR = "provider_error"
DEGRADE_REASON_AUTH_EXPIRED = "auth_expired"
DEGRADE_REASON_CANCELLED = "cancelled"
DEGRADE_REASON_CAP_REACHED = "cap_reached"
DEGRADE_REASON_STALE_SNAPSHOT_USED = "stale_snapshot_used"
DEGRADE_REASON_POLICY_DISABLED = "policy_disabled"

CHANNEL_DEGRADE_REASONS = frozenset(
    {
        DEGRADE_REASON_TIMEOUT,
        DEGRADE_REASON_BUDGET_EXCEEDED,
        DEGRADE_REASON_PROVIDER_ERROR,
        DEGRADE_REASON_AUTH_EXPIRED,
        DEGRADE_REASON_CANCELLED,
        DEGRADE_REASON_CAP_REACHED,
        DEGRADE_REASON_STALE_SNAPSHOT_USED,
        DEGRADE_REASON_POLICY_DISABLED,
    }
)

#: Appendix A.4 ChannelExecutionRecord.status enum values.
CHANNEL_EXECUTION_STATUSES = frozenset({"complete", "partial", "degraded", "failed", "skipped"})

#: Conservative default when a channel has no ratified adf_config budget yet
#: (Phase-0 candidates, Section 8.3.3, are not binding until ratified).
DEFAULT_PER_ATTEMPT_TIMEOUT_SECONDS = 60
DEFAULT_TOTAL_BUDGET_SECONDS = 120


@dataclass(frozen=True, slots=True)
class ChannelExecutionPolicy:
    """Section 8.3.1. The runtime counterpart of ``adf_config.ChannelBudget``."""

    channel: str
    required: bool
    inline_allowed: bool
    per_attempt_timeout_seconds: int
    total_budget_seconds: int
    max_pages: int | None
    max_records: int | None
    stale_fallback_allowed: bool
    prefetch_required: bool


def channel_execution_policy_for(
    channel: str,
    *,
    config: ArchDataFixConfig,
    default_required: bool = False,
) -> ChannelExecutionPolicy:
    """Materialize the Section 8.3.1 policy for *channel* from adf_config.

    An unconfigured channel (no ``arch_data_fix.channels.<channel>`` block,
    or ``mode: off``) yields a safe conservative default rather than raising
    -- gather must keep working before Phase-0 ratifies real budgets per
    program (ADF-W0.6).
    """
    budget = config.channels.get(channel)
    if budget is None:
        return ChannelExecutionPolicy(
            channel=channel,
            required=default_required,
            inline_allowed=True,
            per_attempt_timeout_seconds=DEFAULT_PER_ATTEMPT_TIMEOUT_SECONDS,
            total_budget_seconds=DEFAULT_TOTAL_BUDGET_SECONDS,
            max_pages=None,
            max_records=None,
            stale_fallback_allowed=False,
            prefetch_required=False,
        )
    return ChannelExecutionPolicy(
        channel=channel,
        required=budget.required,
        inline_allowed=budget.inline_allowed,
        per_attempt_timeout_seconds=budget.per_attempt_timeout_seconds,
        total_budget_seconds=budget.total_budget_seconds,
        max_pages=budget.max_pages,
        max_records=budget.max_records,
        stale_fallback_allowed=budget.stale_fallback_allowed,
        prefetch_required=budget.prefetch_required,
    )


@dataclass(frozen=True, slots=True)
class BudgetedCallOutcome:
    """Result of ``run_under_channel_budget``.

    Callers must check ``degraded`` before trusting ``value`` (Section
    8.3.2: "timeout cancellation must not leave a success-shaped empty
    result" -- ``value`` is ``None`` whenever ``degraded`` is True, never a
    silently-empty success shape).
    """

    value: object | None
    degraded: bool
    degrade_reason: str | None
    elapsed_seconds: float


def run_under_channel_budget(
    fn: Callable[[], T],
    *,
    policy: ChannelExecutionPolicy,
) -> BudgetedCallOutcome:
    """Run ``fn()`` bounded by ``policy.per_attempt_timeout_seconds``.

    Runs ``fn`` on a single-worker thread so a hung synchronous provider
    call cannot block the caller past the configured timeout. See the
    module docstring for the documented thread-abandonment trade-off.
    """
    started = time.monotonic()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"channel-{policy.channel}")
    future = executor.submit(fn)
    try:
        value = future.result(timeout=policy.per_attempt_timeout_seconds)
        executor.shutdown(wait=False)
        return BudgetedCallOutcome(value=value, degraded=False, degrade_reason=None, elapsed_seconds=time.monotonic() - started)
    except concurrent.futures.TimeoutError:
        # Do not wait for the worker: that would defeat the timeout. The
        # thread is abandoned (see module docstring); shutdown(wait=False)
        # only stops new submissions to this (single-use) executor.
        executor.shutdown(wait=False)
        _log.warning(
            "channel %s exceeded its %ss per-attempt budget; degrading rather than blocking",
            policy.channel,
            policy.per_attempt_timeout_seconds,
        )
        return BudgetedCallOutcome(
            value=None,
            degraded=True,
            degrade_reason=DEGRADE_REASON_TIMEOUT,
            elapsed_seconds=time.monotonic() - started,
        )
    except Exception:
        executor.shutdown(wait=False)
        raise


__all__ = [
    "DEGRADE_REASON_TIMEOUT",
    "DEGRADE_REASON_BUDGET_EXCEEDED",
    "DEGRADE_REASON_PROVIDER_ERROR",
    "DEGRADE_REASON_AUTH_EXPIRED",
    "DEGRADE_REASON_CANCELLED",
    "DEGRADE_REASON_CAP_REACHED",
    "DEGRADE_REASON_STALE_SNAPSHOT_USED",
    "DEGRADE_REASON_POLICY_DISABLED",
    "CHANNEL_DEGRADE_REASONS",
    "CHANNEL_EXECUTION_STATUSES",
    "DEFAULT_PER_ATTEMPT_TIMEOUT_SECONDS",
    "DEFAULT_TOTAL_BUDGET_SECONDS",
    "ChannelExecutionPolicy",
    "channel_execution_policy_for",
    "BudgetedCallOutcome",
    "run_under_channel_budget",
]

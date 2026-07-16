"""ADF-W1.4 (Section 8.3.1): channel execution budgets.

Covers the enforcement primitive (``run_under_channel_budget``) in
isolation and its wiring into ``channel_runtime.run_channel`` with a fake
slow provider: an over-budget optional channel must degrade within its
configured timeout rather than block the caller (Section 8.3.2 -- "optional
channels cannot delay required-channel completion past their total
budget").
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.adf_config import ArchDataFixConfig, ChannelBudget
from src.core.channel_execution_policy import (
    DEFAULT_PER_ATTEMPT_TIMEOUT_SECONDS,
    DEGRADE_REASON_BUDGET_EXCEEDED,
    DEGRADE_REASON_TIMEOUT,
    ChannelExecutionPolicy,
    channel_execution_policy_for,
    run_under_channel_budget,
)
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import IntegrationError


# --------------------------------------------------------------------------------------
# channel_execution_policy_for
# --------------------------------------------------------------------------------------


def test_unconfigured_channel_gets_safe_default_policy() -> None:
    config = ArchDataFixConfig(program_id="fixture_prog")  # mode=off, no channels
    policy = channel_execution_policy_for("ado", config=config)
    assert policy.channel == "ado"
    assert policy.required is False
    assert policy.per_attempt_timeout_seconds == DEFAULT_PER_ATTEMPT_TIMEOUT_SECONDS


def test_configured_channel_uses_ratified_budget() -> None:
    config = ArchDataFixConfig(
        program_id="fixture_prog",
        channels={
            "workiq": ChannelBudget(
                required=False,
                inline_allowed=False,
                per_attempt_timeout_seconds=5,
                total_budget_seconds=10,
                stale_fallback_allowed=True,
            )
        },
    )
    policy = channel_execution_policy_for("workiq", config=config)
    assert policy.per_attempt_timeout_seconds == 5
    assert policy.inline_allowed is False
    assert policy.stale_fallback_allowed is True


# --------------------------------------------------------------------------------------
# run_under_channel_budget
# --------------------------------------------------------------------------------------


def _policy(*, timeout_seconds: int) -> ChannelExecutionPolicy:
    return ChannelExecutionPolicy(
        channel="workiq",
        required=False,
        inline_allowed=False,
        per_attempt_timeout_seconds=timeout_seconds,
        total_budget_seconds=timeout_seconds * 2,
        max_pages=None,
        max_records=None,
        stale_fallback_allowed=False,
        prefetch_required=False,
    )


def test_fast_call_completes_normally() -> None:
    outcome = run_under_channel_budget(lambda: "ok", policy=_policy(timeout_seconds=5))
    assert outcome.degraded is False
    assert outcome.value == "ok"
    assert outcome.degrade_reason is None


def test_slow_call_degrades_within_budget_not_after_it_completes() -> None:
    """The defining safety property: the caller must not block past the budget."""

    def _hangs_forever() -> str:
        time.sleep(3600)
        return "should never get here in this test's lifetime"

    started = time.monotonic()
    outcome = run_under_channel_budget(_hangs_forever, policy=_policy(timeout_seconds=1))
    elapsed = time.monotonic() - started

    assert outcome.degraded is True
    assert outcome.value is None
    assert outcome.degrade_reason == DEGRADE_REASON_TIMEOUT
    # Bounded by the configured budget, not by the fake provider's 3600s sleep.
    assert elapsed < 5.0


def test_provider_exception_propagates_unchanged() -> None:
    def _raises() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_under_channel_budget(_raises, policy=_policy(timeout_seconds=5))


def test_remaining_budget_caps_attempt_timeout_below_per_attempt() -> None:
    """A sibling call already spent most of total_budget_seconds -- this
    attempt gets whatever's left, not the full per_attempt_timeout_seconds."""

    def _sleeps_past_remaining_budget() -> str:
        time.sleep(2.0)
        return "too slow for the remaining budget"

    started = time.monotonic()
    outcome = run_under_channel_budget(
        _sleeps_past_remaining_budget,
        policy=_policy(timeout_seconds=10),
        remaining_budget_seconds=0.5,
    )
    elapsed = time.monotonic() - started

    assert outcome.degraded is True
    assert outcome.degrade_reason == DEGRADE_REASON_TIMEOUT
    assert elapsed < 2.0  # bounded by remaining_budget_seconds, not per_attempt_timeout_seconds=10


def test_exhausted_remaining_budget_skips_call_entirely() -> None:
    calls: list[str] = []

    def _fn() -> str:
        calls.append("invoked")
        return "should never run"

    outcome = run_under_channel_budget(
        _fn,
        policy=_policy(timeout_seconds=10),
        remaining_budget_seconds=0,
    )

    assert outcome.degraded is True
    assert outcome.degrade_reason == DEGRADE_REASON_BUDGET_EXCEEDED
    assert outcome.elapsed_seconds == 0.0
    assert calls == []  # fn must never be invoked once the total budget is gone


def test_result_exceeding_max_records_degrades_instead_of_truncating() -> None:
    policy = ChannelExecutionPolicy(
        channel="ado",
        required=False,
        inline_allowed=True,
        per_attempt_timeout_seconds=30,
        total_budget_seconds=60,
        max_pages=None,
        max_records=5,
        stale_fallback_allowed=False,
        prefetch_required=False,
    )

    outcome = run_under_channel_budget(
        lambda: list(range(6)),
        policy=policy,
        record_count_fn=len,
    )

    # D-3: an over-budget result is discarded and reported, never silently
    # truncated to fit within max_records.
    assert outcome.degraded is True
    assert outcome.degrade_reason == DEGRADE_REASON_BUDGET_EXCEEDED
    assert outcome.value is None


def test_result_exceeding_max_pages_degrades_instead_of_truncating() -> None:
    policy = ChannelExecutionPolicy(
        channel="ado",
        required=False,
        inline_allowed=True,
        per_attempt_timeout_seconds=30,
        total_budget_seconds=60,
        max_pages=3,
        max_records=None,
        stale_fallback_allowed=False,
        prefetch_required=False,
    )

    outcome = run_under_channel_budget(
        lambda: {"pages": 4},
        policy=policy,
        page_count_fn=lambda result: result["pages"],
    )

    assert outcome.degraded is True
    assert outcome.degrade_reason == DEGRADE_REASON_BUDGET_EXCEEDED
    assert outcome.value is None


def test_result_within_max_records_and_max_pages_succeeds() -> None:
    policy = ChannelExecutionPolicy(
        channel="ado",
        required=False,
        inline_allowed=True,
        per_attempt_timeout_seconds=30,
        total_budget_seconds=60,
        max_pages=3,
        max_records=5,
        stale_fallback_allowed=False,
        prefetch_required=False,
    )

    outcome = run_under_channel_budget(
        lambda: {"records": [1, 2, 3], "pages": 2},
        policy=policy,
        record_count_fn=lambda result: len(result["records"]),
        page_count_fn=lambda result: result["pages"],
    )

    assert outcome.degraded is False
    assert outcome.value == {"records": [1, 2, 3], "pages": 2}


# --------------------------------------------------------------------------------------
# Wiring into channel_runtime.run_channel: optional overrun cannot delay
# required completion.
# --------------------------------------------------------------------------------------


def test_slow_optional_channel_degrades_instead_of_blocking_gather(tmp_path: Path) -> None:
    from src.commands.gather_pipeline import channel_runtime
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ChannelBinding,
        ChannelConfig,
        HydrationResult,
        RunContext,
    )

    current_time = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "fixture_prog"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "arch_data_fix:",
                "  mode: observe",
                "  channels:",
                "    workiq:",
                "      required: false",
                "      per_attempt_timeout_seconds: 1",
                "      total_budget_seconds: 2",
            )
        ),
        encoding="utf-8",
    )

    store = ChannelRegistryStore(program_dir / "channel_registry.sqlite3", "fixture_prog")

    class _NeverDiscovers:
        def discover(self, program_id, config, existing, run_ctx=None):
            del program_id, config, existing, run_ctx
            from src.core.integration_types import DiscoveryCompleteness, DiscoveryResult

            return DiscoveryResult(
                channel="workiq",
                program_id="fixture_prog",
                discovered_refs=(),
                completeness=DiscoveryCompleteness.FULL,
                scope_statuses={},
                scope_state_updates={},
                errors=(),
                computed_at=current_time,
            )

    class _HangingHydrationProvider:
        def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
            del registrations, since, program_id, config, mode, run_ctx
            time.sleep(3600)  # simulates the historical WorkIQ 65+ minute latency
            return HydrationResult(channel="workiq", resources=(), api_call_count=1, errors=(), hydrated_ref_ids=(), failed_ref_ids=())

    binding = ChannelBinding(
        config=ChannelConfig(channel="workiq", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=_NeverDiscovers(),
        hydration_provider=_HangingHydrationProvider(),
        signal_extractor=None,
        discovery_config=object(),
        hydration_config=object(),
    )

    errors: list[IntegrationError] = []
    started = time.monotonic()
    hydration_result, delta = channel_runtime.run_channel(
        binding,
        store,
        program_id="fixture_prog",
        since=current_time - timedelta(days=14),
        verified_at=current_time,
        run_ctx=RunContext(),
        integration_error_sink=errors,
        programs_root=programs_root,
    )
    elapsed = time.monotonic() - started

    # The optional channel's 3600s hang must not delay the caller past its
    # configured 1s per-attempt budget.
    assert elapsed < 10.0
    assert hydration_result is None
    assert any("budget" in error.message.lower() for error in errors)


def test_slow_discovery_leaves_no_total_budget_for_hydration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-3/ADF-W1.4: total_budget_seconds is a combined ceiling across
    discovery + hydration for the same channel this cycle. A discovery call
    that consumes (almost) the whole total budget must starve hydration's
    remaining window -- hydration is skipped entirely on total-budget
    exhaustion even though its own provider call would return instantly.

    Spies on ``run_under_channel_budget`` itself (rather than real sleeps)
    so the assertion on the *remaining* budget passed to the hydration call
    is deterministic, not a wall-clock race.
    """
    from src.commands.gather_pipeline import channel_runtime
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ChannelBinding,
        ChannelConfig,
        DiscoveryCompleteness,
        DiscoveryResult,
        HydrationResult,
        RunContext,
    )

    current_time = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "fixture_prog"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "arch_data_fix:",
                "  mode: observe",
                "  channels:",
                "    workiq:",
                "      required: false",
                "      per_attempt_timeout_seconds: 5",
                "      total_budget_seconds: 6",
            )
        ),
        encoding="utf-8",
    )

    store = ChannelRegistryStore(program_dir / "channel_registry.sqlite3", "fixture_prog")

    discovery_result = DiscoveryResult(
        channel="workiq",
        program_id="fixture_prog",
        discovered_refs=(),
        completeness=DiscoveryCompleteness.FULL,
        scope_statuses={},
        scope_state_updates={},
        errors=(),
        computed_at=current_time,
    )

    class _Discovery:
        def discover(self, program_id, config, existing, run_ctx=None):
            del program_id, config, existing, run_ctx
            return discovery_result

    hydration_calls: list[str] = []

    class _InstantHydrationProvider:
        def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
            del registrations, since, program_id, config, mode, run_ctx
            hydration_calls.append("invoked")
            return HydrationResult(channel="workiq", resources=(), api_call_count=1, errors=(), hydrated_ref_ids=(), failed_ref_ids=())

    binding = ChannelBinding(
        config=ChannelConfig(channel="workiq", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=_Discovery(),
        hydration_provider=_InstantHydrationProvider(),
        signal_extractor=None,
        discovery_config=object(),
        hydration_config=object(),
    )

    remaining_budgets_seen: list[float | None] = []
    real_run_under_channel_budget = channel_runtime.run_under_channel_budget

    def _spy(fn, *, policy, remaining_budget_seconds=None, record_count_fn=None, page_count_fn=None):
        remaining_budgets_seen.append(remaining_budget_seconds)
        if remaining_budgets_seen == [6]:
            # First (discovery) call: report as if it consumed 6.5s of the
            # 6s total budget (over budget already), without any real sleep.
            from src.core.channel_execution_policy import BudgetedCallOutcome

            return BudgetedCallOutcome(value=fn(), degraded=False, degrade_reason=None, elapsed_seconds=6.5)
        return real_run_under_channel_budget(
            fn, policy=policy, remaining_budget_seconds=remaining_budget_seconds,
            record_count_fn=record_count_fn, page_count_fn=page_count_fn,
        )

    monkeypatch.setattr(channel_runtime, "run_under_channel_budget", _spy)

    errors: list[IntegrationError] = []
    hydration_result, delta = channel_runtime.run_channel(
        binding,
        store,
        program_id="fixture_prog",
        since=current_time - timedelta(days=14),
        verified_at=current_time,
        run_ctx=RunContext(),
        integration_error_sink=errors,
        programs_root=programs_root,
    )

    assert remaining_budgets_seen[0] == 6  # discovery starts with the full total budget
    assert remaining_budgets_seen[1] == pytest.approx(-0.5)  # 6 - 6.5: budget already exhausted
    assert hydration_result is None
    # The hydration provider must never even be invoked once discovery alone
    # has already exhausted the channel's combined discovery+hydration
    # budget -- run_under_channel_budget's <=0 floor skips the call outright.
    assert hydration_calls == []
    assert any(
        error.stage == "hydration" and DEGRADE_REASON_BUDGET_EXCEEDED in error.message
        for error in errors
    )

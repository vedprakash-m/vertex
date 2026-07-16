"""ADF-W1.6 (Section 8.5.3): bounded Kusto query concurrency.

``max_concurrency`` defaults to 1 (sequential, unchanged pre-ADF-W1.6
behavior) and is only raised once ADF-W0.6 ratifies parallelism from the
benchmark artifact. This file proves the parallel path's contract:
deterministic result ordering by registration/query_id regardless of
completion order, per-query timeout, and no cross-cancellation (one query's
failure or timeout never affects another's independent result).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from src.core.integration_types import ChannelRegistration, RegistrationStatus
from src.core.kusto_hydration import KustoHydrationConfig, KustoHydrationProvider
from src.core.models_v2 import KustoQuery

_NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def _query(query_id: str) -> KustoQuery:
    return KustoQuery(
        id=query_id,
        cluster="https://cluster",
        database="db",
        kql=f"{query_id} | take 1",
        section=query_id,
        render_as="table",
        confidence="high",
        validated=True,
    )


def _registration(ref_id: str) -> ChannelRegistration:
    return ChannelRegistration(
        channel="kusto",
        program_id="demo",
        provider_instance_id="default",
        ref_id=ref_id,
        ref_kind="kusto_query",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=_NOW,
        last_seen_at=_NOW,
    )


def test_default_max_concurrency_is_sequential(tmp_path: Path) -> None:
    assert KustoHydrationConfig(programs_root=tmp_path).max_concurrency == 1


def test_parallel_execution_preserves_deterministic_result_order(tmp_path: Path) -> None:
    """Queries finish out of order (fast ones first); output must still be
    ordered by the registration/query_id sequence, not completion order."""
    query_ids = ["query-a", "query-b", "query-c", "query-d"]
    queries = tuple(_query(q) for q in query_ids)
    registrations = tuple(_registration(q) for q in query_ids)

    # Deliberately inverted completion order: "query-d" finishes fastest,
    # "query-a" finishes slowest.
    delay_by_id = {"query-a": 0.3, "query-b": 0.2, "query-c": 0.1, "query-d": 0.0}

    def _executor(rendered_query: str) -> list[dict[str, object]]:
        for query_id, delay in delay_by_id.items():
            if rendered_query.id == query_id:
                time.sleep(delay)
                return [{"id": query_id}]
        return []

    provider = KustoHydrationProvider(executor=_executor, query_loader=lambda program_id, programs_root: queries)
    config = KustoHydrationConfig(programs_root=tmp_path, max_concurrency=4, per_query_timeout_seconds=5)

    result = provider.hydrate(registrations, _NOW, "demo", config)

    assert [rs.query_id for rs in result.resources.result_sets] == query_ids
    assert result.hydrated_ref_ids == tuple((q, "kusto_query") for q in query_ids)
    assert result.api_call_count == 4


def test_parallel_execution_isolates_a_failing_query_from_the_others(tmp_path: Path) -> None:
    query_ids = ["query-a", "query-b", "query-c"]
    queries = tuple(_query(q) for q in query_ids)
    registrations = tuple(_registration(q) for q in query_ids)

    def _executor(rendered_query: str) -> list[dict[str, object]]:
        if rendered_query.id == "query-b":
            raise RuntimeError("simulated Kusto query failure")
        return [{"ok": True}]

    provider = KustoHydrationProvider(executor=_executor, query_loader=lambda program_id, programs_root: queries)
    config = KustoHydrationConfig(programs_root=tmp_path, max_concurrency=3, per_query_timeout_seconds=5)

    result = provider.hydrate(registrations, _NOW, "demo", config)

    assert [rs.query_id for rs in result.resources.result_sets] == ["query-a", "query-c"]
    assert result.failed_ref_ids == (("query-b", "kusto_query"),)
    assert result.errors[0].ref_id == "query-b"
    assert "simulated Kusto query failure" in result.errors[0].message


def test_parallel_execution_isolates_a_timed_out_query_from_the_others(tmp_path: Path) -> None:
    """The defining ADF-W1.6 safety property: one hung query degrades on its
    own budget without blocking or failing the others."""
    query_ids = ["query-fast-1", "query-hangs", "query-fast-2"]
    queries = tuple(_query(q) for q in query_ids)
    registrations = tuple(_registration(q) for q in query_ids)

    def _executor(rendered_query: str) -> list[dict[str, object]]:
        if rendered_query.id == "query-hangs":
            time.sleep(3600)
            return [{"should": "never get here"}]
        return [{"ok": True}]

    provider = KustoHydrationProvider(executor=_executor, query_loader=lambda program_id, programs_root: queries)
    config = KustoHydrationConfig(programs_root=tmp_path, max_concurrency=3, per_query_timeout_seconds=1)

    started = time.monotonic()
    result = provider.hydrate(registrations, _NOW, "demo", config)
    elapsed = time.monotonic() - started

    assert elapsed < 10.0  # bounded by the 1s per-query budget, not the 3600s hang
    assert [rs.query_id for rs in result.resources.result_sets] == ["query-fast-1", "query-fast-2"]
    assert result.failed_ref_ids == (("query-hangs", "kusto_query"),)
    assert "budget" in result.errors[0].message.lower()


def test_max_concurrency_bounds_simultaneous_executor_threads(tmp_path: Path) -> None:
    """max_concurrency=2 must never let more than 2 queries run at once,
    even with 5 queries submitted."""
    query_ids = [f"query-{i}" for i in range(5)]
    queries = tuple(_query(q) for q in query_ids)
    registrations = tuple(_registration(q) for q in query_ids)

    in_flight = 0
    max_observed_in_flight = 0
    lock = threading.Lock()

    def _executor(rendered_query: str) -> list[dict[str, object]]:
        nonlocal in_flight, max_observed_in_flight
        with lock:
            in_flight += 1
            max_observed_in_flight = max(max_observed_in_flight, in_flight)
        time.sleep(0.15)
        with lock:
            in_flight -= 1
        return [{"ok": True}]

    provider = KustoHydrationProvider(executor=_executor, query_loader=lambda program_id, programs_root: queries)
    config = KustoHydrationConfig(programs_root=tmp_path, max_concurrency=2, per_query_timeout_seconds=5)

    result = provider.hydrate(registrations, _NOW, "demo", config)

    assert max_observed_in_flight <= 2
    assert result.api_call_count == 5


def test_sequential_mode_never_runs_more_than_one_query_at_once(tmp_path: Path) -> None:
    query_ids = ["query-a", "query-b", "query-c"]
    queries = tuple(_query(q) for q in query_ids)
    registrations = tuple(_registration(q) for q in query_ids)

    in_flight = 0
    max_observed_in_flight = 0
    lock = threading.Lock()

    def _executor(rendered_query: str) -> list[dict[str, object]]:
        nonlocal in_flight, max_observed_in_flight
        with lock:
            in_flight += 1
            max_observed_in_flight = max(max_observed_in_flight, in_flight)
        time.sleep(0.02)
        with lock:
            in_flight -= 1
        return [{"ok": True}]

    provider = KustoHydrationProvider(executor=_executor, query_loader=lambda program_id, programs_root: queries)
    config = KustoHydrationConfig(programs_root=tmp_path)  # max_concurrency=1 default

    result = provider.hydrate(registrations, _NOW, "demo", config)

    assert max_observed_in_flight == 1
    assert [rs.query_id for rs in result.resources.result_sets] == query_ids

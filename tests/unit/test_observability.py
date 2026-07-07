from __future__ import annotations

import io
import json
from datetime import date, datetime, timezone

import pytest

from src.core.exceptions import AuthError, ConfigError, ConfirmError, VertexError, QueryError, RenderError, StateError
from src.core.manifest_writer import build_run_manifest, get_manifest_path, hash_content, write_run_manifest
from src.core.models import ConfirmedDimension, EditionType, RiskLevel, Snapshot, SnapshotItem
from src.core.observability import configure_file_logging, configure_logging, get_command_trace_path
from src.core.retry import retry_with_backoff


class _FakeResponse:
    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}


class _FakeHttpError(Exception):
    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        self.response = _FakeResponse(status_code, retry_after)
        super().__init__(f"HTTP {status_code}")


def _snapshot() -> Snapshot:
    return Snapshot(
        issue_number=78,
        generated_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 5, 8, 45, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=101,
                type="Feature",
                title="Deployment rollout tooling",
                state="Active",
                assigned_to="Vertex Maintainer",
                area_path="One\\Adventure\\Acme",
                target_date=date(2026, 5, 12),
                risk_level=RiskLevel.MEDIUM,
                tags=["acme"],
            ),
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Acme Ramp Readiness",
                name="Deployment Velocity",
                risk=RiskLevel.MEDIUM,
                prior_risk=RiskLevel.LOW,
                item_count=1,
                ado_query_url="https://dev.azure.com/your-org/One/_queries/deployment-velocity",
            ),
        ),
    )


def test_exception_hierarchy_includes_phase_one_types() -> None:
    for exc_type in (ConfigError, AuthError, QueryError, RenderError, ConfirmError, StateError):
        assert issubclass(exc_type, VertexError)


def test_build_and_write_run_manifest_hashes_artifacts(tmp_path) -> None:
    snapshot = _snapshot()
    manifest = build_run_manifest(
        manifest_id="12345678-1234-5678-1234-567812345678",
        issue_number=78,
        edition="acme_weekly",
        started_at=datetime(2026, 5, 5, 8, 59, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 5, 9, 1, tzinfo=timezone.utc),
        config_payload={"edition": {"name": "acme_weekly"}},
        snapshot=snapshot,
        html_content="<html><body>newsletter</body></html>",
        markdown_content="# newsletter",
        ado_calls=3,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
        git_sha="abcdef0",
    )

    assert manifest.config_hash.startswith("sha256:")
    assert manifest.snapshot_hash.startswith("sha256:")
    assert manifest.html_hash == hash_content("<html><body>newsletter</body></html>")

    path = write_run_manifest("acme_weekly", 78, manifest, programs_root=tmp_path)

    assert path == get_manifest_path("acme_weekly", 78, programs_root=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["manifest_id"] == manifest.manifest_id
    assert payload["qg_results"]["QG-6"] is True


def test_retry_with_backoff_honors_retry_after_without_sleeping(monkeypatch) -> None:
    waits: list[float] = []
    attempts = {"count": 0}

    def flaky_call() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise _FakeHttpError(429, "2.5")
        return "ok"

    result = retry_with_backoff(
        flaky_call,
        max_attempts=3,
        sleep_func=waits.append,
        jitter_max=0.0,
    )

    assert result == "ok"
    assert waits == [2.5]


def test_retry_with_backoff_does_not_retry_non_retryable_errors() -> None:
    with pytest.raises(_FakeHttpError):
        retry_with_backoff(lambda: (_ for _ in ()).throw(_FakeHttpError(400)), max_attempts=3, sleep_func=lambda _: None)


def test_configure_logging_supports_human_and_json_output() -> None:
    human_stream = io.StringIO()
    human_logger = configure_logging("1234567890abcdef", stream=human_stream)
    human_logger.info("loaded editions/acme_weekly", extra={"stage": "config"})
    human_output = human_stream.getvalue()
    assert "[run_id=12345678]" in human_output
    assert "config: loaded editions/acme_weekly" in human_output

    json_stream = io.StringIO()
    json_logger = configure_logging("abcdef1234567890", json_output=True, stream=json_stream)
    json_logger.info("loaded editions/acme_weekly", extra={"stage": "config", "count": 1})
    payload = json.loads(json_stream.getvalue())
    assert payload["run_id"] == "abcdef12"
    assert payload["logger"] == "vertex"
    assert payload["stage"] == "config"
    assert payload["count"] == 1


def test_configure_file_logging_writes_structured_trace_file(tmp_path) -> None:
    trace_path = get_command_trace_path("acme", "gather", programs_root=tmp_path)

    logger = configure_file_logging("abcdef1234567890", trace_path=trace_path, logger_name="vertex.test.trace")
    logger.info("stage finished", extra={"stage": "fetch", "count": 3})

    payload = json.loads(trace_path.read_text(encoding="utf-8").strip())
    assert payload["run_id"] == "abcdef12"
    assert payload["stage"] == "fetch"
    assert payload["count"] == 3
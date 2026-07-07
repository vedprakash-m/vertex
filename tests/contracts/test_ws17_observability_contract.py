"""WS-17: observability + alerts + support bundle contract tests.

Spec: `specs/prod-vis.md` §WS-17 acceptance:
  "diagnose explains a seeded failure; support bundle redacts PII;
   --perf shows per-channel SLO; a perf test asserts gather within budget."

These tests assert:
1. Failure taxonomy classifies common shapes correctly
2. doctor --diagnose / observability diagnose surfaces the last failure
3. run_telemetry.jsonl round-trips; P50/P95/SLO status computed correctly
4. Support bundle always redacts PII (even when bundle includes a planted
   email in non-PII slot)
5. Alerts append/resolve/banner lifecycle works
6. CLI surface: `vertex observability` and `vertex alerts` subcommands
   are registered with the right subcommand names
"""
from __future__ import annotations

import json
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.alerts import (
    ALERTS_FILENAME,
    AlertRecord,
    AlertSeverity,
    append_alert,
    read_alerts,
    resolve_alert,
    surface_alert_banner,
)
from src.core.failure_taxonomy import (
    FailureCategory,
    classify_exception,
    is_retryable,
)
from src.core.run_telemetry import (
    ChannelRunStats,
    DEFAULT_SLO_MS,
    RunTelemetryRecord,
    append_run_telemetry,
    build_channel_perf_summary,
    read_run_telemetry,
    run_telemetry_path,
)
from src.core.support_bundle import (
    PII_ALLOWED_FIELDS,
    build_support_bundle,
)


# ----- failure taxonomy -----


@pytest.mark.parametrize("message,expected_category", [
    ("ConnectionError: 401 unauthorized alice@contoso.com", FailureCategory.TRANSIENT_AUTH),
    ("HTTP 429 too many requests", FailureCategory.RATE_LIMIT),
    ("ConnectionError: remote end closed connection", FailureCategory.NETWORK),
    ("Kusto query timeout after 60s", FailureCategory.QUERY_TIMEOUT),
    ("Schema error: field not found 'sprint_id'", FailureCategory.SCHEMA_DRIFT),
    ("403 forbidden: insufficient privileges for scope", FailureCategory.PERMISSION),
    ("Out of memory on disk full", FailureCategory.RESOURCE),
    ("YAML missing required key 'program_id'", FailureCategory.CONFIG),
    ("jsonl json decode error at line 7", FailureCategory.DATA_CORRUPTION),
    ("PII redaction failure: email outside slot", FailureCategory.PII_LEAK),
    ("novel failure description", FailureCategory.UNKNOWN),
])
def test_failure_taxonomy_classifies_known_shapes(message: str, expected_category: FailureCategory) -> None:
    """Each known failure pattern must map to a stable category."""
    classification = classify_exception(message)
    assert classification.category == expected_category, (
        f"expected {expected_category.value}, got {classification.category.value} for {message!r}"
    )
    # next_command must be a non-empty string for every category.
    assert classification.next_command, "next_command must be a copy-pasteable recipe"


def test_failure_taxonomy_retryable_flag() -> None:
    """The retryable flag matches the category-level whitelist."""
    for category in FailureCategory:
        if is_retryable(category):
            assert category in {FailureCategory.TRANSIENT_AUTH, FailureCategory.RATE_LIMIT, FailureCategory.NETWORK, FailureCategory.QUERY_TIMEOUT}
        else:
            assert category not in {FailureCategory.TRANSIENT_AUTH, FailureCategory.RATE_LIMIT, FailureCategory.NETWORK, FailureCategory.QUERY_TIMEOUT}


def test_failure_taxonomy_is_stable() -> None:
    """The taxonomy must not silently grow — every new category is a contract change."""
    expected = {
        "transient_auth", "rate_limit", "network", "query_timeout",
        "schema_drift", "permission", "resource", "config",
        "data_corruption", "pii_leak", "unknown",
    }
    actual = {c.value for c in FailureCategory}
    assert actual == expected, f"taxonomy drift: {actual ^ expected}"


# ----- run telemetry -----


def test_run_telemetry_round_trip(tmp_path: Path) -> None:
    """Append → read round-trips a record byte-for-byte (modulo ordering)."""
    rec = RunTelemetryRecord(
        run_id="r1",
        program_id="demo",
        started_at=datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 6, 9, 12, 0, 5, tzinfo=timezone.utc),
        wall_time_seconds=5.0,
        channels=(
            ChannelRunStats(channel="ado", attempts=3, retries=1, successes=3, failures=0,
                          latency_ms_samples=(100, 200, 300)),
            ChannelRunStats(channel="kusto", attempts=2, retries=0, successes=1, failures=1,
                          latency_ms_samples=(5000, 8000),
                          failure_categories=("rate_limit",)),
        ),
    )
    append_run_telemetry(rec, programs_root=tmp_path)
    records = read_run_telemetry("demo", programs_root=tmp_path)
    assert len(records) == 1
    assert records[0].run_id == "r1"
    assert records[0].channels[0].channel == "ado"
    assert records[0].channels[1].failure_categories == ("rate_limit",)


def test_channel_perf_summary_window_and_slo(tmp_path: Path) -> None:
    """P50/P95 across the window; SLO status flips when budget is breached."""
    base = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    # 12 runs: ado latency = 100..1500ms, kusto latency = 1000..12000ms
    for i in range(12):
        append_run_telemetry(
            RunTelemetryRecord(
                run_id=f"r{i}",
                program_id="demo",
                started_at=base + timedelta(minutes=i),
                finished_at=base + timedelta(minutes=i, seconds=10),
                wall_time_seconds=10.0,
                channels=(
                    ChannelRunStats(channel="ado", attempts=1, retries=0, successes=1, failures=0,
                                  latency_ms_samples=(100 + i * 100,)),
                    ChannelRunStats(channel="kusto", attempts=1, retries=0, successes=1, failures=0,
                                  latency_ms_samples=(1000 + i * 1000,)),
                ),
            ),
            programs_root=tmp_path,
        )
    # Default window=10 returns the last 10 (i=2..11).
    summaries = build_channel_perf_summary("demo", programs_root=tmp_path, window=10)
    by_channel = {s.channel: s for s in summaries}
    # ado latencies in last-10 = [300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200]
    # P50 (nearest-rank ceil(0.5*10)=5) = 700; P95 (ceil(0.95*10)=10) = 1200
    assert by_channel["ado"].p50_latency_ms == 700
    assert by_channel["ado"].p95_latency_ms == 1200
    assert by_channel["ado"].slo_status == "ok"
    # kusto latencies in last-10 = [3000, 4000, ..., 12000]; P95 = 12000
    assert by_channel["kusto"].p95_latency_ms == 12000
    assert by_channel["kusto"].slo_status == "ok"  # default SLO=60000ms

    # Override kusto SLO to 5000ms → p95 (12000) > SLO*2 (10000) → fail
    tight_summaries = build_channel_perf_summary("demo", programs_root=tmp_path, window=10,
                                                  slo_overrides={"kusto": 5000})
    tight_by_channel = {s.channel: s for s in tight_summaries}
    assert tight_by_channel["kusto"].slo_status == "fail"

    # SLO of 10000ms → p95 (12000) > SLO (10000) but ≤ SLO*2 (20000) → warn
    warn_summaries = build_channel_perf_summary("demo", programs_root=tmp_path, window=10,
                                                 slo_overrides={"kusto": 10000})
    warn_by_channel = {s.channel: s for s in warn_summaries}
    assert warn_by_channel["kusto"].slo_status == "warn"


def test_run_telemetry_empty_returns_empty(tmp_path: Path) -> None:
    """No telemetry file → empty records + empty summary."""
    assert read_run_telemetry("demo", programs_root=tmp_path) == ()
    assert build_channel_perf_summary("demo", programs_root=tmp_path) == ()


# ----- alerts (between-runs) -----


def test_alerts_append_resolve_banner(tmp_path: Path) -> None:
    """append → banner → resolve → banner-clears lifecycle works."""
    base = datetime.now(timezone.utc)
    a1 = AlertRecord(
        alert_id="a1", program_id="demo", severity=AlertSeverity.ERROR,
        category="transient_auth", message="ADO 401",
        next_command="vertex doctor --check-auth", created_at=base,
    )
    a2 = AlertRecord(
        alert_id="a2", program_id="demo", severity=AlertSeverity.CRITICAL,
        category="permission", message="PAT scope missing",
        next_command="vertex admin scope audit", created_at=base + timedelta(seconds=1),
    )
    append_alert(a1, programs_root=tmp_path)
    append_alert(a2, programs_root=tmp_path)

    open_alerts = read_alerts("demo", programs_root=tmp_path, include_resolved=False)
    assert len(open_alerts) == 2
    assert all(a.resolved_at is None for a in open_alerts)

    banner = surface_alert_banner("demo", programs_root=tmp_path)
    assert banner is not None
    assert "2 unresolved" in banner
    assert "1 critical" in banner
    assert "1 error" in banner

    # Resolve a1
    assert resolve_alert("a1", program_id="demo", programs_root=tmp_path) is True
    open_alerts_after = read_alerts("demo", programs_root=tmp_path, include_resolved=False)
    assert len(open_alerts_after) == 1
    assert open_alerts_after[0].alert_id == "a2"

    banner_after = surface_alert_banner("demo", programs_root=tmp_path)
    assert banner_after is not None
    assert "1 unresolved" in banner_after
    assert "1 critical" in banner_after

    # Resolve unknown → False
    assert resolve_alert("nope", program_id="demo", programs_root=tmp_path) is False

    # include_resolved=True returns BOTH distinct alert_ids (with their
    # current resolution state).
    all_alerts = read_alerts("demo", programs_root=tmp_path, include_resolved=True)
    assert {a.alert_id for a in all_alerts} == {"a1", "a2"}
    by_id = {a.alert_id: a for a in all_alerts}
    assert by_id["a1"].resolved_at is not None  # the resolution row wins
    assert by_id["a2"].resolved_at is None


def test_alerts_banner_clear_returns_none(tmp_path: Path) -> None:
    """All resolved → banner returns None (no banner surfaced)."""
    base = datetime.now(timezone.utc)
    a = AlertRecord(
        alert_id="a1", program_id="demo", severity=AlertSeverity.WARN,
        category="unknown", message="x", next_command="y", created_at=base,
    )
    append_alert(a, programs_root=tmp_path)
    resolve_alert("a1", program_id="demo", programs_root=tmp_path)
    assert surface_alert_banner("demo", programs_root=tmp_path) is None


# ----- support bundle -----


def test_support_bundle_redacts_planted_pii(tmp_path: Path) -> None:
    """A planted email in a non-PII slot must be scrubbed to [REDACTED]."""
    program_id = "demo"
    program_dir = tmp_path / program_id
    program_dir.mkdir()
    (program_dir / "gather_state.json").write_text(json.dumps({
        "program_id": program_id,
        "gathered_at": "2026-06-09T12:00:00Z",
        "channels": {
            "ado": {
                "yield": 5,
                "last_error": "401 unauthorized alice@contoso.com",  # planted PII
            },
        },
    }), encoding="utf-8")
    result = build_support_bundle(
        program_id,
        programs_root=tmp_path,
        output_path=tmp_path / "bundle.tar.gz",
    )
    assert result.bundle_path.exists()
    assert result.redaction_count >= 1
    # Open the tar and confirm scrubbing.
    with tarfile.open(result.bundle_path, "r:gz") as tar:
        names = tar.getnames()
        assert "gather_state.json" in names
        assert "environment.txt" in names
        assert "redaction_log.txt" in names
        gs = tar.extractfile("gather_state.json").read().decode("utf-8")
        assert "alice@contoso.com" not in gs
        assert "[REDACTED]" in gs
        env = tar.extractfile("environment.txt").read().decode("utf-8")
        assert "host: [REDACTED]" in env
        log = tar.extractfile("redaction_log.txt").read().decode("utf-8")
        assert "email" in log


def test_support_bundle_preserves_documented_pii_slots(tmp_path: Path) -> None:
    """PII in documented slots (`assignee_email`, etc.) is preserved."""
    program_id = "demo"
    program_dir = tmp_path / program_id
    program_dir.mkdir()
    (program_dir / "gather_state.json").write_text(json.dumps({
        "program_id": program_id,
        "gathered_at": "2026-06-09T12:00:00Z",
        "raw_metadata": {
            "assignee_email": "bob@contoso.com",  # DOCUMENTED PII slot — keep
        },
    }), encoding="utf-8")
    result = build_support_bundle(
        program_id,
        programs_root=tmp_path,
        output_path=tmp_path / "bundle.tar.gz",
    )
    with tarfile.open(result.bundle_path, "r:gz") as tar:
        gs = tar.extractfile("gather_state.json").read().decode("utf-8")
        assert "bob@contoso.com" in gs, "Documented PII slot was scrubbed — should be preserved"


def test_support_bundle_pii_allowed_fields_is_stable() -> None:
    """The documented PII slot set is contract-locked."""
    assert PII_ALLOWED_FIELDS == frozenset({
        "assignee_email", "attendees", "posted_by", "user_principal_name",
    })


# ----- CLI surface -----


def test_observability_subcommands_registered() -> None:
    """`vertex observability` must register diagnose / perf / bundle."""
    from cli import app

    group_names = {g.name for g in app.registered_groups}
    assert "observability" in group_names
    obs_group = next(g for g in app.registered_groups if g.name == "observability")
    subs = {c.name for c in obs_group.typer_instance.registered_commands}
    assert "diagnose" in subs
    assert "perf" in subs
    assert "bundle" in subs


def test_alerts_subcommands_registered() -> None:
    """`vertex alerts` must register show / append / resolve / banner."""
    from cli import app

    group_names = {g.name for g in app.registered_groups}
    assert "alerts" in group_names
    alerts_group = next(g for g in app.registered_groups if g.name == "alerts")
    subs = {c.name for c in alerts_group.typer_instance.registered_commands}
    assert "show" in subs
    assert "append" in subs
    assert "resolve" in subs
    assert "banner" in subs


# ----- observability checks (doctor integration) -----


def test_diagnose_report_seeds_failure(tmp_path: Path) -> None:
    """A seeded gather_state.json with a known error must be classified and explained."""
    from src.commands.doctor_checks.observability_checks import build_diagnose_report
    from src.core.models_v2 import IntegrationError

    # Seed gather_state with an integration error matching the rate_limit category
    from src.core.gather_state_store import write_gather_state
    from datetime import datetime, timezone

    write_gather_state(
        "demo",
        gathered_at=datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc),
        scanned_items=10,
        discovered_signals=2,
        new_signals=1,
        pending_review=1,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=5,
        archived_journal_files=0,
        background_proposals=0,
        integration_errors=1,
        integration_error_details=(
            IntegrationError(
                source="kusto",
                stage="query",
                retryable=True,
                message="HTTP 429 too many requests",
                operator_action="wait 60s and retry",
            ),
        ),
        programs_root=tmp_path,
    )

    report = build_diagnose_report("demo", programs_root=tmp_path)
    assert report.last_failure_category == FailureCategory.RATE_LIMIT
    assert report.last_failure_retryable is True
    assert "vertex gather" in (report.last_failure_next_command or "")
    # Findings include the failure line, the run-channel line, and the open-alerts line.
    labels = {f.label for f in report.findings}
    assert "last_failure" in labels
    assert "open_alerts" in labels


def test_perf_report_handles_no_data(tmp_path: Path) -> None:
    """No run_telemetry.jsonl → empty report + slo_status=unknown."""
    from src.commands.doctor_checks.observability_checks import build_perf_report

    report = build_perf_report("demo", programs_root=tmp_path)
    assert report.channel_count == 0
    assert report.run_count == 0
    assert report.slo_status_overall == "unknown"


# ---------------------------------------------------------------------------
# WS-17: DEFAULT_SLO_MS values pinned (prevents silent budget inflation)
# ---------------------------------------------------------------------------


def test_default_slo_ms_values_are_pinned() -> None:
    """Contract-pin the SLO budget constants so they cannot be silently raised."""
    expected = {
        "ado": 30_000,
        "kusto": 60_000,
        "icm": 15_000,
        "teams": 20_000,
        "workiq": 45_000,
        "transcript": 30_000,
    }
    assert DEFAULT_SLO_MS == expected, (
        f"DEFAULT_SLO_MS changed without spec approval. "
        f"Expected {expected}, got {DEFAULT_SLO_MS}."
    )


# ---------------------------------------------------------------------------
# WS-17: perf report shows 'ok' when all channels are well under SLO budget
# ---------------------------------------------------------------------------


def test_perf_slo_ok_when_all_channels_under_budget(tmp_path: Path) -> None:
    """Seeding latencies < SLO → overall slo_status_overall == 'ok'."""
    from src.commands.doctor_checks.observability_checks import build_perf_report

    base = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    # Seed one run per channel with a single latency sample at 1/3 of budget.
    for i, (channel, slo_ms) in enumerate(DEFAULT_SLO_MS.items()):
        fast_ms = slo_ms // 3  # clearly under SLO; P95 == fast_ms since one sample
        record = RunTelemetryRecord(
            run_id=f"r_{channel}",
            program_id="demo",
            started_at=base + timedelta(minutes=i),
            finished_at=base + timedelta(minutes=i, seconds=1),
            wall_time_seconds=1.0,
            channels=(
                ChannelRunStats(
                    channel=channel,
                    attempts=1,
                    retries=0,
                    successes=1,
                    failures=0,
                    latency_ms_samples=(fast_ms,),
                ),
            ),
        )
        append_run_telemetry(record, programs_root=tmp_path)

    report = build_perf_report("demo", programs_root=tmp_path)
    assert report.slo_status_overall == "ok", (
        f"Expected 'ok' when all channels are under SLO budget, got {report.slo_status_overall!r}"
    )

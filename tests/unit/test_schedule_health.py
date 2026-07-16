"""ADF-W5.10: src/core/schedule_health.py."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.prefetch_store import write_prefetch_snapshot
from src.core.schedule_health import (
    evaluate_cockpit_schedule_health,
    evaluate_prefetch_schedule_health,
    evaluate_schedule_health,
)

_NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def test_prefetch_missing_when_no_snapshot(tmp_path: Path) -> None:
    finding = evaluate_prefetch_schedule_health("xpf", programs_root=tmp_path, now=_NOW)
    assert finding.status == "missing"
    assert finding.age_hours is None


def test_prefetch_ok_when_fresh(tmp_path: Path) -> None:
    write_prefetch_snapshot(
        program_id="xpf", channel="workiq", payload={"signals": []}, watermark=None,
        completeness="complete", latency_ms=100.0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    finding = evaluate_prefetch_schedule_health(
        "xpf", programs_root=tmp_path, now=_NOW + timedelta(hours=1)
    )
    assert finding.status == "ok"
    assert finding.age_hours == 1.0


def test_prefetch_warn_when_stale(tmp_path: Path) -> None:
    write_prefetch_snapshot(
        program_id="xpf", channel="workiq", payload={"signals": []}, watermark=None,
        completeness="complete", latency_ms=100.0, ttl_seconds=100000, programs_root=tmp_path, now=_NOW,
    )
    finding = evaluate_prefetch_schedule_health(
        "xpf", programs_root=tmp_path, now=_NOW + timedelta(hours=10), stale_after_hours=6
    )
    assert finding.status == "warn"
    assert finding.age_hours == 10.0


def test_cockpit_missing_when_no_html(tmp_path: Path) -> None:
    finding = evaluate_cockpit_schedule_health("xpf", programs_root=tmp_path, now=_NOW)
    assert finding.status == "missing"


def test_cockpit_ok_when_fresh(tmp_path: Path) -> None:
    html_path = tmp_path / "xpf" / "runtime" / "cockpit" / "cockpit.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text("<html></html>", encoding="utf-8")
    finding = evaluate_cockpit_schedule_health("xpf", programs_root=tmp_path, now=datetime.now(timezone.utc))
    assert finding.status == "ok"


def test_cockpit_warn_when_stale(tmp_path: Path) -> None:
    import os
    import time

    html_path = tmp_path / "xpf" / "runtime" / "cockpit" / "cockpit.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text("<html></html>", encoding="utf-8")
    old_time = time.time() - (40 * 3600)  # 40 hours ago
    os.utime(html_path, (old_time, old_time))
    finding = evaluate_cockpit_schedule_health(
        "xpf", programs_root=tmp_path, now=datetime.now(timezone.utc), stale_after_hours=30
    )
    assert finding.status == "warn"


def test_evaluate_schedule_health_returns_both_findings(tmp_path: Path) -> None:
    findings = evaluate_schedule_health("xpf", programs_root=tmp_path, now=_NOW)
    assert len(findings) == 2
    assert {f.artifact for f in findings} == {"prefetch", "cockpit_html"}

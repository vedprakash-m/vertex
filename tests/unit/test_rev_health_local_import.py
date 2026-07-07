"""REV-G8b local-import health telemetry tests (P2-7).

Exercises the ``build_rev_health_report`` extension that reads
``_rev/last_cycle.json`` + ``_rev/cycle_history.jsonl`` (written by the
pipeline) and the local-import inbox/quarantine filesystem state, and derives
the circuit-breaker / quality-floor-not-established / vault-size / inbox-stale /
quarantine-count warnings.

The checkpoint files are plain JSON, so these tests populate them directly
(rather than running full cycles) to isolate the aggregation logic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from src.core.rev.health import (
    DEFAULT_INBOX_STALE_DAYS,
    build_rev_health_report,
    render_rev_health_human,
)


def _write_last_cycle(programs_root: Path, program_id: str, payload: dict) -> None:
    rev_dir = programs_root / program_id / "_rev"
    rev_dir.mkdir(parents=True, exist_ok=True)
    (rev_dir / "last_cycle.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _append_history(programs_root: Path, program_id: str, records: list[dict]) -> None:
    rev_dir = programs_root / program_id / "_rev"
    rev_dir.mkdir(parents=True, exist_ok=True)
    path = rev_dir / "cycle_history.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


class TestLastCycleAndTrend:
    def test_reads_last_cycle_and_actual_shield_degrade(self, tmp_path: Path) -> None:
        _write_last_cycle(tmp_path, "p1", {
            "schema_version": "1.0", "correlation_id": "c1",
            "stop_category": "complete", "candidates_staged": 3, "enumerated": 4,
            "llm_fallback_count": 1, "shield_degrade": False, "wall_clock_seconds": 12.5,
        })
        report = build_rev_health_report("p1", programs_root=tmp_path)
        assert report.last_cycle is not None
        assert report.last_cycle.stop_category == "complete"
        assert report.last_cycle.candidates_staged == 3
        assert report.last_cycle.llm_fallback_count == 1
        # Actual runtime value from last_cycle.json, not the hardcoded default.
        assert report.shield_degrade is False
        assert report.last_cycle.wall_clock_seconds == 12.5

    def test_fallback_trend_last_three_cycles(self, tmp_path: Path) -> None:
        _append_history(tmp_path, "p2", [
            {"correlation_id": f"c{i}", "stop_category": "complete",
             "candidates_staged": 1, "enumerated": 5, "llm_fallback_count": f,
             "wall_clock_seconds": 1.0}
            for i, f in enumerate([0, 2, 1])
        ])
        report = build_rev_health_report("p2", programs_root=tmp_path)
        assert report.llm_fallback_trend == (0, 2, 1)

    def test_no_cycle_yet_keeps_default_shield_degrade(self, tmp_path: Path) -> None:
        report = build_rev_health_report("p-empty", programs_root=tmp_path)
        assert report.last_cycle is None
        assert report.shield_degrade is True  # default until a cycle runs


class TestCircuitBreaker:
    def test_warns_on_high_fallback_rate_in_last_cycle(self, tmp_path: Path) -> None:
        # 6 fallback out of 10 enumerated = 60% > 50% threshold.
        _write_last_cycle(tmp_path, "p3", {
            "schema_version": "1.0", "correlation_id": "c1",
            "stop_category": "complete", "candidates_staged": 4, "enumerated": 10,
            "llm_fallback_count": 6, "shield_degrade": True, "wall_clock_seconds": 5.0,
        })
        report = build_rev_health_report("p3", programs_root=tmp_path)
        assert report.circuit_breaker_warn is True
        assert any("circuit_breaker" in w for w in report.warnings)

    def test_warns_on_three_consecutive_fallback_cycles(self, tmp_path: Path) -> None:
        _append_history(tmp_path, "p4", [
            {"correlation_id": f"c{i}", "stop_category": "complete",
             "candidates_staged": 1, "enumerated": 5, "llm_fallback_count": f,
             "wall_clock_seconds": 1.0}
            for i, f in enumerate([1, 1, 1])
        ])
        report = build_rev_health_report("p4", programs_root=tmp_path)
        assert report.circuit_breaker_warn is True

    def test_no_warn_when_fallbacks_are_sporadic(self, tmp_path: Path) -> None:
        _write_last_cycle(tmp_path, "p5", {
            "schema_version": "1.0", "correlation_id": "c1",
            "stop_category": "complete", "candidates_staged": 4, "enumerated": 10,
            "llm_fallback_count": 1, "shield_degrade": True, "wall_clock_seconds": 5.0,
        })
        _append_history(tmp_path, "p5", [
            {"correlation_id": "c0", "stop_category": "complete", "candidates_staged": 1,
             "enumerated": 5, "llm_fallback_count": 0, "wall_clock_seconds": 1.0},
        ])
        report = build_rev_health_report("p5", programs_root=tmp_path)
        assert report.circuit_breaker_warn is False


class TestInboxAndQuarantine:
    def test_inbox_stale_warns_when_newest_file_old(self, tmp_path: Path) -> None:
        inbox = tmp_path / "p6" / "rev_inbox"
        inbox.mkdir(parents=True)
        old = inbox / "old.eml"
        old.write_text("x", encoding="utf-8")
        # Backdate mtime beyond the stale threshold.
        stale_ts = time.time() - (DEFAULT_INBOX_STALE_DAYS + 2) * 86400
        import os
        os.utime(old, (stale_ts, stale_ts))
        report = build_rev_health_report("p6", programs_root=tmp_path)
        assert report.inbox is not None
        assert report.inbox.inbox_stale is True
        assert any("inbox_stale" in w for w in report.warnings)

    def test_quarantine_count_and_top_reasons(self, tmp_path: Path) -> None:
        inbox = tmp_path / "p7" / "rev_inbox"
        quarantine = inbox / "quarantine"
        quarantine.mkdir(parents=True)
        # 25 quarantined files, 20 with reason=size_exceeded, 5 with reason=parse_error
        for i in range(20):
            qf = quarantine / f"s{i}.eml"
            qf.write_text("x", encoding="utf-8")
            qf.with_suffix(".reason.txt").write_text(f"size_exceeded: {i} bytes", encoding="utf-8")
        for i in range(5):
            qf = quarantine / f"p{i}.eml"
            qf.write_text("x", encoding="utf-8")
            qf.with_suffix(".reason.txt").write_text("parse_error: bad mime", encoding="utf-8")
        report = build_rev_health_report("p7", programs_root=tmp_path)
        assert report.inbox is not None
        assert report.inbox.quarantine_file_count == 25
        assert report.inbox.quarantine_top_reasons[0][0] == "size_exceeded"
        assert report.inbox.quarantine_top_reasons[0][1] == 20
        assert any("quarantine_count" in w for w in report.warnings)

    def test_inbox_not_found_is_none(self, tmp_path: Path) -> None:
        report = build_rev_health_report("p8", programs_root=tmp_path)
        assert report.inbox is not None
        assert report.inbox.inbox_path is None  # canonical rev_inbox absent


class TestQualityFloorAndVault:
    def test_quality_floor_warn_after_ten_cycles_without_corpus(self, tmp_path: Path) -> None:
        _append_history(tmp_path, "p9", [
            {"correlation_id": f"c{i}", "stop_category": "complete",
             "candidates_staged": 1, "enumerated": 1, "llm_fallback_count": 0,
             "wall_clock_seconds": 1.0}
            for i in range(10)
        ])
        # No _quality/rev_labeled_corpus.jsonl present.
        report = build_rev_health_report("p9", programs_root=tmp_path)
        assert report.quality_floor_not_established_warn is True
        assert any("quality_floor_not_established" in w for w in report.warnings)

    def test_no_quality_floor_warn_when_corpus_present(self, tmp_path: Path) -> None:
        _append_history(tmp_path, "p10", [
            {"correlation_id": f"c{i}", "stop_category": "complete",
             "candidates_staged": 1, "enumerated": 1, "llm_fallback_count": 0,
             "wall_clock_seconds": 1.0}
            for i in range(10)
        ])
        qdir = tmp_path / "p10" / "_quality"
        qdir.mkdir(parents=True)
        (qdir / "rev_labeled_corpus.jsonl").write_text("{}", encoding="utf-8")
        report = build_rev_health_report("p10", programs_root=tmp_path)
        assert report.quality_floor_not_established_warn is False


class TestRendering:
    def test_human_render_surfaces_new_telemetry(self, tmp_path: Path) -> None:
        _write_last_cycle(tmp_path, "p11", {
            "schema_version": "1.0", "correlation_id": "c1",
            "stop_category": "complete", "candidates_staged": 2, "enumerated": 3,
            "llm_fallback_count": 0, "shield_degrade": True, "wall_clock_seconds": 7.0,
        })
        _append_history(tmp_path, "p11", [
            {"correlation_id": "c1", "stop_category": "complete", "candidates_staged": 2,
             "enumerated": 3, "llm_fallback_count": 0, "wall_clock_seconds": 7.0},
        ])
        report = build_rev_health_report("p11", programs_root=tmp_path)
        text = render_rev_health_human(report)
        assert "last cycle" in text
        assert "llm_fallback trend" in text
        # to_dict includes all new keys.
        d = report.to_dict()
        for key in (
            "last_cycle", "llm_fallback_trend", "inbox",
            "circuit_breaker_warn", "quality_floor_not_established_warn",
            "vault_size_warn", "warnings",
        ):
            assert key in d
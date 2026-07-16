"""ADF-W5.8 (Section 8.2.5): ``projection_lag`` cockpit wiring.

Verifies the best-effort emission helper in ``src/commands/cockpit.py`` reads
the PRE-update persisted ``latest.json``, detects lag, and emits an entity-
scoped alert. The detection logic itself is covered by
``test_projection_lag_detector.py``; these tests verify the wiring.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.commands.cockpit import _emit_projection_lag_alert_best_effort
from src.core.alerts import read_alerts
from src.core.reality_store import get_program_reality_db_path

_NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


def _write_latest(program_id: str, *, generated_at: datetime, programs_root: Path) -> None:
    cockpit_dir = programs_root / program_id / "runtime" / "cockpit"
    cockpit_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "program_id": program_id,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
    }
    (cockpit_dir / "latest.json").write_text(json.dumps(payload), encoding="utf-8")


def _touch_fact_store(program_id: str, *, at: datetime, programs_root: Path) -> None:
    db_path = get_program_reality_db_path(program_id, programs_root=programs_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text("x")
    ts = at.timestamp()
    os.utime(db_path, (ts, ts))


def _programs_root(tmp_path: Path) -> Path:
    """Isolate the fact-store resolution. ProgramFactStore resolves db_root as
    ``programs_root.parent / "vertex-db"``, so putting the program under a
    ``programs/`` subdir keeps the fact store inside the test's tmp_path and
    prevents cross-test contamination when the full sweep shares a temp parent."""
    programs_root = tmp_path / "programs"
    (programs_root / "xpf").mkdir(parents=True)
    return programs_root


def test_lagging_snapshot_emits_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    # Persisted projection is 3h old; fact store changed 1min ago -> lagging.
    _write_latest("xpf", generated_at=_NOW - timedelta(hours=3), programs_root=programs_root)
    _touch_fact_store("xpf", at=_NOW - timedelta(minutes=1), programs_root=programs_root)

    _emit_projection_lag_alert_best_effort("xpf", programs_root=programs_root)

    alerts = read_alerts("xpf", programs_root=programs_root)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.category == "projection_lag"
    assert alert.entity_type == "cockpit_snapshot"
    assert alert.entity_id == "xpf"
    assert alert.severity == "warn"
    assert "Projection lag" in alert.message
    assert "rebuild" in alert.next_command.lower()


def test_current_snapshot_emits_no_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    _write_latest("xpf", generated_at=_NOW, programs_root=programs_root)
    _touch_fact_store("xpf", at=_NOW - timedelta(minutes=5), programs_root=programs_root)

    _emit_projection_lag_alert_best_effort("xpf", programs_root=programs_root)
    assert read_alerts("xpf", programs_root=programs_root) == ()


def test_missing_latest_json_is_silent_noop(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    # No latest.json; nothing to compare against.
    _emit_projection_lag_alert_best_effort("xpf", programs_root=programs_root)
    assert read_alerts("xpf", programs_root=programs_root) == ()


def test_corrupt_latest_json_does_not_raise(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    cockpit_dir = programs_root / "xpf" / "runtime" / "cockpit"
    cockpit_dir.mkdir(parents=True)
    (cockpit_dir / "latest.json").write_text("{not valid json", encoding="utf-8")
    # Must not raise.
    _emit_projection_lag_alert_best_effort("xpf", programs_root=programs_root)
    assert read_alerts("xpf", programs_root=programs_root) == ()


def test_no_underlying_data_no_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    _write_latest("xpf", generated_at=_NOW - timedelta(days=5), programs_root=programs_root)
    # No fact store / run_telemetry present -> nothing to lag behind.
    _emit_projection_lag_alert_best_effort("xpf", programs_root=programs_root)
    assert read_alerts("xpf", programs_root=programs_root) == ()


def test_emission_is_idempotent_via_cooldown(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    _write_latest("xpf", generated_at=_NOW - timedelta(hours=3), programs_root=programs_root)
    _touch_fact_store("xpf", at=_NOW, programs_root=programs_root)

    _emit_projection_lag_alert_best_effort("xpf", programs_root=programs_root)
    first = read_alerts("xpf", programs_root=programs_root)
    assert len(first) == 1 and first[0].occurrence_count == 1
    # Second call within the cooldown window suppresses a fresh row.
    _emit_projection_lag_alert_best_effort("xpf", programs_root=programs_root)
    again = read_alerts("xpf", programs_root=programs_root)
    assert len(again) == 1
    assert again[0].suppressed_count >= 1

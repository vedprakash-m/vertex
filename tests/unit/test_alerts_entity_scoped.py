"""ADF-W5.8: entity-scoped alert severity, cooldown, suppressed counts,
owner resolution, and delivery (src/core/alerts.py additive extension)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.adf_config import AlertDelivery, AlertsConfig
from src.core.alerts import (
    AlertSeverity,
    append_or_suppress_alert,
    entity_scoped_alert_id,
    read_alerts,
)

_T0 = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)


def test_entity_scoped_alert_id_is_deterministic() -> None:
    a = entity_scoped_alert_id(program_id="xpf", category="outbox_dead_letter", entity_type="work_item", entity_id="123")
    b = entity_scoped_alert_id(program_id="xpf", category="outbox_dead_letter", entity_type="work_item", entity_id="123")
    assert a == b


def test_entity_scoped_alert_id_differs_by_entity() -> None:
    a = entity_scoped_alert_id(program_id="xpf", category="outbox_dead_letter", entity_type="work_item", entity_id="123")
    b = entity_scoped_alert_id(program_id="xpf", category="outbox_dead_letter", entity_type="work_item", entity_id="456")
    assert a != b


def test_first_detection_creates_a_fresh_unsuppressed_alert(tmp_path: Path) -> None:
    record = append_or_suppress_alert(
        program_id="xpf", category="outbox_dead_letter", entity_type="work_item", entity_id="123",
        severity=AlertSeverity.ERROR, message="Outbox entry dead-lettered", next_command="vertex ledger outbox --show",
        programs_root=tmp_path, now=_T0,
    )
    assert record.occurrence_count == 1
    assert record.suppressed_count == 0
    assert record.first_seen == _T0
    assert record.last_seen == _T0

    open_alerts = read_alerts("xpf", programs_root=tmp_path)
    assert len(open_alerts) == 1
    assert open_alerts[0].entity_id == "123"


def test_repeat_detection_within_cooldown_is_suppressed(tmp_path: Path) -> None:
    append_or_suppress_alert(
        program_id="xpf", category="outbox_dead_letter", entity_type="work_item", entity_id="123",
        severity=AlertSeverity.ERROR, message="first", next_command="cmd",
        programs_root=tmp_path, now=_T0, cooldown_minutes=60,
    )
    second = append_or_suppress_alert(
        program_id="xpf", category="outbox_dead_letter", entity_type="work_item", entity_id="123",
        severity=AlertSeverity.ERROR, message="second (should be suppressed)", next_command="cmd",
        programs_root=tmp_path, now=_T0 + timedelta(minutes=10), cooldown_minutes=60,
    )
    assert second.occurrence_count == 2
    assert second.suppressed_count == 1
    # The suppressed occurrence's message is NOT surfaced as a fresh notification.
    assert second.message == "first"


def test_repeat_detection_after_cooldown_is_a_fresh_notification(tmp_path: Path) -> None:
    append_or_suppress_alert(
        program_id="xpf", category="outbox_dead_letter", entity_type="work_item", entity_id="123",
        severity=AlertSeverity.ERROR, message="first", next_command="cmd",
        programs_root=tmp_path, now=_T0, cooldown_minutes=60,
    )
    second = append_or_suppress_alert(
        program_id="xpf", category="outbox_dead_letter", entity_type="work_item", entity_id="123",
        severity=AlertSeverity.CRITICAL, message="second, cooldown elapsed", next_command="cmd",
        programs_root=tmp_path, now=_T0 + timedelta(minutes=90), cooldown_minutes=60,
    )
    assert second.occurrence_count == 2
    assert second.suppressed_count == 0  # this occurrence was not suppressed
    assert second.message == "second, cooldown elapsed"
    assert second.severity == AlertSeverity.CRITICAL


def test_suppressed_count_carries_forward_as_running_total(tmp_path: Path) -> None:
    append_or_suppress_alert(
        program_id="xpf", category="c", entity_type="t", entity_id="1",
        severity=AlertSeverity.WARN, message="m", next_command="cmd",
        programs_root=tmp_path, now=_T0, cooldown_minutes=60,
    )
    append_or_suppress_alert(  # suppressed
        program_id="xpf", category="c", entity_type="t", entity_id="1",
        severity=AlertSeverity.WARN, message="m2", next_command="cmd",
        programs_root=tmp_path, now=_T0 + timedelta(minutes=10), cooldown_minutes=60,
    )
    third = append_or_suppress_alert(  # cooldown elapsed -- fresh
        program_id="xpf", category="c", entity_type="t", entity_id="1",
        severity=AlertSeverity.WARN, message="m3", next_command="cmd",
        programs_root=tmp_path, now=_T0 + timedelta(minutes=90), cooldown_minutes=60,
    )
    assert third.occurrence_count == 3
    assert third.suppressed_count == 1  # still 1 -- running total, not reset


def test_different_entities_are_independent(tmp_path: Path) -> None:
    append_or_suppress_alert(
        program_id="xpf", category="c", entity_type="work_item", entity_id="123",
        severity=AlertSeverity.WARN, message="m", next_command="cmd",
        programs_root=tmp_path, now=_T0,
    )
    append_or_suppress_alert(
        program_id="xpf", category="c", entity_type="work_item", entity_id="456",
        severity=AlertSeverity.WARN, message="m", next_command="cmd",
        programs_root=tmp_path, now=_T0,
    )
    open_alerts = read_alerts("xpf", programs_root=tmp_path)
    assert {a.entity_id for a in open_alerts} == {"123", "456"}
    assert all(a.occurrence_count == 1 for a in open_alerts)


def test_owner_carries_forward_when_not_re_supplied(tmp_path: Path) -> None:
    append_or_suppress_alert(
        program_id="xpf", category="c", entity_type="t", entity_id="1",
        severity=AlertSeverity.WARN, message="m", next_command="cmd",
        programs_root=tmp_path, now=_T0, owner="alex",
    )
    second = append_or_suppress_alert(
        program_id="xpf", category="c", entity_type="t", entity_id="1",
        severity=AlertSeverity.WARN, message="m2", next_command="cmd",
        programs_root=tmp_path, now=_T0 + timedelta(minutes=90), cooldown_minutes=60,
    )
    assert second.owner == "alex"


def test_default_cooldown_and_delivery_come_from_alerts_config(tmp_path: Path) -> None:
    config = AlertsConfig(delivery=AlertDelivery.DRAFT_EMAIL, cooldown_minutes=30)
    record = append_or_suppress_alert(
        program_id="xpf", category="c", entity_type="t", entity_id="1",
        severity=AlertSeverity.WARN, message="m", next_command="cmd",
        programs_root=tmp_path, now=_T0, alerts_config=config,
    )
    assert record.cooldown_minutes == 30
    assert record.delivery == "draft_email"


def test_explicit_cooldown_and_delivery_override_config(tmp_path: Path) -> None:
    config = AlertsConfig(delivery=AlertDelivery.DRAFT_EMAIL, cooldown_minutes=30)
    record = append_or_suppress_alert(
        program_id="xpf", category="c", entity_type="t", entity_id="1",
        severity=AlertSeverity.WARN, message="m", next_command="cmd",
        programs_root=tmp_path, now=_T0, alerts_config=config,
        cooldown_minutes=999, delivery="cockpit",
    )
    assert record.cooldown_minutes == 999
    assert record.delivery == "cockpit"


def test_legacy_alert_record_without_new_fields_still_parses(tmp_path: Path) -> None:
    # A pre-ADF-W5.8 alert row (no entity_type/owner/etc.) must still read cleanly.
    import json
    path = tmp_path / "programs" / "xpf" / "_alerts" / "alerts.jsonl"
    path.parent.mkdir(parents=True)
    legacy_row = {
        "schema_version": "1.0", "alert_id": "legacy-1", "program_id": "xpf",
        "severity": "warn", "category": "old_category", "message": "legacy alert",
        "next_command": "cmd", "created_at": "2026-01-01T00:00:00Z",
        "resolved_at": None, "context": {},
    }
    path.write_text(json.dumps(legacy_row) + "\n", encoding="utf-8")
    alerts = read_alerts("xpf", programs_root=tmp_path / "programs")
    assert len(alerts) == 1
    assert alerts[0].entity_type is None
    assert alerts[0].occurrence_count == 1
    assert alerts[0].suppressed_count == 0

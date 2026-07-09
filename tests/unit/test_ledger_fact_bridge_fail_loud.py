"""Track A regression tests (specs/fix-data-flow.md §6.1 / PR-5, PS-2).

Covers the three fail-loud signals this track adds around the ledger ->
fact-store bridge, plus the one pre-existing-but-untested behavior (§6.1 item
3 / Assumption A7):

1. `run_bridge_disabled_doctor` — proactive `vertex doctor --fact-bridge` WARN
   for any REV-configured program whose fact-bridge resolves disabled.
2. `run_bridge_failure_backlog_doctor` — reactive backlog WARN reading
   `bridge_failures.jsonl`.
3. `_warn_if_bridgeable_event_silenced_by_disabled_bridge` (invoked from
   `_maybe_bridge_event_to_fact_store`) — the point-in-time stderr warning
   fired the moment a bridgeable event is actually silenced.
4. The `PASSTHROUGH` disposition branch now logs at debug (previously a bare,
   unlogged `return`) — asserted via caplog.
5. `resolve_family_sor_mode`/`load_fact_sor_state` already raise `ConfigError`
   for an invalid mode string (Assumption A7) — this was previously an
   untested code path; locked in here.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.commands.doctor_checks.fact_store_flip_checks import (
    run_bridge_disabled_doctor,
    run_bridge_failure_backlog_doctor,
)
from src.commands.ledger import (
    _maybe_bridge_event_to_fact_store,
    load_bridge_failures,
)
from src.core.exceptions import ConfigError
from src.core.fact_sor_state import load_fact_sor_state
from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence
from src.core.ledger.source_refs import EmailRef

NOW = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)


def _write_program_yaml(programs_root: Path, program_id: str, *, rev_block: str | None) -> None:
    body = f"id: {program_id}\nname: Test Program\n"
    if rev_block is not None:
        body += f"m365:\n  enabled: true\n  rev:\n{rev_block}\n"
    path = programs_root / program_id / "program.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _email_ref(*, message_id: str) -> EmailRef:
    return EmailRef(
        subject="Status update",
        sent_at=NOW,
        sender="pm@example.com",
        message_id=message_id,
        vault_hash="sha256:vault-email-1",
    )


def _milestone_event(*, event_id: str = "evt-fb-1") -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        program_id="acme",
        event_type="milestone.completed.v1",
        occurred_at=NOW,
        recorded_at=NOW,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="rev-mail",
        payload={"milestone_id": "milestone:m1", "completed_on": "2026-07-01"},
        source_ref=_email_ref(message_id=f"{event_id}@example.com"),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )


def _passthrough_event(*, event_id: str = "evt-passthrough-1") -> EventEnvelope:
    # discovery.* is a PASSTHROUGH-disposition prefix (lifecycle/internal, no
    # bridge appender) per src/core/ledger/event_type_registry.py.
    return EventEnvelope(
        event_id=event_id,
        program_id="acme",
        event_type="discovery.candidate_approved.v1",
        occurred_at=NOW,
        recorded_at=NOW,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={},
        source_ref=_email_ref(message_id=f"{event_id}@example.com"),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )


# ---------------------------------------------------------------------------
# 1. run_bridge_disabled_doctor — proactive doctor WARN
# ---------------------------------------------------------------------------


def test_bridge_disabled_doctor_is_ok_when_rev_not_configured(tmp_path: Path) -> None:
    """Minimal failing input this guards: a program with no `m365.rev` block
    at all — the check must not WARN, since there is nothing to bridge."""
    programs_root = tmp_path / "programs"
    _write_program_yaml(programs_root, "acme", rev_block=None)

    report = run_bridge_disabled_doctor(
        edition_name="acme_weekly", program_id="acme", programs_root=programs_root,
    )

    check = report.checks[0]
    assert check.label == "Fact Bridge"
    assert check.status == "ok"
    assert check.metadata is not None
    assert check.metadata["rev_configured"] is False


def test_bridge_disabled_doctor_warns_when_rev_configured_but_bridge_off(tmp_path: Path) -> None:
    """PS-2's headline gap: REV is configured (`m365.rev` present) but
    `fact_bridge_enabled` is absent/False — must WARN, not silently pass."""
    programs_root = tmp_path / "programs"
    _write_program_yaml(
        programs_root, "acme",
        rev_block="    profile: legacy_nl\n    fact_bridge_enabled: false\n",
    )

    report = run_bridge_disabled_doctor(
        edition_name="acme_weekly", program_id="acme", programs_root=programs_root,
    )

    check = report.checks[0]
    assert check.status == "warn"
    assert check.metadata is not None
    assert check.metadata["rev_configured"] is True
    assert check.metadata["fact_bridge_enabled"] is False
    assert "fact_bridge_enabled: true" in check.detail


def test_bridge_disabled_doctor_is_ok_when_bridge_explicitly_enabled(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_yaml(
        programs_root, "acme",
        rev_block="    profile: legacy_nl\n    fact_bridge_enabled: true\n",
    )

    report = run_bridge_disabled_doctor(
        edition_name="acme_weekly", program_id="acme", programs_root=programs_root,
    )

    check = report.checks[0]
    assert check.status == "ok"
    assert check.metadata is not None
    assert check.metadata["fact_bridge_enabled"] is True


def test_bridge_disabled_doctor_respects_env_var_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    _write_program_yaml(
        programs_root, "acme",
        rev_block="    profile: legacy_nl\n    fact_bridge_enabled: false\n",
    )
    monkeypatch.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")

    report = run_bridge_disabled_doctor(
        edition_name="acme_weekly", program_id="acme", programs_root=programs_root,
    )

    assert report.checks[0].status == "ok"


# ---------------------------------------------------------------------------
# 2. run_bridge_failure_backlog_doctor — reactive backlog WARN
# ---------------------------------------------------------------------------


def test_bridge_failure_backlog_is_ok_when_no_failures_recorded(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    report = run_bridge_failure_backlog_doctor(
        edition_name="acme_weekly", program_id="acme", programs_root=programs_root,
    )
    check = report.checks[0]
    assert check.label == "Fact Bridge Failure Backlog"
    assert check.status == "ok"
    assert check.metadata is not None
    assert check.metadata["failure_count"] == 0


def test_bridge_failure_backlog_ok_below_threshold(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "ledger" / "bridge_failures.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"event_id": "evt-1", "program_id": "acme"}) + "\n",
        encoding="utf-8",
    )

    report = run_bridge_failure_backlog_doctor(
        edition_name="acme_weekly", program_id="acme", programs_root=programs_root, threshold=3,
    )
    check = report.checks[0]
    assert check.status == "ok"
    assert check.metadata is not None
    assert check.metadata["repeatedly_failing_event_count"] == 0


def test_bridge_failure_backlog_warns_at_threshold(tmp_path: Path) -> None:
    """Minimal failing input: the same event_id fails bridging >= threshold
    times across replays — the exact "enabled but silently broken" gap this
    check exists to surface (PS-2's v1.1 runtime addition)."""
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "ledger" / "bridge_failures.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"event_id": "evt-flaky", "program_id": "acme"}) for _ in range(3)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = run_bridge_failure_backlog_doctor(
        edition_name="acme_weekly", program_id="acme", programs_root=programs_root, threshold=3,
    )
    check = report.checks[0]
    assert check.status == "warn"
    assert check.metadata is not None
    assert check.metadata["repeatedly_failing_event_count"] == 1


# ---------------------------------------------------------------------------
# 3 & 4. Reactive stderr warning + PASSTHROUGH debug logging, exercised via
# _maybe_bridge_event_to_fact_store directly (the real call site).
# ---------------------------------------------------------------------------


def test_reactive_warning_fires_on_first_silenced_bridgeable_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture,
) -> None:
    """PS-2 §6.1 step 1: the reactive, point-in-time warning at the exact
    silent-failure site — a PROJECTABLE event (milestone.completed.v1) is
    persisted while the bridge is disabled for a REV-configured program."""
    programs_root = tmp_path / "programs"
    _write_program_yaml(
        programs_root, "acme",
        rev_block="    profile: legacy_nl\n    fact_bridge_enabled: false\n",
    )

    with caplog.at_level(logging.WARNING):
        _maybe_bridge_event_to_fact_store(_milestone_event(), programs_root=programs_root)

    captured = capsys.readouterr()
    assert "DISABLED" in captured.err
    assert "milestone.completed.v1" in captured.err
    assert any("DISABLED" in record.message for record in caplog.records)


def test_reactive_warning_does_not_fire_when_rev_not_configured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """No REV config at all means nothing was ever going to bridge — the
    warning must stay silent, not false-positive on every ledger write."""
    programs_root = tmp_path / "programs"
    _write_program_yaml(programs_root, "acme", rev_block=None)

    _maybe_bridge_event_to_fact_store(_milestone_event(), programs_root=programs_root)

    captured = capsys.readouterr()
    assert captured.err == ""


def test_passthrough_branch_logs_at_debug_not_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """PS-2 v1.1 addition: the PASSTHROUGH branch (ledger.py) was a bare
    `return` with zero logging, structurally identical to the disabled-bridge
    silent-failure case. Minimal failing input: a `discovery.*`-prefixed
    (PASSTHROUGH-disposition) event persisted with the bridge enabled."""
    programs_root = tmp_path / "programs"
    _write_program_yaml(
        programs_root, "acme",
        rev_block="    profile: legacy_nl\n    fact_bridge_enabled: true\n",
    )

    with caplog.at_level(logging.DEBUG, logger="src.commands.ledger"):
        _maybe_bridge_event_to_fact_store(_passthrough_event(), programs_root=programs_root)

    assert any(
        "PASSTHROUGH" in record.message and record.levelno == logging.DEBUG
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# 5. Bridge-failure persistence round trip (_record_bridge_failure is private;
# exercised indirectly via load_bridge_failures + a hand-written JSONL line,
# matching how the doctor check itself reads it).
# ---------------------------------------------------------------------------


def test_load_bridge_failures_returns_empty_list_when_file_absent(tmp_path: Path) -> None:
    assert load_bridge_failures("acme", programs_root=tmp_path / "programs") == []


def test_load_bridge_failures_tolerates_malformed_trailing_line(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "ledger" / "bridge_failures.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"event_id": "evt-1"}) + "\n" + "{not valid json\n",
        encoding="utf-8",
    )

    records = load_bridge_failures("acme", programs_root=programs_root)
    assert len(records) == 1
    assert records[0]["event_id"] == "evt-1"


# ---------------------------------------------------------------------------
# 6. Invalid SoR mode string (Assumption A7 / §6.1 item 3): already-correct
# behavior, previously untested. Locks in the ConfigError contract.
# ---------------------------------------------------------------------------


def test_load_fact_sor_state_rejects_invalid_program_level_mode(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "fact_store_sor.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({
            "schema_version": "2.0",
            "mode": "not-a-real-mode",
            "recorded_at": NOW.isoformat(),
        }),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="legacy, shadow, or primary"):
        load_fact_sor_state("acme", programs_root=programs_root)


def test_load_fact_sor_state_rejects_invalid_family_mode_string(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "fact_store_sor.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({
            "schema_version": "2.0",
            "mode": "legacy",
            "recorded_at": NOW.isoformat(),
            "family_modes": {"workitem.state": "not-a-real-mode"},
        }),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="legacy, shadow, or primary"):
        load_fact_sor_state("acme", programs_root=programs_root)

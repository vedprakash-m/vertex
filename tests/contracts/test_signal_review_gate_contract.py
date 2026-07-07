from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.core.models import Confidence
from src.core.models_v2 import Signal, SignalReviewDecision
from src.core.sqlite_stores import SQLiteSignalStore
from src.core.stages import validation_stage as validation_stage_module


def test_inv5_only_approved_signals_within_evidence_window_feed_publish_gates(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    signal_store = SQLiteSignalStore(programs_root=programs_root)
    monkeypatch.setattr(
        validation_stage_module,
        "build_signal_store_for_program_id",
        lambda program_id, *, programs_root: signal_store,
    )

    signal_store.append(
        Signal(
            id="approved-in-window",
            timestamp=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="demo",
            workstream_id="ws-1",
            entity_refs=("WI:1001",),
            text="Approved evidence inside the active gate window.",
            raw_ref="WI:1001",
            confidence=Confidence.HIGH,
        )
    )
    signal_store.append_review(
        "demo",
        SignalReviewDecision(
            signal_id="approved-in-window",
            decision="approved",
            reviewed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
            reviewed_by="reviewer",
        ),
    )

    signal_store.append(
        Signal(
            id="deferred-in-window",
            timestamp=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="demo",
            workstream_id="ws-1",
            entity_refs=("WI:1002",),
            text="Deferred evidence must not reach publish gates.",
            raw_ref="WI:1002",
            confidence=Confidence.MEDIUM,
        )
    )
    signal_store.append_review(
        "demo",
        SignalReviewDecision(
            signal_id="deferred-in-window",
            decision="deferred",
            reviewed_at=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
            reviewed_by="reviewer",
        ),
    )

    signal_store.append(
        Signal(
            id="approved-out-of-window",
            timestamp=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="demo",
            workstream_id="ws-1",
            entity_refs=("WI:1003",),
            text="Approved but stale evidence must fall out of the gate window.",
            raw_ref="WI:1003",
            confidence=Confidence.HIGH,
        )
    )
    signal_store.append_review(
        "demo",
        SignalReviewDecision(
            signal_id="approved-out-of-window",
            decision="approved",
            reviewed_at=datetime(2026, 5, 1, 10, 5, tzinfo=timezone.utc),
            reviewed_by="reviewer",
        ),
    )

    ctx = SimpleNamespace(
        bundle=SimpleNamespace(config=SimpleNamespace(ado=SimpleNamespace(date_window_days=14))),
        resolved_v2=SimpleNamespace(program=SimpleNamespace(id="demo")),
        programs_root=programs_root,
        data_as_of=datetime(2026, 5, 21, 18, 0, tzinfo=timezone.utc),
    )

    journal_signals, approved_signals = validation_stage_module._load_gate_signals(ctx)

    assert {signal.id for signal in journal_signals} == {
        "approved-in-window",
        "deferred-in-window",
        "approved-out-of-window",
    }
    assert [signal.id for signal in approved_signals] == ["approved-in-window"]
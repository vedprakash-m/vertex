"""ME-02: Transcript-based evidence extraction stage."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import pytest

from src.core.models_v2 import Confidence, Signal


def _make_transcript_signal(lane_id: str, content: str) -> Signal:
    return Signal(
        id=f"sig-{lane_id}",
        timestamp=datetime(2026, 6, 17, 18, 0, 0, tzinfo=timezone.utc),
        source="workiq/transcript",
        program_id="acme",
        workstream_id=lane_id,
        entity_refs=(),
        text=content,
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"source_type": "transcript", "message_id": "meet-001"},
    )


def test_extraction_produces_evidence(tmp_path: Path) -> None:
    """Transcript signal with enough content → WorkstreamEvidence with confidence > 0."""
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage

    content = (
        "Executive Reader: We have to go back and report to leadership and partner teams. "
        "The Acme ramp P1 blocker is SCHIE. ADO 36923425 is the burn-in indicator. "
        "Risk: High. ETA is June 19. Owner Name is the DRI. "
        "Next step: resolve ADO 37982484 before July 12. "
        "Status: ramp still blocked, no go/no-go decision made yet."
    )
    sigs = (_make_transcript_signal("acme.schie_gaps", content),)

    ai_calls: list[str] = []
    fake_response = json.dumps({
        "risk_level": "HIGH",
        "etas": [{"label": "ADO 36923425", "eta_date": "2026-06-19", "owner": "Vaishali Mathur"}],
        "blocking_items": ["ADO:36923425"],
        "owners": ["Vaishali Mathur"],
        "narrative_summary": "SCHIE ramp blocker, ETA June 19.",
        "confidence": 0.85,
    })

    def mock_ask(prompt: str) -> str:
        ai_calls.append(prompt)
        return fake_response

    results = run_evidence_extraction_stage(
        workiq_signals=sigs,
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=mock_ask,
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
        dry_run=False,
    )
    assert "acme.schie_gaps" in results
    ev = results["acme.schie_gaps"]
    assert ev.confidence > 0.0
    assert len(ai_calls) == 1  # exactly one AI call per lane


def test_email_signals_excluded(tmp_path: Path) -> None:
    """Email signals are skipped regardless of content."""
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage

    email_sig = Signal(
        id="sig-email",
        timestamp=datetime(2026, 6, 17, 18, 0, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="acme.networking",
        entity_refs=(),
        text="[Contoso] DD PF Pilot – Scorecard & Open Items (06/03/2026) High risk Performance blocking",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"source_type": "email", "message_id": "msg-001"},
    )
    ai_calls: list[str] = []
    results = run_evidence_extraction_stage(
        workiq_signals=(email_sig,),
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=lambda p: (ai_calls.append(p), "{}")[1],
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
    )
    assert results == {}
    assert ai_calls == []  # no AI calls for email signals


def test_short_transcript_skipped(tmp_path: Path) -> None:
    """Transcript with fewer than _MIN_TRANSCRIPT_CHARS is skipped."""
    from src.commands.gather_pipeline.evidence_extraction_stage import (
        run_evidence_extraction_stage, _MIN_TRANSCRIPT_CHARS,
    )
    short = _make_transcript_signal("acme.lso", "Short transcript." * 3)
    assert len(short.text) < _MIN_TRANSCRIPT_CHARS
    ai_calls: list[str] = []
    results = run_evidence_extraction_stage(
        workiq_signals=(short,),
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=lambda p: (ai_calls.append(p), "{}")[1],
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
    )
    assert results == {}
    assert ai_calls == []


def test_evidence_persisted_to_jsonl(tmp_path: Path) -> None:
    """With dry_run=False, evidence_store.jsonl is written."""
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage
    content = "Risk: High. ADO 36923425 burn-in. " * 20  # ensure > 200 chars
    sigs = (_make_transcript_signal("acme.schie_gaps", content),)
    fake_response = json.dumps({
        "risk_level": "HIGH", "etas": [], "blocking_items": [], "owners": [],
        "confidence": 0.75,
    })
    run_evidence_extraction_stage(
        workiq_signals=sigs,
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=lambda _: fake_response,
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
        dry_run=False,
    )
    store = tmp_path / "acme" / "journal" / "evidence_store.jsonl"
    assert store.exists()
    record = json.loads(store.read_text(encoding="utf-8").strip())
    assert record["lane_id"] == "acme.schie_gaps"
    assert record["confidence"] > 0.0


def test_dry_run_skips_persistence(tmp_path: Path) -> None:
    """dry_run=True → WorkstreamEvidence returned but not written to disk."""
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage
    content = "Risk: High. ADO 36923425 burn-in. " * 20
    sigs = (_make_transcript_signal("acme.schie_gaps", content),)
    fake_response = json.dumps({"risk_level": "HIGH", "etas": [], "blocking_items": [], "owners": [], "confidence": 0.7})
    results = run_evidence_extraction_stage(
        workiq_signals=sigs,
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=lambda _: fake_response,
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
        dry_run=True,
    )
    assert "acme.schie_gaps" in results
    store = tmp_path / "acme" / "journal" / "evidence_store.jsonl"
    assert not store.exists()


def test_ai_failure_does_not_propagate(tmp_path: Path) -> None:
    """AI failure for one lane must not crash the stage or affect other lanes."""
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage
    content = "Risk: High. ADO 36923425 burn-in. " * 20
    sigs = (
        _make_transcript_signal("acme.schie_gaps", content),
        _make_transcript_signal("acme.networking", content),
    )
    call_count = [0]

    def flaky_ask(prompt: str) -> str:
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("AI unavailable")
        return json.dumps({"risk_level": "LOW", "etas": [], "blocking_items": [], "owners": [], "confidence": 0.6})

    results = run_evidence_extraction_stage(
        workiq_signals=sigs,
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=flaky_ask,
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
    )
    # One lane succeeds, one is skipped — no crash
    assert len(results) == 1


def test_load_evidence_store_last_wins(tmp_path: Path) -> None:
    """When two records exist for the same lane, the last one wins."""
    import json
    from src.commands.doctor_checks.context_checks import _load_evidence_store
    store = tmp_path / "journal" / "evidence_store.jsonl"
    store.parent.mkdir(parents=True)
    r1 = {"lane_id": "acme.networking", "synthesized_at": "2026-06-10T00:00:00+00:00",
          "risk_level": "medium", "etas": [], "blocking_items": [], "owners": [],
          "raw_excerpts": [], "confidence": 0.6, "narrative_summary": "old"}
    r2 = {"lane_id": "acme.networking", "synthesized_at": "2026-06-17T00:00:00+00:00",
          "risk_level": "high", "etas": [], "blocking_items": [], "owners": [],
          "raw_excerpts": [], "confidence": 0.85, "narrative_summary": "new"}
    store.write_text(json.dumps(r1) + "\n" + json.dumps(r2) + "\n", encoding="utf-8")
    result = _load_evidence_store(tmp_path)
    assert result["acme.networking"].confidence == 0.85
    assert result["acme.networking"].narrative_summary == "new"

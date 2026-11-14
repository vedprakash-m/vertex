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
        "The Acme ramp P1 blocker is EXAMPLETEAM. ADO 900108 is the burn-in indicator. "
        "Risk: High. ETA is June 19. Owner Name is the DRI. "
        "Next step: resolve ADO 900113 before July 12. "
        "Status: ramp still blocked, no go/no-go decision made yet."
    )
    sigs = (_make_transcript_signal("acme.exampleteam_gaps", content),)

    ai_calls: list[str] = []
    fake_response = json.dumps({
        "risk_level": "HIGH",
        "etas": [{"label": "ADO 900108", "eta_date": "2026-06-19", "owner": "Example Person 3"}],
        "blocking_items": ["ADO:900108"],
        "owners": ["Example Person 3"],
        "narrative_summary": "EXAMPLETEAM ramp blocker, ETA June 19.",
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
    assert "acme.exampleteam_gaps" in results
    ev = results["acme.exampleteam_gaps"]
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
        text="[Contoso] EXD EXN Pilot – Scorecard & Open Items (06/03/2026) High risk Performance blocking",
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
    content = "Risk: High. ADO 900108 burn-in. " * 20  # ensure > 200 chars
    sigs = (_make_transcript_signal("acme.exampleteam_gaps", content),)
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
    assert record["lane_id"] == "acme.exampleteam_gaps"
    assert record["confidence"] > 0.0


def test_dry_run_skips_persistence(tmp_path: Path) -> None:
    """dry_run=True → WorkstreamEvidence returned but not written to disk."""
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage
    content = "Risk: High. ADO 900108 burn-in. " * 20
    sigs = (_make_transcript_signal("acme.exampleteam_gaps", content),)
    fake_response = json.dumps({"risk_level": "HIGH", "etas": [], "blocking_items": [], "owners": [], "confidence": 0.7})
    results = run_evidence_extraction_stage(
        workiq_signals=sigs,
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=lambda _: fake_response,
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
        dry_run=True,
    )
    assert "acme.exampleteam_gaps" in results
    store = tmp_path / "acme" / "journal" / "evidence_store.jsonl"
    assert not store.exists()


@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
def test_transcript_missing_source_refs_does_not_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, dry_run: bool,
) -> None:
    from src.ai.content_extractor import ContentExtractionAgent
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage
    from src.core.evidence_models import WorkstreamEvidence
    from src.core.models import RiskLevel

    evidence = WorkstreamEvidence(
        lane_id="synthetic", synthesized_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
        risk_level=RiskLevel.LOW, etas=(), blocking_items=(), owners=(), source_refs=(),
        raw_excerpts=(), confidence=0.8, narrative_summary="Synthetic update",
    )
    monkeypatch.setattr(ContentExtractionAgent, "extract", lambda self, ctx: evidence)

    results = run_evidence_extraction_stage(
        workiq_signals=(_make_transcript_signal("synthetic", "Synthetic transcript. " * 20),),
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=lambda _: "{}",
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
        dry_run=dry_run,
    )

    assert results == {}
    assert not (tmp_path / "acme" / "journal" / "evidence_store.jsonl").exists()
    assert not (tmp_path / "acme" / "journal" / "evidence_quality.jsonl").exists()
    quarantine = tmp_path / "acme" / "journal" / "evidence_quarantine.jsonl"
    if dry_run:
        assert not quarantine.exists()
    else:
        assert "missing_source_identity" in quarantine.read_text(encoding="utf-8")
    assert "missing_source_identity" in caplog.text


@pytest.mark.parametrize(
    ("excerpt", "reason"),
    [
        ("password=synthetic-secret", "password"),
        ("api_key=synthetic-secret", "client_secret"),
        ("do not distribute", "sensitivity_marking"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
def test_transcript_quarantine_is_excluded_from_results_and_quality(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, excerpt: str, reason: str, dry_run: bool,
) -> None:
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage

    response = json.dumps({
        "risk_level": "HIGH", "etas": [], "blocking_items": [], "owners": [],
        "raw_excerpts": [excerpt], "confidence": 0.8,
    })
    results = run_evidence_extraction_stage(
        workiq_signals=(_make_transcript_signal("unsafe", "Synthetic transcript. " * 20),),
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=lambda _: response,
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
        dry_run=dry_run,
    )

    assert results == {}
    journal = tmp_path / "acme" / "journal"
    assert not (journal / "evidence_store.jsonl").exists()
    assert not (journal / "evidence_quality.jsonl").exists()
    quarantine = journal / "evidence_quarantine.jsonl"
    if dry_run:
        assert not quarantine.exists()
    else:
        record = json.loads(quarantine.read_text(encoding="utf-8"))
        assert record["signal_types"] == [reason]
        assert excerpt not in quarantine.read_text(encoding="utf-8")
    assert reason in caplog.text
    assert excerpt not in caplog.text


@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
def test_transcript_results_use_scrubbed_evidence_and_preserve_safe_duplicates(
    tmp_path: Path, dry_run: bool,
) -> None:
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage
    from src.core.evidence_models import WorkstreamEvidence
    from src.core.evidence_quality import load_evidence_quality
    from src.core.evidence_store import load_approved_evidence_by_lane, load_evidence_records

    signal = _make_transcript_signal("safe", "Contact owner@example.net for synthetic status. " * 8)
    response = json.dumps({
        "risk_level": "HIGH",
        "etas": [{"label": "Contact owner@example.net", "eta_date": "2026-06-19", "owner": "owner@example.net"}],
        "blocking_items": ["ADO:12345"],
        "owners": ["owner@example.net"],
        "raw_excerpts": ["Contact owner@example.net for synthetic status."],
        "confidence": 0.8,
    })

    def run() -> dict[str, WorkstreamEvidence]:
        return run_evidence_extraction_stage(
            workiq_signals=(signal,),
            program_id="acme",
            programs_root=tmp_path,
            ask_ai_fn=lambda _: response,
            as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
            dry_run=dry_run,
        )

    first, duplicate = run(), run()
    for result in (first, duplicate):
        assert set(result) == {"safe"}
        evidence = result["safe"]
        assert evidence.privacy_classification == "confidential"
        assert evidence.owners == ("[PII-FILTERED-EMAIL]",)
        assert evidence.etas[0].owner == "[PII-FILTERED-EMAIL]"
        assert "owner@example.net" not in repr(evidence)
        assert "[PII-FILTERED-EMAIL]" in evidence.narrative_summary
        assert "[PII-FILTERED-EMAIL]" in evidence.source_refs[0].description

    records = load_evidence_records("acme", programs_root=tmp_path)
    quality = load_evidence_quality("acme", programs_root=tmp_path)
    if dry_run:
        assert records == ()
        assert quality == []
        assert not (tmp_path / "acme").exists()
    else:
        assert len(records) == 1
        assert records[0].evidence == first["safe"]
        assert records[0].backing_signal_ids == (signal.id,)
        assert len(quality) == 2
        assert all(record.lane_id == "safe" for record in quality)
        assert load_approved_evidence_by_lane(
            "acme", programs_root=tmp_path, approved_signal_ids=set(),
        ) == {}
        assert set(load_approved_evidence_by_lane(
            "acme", programs_root=tmp_path, approved_signal_ids={signal.id},
        )) == {"safe"}


@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
def test_transcript_quarantine_does_not_exclude_other_lanes(tmp_path: Path, dry_run: bool) -> None:
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage
    from src.core.evidence_quality import load_evidence_quality
    from src.core.evidence_store import load_evidence_records

    signals = (
        _make_transcript_signal("unsafe", "Synthetic unsafe transcript. " * 20),
        _make_transcript_signal("safe", "Synthetic safe transcript. " * 20),
    )

    def extract(prompt: str) -> str:
        excerpt = "password=synthetic-secret" if "'unsafe'" in prompt else "Safe synthetic update"
        return json.dumps({
            "risk_level": "LOW", "etas": [], "blocking_items": [], "owners": [],
            "raw_excerpts": [excerpt], "confidence": 0.8,
        })

    results = run_evidence_extraction_stage(
        workiq_signals=signals,
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=extract,
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
        dry_run=dry_run,
    )

    assert set(results) == {"safe"}
    records = load_evidence_records("acme", programs_root=tmp_path)
    quality = load_evidence_quality("acme", programs_root=tmp_path)
    quarantine = tmp_path / "acme" / "journal" / "evidence_quarantine.jsonl"
    if dry_run:
        assert records == ()
        assert quality == []
        assert not quarantine.exists()
    else:
        assert [record.evidence.lane_id for record in records] == ["safe"]
        assert [record.lane_id for record in quality] == ["safe"]
        assert json.loads(quarantine.read_text(encoding="utf-8"))["lane_id"] == "unsafe"


def test_ai_failure_does_not_propagate(tmp_path: Path) -> None:
    """AI failure for one lane must not crash the stage or affect other lanes."""
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage
    content = "Risk: High. ADO 900108 burn-in. " * 20
    sigs = (
        _make_transcript_signal("acme.exampleteam_gaps", content),
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


@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
@pytest.mark.parametrize("failure", ["provider", "confidence"])
def test_extraction_diagnostic_privacy_transcript_failure_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, dry_run: bool, failure: str,
) -> None:
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage
    from src.core.evidence_store import load_evidence_records

    marker = "synthetic-private-transcript-7f2"
    calls: list[str] = []

    def ask(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            if failure == "provider":
                raise RuntimeError(marker)
            return json.dumps({"risk_level": "low", "confidence": marker})
        return json.dumps({"risk_level": "low", "confidence": 0.8, "raw_excerpts": ["Safe update"]})

    result = run_evidence_extraction_stage(
        workiq_signals=(
            _make_transcript_signal("bad", "Synthetic transcript. " * 20),
            _make_transcript_signal("good", "Synthetic transcript. " * 20),
        ),
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=ask,
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
        dry_run=dry_run,
    )

    assert set(result) == {"good"}
    assert len(calls) == 2
    assert "ContentExtractionAgent failed for lane bad" in caplog.text
    assert marker not in caplog.text
    records = load_evidence_records("acme", programs_root=tmp_path)
    assert [record.evidence.lane_id for record in records] == ([] if dry_run else ["good"])


@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
@pytest.mark.parametrize("confidence", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_transcript_confidence_is_not_admitted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, dry_run: bool, confidence: str,
) -> None:
    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage
    from src.core.evidence_store import load_evidence_records

    calls: list[str] = []
    marker = "synthetic-private-excerpt"

    def ask(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return f'{{"risk_level":"low","confidence":{confidence},"raw_excerpts":["{marker}"]}}'
        return '{"risk_level":"low","confidence":0.8,"raw_excerpts":["Safe update"]}'

    result = run_evidence_extraction_stage(
        workiq_signals=(
            _make_transcript_signal("bad", "Synthetic transcript. " * 20),
            _make_transcript_signal("good", "Synthetic transcript. " * 20),
        ),
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=ask,
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
        dry_run=dry_run,
    )

    assert set(result) == {"good"}
    assert len(calls) == 2
    assert "ContentExtractionAgent failed for lane bad" in caplog.text
    assert marker not in caplog.text
    records = load_evidence_records("acme", programs_root=tmp_path)
    assert [record.evidence.lane_id for record in records] == ([] if dry_run else ["good"])


def test_extraction_diagnostic_privacy_transcript_quality_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from unittest.mock import MagicMock

    from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage
    from src.core.evidence_store import load_evidence_records

    marker = "synthetic-private-quality-7f2"
    quality = MagicMock(side_effect=[OSError(marker), None])
    monkeypatch.setattr("src.core.evidence_quality.record_evidence_quality", quality)

    result = run_evidence_extraction_stage(
        workiq_signals=(
            _make_transcript_signal("first", "Synthetic transcript. " * 20),
            _make_transcript_signal("second", "Synthetic transcript. " * 20),
        ),
        program_id="acme",
        programs_root=tmp_path,
        ask_ai_fn=lambda _: json.dumps({"risk_level": "low", "confidence": 0.8}),
        as_of=datetime(2026, 6, 17, tzinfo=timezone.utc),
    )

    assert set(result) == {"first", "second"}
    assert quality.call_count == 2
    assert len(load_evidence_records("acme", programs_root=tmp_path)) == 2
    assert "Failed to record evidence quality for lane first" in caplog.text
    assert marker not in caplog.text


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

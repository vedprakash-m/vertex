from __future__ import annotations

import json
import traceback
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import typer
import yaml
from click.testing import Result
from typer.testing import CliRunner

from src.commands import enrich
from src.commands.enrich import _record_workiq_lane_result as _real_record_lane_result
from src.core.evidence_models import EtaRecord, SourceRef, WorkstreamEvidence
from src.core.evidence_store import load_approved_evidence_by_lane, load_evidence_records
from src.core.journal import read_signals
from src.core.models import Enrichment, RiskLevel
from src.core.models_v2 import ReviewPolicy
from src.core.signal_review import default_review_policy
from src.core.workiq_freshness import mark_workiq_freshness_success as _real_mark_freshness


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    registry = tmp_path / "synthetic" / "workstream_registry.yaml"
    registry.parent.mkdir()
    registry.write_text(
        yaml.safe_dump({
            "workstreams": [{
                "id": "lane-a",
                "name": "Synthetic lane",
                "signal_sources": {
                    "teams_meeting_series": [{"display_name": "Synthetic meeting"}],
                },
                "workiq_latest": "previous update",
                "last_reviewed_date": "2020-01-01",
            }],
        }),
        encoding="utf-8",
    )
    bridge = MagicMock()
    bridge.probe.return_value = SimpleNamespace(available=True, has_workiq=True)
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="synthetic"),
        program=SimpleNamespace(m365=None),
    )
    freshness = MagicMock()
    monkeypatch.setattr(enrich, "_PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(enrich, "resolve_edition", lambda *args, **kwargs: resolved)
    monkeypatch.setattr(enrich, "load_program_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(enrich, "_build_workiq_cost_guard", lambda **kwargs: None)
    monkeypatch.setattr(enrich, "_build_workiq_breaker", lambda **kwargs: None)
    monkeypatch.setattr(enrich, "_is_workiq_lane_suspended", lambda **kwargs: False)
    monkeypatch.setattr(enrich, "_record_workiq_lane_result", lambda **kwargs: None)
    monkeypatch.setattr("src.m365.agency_bridge.AgencyBridge", lambda **kwargs: bridge)
    monkeypatch.setattr("src.core.workiq_freshness.mark_workiq_freshness_success", freshness)
    return SimpleNamespace(
        root=tmp_path, registry=registry, bridge=bridge, resolved=resolved,
        freshness=freshness, monkeypatch=monkeypatch,
    )


def _response(
    summary: str,
    *,
    eta_label: str = "Ship",
    eta_owner: object = "owner",
) -> str:
    return json.dumps({
        "risk_level": "low",
        "etas": [{
            "label": eta_label, "eta_date": "2026-07-01", "owner": eta_owner,
            "status": "open", "ado_id": "1234567890",
        }],
        "blocking_items": ["ADO:12345"],
        "owners": [],
        "raw_excerpts": [summary],
        "confidence": 0.8,
    })


def _run(*, accept: bool = False, dry_run: bool = False) -> None:
    enrich.enrich_command(
        edition="synthetic", lane=None, since="7d", dry_run=dry_run,
        accept=accept, output_format="human", batch=False,
    )


def _thread_evidence(summary: str) -> WorkstreamEvidence:
    return WorkstreamEvidence(
        lane_id="lane-a",
        synthesized_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
        risk_level=RiskLevel.LOW,
        etas=(EtaRecord("Ship", date(2026, 7, 1), "owner", "open", "1234567890"),),
        blocking_items=("ADO:12345",),
        owners=(),
        source_refs=(SourceRef(
            "workiq_email", "Synthetic source", date(2026, 6, 20), None,
            canonical_id="native-thread-1",
        ),),
        raw_excerpts=(summary,),
        confidence=0.8,
        narrative_summary=summary,
    )


def _enable_thread(harness: SimpleNamespace, evidence: WorkstreamEvidence) -> None:
    harness.resolved.program.m365 = SimpleNamespace(
        retrieval=SimpleNamespace(per_thread_extraction=True),
    )
    harness.monkeypatch.setattr(
        enrich, "_extract_per_thread_evidence", lambda **kwargs: ((evidence,), False),
    )


def _assert_review_gate(root: Path) -> None:
    records = load_evidence_records("synthetic", programs_root=root)
    signals = read_signals("synthetic", programs_root=root)
    assert len(records) == len(signals) == 1
    assert records[0].backing_signal_ids == (signals[0].id,)
    assert default_review_policy(signals[0]) is ReviewPolicy.PENDING
    assert load_approved_evidence_by_lane(
        "synthetic", programs_root=root, approved_signal_ids=set(),
    ) == {}
    approved = load_approved_evidence_by_lane(
        "synthetic", programs_root=root, approved_signal_ids={signals[0].id},
    )
    assert approved["lane-a"].narrative_summary == signals[0].text


def _thread_cli(*, dry_run: bool, output_format: str, accept: bool = False) -> Result:
    app = typer.Typer()
    app.command()(enrich.enrich_command)
    args = ["--edition", "synthetic", "--no-batch", "--format", output_format]
    if dry_run:
        args.append("--dry-run")
    if accept:
        args.append("--accept")
    return CliRunner().invoke(app, args)


def _seed_thread_lane_state(harness: SimpleNamespace) -> tuple[Path, bytes]:
    path = harness.root / "lane-state.json"
    initial = json.dumps({
        "lanes": {
            "lane-a": {
                "yield_last_3": [0, 0, 1],
                "zero_yield_streak": 2,
                "workiq_query_suspended": False,
                "last_result": "previous",
            },
        },
    }).encode()
    path.write_bytes(initial)
    harness.monkeypatch.setattr(enrich, "_workiq_lane_state_path", lambda **_: path)
    harness.monkeypatch.setattr(enrich, "_record_workiq_lane_result", _real_record_lane_result)
    return path, initial


@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
@pytest.mark.parametrize("output_format", ["human", "json"])
@pytest.mark.parametrize("rejection", ["credential", "sensitivity", "identity"])
def test_thread_admission_rejected_only_preserves_state_and_omits_result(
    harness: SimpleNamespace, caplog: pytest.LogCaptureFixture,
    dry_run: bool, output_format: str, rejection: str,
) -> None:
    private_id = "synthetic-rejected-thread"
    evidence = _thread_evidence("Synthetic update")
    evidence = replace(
        evidence, source_refs=(replace(evidence.source_refs[0], canonical_id=private_id),),
    )
    if rejection == "credential":
        evidence = replace(evidence, narrative_summary="api_key=synthetic-secret")
    elif rejection == "sensitivity":
        evidence = replace(evidence, narrative_summary="sensitivity: restricted")
    else:
        evidence = replace(evidence, source_refs=())
    _enable_thread(harness, evidence)
    state_path, initial = _seed_thread_lane_state(harness)
    before_registry = harness.registry.read_bytes()

    result = _thread_cli(dry_run=dry_run, output_format=output_format)

    assert result.exit_code == 0, result.output
    assert state_path.read_bytes() == initial
    assert harness.registry.read_bytes() == before_registry
    assert load_evidence_records("synthetic", programs_root=harness.root) == ()
    assert read_signals("synthetic", programs_root=harness.root) == ()
    harness.freshness.assert_not_called()
    quarantine_path = harness.root / "synthetic" / "journal" / "evidence_quarantine.jsonl"
    if dry_run:
        assert not quarantine_path.exists()
    else:
        rows = [json.loads(line) for line in quarantine_path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["signal_types"] == [{
            "credential": "client_secret",
            "sensitivity": "sensitivity_marking",
            "identity": "missing_source_identity",
        }[rejection]]
        assert private_id not in quarantine_path.read_text(encoding="utf-8")
    assert "omitted 1 unsafe evidence record(s)" in result.stderr
    assert private_id not in result.output + caplog.text
    assert "synthetic-secret" not in result.output + caplog.text
    progress = result.stderr if output_format == "json" else result.stdout
    assert "sources=0" in progress
    if output_format == "json":
        assert json.loads(result.stdout) == []
    else:
        assert "Done. 0 sources extracted." in result.stdout


@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
@pytest.mark.parametrize("output_format", ["human", "json"])
@pytest.mark.parametrize("incomplete", [False, True], ids=["complete", "incomplete"])
def test_thread_admission_mixed_results_count_safe_duplicates_and_new_sources(
    harness: SimpleNamespace, dry_run: bool, output_format: str, incomplete: bool,
) -> None:
    duplicate = _thread_evidence("Contact owner@example.net for a synthetic update.")
    new = replace(
        _thread_evidence("Contact reviewer@example.net for a different update."),
        source_refs=(replace(duplicate.source_refs[0], canonical_id="native-thread-2"),),
    )
    rejected = replace(
        _thread_evidence("api_key=synthetic-secret"),
        source_refs=(replace(duplicate.source_refs[0], canonical_id="synthetic-rejected-thread"),),
    )
    _enable_thread(harness, duplicate)
    _run()
    before_records = load_evidence_records("synthetic", programs_root=harness.root)
    before_signals = read_signals("synthetic", programs_root=harness.root)
    assert len(before_records) == len(before_signals) == 1
    state_path, initial = _seed_thread_lane_state(harness)
    freshness_path = harness.root / "freshness.json"
    freshness_path.write_text(json.dumps({
        "native-thread-1": {"freshness_hash": "hash-1", "status": "retrieved"},
        "native-thread-2": {"freshness_hash": "hash-2", "status": "retrieved"},
        "synthetic-rejected-thread": {"freshness_hash": "hash-3", "status": "retrieved"},
    }), encoding="utf-8")
    before_freshness = freshness_path.read_bytes()
    freshness = MagicMock(wraps=_real_mark_freshness)
    harness.monkeypatch.setattr(enrich, "_workiq_thread_freshness_path", lambda **_: freshness_path)
    harness.monkeypatch.setattr("src.core.workiq_freshness.mark_workiq_freshness_success", freshness)
    harness.monkeypatch.setattr(
        enrich, "_extract_per_thread_evidence",
        lambda **_: ((duplicate, rejected, new), incomplete),
    )

    result = _thread_cli(dry_run=dry_run, output_format=output_format)

    assert result.exit_code == 0, result.output
    assert "omitted 1 unsafe evidence record(s)" in result.stderr
    assert "synthetic-rejected-thread" not in result.output
    assert "synthetic-secret" not in result.output
    progress = result.stderr if output_format == "json" else result.stdout
    assert "sources=2" in progress
    if output_format == "json":
        assert [row["source_id"] for row in json.loads(result.stdout)] == [
            "native-thread-1", "native-thread-2",
        ]
    else:
        assert "Done. 2 sources extracted." in result.stdout

    records = load_evidence_records("synthetic", programs_root=harness.root)
    signals = read_signals("synthetic", programs_root=harness.root)
    quarantine_path = harness.root / "synthetic" / "journal" / "evidence_quarantine.jsonl"
    if dry_run:
        assert records == before_records
        assert signals == before_signals
        assert state_path.read_bytes() == initial
        assert freshness_path.read_bytes() == before_freshness
        assert not quarantine_path.exists()
        freshness.assert_not_called()
    else:
        assert len(records) == len(signals) == 2
        assert records[0] == before_records[0]
        assert signals[0] == before_signals[0]
        assert records[1].backing_signal_ids == (signals[1].id,)
        assert signals[1].id == enrich._enrich_signal_id(
            lane_id="lane-a", evidence=new, as_of=signals[1].timestamp,
        )
        assert "[PII-FILTERED-EMAIL]" in records[1].evidence.narrative_summary
        assert default_review_policy(signals[1]) is ReviewPolicy.PENDING
        assert load_approved_evidence_by_lane(
            "synthetic", programs_root=harness.root, approved_signal_ids=set(),
        ) == {}
        state = json.loads(state_path.read_text(encoding="utf-8"))["lanes"]["lane-a"]
        assert state["last_observed_yield"] == 2
        assert state["yield_last_3"] == [2, 0, 0]
        assert state["last_result"] == "per_thread_evidence"
        freshness.assert_called_once_with(freshness_path, "native-thread-2")
        cache = json.loads(freshness_path.read_text(encoding="utf-8"))
        assert cache["native-thread-1"]["status"] == "retrieved"
        assert cache["native-thread-2"]["status"] == "success"
        assert cache["synthetic-rejected-thread"]["status"] == "retrieved"
        rows = [json.loads(line) for line in quarantine_path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1


@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
@pytest.mark.parametrize("incomplete", [False, True], ids=["complete", "incomplete"])
def test_thread_admission_empty_discovery_retains_existing_yield_semantics(
    harness: SimpleNamespace, dry_run: bool, incomplete: bool,
) -> None:
    _enable_thread(harness, _thread_evidence("unused"))
    harness.monkeypatch.setattr(
        enrich, "_extract_per_thread_evidence", lambda **_: ((), incomplete),
    )
    state_path, initial = _seed_thread_lane_state(harness)

    result = _thread_cli(dry_run=dry_run, output_format="json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert "sources=0" in result.stderr
    if dry_run or incomplete:
        assert state_path.read_bytes() == initial
    else:
        state = json.loads(state_path.read_text(encoding="utf-8"))["lanes"]["lane-a"]
        assert state["last_observed_yield"] == 0
        assert state["yield_last_3"] == [0, 0, 0]
        assert state["workiq_query_suspended"] is True
    assert not (harness.root / "synthetic" / "journal" / "evidence_quarantine.jsonl").exists()


def test_thread_admission_does_not_mark_freshness_without_first_source_id(
    harness: SimpleNamespace,
) -> None:
    evidence = _thread_evidence("Synthetic update")
    evidence = replace(
        evidence,
        source_refs=(
            SourceRef("manual", "Synthetic source", None, None, canonical_id=None),
            evidence.source_refs[0],
        ),
    )
    _enable_thread(harness, evidence)
    state_path, _ = _seed_thread_lane_state(harness)

    result = _thread_cli(dry_run=False, output_format="json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["source_id"] is None
    assert len(load_evidence_records("synthetic", programs_root=harness.root)) == 1
    assert len(read_signals("synthetic", programs_root=harness.root)) == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))["lanes"]["lane-a"]
    assert state["last_observed_yield"] == 1
    harness.freshness.assert_not_called()


@pytest.mark.parametrize("failure", ["persistence", "signal"])
def test_thread_admission_failure_does_not_record_lane_success(
    harness: SimpleNamespace, failure: str,
) -> None:
    _enable_thread(harness, _thread_evidence("Synthetic update"))
    state_path, initial = _seed_thread_lane_state(harness)

    def fail(**_: object) -> None:
        raise RuntimeError("synthetic write failure")

    if failure == "persistence":
        harness.monkeypatch.setattr(
            "src.commands.gather_pipeline.evidence_extraction_stage._persist_evidence",
            lambda *_, **__: fail(),
        )
    else:
        harness.monkeypatch.setattr(enrich, "_emit_enrich_signal", fail)

    result = _thread_cli(dry_run=False, output_format="json")

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert state_path.read_bytes() == initial
    harness.freshness.assert_not_called()


@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
@pytest.mark.parametrize("output_format", ["human", "json"])
@pytest.mark.parametrize("failure", ["query", "extraction", "confidence", "nonfinite-confidence"])
def test_extraction_diagnostic_privacy_enrich_failure_continues(
    harness: SimpleNamespace, caplog: pytest.LogCaptureFixture,
    dry_run: bool, output_format: str, failure: str,
) -> None:
    marker = "synthetic-private-enrich-7f2"
    registry = yaml.safe_load(harness.registry.read_text(encoding="utf-8"))
    second = dict(registry["workstreams"][0])
    second["id"] = "lane-b"
    registry["workstreams"].append(second)
    harness.registry.write_text(yaml.safe_dump(registry), encoding="utf-8")
    safe = _response("Safe synthetic update")
    if failure == "query":
        responses = [RuntimeError(marker), safe, safe]
        first_result = "query_error:RuntimeError"
    elif failure == "extraction":
        responses = [safe, RuntimeError(marker), safe, safe]
        first_result = "extraction_error:RuntimeError"
    else:
        confidence = float("nan") if failure == "nonfinite-confidence" else marker
        responses = [safe, json.dumps({"confidence": confidence, "raw_excerpts": [marker]}), safe, safe]
        first_result = "extraction_error:ValueError"
    harness.bridge.ask_workiq.side_effect = responses
    lane_results = MagicMock()
    harness.monkeypatch.setattr(enrich, "_record_workiq_lane_result", lane_results)
    app = typer.Typer()
    app.command()(enrich.enrich_command)
    args = ["--edition", "synthetic", "--no-batch", "--format", output_format]
    if dry_run:
        args.append("--dry-run")

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    assert marker not in result.output
    assert marker not in caplog.text
    progress = result.stderr if output_format == "json" else result.stdout
    assert "ERROR" in progress
    assert "lane-b" in progress
    if output_format == "json":
        assert [row["lane_id"] for row in json.loads(result.stdout)] == ["lane-b"]
    else:
        assert "Done. 1 lanes extracted." in result.stdout
    assert harness.bridge.ask_workiq.call_count == len(responses)
    if dry_run:
        lane_results.assert_not_called()
        assert load_evidence_records("synthetic", programs_root=harness.root) == ()
    else:
        assert [call.kwargs["last_result"] for call in lane_results.call_args_list] == [
            first_result, "structured_evidence",
        ]
        assert [record.evidence.lane_id for record in load_evidence_records(
            "synthetic", programs_root=harness.root,
        )] == ["lane-b"]
        assert [signal.workstream_id for signal in read_signals(
            "synthetic", programs_root=harness.root,
        )] == ["lane-b"]


@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
def test_extraction_diagnostic_privacy_per_thread_confidence(
    harness: SimpleNamespace, dry_run: bool,
) -> None:
    marker = "synthetic-private-thread-confidence-7f2"
    harness.resolved.program.m365 = SimpleNamespace(retrieval=SimpleNamespace(
        per_thread_extraction=True,
        per_thread_top_k=2,
        max_calls_per_cycle=4,
        max_wall_clock_seconds=10,
        per_thread_one_hop=True,
    ))
    item = Enrichment(
        source="mail", source_id="native-thread-1", author="synthetic",
        timestamp=datetime(2026, 6, 21, tzinfo=timezone.utc),
        excerpt="Synthetic source", permalink=None,
        body_text=json.dumps({"risk_level": "low", "confidence": marker}),
    )
    retrieval = SimpleNamespace(
        rejected_identity_count=0, rejected_source_time_count=0,
        rejected_invalid_record_count=0, response_usable=True, enrichments=(item,),
    )
    harness.monkeypatch.setattr(
        "src.m365.workiq_retriever.retrieve_workiq_threads", lambda **_: retrieval,
    )
    lane_results = MagicMock()
    harness.monkeypatch.setattr(enrich, "_record_workiq_lane_result", lane_results)
    app = typer.Typer()
    app.command()(enrich.enrich_command)
    args = ["--edition", "synthetic", "--no-batch", "--format", "json"]
    if dry_run:
        args.append("--dry-run")

    result = CliRunner().invoke(app, args)

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "confidence" in str(result.exception).lower()
    assert marker not in result.output
    assert marker not in "".join(traceback.format_exception(result.exception))
    lane_results.assert_not_called()
    assert load_evidence_records("synthetic", programs_root=harness.root) == ()
    assert read_signals("synthetic", programs_root=harness.root) == ()


@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
@pytest.mark.parametrize("failure", ["provenance", "quality", "both"])
def test_extraction_diagnostic_privacy_enrich_recording_failure(
    harness: SimpleNamespace, caplog: pytest.LogCaptureFixture, dry_run: bool, failure: str,
) -> None:
    marker = "synthetic-private-recording-7f2"
    provenance = MagicMock(side_effect=OSError(marker) if failure in {"provenance", "both"} else None)
    quality = MagicMock(side_effect=OSError(marker) if failure in {"quality", "both"} else None)
    harness.monkeypatch.setattr("src.core.evidence_provenance.record_provenance", provenance)
    harness.monkeypatch.setattr("src.core.evidence_quality.record_evidence_quality", quality)
    harness.bridge.ask_workiq.return_value = _response("Safe synthetic update")
    app = typer.Typer()
    app.command()(enrich.enrich_command)
    args = ["--edition", "synthetic", "--no-batch", "--format", "json"]
    if dry_run:
        args.append("--dry-run")

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    assert [row["lane_id"] for row in json.loads(result.stdout)] == ["lane-a"]
    assert marker not in result.output
    assert marker not in caplog.text
    if dry_run:
        provenance.assert_not_called()
        quality.assert_not_called()
        assert load_evidence_records("synthetic", programs_root=harness.root) == ()
    else:
        provenance.assert_called_once()
        quality.assert_called_once()
        if failure in {"provenance", "both"}:
            assert "Failed to record enrich provenance for lane lane-a" in caplog.text
        if failure in {"quality", "both"}:
            assert "Failed to record enrich quality for lane lane-a" in caplog.text
        _assert_review_gate(harness.root)


@pytest.mark.parametrize("per_thread", [False, True])
def test_review_signal_uses_scrubbed_persisted_summary(
    harness: SimpleNamespace, per_thread: bool,
) -> None:
    summary = "Contact owner@example.net or call (206) 555-0101."
    harness.bridge.ask_workiq.return_value = _response(summary)
    if per_thread:
        _enable_thread(harness, _thread_evidence(summary))

    _run()

    _assert_review_gate(harness.root)
    evidence = load_evidence_records("synthetic", programs_root=harness.root)[0].evidence
    signal = read_signals("synthetic", programs_root=harness.root)[0]
    assert signal.text == evidence.narrative_summary
    assert "[PII-FILTERED-EMAIL]" in signal.text
    assert "[PII-FILTERED-PHONE]" in signal.text
    assert "owner@example.net" not in signal.text
    assert "555-0101" not in signal.text
    assert evidence.etas[0].ado_id == "1234567890"
    if per_thread:
        assert evidence.source_refs[0].canonical_id == "native-thread-1"
        harness.freshness.assert_called_once()


def test_legacy_accept_scrubs_registry_and_preserves_safe_duplicate(
    harness: SimpleNamespace,
) -> None:
    summary = "Contact owner@example.net for the delivery update."
    harness.bridge.ask_workiq.return_value = _response(summary)

    _run(accept=True)
    registry = yaml.safe_load(harness.registry.read_text(encoding="utf-8"))
    value = registry["workstreams"][0]["workiq_latest"]
    assert "[PII-FILTERED-EMAIL]" in value
    assert "owner@example.net" not in value
    _assert_review_gate(harness.root)

    registry["workstreams"][0]["workiq_latest"] = "reset for duplicate control"
    harness.registry.write_text(yaml.safe_dump(registry), encoding="utf-8")
    _run(accept=True)
    assert yaml.safe_load(harness.registry.read_text(encoding="utf-8"))["workstreams"][0]["workiq_latest"] == value
    _assert_review_gate(harness.root)


@pytest.mark.parametrize(
    "summary,eta_label,eta_owner,signal_type",
    [
        ("password=synthetic-secret", "Ship", "owner", "password"),
        ("Synthetic update", "sensitivity: restricted", "owner", "sensitivity_marking"),
        ("Synthetic update", "Ship", "api_key=synthetic-secret", "client_secret"),
        ("Synthetic update", "Ship", {"password": "synthetic-secret"}, "invalid_eta_text"),
    ],
)
def test_legacy_accept_does_not_update_registry_after_quarantine(
    harness: SimpleNamespace, capsys: pytest.CaptureFixture[str],
    summary: str, eta_label: str, eta_owner: object, signal_type: str,
) -> None:
    harness.bridge.ask_workiq.return_value = _response(
        summary, eta_label=eta_label, eta_owner=eta_owner,
    )
    before = harness.registry.read_bytes()

    with pytest.raises(typer.Exit) as exc:
        _run(accept=True)

    assert exc.value.exit_code == 1
    assert harness.registry.read_bytes() == before
    assert load_evidence_records("synthetic", programs_root=harness.root) == ()
    assert read_signals("synthetic", programs_root=harness.root) == ()
    quarantine = (
        harness.root / "synthetic" / "journal" / "evidence_quarantine.jsonl"
    ).read_text(encoding="utf-8")
    assert signal_type in quarantine
    assert "synthetic-secret" not in quarantine
    output = capsys.readouterr()
    assert "synthetic-secret" not in output.out + output.err
    assert "registry" in output.err.lower()


def test_refused_accept_allows_other_lanes_before_nonzero_exit(
    harness: SimpleNamespace, capsys: pytest.CaptureFixture[str],
) -> None:
    registry = yaml.safe_load(harness.registry.read_text(encoding="utf-8"))
    second = dict(registry["workstreams"][0])
    second["id"] = "lane-b"
    registry["workstreams"].append(second)
    harness.registry.write_text(yaml.safe_dump(registry), encoding="utf-8")
    unsafe = _response("password=synthetic-secret")
    safe = _response("Contact owner@example.net for the delivery update.")
    harness.bridge.ask_workiq.side_effect = (unsafe, unsafe, safe, safe)

    with pytest.raises(typer.Exit) as exc:
        _run(accept=True)

    assert exc.value.exit_code == 1
    lanes = yaml.safe_load(harness.registry.read_text(encoding="utf-8"))["workstreams"]
    assert lanes[0]["workiq_latest"] == "previous update"
    assert "[PII-FILTERED-EMAIL]" in lanes[1]["workiq_latest"]
    records = load_evidence_records("synthetic", programs_root=harness.root)
    signals = read_signals("synthetic", programs_root=harness.root)
    assert len(records) == len(signals) == 1
    assert records[0].evidence.lane_id == signals[0].workstream_id == "lane-b"
    assert records[0].backing_signal_ids == (signals[0].id,)
    output = capsys.readouterr()
    assert "synthetic-secret" not in output.out + output.err


def test_refused_accept_exits_nonzero_in_json_cli(harness: SimpleNamespace) -> None:
    harness.bridge.ask_workiq.return_value = _response("pass" + "word=synthetic-secret")
    before = harness.registry.read_bytes()
    app = typer.Typer()
    app.command()(enrich.enrich_command)

    result = CliRunner().invoke(
        app, ["--edition", "synthetic", "--accept", "--no-batch", "--format", "json"],
    )

    assert result.exit_code == 1, result.output
    assert harness.registry.read_bytes() == before
    assert "registry not updated" in result.output
    assert json.loads(result.stdout)[0]["lane_id"] == "lane-a"
    assert "registry not updated" in result.stderr
    assert "synthetic-secret" not in result.output


def test_thread_quarantine_does_not_emit_or_mark_fresh(harness: SimpleNamespace) -> None:
    evidence = _thread_evidence("Synthetic update")
    evidence = replace(
        evidence, etas=(replace(evidence.etas[0], owner="password=synthetic-secret"),),
    )
    _enable_thread(harness, evidence)
    before = harness.registry.read_bytes()

    _run(accept=True)

    assert harness.registry.read_bytes() == before
    assert load_evidence_records("synthetic", programs_root=harness.root) == ()
    assert read_signals("synthetic", programs_root=harness.root) == ()
    quarantine = (
        harness.root / "synthetic" / "journal" / "evidence_quarantine.jsonl"
    ).read_text(encoding="utf-8")
    assert "password" in quarantine
    assert "synthetic-secret" not in quarantine
    harness.freshness.assert_not_called()


@pytest.mark.parametrize("ref_kind", ["absent", "blank", "manual", "nonstring"])
def test_thread_missing_identity_does_not_persist_or_mark_fresh(
    harness: SimpleNamespace, ref_kind: str,
) -> None:
    evidence = _thread_evidence("Synthetic update")
    if ref_kind == "absent":
        refs = ()
    elif ref_kind == "blank":
        refs = (replace(evidence.source_refs[0], canonical_id=" "),)
    elif ref_kind == "nonstring":
        refs = (replace(evidence.source_refs[0], canonical_id=17),)
    else:
        refs = (SourceRef("manual", "Manual note", None, None, canonical_id="manual-id"),)
    _enable_thread(harness, replace(evidence, source_refs=refs))
    before = harness.registry.read_bytes()

    _run(accept=True)

    assert harness.registry.read_bytes() == before
    assert load_evidence_records("synthetic", programs_root=harness.root) == ()
    assert read_signals("synthetic", programs_root=harness.root) == ()
    quarantine = (
        harness.root / "synthetic" / "journal" / "evidence_quarantine.jsonl"
    ).read_text(encoding="utf-8")
    assert "missing_source_identity" in quarantine
    assert "manual-id" not in quarantine
    harness.freshness.assert_not_called()


@pytest.mark.parametrize("ref_kind", ["absent", "blank", "manual", "nonstring"])
def test_legacy_missing_identity_refuses_registry_acceptance(
    harness: SimpleNamespace, ref_kind: str,
) -> None:
    evidence = _thread_evidence("Synthetic update")
    if ref_kind == "absent":
        refs = ()
    elif ref_kind == "blank":
        refs = (replace(evidence.source_refs[0], canonical_id=""),)
    elif ref_kind == "nonstring":
        refs = (replace(evidence.source_refs[0], canonical_id=17),)
    else:
        refs = (SourceRef("manual", "Manual note", None, None, canonical_id="manual-id"),)
    harness.monkeypatch.setattr(
        "src.ai.content_extractor.ContentExtractionAgent.extract",
        lambda self, ctx: replace(evidence, source_refs=refs),
    )
    harness.bridge.ask_workiq.return_value = _response("Synthetic update")
    before = harness.registry.read_bytes()

    with pytest.raises(typer.Exit) as exc:
        _run(accept=True)

    assert exc.value.exit_code == 1
    assert harness.registry.read_bytes() == before
    assert load_evidence_records("synthetic", programs_root=harness.root) == ()
    assert read_signals("synthetic", programs_root=harness.root) == ()
    quarantine = (
        harness.root / "synthetic" / "journal" / "evidence_quarantine.jsonl"
    ).read_text(encoding="utf-8")
    assert "missing_source_identity" in quarantine
    assert "manual-id" not in quarantine


def test_thread_missing_identity_is_omitted_from_dry_run_preview(harness: SimpleNamespace) -> None:
    _enable_thread(harness, replace(_thread_evidence("Synthetic update"), source_refs=()))
    before = harness.registry.read_bytes()

    result = _thread_cli(accept=True, dry_run=True, output_format="json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert "omitted 1 unsafe evidence record(s)" in result.stderr
    assert harness.registry.read_bytes() == before
    assert load_evidence_records("synthetic", programs_root=harness.root) == ()
    assert read_signals("synthetic", programs_root=harness.root) == ()
    assert not (harness.root / "synthetic" / "journal" / "evidence_quarantine.jsonl").exists()
    harness.freshness.assert_not_called()


@pytest.mark.parametrize("per_thread", [False, True])
def test_empty_summary_never_copies_query_into_review_signal(
    harness: SimpleNamespace, per_thread: bool,
) -> None:
    if per_thread:
        _enable_thread(harness, replace(_thread_evidence(""), raw_excerpts=()))
    else:
        registry = yaml.safe_load(harness.registry.read_text(encoding="utf-8"))
        registry["workstreams"][0]["name"] = "owner@example.net"
        harness.registry.write_text(yaml.safe_dump(registry), encoding="utf-8")
        harness.bridge.ask_workiq.return_value = _response("")

    _run()

    signal = read_signals("synthetic", programs_root=harness.root)[0]
    assert signal.text == "WorkIQ extraction summary unavailable."
    assert "owner@example.net" not in signal.text
    assert "per-thread WorkIQ extraction" not in signal.text


def test_dry_run_does_not_write_evidence_signal_or_registry(harness: SimpleNamespace) -> None:
    harness.bridge.ask_workiq.return_value = _response("owner@example.net")
    before = harness.registry.read_bytes()

    _run(accept=True, dry_run=True)

    assert harness.registry.read_bytes() == before
    assert load_evidence_records("synthetic", programs_root=harness.root) == ()
    assert read_signals("synthetic", programs_root=harness.root) == ()

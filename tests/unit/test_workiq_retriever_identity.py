from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.core.workiq_freshness import mark_workiq_freshness_success
from src.m365.workiq_ask_support import DiscoveryRequest, validate_structured_discovery_payload
from src.m365.workiq_retriever import (
    WorkIQRunBudget,
    _canonical_source_id,
    retrieve_workiq_threads,
)


REQUEST = DiscoveryRequest("Synthetic lane", ("synthetic",), date(2026, 6, 1), date(2026, 6, 21), 8)
BODY = json.dumps({
    "risk_level": "low", "etas": [], "blocking_items": [], "owners": [],
    "raw_excerpts": ["Synthetic quote"], "narrative_summary": "Synthetic update", "confidence": 0.8,
})


def _record(**identity: Any) -> dict[str, Any]:
    return {
        "subject": "Synthetic private subject", "from": "fixture@example.net",
        "receivedDateTime": "2026-06-20T10:00:00Z", **identity,
    }


class Bridge:
    def __init__(self, records: list[Any], body: Any = None) -> None:
        self.records = records
        self.body = {"response": BODY} if body is None else body
        self.questions: list[str] = []

    def ask_workiq(self, question: str, *, use_cache: bool = False) -> Any:
        assert use_cache is False
        self.questions.append(question)
        if "Which of my emails" in question:
            return {"emails": self.records}
        return self.body


def _retrieve(bridge: Bridge, **kwargs: Any) -> Any:
    return retrieve_workiq_threads(
        bridge=bridge, request=REQUEST, top_k=kwargs.pop("top_k", 8),
        max_calls=kwargs.pop("max_calls", 10), max_wall_clock_seconds=30, **kwargs,
    )


@pytest.mark.parametrize("identity", [
    {}, {"id": "turn1search2"}, {"conversationId": "turn1search2"},
    {"threadId": "turn2result3"}, {"id": "semantic:preview"},
    {"conversationId": "SeMaNtIc:preview", "id": "turn1search2"},
])
def test_provisional_identity_never_enters_rich_extraction_or_cache(
    identity: dict[str, Any], tmp_path: Path,
) -> None:
    bridge = Bridge([_record(**identity)])
    cache = tmp_path / "freshness.json"
    result = _retrieve(bridge, cache_path=cache)
    assert result.enrichments == ()
    assert result.calls_made == len(bridge.questions) == 1
    assert result.rejected_identity_count == 1
    assert json.loads(cache.read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize("identity, expected", [
    ({}, None),
    ({"conversationId": " "}, None),
    ({"conversationId": 12, "threadId": False, "id": ["native"]}, None),
    ({"conversationId": " turn1search2 ", "id": "native-message"}, "native-message"),
    ({"conversationId": "semantic:preview", "threadId": " native-thread "}, "native-thread"),
    ({"conversationId": "TURN12RESULT3", "threadId": "turn1search2"}, None),
    ({"conversationId": " native-conversation ", "threadId": "native-thread"}, "native-conversation"),
    ({"conversationId": "http://[", "threadId": "native-thread"}, "native-thread"),
    ({"conversationId": "http://["}, None),
    ({"threadId": "https://example.invalid/?threadId=semantic%3Apreview", "id": "native-message"}, "native-message"),
    ({"threadId": "https://example.invalid/?threadId=native-thread"}, "native-thread"),
    ({"threadId": "native-thread"}, "native-thread"),
    ({"id": "native-message"}, "native-message"),
])
def test_canonical_identity_requires_stable_nonempty_string(
    identity: dict[str, Any], expected: str | None,
) -> None:
    assert _canonical_source_id(identity) == expected


def test_mixed_results_preserve_native_evidence_and_freshness_success(tmp_path: Path) -> None:
    bridge = Bridge([
        _record(), _record(id="native-message", conversationId="native-thread"),
        _record(subject="Other synthetic subject", conversationId="turn1result9"),
    ])
    cache = tmp_path / "freshness.json"
    first = _retrieve(bridge, cache_path=cache, one_hop=True)
    assert first.rejected_identity_count == 2
    assert first.enumeration_count == 3
    assert first.calls_made == 2
    assert first.enrichments[0].source_id == "native-thread"
    assert "single-pass" in bridge.questions[1]
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert set(saved) == {"native-thread"}
    assert saved["native-thread"]["status"] == "retrieved"
    mark_workiq_freshness_success(cache, "native-thread")
    second = _retrieve(bridge, cache_path=cache)
    assert second.enrichments == ()
    assert second.rejected_identity_count == 2
    assert second.skipped_unchanged == 1
    assert second.calls_made == 1


def test_duplicate_source_revision_is_extracted_once_with_provider_revision_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.m365 import workiq_retriever

    records = [
        _record(
            id="native-message-1",
            conversationId="native-thread",
            changeKey="revision-1",
        ),
        _record(
            id="native-message-2",
            conversationId="native-thread",
            changeKey="revision-1",
        ),
    ]
    bridge = Bridge(records)
    cache = tmp_path / "freshness.json"
    identities: list[str] = []
    original_hash = workiq_retriever.workiq_thread_freshness_hash

    def observe(**kwargs: Any) -> str:
        identities.append(kwargs["newest_message_identity"])
        return original_hash(**kwargs)

    monkeypatch.setattr(workiq_retriever, "workiq_thread_freshness_hash", observe)

    result = _retrieve(bridge, cache_path=cache)

    assert result.calls_made == 2
    assert len(result.enrichments) == 1
    assert len(identities) == 1
    assert "revision-1" in identities[0]


def test_duplicate_validated_source_is_skipped_without_second_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.m365 import workiq_retriever
    from src.m365.workiq_ask_support import StructuredDiscoveryResult

    record = _record(id="native-message", conversationId="native-thread")
    duplicate = dict(record)
    monkeypatch.setattr(
        workiq_retriever,
        "validate_structured_discovery_payload_with_diagnostics",
        lambda *_args, **_kwargs: StructuredDiscoveryResult(
            {"emails": [record, duplicate]}, 0, True,
        ),
    )

    result = _retrieve(Bridge([]), cache_path=tmp_path / "freshness.json")

    assert len(result.enrichments) == 1
    assert result.calls_made == 2


@pytest.mark.parametrize("source_time", [None, "private-invalid-time"])
def test_unqualified_source_time_never_enters_rich_extraction_or_cache(
    source_time: str | None, tmp_path: Path,
) -> None:
    record = _record(id="native-message")
    record["receivedDateTime"] = source_time
    bridge = Bridge([record])

    result = _retrieve(bridge, cache_path=tmp_path / "freshness.json")

    assert result.rejected_source_time_count == 1
    assert result.enrichments == ()
    assert result.calls_made == 1
    assert result.enumeration_count == 0
    assert bridge.questions == [bridge.questions[0]]
    assert json.loads((tmp_path / "freshness.json").read_text(encoding="utf-8")) == {}


def test_valid_rich_source_time_reaches_evidence_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.commands.enrich import _extract_per_thread_evidence

    source_time = "2026-06-20T10:00:00Z"
    bridge = Bridge([_record(id="native-message")])
    monkeypatch.setattr(
        "src.commands.enrich._workiq_thread_freshness_path",
        lambda **_: tmp_path / "freshness.json",
    )

    evidences, incomplete = _extract_per_thread_evidence(
        bridge=bridge,
        entry={"id": "synthetic", "name": "Synthetic lane"},
        since_dt=datetime(2026, 6, 1, tzinfo=timezone.utc),
        as_of=datetime(2026, 6, 21, tzinfo=timezone.utc),
        retrieval_config=SimpleNamespace(
            per_thread_top_k=8,
            max_calls_per_cycle=10,
            max_wall_clock_seconds=30,
            per_thread_one_hop=False,
        ),
        edition="synthetic",
        programs_root=tmp_path,
    )

    assert incomplete is False
    assert evidences[0].source_refs[0].source_time == datetime.fromisoformat(
        source_time.replace("Z", "+00:00")
    )


def test_nullable_rich_source_time_round_trips_evidence_store(tmp_path: Path) -> None:
    from src.commands.gather_pipeline.evidence_extraction_stage import persist_evidence
    from src.core.evidence_models import SourceRef
    from src.core.evidence_store import load_latest_evidence_by_lane
    from src.core.evidence_models import VerificationState, WorkstreamEvidence
    from src.core.models import RiskLevel

    source_ref = SourceRef(
        source_type="workiq_email",
        description="Synthetic update",
        source_date=None,
        author="fixture@example.net",
        canonical_id="native-message",
        source_time=None,
    )
    evidence = WorkstreamEvidence(
        lane_id="synthetic",
        synthesized_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
        risk_level=RiskLevel.LOW,
        etas=(),
        blocking_items=(),
        owners=(),
        source_refs=(source_ref,),
        raw_excerpts=(),
        confidence=0.8,
        narrative_summary="Synthetic update",
        verification_state=VerificationState.MODEL_SELF_ATTESTED,
    )

    assert persist_evidence(
        evidence,
        program_id="synthetic",
        programs_root=tmp_path,
        workiq_origin=True,
    )
    stored = load_latest_evidence_by_lane(
        "synthetic", programs_root=tmp_path,
    )["synthetic"]
    assert stored.source_refs[0].source_time is None


def test_empty_rich_discovery_is_reported_as_unverified_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from src.commands.enrich import _extract_per_thread_evidence

    bridge = Bridge([])
    monkeypatch.setattr(
        "src.commands.enrich._workiq_thread_freshness_path",
        lambda **_: tmp_path / "freshness.json",
    )

    evidences, incomplete = _extract_per_thread_evidence(
        bridge=bridge,
        entry={"id": "synthetic", "name": "Synthetic lane"},
        since_dt=datetime(2026, 6, 1, tzinfo=timezone.utc),
        as_of=datetime(2026, 6, 21, tzinfo=timezone.utc),
        retrieval_config=SimpleNamespace(
            per_thread_top_k=8,
            max_calls_per_cycle=10,
            max_wall_clock_seconds=30,
            per_thread_one_hop=False,
        ),
        edition="synthetic",
        programs_root=tmp_path,
    )

    assert evidences == ()
    assert incomplete is True
    assert "coverage was not verified" in capsys.readouterr().err


def test_preview_semantic_identity_remains_available() -> None:
    result = validate_structured_discovery_payload(
        {"emails": [_record()]}, window_start=REQUEST.window_start,
        window_end=REQUEST.window_end, limit=8,
    )
    assert result["emails"][0]["id"].startswith("semantic:")


@pytest.mark.parametrize("budget", ["calls", "clock", "top_k"])
def test_retrieval_budgets_remain_bounded(budget: str, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = Bridge([_record(id="native-1"), _record(id="native-2")])
    kwargs: dict[str, Any] = {}
    if budget == "calls":
        kwargs["max_calls"] = 1
    elif budget == "clock":
        clock = iter((0, 31))
        monkeypatch.setattr("src.m365.workiq_retriever.monotonic", lambda: next(clock))
    else:
        kwargs["top_k"] = 1
    result = _retrieve(bridge, **kwargs)
    assert result.calls_made == (2 if budget == "top_k" else 1)
    assert len(result.enrichments) == (1 if budget == "top_k" else 0)


def test_run_budget_is_shared_across_thread_restarts() -> None:
    bridge = Bridge([
        _record(id="native-1", conversationId="thread-1"),
        _record(id="native-2", conversationId="thread-2"),
    ])
    budget = WorkIQRunBudget(max_calls=3, max_wall_clock_seconds=30)

    first = _retrieve(bridge, budget=budget, top_k=1)
    second = _retrieve(bridge, budget=budget, top_k=1)

    assert first.calls_made == 2
    assert len(first.enrichments) == 1
    assert second.calls_made == 1
    assert second.enrichments == ()
    assert second.budget_exhausted is True
    assert len(bridge.questions) == 3


def test_run_budget_rejects_invalid_and_elapsed_reservations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="per-run retrieval budget"):
        WorkIQRunBudget(max_calls=0, max_wall_clock_seconds=30)

    clock = iter((0.0, 2.0))
    monkeypatch.setattr("src.m365.workiq_retriever.monotonic", lambda: next(clock))
    budget = WorkIQRunBudget(max_calls=2, max_wall_clock_seconds=1)
    assert budget.reserve_call() == 1.0
    assert budget.reserve_call() is None


@pytest.mark.parametrize("body", [{}, ""])
def test_empty_extraction_does_not_admit_cache_entry(body: Any, tmp_path: Path) -> None:
    bridge = Bridge([_record(id="native-message")], body=body)
    cache = tmp_path / "freshness.json"
    result = _retrieve(bridge, cache_path=cache)
    assert result.enrichments == ()
    assert result.calls_made == 2
    assert json.loads(cache.read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize("top_k,max_calls,max_seconds", [(0, 2, 30), (11, 2, 30), (1, 0, 30), (1, 2, 0)])
def test_invalid_budget_rejects_before_enumeration(top_k: int, max_calls: int, max_seconds: int) -> None:
    bridge = Bridge([])
    with pytest.raises(ValueError, match="budget"):
        retrieve_workiq_threads(
            bridge=bridge, request=REQUEST, top_k=top_k,
            max_calls=max_calls, max_wall_clock_seconds=max_seconds,
        )
    assert bridge.questions == []


@pytest.mark.parametrize("include_native", [False, True])
def test_actual_enrich_caller_reports_bounded_private_safe_identity_rejections(
    include_native: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from src.commands.enrich import _extract_per_thread_evidence

    records = [_record(conversationId="turn1search2")]
    if include_native:
        records.append(_record(id="native-message", conversationId="native-thread"))
    bridge = Bridge(records)
    monkeypatch.setattr("src.commands.enrich._workiq_thread_freshness_path", lambda **_: tmp_path / "freshness.json")
    evidence, has_rejections = _extract_per_thread_evidence(
        bridge=bridge, entry={"id": "synthetic", "name": "Synthetic lane"},
        since_dt=datetime(2026, 6, 1, tzinfo=timezone.utc),
        as_of=datetime(2026, 6, 21, tzinfo=timezone.utc),
        retrieval_config=SimpleNamespace(
            per_thread_top_k=8, max_calls_per_cycle=10,
            max_wall_clock_seconds=30, per_thread_one_hop=False,
        ),
        edition="synthetic", programs_root=tmp_path,
    )
    stderr = capsys.readouterr().err
    assert "1" in stderr and "stable source identity" in stderr
    assert "turn1search2" not in stderr
    assert "Synthetic private subject" not in stderr
    assert "fixture@example.net" not in stderr
    assert has_rejections is True
    assert len(evidence) == int(include_native)
    if include_native:
        assert evidence[0].source_refs[0].canonical_id == "native-thread"


@pytest.mark.parametrize("entry,body", [
    ({"name": "Synthetic"}, BODY),
    ({"id": "synthetic", "name": ""}, BODY),
    ({"id": "synthetic", "name": "Synthetic"}, "not structured evidence"),
])
def test_enrich_preserves_empty_lane_and_unparseable_extraction_behavior(
    entry: dict[str, Any], body: str, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from src.commands.enrich import _extract_per_thread_evidence

    bridge = Bridge([_record(id="native-message")], body={"response": body})
    monkeypatch.setattr("src.commands.enrich._workiq_thread_freshness_path", lambda **_: tmp_path / "freshness.json")
    assert _extract_per_thread_evidence(
        bridge=bridge, entry=entry,
        since_dt=datetime(2026, 6, 1, tzinfo=timezone.utc),
        as_of=datetime(2026, 6, 21, tzinfo=timezone.utc),
        retrieval_config=SimpleNamespace(
            per_thread_top_k=8, max_calls_per_cycle=10,
            max_wall_clock_seconds=30, per_thread_one_hop=True,
        ), edition="synthetic", programs_root=tmp_path,
    ) == ((), False)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("include_native", [False, True])
def test_enrich_cli_retains_identity_warning_and_valid_partial_results(
    include_native: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer
    from typer.testing import CliRunner
    import src.commands.enrich as command

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 6, 21, tzinfo=timezone.utc)

    retrieval_config = SimpleNamespace(
        per_thread_extraction=True, per_thread_top_k=8, max_calls_per_cycle=10,
        max_wall_clock_seconds=30, per_thread_one_hop=False,
    )
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="synthetic"),
        program=SimpleNamespace(m365=SimpleNamespace(retrieval=retrieval_config)),
    )
    bridge = Bridge([_record(conversationId="turn1search2")] + (
        [_record(id="native-message", conversationId="native-thread")] if include_native else []
    ))
    monkeypatch.setattr(bridge, "probe", lambda: SimpleNamespace(available=True, has_workiq=True), raising=False)
    monkeypatch.setattr("src.m365.agency_bridge.AgencyBridge", lambda **_: bridge)
    monkeypatch.setattr(command, "resolve_edition", lambda *_, **__: resolved)
    monkeypatch.setattr(command, "load_program_context", lambda *_, **__: None)
    monkeypatch.setattr(command, "_load_registry_lanes", lambda *_: [{"id": "synthetic", "name": "Synthetic lane"}])
    monkeypatch.setattr(command, "_build_workiq_breaker", lambda **_: None)
    monkeypatch.setattr(command, "_build_workiq_cost_guard", lambda **_: None)
    monkeypatch.setattr(command, "_is_workiq_lane_suspended", lambda **_: False)
    monkeypatch.setattr(command, "_record_workiq_lane_result", lambda **_: None)
    monkeypatch.setattr(command, "_workiq_thread_freshness_path", lambda **_: tmp_path / "freshness.json")
    monkeypatch.setattr(command, "datetime", Clock)
    app = typer.Typer()
    app.command()(command.enrich_command)
    result = CliRunner().invoke(app, ["--edition", "synthetic", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "rejected 1 record(s) without stable source identity" in result.stderr
    assert f"sources={int(include_native)}" in result.stdout
    assert "turn1search2" not in result.output
    assert "Synthetic private subject" not in result.output
    assert "fixture@example.net" not in result.output


def test_enrich_cli_shares_call_budget_across_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer
    from typer.testing import CliRunner
    import src.commands.enrich as command

    retrieval_config = SimpleNamespace(
        per_thread_extraction=True,
        per_thread_top_k=1,
        max_calls_per_cycle=2,
        max_wall_clock_seconds=30,
        per_thread_one_hop=False,
    )
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="synthetic"),
        program=SimpleNamespace(m365=SimpleNamespace(retrieval=retrieval_config)),
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 6, 21, tzinfo=timezone.utc)

    bridge = Bridge([_record(id="native-message", conversationId="native-thread")])
    monkeypatch.setattr(bridge, "probe", lambda: SimpleNamespace(available=True, has_workiq=True), raising=False)
    monkeypatch.setattr("src.m365.agency_bridge.AgencyBridge", lambda **_: bridge)
    monkeypatch.setattr(command, "resolve_edition", lambda *_, **__: resolved)
    monkeypatch.setattr(command, "load_program_context", lambda *_, **__: None)
    monkeypatch.setattr(command, "_load_registry_lanes", lambda *_: [
        {"id": "lane-a", "name": "Synthetic lane A"},
        {"id": "lane-b", "name": "Synthetic lane B"},
    ])
    monkeypatch.setattr(command, "_build_workiq_breaker", lambda **_: None)
    monkeypatch.setattr(command, "_build_workiq_cost_guard", lambda **_: None)
    monkeypatch.setattr(command, "_is_workiq_lane_suspended", lambda **_: False)
    monkeypatch.setattr(command, "_record_workiq_lane_result", lambda **_: None)
    monkeypatch.setattr(command, "_workiq_thread_freshness_path", lambda **_: tmp_path / "freshness.json")
    monkeypatch.setattr(command, "datetime", Clock)
    app = typer.Typer()
    app.command()(command.enrich_command)

    result = CliRunner().invoke(
        app, ["--edition", "synthetic", "--dry-run", "--format", "json"]
    )

    assert result.exit_code == 0, result.output
    assert len(bridge.questions) == 2
    assert "per-run call or wall-clock budget exhausted" in result.stderr
    assert [row["lane_id"] for row in json.loads(result.stdout)] == ["lane-a"]


@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "preview"])
@pytest.mark.parametrize("invalid_first", [False, True])
def test_enrich_cli_nonfinite_confidence_stops_before_persistence_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool, invalid_first: bool,
) -> None:
    import typer
    from typer.testing import CliRunner
    import src.commands.enrich as command

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 6, 21, tzinfo=timezone.utc)

    records = [_record(id="valid-thread"), _record(id="bad-thread")]
    if invalid_first:
        records.reverse()
    bridge = Bridge(records)

    def ask(question: str, *, use_cache: bool = False) -> Any:
        assert use_cache is False
        bridge.questions.append(question)
        if "Which of my emails" in question:
            return {"emails": records}
        if '"bad-thread"' in question:
            return {"response": '{"risk_level":"low","confidence":"NaN","raw_excerpts":["synthetic-private-excerpt"]}'}
        return {"response": BODY}

    monkeypatch.setattr(bridge, "ask_workiq", ask)
    monkeypatch.setattr(bridge, "probe", lambda: SimpleNamespace(available=True, has_workiq=True), raising=False)
    monkeypatch.setattr("src.m365.agency_bridge.AgencyBridge", lambda **_: bridge)
    retrieval_config = SimpleNamespace(
        per_thread_extraction=True, per_thread_top_k=8, max_calls_per_cycle=10,
        max_wall_clock_seconds=30, per_thread_one_hop=False,
    )
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="synthetic"),
        program=SimpleNamespace(m365=SimpleNamespace(retrieval=retrieval_config)),
    )
    monkeypatch.setattr(command, "_PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(command, "resolve_edition", lambda *_, **__: resolved)
    monkeypatch.setattr(command, "load_program_context", lambda *_, **__: None)
    monkeypatch.setattr(command, "_load_registry_lanes", lambda *_: [{"id": "synthetic", "name": "Synthetic lane"}])
    monkeypatch.setattr(command, "_build_workiq_breaker", lambda **_: None)
    monkeypatch.setattr(command, "_build_workiq_cost_guard", lambda **_: None)
    monkeypatch.setattr(command, "_is_workiq_lane_suspended", lambda **_: False)
    record_lane = MagicMock()
    monkeypatch.setattr(command, "_record_workiq_lane_result", record_lane)
    cache = tmp_path / "freshness.json"
    monkeypatch.setattr(command, "_workiq_thread_freshness_path", lambda **_: cache)
    monkeypatch.setattr(command, "datetime", Clock)
    persist = MagicMock()
    monkeypatch.setattr("src.commands.gather_pipeline.evidence_extraction_stage._persist_evidence", persist)
    emit = MagicMock()
    monkeypatch.setattr(command, "_emit_enrich_signal", emit)
    app = typer.Typer()
    app.command()(command.enrich_command)
    args = ["--edition", "synthetic"] + (["--dry-run"] if dry_run else [])

    for attempt in range(2):
        result = CliRunner().invoke(app, args)
        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert str(result.exception) == "ContentExtractionAgent: invalid confidence"
        assert "synthetic-private-excerpt" not in result.output
        assert len(bridge.questions) == 3 * (attempt + 1)
        if dry_run:
            assert not cache.exists()
        else:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            assert {entry["status"] for entry in saved.values()} == {"retrieved"}

    persist.assert_not_called()
    emit.assert_not_called()
    record_lane.assert_not_called()


@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "normal"])
@pytest.mark.parametrize("existing_cache", [True, False], ids=["existing-cache", "absent-cache"])
def test_enrich_per_thread_preview_preserves_freshness_cache(
    dry_run: bool, existing_cache: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    import typer
    from typer.testing import CliRunner
    import src.commands.enrich as command
    from src.core.workiq_freshness import workiq_thread_freshness_hash

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 6, 21, tzinfo=timezone.utc)

    cache_path = tmp_path / "freshness.json"
    lane_state = tmp_path / "lane-state.json"
    new_record = _record(id="message-new", conversationId="thread-new")
    records = [new_record]
    original = None
    if existing_cache:
        records.insert(0, _record(id="message-old", conversationId="thread-old"))
        old_cache = {
            "thread-old": {
                "freshness_hash": workiq_thread_freshness_hash(
                    conversation_id="thread-old", message_count=1, newest_message_identity="message-old",
                ),
                "status": "success",
            }
        }
        original = json.dumps(old_cache, indent=3).encode("utf-8")
        cache_path.write_bytes(original)
    retrieval = SimpleNamespace(
        per_thread_extraction=True, per_thread_top_k=8, max_calls_per_cycle=10,
        max_wall_clock_seconds=30, per_thread_one_hop=False,
    )
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="synthetic"),
        program=SimpleNamespace(m365=SimpleNamespace(retrieval=retrieval)),
    )
    bridge = Bridge(records)
    monkeypatch.setattr(bridge, "probe", lambda: SimpleNamespace(available=True, has_workiq=True), raising=False)
    monkeypatch.setattr("src.m365.agency_bridge.AgencyBridge", lambda **_: bridge)
    monkeypatch.setattr(command, "resolve_edition", lambda *_, **__: resolved)
    monkeypatch.setattr(command, "load_program_context", lambda *_, **__: None)
    monkeypatch.setattr(command, "_PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(command, "_load_registry_lanes", lambda *_: [{"id": "synthetic", "name": "Synthetic lane"}])
    monkeypatch.setattr(command, "_build_workiq_breaker", lambda **_: None)
    monkeypatch.setattr(command, "_build_workiq_cost_guard", lambda **_: None)
    monkeypatch.setattr(command, "_workiq_lane_state_path", lambda **_: lane_state)
    monkeypatch.setattr(command, "_workiq_thread_freshness_path", lambda **_: cache_path)
    monkeypatch.setattr(command, "datetime", Clock)
    persist = MagicMock(side_effect=lambda evidence, **_: (False, evidence))
    monkeypatch.setattr("src.commands.gather_pipeline.evidence_extraction_stage._persist_evidence", persist)
    app = typer.Typer()
    app.command()(command.enrich_command)
    args = ["--edition", "synthetic"] + (["--dry-run"] if dry_run else [])
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    assert "sources=1" in result.stdout
    assert len(bridge.questions) == 2  # Discovery plus only the new thread.
    if dry_run:
        if existing_cache:
            assert cache_path.read_bytes() == original
        else:
            assert not cache_path.exists()
        assert not lane_state.exists()
        persist.assert_not_called()
    else:
        saved = json.loads(cache_path.read_text(encoding="utf-8"))
        assert saved["thread-new"]["status"] == "retrieved"
        if existing_cache:
            assert saved["thread-old"] == old_cache["thread-old"]
        assert lane_state.exists()
        persist.assert_called_once()


@pytest.mark.parametrize("max_calls", [1, 10])
def test_rich_retrieval_preserves_time_rejections_and_valid_partial_results(
    max_calls: int, tmp_path: Path,
) -> None:
    missing = _record(id="private-missing")
    missing.pop("receivedDateTime")
    invalid = _record(id="private-invalid", receivedDateTime="invalid")
    outside = _record(id="outside", receivedDateTime="2026-05-01T00:00:00Z")
    bridge = Bridge([missing, invalid, outside, _record(id="valid", conversationId="valid-thread")])
    cache = tmp_path / "freshness.json"

    result = _retrieve(bridge, cache_path=cache, max_calls=max_calls)

    assert result.rejected_source_time_count == 2
    assert result.enumeration_count == 1
    assert result.calls_made == (1 if max_calls == 1 else 2)
    assert len(result.enrichments) == (0 if max_calls == 1 else 1)
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == (
        set() if max_calls == 1 else {"valid-thread"}
    )
    assert all(
        "private-missing" not in question and "private-invalid" not in question
        for question in bridge.questions
    )


@pytest.mark.parametrize("invalid_time", [None, "private-invalid-time", 123, "0001-01-01T00:00:00+14:00"])
@pytest.mark.parametrize("existing_cache", [False, True])
def test_rich_retrieval_refuses_invalid_validated_time_before_extraction(
    invalid_time: Any, existing_cache: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.m365.workiq_retriever as retriever

    validate = retriever.validate_structured_discovery_payload_with_diagnostics

    def corrupt_validated_time(*args: Any, **kwargs: Any) -> Any:
        result = validate(*args, **kwargs)
        result.payload["emails"][0]["receivedDateTime"] = invalid_time
        return result

    monkeypatch.setattr(retriever, "validate_structured_discovery_payload_with_diagnostics", corrupt_validated_time)
    bridge = Bridge([_record(id="valid")])
    cache = tmp_path / "freshness.json"
    if existing_cache:
        cache.write_text('{"existing": {}}', encoding="utf-8")
        original_cache = cache.read_bytes()

    with pytest.raises(ValueError, match="Invalid validated WorkIQ source time") as error:
        _retrieve(bridge, cache_path=cache)

    assert str(error.value) == "Invalid validated WorkIQ source time"
    assert len(bridge.questions) == 1
    if existing_cache:
        assert cache.read_bytes() == original_cache
    else:
        assert not cache.exists()


def test_rich_retrieval_rejects_unqualified_source_time() -> None:
    bridge = Bridge([_record(id="valid", receivedDateTime="2026-06-20T10:00:00")])

    result = _retrieve(bridge)

    assert result.enrichments == ()
    assert result.rejected_source_time_count == 1


@pytest.mark.parametrize("max_calls, expected_rejected", [(1, 1), (10, 2)])
def test_rich_retrieval_combines_disjoint_identity_rejections(
    max_calls: int, expected_rejected: int, tmp_path: Path,
) -> None:
    bridge = Bridge([
        {"subject": "Private missing sender", "receivedDateTime": "2026-06-20T10:00:00Z"},
        _record(conversationId="turn1search2"),
        _record(id="valid", conversationId="valid-thread"),
    ])
    cache = tmp_path / "freshness.json"

    result = _retrieve(bridge, cache_path=cache, max_calls=max_calls)

    assert result.rejected_identity_count == expected_rejected
    assert result.enumeration_count == 2
    assert result.calls_made == (1 if max_calls == 1 else 2)
    assert len(result.enrichments) == (0 if max_calls == 1 else 1)
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == (set() if max_calls == 1 else {"valid-thread"})
    assert all("Private missing sender" not in question for question in bridge.questions)


def test_rich_retrieval_uses_native_identity_hidden_by_transient_conversation(tmp_path: Path) -> None:
    bridge = Bridge([{
        "conversationId": "turn1search2", "id": "native-message",
        "receivedDateTime": "2026-06-20T10:00:00Z",
    }])
    cache = tmp_path / "freshness.json"

    result = _retrieve(bridge, cache_path=cache)

    assert result.rejected_identity_count == 0
    assert result.enumeration_count == 1 and result.calls_made == 2
    assert len(result.enrichments) == 1
    assert result.enrichments[0].source_id == "native-message"
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"native-message"}
    assert all("turn1search2" not in question for question in bridge.questions)


@pytest.mark.parametrize("identity", [
    {"conversationId": "http://["},
    {"conversationId": "semantic:preview", "threadId": "http://["},
    {"conversationId": "semantic:preview", "threadId": "https://example.invalid/?threadId=semantic%3Apreview"},
])
@pytest.mark.parametrize("include_native", [False, True])
def test_rich_retrieval_rejects_malformed_and_encoded_preview_fallbacks(
    identity: dict[str, str], include_native: bool, tmp_path: Path,
) -> None:
    bridge = Bridge([_record(**identity)] + (
        [_record(id="valid-message", conversationId="valid-thread")] if include_native else []
    ))
    cache = tmp_path / "freshness.json"

    result = _retrieve(bridge, cache_path=cache)

    assert len(result.enrichments) == int(include_native)
    assert result.rejected_identity_count == 1
    assert result.calls_made == len(bridge.questions) == 1 + int(include_native)
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == ({"valid-thread"} if include_native else set())
    if include_native:
        assert result.enrichments[0].source_id == "valid-thread"
        assert "semantic:preview" not in bridge.questions[1]


@pytest.mark.parametrize("payload", [None, {}, {"emails": None}, {"response": "Private malformed response"}])
def test_rich_retrieval_distinguishes_unusable_structured_responses(payload: Any, tmp_path: Path) -> None:
    bridge = Bridge([])
    bridge.ask_workiq = lambda question, **kwargs: payload
    result = _retrieve(bridge, cache_path=tmp_path / "freshness.json")
    assert result.response_usable is False
    assert result.enrichments == ()
    assert result.calls_made == 1
    assert result.rejected_identity_count == result.rejected_source_time_count == 0
    assert "Private malformed response" not in repr(result)


@pytest.mark.parametrize("include_native", [False, True])
@pytest.mark.parametrize("max_calls", [1, 10])
def test_rich_retrieval_preserves_invalid_record_counts_and_valid_partial_evidence(
    include_native: bool, max_calls: int, tmp_path: Path,
) -> None:
    records: list[Any] = [
        "Private malformed record",
        _record(id="private-control", subject="Private unsafe \x1b[2J content"),
        _record(id="private-url", webUrl="https://untrusted.invalid/private"),
    ]
    if include_native:
        records.append(_record(id="valid-message", conversationId="valid-thread"))
    bridge = Bridge(records)

    cache = tmp_path / "freshness.json"
    result = _retrieve(bridge, cache_path=cache, max_calls=max_calls)

    assert result.rejected_invalid_record_count == 3
    assert result.rejected_identity_count == result.rejected_source_time_count == 0
    assert result.response_usable is True
    expected_evidence = int(include_native and max_calls > 1)
    assert result.enumeration_count == int(include_native)
    assert len(result.enrichments) == expected_evidence
    assert result.calls_made == len(bridge.questions) == 1 + expected_evidence
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == ({"valid-thread"} if expected_evidence else set())
    for private_value in ("Private malformed record", "Private unsafe", "private-control", "private-url", "untrusted.invalid"):
        assert private_value not in repr(result) + repr(bridge.questions)


@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "normal"])
@pytest.mark.parametrize(
    ("kind", "include_native"),
    [(kind, include) for kind in (
        "missing_time", "invalid_time", "identity", "identity_missing", "empty", "outside",
        "invalid_shape", "unsafe_control", "unsafe_url",
    )
     for include in (False, True)] + [("unavailable", False), ("malformed", False)],
)
def test_enrich_cli_does_not_record_rejected_only_results_as_zero_yield(
    kind: str, include_native: bool, dry_run: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer
    from typer.testing import CliRunner
    import src.commands.enrich as command

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 6, 21, tzinfo=timezone.utc)

    rejected: Any = _record(id="private-rejected")
    if kind == "missing_time":
        rejected.pop("receivedDateTime")
    elif kind == "invalid_time":
        rejected["receivedDateTime"] = "invalid"
    elif kind == "identity":
        rejected = _record(conversationId="turn1search2")
    elif kind == "identity_missing":
        rejected = {"subject": "Private missing sender", "receivedDateTime": "2026-06-20T10:00:00Z"}
    elif kind == "invalid_shape":
        rejected = "Private malformed record"
    elif kind == "unsafe_control":
        rejected = _record(id="private-control", subject="Private unsafe \x1b[2J content")
    elif kind == "unsafe_url":
        rejected = _record(id="private-url", webUrl="https://untrusted.invalid/private")
    elif kind == "outside":
        rejected["receivedDateTime"] = "2026-05-01T00:00:00Z"
    records = [] if kind == "empty" else [rejected]
    if include_native:
        records.append(_record(id="valid-message", conversationId="valid-thread"))
    bridge = Bridge(records)
    if kind == "unavailable":
        monkeypatch.setattr(bridge, "ask_workiq", lambda *args, **kwargs: None)
    elif kind == "malformed":
        monkeypatch.setattr(bridge, "ask_workiq", lambda *args, **kwargs: {"response": "Private malformed response"})
    retrieval_config = SimpleNamespace(
        per_thread_extraction=True, per_thread_top_k=8, max_calls_per_cycle=10,
        max_wall_clock_seconds=30, per_thread_one_hop=False,
    )
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="synthetic"),
        program=SimpleNamespace(m365=SimpleNamespace(retrieval=retrieval_config)),
    )
    state_path = tmp_path / "lane-state.json"
    monkeypatch.setattr(command, "_PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(command, "_workiq_lane_state_path", lambda **_: state_path)
    command._record_workiq_lane_result(
        lane_id="synthetic", edition="synthetic", program_id="synthetic", programs_root=tmp_path,
        observed_yield=2, as_of=datetime(2026, 6, 20, tzinfo=timezone.utc), last_result="previous",
    )
    previous_state = state_path.read_bytes()
    monkeypatch.setattr(bridge, "probe", lambda: SimpleNamespace(available=True, has_workiq=True), raising=False)
    monkeypatch.setattr("src.m365.agency_bridge.AgencyBridge", lambda **_: bridge)
    monkeypatch.setattr(command, "resolve_edition", lambda *_, **__: resolved)
    monkeypatch.setattr(command, "load_program_context", lambda *_, **__: None)
    monkeypatch.setattr(command, "_load_registry_lanes", lambda *_: [{"id": "synthetic", "name": "Synthetic lane"}])
    monkeypatch.setattr(command, "_build_workiq_breaker", lambda **_: None)
    monkeypatch.setattr(command, "_build_workiq_cost_guard", lambda **_: None)
    monkeypatch.setattr(command, "_workiq_thread_freshness_path", lambda **_: tmp_path / "freshness.json")
    monkeypatch.setattr(command, "datetime", Clock)
    persist = MagicMock(side_effect=lambda evidence, **_: (False, evidence))
    monkeypatch.setattr("src.commands.gather_pipeline.evidence_extraction_stage._persist_evidence", persist)
    app = typer.Typer()
    app.command()(command.enrich_command)

    args = ["--edition", "synthetic"] + (["--dry-run"] if dry_run else [])
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    unqualified_time = kind in {"missing_time", "invalid_time"}
    if dry_run or (not include_native and kind in {
        "empty", "outside",
    }) or (not include_native and kind in {
        "missing_time", "invalid_time", "identity", "identity_missing", "unavailable", "malformed",
        "invalid_shape", "unsafe_control", "unsafe_url",
    }):
        assert state_path.read_bytes() == previous_state
    else:
        state = json.loads(state_path.read_text(encoding="utf-8"))["lanes"]["synthetic"]
        assert state["last_observed_yield"] == int(include_native)
    assert persist.call_count == (int(include_native) if not dry_run else 0)
    preview_warning = (
        "(dry-run: enrich does not save evidence; WorkIQ budget and circuit-breaker state may be written; "
        "CLI startup may also write state)\n"
    )
    if dry_run:
        assert result.stderr.startswith(preview_warning)
        diagnostics = result.stderr.removeprefix(preview_warning)
    else:
        assert preview_warning not in result.stderr
        diagnostics = result.stderr
    if unqualified_time:
        assert "missing or invalid source time" in diagnostics
    elif kind in {"identity", "identity_missing"}:
        assert "stable source identity" in diagnostics
    elif kind in {"unavailable", "malformed"}:
        assert "structured discovery response unavailable or invalid" in diagnostics
    elif kind in {"invalid_shape", "unsafe_control", "unsafe_url"}:
        assert "1 inspected record occurrence(s) rejected for malformed or unsafe content" in diagnostics
    elif kind in {"empty", "outside"} and not include_native:
        assert "coverage was not verified" in diagnostics
    else:
        assert diagnostics == ""
    assert f"sources={int(include_native)}" in result.stdout
    for private_value in (
        "private-rejected", "turn1search2", "Synthetic private subject", "fixture@example.net",
        "Private malformed response", "Private missing sender", "Private malformed record",
        "Private unsafe", "private-control", "private-url", "untrusted.invalid",
    ):
        assert private_value not in result.output

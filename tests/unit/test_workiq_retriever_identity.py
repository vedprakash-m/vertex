from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.workiq_freshness import mark_workiq_freshness_success
from src.m365.workiq_ask_support import DiscoveryRequest, validate_structured_discovery_payload
from src.m365.workiq_retriever import _canonical_source_id, retrieve_workiq_threads


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
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == (set() if max_calls == 1 else {"valid-thread"})
    assert all("private-missing" not in question and "private-invalid" not in question for question in bridge.questions)


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


@pytest.mark.parametrize(
    ("kind", "include_native"),
    [(kind, include) for kind in (
        "missing_time", "invalid_time", "identity", "identity_missing", "empty", "outside",
        "invalid_shape", "unsafe_control", "unsafe_url",
    )
     for include in (False, True)] + [("unavailable", False), ("malformed", False)],
)
def test_enrich_cli_does_not_record_rejected_only_results_as_zero_yield(
    kind: str, include_native: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    app = typer.Typer()
    app.command()(command.enrich_command)

    result = CliRunner().invoke(app, ["--edition", "synthetic", "--dry-run"])

    assert result.exit_code == 0, result.output
    if not include_native and kind in {
        "missing_time", "invalid_time", "identity", "identity_missing", "unavailable", "malformed",
        "invalid_shape", "unsafe_control", "unsafe_url",
    }:
        assert state_path.read_bytes() == previous_state
    else:
        state = json.loads(state_path.read_text(encoding="utf-8"))["lanes"]["synthetic"]
        assert state["last_observed_yield"] == int(include_native)
    if kind in {"missing_time", "invalid_time"}:
        assert "missing or invalid source time" in result.stderr
    elif kind in {"identity", "identity_missing"}:
        assert "stable source identity" in result.stderr
    elif kind in {"unavailable", "malformed"}:
        assert "structured discovery response unavailable or invalid" in result.stderr
    elif kind in {"invalid_shape", "unsafe_control", "unsafe_url"}:
        assert "1 inspected record occurrence(s) rejected for malformed or unsafe content" in result.stderr
    else:
        assert result.stderr == ""
    assert f"sources={int(include_native)}" in result.stdout
    for private_value in (
        "private-rejected", "turn1search2", "Synthetic private subject", "fixture@example.net",
        "Private malformed response", "Private missing sender", "Private malformed record",
        "Private unsafe", "private-control", "private-url", "untrusted.invalid",
    ):
        assert private_value not in result.output

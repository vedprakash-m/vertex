"""ADF-W4.5 remainder: unit tests for src/ai/dependency_blast_radius_generator.py."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from unittest.mock import Mock

import pytest

import src.ai.dependency_blast_radius_generator as blast_radius_generator
import src.ai.structured_feature_runner as structured_feature_runner
from src.ai.deployment_fallback import FallbackAIClient, FallbackStructuredClient
from src.ai.dependency_blast_radius_generator import (
    POLICY_VERSION,
    generate_dependency_blast_radius_proposal,
    generate_dependency_blast_radius_proposal_via_runner,
)
from src.ai.structured_feature_runner import RunnerContext
from src.core.dependency_blast_radius import DependencyBlastRadiusRequest
from src.core.exceptions import ConfigError, StateError
from src.core.ai_result_cache import AIResultCacheKey, canonical_input_hash, put_ai_result
from src.core.ledger.event_log import canonical_json, get_event_logs_dir, read_events
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    record_ai_release_decision,
    released_terminal_for_run,
)


class _FakeClient:
    def __init__(self, *, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0

    def chat(self, system: str, user: str, *, max_tokens: int = 800, prompt_version: str | None = None) -> str:
        raise NotImplementedError

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], Any],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return parser(self.response)


def _request() -> DependencyBlastRadiusRequest:
    return DependencyBlastRadiusRequest(
        program_id="acme",
        dependency_id="dep-1",
        from_summary="program=acme, workstream=deployment, WI:1001",
        to_summary="program=contoso, workstream=platform, milestone=ms-1",
        risk_if_broken="Contoso's platform milestone slips by two sprints.",
        current_status="active",
        evidence_texts=("Vendor confirmed the API contract review date.",),
        evidence_refs=("sig-1",),
    )


def _valid_response() -> dict[str, Any]:
    return {
        "next_proving_event": "The platform API contract review scheduled for next sprint.",
        "blast_radius_narrative": "If unresolved, Contoso's platform milestone slips, cascading to two downstream teams.",
        "evidence_refs": ["sig-1"],
    }


def _context() -> RunnerContext:
    return RunnerContext(
        program_id="acme", run_id="synthetic-pilot-run", policy_version=POLICY_VERSION,
        snapshot_id="synthetic-pilot-snapshot", principal="synthetic-pilot-reviewer", classification="public",
    )


def _pilot_audit(programs_root: Path, run_id: str) -> list[tuple[str, str]]:
    entries = []
    for event in read_events("acme", programs_root=programs_root):
        if event.payload.get("ai_run_id") != run_id:
            continue
        if event.event_type == "ai.run_lifecycle.v1":
            entries.append(("state", event.payload["state"]))
        elif event.event_type == "ai.release_decision.v1":
            entries.append(("terminal", event.payload["terminal"]))
    return entries


def _pilot_release_binding_hash(cache_key: str, payload: str) -> str:
    envelope = {
        "schema": "dependency_blast_radius.released_cache.v1",
        "cache_key": cache_key,
        "payload": payload,
    }
    return hashlib.sha256(
        json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _pilot_cache_rows(programs_root: Path) -> list[tuple[str, str, str, str]]:
    with sqlite3.connect(blast_radius_generator._released_cache_path(programs_root, "acme")) as connection:
        return connection.execute(
            "SELECT cache_key, payload, released_content_hash, release_run_id FROM released_result_cache"
        ).fetchall()


def _record_synthetic_pilot_binding(programs_root: Path, origin: str) -> None:
    [(cache_key, payload, _, _)] = _pilot_cache_rows(programs_root)
    record_ai_release_decision(
        program_id="acme", ai_run_id=origin, terminal=ReleaseTerminal.RELEASED,
        reason="synthetic test binding", validator_finding_count=0,
        released_content_hash=_pilot_release_binding_hash(cache_key, payload),
        programs_root=programs_root,
    )


def test_valid_response_is_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    proposal = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)

    assert proposal is not None
    assert "API contract review" in proposal.next_proving_event
    assert "cascading" in proposal.blast_radius_narrative
    assert proposal.evidence_refs == ("sig-1",)
    assert released_terminal_for_run(proposal.ai_run_id, program_id="acme", programs_root=programs_root).value == "released"


@pytest.mark.parametrize("use_runner", [False, True], ids=["production", "runner-pilot"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("next_proving_event", None),
        ("next_proving_event", 42),
        ("next_proving_event", False),
        ("next_proving_event", ["synthetic"]),
        ("next_proving_event", {"synthetic": "response-private-marker"}),
        ("blast_radius_narrative", None),
        ("blast_radius_narrative", 42),
        ("blast_radius_narrative", False),
        ("blast_radius_narrative", ["synthetic"]),
        ("blast_radius_narrative", {"synthetic": "value"}),
        ("evidence_refs", None),
        ("evidence_refs", "sig-1"),
        ("evidence_refs", 42),
        ("evidence_refs", {"sig-1": "synthetic"}),
        ("evidence_refs", ["sig-1", None]),
        ("evidence_refs", ["sig-1", 42]),
        ("evidence_refs", ["sig-1", False]),
        ("evidence_refs", ["sig-1", {"synthetic": "response-private-marker"}]),
    ],
)
def test_dependency_response_rejects_malformed_fields_without_release_or_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_runner: bool, field: str, value: object,
) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response[field] = value
    client = _FakeClient(response=response)
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: "synthetic-malformed-response")

    if use_runner:
        proposal = generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=_context(), client=client, programs_root=programs_root,
        )
    else:
        proposal = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)

    assert proposal is None
    assert client.calls == 1
    events = read_events("acme", programs_root=programs_root)
    decisions = [
        event.payload["terminal"] for event in events
        if event.event_type == "ai.release_decision.v1"
        and event.payload.get("ai_run_id") == "synthetic-malformed-response"
    ]
    assert decisions == ["rejected"]
    assert "response-private-marker" not in repr([event.payload for event in events])
    assert _dependency_cache_files(programs_root) == []
    runner_cache = blast_radius_generator._released_cache_path(programs_root, "acme")
    if runner_cache.exists():
        with sqlite3.connect(runner_cache) as connection:
            assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 0


@pytest.mark.parametrize("use_runner", [False, True], ids=["production", "runner-pilot"])
@pytest.mark.parametrize(
    ("scenario", "production_terminal", "runner_terminal", "calls", "finding_count"),
    [
        ("unknown-reference", "rejected", "rejected", 1, 1),
        ("inbound-bounds", "rejected", "discarded", 1, 0),
        ("outbound-bounds", "discarded", "discarded", 0, 0),
        ("provider-error", "discarded", "discarded", 1, 0),
    ],
)
def test_dependency_refusal_audit_omits_untrusted_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_runner: bool, scenario: str,
    production_terminal: str, runner_terminal: str, calls: int, finding_count: int,
) -> None:
    programs_root = tmp_path / "programs"
    run_id = "synthetic-private-refusal"
    marker = "synthetic-untrusted-private-marker"
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: run_id)
    request = _request()
    response = _valid_response()
    client = _FakeClient(response=response)
    if scenario == "unknown-reference":
        response["evidence_refs"] = ["sig-1", marker]
    elif scenario == "inbound-bounds":
        response[marker] = "x" * 200_001
    elif scenario == "outbound-bounds":
        request = replace(request, evidence_texts=({marker: "x" * 200_001},))
    else:
        client.error = RuntimeError(marker)

    if use_runner:
        proposal = generate_dependency_blast_radius_proposal_via_runner(
            request, context=_context(), client=client, programs_root=programs_root,
        )
    else:
        proposal = generate_dependency_blast_radius_proposal(request, client=client, programs_root=programs_root)

    assert proposal is None
    assert client.calls == calls
    events = read_events("acme", programs_root=programs_root)
    decisions = [
        event.payload for event in events
        if event.event_type == "ai.release_decision.v1" and event.payload.get("ai_run_id") == run_id
    ]
    assert len(decisions) == 1
    assert decisions[0]["terminal"] == (runner_terminal if use_runner else production_terminal)
    assert decisions[0]["validator_finding_count"] == finding_count
    if scenario == "unknown-reference":
        assert "evidence_refs" in decisions[0]["reason"]
    elif scenario == "outbound-bounds":
        assert "outbound request" in decisions[0]["reason"]
    elif not use_runner and scenario == "inbound-bounds":
        assert "response" in decisions[0]["reason"]
    elif not use_runner:
        assert "provider call failed" in decisions[0]["reason"]
    assert marker not in repr(events)
    assert _dependency_cache_files(programs_root) == []
    runner_cache = blast_radius_generator._released_cache_path(programs_root, "acme")
    if runner_cache.exists():
        with sqlite3.connect(runner_cache) as connection:
            assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 0


def test_dependency_runner_unexpected_failure_audit_omits_exception_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    run_id = "synthetic-runner-refusal"
    marker = "synthetic-runner-private-marker"
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: run_id)

    def fail_runner(*args: object, **kwargs: object) -> None:
        raise RuntimeError(marker)

    monkeypatch.setattr(blast_radius_generator.StructuredFeatureRunner, "run", fail_runner)
    client = _FakeClient(response=_valid_response())
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is None

    assert client.calls == 0
    events = read_events("acme", programs_root=programs_root)
    decisions = [
        event.payload for event in events
        if event.event_type == "ai.release_decision.v1" and event.payload.get("ai_run_id") == run_id
    ]
    assert len(decisions) == 1
    assert decisions[0]["terminal"] == "discarded"
    assert "Structured runner failed" in decisions[0]["reason"]
    assert marker not in repr(events)
    assert _dependency_cache_files(programs_root) == []


@pytest.mark.parametrize("use_runner", [False, True], ids=["production", "runner-pilot"])
@pytest.mark.parametrize("omit_refs", [False, True], ids=["empty", "absent"])
@pytest.mark.parametrize("empty_request", [False, True], ids=["with-evidence", "empty-evidence"])
def test_dependency_response_keeps_empty_or_absent_evidence_refs_supported(
    tmp_path: Path, use_runner: bool, omit_refs: bool, empty_request: bool,
) -> None:
    programs_root = tmp_path / "programs"
    request = replace(_request(), evidence_texts=(), evidence_refs=()) if empty_request else _request()
    response = _valid_response()
    if omit_refs:
        del response["evidence_refs"]
    else:
        response["evidence_refs"] = []
    client = _FakeClient(response=response)

    if use_runner:
        proposal = generate_dependency_blast_radius_proposal_via_runner(
            request, context=_context(), client=client, programs_root=programs_root,
        )
        replay = generate_dependency_blast_radius_proposal_via_runner(
            request, context=_context(), client=client, programs_root=programs_root,
        )
    else:
        proposal = generate_dependency_blast_radius_proposal(request, client=client, programs_root=programs_root)
        replay = generate_dependency_blast_radius_proposal(request, client=client, programs_root=programs_root)

    assert proposal is not None
    assert proposal.evidence_refs == ()
    assert replay is not None
    assert replay.evidence_refs == ()
    assert client.calls == 1
    assert released_terminal_for_run(proposal.ai_run_id, program_id="acme", programs_root=programs_root) == ReleaseTerminal.RELEASED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("next_proving_event", 42),
        ("blast_radius_narrative", None),
        ("evidence_refs", ["sig-1", 42]),
    ],
)
def test_dependency_runner_refuses_malformed_cached_response_before_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-cache-origin", "synthetic-cache-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is not None

    response = _valid_response()
    response[field] = value
    payload = json.dumps(response, sort_keys=True)
    with sqlite3.connect(blast_radius_generator._released_cache_path(programs_root, "acme")) as connection:
        connection.execute(
            "UPDATE released_result_cache SET payload = ?, released_content_hash = ?",
            (payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()),
        )
    _record_synthetic_pilot_binding(programs_root, "synthetic-cache-origin")

    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is None
    assert client.calls == 1
    decisions = [
        event.payload["terminal"] for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1"
        and event.payload.get("ai_run_id") == "synthetic-cache-replay"
    ]
    assert decisions == ["discarded"]


@pytest.mark.parametrize(
    "defect", ["unknown-reference", "bounds", "non-object", "null", "boolean", "number", "string"],
)
def test_dependency_runner_revalidates_cached_payload_before_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-revalidation-origin", "synthetic-revalidation-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is not None

    private_marker = "synthetic-cache-private-marker"
    response: object = _valid_response()
    if defect == "unknown-reference":
        response = {**_valid_response(), "evidence_refs": ["sig-1", private_marker]}
    elif defect == "bounds":
        response = {**_valid_response(), private_marker: "x" * 200_001}
    elif defect == "non-object":
        response = [private_marker]
    elif defect == "null":
        response = None
    elif defect == "boolean":
        response = False
    elif defect == "number":
        response = 42
    else:
        response = private_marker
    payload = json.dumps(response, sort_keys=True)
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    cache_path = blast_radius_generator._released_cache_path(programs_root, "acme")
    with sqlite3.connect(cache_path) as connection:
        connection.execute(
            "UPDATE released_result_cache SET payload = ?, released_content_hash = ?",
            (payload, content_hash),
        )
    _record_synthetic_pilot_binding(programs_root, "synthetic-revalidation-origin")

    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is None
    assert client.calls == 1
    with sqlite3.connect(cache_path) as connection:
        cached_row = connection.execute(
            "SELECT payload, released_content_hash FROM released_result_cache"
        ).fetchone()
    assert cached_row == (None if defect == "unknown-reference" else (payload, content_hash))
    events = read_events("acme", programs_root=programs_root)
    decisions = [
        event.payload for event in events
        if event.event_type == "ai.release_decision.v1"
        and event.payload.get("ai_run_id") == "synthetic-revalidation-replay"
    ]
    assert len(decisions) == 1
    assert decisions[0]["terminal"] in {"rejected", "discarded"}
    assert private_marker not in repr([event.payload for event in events])


@pytest.mark.parametrize(
    ("evidence_texts", "evidence_refs", "response_ref"),
    [
        (("Vendor confirmed the API contract review date.",), ("sig-1", "orphan-ref"), "orphan-ref"),
        (("Vendor confirmed the API contract review date.", "orphan text"), ("sig-1",), "sig-1"),
    ],
)
def test_unpaired_evidence_is_discarded_before_policy_cache_or_provider(
    evidence_texts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    response_ref: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    run_id = "unpaired-dependency-run"
    request = replace(_request(), evidence_texts=evidence_texts, evidence_refs=evidence_refs)
    response = _valid_response()
    response["evidence_refs"] = [response_ref]
    client = _FakeClient(response=response)
    policy_calls: list[object] = []
    cache_calls: list[object] = []
    load_policy = blast_radius_generator.load_ai_feature_policy
    cached_response = blast_radius_generator._cached_response

    def track_policy(*args: object, **kwargs: object) -> object:
        policy_calls.append(args)
        return load_policy(*args, **kwargs)

    def track_cache(*args: object, **kwargs: object) -> object:
        cache_calls.append(args)
        return cached_response(*args, **kwargs)

    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: run_id)
    monkeypatch.setattr(blast_radius_generator, "load_ai_feature_policy", track_policy)
    monkeypatch.setattr(blast_radius_generator, "_cached_response", track_cache)

    assert generate_dependency_blast_radius_proposal(request, client=client, programs_root=programs_root) is None
    assert client.calls == 0
    assert policy_calls == []
    assert cache_calls == []
    events = [event for event in read_events("acme", programs_root=programs_root) if event.payload.get("ai_run_id") == run_id]
    assert [event.payload["state"] for event in events if event.event_type == "ai.run_lifecycle.v1"] == ["planned"]
    assert [event.payload["terminal"] for event in events if event.event_type == "ai.release_decision.v1"] == ["discarded"]


def test_both_empty_evidence_is_supported(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["evidence_refs"] = []
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal(
        replace(_request(), evidence_texts=(), evidence_refs=()),
        client=client,
        programs_root=programs_root,
    )

    assert proposal is not None
    assert proposal.evidence_refs == ()
    assert client.calls == 1


def test_empty_next_proving_event_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["next_proving_event"] = ""
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_empty_narrative_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["blast_radius_narrative"] = ""
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_evidence_ref_outside_dependency_evidence_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["evidence_refs"] = ["sig-999"]
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_provider_exception_is_discarded_not_raised(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(error=RuntimeError("transport failed"))

    proposal = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_oversized_request_is_discarded_before_calling_the_provider(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    oversized_request = DependencyBlastRadiusRequest(
        program_id="acme", dependency_id="dep-1", from_summary="x", to_summary="x" * 200_001,
        risk_if_broken="x", current_status="active", evidence_texts=(), evidence_refs=(),
    )
    client = _FakeClient(response=_valid_response())

    proposal = generate_dependency_blast_radius_proposal(oversized_request, client=client, programs_root=programs_root)
    assert proposal is None
    assert client.calls == 0


def test_repeat_identical_request_hits_the_cache_no_second_provider_call(tmp_path: Path) -> None:
    # ADF-W5.1: dependency_blast_radius_generator is the fifth live cache adopter.
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    first = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    second = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)

    assert first is not None
    assert second is not None
    assert client.calls == 1  # second call served from the AI result cache
    assert second.next_proving_event == first.next_proving_event
    assert second.ai_run_id != first.ai_run_id


def test_different_dependency_does_not_hit_the_cache(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    other_request = DependencyBlastRadiusRequest(
        program_id="acme", dependency_id="dep-2", from_summary="program=acme, workstream=infra, WI:2002",
        to_summary="program=contoso, workstream=network, milestone=ms-2",
        risk_if_broken="A completely different downstream milestone slips.",
        current_status="active", evidence_texts=("Different evidence.",), evidence_refs=("sig-9",),
    )

    generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    generate_dependency_blast_radius_proposal(other_request, client=client, programs_root=programs_root)

    assert client.calls == 2


@pytest.mark.parametrize("client_class", [FallbackAIClient, FallbackStructuredClient])
def test_fallback_client_exposes_read_only_configured_deployments(
    client_class: type[FallbackAIClient],
) -> None:
    client = client_class(
        deployments=("primary", "backup"), temperature=0.2, budget_usd=1.0,
    )

    assert client.configured_deployments == ("primary", "backup")
    with pytest.raises(AttributeError):
        setattr(client, "configured_deployments", ("other",))


@pytest.mark.parametrize(
    ("first_chain", "second_chain"),
    [
        (("primary-a", "backup"), ("primary-b", "backup")),
        (("primary", "backup-a"), ("primary", "backup-b")),
        (("primary", "backup"), ("backup", "primary")),
        (("primary|backup", "last"), ("primary", "backup|last")),
    ],
)
def test_dependency_cache_partitions_configured_fallback_chains(
    tmp_path: Path, first_chain: tuple[str, ...], second_chain: tuple[str, ...],
) -> None:
    programs_root = tmp_path / "programs"
    first_provider = _FakeClient(response=_valid_response())
    same_chain_provider = _FakeClient(response=_valid_response())
    second_response = _valid_response()
    second_response["next_proving_event"] = "Review the second configured chain at the next checkpoint."
    second_provider = _FakeClient(response=second_response)

    def wrapper(chain: tuple[str, ...], provider: _FakeClient) -> FallbackAIClient:
        return FallbackAIClient(
            deployments=chain, temperature=0.2, budget_usd=1.0,
            client_factory=lambda **kwargs: provider,
        )

    first = generate_dependency_blast_radius_proposal(
        _request(), client=wrapper(first_chain, first_provider), programs_root=programs_root,
    )
    same = generate_dependency_blast_radius_proposal(
        _request(), client=wrapper(first_chain, same_chain_provider), programs_root=programs_root,
    )
    second = generate_dependency_blast_radius_proposal(
        _request(), client=wrapper(second_chain, second_provider), programs_root=programs_root,
    )

    assert first is not None and same is not None and second is not None
    assert same.next_proving_event == first.next_proving_event
    assert second.next_proving_event != first.next_proving_event
    assert (first_provider.calls, same_chain_provider.calls, second_provider.calls) == (1, 0, 1)
    assert len(_dependency_cache_files(programs_root)) == 2


def test_dependency_cache_keeps_historical_unknown_chain_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    provider = _FakeClient(response=_valid_response())
    client = FallbackAIClient(
        deployments=("synthetic-private-deployment",), temperature=0.2, budget_usd=1.0,
        client_factory=lambda **kwargs: provider,
    )
    with monkeypatch.context() as old_identity:
        old_identity.setattr(
            blast_radius_generator, "_resolve_dependency_cache_deployment", lambda _: "unknown", raising=False,
        )
        assert generate_dependency_blast_radius_proposal(
            _request(), client=client, programs_root=programs_root,
        ) is not None
    old_path = _dependency_cache_files(programs_root)[0]
    old_bytes = old_path.read_bytes()

    assert generate_dependency_blast_radius_proposal(
        _request(), client=client, programs_root=programs_root,
    ) is not None

    assert provider.calls == 2
    assert old_path.read_bytes() == old_bytes
    assert len(_dependency_cache_files(programs_root)) == 2
    assert all(
        "synthetic-private-deployment" not in path.read_text(encoding="utf-8")
        for path in _dependency_cache_files(programs_root)
    )


def _dependency_cache_files(programs_root: Path) -> list[Path]:
    return list((programs_root / "acme" / "rev_cache" / "ai_result").glob("*.json"))


@pytest.mark.parametrize("failure", ["schema", "missing", "unknown_ref"])
def test_dependency_release_cache_rejection_does_not_poison_next_call(tmp_path: Path, failure: str) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    if failure == "schema":
        response["next_proving_event"] = "x" * 200_001
    elif failure == "missing":
        response["next_proving_event"] = ""
    else:
        response["evidence_refs"] = ["not-in-request"]
    client = _FakeClient(response=response)

    assert generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root) is None
    assert _dependency_cache_files(programs_root) == []

    client.response = _valid_response()
    first = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    second = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert first is not None and second is not None
    assert client.calls == 2
    assert first.next_proving_event == second.next_proving_event


def test_dependency_release_cache_stores_only_sanitized_content_after_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["next_proving_event"] = "Contact synthetic@example.invalid due to the review."
    response["blast_radius_narrative"] = "The change was caused by the synthetic dependency."
    response["extra_private_field"] = "synthetic-secret-extra"
    client = _FakeClient(response=response)
    original_put = blast_radius_generator.put_ai_result
    writes: list[dict[str, Any]] = []

    def checked_put(key: AIResultCacheKey, value: dict[str, Any], *, programs_root: Path) -> Path:
        events = read_events("acme", programs_root=programs_root)
        releases = [
            event for event in events
            if event.event_type == "ai.release_decision.v1"
            and event.payload.get("ai_run_id") == value["release_run_id"]
        ]
        assert len(releases) == 1
        assert releases[0].payload["terminal"] == "released"
        assert releases[0].payload["released_content_hash"] == canonical_input_hash(canonical_json(value["proposal"]))
        writes.append(value)
        return original_put(key, value, programs_root=programs_root)

    monkeypatch.setattr(blast_radius_generator, "put_ai_result", checked_put)
    first = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    second = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)

    assert first is not None and second is not None
    assert client.calls == 1
    assert len(writes) == 1
    assert second.ai_run_id != first.ai_run_id
    assert second.next_proving_event == first.next_proving_event
    assert second.blast_radius_narrative == first.blast_radius_narrative
    assert "[PII-FILTERED-EMAIL]" in first.next_proving_event
    assert "observed with" in first.blast_radius_narrative
    stored = _dependency_cache_files(programs_root)[0].read_text(encoding="utf-8")
    assert "synthetic@example.invalid" not in stored
    assert "synthetic-secret-extra" not in stored


def test_dependency_release_cache_release_failure_never_writes_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    original_record = blast_radius_generator.record_ai_release_decision

    def fail_release(**kwargs: Any) -> None:
        if kwargs["terminal"] is ReleaseTerminal.RELEASED:
            raise OSError("synthetic release failure")
        original_record(**kwargs)

    monkeypatch.setattr(blast_radius_generator, "record_ai_release_decision", fail_release)
    with pytest.raises(OSError, match="synthetic release failure"):
        generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert client.calls == 1
    assert _dependency_cache_files(programs_root) == []


def test_dependency_release_cache_write_failure_is_explicit_after_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: "synthetic-write-failure")

    def fail_cache_write(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic-secret-cache-detail")

    monkeypatch.setattr(blast_radius_generator, "put_ai_result", fail_cache_write)
    with pytest.raises(StateError, match="cache") as raised:
        generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert "synthetic-secret-cache-detail" not in str(raised.value)
    assert client.calls == 1
    releases = [
        event.payload["terminal"] for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1"
        and event.payload.get("ai_run_id") == "synthetic-write-failure"
    ]
    assert releases == ["released"]


def test_dependency_release_cache_partial_write_failure_preserves_release_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    original_put = blast_radius_generator.put_ai_result

    def fail_after_write(key: AIResultCacheKey, value: dict[str, Any], *, programs_root: Path) -> Path:
        original_put(key, value, programs_root=programs_root)
        raise OSError("synthetic-secret-after-write")

    monkeypatch.setattr(blast_radius_generator, "put_ai_result", fail_after_write)
    with pytest.raises(StateError, match="cache") as raised:
        generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert "synthetic-secret-after-write" not in str(raised.value)
    assert len(_dependency_cache_files(programs_root)) == 1

    monkeypatch.setattr(blast_radius_generator, "put_ai_result", original_put)
    assert generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root) is not None
    assert client.calls == 1


@pytest.mark.parametrize("damage_cache", [False, True])
@pytest.mark.parametrize("damage_ledger", ["malformed", "recursion"])
def test_dependency_release_cache_corrupt_origin_ledger_is_not_repaired_or_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage_cache: bool, damage_ledger: str,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    assert generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root) is not None
    ledger_path = next(get_event_logs_dir("acme", programs_root=programs_root).glob("*.events.jsonl"))
    damage = b'{"incomplete":' if damage_ledger == "malformed" else b"[" * 2000 + b"0" + b"]" * 2000
    ledger_path.write_bytes(ledger_path.read_bytes() + damage)
    if damage_cache:
        _dependency_cache_files(programs_root)[0].write_text('{"incomplete":', encoding="utf-8")
    before = {p.relative_to(programs_root): p.read_bytes() for p in programs_root.rglob("*") if p.is_file()}

    if damage_ledger == "recursion":
        def fail_ledger_read(*args: object, **kwargs: object) -> None:
            raise RecursionError("synthetic-private-ledger")

        monkeypatch.setattr(blast_radius_generator, "read_events", fail_ledger_read)
        monkeypatch.setattr("src.core.quality_gates.ai_release_audit.read_events", fail_ledger_read)

    with pytest.raises(StateError, match="audit"):
        generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)

    after = {p.relative_to(programs_root): p.read_bytes() for p in programs_root.rglob("*") if p.is_file()}
    assert after == before
    assert client.calls == 1


def test_dependency_release_cache_provider_state_error_remains_a_provider_failure(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(error=StateError("synthetic-provider-failure"))
    assert generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root) is None
    decisions = [
        event.payload for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1"
    ]
    assert decisions[-1]["terminal"] == "discarded"
    assert "provider call failed" in decisions[-1]["reason"]


def test_dependency_release_cache_does_not_replay_historical_raw_entry(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    request = _request()
    client = _FakeClient(response=_valid_response())
    legacy_key = AIResultCacheKey(
        program_id=request.program_id,
        feature="dependency_blast_radius_generator",
        canonical_input_hash=canonical_input_hash(blast_radius_generator._build_user_prompt(request)),
        prompt_version=blast_radius_generator.PROMPT_VERSION,
        policy_version=blast_radius_generator.POLICY_VERSION,
        model_deployment=blast_radius_generator._resolve_model_deployment(client),
        context_manifest_hash=canonical_input_hash("|".join(request.evidence_refs)),
        output_schema_version="1",
    )
    old_path = put_ai_result(legacy_key, _valid_response(), programs_root=programs_root)
    before = old_path.read_bytes()

    assert generate_dependency_blast_radius_proposal(request, client=client, programs_root=programs_root) is not None
    assert client.calls == 1
    assert old_path.read_bytes() == before
    assert len(_dependency_cache_files(programs_root)) == 2


def test_dependency_release_cache_distinguishes_dependency_id_even_with_equal_prompt(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    request = _request()
    assert generate_dependency_blast_radius_proposal(request, client=client, programs_root=programs_root) is not None
    assert generate_dependency_blast_radius_proposal(
        replace(request, dependency_id="dep-2"), client=client, programs_root=programs_root,
    ) is not None
    assert client.calls == 2


@pytest.mark.parametrize("program_id", ["", "..", r"..\other", "other/child", r"C:\synthetic"])
def test_dependency_release_cache_rejects_nonbare_program_before_cache_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_id: str,
) -> None:
    client = _FakeClient(response=_valid_response())
    monkeypatch.setattr(
        blast_radius_generator, "_cached_response",
        lambda *args, **kwargs: pytest.fail("invalid program reached the cache"),
    )

    with pytest.raises(StateError, match="bare program identifier"):
        generate_dependency_blast_radius_proposal(
            replace(_request(), program_id=program_id), client=client, programs_root=tmp_path / "programs",
        )
    assert client.calls == 0
    assert not (tmp_path / "programs").exists()


def test_dependency_release_cache_expired_inner_corruption_is_a_miss(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    assert generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root) is not None
    path = _dependency_cache_files(programs_root)[0]
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["captured_at_epoch"] = 1.0
    entry["payload"]["value"] = {"unrecognized": "synthetic-private-canary"}
    path.write_text(json.dumps(entry), encoding="utf-8")

    assert generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root) is not None
    assert generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root) is not None
    assert client.calls == 2
    assert "synthetic-private-canary" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "damage",
    [
        "malformed_json", "invalid_utf8", "stale_schema", "invalid_time", "missing_value", "model_mismatch",
        "malformed_envelope", "modified_content", "wrong_request", "unknown_release", "later_rejection",
        "later_release_without_hash",
    ],
)
def test_dependency_release_cache_invalid_entry_fails_closed_without_touch_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-original", "synthetic-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    assert generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root) is not None
    path = _dependency_cache_files(programs_root)[0]
    entry = json.loads(path.read_text(encoding="utf-8"))
    if damage == "malformed_json":
        path.write_text('{"private-example":', encoding="utf-8")
    elif damage == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif damage in {"later_rejection", "later_release_without_hash"}:
        record_ai_release_decision(
            program_id="acme", ai_run_id="synthetic-original",
            terminal=ReleaseTerminal.REJECTED if damage == "later_rejection" else ReleaseTerminal.RELEASED,
            reason="synthetic later decision", validator_finding_count=0, programs_root=programs_root,
        )
    else:
        if damage == "stale_schema":
            entry["schema_version"] = "invalid"
        elif damage == "invalid_time":
            entry["captured_at_epoch"] = "invalid"
        elif damage == "missing_value":
            del entry["payload"]["value"]
        elif damage == "model_mismatch":
            entry["payload"]["model_deployment"] = "different-model"
        elif damage == "malformed_envelope":
            entry["payload"]["value"] = {"unrecognized": "synthetic-private-canary"}
        elif damage == "modified_content":
            entry["payload"]["value"]["proposal"]["next_proving_event"] = "tampered"
        elif damage == "wrong_request":
            entry["payload"]["value"]["proposal"]["request_hash"] = "different-request"
        else:
            entry["payload"]["value"]["release_run_id"] = "unknown-run"
        path.write_text(json.dumps(entry), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(StateError, match="cache") as raised:
        generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert "synthetic-private-canary" not in str(raised.value)
    assert client.calls == 1
    assert path.read_bytes() == before
    decisions = [
        event.payload for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1"
        and event.payload.get("ai_run_id") == "synthetic-replay"
    ]
    assert len(decisions) == 1
    assert decisions[0]["terminal"] == "discarded"
    assert "synthetic-private-canary" not in decisions[0]["reason"]


# ---------------------------------------------------------------------------
# RELO-W1.3 pilot: generate_dependency_blast_radius_proposal_via_runner.
# Mirrors every test above exactly, proving behavioral equivalence with the
# original implementation now that dispatch goes through
# StructuredFeatureRunner instead of route_through_tiers/AIResultCache directly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "states", "terminal", "calls"),
    [
        ("success", ("planned", "requested", "responded", "schema_validated", "semantically_validated"), "released", 1),
        ("provider-failure", ("planned", "requested"), "discarded", 1),
        ("outbound-bounds", ("planned",), "discarded", 0),
        ("context-refusal", ("planned", "requested"), "discarded", 0),
        ("non-object-response", ("planned", "requested", "responded"), "discarded", 1),
        ("inbound-bounds", ("planned", "requested", "responded"), "discarded", 1),
        ("invalid-shape", ("planned", "requested", "responded", "schema_validated"), "rejected", 1),
        (
            "semantic-refusal",
            ("planned", "requested", "responded", "schema_validated", "semantically_validated"),
            "rejected", 1,
        ),
        (
            "cache-write-failure",
            ("planned", "requested", "responded", "schema_validated", "semantically_validated"),
            "discarded", 1,
        ),
    ],
)
def test_via_runner_lifecycle_records_only_reached_fresh_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
    states: tuple[str, ...], terminal: str, calls: int,
) -> None:
    programs_root = tmp_path / "programs"
    run_id = "synthetic-fresh-lifecycle"
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: run_id)
    request = _request()
    response = _valid_response()
    client = _FakeClient(response=response)

    if scenario == "provider-failure":
        client.error = RuntimeError("synthetic transport failure")
    elif scenario == "outbound-bounds":
        request = replace(request, to_summary="x" * 200_001)
    elif scenario == "context-refusal":
        request = replace(request, evidence_refs=("sig-1", "orphan-ref"))
    elif scenario == "non-object-response":
        client.response = None
    elif scenario == "inbound-bounds":
        response["next_proving_event"] = "x" * 200_001
    elif scenario == "invalid-shape":
        response["next_proving_event"] = None
    elif scenario == "semantic-refusal":
        response["evidence_refs"] = ["sig-unknown"]
    elif scenario == "cache-write-failure":
        def fail_cache_write(*args: object, **kwargs: object) -> None:
            raise StateError("synthetic cache write failure")

        monkeypatch.setattr(blast_radius_generator.ReleasedResultCache, "put", fail_cache_write)

    proposal = generate_dependency_blast_radius_proposal_via_runner(
        request, context=_context(), client=client, programs_root=programs_root,
    )

    assert (proposal is not None) == (terminal == "released")
    assert client.calls == calls
    assert _pilot_audit(programs_root, run_id) == [
        *(("state", state) for state in states), ("terminal", terminal),
    ]


@pytest.mark.parametrize(
    ("scenario", "states", "terminal"),
    [
        ("success", ("planned", "requested", "responded", "schema_validated", "semantically_validated"), "released"),
        ("checksum", ("planned", "requested"), "discarded"),
        ("malformed-json", ("planned", "requested"), "discarded"),
        ("null", ("planned", "requested"), "discarded"),
        ("inbound-bounds", ("planned", "requested", "responded"), "discarded"),
        ("invalid-shape", ("planned", "requested", "responded", "schema_validated"), "discarded"),
        (
            "semantic-refusal",
            ("planned", "requested", "responded", "schema_validated", "semantically_validated"),
            "rejected",
        ),
    ],
)
def test_via_runner_lifecycle_records_only_reached_cache_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
    states: tuple[str, ...], terminal: str,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-lifecycle-origin", "synthetic-lifecycle-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is not None

    response: object = _valid_response()
    if scenario == "null":
        response = None
    elif scenario == "inbound-bounds":
        response = {**_valid_response(), "next_proving_event": "x" * 200_001}
    elif scenario == "invalid-shape":
        response = {**_valid_response(), "next_proving_event": 42}
    elif scenario == "semantic-refusal":
        response = {**_valid_response(), "evidence_refs": ["sig-unknown"]}
    payload = '{"synthetic":' if scenario == "malformed-json" else json.dumps(response, sort_keys=True)
    if scenario != "success":
        cache_path = blast_radius_generator._released_cache_path(programs_root, "acme")
        with sqlite3.connect(cache_path) as connection:
            if scenario == "checksum":
                connection.execute("UPDATE released_result_cache SET payload = ?", ("synthetic-tamper",))
            else:
                connection.execute(
                    "UPDATE released_result_cache SET payload = ?, released_content_hash = ?",
                    (payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()),
                )
        if scenario in {"inbound-bounds", "invalid-shape", "semantic-refusal"}:
            _record_synthetic_pilot_binding(programs_root, "synthetic-lifecycle-origin")

    proposal = generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    )

    assert (proposal is not None) == (terminal == "released")
    assert client.calls == 1
    assert _pilot_audit(programs_root, "synthetic-lifecycle-replay") == [
        *(("state", state) for state in states), ("terminal", terminal),
    ]
    expected_reasons = {
        "inbound-bounds": "released payload failed structural validation.",
        "invalid-shape": "released payload failed to re-parse into a proposal.",
        "semantic-refusal": "released payload failed dependency semantic validation.",
    }
    if scenario in expected_reasons:
        decisions = [
            event.payload for event in read_events("acme", programs_root=programs_root)
            if event.event_type == "ai.release_decision.v1"
            and event.payload.get("ai_run_id") == "synthetic-lifecycle-replay"
        ]
        assert decisions[0]["reason"] == expected_reasons[scenario]
        assert decisions[0]["validator_finding_count"] == int(scenario == "semantic-refusal")


def test_pilot_replay_preserves_multiple_semantic_finding_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-findings-origin", "synthetic-findings-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is not None
    assert len(_pilot_cache_rows(programs_root)) == 1
    monkeypatch.setattr(
        blast_radius_generator, "_validate_semantics", lambda *args, **kwargs: ("first", "second"),
    )

    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is None

    decisions = [
        event.payload for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1"
        and event.payload.get("ai_run_id") == "synthetic-findings-replay"
    ]
    assert len(decisions) == 1
    assert decisions[0]["terminal"] == "rejected"
    assert decisions[0]["reason"] == "released payload failed dependency semantic validation."
    assert decisions[0]["validator_finding_count"] == 2
    assert _pilot_cache_rows(programs_root) == []
    assert client.calls == 1


@pytest.mark.parametrize("failure", ["invalid-shape", "semantic-refusal"])
def test_pilot_refuses_invalid_serialized_revalidation_without_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_id = "synthetic-invalid-serialized"
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: run_id)
    original_parse = blast_radius_generator._parse_proposal
    original_validate = blast_radius_generator._validate_semantics
    parses = 0
    validations = 0

    def parse(*args: Any, **kwargs: Any) -> Any:
        nonlocal parses
        parses += 1
        if failure == "invalid-shape" and parses == 3:
            return None
        return original_parse(*args, **kwargs)

    def validate(*args: Any, **kwargs: Any) -> tuple[str, ...]:
        nonlocal validations
        validations += 1
        if failure == "semantic-refusal" and validations == 2:
            return ("synthetic revalidation finding",)
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(blast_radius_generator, "_parse_proposal", parse)
    monkeypatch.setattr(blast_radius_generator, "_validate_semantics", validate)

    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is None
    assert _pilot_audit(programs_root, run_id) == [
        *(("state", state.value) for state in AIRunState), ("terminal", "discarded"),
    ]
    assert _pilot_cache_rows(programs_root) == []
    assert client.calls == 1


@pytest.mark.parametrize("error_type", [OSError, StateError])
@pytest.mark.parametrize("failed_state", list(AIRunState))
@pytest.mark.parametrize("cache_hit", [False, True], ids=["fresh", "cache"])
def test_via_runner_lifecycle_write_failure_cannot_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception],
    failed_state: AIRunState, cache_hit: bool,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-audit-origin", "synthetic-audit-attempt")) if cache_hit else iter(("synthetic-audit-attempt",))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    if cache_hit:
        assert generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=_context(), client=client, programs_root=programs_root,
        ) is not None

    record_lifecycle = blast_radius_generator.record_ai_run_lifecycle

    def fail_lifecycle(**kwargs: Any) -> None:
        if kwargs["state"] == failed_state:
            raise error_type("synthetic lifecycle write failure")
        record_lifecycle(**kwargs)

    monkeypatch.setattr(blast_radius_generator, "record_ai_run_lifecycle", fail_lifecycle)
    with pytest.raises(error_type, match="synthetic lifecycle write failure"):
        generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=_context(), client=client, programs_root=programs_root,
        )

    assert client.calls == int(cache_hit or failed_state not in {AIRunState.PLANNED, AIRunState.REQUESTED})
    all_states = [state.value for state in AIRunState]
    assert _pilot_audit(programs_root, "synthetic-audit-attempt") == [
        ("state", state) for state in all_states[:all_states.index(failed_state.value)]
    ]
    cache_path = blast_radius_generator._released_cache_path(programs_root, "acme")
    if cache_path.exists():
        with sqlite3.connect(cache_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == int(cache_hit)


def test_via_runner_terminal_write_failure_does_not_return_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_id = "synthetic-terminal-failure"
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: run_id)
    record_decision = blast_radius_generator.record_ai_release_decision

    def fail_release(**kwargs: Any) -> None:
        if kwargs["terminal"] == ReleaseTerminal.RELEASED:
            raise OSError("synthetic terminal write failure")
        record_decision(**kwargs)

    monkeypatch.setattr(blast_radius_generator, "record_ai_release_decision", fail_release)
    with pytest.raises(OSError, match="synthetic terminal write failure"):
        generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=_context(), client=client, programs_root=programs_root,
        )

    assert client.calls == 1
    assert _pilot_audit(programs_root, run_id) == [("state", state.value) for state in AIRunState]
    with sqlite3.connect(blast_radius_generator._released_cache_path(programs_root, "acme")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 0


def test_pilot_release_binding_precedes_cache_write_and_names_ai_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    cache_path = blast_radius_generator._released_cache_path(programs_root, "acme")
    client = _FakeClient(response=_valid_response())
    run_id = "synthetic-binding-origin"
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: run_id)
    original_open = structured_feature_runner.open_program_db
    cache_opens = 0
    released_before_write = False

    def observe_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal cache_opens, released_before_write
        if path == cache_path:
            cache_opens += 1
            if cache_opens == 2:
                decisions = [
                    event.payload for event in read_events("acme", programs_root=programs_root, repair_corruption=False)
                    if event.event_type == "ai.release_decision.v1" and event.payload.get("ai_run_id") == run_id
                ]
                released_before_write = len(decisions) == 1 and decisions[0]["terminal"] == "released"
                assert _pilot_cache_rows(programs_root) == []
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(structured_feature_runner, "open_program_db", observe_open)
    proposal = generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    )
    assert proposal is not None
    assert released_before_write
    [(cache_key, payload, payload_hash, release_run_id)] = _pilot_cache_rows(programs_root)
    assert payload_hash == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert release_run_id == proposal.ai_run_id == run_id
    assert release_run_id != _context().run_id
    decisions = [
        event.payload for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1" and event.payload.get("ai_run_id") == run_id
    ]
    assert decisions[0]["released_content_hash"] == _pilot_release_binding_hash(cache_key, payload)


def test_pilot_release_binding_stores_only_sanitized_proposal_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["next_proving_event"] = "Contact synthetic@example.invalid due to the review."
    response["blast_radius_narrative"] = "The change was caused by the synthetic dependency."
    response["extra_private_field"] = "synthetic-secret-extra"
    client = _FakeClient(response=response)
    run_ids = iter(("synthetic-sanitized-origin", "synthetic-sanitized-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))

    first = generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    )
    replay = generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    )

    assert first is not None and replay is not None
    assert client.calls == 1
    assert "[PII-FILTERED-EMAIL]" in first.next_proving_event
    assert "observed with" in first.blast_radius_narrative
    assert replay.next_proving_event == first.next_proving_event
    assert replay.blast_radius_narrative == first.blast_radius_narrative
    [(cache_key, payload, _, origin)] = _pilot_cache_rows(programs_root)
    assert "synthetic@example.invalid" not in payload
    assert "synthetic-secret-extra" not in payload
    assert origin == first.ai_run_id
    assert json.loads(payload) == {
        "next_proving_event": first.next_proving_event,
        "blast_radius_narrative": first.blast_radius_narrative,
        "evidence_refs": list(first.evidence_refs),
    }
    decisions = [
        event.payload for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1" and event.payload.get("ai_run_id") == origin
    ]
    assert decisions[0]["released_content_hash"] == _pilot_release_binding_hash(cache_key, payload)


@pytest.mark.parametrize("stage", ["fresh", "replay"])
def test_pilot_release_binding_refuses_changed_revalidated_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-stability-origin", "synthetic-stability-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    before: list[tuple[str, str, str, str]] = []
    if stage == "replay":
        assert generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=_context(), client=client, programs_root=programs_root,
        ) is not None
        before = _pilot_cache_rows(programs_root)

    original_parse = blast_radius_generator._parse_proposal
    parses = 0

    def change_revalidated_proposal(*args: Any, **kwargs: Any) -> Any:
        nonlocal parses
        parses += 1
        proposal = original_parse(*args, **kwargs)
        if proposal is not None and (stage == "replay" or parses == 3):
            return replace(proposal, next_proving_event="A different sanitized checkpoint.")
        return proposal

    monkeypatch.setattr(blast_radius_generator, "_parse_proposal", change_revalidated_proposal)
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is None
    run_id = "synthetic-stability-origin" if stage == "fresh" else "synthetic-stability-replay"
    assert _pilot_audit(programs_root, run_id)[-1] == ("terminal", "discarded")
    if stage == "replay":
        decisions = [
            event.payload for event in read_events("acme", programs_root=programs_root)
            if event.event_type == "ai.release_decision.v1" and event.payload.get("ai_run_id") == run_id
        ]
        assert decisions[0]["reason"] == "revalidated proposal did not match released payload."
    assert client.calls == 1
    assert _pilot_cache_rows(programs_root) == before


@pytest.mark.parametrize(
    "damage", ["missing", "legacy", "rejected", "release-without-hash", "modified-payload"],
)
def test_pilot_release_binding_rejects_unverified_origin_without_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    programs_root = tmp_path / "programs"
    cache_path = blast_radius_generator._released_cache_path(programs_root, "acme")
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-binding-origin", "synthetic-binding-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is not None

    if damage in {"rejected", "release-without-hash"}:
        record_ai_release_decision(
            program_id="acme", ai_run_id="synthetic-binding-origin",
            terminal=ReleaseTerminal.REJECTED if damage == "rejected" else ReleaseTerminal.RELEASED,
            reason="synthetic later decision", validator_finding_count=0, programs_root=programs_root,
        )
    else:
        with sqlite3.connect(cache_path) as connection:
            if damage == "modified-payload":
                changed = {**_valid_response(), "next_proving_event": "A different but valid checkpoint."}
                payload = json.dumps(changed, sort_keys=True)
                connection.execute(
                    "UPDATE released_result_cache SET payload = ?, released_content_hash = ?",
                    (payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()),
                )
            else:
                origin = _context().run_id if damage == "legacy" else "missing-origin"
                connection.execute("UPDATE released_result_cache SET release_run_id = ?", (origin,))

    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is None
    assert client.calls == 1
    assert _pilot_cache_rows(programs_root) == []
    assert _pilot_audit(programs_root, "synthetic-binding-replay") == [
        ("state", "planned"), ("state", "requested"), ("terminal", "discarded"),
    ]


def test_pilot_release_binding_rejects_valid_payload_from_different_cache_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    cache_path = blast_radius_generator._released_cache_path(programs_root, "acme")
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-identity-a", "synthetic-identity-b", "synthetic-identity-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    context_a = _context()
    context_b = replace(context_a, snapshot_id="another-snapshot")
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=context_a, client=client, programs_root=programs_root,
    ) is not None
    [(key_a, payload_a, payload_hash_a, origin_a)] = _pilot_cache_rows(programs_root)
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=context_b, client=client, programs_root=programs_root,
    ) is not None
    rows = _pilot_cache_rows(programs_root)
    [(key_b, _, _, _)] = [row for row in rows if row[0] != key_a]
    with sqlite3.connect(cache_path) as connection:
        connection.execute(
            "UPDATE released_result_cache SET payload = ?, released_content_hash = ?, release_run_id = ? "
            "WHERE cache_key = ?",
            (payload_a, payload_hash_a, origin_a, key_b),
        )

    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=context_b, client=client, programs_root=programs_root,
    ) is None
    assert client.calls == 2
    assert [row[0] for row in _pilot_cache_rows(programs_root)] == [key_a]


def test_pilot_release_binding_valid_replay_records_new_bound_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-binding-origin", "synthetic-binding-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    first = generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    )
    second = generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    )
    assert first is not None and second is not None
    assert first.ai_run_id != second.ai_run_id
    assert first.next_proving_event == second.next_proving_event
    assert client.calls == 1
    [(cache_key, payload, _, origin)] = _pilot_cache_rows(programs_root)
    assert origin == first.ai_run_id
    expected_hash = _pilot_release_binding_hash(cache_key, payload)
    decisions = [
        event.payload for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1" and event.payload.get("ai_run_id") in {
            first.ai_run_id, second.ai_run_id,
        }
    ]
    assert len(decisions) == 2
    assert all(decision["terminal"] == "released" for decision in decisions)
    assert all(decision["released_content_hash"] == expected_hash for decision in decisions)


def test_pilot_release_binding_audit_read_error_preserves_row_and_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-binding-origin", "synthetic-binding-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is not None
    before = _pilot_cache_rows(programs_root)

    def fail_read(*args: Any, **kwargs: Any) -> bool:
        raise ConfigError("synthetic-private-ledger-detail")

    monkeypatch.setattr(blast_radius_generator, "released_content_matches", fail_read)
    with pytest.raises(StateError, match="release audit could not be verified") as error:
        generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=_context(), client=client, programs_root=programs_root,
        )
    assert "synthetic-private-ledger-detail" not in str(error.value)
    assert client.calls == 1
    assert _pilot_cache_rows(programs_root) == before
    assert _pilot_audit(programs_root, "synthetic-binding-replay") == [
        ("state", "planned"), ("state", "requested"),
    ]


def test_pilot_release_binding_audit_error_precedes_malformed_payload_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    cache_path = blast_radius_generator._released_cache_path(programs_root, "acme")
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-binding-origin", "synthetic-binding-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is not None
    payload = '{"synthetic":'
    with sqlite3.connect(cache_path) as connection:
        connection.execute(
            "UPDATE released_result_cache SET payload = ?, released_content_hash = ?",
            (payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()),
        )
    before = _pilot_cache_rows(programs_root)
    verifier = Mock(side_effect=ConfigError("synthetic-private-ledger-detail"))
    monkeypatch.setattr(blast_radius_generator, "released_content_matches", verifier)

    with pytest.raises(StateError, match="release audit could not be verified") as error:
        generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=_context(), client=client, programs_root=programs_root,
        )

    assert "synthetic-private-ledger-detail" not in str(error.value)
    verifier.assert_called_once()
    assert client.calls == 1
    assert _pilot_cache_rows(programs_root) == before
    assert _pilot_audit(programs_root, "synthetic-binding-replay") == [
        ("state", "planned"), ("state", "requested"),
    ]


def test_pilot_release_binding_corrupt_ledger_is_not_repaired_before_origin_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-binding-origin", "synthetic-binding-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is not None
    ledger_path = next(get_event_logs_dir("acme", programs_root=programs_root).glob("*.events.jsonl"))
    ledger_path.write_bytes(ledger_path.read_bytes() + b'{"incomplete":')
    before_ledger = ledger_path.read_bytes()
    before_cache = _pilot_cache_rows(programs_root)

    with pytest.raises(StateError, match="release audit could not be verified"):
        generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=_context(), client=client, programs_root=programs_root,
        )
    assert ledger_path.read_bytes() == before_ledger
    assert _pilot_cache_rows(programs_root) == before_cache
    assert client.calls == 1


def test_pilot_release_binding_ambiguous_terminal_failure_writes_no_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_id = "synthetic-ambiguous-terminal"
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: run_id)
    record_decision = blast_radius_generator.record_ai_release_decision

    def fail_after_release(**kwargs: Any) -> None:
        record_decision(**kwargs)
        if kwargs["terminal"] == ReleaseTerminal.RELEASED:
            raise OSError("synthetic terminal write outcome unknown")

    monkeypatch.setattr(blast_radius_generator, "record_ai_release_decision", fail_after_release)
    with pytest.raises(OSError, match="outcome unknown"):
        generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=_context(), client=client, programs_root=programs_root,
        )
    assert client.calls == 1
    assert _pilot_cache_rows(programs_root) == []
    assert _pilot_audit(programs_root, run_id) == [
        *(("state", state.value) for state in AIRunState), ("terminal", "released"),
    ]


def test_pilot_release_binding_cache_write_failure_keeps_release_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    cache_path = blast_radius_generator._released_cache_path(programs_root, "acme")
    client = _FakeClient(response=_valid_response())
    run_id = "synthetic-binding-write-failure"
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: run_id)
    original_open = structured_feature_runner.open_program_db
    cache_opens = 0

    def fail_second_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal cache_opens
        if path == cache_path:
            cache_opens += 1
            if cache_opens == 2:
                raise sqlite3.OperationalError("synthetic-private-cache-detail")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(structured_feature_runner, "open_program_db", fail_second_open)
    with pytest.raises(StateError, match="after release") as error:
        generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=_context(), client=client, programs_root=programs_root,
        )
    assert "synthetic-private-cache-detail" not in str(error.value)
    assert client.calls == 1
    assert _pilot_cache_rows(programs_root) == []
    assert _pilot_audit(programs_root, run_id) == [
        *(("state", state.value) for state in AIRunState), ("terminal", "released"),
    ]


def test_pilot_release_binding_ambiguous_cache_write_preserves_replayable_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-ambiguous-cache", "synthetic-ambiguous-replay"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))
    original_put = blast_radius_generator.ReleasedResultCache.put

    def fail_after_put(self: Any, *args: Any, **kwargs: Any) -> None:
        original_put(self, *args, **kwargs)
        raise OSError("synthetic-private-after-commit")

    monkeypatch.setattr(blast_radius_generator.ReleasedResultCache, "put", fail_after_put)
    with pytest.raises(StateError, match="after release") as error:
        generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=_context(), client=client, programs_root=programs_root,
        )
    assert "synthetic-private-after-commit" not in str(error.value)
    [(cache_key, payload, _, origin)] = _pilot_cache_rows(programs_root)
    assert origin == "synthetic-ambiguous-cache"
    assert _pilot_audit(programs_root, origin)[-1] == ("terminal", "released")
    decisions = [
        event.payload for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1" and event.payload.get("ai_run_id") == origin
    ]
    assert decisions[0]["released_content_hash"] == _pilot_release_binding_hash(cache_key, payload)

    monkeypatch.setattr(blast_radius_generator.ReleasedResultCache, "put", original_put)
    replay = generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    )
    assert replay is not None and replay.ai_run_id == "synthetic-ambiguous-replay"
    assert client.calls == 1


def test_pilot_release_binding_revalidates_serialized_response_before_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_id = "synthetic-serialized-refusal"
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: run_id)
    original_parse = blast_radius_generator._parse_proposal
    parses = 0

    def fail_serialized_parse(*args: Any, **kwargs: Any) -> Any:
        nonlocal parses
        parses += 1
        return None if parses == 3 else original_parse(*args, **kwargs)

    monkeypatch.setattr(blast_radius_generator, "_parse_proposal", fail_serialized_parse)
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is None
    assert client.calls == 1
    assert parses == 3
    assert _pilot_cache_rows(programs_root) == []
    assert _pilot_audit(programs_root, run_id)[-1] == ("terminal", "discarded")


def test_via_runner_valid_response_is_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    proposal = generate_dependency_blast_radius_proposal_via_runner(_request(), context=_context(), client=client, programs_root=programs_root)

    assert proposal is not None
    assert "API contract review" in proposal.next_proving_event
    assert "cascading" in proposal.blast_radius_narrative
    assert proposal.evidence_refs == ("sig-1",)
    assert released_terminal_for_run(proposal.ai_run_id, program_id="acme", programs_root=programs_root).value == "released"


def test_via_runner_empty_next_proving_event_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["next_proving_event"] = ""
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal_via_runner(_request(), context=_context(), client=client, programs_root=programs_root)
    assert proposal is None


def test_via_runner_empty_narrative_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["blast_radius_narrative"] = ""
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal_via_runner(_request(), context=_context(), client=client, programs_root=programs_root)
    assert proposal is None


def test_via_runner_evidence_ref_outside_dependency_evidence_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["evidence_refs"] = ["sig-999"]
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal_via_runner(_request(), context=_context(), client=client, programs_root=programs_root)
    assert proposal is None


def test_via_runner_provider_exception_is_discarded_not_raised(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(error=RuntimeError("transport failed"))

    proposal = generate_dependency_blast_radius_proposal_via_runner(_request(), context=_context(), client=client, programs_root=programs_root)
    assert proposal is None


def test_via_runner_oversized_request_is_discarded_before_calling_the_provider(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    oversized_request = DependencyBlastRadiusRequest(
        program_id="acme", dependency_id="dep-1", from_summary="x", to_summary="x" * 200_001,
        risk_if_broken="x", current_status="active", evidence_texts=(), evidence_refs=(),
    )
    client = _FakeClient(response=_valid_response())

    proposal = generate_dependency_blast_radius_proposal_via_runner(oversized_request, context=_context(), client=client, programs_root=programs_root)
    assert proposal is None
    assert client.calls == 0


def test_via_runner_repeat_identical_request_hits_the_cache_no_second_provider_call(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    first = generate_dependency_blast_radius_proposal_via_runner(_request(), context=_context(), client=client, programs_root=programs_root)
    second = generate_dependency_blast_radius_proposal_via_runner(_request(), context=_context(), client=client, programs_root=programs_root)

    assert first is not None
    assert second is not None
    assert client.calls == 1  # second call served from the runner's ReleasedResultCache
    assert second.next_proving_event == first.next_proving_event
    assert second.ai_run_id != first.ai_run_id


def test_via_runner_corrupt_cache_is_discarded_without_second_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-first", "synthetic-second"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))

    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is not None
    with sqlite3.connect(blast_radius_generator._released_cache_path(programs_root, "acme")) as connection:
        connection.execute("UPDATE released_result_cache SET payload = ?", ("tampered-secret",))

    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is None
    assert client.calls == 1
    events = [
        event for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1" and event.payload.get("ai_run_id") == "synthetic-second"
    ]
    assert len(events) == 1
    assert events[0].payload["terminal"] == "discarded"
    assert events[0].payload["reason"] == "Structured runner state failure; no proposal released."


def test_via_runner_malformed_cached_json_is_discarded_and_evicted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_ids = iter(("synthetic-first", "synthetic-second"))
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: next(run_ids))

    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is not None
    db_path = blast_radius_generator._released_cache_path(programs_root, "acme")
    malformed = '{"private-example":'
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE released_result_cache SET payload = ?, released_content_hash = ?",
            (malformed, hashlib.sha256(malformed.encode("utf-8")).hexdigest()),
        )

    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is None
    assert client.calls == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 0
    events = [
        event for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1" and event.payload.get("ai_run_id") == "synthetic-second"
    ]
    assert len(events) == 1
    assert events[0].payload["terminal"] == "discarded"
    assert events[0].payload["reason"] == "Structured runner state failure; no proposal released."


def test_via_runner_cache_write_failure_is_discarded_after_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    run_id = "synthetic-write-failure"
    monkeypatch.setattr(blast_radius_generator, "new_ai_run_id", lambda: run_id)

    def fail_cache_write(*args: object, **kwargs: object) -> None:
        raise StateError("sensitive cache detail")

    monkeypatch.setattr(blast_radius_generator.ReleasedResultCache, "put", fail_cache_write)
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=programs_root,
    ) is None
    assert client.calls == 1
    events = [
        event for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1" and event.payload.get("ai_run_id") == run_id
    ]
    assert len(events) == 1
    assert events[0].payload["terminal"] == "discarded"
    assert events[0].payload["reason"] == "Structured runner state failure; no proposal released."


def test_via_runner_different_dependency_does_not_hit_the_cache(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    other_request = DependencyBlastRadiusRequest(
        program_id="acme", dependency_id="dep-2", from_summary="program=acme, workstream=infra, WI:2002",
        to_summary="program=contoso, workstream=network, milestone=ms-2",
        risk_if_broken="A completely different downstream milestone slips.",
        current_status="active", evidence_texts=("Different evidence.",), evidence_refs=("sig-9",),
    )

    generate_dependency_blast_radius_proposal_via_runner(_request(), context=_context(), client=client, programs_root=programs_root)
    generate_dependency_blast_radius_proposal_via_runner(other_request, context=_context(), client=client, programs_root=programs_root)

    assert client.calls == 2


def test_via_runner_and_original_are_independent_caches(tmp_path: Path) -> None:
    # The two implementations must not silently share cache state -- a
    # request already cached by the original path is still a genuine cache
    # miss (and a real provider call) via the runner path, and vice versa.
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert client.calls == 1

    generate_dependency_blast_radius_proposal_via_runner(_request(), context=_context(), client=client, programs_root=programs_root)
    assert client.calls == 2

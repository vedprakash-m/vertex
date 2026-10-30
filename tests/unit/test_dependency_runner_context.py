from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
from typing import Any, Callable

import pytest

from src.ai.dependency_blast_radius_generator import generate_dependency_blast_radius_proposal_via_runner
from src.ai.dependency_blast_radius_generator import POLICY_VERSION
from src.ai.structured_feature_runner import RunnerContext, StructuredFeatureRunner
from src.core.context_compiler import (
    CompiledContext,
    ContextCompileRequest,
    DeterministicContextCompiler,
    ValidationContext,
)
from src.core.dependency_blast_radius import DependencyBlastRadiusRequest
from src.core.exceptions import StateError


def _context() -> RunnerContext:
    return RunnerContext(
        program_id="demo", run_id="synthetic-run", policy_version=POLICY_VERSION,
        snapshot_id="synthetic-snapshot", principal="synthetic-reviewer", classification="public",
    )


class _Client:
    def __init__(self, refs: tuple[str, ...] = ("evidence-1",)) -> None:
        self.calls: list[tuple[str, str]] = []
        self.refs = refs

    def structured(
        self, system: str, user: str, *, parser: Callable[[dict[str, Any]], Any],
        max_tokens: int = 800, prompt_version: str | None = None,
    ) -> Any:
        self.calls.append((system, user))
        return parser({
            "next_proving_event": "Review the dependency contract at the next checkpoint.",
            "blast_radius_narrative": "The downstream delivery depends on the upstream contract.",
            "evidence_refs": list(self.refs),
        })

    def chat(self, system: str, user: str, *, max_tokens: int = 800, prompt_version: str | None = None) -> str:
        pytest.fail("Unexpected chat dispatch")


def _request() -> DependencyBlastRadiusRequest:
    return DependencyBlastRadiusRequest(
        program_id="demo", dependency_id="dependency-1",
        from_summary="upstream-context-marker", to_summary="downstream-context-marker",
        risk_if_broken="risk-context-marker", current_status="status-context-marker",
        evidence_refs=("evidence-1",), evidence_texts=("evidence-context-marker",),
    )


def test_provider_receives_only_complete_compiled_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captures: list[CompiledContext] = []
    validations: list[ValidationContext] = []
    original = DeterministicContextCompiler.compile

    def compile_and_capture(
        self: DeterministicContextCompiler, request: ContextCompileRequest, *, validation_context: ValidationContext,
    ) -> CompiledContext:
        compiled = original(self, request, validation_context=validation_context)
        captures.append(compiled)
        validations.append(validation_context)
        return compiled

    monkeypatch.setattr(DeterministicContextCompiler, "compile", compile_and_capture)
    client = _Client()
    result = generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=_context(), client=client, programs_root=tmp_path,
    )
    assert result is not None
    assert len(captures) == 1
    assert validations[0].run_id == _context().run_id
    assert validations[0].classification == _context().classification
    assert validations[0].execution_mode == "observe"
    assert result.ai_run_id != _context().run_id
    compiled = captures[0]
    assert client.calls == [(compiled.prompt_text, "")]
    for value in (
        "upstream-context-marker", "downstream-context-marker", "risk-context-marker",
        "status-context-marker", "evidence-context-marker",
    ):
        assert value in compiled.prompt_text
    assert "evidence-1" in compiled.manifest.included_evidence_ids
    assert hashlib.sha256(client.calls[0][0].encode()).hexdigest() == compiled.manifest.context_hash


@pytest.mark.parametrize("field", ["from_summary", "to_summary", "risk_if_broken", "current_status"])
def test_oversized_dependency_context_abstains_before_provider(field: str, tmp_path: Path) -> None:
    client = _Client()
    request = replace(_request(), **{field: "synthetic-context-" * 3000})
    assert generate_dependency_blast_radius_proposal_via_runner(
        request, context=_context(), client=client, programs_root=tmp_path,
    ) is None
    assert client.calls == []


@pytest.mark.parametrize("refs,texts", [
    (("evidence-1", "unmatched-ref"), ("evidence-context-marker",)),
    (("evidence-1",), ("evidence-context-marker", "unmatched-text")),
])
def test_unmatched_evidence_abstains_before_compilation(
    refs: tuple[str, ...], texts: tuple[str, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    monkeypatch.setattr(
        DeterministicContextCompiler, "compile",
        lambda *args, **kwargs: pytest.fail("Unmatched evidence reached compilation"),
    )
    assert generate_dependency_blast_radius_proposal_via_runner(
        replace(_request(), evidence_refs=refs, evidence_texts=texts),
        context=_context(), client=client, programs_root=tmp_path,
    ) is None
    assert client.calls == []


def test_empty_evidence_pairs_preserve_supported_pilot_result(tmp_path: Path) -> None:
    client = _Client(refs=())
    assert generate_dependency_blast_radius_proposal_via_runner(
        replace(_request(), evidence_refs=(), evidence_texts=()),
        context=_context(), client=client, programs_root=tmp_path,
    ) is not None


@pytest.mark.parametrize("field", ["evidence_refs", "evidence_texts"])
def test_unmatched_pair_cannot_reuse_previously_released_cache(field: str, tmp_path: Path) -> None:
    request = _request()
    client = _Client()
    assert generate_dependency_blast_radius_proposal_via_runner(
        request, context=_context(), client=client, programs_root=tmp_path,
    ) is not None
    assert generate_dependency_blast_radius_proposal_via_runner(
        replace(request, **{field: getattr(request, field) + ("unmatched",)}),
        context=_context(), client=client, programs_root=tmp_path,
    ) is None
    assert len(client.calls) == 1


def test_context_is_required_before_any_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.ai.dependency_blast_radius_generator as generator

    client = _Client()
    monkeypatch.setattr(generator, "new_ai_run_id", lambda: pytest.fail("Missing context reached audit"))
    with pytest.raises(TypeError, match="context"):
        generate_dependency_blast_radius_proposal_via_runner(  # type: ignore[call-arg]
            _request(), client=client, programs_root=tmp_path,
        )
    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("changes", [{"program_id": "other"}, {"policy_version": "other-policy"}])
def test_context_mismatch_refused_before_any_effect(
    changes: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.ai.dependency_blast_radius_generator as generator

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Mismatched context reached a side effect")

    client = _Client()
    for name in (
        "new_ai_run_id", "load_ai_feature_policy", "record_ai_run_lifecycle",
        "record_ai_release_decision", "ReleasedResultCache", "_released_cache_path",
    ):
        monkeypatch.setattr(generator, name, forbidden)
    monkeypatch.setattr(DeterministicContextCompiler, "compile", forbidden)
    with pytest.raises(StateError, match="program|policy"):
        generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=replace(_context(), **changes), client=client, programs_root=tmp_path,
        )
    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field,value", [
    ("snapshot_id", "synthetic-other-snapshot"),
    ("principal", "synthetic-other-reviewer"),
    ("classification", "internal"),
])
def test_actual_cache_isolates_supplied_context(field: str, value: str, tmp_path: Path) -> None:
    client = _Client()
    contexts = (_context(), _context(), replace(_context(), **{field: value}))
    proposals = [
        generate_dependency_blast_radius_proposal_via_runner(
            _request(), context=context, client=client, programs_root=tmp_path,
        )
        for context in contexts
    ]
    assert all(proposal is not None for proposal in proposals)
    assert len(client.calls) == 2
    assert len({proposal.ai_run_id for proposal in proposals if proposal is not None}) == 3
    assert all(proposal.ai_run_id != _context().run_id for proposal in proposals if proposal is not None)


def test_supplied_context_reaches_runner_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = StructuredFeatureRunner.__init__
    contexts: list[RunnerContext] = []

    def capture(self: StructuredFeatureRunner, *, context: RunnerContext, **kwargs: Any) -> None:
        contexts.append(context)
        original(self, context=context, **kwargs)

    monkeypatch.setattr(StructuredFeatureRunner, "__init__", capture)
    context = _context()
    assert generate_dependency_blast_radius_proposal_via_runner(
        _request(), context=context, client=_Client(), programs_root=tmp_path,
    ) is not None
    assert contexts == [context]
    assert contexts[0] is context

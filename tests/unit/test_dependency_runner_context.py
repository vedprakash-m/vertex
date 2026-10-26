from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
from typing import Any, Callable

import pytest

from src.ai.dependency_blast_radius_generator import generate_dependency_blast_radius_proposal_via_runner
from src.core.context_compiler import (
    CompiledContext,
    ContextCompileRequest,
    DeterministicContextCompiler,
    ValidationContext,
)
from src.core.dependency_blast_radius import DependencyBlastRadiusRequest


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
    original = DeterministicContextCompiler.compile

    def compile_and_capture(
        self: DeterministicContextCompiler, request: ContextCompileRequest, *, validation_context: ValidationContext,
    ) -> CompiledContext:
        compiled = original(self, request, validation_context=validation_context)
        captures.append(compiled)
        return compiled

    monkeypatch.setattr(DeterministicContextCompiler, "compile", compile_and_capture)
    client = _Client()
    result = generate_dependency_blast_radius_proposal_via_runner(
        _request(), client=client, programs_root=tmp_path,
    )
    assert result is not None
    assert len(captures) == 1
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
        request, client=client, programs_root=tmp_path,
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
        client=client, programs_root=tmp_path,
    ) is None
    assert client.calls == []


def test_empty_evidence_pairs_preserve_supported_pilot_result(tmp_path: Path) -> None:
    client = _Client(refs=())
    assert generate_dependency_blast_radius_proposal_via_runner(
        replace(_request(), evidence_refs=(), evidence_texts=()),
        client=client, programs_root=tmp_path,
    ) is not None


@pytest.mark.parametrize("field", ["evidence_refs", "evidence_texts"])
def test_unmatched_pair_cannot_reuse_previously_released_cache(field: str, tmp_path: Path) -> None:
    request = _request()
    client = _Client()
    assert generate_dependency_blast_radius_proposal_via_runner(
        request, client=client, programs_root=tmp_path,
    ) is not None
    assert generate_dependency_blast_radius_proposal_via_runner(
        replace(request, **{field: getattr(request, field) + ("unmatched",)}),
        client=client, programs_root=tmp_path,
    ) is None
    assert len(client.calls) == 1

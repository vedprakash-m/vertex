"""RELO-W1.2/W1.5/W1.6 approved items D and D2: bounded exact whole-prefix
packing for estimate-only estimators, and selection identity provenance."""

from __future__ import annotations

import hashlib
import json
import logging
import random
from pathlib import Path

import pytest

from src.core.alerts import read_alerts
from src.core.context_compiler import (
    ContentOrigin,
    ContextCompileRejected,
    ContextCompileRequest,
    ContextPackingUnavailable,
    DeterministicContextCompiler,
    EvidenceSpan,
    ValidationContext,
    context_manifest_path,
    read_context_manifest_selection_identity,
)
from src.core.selection_identity import UNVERIFIED, selection_identity_v1

_BOUND_BYTES = 8 * 1024 * 1024


def _span(evidence_id: str, text: str, *, required: bool = False, source_family: str = "ado",
          authority: float = 0.0) -> EvidenceSpan:
    return EvidenceSpan(
        evidence_id=evidence_id, source_family=source_family, text=text, required=required,
        origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified",
        injection_screen="pass", salience_inputs={"source_authority": authority},
    )


def _request(required: tuple[EvidenceSpan, ...], optional: tuple[EvidenceSpan, ...], *,
             max_input_tokens: int, reserved_output_tokens: int = 0, system: str = "sys",
             schema: str = "schema") -> ContextCompileRequest:
    return ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions=system, output_schema_text=schema, required_evidence=required,
        optional_evidence=optional, max_input_tokens=max_input_tokens,
        reserved_output_tokens=reserved_output_tokens,
    )


def _ctx() -> ValidationContext:
    return ValidationContext(program_id="acme", run_id="run-1", execution_mode="observe", classification="internal")


class _ByteMeteringEstimator:
    """Estimate-only (no ``incremental_estimates``): records UTF-8 input bytes."""

    tokenizer_id = "metering"

    def __init__(self) -> None:
        self.input_bytes = 0
        self.calls = 0

    def estimate(self, text: str) -> int:
        self.calls += 1
        self.input_bytes += len(text.encode("utf-8"))
        return len(text)


# --- independent oracle -------------------------------------------------------


def _oracle_render(system: str, schema: str, spans: list[EvidenceSpan]) -> str:
    blocks = []
    for span in spans:
        kind = "REQUIRED" if span.required else "EVIDENCE"
        blocks.append(
            f"[{kind} {span.evidence_id} origin=system trust=high verification=verified]\n{span.text}"
        )
    sections = [part for part in (system.strip(), schema.strip()) if part]
    if blocks:
        sections.append("Evidence:\n\n" + "\n\n".join(blocks))
    return "\n\n".join(sections)


def _oracle_greedy(required: list[EvidenceSpan], optional: list[EvidenceSpan], estimate, ceiling: int,
                   system: str, schema: str) -> list[str]:
    ranked = sorted(optional, key=lambda span: (-0.20 * span.salience_inputs["source_authority"], span.evidence_id))
    chosen: list[EvidenceSpan] = []
    for span in ranked:
        if estimate(_oracle_render(system, schema, [*required, *chosen, span])) <= ceiling:
            chosen.append(span)
    return [span.evidence_id for span in (*required, *chosen)]


class _ChaoticEstimator:
    """Non-additive, non-monotonic estimate-only estimator."""

    tokenizer_id = "chaotic"

    def estimate(self, text: str) -> int:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return max(0, len(text) // 3 + digest[0] % 23 - 11)


@pytest.mark.parametrize("seed", range(40))
def test_whole_prefix_packing_matches_independent_brute_greedy_oracle(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    required = [_span(f"req{i}", f"required {seed} {i} " + "r" * rng.randint(0, 30), required=True)
                for i in range(rng.randint(0, 2))]
    optional = [_span(f"opt{i:02}", f"fact {seed} {i} " + "x" * rng.randint(0, 80), authority=rng.random())
                for i in range(rng.randint(1, 9))]
    estimator = _ChaoticEstimator()
    required_tokens = estimator.estimate(_oracle_render("sys", "schema", required))
    ceiling = required_tokens + rng.randint(0, 200)
    reserved = rng.randint(0, 5)
    request = _request(tuple(required), tuple(optional), max_input_tokens=ceiling + reserved,
                       reserved_output_tokens=reserved)

    result = DeterministicContextCompiler(token_estimator=estimator, programs_root=tmp_path / "p").compile(
        request, validation_context=_ctx(),
    )

    expected_ids = _oracle_greedy(required, optional, estimator.estimate, ceiling, "sys", "schema")
    assert list(result.manifest.included_evidence_ids) == expected_ids
    by_id = {span.evidence_id: span for span in (*required, *optional)}
    oracle_prompt = _oracle_render("sys", "schema", [by_id[evidence_id] for evidence_id in expected_ids])
    assert result.prompt_text == oracle_prompt
    independent_tokens = _ChaoticEstimator().estimate(result.prompt_text)
    assert independent_tokens == result.manifest.token_estimate_total
    assert independent_tokens + reserved <= request.max_input_tokens


def test_estimate_only_packing_excludes_nonadditive_third_span_instead_of_rejecting(tmp_path: Path) -> None:
    class BoundaryEstimator:
        tokenizer_id = "boundary-overhead"

        def estimate(self, text: str) -> int:
            return len(text) + (5 if "\n\n[EVIDENCE opt " in text else 0)

    first = _span("first", "first optional fact", authority=1.0)
    middle = _span("middle", "middle optional fact", authority=0.75)
    opt = _span("opt", "optional fact", authority=0.5)
    full_prompt = _oracle_render("sys", "schema", [first, middle, opt])
    programs_root = tmp_path / "programs"
    request = _request((), (first, middle, opt), max_input_tokens=len(full_prompt) + 4, reserved_output_tokens=4)

    result = DeterministicContextCompiler(token_estimator=BoundaryEstimator(), programs_root=programs_root).compile(
        request, validation_context=_ctx(),
    )

    assert result.prompt_text == _oracle_render("sys", "schema", [first, middle])
    assert [(entry.evidence_id, entry.reason) for entry in result.excluded] == [("opt", "token_budget")]
    assert tuple(read_alerts("acme", programs_root=programs_root)) == ()


def _bulk_spans(count: int, body: str) -> tuple[EvidenceSpan, ...]:
    return tuple(_span(f"ev-{index:04}", f"{index:04} {body}") for index in range(count))


def test_estimate_only_packing_refuses_above_bound_without_partial_prompt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    estimator = _ByteMeteringEstimator()
    programs_root = tmp_path / "programs"
    request = _request((), _bulk_spans(300, "a" * 1000), max_input_tokens=10_000_000)
    caplog.set_level(logging.WARNING)

    with pytest.raises(ContextPackingUnavailable, match="8388608 UTF-8 bytes") as refusal:
        DeterministicContextCompiler(token_estimator=estimator, programs_root=programs_root).compile(
            request, validation_context=_ctx(),
        )

    assert isinstance(refusal.value, ContextCompileRejected)
    assert str(refusal.value).startswith("Unavailable:")
    assert estimator.input_bytes <= _BOUND_BYTES
    assert not programs_root.exists()


def test_bound_counts_utf8_bytes_not_characters(tmp_path: Path) -> None:
    ascii_estimator = _ByteMeteringEstimator()
    ascii_result = DeterministicContextCompiler(token_estimator=ascii_estimator, programs_root=tmp_path / "a").compile(
        _request((), _bulk_spans(100, "a" * 900), max_input_tokens=10_000_000), validation_context=_ctx(),
    )
    assert len(ascii_result.included) == 100
    assert 4 * 1024 * 1024 < ascii_estimator.input_bytes <= _BOUND_BYTES

    euro_estimator = _ByteMeteringEstimator()
    with pytest.raises(ContextPackingUnavailable):
        DeterministicContextCompiler(token_estimator=euro_estimator, programs_root=tmp_path / "e").compile(
            _request((), _bulk_spans(100, "\u20ac" * 900), max_input_tokens=10_000_000), validation_context=_ctx(),
        )
    assert euro_estimator.input_bytes <= _BOUND_BYTES


def test_bound_is_per_compile(tmp_path: Path) -> None:
    compiler = DeterministicContextCompiler(token_estimator=_ByteMeteringEstimator(), programs_root=tmp_path / "p")
    request = _request((), _bulk_spans(100, "a" * 900), max_input_tokens=10_000_000)
    first = compiler.compile(request, validation_context=_ctx())
    second = compiler.compile(request, validation_context=_ctx())
    assert first.prompt_text == second.prompt_text


def test_incremental_estimator_keeps_linear_path_beyond_bound(tmp_path: Path) -> None:
    class IncrementalEstimator(_ByteMeteringEstimator):
        incremental_estimates = True

    estimator = IncrementalEstimator()
    result = DeterministicContextCompiler(token_estimator=estimator, programs_root=tmp_path / "p").compile(
        _request((), _bulk_spans(300, "a" * 1000), max_input_tokens=10_000_000), validation_context=_ctx(),
    )
    assert len(result.included) == 300
    assert estimator.input_bytes <= 3 * len(result.prompt_text.encode("utf-8"))


# --- D2: selection identity ------------------------------------------------------


def _expected_identity(spans: tuple[EvidenceSpan, ...]) -> str:
    return selection_identity_v1(
        (span.source_family, span.evidence_id, hashlib.sha256(span.text.encode("utf-8")).hexdigest())
        for span in spans
    )


def test_compiled_context_exposes_selection_identity_beside_unchanged_prompt_hash(tmp_path: Path) -> None:
    required = _span("req", "required fact", required=True, source_family="ado")
    kept = _span("kept", "kept fact", source_family="teams", authority=1.0)
    dropped = _span("dropped", "dropped fact " * 50, source_family="email", authority=0.5)
    programs_root = tmp_path / "programs"
    request = _request((required,), (kept, dropped),
                       max_input_tokens=len(_oracle_render("sys", "schema", [required, kept])))

    result = DeterministicContextCompiler(token_estimator=_ByteMeteringEstimator(), programs_root=programs_root).compile(
        request, validation_context=_ctx(),
    )

    assert result.manifest.included_evidence_ids == ("req", "kept")
    assert result.selection_identity == _expected_identity((required, kept))
    assert result.manifest.selection_identity == result.selection_identity
    assert result.manifest.context_hash == hashlib.sha256(result.prompt_text.encode("utf-8")).hexdigest()
    path = context_manifest_path(result.manifest, programs_root=programs_root)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["selection_identity"] == result.selection_identity
    assert stored["context_hash"] == result.manifest.context_hash
    assert read_context_manifest_selection_identity(path) == result.selection_identity


def test_selection_identity_tracks_source_ref_without_changing_prompt_hash(tmp_path: Path) -> None:
    def compile_with(source_family: str):
        span = _span("ev", "same fact", source_family=source_family)
        return DeterministicContextCompiler(
            token_estimator=_ByteMeteringEstimator(), programs_root=tmp_path / source_family,
        ).compile(_request((), (span,), max_input_tokens=10_000), validation_context=_ctx())

    ado, teams = compile_with("ado"), compile_with("teams")
    assert ado.manifest.context_hash == teams.manifest.context_hash
    assert ado.selection_identity != teams.selection_identity


def test_legacy_manifest_without_selection_identity_reads_unverified_without_rewrite(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    programs_root = tmp_path / "programs"
    compiler = DeterministicContextCompiler(token_estimator=_ByteMeteringEstimator(), programs_root=programs_root)
    request = _request((), (_span("ev", "fact"),), max_input_tokens=10_000)
    first = compiler.compile(request, validation_context=_ctx())
    path = context_manifest_path(first.manifest, programs_root=programs_root)
    legacy = json.loads(path.read_text(encoding="utf-8"))
    del legacy["selection_identity"]
    path.write_text(json.dumps(legacy, indent=2, sort_keys=True), encoding="utf-8")
    legacy_bytes = path.read_bytes()
    caplog.set_level(logging.WARNING, logger="src.core.context_compiler")

    second = compiler.compile(request, validation_context=_ctx())

    assert second.selection_identity == first.selection_identity
    assert path.read_bytes() == legacy_bytes
    assert read_context_manifest_selection_identity(path) == UNVERIFIED
    assert path.read_bytes() == legacy_bytes
    assert not [record for record in caplog.records if record.name == "src.core.context_compiler"]

"""ADF-W2.7 (specs/arch-data-fix.md Appendix A.1): tests for the provider
tokenizer adapters in ``src/ai/context_tokenizers.py``."""

from __future__ import annotations

import builtins
import importlib

import pytest

from src.ai import context_tokenizers
from src.ai.context_tokenizers import (
    CharHeuristicTokenEstimator,
    TiktokenTokenEstimator,
    resolve_token_estimator,
)
from src.core.context_compiler import TokenEstimator


# ---------------------------------------------------------------------------
# CharHeuristicTokenEstimator
# ---------------------------------------------------------------------------


class TestCharHeuristicTokenEstimator:
    def test_empty_text_is_zero(self) -> None:
        est = CharHeuristicTokenEstimator()
        assert est.estimate("") == 0

    def test_default_4_chars_per_token_minimum_one(self) -> None:
        est = CharHeuristicTokenEstimator()
        # "abc" -> 3 chars // 4 = 0, clamped to 1
        assert est.estimate("abc") == 1
        # "abcdefgh" -> 8 // 4 = 2
        assert est.estimate("abcdefgh") == 2

    def test_tokenizer_id_is_distinct_and_configurable(self) -> None:
        est = CharHeuristicTokenEstimator(label="gpt-4o", chars_per_token=3)
        assert est.tokenizer_id == "char_heuristic:gpt-4o:3cpt"

    def test_custom_chars_per_token(self) -> None:
        est = CharHeuristicTokenEstimator(chars_per_token=2)
        assert est.estimate("abcd") == 2

    def test_rejects_non_positive_chars_per_token(self) -> None:
        with pytest.raises(ValueError):
            CharHeuristicTokenEstimator(chars_per_token=0)
        with pytest.raises(ValueError):
            CharHeuristicTokenEstimator(chars_per_token=-1)

    def test_satisfies_protocol(self) -> None:
        est: TokenEstimator = CharHeuristicTokenEstimator()
        assert est.tokenizer_id
        assert isinstance(est.estimate("hello world"), int)


# ---------------------------------------------------------------------------
# TiktokenTokenEstimator
# ---------------------------------------------------------------------------

_HAS_TIKTOKEN = importlib.util.find_spec("tiktoken") is not None
pytestmark_tiktoken = pytest.mark.skipif(not _HAS_TIKTOKEN, reason="tiktoken not installed")


class TestTiktokenTokenEstimator:
    def test_empty_text_is_zero(self) -> None:
        if not _HAS_TIKTOKEN:
            pytest.skip("tiktoken not installed")
        est = TiktokenTokenEstimator("gpt-4o")
        assert est.estimate("") == 0

    def test_tokenizer_id_records_encoding(self) -> None:
        if not _HAS_TIKTOKEN:
            pytest.skip("tiktoken not installed")
        est = TiktokenTokenEstimator("gpt-4o")
        # gpt-4o resolves to o200k_base
        assert est.tokenizer_id == "tiktoken:o200k_base"

    def test_estimate_matches_tiktoken_directly(self) -> None:
        if not _HAS_TIKTOKEN:
            pytest.skip("tiktoken not installed")
        import tiktoken

        est = TiktokenTokenEstimator("gpt-4o-mini")
        text = "The quick brown fox jumps over the lazy dog."
        assert est.estimate(text) == len(tiktoken.encoding_for_model("gpt-4o-mini").encode(text))

    def test_legacy_deployment_alias_uses_override(self) -> None:
        if not _HAS_TIKTOKEN:
            pytest.skip("tiktoken not installed")
        est = TiktokenTokenEstimator("gpt-35-turbo")
        assert est.tokenizer_id == "tiktoken:cl100k_base"

    def test_cl100k_models_resolve(self) -> None:
        if not _HAS_TIKTOKEN:
            pytest.skip("tiktoken not installed")
        est = TiktokenTokenEstimator("gpt-4")
        assert est.tokenizer_id == "tiktoken:cl100k_base"

    def test_satisfies_protocol(self) -> None:
        if not _HAS_TIKTOKEN:
            pytest.skip("tiktoken not installed")
        est: TokenEstimator = TiktokenTokenEstimator("gpt-4o")
        assert est.tokenizer_id
        assert isinstance(est.estimate("hello world"), int)


class TestTiktokenUnavailable:
    """When tiktoken is genuinely absent, construction must fail loudly so a
    caller that explicitly asked for tiktoken does not get a silent heuristic."""

    def test_raises_when_tiktoken_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(context_tokenizers, "_try_import_tiktoken", lambda: None)
        with pytest.raises(ImportError, match="tiktoken is not installed"):
            TiktokenTokenEstimator("gpt-4o")

    def test_raises_for_unrecognized_deployment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if not _HAS_TIKTOKEN:
            pytest.skip("tiktoken not installed")
        # tiktoken has no encoding for an arbitrary string
        with pytest.raises(ValueError, match="no encoding"):
            TiktokenTokenEstimator("not-a-real-model-xyz")


# ---------------------------------------------------------------------------
# resolve_token_estimator factory
# ---------------------------------------------------------------------------


class TestResolveTokenEstimator:
    def test_returns_tiktoken_for_known_deployment(self) -> None:
        resolve_token_estimator.cache_clear()
        if not _HAS_TIKTOKEN:
            pytest.skip("tiktoken not installed")
        est = resolve_token_estimator("gpt-4o")
        assert isinstance(est, TiktokenTokenEstimator)
        assert est.tokenizer_id == "tiktoken:o200k_base"

    def test_returns_heuristic_when_tiktoken_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resolve_token_estimator.cache_clear()
        monkeypatch.setattr(context_tokenizers, "_try_import_tiktoken", lambda: None)
        est = resolve_token_estimator("gpt-4o")
        assert isinstance(est, CharHeuristicTokenEstimator)
        assert "gpt-4o" in est.tokenizer_id

    def test_returns_heuristic_for_unknown_deployment(self) -> None:
        resolve_token_estimator.cache_clear()
        est = resolve_token_estimator("totally-unknown-model-xyz")
        assert isinstance(est, CharHeuristicTokenEstimator)

    def test_empty_deployment_falls_back_to_heuristic(self) -> None:
        resolve_token_estimator.cache_clear()
        est = resolve_token_estimator("")
        assert isinstance(est, CharHeuristicTokenEstimator)

    def test_cached_for_same_deployment(self) -> None:
        resolve_token_estimator.cache_clear()
        est1 = resolve_token_estimator("gpt-4o")
        est2 = resolve_token_estimator("gpt-4o")
        assert est1 is est2

    def test_estimates_are_consistent_across_calls(self) -> None:
        resolve_token_estimator.cache_clear()
        est = resolve_token_estimator("gpt-4o")
        text = "Vertex is a deterministic kernel for TPM/EM program intelligence."
        assert est.estimate(text) == est.estimate(text)


# ---------------------------------------------------------------------------
# Integration: the adapter is a valid drop-in for the ContextCompiler
# ---------------------------------------------------------------------------


class TestContextCompilerIntegration:
    def test_tiktoken_estimator_is_injectable_into_compiler(self) -> None:
        if not _HAS_TIKTOKEN:
            pytest.skip("tiktoken not installed")
        from src.core.context_compiler import (
            ContextCompileRequest,
            DeterministicContextCompiler,
            EvidenceSpan,
            ValidationContext,
            ContentOrigin,
        )

        est = resolve_token_estimator("gpt-4o")
        compiler = DeterministicContextCompiler(token_estimator=est)
        span = EvidenceSpan(
            evidence_id="ev1",
            source_family="ado",
            text="A required evidence span for the compile.",
            required=True,
            origin=ContentOrigin.AUTHORED,
            trust_level="high",
            verification_state="verified",
            injection_screen="pass",
            salience_inputs={},
            token_estimate=est.estimate("A required evidence span for the compile."),
        )
        request = ContextCompileRequest(
            program_id="test-prog",
            edition_id=None,
            feature="test_feature",
            prompt_version="v1",
            system_instructions="System instructions.",
            output_schema_text="Schema.",
            required_evidence=(span,),
            optional_evidence=(),
            max_input_tokens=8192,
            reserved_output_tokens=100,
        )
        validation = ValidationContext(
            program_id="test-prog",
            run_id="run-1",
            execution_mode="observe",
            classification="internal",
        )
        compiled = compiler.compile(request, validation_context=validation)
        assert compiled.manifest.tokenizer_id == est.tokenizer_id
        assert "ev1" in compiled.manifest.included_evidence_ids

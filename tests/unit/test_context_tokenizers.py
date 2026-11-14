"""ADF-W2.7 (specs/arch-data-fix.md Appendix A.1): tests for the provider
tokenizer adapters in ``src/ai/context_tokenizers.py``."""

from __future__ import annotations

import builtins
import importlib
import json
import sys
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.ai import context_tokenizers
from src.ai.context_tokenizers import (
    CharHeuristicTokenEstimator,
    TiktokenTokenEstimator,
    resolve_token_estimator,
)
from src.core.context_compiler import ContextCompileRejected, DeterministicTokenEstimator, TokenEstimator


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
    @pytest.mark.parametrize("error_type", [ValueError, TypeError, RuntimeError, OverflowError])
    def test_resolved_tokenizer_encoding_failure_is_rejected_without_content(
        self, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception],
    ) -> None:
        prompt = "private prompt text"
        detail = "private encoder detail"
        encode = Mock(side_effect=error_type(detail))
        encoding = SimpleNamespace(encode_ordinary=encode)
        module = SimpleNamespace(
            encoding_name_for_model=lambda _: "synthetic_encoding",
            get_encoding=lambda _: encoding,
        )
        monkeypatch.setattr(context_tokenizers, "_try_import_tiktoken", lambda: module)
        estimator = TiktokenTokenEstimator("synthetic-model")

        with pytest.raises(ContextCompileRejected, match="Tokenizer failed to estimate input") as error:
            estimator.estimate(prompt)

        assert estimator.tokenizer_id == "tiktoken:synthetic_encoding"
        encode.assert_called_once_with(prompt)
        rendered = "".join(traceback.format_exception(error.value))
        assert detail not in rendered
        assert prompt not in rendered

    def test_resolved_tokenizer_unexpected_failure_does_not_fall_back(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        encoding = SimpleNamespace(encode_ordinary=Mock(side_effect=LookupError("unexpected failure")))
        module = SimpleNamespace(
            encoding_name_for_model=lambda _: "synthetic_encoding",
            get_encoding=lambda _: encoding,
        )
        monkeypatch.setattr(context_tokenizers, "_try_import_tiktoken", lambda: module)
        estimator = TiktokenTokenEstimator("synthetic-model")

        with pytest.raises(LookupError, match="unexpected failure"):
            estimator.estimate("prompt")

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

    @pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4"])
    def test_literal_special_token_is_counted_as_text(self, model: str) -> None:
        tiktoken = pytest.importorskip("tiktoken")
        text = "prefix <|endoftext|> " + "\u6f22\u8a9e\U0001f642" * 24
        expected = len(tiktoken.encoding_for_model(model).encode_ordinary(text))
        est = TiktokenTokenEstimator(model)

        assert expected > len(text) // 4
        assert est.estimate(text) == expected
        assert est.tokenizer_id == f"tiktoken:{tiktoken.encoding_for_model(model).name}"

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
    @pytest.mark.parametrize("constructor", ["direct", "factory"])
    @pytest.mark.parametrize("failure_kind", ["submodule", "dependency", "unspecified", "import-error"])
    def test_resolved_tokenizer_broken_package_import_is_not_absence(
        self, monkeypatch: pytest.MonkeyPatch, constructor: str, failure_kind: str,
    ) -> None:
        failure: ImportError
        if failure_kind == "submodule":
            failure = ModuleNotFoundError("synthetic submodule failure", name="tiktoken.registry")
        elif failure_kind == "dependency":
            failure = ModuleNotFoundError("synthetic dependency failure", name="regex")
        elif failure_kind == "unspecified":
            failure = ModuleNotFoundError("synthetic unclassified failure")
        else:
            failure = ImportError("synthetic import failure")
        original_import = importlib.import_module

        def import_module(name: str, package: str | None = None) -> object:
            if name == "tiktoken":
                raise failure
            return original_import(name, package)

        monkeypatch.setattr(context_tokenizers.importlib, "import_module", import_module)
        resolve_token_estimator.cache_clear()
        try:
            construct = TiktokenTokenEstimator if constructor == "direct" else resolve_token_estimator
            with pytest.raises(type(failure)) as error:
                construct("synthetic-model")
            assert error.value is failure
        finally:
            resolve_token_estimator.cache_clear()

    def test_resolved_tokenizer_absent_top_level_package_still_falls_back(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_import = importlib.import_module

        def import_module(name: str, package: str | None = None) -> object:
            if name == "tiktoken":
                raise ModuleNotFoundError("synthetic missing package", name="tiktoken")
            return original_import(name, package)

        monkeypatch.setattr(context_tokenizers.importlib, "import_module", import_module)
        resolve_token_estimator.cache_clear()
        try:
            estimator = resolve_token_estimator("synthetic-model")
            assert isinstance(estimator, CharHeuristicTokenEstimator)
            assert estimator.tokenizer_id == "char_heuristic:synthetic-model:4cpt"
            with pytest.raises(ImportError, match="tiktoken is not installed"):
                TiktokenTokenEstimator("synthetic-model")
        finally:
            resolve_token_estimator.cache_clear()

    def test_resolved_tokenizer_blocked_top_level_import_still_falls_back(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(sys.modules, "tiktoken", None)
        resolve_token_estimator.cache_clear()
        try:
            assert isinstance(resolve_token_estimator("synthetic-model"), CharHeuristicTokenEstimator)
            with pytest.raises(ImportError, match="tiktoken is not installed"):
                TiktokenTokenEstimator("synthetic-model")
        finally:
            resolve_token_estimator.cache_clear()

    @pytest.mark.parametrize("failure_at", [1, 2])
    def test_resolved_tokenizer_failed_import_is_not_cached(
        self, monkeypatch: pytest.MonkeyPatch, failure_at: int,
    ) -> None:
        failure = ImportError("synthetic import failure")
        encoding = SimpleNamespace(encode_ordinary=lambda _text: [1])
        module = SimpleNamespace(
            encoding_name_for_model=lambda _: "synthetic_encoding",
            get_encoding=lambda _: encoding,
        )
        original_import = importlib.import_module
        imports = Mock(side_effect=[module] * (failure_at - 1) + [failure, module, module])

        def import_module(name: str, package: str | None = None) -> object:
            if name == "tiktoken":
                return imports()
            return original_import(name, package)

        monkeypatch.setattr(context_tokenizers.importlib, "import_module", import_module)
        resolve_token_estimator.cache_clear()
        try:
            with pytest.raises(ImportError) as error:
                resolve_token_estimator("synthetic-model")
            assert error.value is failure
            estimator = resolve_token_estimator("synthetic-model")
            assert estimator.tokenizer_id == "tiktoken:synthetic_encoding"
            assert estimator.estimate("example") == 1
            assert resolve_token_estimator("synthetic-model") is estimator
            assert imports.call_count == failure_at + 2
        finally:
            resolve_token_estimator.cache_clear()

    @pytest.mark.parametrize("constructor", ["direct", "factory"])
    @pytest.mark.parametrize("error_type", [KeyError, OSError])
    def test_resolved_tokenizer_legacy_override_exposes_asset_failure(
        self, monkeypatch: pytest.MonkeyPatch, constructor: str, error_type: type[Exception],
    ) -> None:
        failure = error_type("synthetic asset failure")
        lookup = Mock()
        load = Mock(side_effect=failure)
        module = SimpleNamespace(encoding_name_for_model=lookup, get_encoding=load)
        monkeypatch.setattr(context_tokenizers, "_try_import_tiktoken", lambda: module)
        resolve_token_estimator.cache_clear()
        try:
            construct = TiktokenTokenEstimator if constructor == "direct" else resolve_token_estimator
            with pytest.raises(error_type) as error:
                construct("gpt-35-turbo")
            assert error.value is failure
            lookup.assert_not_called()
            load.assert_called_once_with("cl100k_base")
        finally:
            resolve_token_estimator.cache_clear()

    @pytest.mark.parametrize("constructor", ["direct", "factory"])
    @pytest.mark.parametrize("error_type", [KeyError, OSError, ValueError, RuntimeError])
    def test_resolved_tokenizer_asset_failure_is_not_an_unknown_model(
        self, monkeypatch: pytest.MonkeyPatch, constructor: str, error_type: type[Exception],
    ) -> None:
        failure = error_type("synthetic asset failure")
        lookup = Mock(return_value="synthetic_encoding")
        load = Mock(side_effect=failure)
        module = SimpleNamespace(
            encoding_name_for_model=lookup,
            encoding_for_model=lambda name: load(lookup(name)),
            get_encoding=load,
        )
        monkeypatch.setattr(context_tokenizers, "_try_import_tiktoken", lambda: module)
        resolve_token_estimator.cache_clear()
        try:
            construct = TiktokenTokenEstimator if constructor == "direct" else resolve_token_estimator
            with pytest.raises(error_type) as error:
                construct("synthetic-model")
            assert error.value is failure
            load.assert_called_once_with("synthetic_encoding")
        finally:
            resolve_token_estimator.cache_clear()

    @pytest.mark.parametrize("constructor", ["direct", "factory"])
    @pytest.mark.parametrize("error_type", [OSError, RuntimeError])
    def test_resolved_tokenizer_unexpected_name_lookup_failure_is_not_an_unknown_model(
        self, monkeypatch: pytest.MonkeyPatch, constructor: str, error_type: type[Exception],
    ) -> None:
        failure = error_type("synthetic lookup failure")
        lookup = Mock(side_effect=failure)
        load = Mock()
        module = SimpleNamespace(
            encoding_name_for_model=lookup,
            encoding_for_model=lambda name: load(lookup(name)),
            get_encoding=load,
        )
        monkeypatch.setattr(context_tokenizers, "_try_import_tiktoken", lambda: module)
        resolve_token_estimator.cache_clear()
        try:
            construct = TiktokenTokenEstimator if constructor == "direct" else resolve_token_estimator
            with pytest.raises(error_type) as error:
                construct("synthetic-model")
            assert error.value is failure
            load.assert_not_called()
        finally:
            resolve_token_estimator.cache_clear()

    def test_resolved_tokenizer_unknown_model_still_uses_identified_heuristic(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lookup = Mock(side_effect=KeyError("unrecognized model"))
        load = Mock()
        module = SimpleNamespace(
            encoding_name_for_model=lookup,
            encoding_for_model=lambda name: load(lookup(name)),
            get_encoding=load,
        )
        monkeypatch.setattr(context_tokenizers, "_try_import_tiktoken", lambda: module)
        resolve_token_estimator.cache_clear()
        try:
            estimator = resolve_token_estimator("unknown-synthetic")
            assert isinstance(estimator, CharHeuristicTokenEstimator)
            assert estimator.tokenizer_id == "char_heuristic:unknown-synthetic:4cpt"
            with pytest.raises(ValueError, match="no encoding"):
                TiktokenTokenEstimator("unknown-synthetic")
            load.assert_not_called()
        finally:
            resolve_token_estimator.cache_clear()

    def test_resolved_tokenizer_failed_construction_is_not_cached(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        failure = OSError("synthetic asset failure")
        encoding = SimpleNamespace(name="synthetic_encoding", encode_ordinary=lambda _text: [1])
        lookup = Mock(return_value="synthetic_encoding")
        load = Mock(side_effect=[failure, encoding])
        module = SimpleNamespace(
            encoding_name_for_model=lookup,
            encoding_for_model=lambda name: load(lookup(name)),
            get_encoding=load,
        )
        monkeypatch.setattr(context_tokenizers, "_try_import_tiktoken", lambda: module)
        resolve_token_estimator.cache_clear()
        try:
            with pytest.raises(OSError) as error:
                resolve_token_estimator("synthetic-model")
            assert error.value is failure
            estimator = resolve_token_estimator("synthetic-model")
            assert isinstance(estimator, TiktokenTokenEstimator)
            assert estimator.tokenizer_id == "tiktoken:synthetic_encoding"
            assert estimator.estimate("example") == 1
            assert resolve_token_estimator("synthetic-model") is estimator
            assert load.call_count == 2
        finally:
            resolve_token_estimator.cache_clear()

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
    @pytest.mark.parametrize(
        ("backend", "expected_id", "expected_count"),
        [
            ("missing", "char_heuristic:4cpt", 2),
            ("known", "tiktoken:synthetic-encoding", 3),
            ("unknown", "tiktoken:cl100k_base", 3),
        ],
    )
    def test_default_estimator_provenance_records_actual_backend(
        self, monkeypatch: pytest.MonkeyPatch, backend: str, expected_id: str, expected_count: int,
    ) -> None:
        model = 'synthetic:model"]'
        original_import = importlib.import_module
        loaded: list[str] = []

        def lookup(_model: str) -> str:
            if backend == "unknown":
                raise KeyError("unknown model")
            return "synthetic-encoding"

        def load(name: str) -> SimpleNamespace:
            loaded.append(name)
            return SimpleNamespace(encode_ordinary=lambda _text: [1, 2, 3])

        module = SimpleNamespace(encoding_name_for_model=lookup, get_encoding=load)

        def import_module(name: str, package: str | None = None) -> object:
            if name != "tiktoken":
                return original_import(name, package)
            if backend == "missing":
                raise ModuleNotFoundError("absent", name="tiktoken")
            return module

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        estimator = DeterministicTokenEstimator(model=model)
        assert estimator.estimate("abcdefgh") == expected_count
        prefix = "context_budget.estimate_tokens:"
        assert estimator.tokenizer_id.startswith(prefix)
        assert json.loads(estimator.tokenizer_id[len(prefix):]) == [model, expected_id]
        assert loaded == ([] if backend == "missing" else [expected_id.removeprefix("tiktoken:")])

    def test_default_estimator_empty_input_does_not_resolve_backend(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_import = importlib.import_module
        calls: list[str] = []

        def import_module(name: str, package: str | None = None) -> object:
            if name == "tiktoken":
                calls.append(name)
                raise AssertionError("empty input must not resolve the backend")
            return original_import(name, package)

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        estimator = DeterministicTokenEstimator(model="synthetic")
        assert estimator.estimate("") == 0
        prefix = "context_budget.estimate_tokens:"
        assert json.loads(estimator.tokenizer_id[len(prefix):]) == ["synthetic", "unresolved"]
        assert calls == []

    @pytest.mark.parametrize("already_bound", [False, True])
    def test_default_estimator_empty_compilation_preserves_backend_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_bound: bool,
    ) -> None:
        from src.core.context_compiler import (
            ContextCompileRequest,
            DeterministicContextCompiler,
            ValidationContext,
            context_manifest_path,
        )

        resolver = Mock(return_value=("tiktoken:synthetic-encoding", lambda text: len(text)))
        monkeypatch.setattr("src.core.context_budget._resolve_strict_tokenizer", resolver)
        original_import = importlib.import_module

        def import_module(name: str, package: str | None = None) -> object:
            if name == "tiktoken":
                raise AssertionError("empty compilation must not resolve the backend")
            return original_import(name, package)

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        request = ContextCompileRequest(
            program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
            system_instructions="", output_schema_text="", required_evidence=(), optional_evidence=(),
            max_input_tokens=0, reserved_output_tokens=0,
        )
        validation = ValidationContext(
            program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
        )
        estimator = DeterministicTokenEstimator(model="synthetic")
        if already_bound:
            assert estimator.estimate("test") == 4
        compiled = DeterministicContextCompiler(
            token_estimator=estimator,
            programs_root=tmp_path / "programs",
        ).compile(request, validation_context=validation)

        prefix = "context_budget.estimate_tokens:"
        backend = "tiktoken:synthetic-encoding" if already_bound else "unresolved"
        assert compiled.prompt_text == ""
        assert compiled.manifest.token_estimate_total == 0
        assert json.loads(compiled.manifest.tokenizer_id[len(prefix):]) == ["synthetic", backend]
        assert resolver.call_count == int(already_bound)
        path = context_manifest_path(compiled.manifest, programs_root=tmp_path / "programs")
        assert json.loads(path.read_text(encoding="utf-8"))["tokenizer_id"] == compiled.manifest.tokenizer_id

    def test_default_estimator_resolves_once_across_threads_and_environment_change(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_import = importlib.import_module
        first_started, second_started, release = Event(), Event(), Event()
        counter_lock = Lock()
        calls = 0
        module = SimpleNamespace(
            encoding_name_for_model=lambda _model: "synthetic-encoding",
            get_encoding=lambda _name: SimpleNamespace(encode_ordinary=lambda _text: [1, 2, 3]),
        )

        def import_module(name: str, package: str | None = None) -> object:
            nonlocal calls
            if name != "tiktoken":
                return original_import(name, package)
            with counter_lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                first_started.set()
                if not release.wait(5):
                    raise RuntimeError("test timed out waiting to release resolution")
                raise ModuleNotFoundError("absent", name="tiktoken")
            return module

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        estimator = DeterministicTokenEstimator(model="synthetic")

        def second_estimate() -> int:
            second_started.set()
            return estimator.estimate("abcdefgh")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(estimator.estimate, "abcdefgh")
            try:
                assert first_started.wait(5)
                second = pool.submit(second_estimate)
                assert second_started.wait(5)
            finally:
                release.set()
            assert (first.result(), second.result()) == (2, 2)

        assert estimator.estimate("abcdefgh") == 2
        assert calls == 1
        replacement = DeterministicTokenEstimator(model="synthetic")
        assert replacement.estimate("abcdefgh") == 3
        assert replacement.tokenizer_id != estimator.tokenizer_id
        assert calls == 2

    def test_default_estimator_failed_resolution_can_retry(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_import = importlib.import_module
        attempts = 0
        module = SimpleNamespace(
            encoding_name_for_model=lambda _model: "synthetic-encoding",
            get_encoding=lambda _name: SimpleNamespace(encode_ordinary=lambda _text: [1]),
        )

        def import_module(name: str, package: str | None = None) -> object:
            nonlocal attempts
            if name != "tiktoken":
                return original_import(name, package)
            attempts += 1
            if attempts == 1:
                raise ImportError("private failure")
            return module

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        estimator = DeterministicTokenEstimator(model="synthetic")
        with pytest.raises(ContextCompileRejected, match="Tokenizer failed to estimate input"):
            estimator.estimate("abc")
        prefix = "context_budget.estimate_tokens:"
        assert json.loads(estimator.tokenizer_id[len(prefix):]) == ["synthetic", "unresolved"]
        assert estimator.estimate("abc") == 1
        assert json.loads(estimator.tokenizer_id[len(prefix):]) == ["synthetic", "tiktoken:synthetic-encoding"]
        assert attempts == 2

    def test_default_estimator_bound_encoding_failure_does_not_switch_backends(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_import = importlib.import_module
        encode = Mock(side_effect=[[1, 2], ValueError("private encoder failure")])
        load = Mock(return_value=SimpleNamespace(encode_ordinary=encode))
        module = SimpleNamespace(encoding_name_for_model=lambda _model: "synthetic-encoding", get_encoding=load)

        def import_module(name: str, package: str | None = None) -> object:
            return module if name == "tiktoken" else original_import(name, package)

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        estimator = DeterministicTokenEstimator(model="synthetic")
        assert estimator.estimate("first") == 2
        identity = estimator.tokenizer_id
        prompt = "private prompt"
        with pytest.raises(ContextCompileRejected, match="Tokenizer failed to estimate input") as rejection:
            estimator.estimate(prompt)
        assert estimator.tokenizer_id == identity
        rendered = "".join(traceback.format_exception(rejection.value))
        assert prompt not in rendered
        assert "private encoder failure" not in rendered
        load.assert_called_once_with("synthetic-encoding")

    def test_default_estimator_identity_distinguishes_models(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def resolve(model: str) -> tuple[str, Callable[[str], int]]:
            calls.append(model)
            return "tiktoken:synthetic", len

        monkeypatch.setattr("src.core.context_budget._resolve_strict_tokenizer", resolve)
        default = DeterministicTokenEstimator()
        same = DeterministicTokenEstimator(model="gpt-4o-mini")
        different = DeterministicTokenEstimator(model="gpt-4")

        assert default.tokenizer_id == same.tokenizer_id
        assert default.tokenizer_id != different.tokenizer_id
        assert default.estimate("example") == different.estimate("example") == 7
        assert calls == ["gpt-4o-mini", "gpt-4"]

    @pytest.mark.parametrize(
        "error",
        [
            ModuleNotFoundError("private submodule failure", name="tiktoken.registry"),
            ModuleNotFoundError("private dependency failure", name="synthetic_dependency"),
            ModuleNotFoundError("private unclassified failure"),
            ImportError("private incompatible import"),
        ],
        ids=["submodule", "dependency", "unclassified", "import-error"],
    )
    def test_default_estimator_rejects_broken_import(
        self, monkeypatch: pytest.MonkeyPatch, error: ImportError,
    ) -> None:
        original_import = importlib.import_module

        def import_module(name: str, package: str | None = None) -> object:
            if name == "tiktoken":
                raise error
            return original_import(name, package)

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        prompt = "private prompt content"
        with pytest.raises(ContextCompileRejected, match="Tokenizer failed to estimate input") as rejection:
            DeterministicTokenEstimator().estimate(prompt)

        rendered = "".join(traceback.format_exception(rejection.value))
        assert str(error) not in rendered
        assert prompt not in rendered

    @pytest.mark.parametrize("phase", ["name", "asset", "encode"])
    @pytest.mark.parametrize("error_type", [ValueError, TypeError, RuntimeError, OverflowError, OSError])
    def test_default_estimator_rejects_tokenizer_failure(
        self, monkeypatch: pytest.MonkeyPatch, phase: str, error_type: type[Exception],
    ) -> None:
        detail = "private tokenizer failure"
        prompt = "private prompt content"
        requested_encodings: list[str] = []

        def lookup(_model: str) -> str:
            if phase == "name":
                raise error_type(detail)
            return "synthetic-known"

        def encode(_text: str) -> list[int]:
            if phase == "encode":
                raise error_type(detail)
            return [1, 2]

        def load(name: str) -> SimpleNamespace:
            requested_encodings.append(name)
            if phase == "asset" and name == "synthetic-known":
                raise error_type(detail)
            return SimpleNamespace(encode_ordinary=encode)

        def legacy_resolve(_model: str) -> SimpleNamespace:
            if phase in {"name", "asset"}:
                raise error_type(detail)
            return SimpleNamespace(encode_ordinary=encode)

        module = SimpleNamespace(
            encoding_name_for_model=lookup, get_encoding=load, encoding_for_model=legacy_resolve,
        )
        original_import = importlib.import_module

        def import_module(name: str, package: str | None = None) -> object:
            return module if name == "tiktoken" else original_import(name, package)

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        with pytest.raises(ContextCompileRejected, match="Tokenizer failed to estimate input") as rejection:
            DeterministicTokenEstimator().estimate(prompt)

        rendered = "".join(traceback.format_exception(rejection.value))
        assert detail not in rendered
        assert prompt not in rendered
        assert "cl100k_base" not in requested_encodings

    def test_default_estimator_does_not_misclassify_missing_known_asset(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        detail = "private missing asset"

        def load_encoding(name: str) -> SimpleNamespace:
            if name == "synthetic-known":
                raise KeyError(detail)
            return SimpleNamespace(encode_ordinary=lambda _text: [1])

        load = Mock(side_effect=load_encoding)
        module = SimpleNamespace(
            encoding_name_for_model=lambda _model: "synthetic-known",
            encoding_for_model=Mock(side_effect=KeyError(detail)),
            get_encoding=load,
        )
        original_import = importlib.import_module

        def import_module(name: str, package: str | None = None) -> object:
            return module if name == "tiktoken" else original_import(name, package)

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        with pytest.raises(ContextCompileRejected, match="Tokenizer failed to estimate input") as rejection:
            DeterministicTokenEstimator().estimate("private prompt content")

        assert detail not in "".join(traceback.format_exception(rejection.value))
        load.assert_called_once_with("synthetic-known")

    def test_default_estimator_rejects_missing_unknown_model_fallback(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def unknown(_model: str) -> str:
            raise KeyError("unknown synthetic model")

        detail = "private missing fallback"
        prompt = "private prompt"
        load = Mock(side_effect=OSError(detail))
        module = SimpleNamespace(encoding_name_for_model=unknown, get_encoding=load)
        original_import = importlib.import_module

        def import_module(name: str, package: str | None = None) -> object:
            return module if name == "tiktoken" else original_import(name, package)

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        with pytest.raises(ContextCompileRejected, match="Tokenizer failed to estimate input") as rejection:
            DeterministicTokenEstimator(model="unknown-synthetic").estimate(prompt)

        rendered = "".join(traceback.format_exception(rejection.value))
        assert detail not in rendered
        assert prompt not in rendered
        load.assert_called_once_with("cl100k_base")

    @pytest.mark.parametrize("phase", ["import", "name", "asset", "encode"])
    def test_default_estimator_unexpected_failure_does_not_fall_back(
        self, monkeypatch: pytest.MonkeyPatch, phase: str,
    ) -> None:
        def fail() -> None:
            raise AssertionError("unexpected tokenizer failure")

        def lookup(_model: str) -> str:
            if phase == "name":
                fail()
            return "synthetic-known"

        def encode(_text: str) -> list[int]:
            if phase == "encode":
                fail()
            return [1]

        def load(name: str) -> SimpleNamespace:
            if phase == "asset" and name == "synthetic-known":
                fail()
            return SimpleNamespace(encode_ordinary=encode)

        def legacy_resolve(_model: str) -> SimpleNamespace:
            if phase in {"name", "asset"}:
                fail()
            return SimpleNamespace(encode_ordinary=encode)

        module = SimpleNamespace(
            encoding_name_for_model=lookup, get_encoding=load, encoding_for_model=legacy_resolve,
        )
        original_import = importlib.import_module

        def import_module(name: str, package: str | None = None) -> object:
            if name == "tiktoken":
                if phase == "import":
                    fail()
                return module
            return original_import(name, package)

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        with pytest.raises(AssertionError, match="unexpected tokenizer failure"):
            DeterministicTokenEstimator().estimate("synthetic prompt")

    def test_default_estimator_retains_absent_package_heuristic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_import = importlib.import_module

        def import_module(name: str, package: str | None = None) -> object:
            if name == "tiktoken":
                raise ModuleNotFoundError("absent", name="tiktoken")
            return original_import(name, package)

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        assert DeterministicTokenEstimator().estimate("abcdefgh") == 2

    def test_default_estimator_retains_unknown_model_encoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def unknown(_model: str) -> str:
            raise KeyError("unknown synthetic model")

        encoding = SimpleNamespace(encode_ordinary=lambda _text: [1, 2, 3])
        load = Mock(return_value=encoding)
        module = SimpleNamespace(
            encoding_name_for_model=unknown, encoding_for_model=unknown, get_encoding=load,
        )
        original_import = importlib.import_module

        def import_module(name: str, package: str | None = None) -> object:
            return module if name == "tiktoken" else original_import(name, package)

        monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
        assert DeterministicTokenEstimator(model="unknown-synthetic").estimate("abcdefgh") == 3
        load.assert_called_once_with("cl100k_base")

    @pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4"])
    @pytest.mark.parametrize("adapter", ["default", "provider"])
    @pytest.mark.parametrize("budget_delta", [-1, 0])
    def test_literal_special_token_obeys_rendered_budget(
        self, tmp_path: Path, model: str, adapter: str, budget_delta: int,
    ) -> None:
        tiktoken = pytest.importorskip("tiktoken")
        from src.core.context_compiler import (
            ContextCompileRejected,
            ContextCompileRequest,
            DeterministicContextCompiler,
            DeterministicTokenEstimator,
            ValidationContext,
        )

        text = "prefix <|endoftext|> " + "\u6f22\u8a9e\U0001f642" * 24
        expected = len(tiktoken.encoding_for_model(model).encode_ordinary(text))
        estimator: TokenEstimator = (
            DeterministicTokenEstimator(model=model)
            if adapter == "default"
            else TiktokenTokenEstimator(model)
        )
        request = ContextCompileRequest(
            program_id="test-prog", edition_id=None, feature="test_feature", prompt_version="v1",
            system_instructions=text, output_schema_text="", required_evidence=(), optional_evidence=(),
            max_input_tokens=expected + budget_delta, reserved_output_tokens=0,
        )
        compiler = DeterministicContextCompiler(
            token_estimator=estimator, programs_root=tmp_path / "programs",
        )
        validation = ValidationContext(
            program_id="test-prog", run_id="run-1", execution_mode="observe", classification="internal",
        )

        assert expected > len(text) // 4
        if budget_delta < 0:
            with pytest.raises(ContextCompileRejected, match="reserved tokens"):
                compiler.compile(request, validation_context=validation)
            assert not list((tmp_path / "programs").glob("test-prog/runtime/context_manifests/*.json"))
        else:
            result = compiler.compile(request, validation_context=validation)
            assert result.manifest.token_estimate_total == expected
            assert result.manifest.tokenizer_id == estimator.tokenizer_id

    def test_tiktoken_estimator_is_injectable_into_compiler(self, tmp_path: Path) -> None:
        if not _HAS_TIKTOKEN:
            pytest.skip("tiktoken not installed")
        from src.core.context_compiler import (
            ContextCompileRequest,
            DeterministicContextCompiler,
            EvidenceSpan,
            ValidationContext,
            ContentOrigin,
            context_manifest_path,
        )

        est = resolve_token_estimator("gpt-4o")
        programs_root = tmp_path / "programs"
        compiler = DeterministicContextCompiler(token_estimator=est, programs_root=programs_root)
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
        assert context_manifest_path(compiled.manifest, programs_root=programs_root).is_file()

    @pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4"])
    def test_manifest_counts_complete_rendered_prompt(self, tmp_path: Path, model: str) -> None:
        if not _HAS_TIKTOKEN:
            pytest.skip("tiktoken not installed")
        from src.core.context_compiler import (
            ContentOrigin,
            ContextCompileRequest,
            DeterministicContextCompiler,
            EvidenceSpan,
            ValidationContext,
        )

        def span(evidence_id: str, text: str, *, required: bool) -> EvidenceSpan:
            return EvidenceSpan(
                evidence_id=evidence_id, source_family="ado", text=text, required=required,
                origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified",
                injection_screen="pass",
            )

        required = span("required", "Evidence with Unicode: Ω and 🚀.", required=True)
        optional = span("optional", "A different optional fact.", required=False)
        request = ContextCompileRequest(
            program_id="test-prog", edition_id=None, feature="test_feature", prompt_version="v1",
            system_instructions="Instructions.", output_schema_text="Schema.",
            required_evidence=(required,), optional_evidence=(optional,),
            max_input_tokens=8192, reserved_output_tokens=100,
        )
        validation = ValidationContext(
            program_id="test-prog", run_id="run-1", execution_mode="observe", classification="internal",
        )
        estimator = TiktokenTokenEstimator(model)
        result = DeterministicContextCompiler(
            token_estimator=estimator, programs_root=tmp_path / "programs",
        ).compile(request, validation_context=validation)
        required_prompt = (
            "Instructions.\n\nSchema.\n\nEvidence:\n\n"
            "[REQUIRED required origin=system trust=high verification=verified]\n"
            "Evidence with Unicode: Ω and 🚀."
        )
        assert result.manifest.included_evidence_ids == ("required", "optional")
        assert result.manifest.reserved_tokens == estimator.estimate(required_prompt) + request.reserved_output_tokens
        assert result.manifest.token_estimate_total == estimator.estimate(result.prompt_text)
        assert result.manifest.token_estimate_total + request.reserved_output_tokens <= request.max_input_tokens

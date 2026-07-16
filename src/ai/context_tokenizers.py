"""Provider-specific ``TokenEstimator`` adapters (ADF-W2.7 / Appendix A.1).

The deterministic ContextCompiler (``src/core/context_compiler.py``) consumes a
``TokenEstimator`` Protocol and ships with a Zone-A default
(``DeterministicTokenEstimator``) that wraps ``context_budget.estimate_tokens``.
That default is sufficient when a caller does not know which provider/model a
compile is destined for. Appendix A.1 also names ``src/ai/context_tokenizers.py``
as the home for *provider-specific* adapters that can be *injected* when the
target deployment is known, so the manifest's ``tokenizer_id`` field honestly
records the exact encoding the budget was computed against rather than a
generic fallback.

This module is intentionally Zone B (``src/ai``): it is the natural layering
point for anything that must know a provider's model identity, and the Zone-A
compiler stays provider-agnostic by depending only on the Protocol. ``tiktoken``
is optional here just as it is in ``context_budget``: if it is not installed the
adapters degrade to the same character heuristic.

Design:
- ``TiktokenTokenEstimator`` resolves a deployment/model name to the concrete
  ``tiktoken`` encoding (cached by tiktoken itself), encodes once, and reports a
  precise ``tokenizer_id`` (``tiktoken:<encoding>``).
- ``CharHeuristicTokenEstimator`` is the zero-dependency estimator used when
  tiktoken is unavailable or a deployment is unrecognized; it is deterministic
  and dependency-free so the same contract holds in every environment.
- ``resolve_token_estimator(deployment=...)`` is the factory callers use:
  tiktoken-backed when the deployment resolves, heuristic otherwise. This is the
  hook the tier router / a production ``DeterministicContextCompiler`` call site
  uses to pick the right adapter at compile time.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from functools import lru_cache

from src.core.context_compiler import TokenEstimator

#: tiktoken is an optional tokenizer dependency (not a provider SDK). It is the
#: same optional import ``context_budget.estimate_tokens`` already uses, kept in
#: a single ``try/except`` so a missing dependency degrades identically here.
def _try_import_tiktoken():
    try:
        return importlib.import_module("tiktoken")
    except ImportError:
        return None


#: Deployment/model -> tiktoken encoding name. tiktoken's own
#: ``encoding_for_model`` already resolves ``gpt-4o``/``gpt-4o-mini``/``o3-*``
#: to ``o200k_base`` and ``gpt-4``/``gpt-35-turbo`` to ``cl100k_base``; this
#: table exists so unrecognized Azure deployment aliases (e.g. a deployment
#: named ``xpf-gpt4o`` that is actually ``gpt-4o``) can still be pinned without
#: waiting for upstream. New entries require only a mapping change, never code.
_DEPLOYMENT_ENCODING_OVERRIDES: Mapping[str, str] = {
    # Legacy Azure OpenAI deployment aliases still seen in older programs.
    "gpt-35-turbo": "cl100k_base",
    "gpt-35-turbo-16k": "cl100k_base",
}


def _resolve_encoding_name(tiktoken_module, model_or_deployment: str) -> str | None:
    """Resolve a model/deployment name to a tiktoken encoding name, or None.

    Override table first (explicit program/operator pin wins), then tiktoken's
    own model registry, then None so the caller can fall back to the heuristic.
    """
    key = (model_or_deployment or "").strip()
    override = _DEPLOYMENT_ENCODING_OVERRIDES.get(key)
    if override is not None:
        return override
    try:
        return tiktoken_module.encoding_for_model(key).name
    except Exception:
        return None


class TiktokenTokenEstimator:
    """``TokenEstimator`` backed by the real ``tiktoken`` encoding for a known
    deployment/model. ``tokenizer_id`` is ``tiktoken:<encoding_name>`` so a
    context manifest records the exact encoding the budget was computed with.

    Construction is cheap: tiktoken memoizes ``Encoding`` objects by identity, so
    repeated construction with the same encoding name returns the same cached
    object. Encoding resolution failures raise at construction time (fail loud
    when a caller explicitly asks for a tiktoken adapter) rather than silently
    degrading -- callers that want graceful degradation should use
    ``resolve_token_estimator`` instead.
    """

    def __init__(self, deployment: str) -> None:
        tiktoken_module = _try_import_tiktoken()
        if tiktoken_module is None:
            raise _tiktoken_unavailable_error(deployment)
        encoding_name = _resolve_encoding_name(tiktoken_module, deployment)
        if encoding_name is None:
            raise ValueError(
                f"tiktoken has no encoding for deployment/model {deployment!r}; "
                "use resolve_token_estimator() for graceful fallback or pin the encoding."
            )
        self._encoding = tiktoken_module.get_encoding(encoding_name)
        self.tokenizer_id = f"tiktoken:{encoding_name}"

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        try:
            return len(self._encoding.encode(text))
        except Exception:
            # An encode failure on otherwise-resolved encoding is pathological;
            # fall back to the heuristic rather than abort the compile.
            return max(1, len(text) // 4)


class CharHeuristicTokenEstimator:
    """Zero-dependency ``TokenEstimator`` used when tiktoken is unavailable or
    a deployment is unrecognized. Mirrors the ``len(text) // 4`` heuristic in
    ``context_budget.estimate_tokens``'s fallback branches so estimates are
    consistent across environments; the divisor is configurable for English vs
    CJK-heavy corpora but defaults to the established 4-chars-per-token value.
    """

    def __init__(self, *, chars_per_token: int = 4, label: str = "heuristic") -> None:
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self._chars_per_token = chars_per_token
        self.tokenizer_id = f"char_heuristic:{label}:{chars_per_token}cpt"

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // self._chars_per_token)


def _tiktoken_unavailable_error(deployment: str) -> ImportError:
    return ImportError(
        f"tiktoken is not installed; cannot build a tiktoken-backed estimator for "
        f"{deployment!r}. Install the 'ai' extra or use resolve_token_estimator() "
        "for graceful fallback to the character heuristic."
    )


@lru_cache(maxsize=128)
def resolve_token_estimator(deployment: str) -> TokenEstimator:
    """Factory: return the best available ``TokenEstimator`` for a deployment.

    Tries a tiktoken-backed estimator first (precise ``tokenizer_id``); falls
    back to the character heuristic (with a recognizable ``tokenizer_id`` so a
    manifest still honestly records which estimator produced the budget) when
    tiktoken is missing or the deployment is unrecognized. Cached so repeated
    resolves for the same deployment return the identical adapter object.

    This is the call site a tier router or production ``DeterministicContextCompiler``
    construction uses to inject the right adapter at compile time, rather than
    relying on the Zone-A default estimator.
    """
    key = (deployment or "").strip() or "unknown"
    tiktoken_module = _try_import_tiktoken()
    if tiktoken_module is not None:
        encoding_name = _resolve_encoding_name(tiktoken_module, key)
        if encoding_name is not None:
            return TiktokenTokenEstimator(key)
    return CharHeuristicTokenEstimator(label=key)


__all__ = [
    "CharHeuristicTokenEstimator",
    "TiktokenTokenEstimator",
    "resolve_token_estimator",
]

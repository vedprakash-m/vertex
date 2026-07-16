"""ADF-W2.7 (specs/arch-data-fix.md Appendix A.1, Section 8.7): the one
deterministic ContextCompiler every provider-bound AI feature must consume
(Section 8.7.1 -- "Direct prompt assembly of unbounded evidence is
prohibited"). Zone A owns this module.

Local semantic reranking of optional evidence (Section 8.7.2 step 7) is
explicitly a no-op here: the spec keeps it "disabled until the
certification re-baseline authorizes it." Everything else in the required
ordering runs for real:

1. classification/privacy filter -- NOT this module's job: ``EvidenceSpan``'s
   own contract requires callers to hand it "normalized plain text, already
   privacy-filtered" (Appendix A.1's docstring). This compiler trusts that
   contract rather than re-filtering.
2. credential/prompt-injection screening (Appendix B.5) -- via
   ``src.core.injection_detector`` (moved here from ``src/ai`` for the same
   Zone-A-safe-deterministic-heuristic reason ADF-W0.14 moved token
   estimation). A span whose ``injection_screen`` already carries a verdict
   (screened upstream) is trusted as-is; only spans still at the default
   ``"pass"`` are actively scanned here.
3. required-evidence reservation -- required spans are never excluded by
   any later step (Section 8.7.3).
4. exact entity/source-authority filtering -- folded into salience ranking
   (factors ``entity_match``/``source_authority``) and quotas; Appendix B
   defines no separate hard-filter algorithm for this step.
5. request-local redundancy collapse (Appendix B.3).
6. deterministic salience ranking (Appendix B.1).
7. optional local semantic reranking -- no-op (see above).
8. per-source quotas (Appendix B.2 step 2).
9. token-budget packing (Appendix B.2 steps 1/3/4).
10. manifest and hash emission (Section 8.7.6), persisted content-addressed
    under ``programs/<id>/runtime/context_manifests/<context_hash>.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol

from src.core.alerts import append_or_suppress_alert
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.exceptions import StateError
from src.core.injection_detector import InjectionDetector

log = logging.getLogger(__name__)

CONTEXT_MANIFEST_SCHEMA_VERSION = "1"
#: Placeholder until ai_policy.yaml carries a real, versioned compiler policy
#: (Section 8.7.6 requires "policy_version" in every manifest).
_POLICY_VERSION = "adf-w2.7.v1"

#: Appendix B.1 candidate default weights (config
#: ``arch_data_fix.context.salience_weights`` may override without code
#: change per Phase-0 ratification).
_DEFAULT_SALIENCE_WEIGHTS: dict[str, float] = {
    "source_authority": 0.20,
    "verification_state": 0.15,
    "materiality": 0.15,
    "entity_match": 0.15,
    "risk_critical_path": 0.10,
    "recency": 0.10,
    "novelty": 0.05,
    "contradiction_value": 0.05,
    "operator_feedback": 0.05,
}

#: Appendix B.5: signal types strong enough on their own to exclude a span
#: outright (credential/tool-instruction-shaped payloads) rather than merely
#: flag it (softer imperative/override phrasing).
_HARD_INJECTION_SIGNAL_TYPES = frozenset({"base64", "data_uri", "webhook"})


class TokenEstimator(Protocol):
    tokenizer_id: str

    def estimate(self, text: str) -> int: ...


class ContentOrigin(str, Enum):
    AUTHORED = "authored"
    SYSTEM = "system"
    EXTERNAL_UNVERIFIED = "external_unverified"


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    evidence_id: str
    source_family: str
    text: str
    required: bool
    origin: ContentOrigin
    trust_level: str
    verification_state: str
    injection_screen: str  # "pass" | "flagged" | "excluded"
    salience_inputs: Mapping[str, float] = field(default_factory=dict)
    token_estimate: int = 0


@dataclass(frozen=True, slots=True)
class ContextCompileRequest:
    program_id: str
    edition_id: str | None
    feature: str
    prompt_version: str
    system_instructions: str
    output_schema_text: str
    required_evidence: tuple[EvidenceSpan, ...]
    optional_evidence: tuple[EvidenceSpan, ...]
    max_input_tokens: int
    reserved_output_tokens: int
    per_source_quotas: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidationContext:
    program_id: str
    run_id: str
    execution_mode: str
    classification: str
    workflow_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExcludedSpan:
    evidence_id: str
    content_hash: str
    reason: str  # quota_exceeded | token_budget | redundant_duplicate | injection_excluded | privacy_filtered


@dataclass(frozen=True, slots=True)
class ContextManifest:
    schema_version: str
    program_id: str
    feature: str
    prompt_version: str
    policy_version: str
    tokenizer_id: str
    model_deployment: str | None
    included_evidence_ids: tuple[str, ...]
    excluded: tuple[ExcludedSpan, ...]
    source_distribution: Mapping[str, int]
    token_estimate_total: int
    reserved_tokens: int
    truncated: bool
    classification: str
    compile_ms: float
    cache_key: str
    context_hash: str
    compiled_at: datetime


@dataclass(frozen=True, slots=True)
class CompiledContext:
    prompt_text: str
    included: tuple[EvidenceSpan, ...]
    excluded: tuple[ExcludedSpan, ...]
    manifest: ContextManifest


class ContextCompileRejected(Exception):
    """QG-32 (Appendix B.2 step 1): reserved tokens alone (system +
    schema + required evidence + reserved output) exceed max_input_tokens.
    The caller must use its deterministic fallback -- this is not retried
    with a smaller evidence set inside the compiler."""


class DeterministicTokenEstimator:
    """Default ``TokenEstimator``: wraps the existing Zone-A tokenizer
    (``context_budget.estimate_tokens``, ADF-W0.14) rather than duplicating
    tokenization logic. Provider-specific tokenizer adapters (Appendix A.1's
    "src/ai/context_tokenizers.py") can be injected instead when needed."""

    tokenizer_id = "context_budget.estimate_tokens"

    def __init__(self, *, model: str = "gpt-4o-mini") -> None:
        self._model = model

    def estimate(self, text: str) -> int:
        from src.core.context_budget import estimate_tokens

        return estimate_tokens(text, model=self._model)


class DeterministicContextCompiler:
    def __init__(
        self,
        *,
        token_estimator: TokenEstimator | None = None,
        salience_weights: Mapping[str, float] | None = None,
        injection_detector: InjectionDetector | None = None,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> None:
        self._token_estimator = token_estimator or DeterministicTokenEstimator()
        self._weights = dict(salience_weights or _DEFAULT_SALIENCE_WEIGHTS)
        self._injection_detector = injection_detector or InjectionDetector()
        self._programs_root = programs_root

    def compile(self, request: ContextCompileRequest, *, validation_context: ValidationContext) -> CompiledContext:
        start = time.perf_counter()
        excluded: list[ExcludedSpan] = []

        # Step 2: injection screening.
        required_spans = [self._screen_span(span) for span in request.required_evidence]
        screened_optional = [self._screen_span(span) for span in request.optional_evidence]

        # Step 3: required evidence is never dropped (Section 8.7.3) -- only
        # optional evidence can actually be excluded for an injection verdict.
        optional_after_injection: list[EvidenceSpan] = []
        for span in screened_optional:
            if span.injection_screen == "excluded":
                excluded.append(_excluded(span, reason="injection_excluded"))
            else:
                optional_after_injection.append(span)

        # Step 5: request-local redundancy collapse (optional evidence only;
        # required evidence is preserved whole per step 3).
        deduped_optional, redundancy_excluded = _collapse_redundancy(optional_after_injection)
        excluded.extend(redundancy_excluded)

        # Step 6: deterministic salience ranking.
        ranked_optional = _rank_by_salience(deduped_optional, self._weights)

        # Step 9 (part 1) / QG-32: reserved-token check.
        required_tokens = sum(span.token_estimate for span in required_spans)
        fixed_tokens = (
            self._token_estimator.estimate(request.system_instructions)
            + self._token_estimator.estimate(request.output_schema_text)
            + required_tokens
        )
        reserved_tokens = fixed_tokens + request.reserved_output_tokens
        if reserved_tokens > request.max_input_tokens:
            # ADF-W5.8 (Section 8.2.5's "context budget exceeded" category):
            # best-effort, must never mask the real QG-32 rejection below.
            try:
                append_or_suppress_alert(
                    program_id=request.program_id, category="context_budget_exceeded",
                    entity_type="feature", entity_id=request.feature, severity="warn",
                    message=f"reserved tokens ({reserved_tokens}) exceed max_input_tokens ({request.max_input_tokens}) (QG-32).",
                    next_command=f"vertex cockpit show --program {request.program_id}",
                    programs_root=self._programs_root,
                )
            except (OSError, StateError):
                pass
            raise ContextCompileRejected(
                f"reserved tokens ({reserved_tokens}) exceed max_input_tokens "
                f"({request.max_input_tokens}) for feature {request.feature!r} (QG-32)"
            )

        # Step 8: per-source quotas.
        quota_kept, quota_excluded = _apply_source_quotas(ranked_optional, request.per_source_quotas)
        excluded.extend(quota_excluded)

        # Step 9 (part 2): greedy token-budget packing.
        packing_ceiling = request.max_input_tokens - request.reserved_output_tokens
        packed, packing_excluded = _pack_within_budget(quota_kept, running_total=fixed_tokens, ceiling=packing_ceiling)
        excluded.extend(packing_excluded)

        included = tuple(required_spans) + tuple(packed)
        prompt_text = _render_prompt(request, included)
        token_estimate_total = fixed_tokens + sum(span.token_estimate for span in packed)

        source_distribution: dict[str, int] = {}
        for span in included:
            source_distribution[span.source_family] = source_distribution.get(span.source_family, 0) + 1

        compiled_at = datetime.now(timezone.utc)
        context_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
        cache_key = _build_cache_key(request, context_hash)
        compile_ms = (time.perf_counter() - start) * 1000

        manifest = ContextManifest(
            schema_version=CONTEXT_MANIFEST_SCHEMA_VERSION,
            program_id=request.program_id,
            feature=request.feature,
            prompt_version=request.prompt_version,
            policy_version=_POLICY_VERSION,
            tokenizer_id=self._token_estimator.tokenizer_id,
            model_deployment=None,
            included_evidence_ids=tuple(span.evidence_id for span in included),
            excluded=tuple(excluded),
            source_distribution=source_distribution,
            token_estimate_total=token_estimate_total,
            reserved_tokens=reserved_tokens,
            truncated=bool(quota_excluded or packing_excluded),
            classification=validation_context.classification,
            compile_ms=compile_ms,
            cache_key=cache_key,
            context_hash=context_hash,
            compiled_at=compiled_at,
        )
        _persist_manifest_best_effort(manifest, programs_root=self._programs_root)
        return CompiledContext(prompt_text=prompt_text, included=included, excluded=tuple(excluded), manifest=manifest)

    def _screen_span(self, span: EvidenceSpan) -> EvidenceSpan:
        if span.injection_screen != "pass":
            return span  # already screened upstream (or pre-flagged/excluded); trust it
        result = self._injection_detector.scan(span.text)
        if not result.injection_detected:
            return span
        if any(signal.signal_type in _HARD_INJECTION_SIGNAL_TYPES for signal in result.signals):
            return replace(span, injection_screen="excluded")
        return replace(span, injection_screen="flagged", origin=ContentOrigin.EXTERNAL_UNVERIFIED, trust_level="downgraded")


def _content_hash(text: str) -> str:
    """Appendix B.3: sha256 of NFKC-normalized, lowercased,
    whitespace-collapsed span text."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    collapsed = " ".join(normalized.split())
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


def _excluded(span: EvidenceSpan, *, reason: str) -> ExcludedSpan:
    return ExcludedSpan(evidence_id=span.evidence_id, content_hash=_content_hash(span.text), reason=reason)


def _collapse_redundancy(spans: list[EvidenceSpan]) -> tuple[list[EvidenceSpan], list[ExcludedSpan]]:
    """Appendix B.3. Keyed by content hash only -- ``EvidenceSpan`` carries
    no separate "linked fact id" field to additionally scope by, and no
    provenance-list field to merge losers' source references into (the
    binding Appendix A.1 contract has neither); the achievable subset of
    B.3 is: identify duplicate renderings, keep the one with the highest
    ``source_authority`` salience input, exclude the rest."""
    by_hash: dict[str, list[EvidenceSpan]] = {}
    order: list[str] = []
    for span in spans:
        key = _content_hash(span.text)
        if key not in by_hash:
            order.append(key)
        by_hash.setdefault(key, []).append(span)

    kept: list[EvidenceSpan] = []
    excluded: list[ExcludedSpan] = []
    for key in order:
        group = by_hash[key]
        if len(group) == 1:
            kept.append(group[0])
            continue
        winner = max(group, key=lambda span: (span.salience_inputs.get("source_authority", 0.0), span.evidence_id))
        kept.append(winner)
        for loser in group:
            if loser is not winner:
                excluded.append(_excluded(loser, reason="redundant_duplicate"))
    return kept, excluded


def _score_span(span: EvidenceSpan, weights: Mapping[str, float]) -> float:
    return sum(weight * span.salience_inputs.get(factor, 0.0) for factor, weight in weights.items())


def _rank_by_salience(spans: list[EvidenceSpan], weights: Mapping[str, float]) -> list[EvidenceSpan]:
    """Appendix B.1: stable sort by (score desc, evidence_id asc). No randomness."""
    return sorted(spans, key=lambda span: (-_score_span(span, weights), span.evidence_id))


def _apply_source_quotas(
    ranked_spans: list[EvidenceSpan], quotas: Mapping[str, int]
) -> tuple[list[EvidenceSpan], list[ExcludedSpan]]:
    """Appendix B.2 step 2: within each source_family, keep the top-quota
    spans by salience (spans are already salience-ordered on input); 0 = unlimited."""
    kept: list[EvidenceSpan] = []
    excluded: list[ExcludedSpan] = []
    counts: dict[str, int] = {}
    for span in ranked_spans:
        quota = quotas.get(span.source_family, 0)
        count = counts.get(span.source_family, 0)
        if quota > 0 and count >= quota:
            excluded.append(_excluded(span, reason="quota_exceeded"))
            continue
        counts[span.source_family] = count + 1
        kept.append(span)
    return kept, excluded


def _pack_within_budget(
    spans: list[EvidenceSpan], *, running_total: int, ceiling: int
) -> tuple[list[EvidenceSpan], list[ExcludedSpan]]:
    """Appendix B.2 steps 3/4: greedily add spans (already in salience
    order) while running_total + span.token_estimate <= ceiling; spans are
    included whole or excluded whole (no mid-span truncation)."""
    packed: list[EvidenceSpan] = []
    excluded: list[ExcludedSpan] = []
    total = running_total
    for span in spans:
        if total + span.token_estimate <= ceiling:
            packed.append(span)
            total += span.token_estimate
        else:
            excluded.append(_excluded(span, reason="token_budget"))
    return packed, excluded


def _render_prompt(request: ContextCompileRequest, included: tuple[EvidenceSpan, ...]) -> str:
    parts = [request.system_instructions.strip(), request.output_schema_text.strip()]
    if included:
        evidence_lines = []
        for span in included:
            marker = "REQUIRED" if span.required else "EVIDENCE"
            header = f"[{marker} {span.evidence_id} origin={span.origin.value} trust={span.trust_level} verification={span.verification_state}]"
            evidence_lines.append(f"{header}\n{span.text}")
        parts.append("Evidence:\n\n" + "\n\n".join(evidence_lines))
    return "\n\n".join(part for part in parts if part)


def _build_cache_key(request: ContextCompileRequest, context_hash: str) -> str:
    """Section 8.8.3's full cache-key contract also includes
    model/deployment and output-schema-version -- not available at compile
    time (model/deployment is resolved by the tier router that calls this
    compiler, a separate not-yet-built piece; ADF-W2.7 does not invent that
    routing layer). This is the compile-time-available subset."""
    raw = "|".join((request.program_id, request.feature, request.prompt_version, _POLICY_VERSION, context_hash))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def context_manifest_path(manifest: ContextManifest, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / manifest.program_id / "runtime" / "context_manifests" / f"{manifest.context_hash}.json"


def _manifest_to_dict(manifest: ContextManifest) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "program_id": manifest.program_id,
        "feature": manifest.feature,
        "prompt_version": manifest.prompt_version,
        "policy_version": manifest.policy_version,
        "tokenizer_id": manifest.tokenizer_id,
        "model_deployment": manifest.model_deployment,
        "included_evidence_ids": list(manifest.included_evidence_ids),
        "excluded": [
            {"evidence_id": entry.evidence_id, "content_hash": entry.content_hash, "reason": entry.reason}
            for entry in manifest.excluded
        ],
        "source_distribution": dict(manifest.source_distribution),
        "token_estimate_total": manifest.token_estimate_total,
        "reserved_tokens": manifest.reserved_tokens,
        "truncated": manifest.truncated,
        "classification": manifest.classification,
        "compile_ms": manifest.compile_ms,
        "cache_key": manifest.cache_key,
        "context_hash": manifest.context_hash,
        "compiled_at": manifest.compiled_at.isoformat(),
    }


def _persist_manifest_best_effort(manifest: ContextManifest, *, programs_root: Path) -> None:
    """Content-addressed (Appendix A.1): identical context_hash means an
    identical manifest was already persisted, so this is a no-op on
    collision. Best-effort -- a local disk write failure must never break
    the compile itself (the caller already has the CompiledContext in
    memory; the manifest file is an audit artifact, not the source of truth)."""
    try:
        path = context_manifest_path(manifest, programs_root=programs_root)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_manifest_to_dict(manifest), indent=2, sort_keys=True), encoding="utf-8")
    except Exception:  # pragma: no cover - audit write must never break compile
        log.warning("context_compiler: failed to persist manifest %s", manifest.context_hash, exc_info=True)


__all__ = [
    "CompiledContext",
    "ContentOrigin",
    "ContextCompileRejected",
    "ContextCompileRequest",
    "ContextManifest",
    "DeterministicContextCompiler",
    "DeterministicTokenEstimator",
    "EvidenceSpan",
    "ExcludedSpan",
    "TokenEstimator",
    "ValidationContext",
    "context_manifest_path",
]

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
import math
import os
import tempfile
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Protocol

from src.core.alerts import append_or_suppress_alert
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.exceptions import StateError
from src.core.injection_detector import InjectionDetector
from src.core.workspace_paths import is_bare_identifier

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
    """Reject an unsafe or unbuildable context without dropping required evidence."""


class DeterministicTokenEstimator:
    """Default ``TokenEstimator`` using the Zone-A strict tokenizer resolver.

    Provider-specific adapters can be injected instead when needed.
    """

    def __init__(self, *, model: str = "gpt-4o-mini") -> None:
        self._model = model
        self._resolution_lock = Lock()
        self._estimate: Callable[[str], int] | None = None
        self.tokenizer_id = self._identity("unresolved")

    def _identity(self, backend: str) -> str:
        return "context_budget.estimate_tokens:" + json.dumps(
            [self._model, backend], separators=(",", ":"), ensure_ascii=True,
        )

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        from src.core.context_budget import _resolve_strict_tokenizer

        try:
            with self._resolution_lock:
                estimate = self._estimate
                if estimate is None:
                    backend, estimate = _resolve_strict_tokenizer(self._model)
                    self.tokenizer_id = self._identity(backend)
                    self._estimate = estimate
            return estimate(text)
        except (ImportError, KeyError, ValueError, TypeError, RuntimeError, OverflowError, OSError):
            raise ContextCompileRejected("Tokenizer failed to estimate input.") from None


class DeterministicContextCompiler:
    def __init__(
        self,
        *,
        token_estimator: TokenEstimator | None = None,
        salience_weights: Mapping[str, float] | None = None,
        injection_detector: InjectionDetector | None = None,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> None:
        self._token_estimator = token_estimator if token_estimator is not None else DeterministicTokenEstimator()
        self._weights = dict(salience_weights or _DEFAULT_SALIENCE_WEIGHTS)
        self._injection_detector = injection_detector if injection_detector is not None else InjectionDetector()
        self._programs_root = programs_root

    def compile(self, request: ContextCompileRequest, *, validation_context: ValidationContext) -> CompiledContext:
        if not isinstance(request.program_id, str) or not is_bare_identifier(request.program_id):
            raise ContextCompileRejected("Context compiler requires a bare program identifier.")
        if request.program_id != validation_context.program_id:
            raise ContextCompileRejected("Context compiler program identifiers do not match.")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (request.max_input_tokens, request.reserved_output_tokens)
        ):
            raise ContextCompileRejected("Context compiler token budgets must be non-negative integers.")
        if any(span.required is not True for span in request.required_evidence) or any(
            span.required is not False for span in request.optional_evidence
        ):
            raise ContextCompileRejected("Context compiler evidence requiredness does not match its collection.")
        quota_error = "Context compiler source quotas must map source-family strings to non-negative integers."
        if not isinstance(request.per_source_quotas, Mapping):
            raise ContextCompileRejected(quota_error)
        source_quotas = dict(request.per_source_quotas)
        if any(
            not isinstance(source, str) or not isinstance(quota, int) or isinstance(quota, bool) or quota < 0
            for source, quota in source_quotas.items()
        ):
            raise ContextCompileRejected(quota_error)
        if any(
            not isinstance(span.injection_screen, str)
            or span.injection_screen not in ("pass", "flagged", "excluded")
            for span in (*request.required_evidence, *request.optional_evidence)
        ):
            raise ContextCompileRejected("Context compiler injection-screen verdicts must be pass, flagged, or excluded.")

        start = time.perf_counter()
        excluded: list[ExcludedSpan] = []

        # Step 2: injection screening. RELO-W1.2 (Sec 8.2.1): every span's
        # token_estimate is recomputed here from its actual rendered
        # representation (header + text, matching _render_prompt exactly) --
        # a caller-supplied token_estimate is advisory only and never trusted
        # for budget math from this point on.
        required_spans = [self._recompute_tokens(self._screen_span(span)) for span in request.required_evidence]
        screened_optional = [self._recompute_tokens(self._screen_span(span)) for span in request.optional_evidence]

        # RELO-W1.2 (Sec 8.2.1): a required span carrying a hard-injection
        # verdict rejects the whole compilation with an evidence-gap record,
        # rather than either silently including it (a prompt-injection risk)
        # or silently excluding it (arch-data-fix.md's older "required is
        # never dropped" rule, which predates this stricter RELO contract).
        hard_excluded_required = [span for span in required_spans if span.injection_screen == "excluded"]
        if hard_excluded_required:
            raise ContextCompileRejected(
                "required evidence "
                f"{', '.join(span.evidence_id for span in hard_excluded_required)} "
                f"carries a hard prompt-injection verdict for feature {request.feature!r}; "
                "compilation rejected rather than including or silently dropping required evidence (evidence_gap)"
            )

        # Step 3: required evidence is never dropped for any other reason
        # (Section 8.7.3) -- only optional evidence can be excluded for a
        # (non-hard) injection verdict.
        optional_after_injection: list[EvidenceSpan] = []
        for span in screened_optional:
            if span.injection_screen == "excluded":
                excluded.append(_excluded(span, reason="injection_excluded"))
            else:
                salience_error = "Context compiler consumed salience inputs must be numbers between 0 and 1."
                if not isinstance(span.salience_inputs, Mapping):
                    raise ContextCompileRejected(salience_error)
                salience_inputs = dict(span.salience_inputs)
                if any(
                    (factor == "source_authority" or factor in self._weights)
                    and (
                        isinstance(value, bool) or not isinstance(value, (int, float))
                        or not 0 <= value <= 1
                    )
                    for factor, value in salience_inputs.items()
                ):
                    raise ContextCompileRejected(salience_error)
                optional_after_injection.append(replace(span, salience_inputs=salience_inputs))

        # Step 5: request-local redundancy collapse (optional evidence only;
        # required evidence is preserved whole per step 3).
        deduped_optional, redundancy_excluded = _collapse_redundancy(optional_after_injection)
        excluded.extend(redundancy_excluded)

        # Step 6: deterministic salience ranking.
        ranked_optional = _rank_by_salience(deduped_optional, self._weights)

        # Step 9 (part 1) / QG-32: reserved-token check.
        required_prompt = _render_prompt(request, tuple(required_spans))
        required_tokens = self._estimate_tokens(required_prompt)
        reserved_tokens = required_tokens + request.reserved_output_tokens
        self._check_budget(request, total_tokens=reserved_tokens, label="reserved tokens")

        # Step 8: per-source quotas.
        quota_kept, quota_excluded = _apply_source_quotas(ranked_optional, source_quotas)
        excluded.extend(quota_excluded)

        # Step 9 (part 2): greedy token-budget packing.
        packing_ceiling = request.max_input_tokens - request.reserved_output_tokens
        packed: list[EvidenceSpan] = []
        packing_excluded: list[ExcludedSpan] = []
        prompt_text = required_prompt
        token_estimate_total = required_tokens
        if quota_kept:
            separator_tokens = self._estimate_tokens("\n\n")
            for span in quota_kept[:2]:
                candidate_prompt = _render_prompt(request, (*required_spans, *packed, span))
                candidate_tokens = self._estimate_tokens(candidate_prompt)
                if candidate_tokens <= packing_ceiling:
                    packed.append(span)
                    prompt_text = candidate_prompt
                    token_estimate_total = candidate_tokens
                else:
                    packing_excluded.append(_excluded(span, reason="token_budget"))
            if len(quota_kept) > 2:
                first_prefix_tokens = separator_tokens
                if not packed and not required_spans:
                    first_prefix = ("\n\n" if required_prompt else "") + "Evidence:\n\n"
                    first_prefix_tokens = self._estimate_tokens(first_prefix)
                later_packed, later_excluded = _pack_within_budget(
                    quota_kept[2:], running_total=token_estimate_total, ceiling=packing_ceiling,
                    first_prefix_tokens=first_prefix_tokens, separator_tokens=separator_tokens,
                )
                packed.extend(later_packed)
                packing_excluded.extend(later_excluded)
                if later_packed:
                    prompt_text = _render_prompt(request, (*required_spans, *packed))
                    token_estimate_total = self._estimate_tokens(prompt_text)
        excluded.extend(packing_excluded)

        included = tuple(required_spans) + tuple(packed)
        self._check_budget(
            request, total_tokens=token_estimate_total + request.reserved_output_tokens,
            label="rendered prompt and reserved output tokens",
        )

        source_distribution: dict[str, int] = {}
        for span in included:
            source_distribution[span.source_family] = source_distribution.get(span.source_family, 0) + 1

        compiled_at = datetime.now(timezone.utc)
        context_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
        tokenizer_id = self._token_estimator.tokenizer_id
        cache_key = _build_cache_key(request, context_hash, tokenizer_id=tokenizer_id)
        compile_ms = (time.perf_counter() - start) * 1000

        manifest = ContextManifest(
            schema_version=CONTEXT_MANIFEST_SCHEMA_VERSION,
            program_id=request.program_id,
            feature=request.feature,
            prompt_version=request.prompt_version,
            policy_version=_POLICY_VERSION,
            tokenizer_id=tokenizer_id,
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

    def _estimate_tokens(self, text: str) -> int:
        estimate = self._token_estimator.estimate(text)
        if not isinstance(estimate, int) or isinstance(estimate, bool) or estimate < 0:
            raise ContextCompileRejected("Context compiler token estimates must be non-negative integers.")
        return estimate

    def _check_budget(self, request: ContextCompileRequest, *, total_tokens: int, label: str) -> None:
        if total_tokens <= request.max_input_tokens:
            return
        message = f"{label} ({total_tokens}) exceed max_input_tokens ({request.max_input_tokens})"
        # ADF-W5.8: alert persistence must not mask the QG-32 rejection.
        try:
            append_or_suppress_alert(
                program_id=request.program_id, category="context_budget_exceeded",
                entity_type="feature", entity_id=request.feature, severity="warn",
                message=f"{message} (QG-32).",
                next_command=f"vertex cockpit show --program {request.program_id}",
                programs_root=self._programs_root,
            )
        except (OSError, StateError):
            log.warning(
                "context_compiler: unable to record context budget alert; QG-32 rejection remains in effect"
            )
        raise ContextCompileRejected(f"{message} for feature {request.feature!r} (QG-32)")

    def _screen_span(self, span: EvidenceSpan) -> EvidenceSpan:
        if span.injection_screen != "pass":
            return span  # already screened upstream (or pre-flagged/excluded); trust it
        result = self._injection_detector.scan(span.text)
        if not result.injection_detected:
            return span
        if any(signal.signal_type in _HARD_INJECTION_SIGNAL_TYPES for signal in result.signals):
            return replace(span, injection_screen="excluded")
        return replace(span, injection_screen="flagged", origin=ContentOrigin.EXTERNAL_UNVERIFIED, trust_level="downgraded")

    def _recompute_tokens(self, span: EvidenceSpan) -> EvidenceSpan:
        """RELO-W1.2 (Sec 8.2.1): a caller-supplied ``token_estimate`` is
        advisory only. Recompute centrally from the span's actual rendered
        representation (the same header+text `_render_span_text` builds),
        so accounting reflects what is truly emitted into the prompt, not
        raw span text alone."""
        return replace(span, token_estimate=self._estimate_tokens(_render_span_text(span)))


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
    message = "Context compiler salience score must be a finite number."
    try:
        score = sum(weight * span.salience_inputs.get(factor, 0.0) for factor, weight in weights.items())
        if math.isfinite(score):
            return score
    except (TypeError, OverflowError):
        raise ContextCompileRejected(message) from None
    raise ContextCompileRejected(message)


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
    spans: list[EvidenceSpan], *, running_total: int, ceiling: int,
    first_prefix_tokens: int, separator_tokens: int,
) -> tuple[list[EvidenceSpan], list[ExcludedSpan]]:
    """Greedily pack whole spans using separately estimated prompt boundaries."""
    packed: list[EvidenceSpan] = []
    excluded: list[ExcludedSpan] = []
    total = running_total
    for span in spans:
        prefix_tokens = separator_tokens if packed else first_prefix_tokens
        if total + prefix_tokens + span.token_estimate <= ceiling:
            packed.append(span)
            total += prefix_tokens + span.token_estimate
        else:
            excluded.append(_excluded(span, reason="token_budget"))
    return packed, excluded


def _render_span_text(span: EvidenceSpan) -> str:
    """The exact per-span text `_render_prompt` emits (header + body) --
    shared so RELO-W1.2's centralized token recomputation counts the same
    rendered representation that actually lands in the prompt, not raw
    span text alone."""
    marker = "REQUIRED" if span.required else "EVIDENCE"
    header = f"[{marker} {span.evidence_id} origin={span.origin.value} trust={span.trust_level} verification={span.verification_state}]"
    return f"{header}\n{span.text}"


def _render_prompt(request: ContextCompileRequest, included: tuple[EvidenceSpan, ...]) -> str:
    parts = [request.system_instructions.strip(), request.output_schema_text.strip()]
    if included:
        evidence_lines = [_render_span_text(span) for span in included]
        parts.append("Evidence:\n\n" + "\n\n".join(evidence_lines))
    return "\n\n".join(part for part in parts if part)


def _build_cache_key(request: ContextCompileRequest, context_hash: str, *, tokenizer_id: str) -> str:
    """Hash the compile-time identity subset; deployment and versioned
    schema and validator identities are not available here."""
    raw = json.dumps(
        (
            request.program_id,
            request.feature,
            request.prompt_version,
            _POLICY_VERSION,
            tokenizer_id,
            request.output_schema_text.strip(),
            context_hash,
        ),
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def context_manifest_path(manifest: ContextManifest, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    if (
        not isinstance(manifest.program_id, str)
        or not is_bare_identifier(manifest.program_id)
        or not isinstance(manifest.context_hash, str)
        or not is_bare_identifier(manifest.context_hash)
    ):
        raise StateError("Context manifest path requires bare program and hash identifiers.")
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


def _reject_duplicate_manifest_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def _warn_if_manifest_differs(path: Path, expected: dict[str, object], serialized: str) -> None:
    limit = 2 * len(serialized)
    try:
        for attempt in range(6):
            try:
                with path.open("rb") as stream:
                    raw = stream.read(limit + 1)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 5:
                    raise
                # A concurrent Windows rename can briefly deny destination reads.
                time.sleep(0.01)
        if len(raw) > limit:
            log.warning("context_compiler: unable to verify existing context manifest")
            return
        existing = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_manifest_keys)
    except (OSError, UnicodeError, ValueError, RecursionError):
        log.warning("context_compiler: unable to verify existing context manifest")
        return

    if not isinstance(existing, dict):
        log.warning("context_compiler: existing context manifest differs from compilation")
        return
    compiled_at = existing.pop("compiled_at", None)
    compile_ms = existing.pop("compile_ms", None)
    expected.pop("compiled_at")
    expected.pop("compile_ms")
    valid_time = False
    if isinstance(compiled_at, str):
        try:
            timestamp = datetime.fromisoformat(compiled_at)
            valid_time = timestamp.utcoffset() == timezone.utc.utcoffset(timestamp)
        except ValueError:
            pass
    valid_duration = (
        isinstance(compile_ms, (int, float))
        and not isinstance(compile_ms, bool)
        and compile_ms >= 0
        and (not isinstance(compile_ms, float) or math.isfinite(compile_ms))
    )
    if (
        not valid_time
        or not valid_duration
        or json.dumps(existing, sort_keys=True) != json.dumps(expected, sort_keys=True)
    ):
        log.warning("context_compiler: existing context manifest differs from compilation")


def _persist_manifest_best_effort(manifest: ContextManifest, *, programs_root: Path) -> None:
    """Persist a content-addressed manifest without replacing an existing one."""
    temp_path: Path | None = None
    try:
        path = context_manifest_path(manifest, programs_root=programs_root)
        payload = _manifest_to_dict(manifest)
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        if path.exists():
            _warn_if_manifest_differs(path, payload, serialized)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".context-manifest-", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        os.close(fd)
        temp_path.write_text(serialized, encoding="utf-8")
        try:
            os.link(temp_path, path)
        except FileExistsError:
            _warn_if_manifest_differs(path, payload, serialized)
        except OSError:
            if os.name != "nt":
                raise
            try:
                # Windows rename refuses an existing destination; POSIX rename may replace it.
                os.rename(temp_path, path)
            except FileExistsError:
                _warn_if_manifest_differs(path, payload, serialized)
    except Exception:  # pragma: no cover - audit write must never break compile
        log.warning("context_compiler: failed to persist context manifest")
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                log.warning("context_compiler: failed to remove temporary context manifest")


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

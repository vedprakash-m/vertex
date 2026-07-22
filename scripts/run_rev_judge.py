#!/usr/bin/env python
"""Run the REV LLM-as-judge harness (specs/backlog.md WO-4/rev_judge) over a
local fixture corpus.

Usage:
    python scripts/run_rev_judge.py --corpus-dir tests/fixtures/rev_judge_corpus
    python scripts/run_rev_judge.py --corpus-dir <dir> --out output/rev-judge-report.json

Compares DeterministicRevExtractor vs LLMRevExtractor on the same fixture
messages, then asks the ``rev_judge`` LLM feature to score both against
optional ground truth. WO-4's inventory found ``src.ai.rev.judge
.judge_extractions()`` had no production/script caller anywhere in
src/commands/ or scripts/ -- only test coverage. This script closes that gap
so the harness is something an operator can actually run.

Corpus directory layout (one file per message):
    <message_id>.txt   -- canonical message text; first line is the subject.
    ground_truth.json  -- optional {message_id: [fact_string, ...]}.

Fail-closed: if no AI deployment is configured (VERTEX_AI_DEPLOYMENT /
AZURE_OPENAI_DEPLOYMENT), the LLM extractor is unavailable and this exits
with an error rather than silently comparing the deterministic extractor
against itself.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.ai.deployment_fallback import FallbackStructuredClient, resolve_ai_deployments_for_feature  # noqa: E402
from src.ai.rev.extractor import DeterministicRevExtractor, ExtractedClaim, LLMRevExtractor, LLMRevExtractorUnavailable  # noqa: E402
from src.ai.rev.judge import judge_extractions  # noqa: E402
from src.core.rev.entity_types import EntityType  # noqa: E402
from src.core.rev.identity import CanonicalItemIdentity  # noqa: E402
from src.core.rev.normalizer import chunk_canonical  # noqa: E402
from src.core.rev.ports import HydratedContent  # noqa: E402
from src.core.rev.result import Success  # noqa: E402

log = logging.getLogger("rev_judge")


def _load_corpus(corpus_dir: Path) -> tuple[list[dict[str, str]], dict[str, str], dict[str, list[str]]]:
    messages: list[dict[str, str]] = []
    canonical_texts: dict[str, str] = {}

    ground_truth: dict[str, list[str]] = {}
    gt_path = corpus_dir / "ground_truth.json"
    if gt_path.exists():
        ground_truth = json.loads(gt_path.read_text(encoding="utf-8"))

    for text_path in sorted(corpus_dir.glob("*.txt")):
        message_id = text_path.stem
        text = text_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        subject = lines[0].strip() if lines else message_id
        canonical_texts[message_id] = text
        messages.append({"message_id": message_id, "subject": subject})
    return messages, canonical_texts, ground_truth


def _hydrate(message_id: str, text: str) -> HydratedContent:
    identity = CanonicalItemIdentity(
        source_type=EntityType.MESSAGE,
        tenant_id="local-fixture",
        principal_mailbox="fixture@local",
        container="rev_judge_corpus",
        resource_id=message_id,
    )
    return HydratedContent(
        identity=identity,
        canonical_text=text,
        normalized_source_hash="sha256:" + text.encode("utf-8").hex()[:64],
        chunks=tuple(chunk_canonical(text)) if text else (),
        route_metadata={},
        metadata_only=False,
    )


def _extract_claims(extractor, hydrated: HydratedContent, *, correlation_id: str) -> tuple[ExtractedClaim, ...]:
    """Best-effort claim extraction: any non-Success PortResult (Unsupported/
    Forbidden/RateLimited/Incomplete-with-no-value) degrades to no claims
    rather than raising, matching this harness's evaluation-only purpose."""
    result = extractor.extract(hydrated, correlation_id=correlation_id)
    if isinstance(result, Success):
        return result.value
    value = getattr(result, "value", None)
    return value if value is not None else ()


def _resolve_judge_client() -> FallbackStructuredClient | None:
    """Build the rev_judge LLM client from the standard AI deployment env
    vars, mirroring LLMRevExtractor.from_env()'s own resolution but scoped
    to the ``rev_judge`` feature (its own ai_policy.yaml budget/temperature).
    Returns None (fail-closed, not raise) when no deployment is configured.
    """
    deployments = resolve_ai_deployments_for_feature(
        feature_name="rev_judge",
        primary_candidates=(None,),
        backup_candidates=(None,),
        primary_fallback_envs=("VERTEX_AI_JUDGE_DEPLOYMENT", "VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
        backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
    )
    if not deployments:
        return None
    return FallbackStructuredClient(deployments=deployments, temperature=0.0, budget_usd=0.25)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus-dir", required=True, type=Path,
                        help="Directory of <message_id>.txt fixture files (+ optional ground_truth.json)")
    parser.add_argument("--program", default="", help="Optional program id; enables the P2-8 judge result cache")
    parser.add_argument("--programs-root", type=Path, default=REPO_ROOT / "programs")
    parser.add_argument("--out", default="output/rev-judge-report.json")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    corpus_dir: Path = args.corpus_dir
    if not corpus_dir.is_dir():
        log.error("corpus dir not found: %s", corpus_dir)
        return 2

    messages, canonical_texts, ground_truth = _load_corpus(corpus_dir)
    if not messages:
        log.error("no *.txt fixture messages found under %s", corpus_dir)
        return 2

    try:
        llm_extractor = LLMRevExtractor.from_env()
    except LLMRevExtractorUnavailable as error:
        log.error("LLM extractor unavailable: %s", error)
        log.error("Set VERTEX_AI_DEPLOYMENT (or AZURE_OPENAI_DEPLOYMENT) to run the judge comparison.")
        return 2

    judge_client = _resolve_judge_client()
    if judge_client is None:
        log.error("rev_judge LLM client unavailable — no deployment configured "
                  "(VERTEX_AI_JUDGE_DEPLOYMENT / VERTEX_AI_DEPLOYMENT / AZURE_OPENAI_DEPLOYMENT).")
        return 2

    det_extractor = DeterministicRevExtractor()
    det_claims: dict[str, tuple[ExtractedClaim, ...]] = {}
    llm_claims: dict[str, tuple[ExtractedClaim, ...]] = {}
    for msg in messages:
        mid = msg["message_id"]
        hydrated = _hydrate(mid, canonical_texts[mid])
        det_claims[mid] = _extract_claims(det_extractor, hydrated, correlation_id=f"rev-judge:{mid}:det")
        llm_claims[mid] = _extract_claims(llm_extractor, hydrated, correlation_id=f"rev-judge:{mid}:llm")

    report = judge_extractions(
        messages=messages,
        extractor_a_name="deterministic",
        extractor_a_claims=det_claims,
        extractor_b_name="llm",
        extractor_b_claims=llm_claims,
        canonical_texts=canonical_texts,
        ground_truth=ground_truth or None,
        client=judge_client,
        use_cache=bool(args.program),
        cache_program_id=args.program or None,
        cache_programs_root=args.programs_root,
    )

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = REPO_ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    log.info("report written to %s", out_path)

    print(report.render_human())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

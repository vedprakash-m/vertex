"""ContentExtractionAgent: Zone B agent for structured evidence extraction (BL-20).

Builds extraction prompts from registry lane metadata and parses structured
WorkstreamEvidence from AI responses. Zone B — must NOT import Zone C (src/m365/).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from src.core.evidence_models import EtaRecord, SourceRef, VerificationState, WorkstreamEvidence
from src.core.models import Enrichment, RiskLevel


log = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_BLOCKING_RE = re.compile(r"^(ADO|IcM|PR|PIPELINE):\d+$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExtractionContext:
    """Context provided to ContentExtractionAgent for a single lane extraction."""
    lane_id: str
    lane_why: str
    lane_what: str
    lane_name: str
    enrichments: tuple[Enrichment, ...]


class ContentExtractionAgent:
    """Zone B agent: extract structured WorkstreamEvidence from Enrichment body_text.

    Constructor injection pattern: ask_ai_fn is passed in so this class never
    imports AgencyBridge (Zone C). In production, pass AgencyBridge.ask_workiq.
    In tests, pass a mock.
    """

    def __init__(self, ask_ai_fn: Callable[[str], str | None]) -> None:
        self._ask_ai = ask_ai_fn

    def extract(self, ctx: ExtractionContext) -> WorkstreamEvidence | None:
        """Extract structured evidence for one lane. Returns None if no body_text found."""
        bodies = [e.body_text for e in ctx.enrichments if e.body_text]
        if not bodies:
            return None

        prompt = self._build_prompt(ctx, bodies)
        response = self._ask_ai(prompt)
        if not response:
            return None

        return self._parse_response(response, ctx)

    def _build_prompt(self, ctx: ExtractionContext, bodies: list[str]) -> str:
        bodies_section = "\n\n---\n\n".join(
            f"[Source {i + 1}]\n{b[:4000]}" for i, b in enumerate(bodies[:5])
        )
        today_year = date.today().year
        return f"""You are a Technical Program Manager analyzing evidence for the workstream: {ctx.lane_name!r}.

Context:
- Why this workstream matters: {ctx.lane_why}
- What to track: {ctx.lane_what}

Evidence (from emails, transcripts, documents):
{bodies_section}

Extract the following and return as JSON with exactly these keys:
{{
  "risk_level": "<blocked|high|medium|low|done|unknown>",
  "etas": [
    {{"label": "<item name>", "eta_date": "<YYYY-MM-DD>", "owner": "<alias or null>",
      "status": "<open|closed|missed>", "ado_id": "<number or null>"}}
  ],
  "blocking_items": ["<ADO:NNNNN or IcM:NNNNN or PR:NNNN or PIPELINE:NNNN>"],
  "owners": ["<name or alias>"],
  "raw_excerpts": ["<verbatim quote relevant to status>"],
  "confidence": <0.0-1.0>
}}

Rules:
- risk_level: use the HIGHEST risk level found across all sources.
- etas: only include items with explicit dates. If a date is "6/12" with no year, assume year {today_year}.
- blocking_items: ADO IDs as "ADO:NNNNN", IcM IDs as "IcM:NNNNN", PR IDs as "PR:NNNN", and pipeline runs as "PIPELINE:NNNN".
- confidence: 0.9 if evidence is unambiguous and recent; 0.5-0.7 if partial or older; 0.3 if uncertain.
- Return ONLY valid JSON. No markdown, no commentary."""

    def _parse_response(self, response: str, ctx: ExtractionContext) -> WorkstreamEvidence | None:
        try:
            payload = _extract_json(response)
            if not isinstance(payload, dict):
                log.warning("ContentExtractionAgent: non-dict JSON for lane %s", ctx.lane_id)
                return None
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("ContentExtractionAgent: JSON parse failed for %s: %s", ctx.lane_id, exc)
            return None

        risk_level = RiskLevel.from_string(payload.get("risk_level"))
        confidence = float(payload.get("confidence") or 0.0)
        confidence = max(0.0, min(1.0, confidence))

        etas: list[EtaRecord] = []
        for raw_eta in (payload.get("etas") or []):
            if not isinstance(raw_eta, dict):
                continue
            try:
                eta_date = date.fromisoformat(str(raw_eta.get("eta_date", ""))[:10])
            except ValueError:
                continue
            status_raw = str(raw_eta.get("status", "open")).lower()
            status = status_raw if status_raw in ("open", "closed", "missed") else "open"
            etas.append(EtaRecord(
                label=str(raw_eta.get("label", ""))[:100],
                eta_date=eta_date,
                owner=raw_eta.get("owner") or None,
                status=status,
                ado_id=str(raw_eta.get("ado_id")) if raw_eta.get("ado_id") else None,
            ))

        blocking_items = tuple(
            str(item) for item in (payload.get("blocking_items") or [])
            if isinstance(item, str) and _BLOCKING_RE.match(item.strip())
        )
        owners = tuple(
            str(o)[:80] for o in (payload.get("owners") or [])
            if isinstance(o, str) and o.strip()
        )
        raw_excerpts = tuple(
            str(x)[:500] for x in (payload.get("raw_excerpts") or [])
            if isinstance(x, str) and x.strip()
        )

        source_refs = tuple(
            SourceRef(
                source_type=_map_source_type(e.source),  # type: ignore[arg-type]
                description=e.excerpt,
                source_date=e.timestamp.date() if e.timestamp else None,
                author=e.author,
                permalink=e.permalink,
                extraction_method="transcript" if e.source == "transcript" else "two_hop",
                canonical_id=e.source_id,
            )
            for e in ctx.enrichments
            if e.body_text
        )

        return WorkstreamEvidence(
            lane_id=ctx.lane_id,
            synthesized_at=datetime.now(timezone.utc),
            risk_level=risk_level,
            etas=tuple(etas),
            blocking_items=blocking_items,
            owners=owners,
            source_refs=source_refs,
            raw_excerpts=raw_excerpts,
            confidence=confidence,
            narrative_summary=_build_narrative(payload, ctx),
            verification_state=VerificationState.MODEL_SELF_ATTESTED,
        )


def _extract_json(response: str) -> Any:
    """Extract and parse JSON from an AI response, stripping markdown code fences."""
    stripped = response.strip()
    fence_match = _JSON_FENCE_RE.search(stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()
    # Find first { ... } block as fallback
    brace_match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if brace_match:
        stripped = brace_match.group(0)
    return json.loads(stripped)


def _map_source_type(source: str) -> str:
    mapping = {
        "mail": "workiq_email",
        "transcript": "workiq_transcript",
        "teams_chat": "workiq_teams",
        "local_kb": "local_kb",
        "calendar": "workiq_email",
    }
    return mapping.get(source, "manual")


def _build_narrative(payload: dict, ctx: ExtractionContext) -> str:
    excerpts = payload.get("raw_excerpts") or []
    if excerpts:
        return excerpts[0][:300]
    return f"Extracted from {len(ctx.enrichments)} source(s) for lane {ctx.lane_id}."

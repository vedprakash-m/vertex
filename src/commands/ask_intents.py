"""WI-7.3: Named intent handlers for `vertex ask` (§6.12.4).

10 named intents that answer TPM questions directly from ProgramReality
with zero frontier AI cost (O-14). Tier 0/1 matching; frontier fallback
is citation-only.

Miss-loop: unroutable questions are logged to a rotated JSONL sidecar.
`--cluster-misses` reads the sidecar and proposes intent_routes.yaml entries.

Zone A callers only — this module MUST NOT import from src.ai or src.m365.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.core.program_reality import (
    AttentionItem,
    FactAssessment,
    ProgramReality,
)


# ---------------------------------------------------------------------------
# 10 named intents (§6.12.4)
# ---------------------------------------------------------------------------

NAMED_INTENTS: tuple[str, ...] = (
    "open_risks",
    "open_actions",
    "stale_decisions",
    "open_dependencies",
    "active_milestones",
    "open_conflicts",
    "attention_items",
    "commitments_slipped",
    "metrics_off_target",
    "pending_actuations",
)


# ---------------------------------------------------------------------------
# Tier-0 keyword-based intent matcher
# ---------------------------------------------------------------------------

# Each entry maps canonical intent → frozenset of keyword phrases.
# All keywords are lowercase; multi-word phrases must appear in order
# (joined by .* in the matcher regex).
_INTENT_KEYWORDS: dict[str, frozenset[str]] = {
    "open_risks": frozenset({
        "risk", "risks", "open risk", "open risks",
        "active risk", "blocking risk", "outstanding risk",
    }),
    "open_actions": frozenset({
        "action", "actions", "open action", "action item",
        "action items", "outstanding action", "to-do", "todo",
    }),
    "stale_decisions": frozenset({
        "stale decision", "stale decisions", "old decision",
        "unanswered decision", "unresolved decision",
        "decision", "decisions",
    }),
    "open_dependencies": frozenset({
        "dependency", "dependencies", "open dependency",
        "blocking dependency", "dependent on", "depends on",
    }),
    "active_milestones": frozenset({
        "milestone", "milestones", "active milestone",
        "upcoming milestone", "at risk milestone", "on track milestone",
    }),
    "open_conflicts": frozenset({
        "conflict", "conflicts", "data conflict",
        "source conflict", "open conflict", "unresolved conflict",
    }),
    "attention_items": frozenset({
        "attention", "attention items", "what needs attention",
        "priority items", "top issues", "high priority",
    }),
    "commitments_slipped": frozenset({
        "commitment", "commitments", "slipped commitment",
        "missed commitment", "overdue commitment", "commitment slip",
        "what slipped", "what has slipped",
    }),
    "metrics_off_target": frozenset({
        "metric", "metrics", "off target", "metric off target",
        "below target", "kpi", "kpis", "metric status",
    }),
    "pending_actuations": frozenset({
        "pending actuation", "pending actuations", "proposed actuation",
        "actuation queue", "pending action proposal",
    }),
}


def match_named_intent(question: str) -> str | None:
    """Tier-0 keyword match. Returns intent name or None.

    Scans the 10 named intents in definition order. First match wins.
    Case-insensitive; strips leading/trailing whitespace.
    """
    q = question.strip().lower()
    for intent in NAMED_INTENTS:
        for phrase in sorted(_INTENT_KEYWORDS[intent], key=len, reverse=True):
            # Multi-word phrase: words must appear as substrings in order
            words = phrase.split()
            if len(words) == 1:
                pattern = r"\b" + re.escape(phrase) + r"\b"
            else:
                pattern = r".*".join(r"\b" + re.escape(w) + r"\b" for w in words)
            if re.search(pattern, q):
                return intent
    return None


# ---------------------------------------------------------------------------
# Per-intent data renderers (templates)
# ---------------------------------------------------------------------------

def _render_open_risks(reality: ProgramReality) -> str:
    items = [
        a for a in reality.risks()
        if str(getattr(a.record, "status", "")).lower() not in
           ("closed", "mitigated", "accepted")
    ]
    if not items:
        return "No open risks found."
    lines = [f"Open risks for {reality.program_id} ({len(items)} total):"]
    for a in items:
        r = a.record
        impact = getattr(r, "impact", "?")
        owner = getattr(r, "owner_alias", "?")
        title = getattr(r, "title", "?")
        stale_flag = " [STALE]" if a.stale else ""
        truth = a.truth_level.value if hasattr(a.truth_level, "value") else str(a.truth_level)
        lines.append(f"  • {title} | impact={impact} | owner={owner}{stale_flag} [{truth}]")
    return "\n".join(lines)


def _render_open_actions(reality: ProgramReality) -> str:
    items = [
        a for a in reality.actions()
        if str(getattr(a.record, "status", "")).lower() in ("open", "in_progress", "proposed")
    ]
    if not items:
        return "No open action items found."
    lines = [f"Open actions for {reality.program_id} ({len(items)} total):"]
    for a in items:
        r = a.record
        text = getattr(r, "text", "?")
        owner = getattr(r, "owner_alias", "?")
        due = getattr(r, "due_date", None)
        due_str = f" | due={due}" if due else ""
        stale_flag = " [STALE]" if a.stale else ""
        truth = a.truth_level.value if hasattr(a.truth_level, "value") else str(a.truth_level)
        lines.append(f"  • {text} | owner={owner}{due_str}{stale_flag} [{truth}]")
    return "\n".join(lines)


def _render_stale_decisions(reality: ProgramReality) -> str:
    items = [a for a in reality.decisions() if a.stale]
    if not items:
        return "No stale decisions found."
    lines = [f"Stale decisions for {reality.program_id} ({len(items)} total):"]
    for a in items:
        r = a.record
        title = getattr(r, "title", "?")
        status = getattr(r, "status", "?")
        date_val = getattr(r, "decision_date", None)
        truth = a.truth_level.value if hasattr(a.truth_level, "value") else str(a.truth_level)
        lines.append(f"  • {title} | status={status} | date={date_val} [{truth}]")
    return "\n".join(lines)


def _render_open_dependencies(reality: ProgramReality) -> str:
    items = [
        a for a in reality.dependencies()
        if str(getattr(a.record, "status", "")).lower() not in ("resolved", "closed", "not_applicable")
    ]
    if not items:
        return "No open dependencies found."
    lines = [f"Open dependencies for {reality.program_id} ({len(items)} total):"]
    for a in items:
        r = a.record
        name = getattr(r, "name", getattr(r, "title", "?"))
        status = getattr(r, "status", "?")
        dep_type = getattr(r, "dependency_type", "?")
        truth = a.truth_level.value if hasattr(a.truth_level, "value") else str(a.truth_level)
        lines.append(f"  • {name} | type={dep_type} | status={status} [{truth}]")
    return "\n".join(lines)


def _render_active_milestones(reality: ProgramReality) -> str:
    items = [
        a for a in reality.milestones()
        if str(getattr(a.record, "status", "")).lower() in
           ("on_track", "at_risk", "missed", "deferred")
    ]
    if not items:
        return "No active milestones found."
    lines = [f"Active milestones for {reality.program_id} ({len(items)} total):"]
    for a in items:
        r = a.record
        name = getattr(r, "name", "?")
        status = getattr(r, "status", "?")
        target = getattr(r, "target_date", None)
        owner = getattr(r, "owner_alias", "?")
        truth = a.truth_level.value if hasattr(a.truth_level, "value") else str(a.truth_level)
        lines.append(f"  • {name} | status={status} | target={target} | owner={owner} [{truth}]")
    return "\n".join(lines)


def _render_open_conflicts(reality: ProgramReality) -> str:
    items = list(reality.conflicts(open_only=True))
    if not items:
        return "No open data conflicts found."
    lines = [f"Open conflicts for {reality.program_id} ({len(items)} total):"]
    for c in items:
        refs = ", ".join(c.entity_refs) if c.entity_refs else "—"
        lines.append(f"  • [{c.family}] {c.description} | refs={refs}")
    return "\n".join(lines)


def _render_attention_items(reality: ProgramReality) -> str:
    items = list(reality.attention())
    if not items:
        return "No attention items."
    lines = [f"Attention items for {reality.program_id} ({len(items)} total):"]
    for i in items:
        prov_flag = " [PROVISIONAL]" if i.provisional_inputs else ""
        lines.append(f"  • [P{i.priority}] [{i.kind}] {i.description}{prov_flag}")
        lines.append(f"    → {i.action_hint}")
    return "\n".join(lines)


def _render_commitments_slipped(reality: ProgramReality) -> str:
    items = [
        a for a in reality.commitments()
        if getattr(a.record, "is_slipped", False)
        and str(getattr(a.record, "status", "")).lower()
           not in ("closed", "cancelled", "delivered")
    ]
    if not items:
        return "No slipped commitments found."
    lines = [f"Slipped commitments for {reality.program_id} ({len(items)} total):"]
    for a in items:
        r = a.record
        title = getattr(r, "title", "?")
        direction = getattr(r, "direction", "?")
        due = getattr(r, "due_date", "?")
        slips = getattr(r, "slip_count", 0)
        truth = a.truth_level.value if hasattr(a.truth_level, "value") else str(a.truth_level)
        lines.append(f"  • {title} | dir={direction} | due={due} | slipped={slips}x [{truth}]")
    return "\n".join(lines)


def _render_metrics_off_target(reality: ProgramReality) -> str:
    items = list(reality.metric_observations())
    off = [
        a for a in items
        if str(getattr(a.record, "quality_state", "")).lower() in ("degraded", "stale", "unknown")
    ]
    if not off:
        return "No off-target metrics found (or no metric observations loaded)."
    lines = [f"Off-target metrics for {reality.program_id} ({len(off)} total):"]
    for a in off:
        r = a.record
        metric_id = getattr(r, "metric_id", "?")
        state = getattr(r, "quality_state", "?")
        value = getattr(r, "value", None)
        truth = a.truth_level.value if hasattr(a.truth_level, "value") else str(a.truth_level)
        val_str = f" | value={value}" if value is not None else ""
        lines.append(f"  • {metric_id} | state={state}{val_str} [{truth}]")
    return "\n".join(lines)


def _render_pending_actuations(reality: ProgramReality) -> str:
    items = list(reality.pending_actuations())
    if not items:
        return "No pending actuations."
    lines = [f"Pending actuations for {reality.program_id} ({len(items)} total):"]
    for p in items:
        lines.append(
            f"  • [{p.rule_id}] {p.operation} on {p.entity_ref} "
            f"| adapter={p.adapter} | approved={p.approved}"
        )
    return "\n".join(lines)


_INTENT_RENDERERS: dict[str, Callable[[ProgramReality], str]] = {
    "open_risks": _render_open_risks,
    "open_actions": _render_open_actions,
    "stale_decisions": _render_stale_decisions,
    "open_dependencies": _render_open_dependencies,
    "active_milestones": _render_active_milestones,
    "open_conflicts": _render_open_conflicts,
    "attention_items": _render_attention_items,
    "commitments_slipped": _render_commitments_slipped,
    "metrics_off_target": _render_metrics_off_target,
    "pending_actuations": _render_pending_actuations,
}


def render_named_intent(intent: str, reality: ProgramReality) -> str:
    """Render a named intent against loaded ProgramReality.

    Raises ``KeyError`` if intent is not a known named intent.
    Each result includes truth-level and staleness citations (O-14).
    """
    renderer = _INTENT_RENDERERS[intent]
    body = renderer(reality)
    as_of_str = reality.as_of.strftime("%Y-%m-%dT%H:%MZ") if hasattr(reality, "as_of") else "?"
    header = f"[intent={intent} | program={reality.program_id} | as_of={as_of_str}]"
    return f"{header}\n{body}"


# ---------------------------------------------------------------------------
# Miss log (unroutable questions)
# ---------------------------------------------------------------------------

_MISS_LOG_DEFAULT = Path(__file__).resolve().parents[2] / "programs" / "_shared" / "ask_misses.jsonl"


def log_miss(question: str, *, path: Path = _MISS_LOG_DEFAULT) -> None:
    """Append an unroutable question to the miss log sidecar."""
    from src.core.jsonl_utils import append_jsonl_line
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "question": question,
    }
    append_jsonl_line(path, json.dumps(entry, ensure_ascii=False) + "\n")


def cluster_misses(*, path: Path = _MISS_LOG_DEFAULT, min_cluster_size: int = 2) -> str:
    """Read miss log and return deterministic cluster proposals.

    Groups questions by shared significant tokens and proposes new
    intent_routes.yaml entries for acceptance by the operator.

    Returns a formatted string for display.
    """
    if not path.exists():
        return "No miss log found at {}".format(path)

    from src.core.jsonl_utils import parse_jsonl_line as _parse_line

    questions: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = _parse_line(line)
        except (ValueError, KeyError):
            continue
        if not isinstance(entry, dict):
            continue
        q = entry.get("question", "")
        if q:
            questions.append(q.lower())

    if not questions:
        return "Miss log is empty."

    # Simple deterministic clustering: group by most-common non-stop-word tokens
    stop = frozenset({
        "a", "an", "the", "is", "are", "what", "which", "show", "me", "list",
        "all", "of", "for", "in", "to", "do", "i", "my", "our", "and", "or",
        "can", "you", "tell", "find", "get", "give", "please", "how", "many",
        "have", "has", "with", "any", "that", "this", "it", "on", "by",
    })

    def significant_tokens(q: str) -> frozenset[str]:
        tokens = re.findall(r"\b[a-z]{3,}\b", q)
        return frozenset(t for t in tokens if t not in stop)

    # Build token → questions map
    token_to_qs: dict[str, list[str]] = {}
    for q in questions:
        for tok in significant_tokens(q):
            token_to_qs.setdefault(tok, []).append(q)

    # Find clusters: questions sharing ≥1 significant token
    clustered: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for tok, qs in sorted(token_to_qs.items(), key=lambda kv: -len(kv[1])):
        unique_qs = [q for q in qs if q not in seen]
        if len(unique_qs) < min_cluster_size:
            continue
        clustered.append((tok, unique_qs[:5]))
        seen.update(unique_qs)

    if not clustered:
        return (
            f"No clusters of ≥{min_cluster_size} found in {len(questions)} misses. "
            "All questions appear to be unique or long-tail."
        )

    lines = [
        f"Miss clusters from {path} ({len(questions)} total misses):",
        "",
        "Proposed intent_routes.yaml additions (review before accepting):",
        "",
    ]
    for i, (tok, qs) in enumerate(clustered, 1):
        lines.append(f"Cluster {i}: keyword='{tok}' ({len(qs)} hits)")
        for q in qs:
            lines.append(f"  - {q}")
        # Propose a route entry
        lines.append(f"  → Proposed route: keyword: [{tok}] → intent: <FILL_IN>")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Citation-only fallback (for questions that need frontier)
# ---------------------------------------------------------------------------

def citation_only_fallback(question: str, reality: ProgramReality) -> str:
    """Generate a citation-only response when no named intent matches.

    Returns relevant facts from ProgramReality WITHOUT AI synthesis.
    The answer cites truth levels and evidence; it never infers.
    """
    lines = [
        f"No named intent matched. Showing relevant facts for '{question[:80]}':",
        f"[program={reality.program_id} | citation-only mode]",
        "",
    ]

    # Show attention items as most relevant starting point
    attention = reality.attention()
    if attention:
        lines.append(f"Attention items ({len(attention)}):")
        for item in attention[:5]:
            lines.append(f"  • [{item.kind}] {item.description}")
        if len(attention) > 5:
            lines.append(f"  … {len(attention) - 5} more")
        lines.append("")

    # Show freshness summary
    freshness = reality.freshness()
    if freshness:
        stale_domains = [f.domain for f in freshness if f.stale_count > 0]
        if stale_domains:
            lines.append(f"Stale domains: {', '.join(stale_domains)}")
            lines.append("")

    lines.append(
        "To get a synthesized answer, use `vertex ask <question> --program <id>` "
        "after an AI deployment is configured, or use a named intent:\n"
        "  " + ", ".join(NAMED_INTENTS)
    )
    return "\n".join(lines)

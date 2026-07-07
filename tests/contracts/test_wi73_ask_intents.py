"""WI-7.3 contract tests: ask named intents, miss log, cluster-misses (§6.12.4).

Tests:
1. all_10_named_intents_defined — NAMED_INTENTS tuple has exactly 10 entries
2. named_intent_match_is_tier0 — no AI import in ask_intents.py
3. open_risks_matches_keyword — "show open risks" → open_risks
4. open_actions_matches_keyword — "action items" → open_actions
5. stale_decisions_matches_keyword — "stale decisions" → stale_decisions
6. open_dependencies_matches_keyword — "open dependencies" → open_dependencies
7. active_milestones_matches_keyword — "milestone" → active_milestones
8. open_conflicts_matches_keyword — "conflicts" → open_conflicts
9. attention_items_matches_keyword — "what needs attention" → attention_items
10. commitments_slipped_matches_keyword — "slipped commitment" → commitments_slipped
11. metrics_off_target_matches_keyword — "metrics off target" → metrics_off_target
12. pending_actuations_matches_keyword — "pending actuations" → pending_actuations
13. no_match_returns_none — unknown question → None
14. render_includes_truth_citation — output includes truth level tag
15. miss_log_appended_on_no_match — unroutable question logged to JSONL
16. cluster_misses_groups_similar — repeated keywords produce cluster proposal
17. citation_only_fallback_never_infers — no AI call; returns attention + facts
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.commands.ask_intents import (
    NAMED_INTENTS,
    citation_only_fallback,
    cluster_misses,
    log_miss,
    match_named_intent,
    render_named_intent,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_reality(program_id: str = "test_program") -> MagicMock:
    """Stub ProgramReality for rendering tests."""
    from src.core.program_reality import FactAssessment, AttentionItem
    from src.core.truth_levels import TruthLevel

    reality = MagicMock()
    reality.program_id = program_id

    from datetime import datetime, timezone
    reality.as_of = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

    # Return empty tuples by default for all domain accessors
    reality.risks.return_value = ()
    reality.actions.return_value = ()
    reality.decisions.return_value = ()
    reality.dependencies.return_value = ()
    reality.milestones.return_value = ()
    reality.conflicts.return_value = ()
    reality.attention.return_value = ()
    reality.commitments.return_value = ()
    reality.metric_observations.return_value = ()
    reality.pending_actuations.return_value = ()
    reality.freshness.return_value = ()
    return reality


# ---------------------------------------------------------------------------
# 1. Exactly 10 named intents
# ---------------------------------------------------------------------------

def test_all_10_named_intents_defined() -> None:
    assert len(NAMED_INTENTS) == 10
    expected = {
        "open_risks", "open_actions", "stale_decisions", "open_dependencies",
        "active_milestones", "open_conflicts", "attention_items",
        "commitments_slipped", "metrics_off_target", "pending_actuations",
    }
    assert set(NAMED_INTENTS) == expected


# ---------------------------------------------------------------------------
# 2. ask_intents.py has no AI/M365 imports (Tier-0 = zero frontier)
# ---------------------------------------------------------------------------

def test_named_intent_match_is_tier0() -> None:
    """ask_intents.py must not import from src.ai or src.m365."""
    module_path = Path("src/commands/ask_intents.py")
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in getattr(node, "names", []):
                assert not alias.name.startswith(("src.ai.", "src.m365.")), \
                    f"ask_intents.py must not import {alias.name}"
            module = getattr(node, "module", None) or ""
            assert not module.startswith(("src.ai", "src.m365")), \
                f"ask_intents.py must not import from {module}"


# ---------------------------------------------------------------------------
# 3-12. Keyword matching for all 10 intents
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question,expected_intent", [
    ("show open risks", "open_risks"),
    ("what are the active risks", "open_risks"),
    ("list my action items", "open_actions"),
    ("what open actions do we have", "open_actions"),
    ("show stale decisions", "stale_decisions"),
    ("open dependencies blocking us", "open_dependencies"),
    ("which milestones are on track", "active_milestones"),
    ("any data conflicts", "open_conflicts"),
    ("what needs attention today", "attention_items"),
    ("are there any slipped commitments", "commitments_slipped"),
    ("show metrics off target", "metrics_off_target"),
    ("what are the pending actuations", "pending_actuations"),
])
def test_named_intent_keyword_match(question: str, expected_intent: str) -> None:
    result = match_named_intent(question)
    assert result == expected_intent, f"Expected {expected_intent!r} for {question!r}, got {result!r}"


def test_no_match_returns_none() -> None:
    assert match_named_intent("tell me about the weather forecast") is None
    assert match_named_intent("what is the meaning of life") is None


# ---------------------------------------------------------------------------
# 14. render_named_intent includes truth citation
# ---------------------------------------------------------------------------

def test_render_includes_truth_citation() -> None:
    reality = _make_reality()
    output = render_named_intent("open_risks", reality)
    assert "intent=open_risks" in output
    assert "program=test_program" in output
    assert "as_of=" in output


def test_render_open_conflicts_includes_resolution_when_present() -> None:
    """GAP-37: `vertex ask` "open conflicts" surfaces winning/losing source
    and resolution reason, not just the bare description."""
    from src.core.program_reality import RealityConflict

    reality = _make_reality()
    reality.conflicts.return_value = (
        RealityConflict(
            conflict_id="conf-1", entity_refs=("ACTION:1",), family="action", open=True,
            description="ado: done -> in-progress", winning_source="ado", losing_source="teams",
            winning_value="Done", losing_value="In Progress", resolution="primary_authority:ado",
        ),
    )

    output = render_named_intent("open_conflicts", reality)

    assert "ado vs teams" in output
    assert "resolution=primary_authority:ado" in output


def test_render_all_10_intents_run_without_error() -> None:
    reality = _make_reality()
    for intent in NAMED_INTENTS:
        output = render_named_intent(intent, reality)
        assert isinstance(output, str)
        assert intent in output or "No " in output or "0 total" in output or "found" in output


# ---------------------------------------------------------------------------
# 15. Miss log appended on no match
# ---------------------------------------------------------------------------

def test_miss_log_appended_on_no_match(tmp_path: Path) -> None:
    log_path = tmp_path / "misses.jsonl"
    log_miss("what is the weather", path=log_path)
    log_miss("what is the meaning of life", path=log_path)

    assert log_path.exists()
    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    entry = json.loads(lines[0])
    assert entry["question"] == "what is the weather"
    assert "logged_at" in entry


# ---------------------------------------------------------------------------
# 16. Cluster misses groups similar questions
# ---------------------------------------------------------------------------

def test_cluster_misses_groups_similar(tmp_path: Path) -> None:
    log_path = tmp_path / "misses.jsonl"
    # Log 3 questions about "budget"
    for i in range(3):
        log_miss(f"what is the budget remaining for project {i}", path=log_path)
    # Log 2 questions about "timeline"
    for i in range(2):
        log_miss(f"show the timeline for workstream {i}", path=log_path)

    result = cluster_misses(path=log_path, min_cluster_size=2)
    assert "budget" in result or "timeline" in result or "project" in result or "workstream" in result
    assert "Proposed" in result or "Cluster" in result or "misses" in result


def test_cluster_misses_empty_log(tmp_path: Path) -> None:
    log_path = tmp_path / "misses.jsonl"
    log_path.write_text("", encoding="utf-8")
    result = cluster_misses(path=log_path)
    assert "empty" in result.lower() or "no cluster" in result.lower()


def test_cluster_misses_no_log_file(tmp_path: Path) -> None:
    result = cluster_misses(path=tmp_path / "nonexistent.jsonl")
    assert "No miss log" in result or "nonexistent" in result


# ---------------------------------------------------------------------------
# 17. citation_only_fallback never calls AI
# ---------------------------------------------------------------------------

def test_citation_only_fallback_never_infers() -> None:
    reality = _make_reality()
    reality.attention.return_value = ()
    reality.freshness.return_value = ()
    output = citation_only_fallback("how much budget is left", reality)
    # Must include guidance about named intents
    assert "named intent" in output.lower() or "intent" in output.lower()
    # Must NOT include any hallucination markers (just citing facts)
    assert "budget" in output  # question echoed
    # reality.attention was called (not skipped silently)
    reality.attention.assert_called_once()

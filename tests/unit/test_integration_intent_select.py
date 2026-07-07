"""Direct coverage for the extracted integration intent-select helpers (D-13)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import typer

from src.commands.integration_intent_select import _next_source_action, _resolve_selected_intent
from src.core.discovery_intent import SourceCandidateStatus


def _cand(status: SourceCandidateStatus = SourceCandidateStatus.PENDING):
    return SimpleNamespace(status=status)


def test_next_source_action_branches() -> None:
    intent = SimpleNamespace()
    assert "map it back" in _next_source_action(intent=None, derived_status="searching", candidates=[_cand()], attempts=[])
    assert "No source intent is linked" in _next_source_action(intent=None, derived_status="searching", candidates=[], attempts=[])
    assert "No action needed" in _next_source_action(intent=intent, derived_status="resolved", candidates=[], attempts=[])
    assert "Review the pending" in _next_source_action(intent=intent, derived_status="searching", candidates=[_cand()], attempts=[])
    assert "already been attempted" in _next_source_action(intent=intent, derived_status="searching", candidates=[], attempts=[object()])
    assert "No discovery evidence" in _next_source_action(intent=intent, derived_status="searching", candidates=[], attempts=[])


class _Store:
    def __init__(self, intents_by_match) -> None:  # noqa: ANN001
        self._intents_by_match = intents_by_match

    def get_intent_matches(self, candidate_id):  # noqa: ANN001
        return [SimpleNamespace(intent_id=iid) for iid in self._intents_by_match]

    def get_intent(self, intent_id):  # noqa: ANN001
        return self._intents_by_match.get(intent_id)


def _intent(iid: str):
    return SimpleNamespace(intent_id=iid)


def test_resolve_selected_intent_no_links_raises() -> None:
    store = _Store({})
    with pytest.raises(typer.BadParameter, match="not linked to any source intent"):
        _resolve_selected_intent(store, SimpleNamespace(candidate_id="c1"), intent_id=None)


def test_resolve_selected_intent_single() -> None:
    store = _Store({"i1": _intent("i1")})
    selected, others = _resolve_selected_intent(store, SimpleNamespace(candidate_id="c1"), intent_id=None)
    assert selected.intent_id == "i1"
    assert others == ()


def test_resolve_selected_intent_ambiguous_requires_intent_id() -> None:
    store = _Store({"i1": _intent("i1"), "i2": _intent("i2")})
    with pytest.raises(typer.BadParameter, match="matches multiple intents"):
        _resolve_selected_intent(store, SimpleNamespace(candidate_id="c1"), intent_id=None)


def test_resolve_selected_intent_explicit_id_unlinks_others() -> None:
    store = _Store({"i1": _intent("i1"), "i2": _intent("i2")})
    selected, others = _resolve_selected_intent(store, SimpleNamespace(candidate_id="c1"), intent_id="i1")
    assert selected.intent_id == "i1"
    assert tuple(o.intent_id for o in others) == ("i2",)


def test_resolve_selected_intent_unknown_id_raises() -> None:
    store = _Store({"i1": _intent("i1")})
    with pytest.raises(typer.BadParameter, match="not linked to intent 'i9'"):
        _resolve_selected_intent(store, SimpleNamespace(candidate_id="c1"), intent_id="i9")

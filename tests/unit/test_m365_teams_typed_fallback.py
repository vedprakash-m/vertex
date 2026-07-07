"""GAP-17: TeamsReader typed fallback path via search_teams."""
from __future__ import annotations

from types import SimpleNamespace

from src.m365.teams_reader import TeamsReader


def _bridge_with_typed_results(results: list[dict]) -> object:
    """Bridge that returns structured Graph-API data from search_teams."""

    def _search_teams(**kwargs):
        return {"value": results}

    return SimpleNamespace(search_teams=_search_teams)


def _bridge_with_workiq_only(payload: dict | None) -> object:
    """Bridge with only ask_workiq (no typed search_teams)."""

    def _ask_workiq(*args, **kwargs):
        return payload

    return SimpleNamespace(ask_workiq=_ask_workiq)


def test_search_teams_uses_typed_path_when_available() -> None:
    """When bridge exposes search_teams, it is preferred and source='graph'."""
    bridge = _bridge_with_typed_results(
        [
            {
                "id": "msg-1",
                "channel": "eng-planning",
                "from": {"user": {"displayName": "Alice"}},
                "createdDateTime": "2026-06-10T12:00:00Z",
                "webUrl": "https://teams.example/m1",
                "bodyPreview": "Hello",
                "threadId": "thread-1",
            }
        ]
    )
    reader = TeamsReader(bridge)  # type: ignore[arg-type]
    page = reader.search_teams(channel="eng-planning", query="hello")
    assert page.source == "graph"
    assert len(page.records) == 1
    rec = page.records[0]
    assert rec.source_id == "msg-1"
    assert rec.channel == "eng-planning"
    assert rec.sender == "Alice"
    assert rec.sent_at == "2026-06-10T12:00:00Z"
    assert rec.web_url == "https://teams.example/m1"
    assert rec.preview == "Hello"
    assert rec.thread_id == "thread-1"


def test_search_teams_falls_back_to_workiq_when_typed_returns_empty() -> None:
    """If typed path returns 0 records, fall back to workiq prose path."""
    bridge_typed = _bridge_with_typed_results([])

    def _ask_workiq(*args, **kwargs):
        return {
            "messages": [
                {
                    "id": "msg-2",
                    "channel": "design",
                    "bodyPreview": "Design review",
                    "webUrl": "https://teams.example/m2",
                }
            ]
        }

    bridge_typed.ask_workiq = _ask_workiq  # type: ignore[attr-defined]
    reader = TeamsReader(bridge_typed)  # type: ignore[arg-type]
    page = reader.search_teams(channel="design", query="review")
    assert page.source == "workiq"
    assert len(page.records) == 1
    assert page.records[0].source_id == "msg-2"


def test_search_teams_falls_back_to_workiq_when_typed_unavailable() -> None:
    """If bridge has no search_teams, fall back to ask_workiq."""
    bridge = _bridge_with_workiq_only(
        {
            "messages": [
                {
                    "id": "msg-3",
                    "channel": "ops",
                    "bodyPreview": "Ops sync",
                }
            ]
        }
    )
    reader = TeamsReader(bridge)  # type: ignore[arg-type]
    page = reader.search_teams(channel="ops", query="sync")
    assert page.source == "workiq"
    assert page.records[0].source_id == "msg-3"


def test_search_teams_handles_typed_bridge_exception() -> None:
    """If typed path raises, fall back to workiq."""

    def _search_teams(**kwargs):
        raise ConnectionError("Graph API down")

    def _ask_workiq(*args, **kwargs):
        return {
            "messages": [
                {
                    "id": "msg-4",
                    "channel": "eng",
                    "bodyPreview": "Fallback hit",
                }
            ]
        }

    bridge = SimpleNamespace(search_teams=_search_teams, ask_workiq=_ask_workiq)
    reader = TeamsReader(bridge)  # type: ignore[arg-type]
    page = reader.search_teams(channel="eng", query="anything")
    assert page.source == "workiq"
    assert page.records[0].source_id == "msg-4"


def test_search_teams_handles_typed_path_signature_mismatch() -> None:
    """Older bridge signature (no since/limit) still works."""

    def _search_teams(*, channel, query):  # no since, no limit
        return {
            "value": [
                {
                    "id": "msg-5",
                    "channel": channel,
                    "bodyPreview": f"Result for {query}",
                }
            ]
        }

    bridge = SimpleNamespace(search_teams=_search_teams)
    reader = TeamsReader(bridge)  # type: ignore[arg-type]
    page = reader.search_teams(channel="qa", query="flaky")
    assert page.source == "graph"
    assert page.records[0].source_id == "msg-5"


def test_search_teams_handles_list_payload_shape() -> None:
    """Typed path may return a bare list rather than {value: [...]}."""
    bridge = SimpleNamespace(
        search_teams=lambda **kw: [
            {"id": "msg-6", "channel": "x", "bodyPreview": "y"}
        ]
    )
    reader = TeamsReader(bridge)  # type: ignore[arg-type]
    page = reader.search_teams(channel="x", query="y")
    assert page.source == "graph"
    assert page.records[0].source_id == "msg-6"


def test_records_from_typed_payload_skips_non_dict_entries() -> None:
    """Non-dict entries in the payload are silently skipped."""
    bridge = SimpleNamespace(
        search_teams=lambda **kw: {
            "value": [
                "junk-string",
                42,
                None,
                {"id": "ok", "channel": "c", "bodyPreview": "p"},
            ]
        }
    )
    reader = TeamsReader(bridge)  # type: ignore[arg-type]
    page = reader.search_teams(channel="c", query="p")
    assert page.source == "graph"
    assert len(page.records) == 1
    assert page.records[0].source_id == "ok"

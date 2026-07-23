"""Unit tests for src/ai/setup_assistant.py's response parsing.

specs/backlog.md BL-C2: setup.py's own near-duplicate inline workstream-
suggestion parser was retired in favor of this module's
_parse_ai_workstreams -- this test (moved from test_commands_setup.py's
now-deleted _parse_ws_suggestions test) preserves the PII-scrub safety-net
coverage that consolidation must not silently drop.
"""

from __future__ import annotations

from src.ai.setup_assistant import SuggestedWorkstream, _parse_ai_workstreams


def test_parse_ai_workstreams_runs_text_through_safety_pipeline() -> None:
    suggestions = _parse_ai_workstreams(
        {
            "workstreams": [
                {
                    "name": "Reliability",
                    "description": "Track platform health with foo@gmail.com.",
                }
            ]
        }
    )

    assert suggestions == [
        SuggestedWorkstream(
            name="Reliability",
            description="Track platform health with [PII-FILTERED-EMAIL].",
            area_paths=(),
            confidence="inferred",
            rationale="",
        )
    ]


def test_parse_ai_workstreams_skips_entries_with_blank_name() -> None:
    suggestions = _parse_ai_workstreams(
        {"workstreams": [{"name": "  ", "description": "No name here."}]}
    )

    assert suggestions == []


def test_parse_ai_workstreams_returns_empty_for_non_dict_payload() -> None:
    assert _parse_ai_workstreams(["not", "a", "dict"]) == []


def test_parse_ai_workstreams_returns_empty_when_workstreams_key_is_not_a_list() -> None:
    assert _parse_ai_workstreams({"workstreams": "not-a-list"}) == []

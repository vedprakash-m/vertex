from __future__ import annotations

from src.ai.ai_mode import AIMode, set_ai_mode
from src.commands.decision_brief import _enrich_with_ai
from src.core.decision_brief_engine import DecisionBrief


def test_enrich_with_ai_returns_original_brief_when_invocation_ai_disabled() -> None:
    brief = DecisionBrief(
        issue_number=42,
        edition_name="acme_weekly",
        generated_at="2026-06-06 12:00",
        items=(),
        total_pending=0,
        ai_enriched=False,
    )
    set_ai_mode(AIMode.DISABLED)
    try:
        enriched = _enrich_with_ai(
            brief=brief,
            bundle=object(),
            program_id="acme",
            create_ai_client=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("AI client should not be created when AIMode.DISABLED")),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert enriched is brief

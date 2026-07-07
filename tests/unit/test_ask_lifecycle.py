from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.ask_lifecycle import DecisionAskLifecycleStage, build_decision_ask_lifecycle_proposals, evaluate_decision_ask_lifecycle
from src.core.models_v2 import DecisionAsk


def test_evaluate_decision_ask_lifecycle_returns_nudge_for_aged_inactive_ask() -> None:
    proposal = evaluate_decision_ask_lifecycle(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Need LT decision on contingency scope.",
            entity_refs=("WI:1201",),
            ask_date=date(2026, 5, 5),
            owner_alias="operator",
        ),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )

    assert proposal is not None
    assert proposal.stage is DecisionAskLifecycleStage.NUDGE
    assert proposal.inactive_days == 16
    assert proposal.command == "vertex decisions nudge --program acme --id ask-1 --dry-run"


def test_evaluate_decision_ask_lifecycle_uses_last_touch_to_suppress_followup() -> None:
    proposal = evaluate_decision_ask_lifecycle(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Need LT decision on contingency scope.",
            entity_refs=("WI:1201",),
            ask_date=date(2026, 5, 1),
            owner_alias="operator",
            last_touched_at=datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc),
        ),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )

    assert proposal is None


def test_evaluate_decision_ask_lifecycle_returns_watch_for_newly_aged_ask() -> None:
    proposal = evaluate_decision_ask_lifecycle(
        DecisionAsk(
            id="ask-watch",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Confirm the owner and date still look current.",
            entity_refs=("WI:1202",),
            ask_date=date(2026, 5, 13),
            owner_alias="operator",
        ),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )

    assert proposal is not None
    assert proposal.stage is DecisionAskLifecycleStage.WATCH
    assert proposal.command == "vertex decisions aging --program acme --min-age-days 7"


def test_build_decision_ask_lifecycle_proposals_sorts_escalations_ahead_of_nudges() -> None:
    proposals = build_decision_ask_lifecycle_proposals(
        (
            DecisionAsk(
                id="ask-nudge",
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                text="Need follow-up.",
                entity_refs=(),
                ask_date=date(2026, 5, 5),
                owner_alias="operator",
            ),
            DecisionAsk(
                id="ask-escalate",
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                text="Need escalation.",
                entity_refs=(),
                ask_date=date(2026, 4, 25),
                owner_alias="operator",
            ),
        ),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )

    assert [proposal.ask.id for proposal in proposals] == ["ask-escalate", "ask-nudge"]
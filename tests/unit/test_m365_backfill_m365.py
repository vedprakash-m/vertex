from __future__ import annotations

from src.core.backfill_loader import BackfillConfig, BackfillDirection, BackfillDirectionGroup
from src.core.exceptions import StateError
from src.m365.agency_bridge import AgencyCapabilities
from src.m365.backfill_m365 import M365Backfiller


class _FakeBridge:
    def __init__(self, capabilities: AgencyCapabilities, responses: list[dict[str, object] | None]) -> None:
        self.capabilities = capabilities
        self.responses = responses
        self.questions: list[str] = []

    def probe(self) -> AgencyCapabilities:
        return self.capabilities

    def ask_workiq(self, question: str) -> dict[str, object] | None:
        self.questions.append(question)
        return self.responses.pop(0)


def test_m365_backfiller_discovers_structured_and_summary_results() -> None:
    bridge = _FakeBridge(
        AgencyCapabilities(available=True, has_workiq=True, tier="msft"),
        responses=[
            {"items": [{"id": "mail-1", "subject": "Issue 051", "webUrl": "https://outlook.office.com/mail/1"}]},
            {"response": "Found 3 feedback threads involving Rushi and newsletter drafts."},
        ],
    )
    backfiller = M365Backfiller(bridge)

    discoveries = backfiller.discover_all(
        BackfillConfig(
            newsletters=BackfillDirectionGroup(
                search_strategy="m365",
                directions=(
                    BackfillDirection(
                        question=None,
                        source="email",
                        filter="to:acme_newsletter@example.com",
                        date_range="last 12 months",
                        description="Past issues",
                        extract=None,
                    ),
                ),
            ),
            feedback=BackfillDirectionGroup(
                search_strategy=None,
                directions=(
                    BackfillDirection(
                        question="Find review feedback threads",
                        source=None,
                        filter=None,
                        date_range=None,
                        description=None,
                        extract=None,
                    ),
                ),
            ),
            meetings=BackfillDirectionGroup(search_strategy=None, directions=()),
            people_intelligence=BackfillDirectionGroup(search_strategy=None, directions=()),
        )
    )

    assert bridge.questions == [
        "email | to:acme_newsletter@example.com | last 12 months | Past issues",
        "Find review feedback threads",
    ]
    assert discoveries["newsletters"][0].source_id == "mail-1"
    assert discoveries["newsletters"][0].permalink == "https://outlook.office.com/mail/1"
    assert discoveries["feedback"][0].summary == "Found 3 feedback threads involving Rushi and newsletter drafts."


def test_m365_backfiller_requires_workiq_access() -> None:
    backfiller = M365Backfiller(_FakeBridge(AgencyCapabilities(available=False), responses=[]))

    try:
        backfiller.discover_all(
            BackfillConfig(
                newsletters=BackfillDirectionGroup(search_strategy="m365", directions=()),
                feedback=BackfillDirectionGroup(search_strategy=None, directions=()),
                meetings=BackfillDirectionGroup(search_strategy=None, directions=()),
                people_intelligence=BackfillDirectionGroup(search_strategy=None, directions=()),
            )
        )
    except StateError as error:
        assert "WorkIQ access is unavailable" in str(error)
    else:
        raise AssertionError("Expected StateError when WorkIQ access is unavailable")
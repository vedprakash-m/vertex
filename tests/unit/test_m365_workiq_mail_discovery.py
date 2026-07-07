from __future__ import annotations

from src.m365.graph_mail_client import MailRecord, MailSearchPage
from src.m365.workiq_mail_discovery import WorkIQMailDiscovery


class _FakeMailClient:
    def __init__(self, pages: dict[str, MailSearchPage]) -> None:
        self.pages = pages
        self.queries: list[str] = []

    def search_emails(
        self,
        *,
        query: str,
        limit: int = 25,
        cursor: str | None = None,
        timeout_seconds: int | None = None,
        allow_cli_fallback: bool = True,
    ) -> MailSearchPage:
        del limit, cursor, timeout_seconds
        self.allow_cli_fallback = allow_cli_fallback
        self.queries.append(query)
        return self.pages.get(query, MailSearchPage(records=(), next_cursor=None, source="workiq"))


def test_workiq_mail_discovery_prefers_owner_matched_thread_candidate() -> None:
    client = _FakeMailClient(
        {
            "Firmware Ramp Mail": MailSearchPage(
                records=(
                    MailRecord(
                        source_id="mail-1",
                        subject="Firmware Ramp Mail Update",
                        sender="operator@example.com",
                        recipients=("ops@example.com",),
                        received_at=None,
                        web_url="https://outlook.office.com/mail/mail-1",
                        preview="Latest firmware ramp status.",
                        thread_id="thread-1",
                    ),
                    MailRecord(
                        source_id="mail-2",
                        subject="Firmware Ramp Mail Update",
                        sender="other@example.com",
                        recipients=("ops@example.com",),
                        received_at=None,
                        web_url="https://outlook.office.com/mail/mail-2",
                        preview="Latest firmware ramp status.",
                        thread_id="thread-2",
                    ),
                ),
                next_cursor=None,
                source="workiq",
            )
        }
    )
    discovery = WorkIQMailDiscovery(mail_client=client)

    candidates = discovery.discover_candidates(
        "Firmware Ramp Mail",
        limit=5,
        owner_aliases=("operator@example.com",),
    )

    assert [candidate.discovered_id for candidate in candidates] == ["thread-1", "thread-2"]
    assert candidates[0].match_score > candidates[1].match_score


def test_workiq_mail_discovery_skips_message_only_results_without_thread_id() -> None:
    client = _FakeMailClient(
        {
            "SCHIE Mail Thread": MailSearchPage(
                records=(
                    MailRecord(
                        source_id="mail-1",
                        subject="SCHIE Mail Thread",
                        sender="owner@example.com",
                        recipients=("team@example.com",),
                        received_at=None,
                        web_url="https://outlook.office.com/mail/mail-1",
                        preview="Message only payload with no durable thread id.",
                    ),
                ),
                next_cursor=None,
                source="workiq",
            )
        }
    )
    discovery = WorkIQMailDiscovery(mail_client=client)

    candidates = discovery.discover_candidates("SCHIE Mail Thread", limit=5)

    assert candidates == ()

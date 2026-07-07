"""Direct coverage for the extracted weekly summary card cluster.

Guards the D-25 / Phase 3 extraction from ``src/commands/confirm.py`` into
``src/commands/confirm_stages/weekly_summary_card.py``. Validates request
gating and the failure-isolation contract of the post helper (rendering or
webhook failures degrade to warnings, never raise).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from src.commands.confirm_stages import weekly_summary_card
from src.commands.confirm_stages.weekly_summary_card import (
    build_confirm_weekly_summary_teams_sender,
    post_confirm_weekly_summary_card,
    validate_weekly_summary_card_request,
)


def _bundle(*, cadence: str = "weekly", edition_type: str = "detailed", webhook: str | None = "https://hook"):
    return SimpleNamespace(
        config=SimpleNamespace(
            edition=SimpleNamespace(cadence=cadence, type=edition_type),
            m365=SimpleNamespace(teams_incoming_webhook_url=webhook),
        )
    )


def test_validate_request_accepts_weekly_newsletter() -> None:
    # Should not raise.
    validate_weekly_summary_card_request(bundle=_bundle(), draft_state={})


@pytest.mark.parametrize(
    "draft_type",
    ["deck", "lookback"],
)
def test_validate_request_rejects_deck_and_lookback(draft_type: str) -> None:
    with pytest.raises(typer.BadParameter):
        validate_weekly_summary_card_request(bundle=_bundle(), draft_state={"edition_type": draft_type})


def test_validate_request_rejects_non_weekly_cadence() -> None:
    with pytest.raises(typer.BadParameter):
        validate_weekly_summary_card_request(bundle=_bundle(cadence="monthly"), draft_state={})


def test_validate_request_requires_webhook() -> None:
    with pytest.raises(typer.BadParameter):
        validate_weekly_summary_card_request(bundle=_bundle(webhook=None), draft_state={})


def test_post_card_render_failure_degrades_to_warning(monkeypatch) -> None:
    def _boom(**_kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(weekly_summary_card, "write_confirm_weekly_summary_card", _boom)
    card_path, posted, warning = post_confirm_weekly_summary_card(
        bundle=_bundle(),
        edition_name="acme_weekly",
        issue_number=1,
        report=SimpleNamespace(items=()),
        archive_paths=SimpleNamespace(),
        webhook_url="https://hook",
    )
    assert card_path is None and posted is False
    assert warning is not None and "skipped" in warning
    fake_path = Path("/tmp/card.json")
    monkeypatch.setattr(
        weekly_summary_card,
        "write_confirm_weekly_summary_card",
        lambda **_kwargs: (fake_path, {"payload": True}),
    )

    def _failing_sender(_webhook_url):
        def _sender(_payload):
            raise RuntimeError("webhook down")

        return _sender

    monkeypatch.setattr(weekly_summary_card, "build_confirm_weekly_summary_teams_sender", _failing_sender)
    card_path, posted, warning = post_confirm_weekly_summary_card(
        bundle=_bundle(),
        edition_name="acme_weekly",
        issue_number=1,
        report=SimpleNamespace(items=()),
        archive_paths=SimpleNamespace(),
        webhook_url="https://hook",
    )
    assert card_path == fake_path and posted is False
    assert warning is not None and "not posted" in warning


def test_post_card_success(monkeypatch) -> None:
    fake_path = Path("/tmp/card.json")
    sent: list[dict] = []
    monkeypatch.setattr(
        weekly_summary_card,
        "write_confirm_weekly_summary_card",
        lambda **_kwargs: (fake_path, {"payload": True}),
    )
    monkeypatch.setattr(
        weekly_summary_card,
        "build_confirm_weekly_summary_teams_sender",
        lambda _webhook_url: sent.append,
    )
    card_path, posted, warning = post_confirm_weekly_summary_card(
        bundle=_bundle(),
        edition_name="acme_weekly",
        issue_number=1,
        report=SimpleNamespace(items=()),
        archive_paths=SimpleNamespace(),
        webhook_url="https://hook",
    )
    assert card_path == fake_path and posted is True and warning is None
    assert sent == [{"payload": True}]


def test_build_teams_sender_posts_card(monkeypatch) -> None:
    posted: list[dict] = []

    class _FakeClient:
        def __init__(self, *, webhook_url):
            self.webhook_url = webhook_url

        def post_card(self, payload):
            posted.append(payload)

    monkeypatch.setattr(weekly_summary_card, "TeamsWebhookClient", _FakeClient)
    sender = build_confirm_weekly_summary_teams_sender("https://hook")
    sender({"a": 1})
    assert posted == [{"a": 1}]

"""Unit tests for report_lookback.py's AI retrospective synthesis wiring
(specs/backlog.md WO-4/BL-C2): the feature-name/registry-prompt mismatch
the AI call-site inventory found -- it borrowed exec_summary_drafter's
policy while using its own, unregistered inline prompt. Fixed by giving it
its own registered feature (lookback_retrospective) routed through
route_through_tiers, with the prompt moved into the registry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from src.commands.report_lookback import _build_lookback_ai_retrospective_rows
from src.core.models import EditionType, Snapshot
from src.core.view_models import RetrospectiveIntelligenceRow, RetrospectiveIntelligenceSummary


class _FakeClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[tuple[str, str, int, str | None]] = []

    def chat(self, system: str, user: str, **kwargs: Any) -> str:
        return ""

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], Any],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> Any:
        self.calls.append((system, user, max_tokens, prompt_version))
        return parser(self._response)


def _snapshot(issue_number: int) -> Snapshot:
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    return Snapshot(
        issue_number=issue_number,
        generated_at=now,
        ado_data_as_of=now,
        edition_type=EditionType.DETAILED,
        items=(),
        scorecards=(),
    )


def _summary(*, rows: tuple[RetrospectiveIntelligenceRow, ...]) -> RetrospectiveIntelligenceSummary:
    return RetrospectiveIntelligenceSummary(
        chronic_workstream_count=1,
        recovered_workstream_count=0,
        recurring_drift_count=0,
        worsened_workstream_count=0,
        improved_workstream_count=0,
        claim_accuracy_signal_count=0,
        charter_evaluation_signal_count=0,
        rows=rows,
    )


def test_uses_the_registered_prompt_not_an_inline_literal() -> None:
    client = _FakeClient({
        "insights": [{"category": "AI synthesis", "title": "Pattern found", "detail": "Detail text here."}],
    })
    retrospective = _summary(rows=(RetrospectiveIntelligenceRow(category="chronic", title="WS-A", detail="stuck"),))
    snapshots = (_snapshot(1), _snapshot(2))

    result = _build_lookback_ai_retrospective_rows(
        client=client,
        retrospective_intelligence=retrospective,
        snapshots=snapshots,
    )

    assert len(client.calls) == 1
    system_prompt, _user, max_tokens, prompt_version = client.calls[0]
    assert prompt_version == "lookback_retrospective.v1"
    assert max_tokens == 700
    assert "synthesizing a TPM retrospective" in system_prompt
    assert len(result) == 1
    assert result[0].title == "Pattern found"


def test_empty_when_no_retrospective_rows() -> None:
    client = _FakeClient({"insights": []})
    retrospective = _summary(rows=())

    result = _build_lookback_ai_retrospective_rows(
        client=client,
        retrospective_intelligence=retrospective,
        snapshots=(_snapshot(1),),
    )

    assert result == ()
    assert client.calls == []

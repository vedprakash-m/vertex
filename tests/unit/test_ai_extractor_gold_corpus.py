from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.ai.claim_extractor import ClaimExtractor, _extract_deterministic_claims
from src.ai.action_extractor import ActionExtractor, _extract_deterministic_actions
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import Signal

_ROOT = Path(__file__).parents[2]
_CLAIMS_DIR = _ROOT / "programs" / "acme" / "gold_corpus" / "claims"
_ACTIONS_DIR = _ROOT / "programs" / "acme" / "gold_corpus" / "actions"

pytestmark = pytest.mark.skipif(
    not _CLAIMS_DIR.exists() and not _ACTIONS_DIR.exists(),
    reason="Requires local gold corpus data under programs/acme/gold_corpus/",
)


class _FailingClient:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, *_args, **_kwargs) -> str:
        self.calls += 1
        raise AssertionError("AI client should not be called for deterministic gold corpus cases")

    def structured(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("AI client should not be called for deterministic gold corpus cases")


@pytest.mark.parametrize("path", sorted(_CLAIMS_DIR.glob("*.yaml")) if _CLAIMS_DIR.exists() else [])
def test_claim_gold_corpus_cases(path: Path) -> None:
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(case, dict)
    input_payload = case["input"]
    expected = case["expected_output"]
    assert isinstance(input_payload, dict)
    assert isinstance(expected, dict)
    items = tuple(_sample_item(item_id) for item_id in input_payload.get("items", []))

    deterministic_result, confidence = _extract_deterministic_claims(
        program_id=input_payload["program_id"],
        edition_id=input_payload["edition_id"],
        issue_number=int(input_payload["issue_number"]),
        claim_date=_coerce_date(input_payload["claim_date"]),
        narratives=dict(input_payload["narratives"]),
        items=items,
        valid_workstream_ids=tuple(input_payload.get("valid_workstream_ids", [])),
        workstream_area_paths=_coerce_workstream_area_paths(input_payload.get("workstream_area_paths")),
    )

    assert deterministic_result is not None
    assert confidence >= float(case["expected_deterministic_confidence"])
    assert [(claim.text, claim.owner_alias, claim.due_date.isoformat() if claim.due_date else None, claim.workstream_id, list(claim.entity_refs)) for claim in deterministic_result.claims] == [
        (
            claim["text"],
            claim.get("owner_alias"),
            _coerce_optional_date(claim.get("due_date")),
            claim.get("workstream_id"),
            claim.get("entity_refs", []),
        )
        for claim in expected.get("claims", [])
    ]
    assert [(ask.text, ask.owner_alias, list(ask.entity_refs)) for ask in deterministic_result.decision_asks] == [
        (
            ask["text"],
            ask.get("owner_alias"),
            ask.get("entity_refs", []),
        )
        for ask in expected.get("decision_asks", [])
    ]

    client = _FailingClient()
    result = ClaimExtractor(client=client).extract_claims(
        program_id=input_payload["program_id"],
        edition_id=input_payload["edition_id"],
        issue_number=int(input_payload["issue_number"]),
        claim_date=_coerce_date(input_payload["claim_date"]),
        narratives=dict(input_payload["narratives"]),
        items=items,
        valid_workstream_ids=tuple(input_payload.get("valid_workstream_ids", [])),
        workstream_area_paths=_coerce_workstream_area_paths(input_payload.get("workstream_area_paths")),
    )
    assert client.calls == 0
    assert result == deterministic_result


@pytest.mark.parametrize("path", sorted(_ACTIONS_DIR.glob("*.yaml")) if _ACTIONS_DIR.exists() else [])
def test_action_gold_corpus_cases(path: Path) -> None:
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(case, dict)
    input_payload = case["input"]
    assert isinstance(input_payload, dict)
    signals = tuple(_signal_from_case(signal) for signal in input_payload["signals"])

    deterministic_actions, confidence = _extract_deterministic_actions(
        program_id=input_payload["program_id"],
        signals=signals,
    )

    assert deterministic_actions is not None
    assert confidence >= float(case["expected_deterministic_confidence"])
    assert [
        (
            action.text,
            action.owner_alias,
            action.due_date.isoformat() if action.due_date else None,
            list(action.linked_work_item_ids),
            action.workstream_id,
        )
        for action in deterministic_actions
    ] == [
        (
            action["text"],
            action["owner_alias"],
            _coerce_optional_date(action.get("due_date")),
            action.get("linked_work_item_ids", []),
            action.get("workstream_id"),
        )
        for action in case["expected_output"]["actions"]
    ]

    client = _FailingClient()
    actions = ActionExtractor(client=client).extract_actions(
        program_id=input_payload["program_id"],
        signals=signals,
    )
    assert client.calls == 0
    assert actions == deterministic_actions


def _sample_item(item_id: int) -> WorkItem:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    return WorkItem(
        id=item_id,
        type="Feature",
        title=f"Item {item_id}",
        state="Active",
        assigned_to="owner@example.com",
        assigned_to_email="owner@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Sprint 1",
        target_date=date(2026, 6, 15),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={"changed_date": as_of.isoformat()},
        revisions=[],
        comments=[],
        fetched_at=as_of,
    )


def _signal_from_case(payload: dict[str, object]) -> Signal:
    metadata = {"sender_alias": payload.get("sender_alias")} if payload.get("sender_alias") is not None else None
    return Signal(
        id=str(payload["id"]),
        timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        source=str(payload["source"]),
        program_id=str(payload["program_id"]),
        workstream_id=str(payload["workstream_id"]) if payload.get("workstream_id") is not None else None,
        entity_refs=tuple(str(ref) for ref in _coerce_refs(payload.get("entity_refs"))),
        text=str(payload["text"]),
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata=metadata,
        thread_id=None,
    )


def _coerce_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _coerce_optional_date(value: object) -> str | None:
    if value is None:
        return None
    return _coerce_date(value).isoformat()


def _coerce_refs(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    return []


def _coerce_workstream_area_paths(value: object) -> dict[str, tuple[str, ...]] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, tuple[str, ...]] = {}
    for workstream_id, area_paths in value.items():
        if not isinstance(workstream_id, str):
            continue
        if isinstance(area_paths, list):
            result[workstream_id] = tuple(str(area_path) for area_path in area_paths)
    return result or None

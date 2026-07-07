from __future__ import annotations

from datetime import date

import pytest

from src.ai.ai_mode import AIMode, set_ai_mode
from src.ai.claim_extractor import PROMPT_VERSION, ClaimExtractor, ClaimExtractorError
from src.core.models_v2 import AIConfig, Program
from src.core.models import RiskLevel, WorkItem


class _FakeAIClient:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls = 0
        self.last_prompt_version: str | None = None
        self.last_user: str | None = None

    def chat(self, system: str, user: str, *, max_tokens: int = 800, prompt_version: str | None = None) -> str:
        del system, user, max_tokens, prompt_version
        self.calls += 1
        raise AssertionError("chat should not be called in claim extractor tests")

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
        del system, max_tokens
        self.calls += 1
        self.last_prompt_version = prompt_version
        self.last_user = user
        return parser(self.payload)


def test_claim_extractor_prefers_deterministic_claim_patterns_before_ai() -> None:
    client = _FakeAIClient(
        {
            "claims": [
                {
                    "text": "WI:1001 Deployment readiness expected by 2026-06-15",
                    "entity_refs": ["WI:1001"],
                    "due_date": "2026-06-15",
                    "owner_alias": "Owner@example.com",
                    "workstream_id": "deployment_readiness",
                }
            ],
            "decision_asks": [
                {
                    "text": "WI:1001 Need LT decision on SCHIE timeline",
                    "entity_refs": ["WI:1001"],
                    "owner_alias": "lt",
                }
            ],
        }
    )

    result = ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={"exec_summary.md": "WI:1001 Deployment readiness expected by June 15. Need LT decision on SCHIE timeline."},
        items=(_sample_item(1001),),
        valid_workstream_ids=("deployment_readiness",),
    )

    assert len(result.claims) == 1
    assert result.claims[0].text == "WI:1001 Deployment readiness expected by June 15"
    assert result.claims[0].entity_refs == ("WI:1001",)
    assert result.claims[0].due_date is not None and result.claims[0].due_date.isoformat() == "2026-06-15"
    assert result.claims[0].owner_alias == "owner"
    assert result.claims[0].workstream_id is None
    assert len(result.decision_asks) == 1
    assert result.decision_asks[0].text == "Need LT decision on SCHIE timeline"
    assert result.decision_asks[0].owner_alias is None
    assert client.calls == 0

def test_claim_extractor_supports_hyphenated_follow_up_claims_without_calling_ai() -> None:
    client = _FakeAIClient(
        {
            "claims": [
                {
                    "text": "should not run",
                    "entity_refs": [],
                    "due_date": None,
                    "owner_alias": None,
                    "workstream_id": None,
                }
            ],
            "decision_asks": [],
        }
    )

    result = ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={"exec_summary.md": "WI:1001 rollout follow-up by May 20."},
        items=(_sample_item(1001),),
    )

    assert len(result.claims) == 1
    assert result.claims[0].text == "WI:1001 rollout follow-up by May 20"
    assert result.claims[0].due_date is not None and result.claims[0].due_date.isoformat() == "2026-05-20"
    assert client.calls == 0


def test_claim_extractor_preserves_explicit_at_alias_owner_without_calling_ai() -> None:
    client = _FakeAIClient(
        {
            "claims": [
                {
                    "text": "should not run",
                    "entity_refs": [],
                    "due_date": None,
                    "owner_alias": None,
                    "workstream_id": None,
                }
            ],
            "decision_asks": [],
        }
    )

    result = ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={"exec_summary.md": "@priya will deliver WI:1001 by June 15."},
        items=(_sample_item(1001),),
    )

    assert len(result.claims) == 1
    assert result.claims[0].owner_alias == "priya"
    assert client.calls == 0


def test_claim_extractor_preserves_plain_owner_phrase_without_calling_ai() -> None:
    client = _FakeAIClient(
        {
            "claims": [
                {
                    "text": "should not run",
                    "entity_refs": [],
                    "due_date": None,
                    "owner_alias": None,
                    "workstream_id": None,
                }
            ],
            "decision_asks": [],
        }
    )

    result = ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={"exec_summary.md": "Owner Priya will deliver WI:1001 by June 15."},
        items=(_sample_item(1001),),
    )

    assert len(result.claims) == 1
    assert result.claims[0].owner_alias == "priya"
    assert client.calls == 0


def test_claim_extractor_preserves_owner_colon_phrase_without_calling_ai() -> None:
    client = _FakeAIClient(
        {
            "claims": [
                {
                    "text": "should not run",
                    "entity_refs": [],
                    "due_date": None,
                    "owner_alias": None,
                    "workstream_id": None,
                }
            ],
            "decision_asks": [],
        }
    )

    result = ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={"exec_summary.md": "Owner: Priya will deliver WI:1001 by June 15."},
        items=(_sample_item(1001),),
    )

    assert len(result.claims) == 1
    assert result.claims[0].owner_alias == "priya"
    assert client.calls == 0
    assert client.last_prompt_version is None
    assert client.last_user is None


def test_claim_extractor_rejects_entity_refs_outside_allowed_item_set() -> None:
    client = _FakeAIClient(
        {
            "claims": [
                {
                    "text": "WI:9999 Deployment readiness expected by 2026-06-15",
                    "entity_refs": ["WI:9999"],
                    "due_date": "2026-06-15",
                    "owner_alias": "owner",
                    "workstream_id": None,
                }
            ],
            "decision_asks": [],
        }
    )

    with pytest.raises(ClaimExtractorError, match="outside the allowed set"):
        ClaimExtractor(client=client).extract_claims(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            claim_date=date(2026, 5, 10),
            narratives={"exec_summary.md": "Narrative text."},
            items=(_sample_item(1001),),
        )


def test_claim_extractor_uses_grounded_citations_when_entity_refs_missing() -> None:
    client = _FakeAIClient(
        {
            "claims": [
                {
                    "text": "WI:1001 Deployment readiness expected by 2026-06-15",
                    "entity_refs": [],
                    "due_date": "2026-06-15",
                    "owner_alias": None,
                    "workstream_id": None,
                }
            ],
            "decision_asks": [],
        }
    )

    result = ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={"exec_summary.md": "WI:1001 Deployment readiness expected by June 15."},
        items=(_sample_item(1001),),
    )

    assert result.claims[0].entity_refs == ("WI:1001",)


def test_claim_extractor_rejects_ai_workstream_id_mismatched_area_path() -> None:
    client = _FakeAIClient(
        {
            "claims": [
                {
                    "text": "WI:1001 Deployment readiness expected by 2026-06-15",
                    "entity_refs": ["WI:1001"],
                    "due_date": "2026-06-15",
                    "owner_alias": "owner",
                    "workstream_id": "platform_ops",
                }
            ],
            "decision_asks": [],
        }
    )

    with pytest.raises(ClaimExtractorError, match="does not match configured area paths"):
        ClaimExtractor(client=client).extract_claims(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            claim_date=date(2026, 5, 10),
            narratives={"exec_summary.md": "Narrative text."},
            items=(_sample_item(1001),),
            valid_workstream_ids=("deployment_readiness", "platform_ops"),
            workstream_area_paths={
                "deployment_readiness": (r"One\Adventure\Acme",),
                "platform_ops": (r"One\Adventure\Platform",),
            },
        )

    assert client.last_user is not None and "Allowed workstream area paths:" in client.last_user


def test_claim_extractor_uses_existing_regex_patterns_without_calling_ai() -> None:
    client = _FakeAIClient(
        {
            "claims": [{"text": "should not run", "entity_refs": [], "due_date": None, "owner_alias": None, "workstream_id": None}],
            "decision_asks": [],
        }
    )

    result = ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={
            "exec_summary.md": "WI:1001 UD chunking fix expected by June 15. Need LT decision on SCHIE timeline."
        },
        items=(_sample_item(1001),),
        valid_workstream_ids=("deployment_readiness",),
    )

    assert tuple(entry.text for entry in result.claims) == ("WI:1001 UD chunking fix expected by June 15",)
    assert result.claims[0].due_date == date(2026, 6, 15)
    assert tuple(entry.text for entry in result.decision_asks) == ("Need LT decision on SCHIE timeline",)
    assert client.calls == 0


def test_claim_extractor_uses_area_path_evidence_for_regex_workstream_inference_without_calling_ai() -> None:
    client = _FakeAIClient(
        {
            "claims": [
                {
                    "text": "should not run",
                    "entity_refs": [],
                    "due_date": None,
                    "owner_alias": None,
                    "workstream_id": None,
                }
            ],
            "decision_asks": [],
        }
    )

    result = ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={"exec_summary.md": "WI:1001 Deployment readiness expected by June 15."},
        items=(_sample_item(1001),),
        valid_workstream_ids=("deployment_readiness",),
        workstream_area_paths={"deployment_readiness": (r"One\Adventure\Acme",)},
    )

    assert len(result.claims) == 1
    assert result.claims[0].workstream_id == "deployment_readiness"
    assert client.calls == 0


def test_claim_extractor_rejects_non_object_payload() -> None:
    client = _FakeAIClient([])

    with pytest.raises(ClaimExtractorError, match="AI claim payload must be an object"):
        ClaimExtractor(client=client).extract_claims(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            claim_date=date(2026, 5, 10),
            narratives={"exec_summary.md": "Narrative text."},
            items=(_sample_item(1001),),
        )        


def test_claim_extractor_returns_empty_result_when_invocation_ai_disabled() -> None:
    client = _FakeAIClient(
        {
            "claims": [{"text": "should not run", "entity_refs": [], "due_date": None, "owner_alias": None, "workstream_id": None}],
            "decision_asks": [],
        }
    )
    set_ai_mode(AIMode.DISABLED)
    try:
        result = ClaimExtractor(client=client).extract_claims(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            claim_date=date(2026, 5, 10),
            narratives={"exec_summary.md": "Narrative text."},
            items=(_sample_item(1001),),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert result.claims == ()
    assert result.decision_asks == ()
    assert result.warnings == ()
    assert client.calls == 0


def test_claim_extractor_uses_deterministic_canonical_claims_when_invocation_ai_disabled() -> None:
    client = _FakeAIClient(
        {
            "claims": [{"text": "should not run", "entity_refs": [], "due_date": None, "owner_alias": None, "workstream_id": None}],
            "decision_asks": [],
        }
    )
    set_ai_mode(AIMode.DISABLED)
    try:
        result = ClaimExtractor(client=client).extract_claims(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            claim_date=date(2026, 5, 10),
            narratives={
                "exec_summary.md": (
                    "Claim: Deployment readiness will finish by 2026-06-15 | owner=priya | due=2026-06-15 | "
                    "workstream=deployment_readiness | refs=WI:1001\n"
                    "Decision ask: Approve the mitigation plan | owner=lt | refs=WI:1001"
                )
            },
            items=(_sample_item(1001),),
            valid_workstream_ids=("deployment_readiness",),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert len(result.claims) == 1
    assert result.claims[0].text == "Deployment readiness will finish by 2026-06-15"
    assert result.claims[0].owner_alias == "priya"
    assert result.claims[0].due_date is not None and result.claims[0].due_date.isoformat() == "2026-06-15"
    assert result.claims[0].workstream_id == "deployment_readiness"
    assert result.claims[0].entity_refs == ("WI:1001",)
    assert len(result.decision_asks) == 1
    assert result.decision_asks[0].text == "Approve the mitigation plan"
    assert result.decision_asks[0].owner_alias == "lt"
    assert result.decision_asks[0].entity_refs == ("WI:1001",)
    assert client.calls == 0


def test_claim_extractor_uses_existing_regex_patterns_when_invocation_ai_disabled() -> None:
    client = _FakeAIClient(
        {
            "claims": [{"text": "should not run", "entity_refs": [], "due_date": None, "owner_alias": None, "workstream_id": None}],
            "decision_asks": [],
        }
    )
    set_ai_mode(AIMode.DISABLED)
    try:
        result = ClaimExtractor(client=client).extract_claims(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            claim_date=date(2026, 5, 10),
            narratives={
                "exec_summary.md": "WI:1001 UD chunking fix expected by June 15. Need LT decision on SCHIE timeline."
            },
            items=(_sample_item(1001),),
            valid_workstream_ids=("deployment_readiness",),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert tuple(entry.text for entry in result.claims) == ("WI:1001 UD chunking fix expected by June 15",)
    assert tuple(entry.text for entry in result.decision_asks) == ("Need LT decision on SCHIE timeline",)
    assert client.calls == 0


def test_claim_extractor_from_program_does_not_require_env_when_invocation_ai_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("VERTEX_AI_DEPLOYMENT", raising=False)
    set_ai_mode(AIMode.DISABLED)
    try:
        extractor = ClaimExtractor.from_program(
            Program(
                schema_version="2.0",
                id="acme",
                name="Acme",
                ai=AIConfig(enabled=True, budget_usd_per_run=0.25),
            )
        )
        result = extractor.extract_claims(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            claim_date=date(2026, 5, 10),
            narratives={"exec_summary.md": "Narrative text."},
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert result.claims == ()
    assert result.decision_asks == ()
    assert result.warnings == ()


def test_claim_extractor_from_program_uses_deterministic_claims_when_invocation_ai_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("VERTEX_AI_DEPLOYMENT", raising=False)
    set_ai_mode(AIMode.DISABLED)
    try:
        extractor = ClaimExtractor.from_program(
            Program(
                schema_version="2.0",
                id="acme",
                name="Acme",
                ai=AIConfig(enabled=True, budget_usd_per_run=0.25),
            )
        )
        result = extractor.extract_claims(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            claim_date=date(2026, 5, 10),
            narratives={
                "exec_summary.md": (
                    "Claim: Deployment readiness will finish by 2026-06-15 | owner=priya | due=2026-06-15 | "
                    "workstream=deployment_readiness | refs=WI:1001"
                )
            },
            items=(_sample_item(1001),),
            valid_workstream_ids=("deployment_readiness",),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert len(result.claims) == 1
    assert result.claims[0].text == "Deployment readiness will finish by 2026-06-15"
    assert result.claims[0].owner_alias == "priya"


def _sample_item(item_id: int) -> WorkItem:
    from datetime import datetime, timezone

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

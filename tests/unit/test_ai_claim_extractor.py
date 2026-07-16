from __future__ import annotations

from datetime import date
from pathlib import Path

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


def test_claim_extractor_rejects_entity_refs_outside_allowed_item_set(tmp_path: Path) -> None:
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
            programs_root=tmp_path,
        )


def test_claim_extractor_uses_grounded_citations_when_entity_refs_missing(tmp_path: Path) -> None:
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
        programs_root=tmp_path,
    )

    assert result.claims[0].entity_refs == ("WI:1001",)


def test_claim_extractor_rejects_ai_workstream_id_mismatched_area_path(tmp_path: Path) -> None:
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
            programs_root=tmp_path,
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


def test_claim_extractor_rejects_non_object_payload(tmp_path: Path) -> None:
    client = _FakeAIClient([])

    with pytest.raises(ClaimExtractorError, match="AI claim payload must be an object"):
        ClaimExtractor(client=client).extract_claims(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            claim_date=date(2026, 5, 10),
            narratives={"exec_summary.md": "Narrative text."},
            items=(_sample_item(1001),),
            programs_root=tmp_path,
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


def test_claim_extractor_parses_deterministic_dependency_status_marker_when_invocation_ai_disabled() -> None:
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
                    "Claim: Dependency on Team Rome is now broken | refs=DEP:dep-rome | "
                    "status_family=dependency | status_value=broken"
                )
            },
            items=(_sample_item(1001),),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert len(result.claims) == 1
    claim = result.claims[0]
    assert claim.entity_refs == ("DEP:DEP-ROME",)
    assert claim.claimed_status_family == "dependency"
    assert claim.claimed_status_value == "broken"
    assert client.calls == 0


def test_claim_extractor_falls_back_when_deterministic_marker_has_unknown_status_family() -> None:
    """An invalid `status_family` value makes `_parse_deterministic_claim_line`
    raise internally, but that's caught and treated as "this line isn't a
    valid deterministic marker" (returns None), which aborts deterministic
    extraction for the whole narrative set and falls through to the
    regex-based path -- not a raised ClaimExtractorError bubbling out of
    extract_claims. With AI disabled and no regex pattern matching this
    text, the result is simply no claims extracted, not an error."""
    client = _FakeAIClient({"claims": [], "decision_asks": []})
    set_ai_mode(AIMode.DISABLED)
    try:
        result = ClaimExtractor(client=client).extract_claims(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            claim_date=date(2026, 5, 10),
            narratives={
                "exec_summary.md": (
                    "Claim: Something happened | refs=DEP:dep-rome | status_family=not_a_family | status_value=broken"
                )
            },
            items=(_sample_item(1001),),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert result.claims == ()


def test_claim_extractor_parses_ai_path_optional_dependency_status_fields(tmp_path: Path) -> None:
    client = _FakeAIClient(
        {
            "claims": [
                {
                    "text": "The dependency on Team Rome is now resolved.",
                    "entity_refs": ["DEP:dep-rome"],
                    "due_date": None,
                    "owner_alias": None,
                    "workstream_id": None,
                    "claimed_status_family": "dependency",
                    "claimed_status_value": "resolved",
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
        narratives={"exec_summary.md": "The dependency on Team Rome is now resolved."},
        items=(_sample_item(1001),),
        programs_root=tmp_path,
    )

    assert len(result.claims) == 1
    claim = result.claims[0]
    assert claim.entity_refs == ("DEP:DEP-ROME",)
    assert claim.claimed_status_family == "dependency"
    assert claim.claimed_status_value == "resolved"


def test_claim_extractor_parses_ai_path_risk_milestone_action_status_refs(tmp_path: Path) -> None:
    """ADF-W2.10 P7 (Section 8.10.9): RISK:/MS:/ACTION: refs are accepted as
    opaque passthrough ids (mirroring DEP:), and risk/milestone/action status
    families are accepted. Cross-validation happens at comparison time, not
    extraction time."""
    client = _FakeAIClient(
        {
            "claims": [
                {
                    "text": "Risk R-1 is mitigated.",
                    "entity_refs": ["RISK:r-1"],
                    "due_date": None,
                    "owner_alias": None,
                    "workstream_id": None,
                    "claimed_status_family": "risk",
                    "claimed_status_value": "mitigated",
                },
                {
                    "text": "Milestone M-1 is at risk.",
                    "entity_refs": ["MS:m-1"],
                    "due_date": None,
                    "owner_alias": None,
                    "workstream_id": None,
                    "claimed_status_family": "milestone",
                    "claimed_status_value": "at_risk",
                },
                {
                    "text": "Action A-1 is done.",
                    "entity_refs": ["ACTION:a-1"],
                    "due_date": None,
                    "owner_alias": None,
                    "workstream_id": None,
                    "claimed_status_family": "action",
                    "claimed_status_value": "done",
                },
            ],
            "decision_asks": [],
        }
    )

    result = ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={"exec_summary.md": "Risk, milestone, and action status notes."},
        items=(_sample_item(1001),),
        programs_root=tmp_path,
    )

    assert len(result.claims) == 3
    by_family = {claim.claimed_status_family: claim for claim in result.claims}
    assert by_family["risk"].entity_refs == ("RISK:R-1",)
    assert by_family["risk"].claimed_status_value == "mitigated"
    assert by_family["milestone"].entity_refs == ("MS:M-1",)
    assert by_family["milestone"].claimed_status_value == "at_risk"
    assert by_family["action"].entity_refs == ("ACTION:A-1",)
    assert by_family["action"].claimed_status_value == "done"


def test_claim_extractor_ai_path_leaves_status_fields_null_when_absent(tmp_path: Path) -> None:
    """Existing callers whose AI JSON omits the new optional fields entirely
    (pre-P6 fixtures, golden corpus) must still parse -- these keys are
    optional, not required."""
    client = _FakeAIClient(
        {
            "claims": [
                {
                    "text": "WI:1001 lands on schedule.",
                    "entity_refs": ["WI:1001"],
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
        narratives={"exec_summary.md": "WI:1001 lands on schedule."},
        items=(_sample_item(1001),),
        programs_root=tmp_path,
    )

    assert len(result.claims) == 1
    assert result.claims[0].claimed_status_family is None
    assert result.claims[0].claimed_status_value is None


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


def _ai_claim_payload() -> dict:
    return {
        "claims": [
            {
                "text": "WI:1001 Deployment readiness expected by 2026-06-15",
                "entity_refs": ["WI:1001"],
                "due_date": "2026-06-15",
                "owner_alias": "owner",
                "workstream_id": None,
            }
        ],
        "decision_asks": [],
    }


def test_claim_extractor_records_released_terminal_on_success(tmp_path: Path) -> None:
    # ADF-W5.1/P7: claim_extractor's frontier-tier AISchemaGateway migration
    # must record a durable QG-29 "released" terminal for a successful
    # frontier-path extraction, same as risk_proposal_generator's
    # release-audit contract -- scoped only to genuine AI calls (a
    # deterministic-tier hit never touches AISchemaGateway at all, so it
    # records no lifecycle/audit trail).
    from src.core.ledger.event_log import read_events

    client = _FakeAIClient(_ai_claim_payload())

    result = ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={"exec_summary.md": "Narrative text."},
        items=(_sample_item(1001),),
        programs_root=tmp_path,
    )

    assert len(result.claims) == 1
    events = read_events("acme", programs_root=tmp_path)
    release_decisions = [event for event in events if event.event_type == "ai.release_decision.v1"]
    assert release_decisions
    assert release_decisions[-1].payload["terminal"] == "released"


def test_claim_extractor_repeat_identical_frontier_request_hits_the_cache(tmp_path: Path) -> None:
    # ADF-W5.1/P7: identical program/narratives/items should be served from
    # the AI result cache on the second frontier-path call rather than
    # invoking the provider again.
    client = _FakeAIClient(_ai_claim_payload())
    narratives = {"exec_summary.md": "Narrative text."}
    items = (_sample_item(1001),)

    first = ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives=narratives,
        items=items,
        programs_root=tmp_path,
    )
    second = ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives=narratives,
        items=items,
        programs_root=tmp_path,
    )

    assert client.calls == 1
    assert len(first.claims) == 1 and len(second.claims) == 1
    assert first.claims[0].text == second.claims[0].text


def test_claim_extractor_different_narratives_do_not_hit_the_cache(tmp_path: Path) -> None:
    client = _FakeAIClient(_ai_claim_payload())
    items = (_sample_item(1001),)

    ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={"exec_summary.md": "Narrative text."},
        items=items,
        programs_root=tmp_path,
    )
    ClaimExtractor(client=client).extract_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={"exec_summary.md": "A totally different narrative text."},
        items=items,
        programs_root=tmp_path,
    )

    assert client.calls == 2


def test_claim_extractor_oversized_frontier_request_is_discarded_before_calling_the_provider(tmp_path: Path) -> None:
    # ADF-W5.1/P7: AISchemaGateway bounds must reject an oversized outbound
    # request payload before ever invoking the frontier provider.
    client = _FakeAIClient({"claims": [], "decision_asks": []})

    with pytest.raises(ClaimExtractorError, match="AISchemaGateway rejected the outbound request"):
        ClaimExtractor(client=client).extract_claims(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            claim_date=date(2026, 5, 10),
            narratives={"exec_summary.md": "x" * 200_001},
            items=(_sample_item(1001),),
            programs_root=tmp_path,
        )

    assert client.calls == 0

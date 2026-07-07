from __future__ import annotations

from datetime import date, datetime, timezone
import json

import pytest

from src.core.claim_tracker import (
    assess_claim_entries,
    append_claim_entry,
    append_claim_status_update,
    append_decision_ask,
    claim_log_checksum_matches,
    extract_claims_from_confirmed_narratives,
    get_claims_checksum_path,
    get_claims_path,
    list_claim_quarantine_paths,
    load_claim_entries,
    load_decision_asks,
    load_decision_asks,
    load_open_decision_asks,
    load_claim_status_updates,
    record_confirmed_claims,
)
from src.core.claim_extraction_calibration_store import get_claim_extraction_calibration_path, load_claim_extraction_calibration_records
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import ClaimEntry, ClaimStatusUpdate, DecisionAsk, ResurfacingPolicy
from src.core.program_fact_store import load_program_facts, project_claim_entries, project_claim_status_updates, project_decision_asks


def test_extract_claims_and_decision_asks_from_confirmed_narratives() -> None:
    result = extract_claims_from_confirmed_narratives(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={
            "exec_summary.md": "WI:1001 UD chunking fix expected by June 15. Need LT decision on SCHIE timeline.",
            "ws_deployment_readiness.md": "WI:1002 rollout follow up by 2026-06-20.",
        },
        items=(
            _sample_item(1001, target_date=date(2026, 6, 15)),
            _sample_item(1002, target_date=date(2026, 6, 20)),
        ),
        valid_workstream_ids=("deployment_readiness",),
    )

    assert tuple(entry.text for entry in result.claims) == (
        "WI:1001 UD chunking fix expected by June 15",
        "WI:1002 rollout follow up by 2026-06-20",
    )
    assert result.claims[0].due_date == date(2026, 6, 15)
    assert result.claims[1].workstream_id == "deployment_readiness"
    assert tuple(entry.text for entry in result.decision_asks) == ("Need LT decision on SCHIE timeline",)


def test_extract_claims_from_confirmed_narratives_supports_hyphenated_follow_up_claims() -> None:
    result = extract_claims_from_confirmed_narratives(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={
            "exec_summary.md": "WI:1001 rollout follow-up by May 20.",
        },
        items=(
            _sample_item(1001, target_date=date(2026, 5, 20)),
        ),
    )

    assert len(result.claims) == 1
    assert result.claims[0].text == "WI:1001 rollout follow-up by May 20"
    assert result.claims[0].due_date == date(2026, 5, 20)
    assert result.claims[0].entity_refs == ("WI:1001",)


def test_extract_claims_from_confirmed_narratives_prefers_explicit_at_alias_owner() -> None:
    result = extract_claims_from_confirmed_narratives(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={
            "exec_summary.md": "@priya will deliver WI:1001 by June 15.",
        },
        items=(
            _sample_item(1001, target_date=date(2026, 6, 15)),
        ),
    )

    assert len(result.claims) == 1
    assert result.claims[0].owner_alias == "priya"


def test_extract_claims_from_confirmed_narratives_supports_plain_owner_phrase() -> None:
    result = extract_claims_from_confirmed_narratives(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={
            "exec_summary.md": "Owner Priya will deliver WI:1001 by June 15.",
        },
        items=(
            _sample_item(1001, target_date=date(2026, 6, 15)),
        ),
    )

    assert len(result.claims) == 1
    assert result.claims[0].owner_alias == "priya"


def test_extract_claims_from_confirmed_narratives_supports_owner_colon_phrase() -> None:
    result = extract_claims_from_confirmed_narratives(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={
            "exec_summary.md": "Owner: Priya will deliver WI:1001 by June 15.",
        },
        items=(
            _sample_item(1001, target_date=date(2026, 6, 15)),
        ),
    )

    assert len(result.claims) == 1
    assert result.claims[0].owner_alias == "priya"

def test_record_confirmed_claims_skips_duplicates_and_flags_stale_claims(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    narratives = {"exec_summary.md": "WI:1001 UD chunking fix expected by June 15."}
    current_items = (_sample_item(1001, target_date=date(2026, 7, 10)),)

    first = record_confirmed_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives=narratives,
        items=current_items,
        programs_root=programs_root,
    )
    second = record_confirmed_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=78,
        claim_date=date(2026, 5, 17),
        narratives=narratives,
        items=current_items,
        programs_root=programs_root,
    )

    claims = load_claim_entries("acme", programs_root)
    decision_asks = load_decision_asks("acme", programs_root)
    assessments = assess_claim_entries(
        claims,
        items=current_items,
        as_of=datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),
    )

    assert len(first.written_claims) == 1
    assert not decision_asks
    assert len(second.written_claims) == 0
    assert any("duplicate claim candidate" in warning for warning in second.warnings)
    assert len(claims) == 1
    assert assessments[0].effective_status == "stale"
    assert assessments[0].reason is not None and "current ADO target date is 2026-07-10" in assessments[0].reason


def test_record_confirmed_claims_uses_provided_extraction_result_and_logs_calibration(tmp_path) -> None:
    programs_root = tmp_path / "programs"

    recorded = record_confirmed_claims(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        claim_date=date(2026, 5, 10),
        narratives={"exec_summary.md": "WI:1001 UD chunking fix expected by June 15."},
        items=(_sample_item(1001, target_date=date(2026, 6, 15)),),
        extraction_result=_sample_ai_claim_extraction_result(),
        extraction_mode="calibration",
        programs_root=programs_root,
    )

    claims = load_claim_entries("acme", programs_root)
    asks = load_decision_asks("acme", programs_root)
    calibration_records = load_claim_extraction_calibration_records("acme", programs_root=programs_root)

    assert len(recorded.written_claims) == 4
    assert len(recorded.written_decision_asks) == 1
    assert claims[0].program_id == "acme"
    assert claims[0].edition_id == "acme_weekly"
    assert claims[0].issue_number == 77
    assert asks[0].program_id == "acme"
    assert asks[0].issue_number == 77
    assert len(calibration_records) == 1
    assert calibration_records[0].ai_claim_count == 4
    assert calibration_records[0].regex_claim_count == 1
    assert calibration_records[0].shared_claim_count == 1
    assert calibration_records[0].ai_only_count == 3
    assert calibration_records[0].regex_only_count == 0
    assert calibration_records[0].agreement_rate == 0.25
    assert any("AI extraction found more claims than regex" in warning for warning in recorded.warnings)


def test_load_claim_extraction_calibration_records_rejects_non_string_recorded_at(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    calibration_path = get_claim_extraction_calibration_path("acme", programs_root=programs_root)
    calibration_path.parent.mkdir(parents=True, exist_ok=True)
    calibration_path.write_text(
        json.dumps(
            {
                "issue_number": 77,
                "recorded_at": 123,
                "mode": "calibration",
                "ai_claim_count": 4,
                "regex_claim_count": 1,
                "shared_claim_count": 1,
                "ai_only_count": 3,
                "regex_only_count": 0,
                "agreement_rate": 0.25,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="recorded_at must be a string"):
        load_claim_extraction_calibration_records("acme", programs_root=programs_root)


def test_load_claim_extraction_calibration_records_rejects_naive_recorded_at(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    calibration_path = get_claim_extraction_calibration_path("acme", programs_root=programs_root)
    calibration_path.parent.mkdir(parents=True, exist_ok=True)
    calibration_path.write_text(
        json.dumps(
            {
                "issue_number": 77,
                "recorded_at": "2026-05-18T09:30:00",
                "mode": "calibration",
                "ai_claim_count": 4,
                "regex_claim_count": 1,
                "shared_claim_count": 1,
                "ai_only_count": 3,
                "regex_only_count": 0,
                "agreement_rate": 0.25,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="recorded_at must include timezone information"):
        load_claim_extraction_calibration_records("acme", programs_root=programs_root)


def test_load_claim_extraction_calibration_records_rejects_non_string_mode(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    calibration_path = get_claim_extraction_calibration_path("acme", programs_root=programs_root)
    calibration_path.parent.mkdir(parents=True, exist_ok=True)
    calibration_path.write_text(
        json.dumps(
            {
                "issue_number": 77,
                "recorded_at": "2026-05-18T09:30:00+00:00",
                "mode": 123,
                "ai_claim_count": 4,
                "regex_claim_count": 1,
                "shared_claim_count": 1,
                "ai_only_count": 3,
                "regex_only_count": 0,
                "agreement_rate": 0.25,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="mode must be a string"):
        load_claim_extraction_calibration_records("acme", programs_root=programs_root)


def test_load_claim_extraction_calibration_records_rejects_numeric_string_issue_number(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    calibration_path = get_claim_extraction_calibration_path("acme", programs_root=programs_root)
    calibration_path.parent.mkdir(parents=True, exist_ok=True)
    calibration_path.write_text(
        json.dumps(
            {
                "issue_number": "77",
                "recorded_at": "2026-05-18T09:30:00+00:00",
                "mode": "calibration",
                "ai_claim_count": 4,
                "regex_claim_count": 1,
                "shared_claim_count": 1,
                "ai_only_count": 3,
                "regex_only_count": 0,
                "agreement_rate": 0.25,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="issue_number must be an integer"):
        load_claim_extraction_calibration_records("acme", programs_root=programs_root)


def test_load_claim_extraction_calibration_records_rejects_numeric_string_ai_claim_count(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    calibration_path = get_claim_extraction_calibration_path("acme", programs_root=programs_root)
    calibration_path.parent.mkdir(parents=True, exist_ok=True)
    calibration_path.write_text(
        json.dumps(
            {
                "issue_number": 77,
                "recorded_at": "2026-05-18T09:30:00+00:00",
                "mode": "calibration",
                "ai_claim_count": "4",
                "regex_claim_count": 1,
                "shared_claim_count": 1,
                "ai_only_count": 3,
                "regex_only_count": 0,
                "agreement_rate": 0.25,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="ai_claim_count must be an integer"):
        load_claim_extraction_calibration_records("acme", programs_root=programs_root)


def test_decision_ask_round_trips_lifecycle_fields(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Need LT decision on rollout path.",
            entity_refs=("WI:1001",),
            ask_date=date(2026, 5, 10),
            owner_alias="lt",
            expiry_date=date(2026, 5, 31),
            resurfacing_policy=ResurfacingPolicy(watch_days=5, nudge_days=10, escalate_days=15),
            affected_milestone_ids=("m1", "m2"),
            last_touched_at=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
        ),
        programs_root=programs_root,
    )

    asks = load_decision_asks("acme", programs_root)

    assert len(asks) == 1
    assert asks[0].expiry_date == date(2026, 5, 31)
    assert asks[0].resurfacing_policy == ResurfacingPolicy(watch_days=5, nudge_days=10, escalate_days=15)
    assert asks[0].affected_milestone_ids == ("m1", "m2")
    assert asks[0].last_touched_at == datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc)


def test_load_open_decision_asks_projects_latest_open_touch_timestamp(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    append_decision_ask(
        DecisionAsk(
            id="ask-2",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Need LT decision on launch sequencing.",
            entity_refs=("WI:1002",),
            ask_date=date(2026, 5, 10),
            owner_alias="lt",
        ),
        programs_root=programs_root,
    )
    append_claim_status_update(
        "acme",
        ClaimStatusUpdate(
            claim_id="ask-2",
            new_status="open",
            updated_at=datetime(2026, 5, 18, 9, 30, tzinfo=timezone.utc),
            updated_by="operator",
            note="Decision ask touched by follow-up draft.",
        ),
        programs_root=programs_root,
    )

    asks = load_open_decision_asks("acme", programs_root)
    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)

    assert len(asks) == 1
    assert asks[0].last_touched_at == datetime(2026, 5, 18, 9, 30, tzinfo=timezone.utc)
    assert project_decision_asks(snapshot)[0].last_touched_at == datetime(2026, 5, 18, 9, 30, tzinfo=timezone.utc)


def test_claim_tracker_dual_writes_claim_entry_and_status_update_to_fact_store(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id=None,
            text="Launch gate depends on partner readiness.",
            entity_refs=("WI:1001",),
            claim_date=date(2026, 5, 10),
            owner_alias="alex",
            due_date=date(2026, 6, 1),
        ),
        programs_root=programs_root,
    )
    append_claim_status_update(
        "acme",
        ClaimStatusUpdate(
            claim_id="claim-1",
            new_status="stale",
            updated_at=datetime(2026, 5, 18, 9, 30, tzinfo=timezone.utc),
            updated_by="operator",
            note="Need refreshed evidence.",
        ),
        programs_root=programs_root,
    )

    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)

    assert project_claim_entries(snapshot) == ()
    assert project_claim_status_updates(snapshot)[0].claim_id == "claim-1"


def test_load_claim_entries_quarantines_invalid_jsonl_and_preserves_valid_records(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        "\n".join(
            (
                '{"record_type":"claim","id":"claim-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"workstream_id":"ws","text":"Ship by June 15","entity_refs":["WI:1001"],"claim_date":"2026-05-10","owner_alias":"owner","due_date":"2026-06-15","status":"open","contradiction_status":"none","source_confidence_tier":"grounded","last_validated_date":null}',
                '{"record_type":"claim",',
                '{"record_type":"status_update","claim_id":"claim-1","new_status":"closed","updated_at":"2026-05-18T09:30:00+00:00","updated_by":"operator","note":"closed"}',
            )
        )
        + "\n",
        encoding="utf-8",
    )

    claims = load_claim_entries("acme", programs_root)
    quarantines = list_claim_quarantine_paths("acme", programs_root)
    rewritten_lines = claims_path.read_text(encoding="utf-8").splitlines()

    assert len(claims) == 1
    assert claims[0].id == "claim-1"
    assert len(quarantines) == 1
    assert quarantines[0].read_text(encoding="utf-8").count('"record_type"') == 3
    assert len(rewritten_lines) == 2
    assert claim_log_checksum_matches("acme", programs_root) is True


def test_claim_log_writes_and_tracks_checksum(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Need LT decision on rollout path.",
            entity_refs=("WI:1001",),
            ask_date=date(2026, 5, 10),
            owner_alias="lt",
        ),
        programs_root=programs_root,
    )

    checksum_path = get_claims_checksum_path("acme", programs_root)

    assert checksum_path.exists()
    assert claim_log_checksum_matches("acme", programs_root) is True


def test_load_claim_entries_rejects_numeric_string_issue_number(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"claim","id":"claim-1","program_id":"acme","edition_id":"acme_weekly","issue_number":"77","workstream_id":"ws","text":"Ship by June 15","entity_refs":["WI:1001"],"claim_date":"2026-05-10","owner_alias":"owner","due_date":"2026-06-15","status":"open","contradiction_status":"none","source_confidence_tier":"grounded","last_validated_date":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="issue_number must be an integer"):
        load_claim_entries("acme", programs_root)


def test_load_decision_asks_rejects_numeric_string_issue_number(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"decision_ask","id":"ask-1","program_id":"acme","edition_id":"acme_weekly","issue_number":"77","text":"Need LT decision on rollout path.","entity_refs":["WI:1001"],"ask_date":"2026-05-10","owner_alias":"lt","status":"open","resolution":null,"expiry_date":null,"resurfacing_policy":null,"affected_milestone_ids":[],"last_touched_at":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="issue_number must be an integer"):
        load_decision_asks("acme", programs_root)


def test_load_claim_entries_rejects_non_string_id(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"claim","id":123,"program_id":"acme","edition_id":"acme_weekly","issue_number":77,"workstream_id":"ws","text":"Ship by June 15","entity_refs":["WI:1001"],"claim_date":"2026-05-10","owner_alias":"owner","due_date":"2026-06-15","status":"open","contradiction_status":"none","source_confidence_tier":"grounded","last_validated_date":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="id must be a string"):
        load_claim_entries("acme", programs_root)


def test_load_claim_entries_rejects_non_string_workstream_id(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"claim","id":"claim-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"workstream_id":999,"text":"Ship by June 15","entity_refs":["WI:1001"],"claim_date":"2026-05-10","owner_alias":"owner","due_date":"2026-06-15","status":"open","contradiction_status":"none","source_confidence_tier":"grounded","last_validated_date":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="workstream_id must be a string"):
        load_claim_entries("acme", programs_root)


def test_load_claim_entries_rejects_non_string_entity_ref(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"claim","id":"claim-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"workstream_id":"ws","text":"Ship by June 15","entity_refs":[1001],"claim_date":"2026-05-10","owner_alias":"owner","due_date":"2026-06-15","status":"open","contradiction_status":"none","source_confidence_tier":"grounded","last_validated_date":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="entity_refs must contain strings only"):
        load_claim_entries("acme", programs_root)


def test_load_claim_entries_rejects_non_string_contradiction_status(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"claim","id":"claim-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"workstream_id":"ws","text":"Ship by June 15","entity_refs":["WI:1001"],"claim_date":"2026-05-10","owner_alias":"owner","due_date":"2026-06-15","status":"open","contradiction_status":123,"source_confidence_tier":"grounded","last_validated_date":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="contradiction_status must be a string"):
        load_claim_entries("acme", programs_root)


def test_load_claim_entries_rejects_unknown_contradiction_status(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"claim","id":"claim-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"workstream_id":"ws","text":"Ship by June 15","entity_refs":["WI:1001"],"claim_date":"2026-05-10","owner_alias":"owner","due_date":"2026-06-15","status":"open","contradiction_status":"bogus","source_confidence_tier":"grounded","last_validated_date":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported contradiction_status 'bogus'"):
        load_claim_entries("acme", programs_root)


def test_load_claim_entries_rejects_non_string_source_confidence_tier(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"claim","id":"claim-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"workstream_id":"ws","text":"Ship by June 15","entity_refs":["WI:1001"],"claim_date":"2026-05-10","owner_alias":"owner","due_date":"2026-06-15","status":"open","contradiction_status":"ok","source_confidence_tier":123,"last_validated_date":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="source_confidence_tier must be a string"):
        load_claim_entries("acme", programs_root)


def test_load_claim_entries_rejects_unknown_source_confidence_tier(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"claim","id":"claim-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"workstream_id":"ws","text":"Ship by June 15","entity_refs":["WI:1001"],"claim_date":"2026-05-10","owner_alias":"owner","due_date":"2026-06-15","status":"open","contradiction_status":"ok","source_confidence_tier":"bogus","last_validated_date":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported source_confidence_tier 'bogus'"):
        load_claim_entries("acme", programs_root)


def test_load_decision_asks_rejects_non_string_affected_milestone_id(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"decision_ask","id":"ask-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"text":"Need LT decision on rollout path.","entity_refs":["WI:1001"],"ask_date":"2026-05-10","owner_alias":"lt","status":"open","resolution":null,"expiry_date":null,"resurfacing_policy":null,"affected_milestone_ids":[1],"last_touched_at":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="affected_milestone_ids must contain strings only"):
        load_decision_asks("acme", programs_root)


def test_load_decision_asks_rejects_numeric_string_resurfacing_policy_days(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"decision_ask","id":"ask-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"text":"Need LT decision on rollout path.","entity_refs":["WI:1001"],"ask_date":"2026-05-10","owner_alias":"lt","status":"open","resolution":null,"expiry_date":null,"resurfacing_policy":{"watch_days":"7","nudge_days":14,"escalate_days":21},"affected_milestone_ids":[],"last_touched_at":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="resurfacing_policy.watch_days must be an integer"):
        load_decision_asks("acme", programs_root)


def test_load_decision_asks_rejects_non_string_status(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"decision_ask","id":"ask-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"text":"Need LT decision on rollout path.","entity_refs":["WI:1001"],"ask_date":"2026-05-10","owner_alias":"lt","status":123,"resolution":null,"expiry_date":null,"resurfacing_policy":null,"affected_milestone_ids":[],"last_touched_at":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="status must be a string"):
        load_decision_asks("acme", programs_root)


def test_load_decision_asks_rejects_unknown_status(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"decision_ask","id":"ask-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"text":"Need LT decision on rollout path.","entity_refs":["WI:1001"],"ask_date":"2026-05-10","owner_alias":"lt","status":"bogus","resolution":null,"expiry_date":null,"resurfacing_policy":null,"affected_milestone_ids":[],"last_touched_at":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported decision ask status 'bogus'"):
        load_decision_asks("acme", programs_root)


def test_load_claim_status_updates_rejects_non_string_note(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"status_update","claim_id":"claim-1","new_status":"resolved","updated_at":"2026-05-18T09:30:00+00:00","updated_by":"operator","note":123}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="note must be a string"):
        load_claim_status_updates("acme", programs_root)


def test_load_claim_entries_rejects_non_string_record_type(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":123,"id":"claim-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"workstream_id":"ws","text":"Ship by June 15","entity_refs":["WI:1001"],"claim_date":"2026-05-10","owner_alias":"owner","due_date":"2026-06-15","status":"open","contradiction_status":"none","source_confidence_tier":"grounded","last_validated_date":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="record_type must be a string"):
        load_claim_entries("acme", programs_root)


def test_load_claim_entries_rejects_unknown_record_type(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"bogus","id":"claim-1","program_id":"acme","edition_id":"acme_weekly","issue_number":77,"workstream_id":"ws","text":"Ship by June 15","entity_refs":["WI:1001"],"claim_date":"2026-05-10","owner_alias":"owner","due_date":"2026-06-15","status":"open","contradiction_status":"none","source_confidence_tier":"grounded","last_validated_date":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unknown claim log record_type 'bogus'"):
        load_claim_entries("acme", programs_root)


def test_load_claim_status_updates_rejects_unknown_new_status(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("acme", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"status_update","claim_id":"claim-1","new_status":"bogus","updated_at":"2026-05-18T09:30:00+00:00","updated_by":"operator","note":"closed"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported claim status 'bogus'"):
        load_claim_status_updates("acme", programs_root)


def _sample_item(item_id: int, *, target_date: date) -> WorkItem:
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
        target_date=target_date,
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={"changed_date": as_of.isoformat()},
        revisions=[],
        comments=[],
        fetched_at=as_of,
    )


def _sample_ai_claim_extraction_result():
    return extract_claims_from_confirmed_narratives(
        program_id="other",
        edition_id="other_weekly",
        issue_number=1,
        claim_date=date(2026, 5, 1),
        narratives={
            "exec_summary.md": (
                "WI:1001 UD chunking fix expected by June 15. "
                "WI:1002 rollout expected by June 20. "
                "WI:1003 compliance expected by June 21. "
                "WI:1004 buildout expected by June 22. "
                "Need LT decision on SCHIE timeline."
            )
        },
        items=(
            _sample_item(1001, target_date=date(2026, 6, 15)),
            _sample_item(1002, target_date=date(2026, 6, 20)),
            _sample_item(1003, target_date=date(2026, 6, 21)),
            _sample_item(1004, target_date=date(2026, 6, 22)),
        ),
    )
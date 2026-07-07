from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.models_v2 import RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.risk_register_engine import assess_risk_staleness, compute_risk_score, link_risk_action, load_risk_history, load_risk_register, record_risk_update, save_risk_register
from src.core.program_fact_store import load_program_facts, project_risk_entries


def test_load_risk_register_returns_empty_tuple_when_file_absent(tmp_path: Path) -> None:
    assert load_risk_register("acme", programs_root=tmp_path / "programs") == ()


def test_load_risk_register_reads_yaml(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    register_path = programs_root / "acme" / "risk_register.yaml"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "risks:",
                "  - id: risk-1",
                "    title: Firmware sign-off lag",
                "    description: Firmware sign-off may miss the pilot checkpoint.",
                "    probability: likely",
                "    impact: high",
                "    category: schedule",
                "    owner_alias: maintainer",
                "    mitigation_plan: Escalate in the weekly checkpoint.",
                "    mitigation_due_date: 2026-05-20",
                "    linked_workstream_ids:",
                "      - acme",
                "    linked_work_item_ids:",
                "      - 900001",
                "    linked_milestone_ids:",
                "      - m4-contoso-pilot",
                "    linked_claim_ids:",
                "      - claim-1",
                "    linked_action_ids:",
                "      - action-1",
                "    status: open",
                "    identified_date: 2026-05-01",
                "    identified_in_vertex_issue: 77",
                "    last_reviewed_date: 2026-05-08",
                "    entity_refs:",
                "      - WI:900001",
            )
        ),
        encoding="utf-8",
    )

    entries = load_risk_register("acme", programs_root=programs_root)

    assert len(entries) == 1
    assert entries[0].category == RiskCategory.SCHEDULE
    assert entries[0].mitigation_due_date == date(2026, 5, 20)
    assert entries[0].linked_work_item_ids == (900001,)


def test_load_risk_register_rejects_invalid_schema(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    register_path = programs_root / "acme" / "risk_register.yaml"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text('schema_version: "2.0"\nrisks: []\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="Unsupported risk register schema_version"):
        load_risk_register("acme", programs_root=programs_root)


def test_load_risk_register_rejects_non_string_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    register_path = programs_root / "acme" / "risk_register.yaml"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "risks:",
                "  - id: risk-1",
                "    title: Firmware sign-off lag",
                "    description: Firmware sign-off may miss the pilot checkpoint.",
                "    probability: likely",
                "    impact: high",
                "    category: schedule",
                "    owner_alias: maintainer",
                "    status: 1",
                "    identified_date: 2026-05-01",
                "    entity_refs: []",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="status must be a string"):
        load_risk_register("acme", programs_root=programs_root)


def test_load_risk_register_rejects_non_string_entity_ref(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    register_path = programs_root / "acme" / "risk_register.yaml"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "risks:",
                "  - id: risk-1",
                "    title: Firmware sign-off lag",
                "    description: Firmware sign-off may miss the pilot checkpoint.",
                "    probability: likely",
                "    impact: high",
                "    category: schedule",
                "    owner_alias: maintainer",
                "    status: open",
                "    identified_date: 2026-05-01",
                "    entity_refs:",
                "      - 900001",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="entity_refs must contain strings only"):
        load_risk_register("acme", programs_root=programs_root)


def test_load_risk_register_rejects_numeric_string_issue_number(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    register_path = programs_root / "acme" / "risk_register.yaml"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "risks:",
                "  - id: risk-1",
                "    title: Firmware sign-off lag",
                "    description: Firmware sign-off may miss the pilot checkpoint.",
                "    probability: likely",
                "    impact: high",
                "    category: schedule",
                "    owner_alias: maintainer",
                "    status: open",
                "    identified_date: 2026-05-01",
                '    identified_in_vertex_issue: "77"',
                "    entity_refs: []",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="identified_in_vertex_issue must be an integer"):
        load_risk_register("acme", programs_root=programs_root)


def test_assess_risk_staleness_flags_unreviewed_open_risk() -> None:
    entry = _risk_entry(identified_date=date(2026, 4, 1), last_reviewed_date=None)

    assert assess_risk_staleness(entry, as_of=date(2026, 5, 10)) is True


def test_compute_risk_score_multiplies_probability_and_impact() -> None:
    entry = _risk_entry(probability=RiskProbability.LIKELY, impact=RiskImpact.HIGH)

    assert compute_risk_score(entry) == 9


def test_save_risk_register_round_trips_and_creates_backup(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    register_path = programs_root / "acme" / "risk_register.yaml"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text('schema_version: "1.0"\nrisks: []\n', encoding="utf-8")

    save_risk_register("acme", (_risk_entry(),), programs_root=programs_root)

    backup_path = register_path.with_suffix(".yaml.bak")
    assert backup_path.exists()
    entries = load_risk_register("acme", programs_root=programs_root)
    assert len(entries) == 1
    assert entries[0].title == "Firmware sign-off lag"


def test_save_risk_register_dual_writes_current_fact_store_projection(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry = _risk_entry()

    save_risk_register("acme", (entry,), programs_root=programs_root)

    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)

    assert tuple(replace(risk, fact_id=None, last_validated_at=None) for risk in project_risk_entries(snapshot)) == (entry,)


def test_save_risk_register_closes_removed_fact_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry = _risk_entry()

    save_risk_register("acme", (entry,), programs_root=programs_root)
    save_risk_register("acme", (), programs_root=programs_root)

    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)

    assert project_risk_entries(snapshot) == ()


def test_record_risk_update_and_load_risk_history(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    record_risk_update(
        "acme",
        "risk-1",
        "open",
        "escalated",
        "maintainer",
        "Escalated in the weekly sync.",
        programs_root=programs_root,
    )
    record_risk_update(
        "acme",
        "risk-2",
        "open",
        "mitigated",
        "maintainer",
        None,
        programs_root=programs_root,
    )

    history = load_risk_history("acme", "risk-1", programs_root=programs_root)

    assert len(history) == 1
    assert history[0]["risk_id"] == "risk-1"
    assert history[0]["new_status"] == "escalated"
    assert history[0]["author"] == "maintainer"


def test_link_risk_action_appends_new_action_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    save_risk_register("acme", (_risk_entry(linked_action_ids=("action-1",)),), programs_root=programs_root)

    updated = link_risk_action("acme", "risk-1", "action-2", programs_root=programs_root)
    entries = load_risk_register("acme", programs_root=programs_root)

    assert updated.linked_action_ids == ("action-1", "action-2")
    assert entries[0].linked_action_ids == ("action-1", "action-2")


def _risk_entry(
    *,
    probability: RiskProbability = RiskProbability.LIKELY,
    impact: RiskImpact = RiskImpact.HIGH,
    identified_date: date = date(2026, 5, 1),
    last_reviewed_date: date | None = date(2026, 5, 8),
    linked_action_ids: tuple[str, ...] = ("action-1",),
) -> RiskEntry:
    return RiskEntry(
        id="risk-1",
        program_id="acme",
        title="Firmware sign-off lag",
        description="Firmware sign-off may miss the pilot checkpoint.",
        probability=probability,
        impact=impact,
        category=RiskCategory.SCHEDULE,
        owner_alias="maintainer",
        mitigation_plan="Escalate in the weekly checkpoint.",
        mitigation_due_date=date(2026, 5, 20),
        linked_workstream_ids=("acme",),
        linked_work_item_ids=(900001,),
        linked_milestone_ids=("m4-contoso-pilot",),
        linked_claim_ids=("claim-1",),
        linked_action_ids=linked_action_ids,
        status=RiskStatus.OPEN,
        identified_date=identified_date,
        identified_in_vertex_issue=77,
        last_reviewed_date=last_reviewed_date,
        entity_refs=("WI:900001",),
    )


# ---------------------------------------------------------------------------
# FR-SG-20: derive_strategic_risk_level
# ---------------------------------------------------------------------------

from src.core.models_v2 import RiskDerivedLevel
from src.core.risk_register_engine import derive_strategic_risk_level
from src.core.models import WorkItem, RiskLevel


def _work_item(
    work_item_id: int,
    *,
    risk_level: RiskLevel = RiskLevel.MEDIUM,
    state: str = "Active",
) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title="Test item",
        state=state,
        assigned_to=None,
        assigned_to_email=None,
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\Sprint 24",
        target_date=date(2026, 5, 20),
        risk_level=risk_level,
        tags=[],
        custom_fields={},
    )


def test_derive_strategic_risk_level_returns_risk_derived_level() -> None:
    result = derive_strategic_risk_level("acme", (_work_item(101),))
    assert isinstance(result, RiskDerivedLevel)


def test_derive_strategic_risk_level_is_always_proposal() -> None:
    result = derive_strategic_risk_level("acme", (_work_item(101),))
    assert result.is_proposal is True


def test_derive_strategic_risk_level_empty_items_returns_unknown() -> None:
    result = derive_strategic_risk_level("acme", ())
    assert result.proposed_level == RiskLevel.UNKNOWN


def test_derive_strategic_risk_level_majority_high_items_returns_high() -> None:
    items = (
        _work_item(1, risk_level=RiskLevel.HIGH),
        _work_item(2, risk_level=RiskLevel.HIGH),
        _work_item(3, risk_level=RiskLevel.MEDIUM),
    )
    result = derive_strategic_risk_level("acme", items)
    assert result.proposed_level == RiskLevel.HIGH


def test_derive_strategic_risk_level_scope_delta_upgrades_to_high() -> None:
    # All items are medium, but new high-risk items appeared → should upgrade
    items = (_work_item(1, risk_level=RiskLevel.MEDIUM),)
    result = derive_strategic_risk_level("acme", items, scope_delta_new_high_risk=3)
    assert result.proposed_level == RiskLevel.HIGH
    assert result.upgrade_reason is not None


def test_derive_strategic_risk_level_low_trajectory_credibility_upgrades() -> None:
    # Medium base + very low trajectory credibility → should upgrade
    items = (_work_item(1, risk_level=RiskLevel.MEDIUM),)
    result = derive_strategic_risk_level("acme", items, trajectory_credibility=0.2)
    assert result.proposed_level in (RiskLevel.MEDIUM, RiskLevel.HIGH)
    # With low credibility, upgrade reason should be set
    assert result.upgrade_reason is not None


def test_derive_strategic_risk_level_escalated_risk_entry_upgrades() -> None:
    # MEDIUM base + escalated risk register entry → HIGH (one upgrade step: MEDIUM→HIGH)
    items = (_work_item(1, risk_level=RiskLevel.MEDIUM),)
    escalated_entry = _risk_entry()
    from src.core.models_v2 import RiskStatus
    import dataclasses
    escalated = dataclasses.replace(escalated_entry, status=RiskStatus.ESCALATED)
    result = derive_strategic_risk_level("acme", items, risk_entries=(escalated,))
    assert result.proposed_level == RiskLevel.HIGH
    assert result.upgrade_reason is not None


def test_derive_strategic_risk_level_upgrade_reason_populated_on_upgrade() -> None:
    items = (_work_item(1, risk_level=RiskLevel.MEDIUM),)
    result = derive_strategic_risk_level("acme", items, scope_delta_new_high_risk=1)
    assert result.upgrade_reason is not None
    assert result.downgrade_reason is None


def test_derive_strategic_risk_level_no_upgrade_for_low_items() -> None:
    items = (
        _work_item(1, risk_level=RiskLevel.LOW),
        _work_item(2, risk_level=RiskLevel.LOW),
    )
    result = derive_strategic_risk_level("acme", items)
    assert result.proposed_level == RiskLevel.LOW
    assert result.upgrade_reason is None
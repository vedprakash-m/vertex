from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.models_v2 import RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.risk_register_engine import assess_risk_staleness, compute_risk_score, link_risk_action, load_risk_history, load_risk_register, record_risk_update, save_risk_register
from src.core.risk_register_engine import compute_risk_delta_preview_hash, preview_risk_upserts_from_signals, upsert_risk_from_signal
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


def test_save_risk_register_threads_source_signal_ids_onto_the_fact_revision(tmp_path: Path) -> None:
    # ADF-W2.4/W2.5: a signal-derived risk's source_signal_ids must land on
    # the ProgramFactRevision's own top-level field (what fact_lineage_
    # coverage.py's classifier actually inspects), not just inside the
    # payload dict RiskEntry projection round-trips through.
    programs_root = tmp_path / "programs"
    entry = _risk_entry_with_source_signal_ids(("sig-abc-123",))

    save_risk_register("acme", (entry,), programs_root=programs_root)

    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)
    risk_facts = [fact for fact in snapshot.facts if fact.fact_type == "risk.entry"]

    assert len(risk_facts) == 1
    assert risk_facts[0].source_signal_ids == ("sig-abc-123",)

    from src.core.fact_lineage_coverage import has_fact_provenance
    assert has_fact_provenance(risk_facts[0]) is True


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


def _risk_entry_with_source_signal_ids(source_signal_ids: tuple[str, ...]) -> RiskEntry:
    return replace(_risk_entry(), source_signal_ids=source_signal_ids)


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


class TestRiskDeltaPreview:
    """D-17/ARM-GATHER-11 AG-6.3: confirm dry-run's non-mutating risk-register
    delta preview must never drift from what upsert_risk_from_signal (the
    real, mutating path) would actually do -- both share _decide_risk_upsert.
    """

    def test_preview_new_signal_reports_new_without_writing_register(self, tmp_path: Path) -> None:
        register_path = tmp_path / "acme" / "risk_register.yaml"
        assert not register_path.exists()

        previews = preview_risk_upserts_from_signals(
            "acme",
            (("sig-1", "Build pipeline flakiness increasing", ("12345",), "ws-release"),),
            programs_root=tmp_path,
        )

        assert len(previews) == 1
        assert previews[0].signal_id == "sig-1"
        assert previews[0].action == "new"
        assert previews[0].title.startswith("Build pipeline flakiness")
        # Non-mutating: no register file was created by the preview.
        assert not register_path.exists()
        assert load_risk_register("acme", programs_root=tmp_path) == ()

    def test_preview_matches_real_upsert_for_new_then_repeated_signal(self, tmp_path: Path) -> None:
        signal_args = dict(
            signal_id="sig-2",
            signal_text="Deployment rollout stalled on ring 2",
            signal_entity_refs=("55555",),
            signal_workstream_id="ws-deploy",
        )

        preview_before = preview_risk_upserts_from_signals(
            "acme", ((signal_args["signal_id"], signal_args["signal_text"], signal_args["signal_entity_refs"], signal_args["signal_workstream_id"]),), programs_root=tmp_path
        )
        assert preview_before[0].action == "new"

        real_entry = upsert_risk_from_signal("acme", programs_root=tmp_path, **signal_args)

        # AG-6.5: preview's predicted risk_id/title match what the real upsert produced.
        assert preview_before[0].risk_id == real_entry.id
        assert preview_before[0].title == real_entry.title

        # Re-observing the identical signal is a no-op for both preview and real upsert.
        preview_after = preview_risk_upserts_from_signals(
            "acme", ((signal_args["signal_id"], signal_args["signal_text"], signal_args["signal_entity_refs"], signal_args["signal_workstream_id"]),), programs_root=tmp_path
        )
        assert preview_after[0].action == "no_change"
        assert preview_after[0].risk_id == real_entry.id

    def test_preview_matches_field_for_matching_entity_ref_update(self, tmp_path: Path) -> None:
        _write_minimal_register_for_preview(
            tmp_path / "acme",
            [
                {
                    "id": "risk-existing",
                    "program_id": "acme",
                    "title": "Existing risk",
                    "description": "Already tracked",
                    "probability": "likely",
                    "impact": "high",
                    "category": "technical",
                    "owner_alias": "owner",
                    "status": "open",
                    "identified_date": "2026-07-01",
                    "entity_refs": ["77777"],
                    "source_signal_ids": ["sig-original"],
                }
            ],
        )

        previews = preview_risk_upserts_from_signals(
            "acme",
            (("sig-new", "A related update on the same risk", ("77777",), None),),
            programs_root=tmp_path,
        )

        assert previews[0].action == "updated"
        assert previews[0].risk_id == "risk-existing"
        # Non-mutating: the register on disk is untouched.
        reloaded = load_risk_register("acme", programs_root=tmp_path)
        assert reloaded[0].source_signal_ids == ("sig-original",)

    def test_preview_hash_is_stable_for_identical_input_and_changes_on_divergence(self, tmp_path: Path) -> None:
        signals = (("sig-1", "Risk A", ("1",), None), ("sig-2", "Risk B", ("2",), None))

        preview_a = preview_risk_upserts_from_signals("acme", signals, programs_root=tmp_path)
        preview_b = preview_risk_upserts_from_signals("acme", signals, programs_root=tmp_path)
        assert compute_risk_delta_preview_hash(preview_a) == compute_risk_delta_preview_hash(preview_b)

        different_signals = signals + (("sig-3", "Risk C", ("3",), None),)
        preview_c = preview_risk_upserts_from_signals("acme", different_signals, programs_root=tmp_path)
        assert compute_risk_delta_preview_hash(preview_a) != compute_risk_delta_preview_hash(preview_c)

    def test_preview_batch_sequential_matching_mirrors_real_upsert_loop(self, tmp_path: Path) -> None:
        # Two signals sharing an entity_ref within the same preview batch: the
        # second must be seen as matching the first's not-yet-persisted new risk,
        # exactly like the real per-signal upsert loop in archive_transaction.py.
        signals = (
            ("sig-1", "Storage node flapping under load", ("88888",), None),
            ("sig-2", "Storage node flapping under load (follow-up)", ("88888",), None),
        )

        previews = preview_risk_upserts_from_signals("acme", signals, programs_root=tmp_path)

        assert previews[0].action == "new"
        assert previews[1].action == "updated"
        assert previews[1].risk_id == previews[0].risk_id


def _write_minimal_register_for_preview(program_dir: Path, risks: list[dict]) -> None:
    import yaml

    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "risk_register.yaml").write_text(
        yaml.safe_dump({"schema_version": "1.0", "risks": risks}, sort_keys=False),
        encoding="utf-8",
    )
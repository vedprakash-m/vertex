from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

from src.core.models_v2 import RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.program_fact_store import load_program_facts, project_risk_entries
from src.core.risk_dedupe import RiskDedupeFinding, derive_risk_dedupe_key, detect_risk_duplicates
from src.core.risk_register_engine import _risk_entry_to_record, load_risk_register, save_risk_register

_LIFECYCLE = {
    "dedupe_key": "k-1",
    "supersedes": "risk-0",
    "superseded_by": "risk-2",
    "escalation_state": "escalated_to_vp",
    "contingency_trigger": "pilot slips past 2026-06-01",
    "residual_risk": "low",
    "outcome": "mitigated",
    "effectiveness": "sign-off landed on time",
}


def _risk(risk_id: str = "risk-1", **overrides: object) -> RiskEntry:
    base = RiskEntry(
        id=risk_id,
        program_id="acme",
        title="Firmware sign-off lag",
        description="Firmware sign-off may miss the pilot checkpoint.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.SCHEDULE,
        owner_alias="maintainer",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=(900002, 900001),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 5, 1),
        identified_in_vertex_issue=None,
        last_reviewed_date=None,
        entity_refs=(),
    )
    return replace(base, **overrides)


def test_new_fields_default_to_none_and_are_omitted_from_record() -> None:
    risk = _risk()
    assert all(getattr(risk, name) is None for name in _LIFECYCLE)
    assert not set(_LIFECYCLE) & set(_risk_entry_to_record(risk))


def test_lifecycle_fields_round_trip_yaml_and_fact_projection(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    risk = _risk(**_LIFECYCLE)
    save_risk_register("acme", (risk,), programs_root=root)
    assert load_risk_register("acme", programs_root=root) == (risk,)
    projected, = project_risk_entries(load_program_facts("acme", programs_root=root, fact_types=("risk.entry",)))
    assert {name: getattr(projected, name) for name in _LIFECYCLE} == _LIFECYCLE


def test_unset_fields_keep_saved_yaml_byte_identical(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    save_risk_register("acme", (_risk(),), programs_root=root)
    text = (root / "acme" / "risk_register.yaml").read_text(encoding="utf-8")
    assert not any(f"{name}:" in text for name in _LIFECYCLE)


def test_derive_dedupe_key_normalizes_title_owner_and_work_items() -> None:
    a = _risk(title="  Firmware   Sign-off LAG! ", owner_alias="Maintainer")
    b = _risk(title="firmware sign off lag", owner_alias="maintainer", linked_work_item_ids=(900001, 900002))
    assert derive_risk_dedupe_key(a) == derive_risk_dedupe_key(b) == "firmware sign off lag|maintainer|900001,900002"
    assert derive_risk_dedupe_key(_risk(linked_work_item_ids=())) == "firmware sign off lag|maintainer|"


def test_detect_duplicates_groups_by_explicit_or_derived_key_without_mutation() -> None:
    risks = (
        _risk("r-b"),
        _risk("r-a", title="FIRMWARE sign-off lag"),
        _risk("r-c", title="Different", dedupe_key="firmware sign off lag|maintainer|900001,900002"),
        _risk("r-d", title="Unique"),
    )
    findings = detect_risk_duplicates(risks)
    assert findings == (
        RiskDedupeFinding(
            kind="duplicate",
            risk_ids=("r-a", "r-b", "r-c"),
            dedupe_key="firmware sign off lag|maintainer|900001,900002",
        ),
    )
    assert detect_risk_duplicates(tuple(reversed(risks))) == findings


def test_detect_supersede_chain_dangling_and_cycle() -> None:
    risks = (
        _risk("r1", title="t1"),
        _risk("r2", title="t2", supersedes="r1"),
        _risk("r3", title="t3", superseded_by=None),
        _risk("r2b", title="t2b", superseded_by="r3"),
        _risk("r4", title="t4", supersedes="ghost"),
        _risk("c1", title="c1", superseded_by="c2"),
        _risk("c2", title="c2", superseded_by="c1"),
    )
    findings = detect_risk_duplicates(risks)
    assert findings == (
        RiskDedupeFinding(kind="supersede_chain", risk_ids=("r1", "r2")),
        RiskDedupeFinding(kind="supersede_chain", risk_ids=("r2b", "r3")),
        RiskDedupeFinding(kind="supersede_cycle", risk_ids=("c1", "c2")),
        RiskDedupeFinding(kind="supersede_dangling", risk_ids=("r4", "ghost")),
    )


def test_detect_supersede_cycle_entered_by_multiple_chains() -> None:
    risks = (
        _risk("a", title="a", superseded_by="c"),
        _risk("b", title="b", superseded_by="d"),
        _risk("c", title="c", superseded_by="d"),
        _risk("d", title="d", superseded_by="c"),
    )
    findings = (
        RiskDedupeFinding(kind="supersede_chain", risk_ids=("a", "c", "d")),
        RiskDedupeFinding(kind="supersede_chain", risk_ids=("b", "d", "c")),
        RiskDedupeFinding(kind="supersede_cycle", risk_ids=("c", "d")),
    )
    assert detect_risk_duplicates(risks) == findings
    assert detect_risk_duplicates(tuple(reversed(risks))) == findings


def test_detect_supersede_merge_and_fork_without_mutating_edges() -> None:
    risks = (
        _risk("old-a", title="old-a", superseded_by="new"),
        _risk("old-b", title="old-b", superseded_by="new"),
        _risk("forked", title="forked", superseded_by="replacement-a"),
        _risk("new", title="new"),
        _risk("replacement-a", title="replacement-a"),
        _risk("replacement-b", title="replacement-b", supersedes="forked"),
    )

    findings = detect_risk_duplicates(risks)

    assert RiskDedupeFinding(
        kind="supersede_merge", risk_ids=("new", "old-a", "old-b"),
    ) in findings
    assert RiskDedupeFinding(
        kind="supersede_fork",
        risk_ids=("forked", "replacement-a", "replacement-b"),
    ) in findings
    assert detect_risk_duplicates(tuple(reversed(risks))) == findings


def test_detect_supersede_self_cycle_after_chain() -> None:
    risks = (
        _risk("a", title="a", superseded_by="b"),
        _risk("b", title="b", superseded_by="b"),
    )
    assert detect_risk_duplicates(risks) == (
        RiskDedupeFinding(kind="supersede_chain", risk_ids=("a", "b")),
        RiskDedupeFinding(kind="supersede_cycle", risk_ids=("b",)),
    )

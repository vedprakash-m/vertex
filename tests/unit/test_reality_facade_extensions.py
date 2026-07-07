from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from cli import app
from src.core.program_fact_store import (
    FactLifecycleState,
    FactPrecedence,
    FactReviewState,
    ProgramFactRevision,
    ProgramFactSnapshot,
)
from src.core.program_reality import (
    ActuationProposal,
    AttentionItem,
    EvidenceRef,
    FactAssessment,
    FactExplanation,
    FleetReality,
    RealityConflict,
    RealityDomainFreshness,
    TruthLevel,
)


runner = CliRunner()


def _fact(
    *,
    fact_id: str,
    fact_type: str,
    natural_key: str,
    entity_refs: tuple[str, ...],
    payload: dict[str, object] | None = None,
    source_signal_ids: tuple[str, ...] = (),
) -> ProgramFactRevision:
    return ProgramFactRevision(
        revision_id=f"rev-{fact_id}",
        fact_id=fact_id,
        program_id="demo",
        natural_key=natural_key,
        fact_type=fact_type,
        scope="program",
        entity_refs=entity_refs,
        payload=dict(payload or {}),
        source_signal_ids=source_signal_ids,
        confidence=None,
        precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        review_state=FactReviewState.ACCEPTED,
        lifecycle_state=FactLifecycleState.ACTIVE,
        valid_from=None,
        valid_until=None,
        recorded_at=datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc),
        superseded_at=None,
        projection_history=(),
        proposed_against_revision_id=None,
        created_by="unit-test",
    )


def test_program_reality_explain_surfaces_evidence_and_open_conflicts() -> None:
    from src.core.program_reality import ProgramReality

    action_fact = _fact(
        fact_id="fact-action-1",
        fact_type="action.item",
        natural_key="action:1",
        entity_refs=("ACTION:1",),
        source_signal_ids=("sig-1",),
    )
    conflict_fact = _fact(
        fact_id="fact-conflict-1",
        fact_type="fact.conflict",
        natural_key="conflict:action:1",
        entity_refs=("ACTION:1",),
        payload={
            "family": "action",
            "description": "ADO says done, Teams says in-progress",
            "resolved": False,
            "is_material": True,
        },
    )
    snapshot = ProgramFactSnapshot(
        program_id="demo",
        as_of=datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc),
        facts=(action_fact, conflict_fact),
    )
    assessment = FactAssessment(
        record=SimpleNamespace(title="Action 1"),
        fact_id="fact-action-1",
        truth_level=TruthLevel.CORROBORATED,
        disputed=True,
        stale=False,
        provisional_inputs=True,
        evidence=("sig-1", "ACTION:1"),
    )
    reality = ProgramReality(
        program_id="demo",
        snapshot=snapshot,
        sor_mode="primary",
        as_of=datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc),
        _entity_fact_index={"ACTION:1": [assessment]},
        _actions=(assessment,),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
    )

    explanation = reality.explain("fact-action-1")

    assert explanation is not None
    assert explanation.truth_level is TruthLevel.CORROBORATED
    assert explanation.disputed is True
    assert explanation.provisional_inputs is True
    assert explanation.source_signal_ids == ("sig-1",)
    assert explanation.entity_refs == ("ACTION:1",)
    assert explanation.evidence == (
        EvidenceRef(signal_id="sig-1", entity_ref=None, source=None),
        EvidenceRef(signal_id=None, entity_ref="ACTION:1", source=None),
    )
    assert len(explanation.open_conflicts) == 1
    assert explanation.open_conflicts[0].description == "ADO says done, Teams says in-progress"


def test_program_reality_conflicts_reads_the_real_conflict_description_field() -> None:
    """GAP-37 real-data bug: fact_bridge.py's _append_conflict_fact writes
    "conflict_description" (the schema-required field), not "description" --
    conflicts() must read the real field or every production conflict has an
    empty description."""
    from src.core.program_reality import ProgramReality

    conflict_fact = _fact(
        fact_id="fact-conflict-real-shape",
        fact_type="fact.conflict",
        natural_key="conflict:action:2",
        entity_refs=("ACTION:2",),
        payload={
            "family": "action",
            "conflict_description": "action: done → in-progress",
            "resolved": False,
            "winning_source": "ado",
            "losing_source": "teams",
            "winning_value": "Done",
            "losing_value": "In Progress",
            "resolution": "primary_authority:ado",
            "detected_at": "2026-07-26T12:00:00+00:00",
        },
    )
    snapshot = ProgramFactSnapshot(
        program_id="demo", as_of=datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc), facts=(conflict_fact,),
    )
    reality = ProgramReality(
        program_id="demo", snapshot=snapshot, sor_mode="primary",
        as_of=datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc),
        _entity_fact_index={}, _actions=(), _risks=(), _decisions=(), _dependencies=(),
        _milestones=(), _assumptions=(), _workstreams=(), _claims=(),
    )

    conflicts = reality.conflicts(open_only=True)

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.description == "action: done → in-progress"
    assert conflict.winning_source == "ado"
    assert conflict.losing_source == "teams"
    assert conflict.winning_value == "Done"
    assert conflict.losing_value == "In Progress"
    assert conflict.resolution == "primary_authority:ado"
    assert conflict.detected_at == "2026-07-26T12:00:00+00:00"


def test_fleet_reality_aggregates_attention_conflicts_actuation_and_freshness() -> None:
    class _FakeProgram:
        def __init__(
            self,
            program_id: str,
            *,
            attention_items: tuple[AttentionItem, ...],
            conflicts: tuple[RealityConflict, ...],
            actuations: tuple[ActuationProposal, ...],
            freshness_rows: tuple[RealityDomainFreshness, ...],
        ) -> None:
            self.program_id = program_id
            self._attention_items = attention_items
            self._conflicts = conflicts
            self._actuations = actuations
            self._freshness_rows = freshness_rows

        def attention(self, *, owner: str | None = None) -> tuple[AttentionItem, ...]:
            del owner
            return self._attention_items

        def conflicts(self, *, open_only: bool = True) -> tuple[RealityConflict, ...]:
            del open_only
            return self._conflicts

        def pending_actuations(self) -> tuple[ActuationProposal, ...]:
            return self._actuations

        def freshness(self) -> tuple[RealityDomainFreshness, ...]:
            return self._freshness_rows

        def to_dict(self, *, max_classification: str = "internal") -> dict[str, object]:
            return {"program_id": self.program_id, "max_classification": max_classification}

    p1 = _FakeProgram(
        "fabrikam",
        attention_items=(
            AttentionItem(
                kind="stale_high_severity",
                priority=1,
                record=None,
                description="risk stale",
                action_hint="update it",
            ),
        ),
        conflicts=(
            RealityConflict(
                conflict_id="conf-1",
                entity_refs=("ACTION:1",),
                family="action",
                open=True,
                description="open conflict",
            ),
        ),
        actuations=(
            ActuationProposal(
                proposal_id="ap-1",
                rule_id="rule-1",
                adapter="ado",
                operation="update_work_item",
                entity_ref="WI:1",
                payload={},
                proposed_at=datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc),
            ),
        ),
        freshness_rows=(
            RealityDomainFreshness(
                domain="actions",
                fact_count=2,
                stale_count=1,
                latest_recorded_at=None,
                sor_mode="primary",
            ),
        ),
    )
    p2 = _FakeProgram(
        "acme",
        attention_items=(
            AttentionItem(
                kind="unanswered_decision",
                priority=2,
                record=None,
                description="decision stale",
                action_hint="resolve it",
            ),
        ),
        conflicts=(),
        actuations=(),
        freshness_rows=(
            RealityDomainFreshness(
                domain="risks",
                fact_count=3,
                stale_count=0,
                latest_recorded_at=None,
                sor_mode="shadow",
            ),
        ),
    )

    fleet = FleetReality((p1, p2))

    assert fleet.program_ids() == ("fabrikam", "acme")
    assert [entry.program_id for entry in fleet.attention()] == ["fabrikam", "acme"]
    assert len(fleet.conflicts()) == 1
    assert fleet.conflicts()[0].program_id == "fabrikam"
    assert len(fleet.pending_actuations()) == 1
    assert fleet.pending_actuations()[0].program_id == "fabrikam"
    assert len(fleet.freshness()) == 2

    payload = fleet.to_dict()
    assert payload["scope"] == "fleet"
    assert payload["program_count"] == 2
    assert payload["open_conflict_count"] == 1
    assert payload["pending_actuation_count"] == 1
    assert payload["attention_count"] == 2


def test_reality_explain_command_renders_json(monkeypatch) -> None:
    explanation = FactExplanation(
        program_id="demo",
        fact_id="fact-1",
        fact_type="action.item",
        natural_key="action:1",
        truth_level=TruthLevel.CORROBORATED,
        disputed=True,
        stale=False,
        provisional_inputs=True,
        evidence=(
            EvidenceRef(signal_id="sig-1", entity_ref=None, source=None),
            EvidenceRef(signal_id=None, entity_ref="ACTION:1", source=None),
        ),
        open_conflicts=(
            RealityConflict(
                conflict_id="conf-1",
                entity_refs=("ACTION:1",),
                family="action",
                open=True,
                description="disagreement",
            ),
        ),
        source_signal_ids=("sig-1",),
        entity_refs=("ACTION:1",),
    )

    class _FakeReality:
        def explain(self, fact_id: str) -> FactExplanation | None:
            return explanation if fact_id == "fact-1" else None

    monkeypatch.setattr(
        "src.core.program_reality.ProgramReality.load",
        lambda program_id, programs_root=Path("."): _FakeReality(),
    )

    result = runner.invoke(
        app,
        ["reality", "explain", "--program", "demo", "--fact-id", "fact-1", "--format", "json"],
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["program_id"] == "demo"
    assert payload["truth_level"] == "corroborated"
    assert payload["disputed"] is True
    assert payload["open_conflicts"][0]["conflict_id"] == "conf-1"


def test_reality_explain_command_renders_conflict_resolution_fields_when_present(monkeypatch) -> None:
    """GAP-37: `vertex reality explain` surfaces winning/losing source+value
    and the resolution reason, not just the conflict's bare description."""
    explanation = FactExplanation(
        program_id="demo",
        fact_id="fact-1",
        fact_type="action.item",
        natural_key="action:1",
        truth_level=TruthLevel.CORROBORATED,
        disputed=True,
        stale=False,
        provisional_inputs=True,
        evidence=(),
        open_conflicts=(
            RealityConflict(
                conflict_id="conf-1",
                entity_refs=("ACTION:1",),
                family="action",
                open=True,
                description="ado: done -> in-progress",
                winning_source="ado",
                losing_source="teams",
                winning_value="Done",
                losing_value="In Progress",
                resolution="primary_authority:ado",
                detected_at="2026-07-26T12:00:00+00:00",
            ),
        ),
        source_signal_ids=(),
        entity_refs=("ACTION:1",),
    )

    class _FakeReality:
        def explain(self, fact_id: str) -> FactExplanation | None:
            return explanation if fact_id == "fact-1" else None

    monkeypatch.setattr(
        "src.core.program_reality.ProgramReality.load",
        lambda program_id, programs_root=Path("."): _FakeReality(),
    )

    json_result = runner.invoke(app, ["reality", "explain", "--program", "demo", "--fact-id", "fact-1", "--format", "json"])
    payload = json.loads(json_result.stdout)
    conflict_payload = payload["open_conflicts"][0]
    assert conflict_payload["winning_source"] == "ado"
    assert conflict_payload["losing_source"] == "teams"
    assert conflict_payload["winning_value"] == "Done"
    assert conflict_payload["losing_value"] == "In Progress"
    assert conflict_payload["resolution"] == "primary_authority:ado"

    text_result = runner.invoke(app, ["reality", "explain", "--program", "demo", "--fact-id", "fact-1", "--format", "text"])
    assert "ado (Done) beat teams (In Progress)" in text_result.stdout
    assert "primary_authority:ado" in text_result.stdout


def test_build_fleet_status_payload_aggregates_facade_counts(monkeypatch, tmp_path: Path) -> None:
    from src.commands.reality import _build_fleet_status_payload

    class _FakeProgram:
        def __init__(self, program_id: str) -> None:
            self.program_id = program_id

        def attention(self, *, owner: str | None = None) -> tuple[AttentionItem, ...]:
            del owner
            return (
                AttentionItem(
                    kind="structural_gap",
                    priority=1,
                    record=None,
                    description=f"{self.program_id} attention",
                    action_hint="inspect",
                ),
            )

        def conflicts(self, *, open_only: bool = True) -> tuple[RealityConflict, ...]:
            del open_only
            return (
                RealityConflict(
                    conflict_id=f"conf-{self.program_id}",
                    entity_refs=(f"PROGRAM:{self.program_id}",),
                    family="program",
                    open=True,
                    description="conflict",
                ),
            )

        def pending_actuations(self) -> tuple[ActuationProposal, ...]:
            return (
                ActuationProposal(
                    proposal_id=f"ap-{self.program_id}",
                    rule_id="rule-1",
                    adapter="ado",
                    operation="update_work_item",
                    entity_ref=f"WI:{self.program_id}",
                    payload={},
                    proposed_at=datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc),
                ),
            )

        def freshness(self) -> tuple[RealityDomainFreshness, ...]:
            return (
                RealityDomainFreshness(
                    domain="actions",
                    fact_count=1,
                    stale_count=0,
                    latest_recorded_at=None,
                    sor_mode="primary",
                ),
            )

        def to_dict(self, *, max_classification: str = "internal") -> dict[str, object]:
            return {"program_id": self.program_id, "max_classification": max_classification}

    monkeypatch.setattr(
        "src.core.program_reality.ProgramReality.load",
        lambda program_id, programs_root=Path("."): _FakeProgram(program_id),
    )
    monkeypatch.setattr(
        "src.commands.reality._build_program_status_payload",
        lambda program_id, programs_root, db_root, output_root: {"program_id": program_id, "qg27": {"passed": True}},
    )

    payload = _build_fleet_status_payload(
        program_ids=("fabrikam", "acme"),
        programs_root=tmp_path,
        db_root=None,
    )

    assert payload["scope"] == "fleet_status"
    assert payload["program_count"] == 2
    assert payload["loaded_program_count"] == 2
    assert payload["attention_count"] == 2
    assert payload["open_conflict_count"] == 2
    assert payload["pending_actuation_count"] == 2
    assert payload["freshness_record_count"] == 2
    assert [entry["program_id"] for entry in payload["programs"]] == ["fabrikam", "acme"]


# ---------------------------------------------------------------------------
# GAP-19: per-family SoR mode (WI-5.2) is exposed on the ProgramReality facade
# ---------------------------------------------------------------------------


def test_program_reality_family_sor_modes_default_to_program_mode(tmp_path: Path, monkeypatch) -> None:
    """When the SoR state has no family_modes, every family reports the program-level mode."""
    from datetime import datetime, timezone
    from src.core.fact_sor_state import save_fact_sor_state

    program_root = tmp_path / "programs" / "acme"
    program_root.mkdir(parents=True)
    save_fact_sor_state(
        program_id="acme",
        mode="primary",
        recorded_at=datetime.now(timezone.utc),
        recorded_by="test",
        programs_root=tmp_path / "programs",
    )

    fake_reality = SimpleNamespace(
        _family_sor_modes={
            "workitem.state": "primary",
            "metric": "primary",
            "incident": "primary",
            "judgment": "primary",
            "commitment": "primary",
            "narrative": "primary",
        },
        _sor_mode="primary",
    )

    # Now load the reality and check the accessor returns the program mode
    # for any family not explicitly overridden.
    reality = SimpleNamespace(
        _sor_mode="primary",
        _family_sor_modes={"workitem.state": "primary", "metric": "primary", "incident": "primary", "judgment": "primary", "commitment": "primary", "narrative": "primary"},
        family_sor_mode=lambda self, family: self._family_sor_modes.get(family, self._sor_mode),
    )
    # Bind methods to the namespace.
    from types import MethodType

    def _family_sor_mode(self, family):
        return self._family_sor_modes.get(family, self._sor_mode)

    reality.family_sor_mode = MethodType(_family_sor_mode, reality)

    assert reality.family_sor_mode("workitem.state") == "primary"
    assert reality.family_sor_mode("metric") == "primary"
    assert reality.family_sor_mode("judgment") == "primary"


def test_program_reality_family_sor_modes_resolve_family_override(tmp_path: Path) -> None:
    """Per-family overrides in fact_store_sor.yaml propagate through the facade."""
    from datetime import datetime, timezone
    from src.core.fact_sor_state import save_fact_sor_state

    program_root = tmp_path / "programs" / "acme"
    program_root.mkdir(parents=True)
    save_fact_sor_state(
        program_id="acme",
        mode="primary",
        recorded_at=datetime.now(timezone.utc),
        recorded_by="test",
        programs_root=tmp_path / "programs",
        family_modes={"judgment": "shadow", "metric": "legacy"},
    )

    from src.core.fact_sor_state import (
        AUTHORITY_FAMILIES,
        load_fact_sor_state,
    )

    state = load_fact_sor_state("acme", programs_root=tmp_path / "programs")
    assert state is not None
    # The two overrides are honored; everything else falls back to the
    # program-level "primary" mode.
    assert state.family_modes.get("judgment") == "shadow"
    assert state.family_modes.get("metric") == "legacy"
    assert state.family_modes.get("workitem.state", state.mode) == "primary"
    assert state.family_modes.get("incident", state.mode) == "primary"
    assert state.family_modes.get("commitment", state.mode) == "primary"
    assert state.family_modes.get("narrative", state.mode) == "primary"
    # AUTHORITY_FAMILIES list is the contract.
    assert "judgment" in AUTHORITY_FAMILIES
    assert "workitem.state" in AUTHORITY_FAMILIES

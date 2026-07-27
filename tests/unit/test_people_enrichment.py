"""specs/bklg.md BL-E3: tests for the routine people-registry
enrichment/freshness-cadence system."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from src.core.exceptions import ConfigError
from src.core.people_directory_schema import (
    FieldVerification,
    PersonDirectory,
    PersonStatus,
    write_people_directory,
)
from src.core.policy_loader import FreshnessPolicy
from src.core.alerts import read_alerts
from src.core import people_enrichment
from src.core.people_enrichment import (
    ENRICHABLE_FIELDS,
    EnrichmentCandidateEvent,
    build_workiq_question,
    enrichment_ledger_path,
    fold_enrichment_candidates,
    list_pending_enrichment_candidates,
    maybe_alert_enrichment_due,
    read_enrichment_events,
    record_cadence_tick,
    record_enrichment_event,
    resolve_enrichment_due_alert,
    select_enrichment_candidates,
)

_NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _write_program(programs_root: Path, program_id: str, *, stakeholder_aliases: tuple[str, ...]) -> None:
    prog_dir = programs_root / program_id
    prog_dir.mkdir(parents=True, exist_ok=True)
    (prog_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "3.0",
                "id": program_id,
                "name": "Test Program",
                "stakeholder_register": [{"alias": alias, "email": f"{alias}@microsoft.com"} for alias in stakeholder_aliases],
            }
        ),
        encoding="utf-8",
    )


def _write_directory(knowledge_root: Path, people: tuple[PersonDirectory, ...]) -> None:
    knowledge_root.mkdir(parents=True, exist_ok=True)
    write_people_directory(knowledge_root / "people_directory.yaml", people)


def _verification(field_name: str, verified_at: datetime) -> FieldVerification:
    return FieldVerification(
        field_name=field_name, source="test", source_ref=None, observed_at=verified_at,
        verified_at=verified_at, recorded_at=verified_at, verified_by_principal="tester",
    )


class TestSelectEnrichmentCandidates:
    def test_selects_never_verified_empty_field_for_a_referenced_person(self, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        knowledge_root = programs_root.parent / "knowledge"
        _write_program(programs_root, "xpf", stakeholder_aliases=("alice",))
        _write_directory(knowledge_root, (PersonDirectory(entity_id="person:alice", alias="alice", status=PersonStatus.ACTIVE),))

        selected = select_enrichment_candidates(program_id="xpf", programs_root=programs_root, as_of=_NOW)

        field_names = {entry.field_name for _, entry in selected}
        assert field_names == set(ENRICHABLE_FIELDS)
        assert all(person.alias == "alice" for person, _ in selected)

    def test_excludes_a_non_stakeholder(self, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        knowledge_root = programs_root.parent / "knowledge"
        _write_program(programs_root, "xpf", stakeholder_aliases=("alice",))
        _write_directory(
            knowledge_root,
            (
                PersonDirectory(entity_id="person:alice", alias="alice", status=PersonStatus.ACTIVE),
                PersonDirectory(entity_id="person:bob", alias="bob", status=PersonStatus.ACTIVE),
            ),
        )

        selected = select_enrichment_candidates(program_id="xpf", programs_root=programs_root, as_of=_NOW)

        assert all(person.alias == "alice" for person, _ in selected)

    def test_excludes_a_sentinel_person_marked_exempt_from_vitality(self, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        knowledge_root = programs_root.parent / "knowledge"
        _write_program(programs_root, "xpf", stakeholder_aliases=("alice", "unassigned"))
        _write_directory(
            knowledge_root,
            (
                PersonDirectory(entity_id="person:alice", alias="alice", status=PersonStatus.ACTIVE),
                PersonDirectory(
                    entity_id="person:unassigned", alias="unassigned", display_name="Unassigned",
                    title="System sentinel", status=PersonStatus.ACTIVE, exempt_from_vitality=True,
                ),
            ),
        )

        selected = select_enrichment_candidates(program_id="xpf", programs_root=programs_root, as_of=_NOW)

        assert all(person.alias != "unassigned" for person, _ in selected)
        assert any(person.alias == "alice" for person, _ in selected)

    def test_excludes_a_field_that_already_has_a_fresh_verification(self, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        knowledge_root = programs_root.parent / "knowledge"
        _write_program(programs_root, "xpf", stakeholder_aliases=("alice",))
        _write_directory(
            knowledge_root,
            (
                PersonDirectory(
                    entity_id="person:alice", alias="alice", title="Senior TPM", status=PersonStatus.ACTIVE,
                    verifications=(_verification("title", _NOW - timedelta(days=5)),),
                ),
            ),
        )

        selected = select_enrichment_candidates(program_id="xpf", programs_root=programs_root, as_of=_NOW)

        assert not any(entry.field_name == "title" for _, entry in selected)
        # department/manager_entity_id are still empty and never verified -- still candidates.
        assert {entry.field_name for _, entry in selected} == {"department", "manager_entity_id"}

    def test_selects_a_field_with_a_stale_verification(self, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        knowledge_root = programs_root.parent / "knowledge"
        _write_program(programs_root, "xpf", stakeholder_aliases=("alice",))
        _write_directory(
            knowledge_root,
            (
                PersonDirectory(
                    entity_id="person:alice", alias="alice", title="Senior TPM", status=PersonStatus.ACTIVE,
                    verifications=(_verification("title", _NOW - timedelta(days=200)),),
                ),
            ),
        )

        selected = select_enrichment_candidates(program_id="xpf", programs_root=programs_root, freshness_days=90, as_of=_NOW)

        stale_title = next((entry for _, entry in selected if entry.field_name == "title"), None)
        assert stale_title is not None
        assert stale_title.age_days == 200

    def test_excludes_a_field_already_pending_review(self, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        knowledge_root = programs_root.parent / "knowledge"
        _write_program(programs_root, "xpf", stakeholder_aliases=("alice",))
        _write_directory(knowledge_root, (PersonDirectory(entity_id="person:alice", alias="alice", status=PersonStatus.ACTIVE),))
        record_enrichment_event(
            EnrichmentCandidateEvent(
                recorded_at=_NOW, program_id="xpf", candidate_id="cand-1", entity_id="person:alice",
                alias="alice", field_name="title", current_value=None, event="proposed",
                workiq_question="What is alice's title?", workiq_answer="Senior TPM",
            ),
            programs_root=programs_root,
        )

        selected = select_enrichment_candidates(program_id="xpf", programs_root=programs_root, as_of=_NOW)

        assert not any(entry.field_name == "title" for _, entry in selected)
        assert {entry.field_name for _, entry in selected} == {"department", "manager_entity_id"}

    def test_respects_max_candidates(self, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        knowledge_root = programs_root.parent / "knowledge"
        _write_program(programs_root, "xpf", stakeholder_aliases=("alice",))
        _write_directory(knowledge_root, (PersonDirectory(entity_id="person:alice", alias="alice", status=PersonStatus.ACTIVE),))

        selected = select_enrichment_candidates(program_id="xpf", programs_root=programs_root, as_of=_NOW, max_candidates=1)

        assert len(selected) == 1

    def test_no_stakeholders_returns_empty(self, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        knowledge_root = programs_root.parent / "knowledge"
        _write_program(programs_root, "xpf", stakeholder_aliases=())
        _write_directory(knowledge_root, (PersonDirectory(entity_id="person:alice", alias="alice", status=PersonStatus.ACTIVE),))

        selected = select_enrichment_candidates(program_id="xpf", programs_root=programs_root, as_of=_NOW)

        assert selected == ()


class TestBuildWorkiqQuestion:
    @pytest.mark.parametrize("field_name", ENRICHABLE_FIELDS)
    def test_builds_a_question_per_enrichable_field(self, field_name: str) -> None:
        question = build_workiq_question(display_name="Alice Smith", alias="alice", field_name=field_name)
        assert "Alice Smith" in question
        assert question.endswith("?")

    def test_falls_back_to_alias_when_no_display_name(self) -> None:
        question = build_workiq_question(display_name=None, alias="alice", field_name="title")
        assert "alice" in question

    def test_unknown_field_raises(self) -> None:
        with pytest.raises(ValueError, match="No WorkIQ question template"):
            build_workiq_question(display_name="Alice", alias="alice", field_name="favorite_color")


class TestEnrichmentLedger:
    def test_record_and_read_roundtrip(self, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        event = EnrichmentCandidateEvent(
            recorded_at=_NOW, program_id="xpf", candidate_id="cand-1", entity_id="person:alice",
            alias="alice", field_name="title", current_value=None, event="proposed",
            workiq_question="What is alice's title?", workiq_answer="Senior TPM",
        )
        record_enrichment_event(event, programs_root=programs_root)

        events = read_enrichment_events("xpf", programs_root=programs_root)

        assert events == (event,)
        assert enrichment_ledger_path("xpf", programs_root=programs_root).exists()

    def test_fold_reports_pending_when_only_proposed(self) -> None:
        events = (
            EnrichmentCandidateEvent(
                recorded_at=_NOW, program_id="xpf", candidate_id="cand-1", entity_id="person:alice",
                alias="alice", field_name="title", current_value=None, event="proposed",
                workiq_question="Q", workiq_answer="Senior TPM",
            ),
        )

        states = fold_enrichment_candidates(events)

        assert len(states) == 1
        assert states[0].status == "pending"

    def test_fold_reports_accepted_after_resolution(self) -> None:
        events = (
            EnrichmentCandidateEvent(
                recorded_at=_NOW, program_id="xpf", candidate_id="cand-1", entity_id="person:alice",
                alias="alice", field_name="title", current_value=None, event="proposed",
                workiq_question="Q", workiq_answer="Senior TPM",
            ),
            EnrichmentCandidateEvent(
                recorded_at=_NOW + timedelta(hours=1), program_id="xpf", candidate_id="cand-1", entity_id="person:alice",
                alias="alice", field_name="title", current_value=None, event="accepted",
                reviewed_value="Senior TPM", reviewed_by="sample_reviewer", reviewed_reason="confirmed", applied=True,
            ),
        )

        states = fold_enrichment_candidates(events)

        assert len(states) == 1
        assert states[0].status == "accepted"
        assert states[0].applied is True
        assert states[0].reviewed_value == "Senior TPM"

    def test_fold_reports_rejected_after_resolution(self) -> None:
        events = (
            EnrichmentCandidateEvent(
                recorded_at=_NOW, program_id="xpf", candidate_id="cand-1", entity_id="person:alice",
                alias="alice", field_name="title", current_value=None, event="proposed",
                workiq_question="Q", workiq_answer="Uncertain guess",
            ),
            EnrichmentCandidateEvent(
                recorded_at=_NOW + timedelta(hours=1), program_id="xpf", candidate_id="cand-1", entity_id="person:alice",
                alias="alice", field_name="title", current_value=None, event="rejected",
                reviewed_by="sample_reviewer", reviewed_reason="WorkIQ answer was not credible",
            ),
        )

        states = fold_enrichment_candidates(events)

        assert states[0].status == "rejected"
        assert states[0].applied is False

    def test_list_pending_excludes_resolved_candidates(self, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        record_enrichment_event(
            EnrichmentCandidateEvent(
                recorded_at=_NOW, program_id="xpf", candidate_id="cand-1", entity_id="person:alice",
                alias="alice", field_name="title", current_value=None, event="proposed",
                workiq_question="Q1", workiq_answer="A1",
            ),
            programs_root=programs_root,
        )
        record_enrichment_event(
            EnrichmentCandidateEvent(
                recorded_at=_NOW, program_id="xpf", candidate_id="cand-2", entity_id="person:bob",
                alias="bob", field_name="department", current_value=None, event="proposed",
                workiq_question="Q2", workiq_answer="A2",
            ),
            programs_root=programs_root,
        )
        record_enrichment_event(
            EnrichmentCandidateEvent(
                recorded_at=_NOW + timedelta(hours=1), program_id="xpf", candidate_id="cand-2", entity_id="person:bob",
                alias="bob", field_name="department", current_value=None, event="rejected",
                reviewed_by="sample_reviewer", reviewed_reason="not credible",
            ),
            programs_root=programs_root,
        )

        pending = list_pending_enrichment_candidates("xpf", programs_root=programs_root)

        assert len(pending) == 1
        assert pending[0].candidate_id == "cand-1"


class TestCadenceTrigger:
    """BL-E4 activation: the operator explicitly rejected an OS-level
    (Task Scheduler) cadence, wanting the enrichment reminder tied to
    Vertex's own operational rhythm (nudge/report run counts) instead."""

    def _patch_policy(self, monkeypatch: pytest.MonkeyPatch, *, nudge_every: int | None, report_every: int | None) -> None:
        policy = FreshnessPolicy(
            fact_type_ttl_days={}, gather_cadence_hours={},
            people_registry_enrichment_nudge_every=nudge_every, people_registry_enrichment_report_every=report_every,
        )
        monkeypatch.setattr(people_enrichment, "load_freshness_policy", lambda: policy)

    def test_record_cadence_tick_counts_independently_per_kind(self, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"

        assert record_cadence_tick("xpf", "nudge_run", programs_root=programs_root, now=_NOW) == 1
        assert record_cadence_tick("xpf", "nudge_run", programs_root=programs_root, now=_NOW) == 2
        assert record_cadence_tick("xpf", "report_run", programs_root=programs_root, now=_NOW) == 1

    def test_maybe_alert_enrichment_due_does_not_fire_before_threshold(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        programs_root = tmp_path / "programs"
        self._patch_policy(monkeypatch, nudge_every=5, report_every=3)

        for _ in range(2):
            fired = maybe_alert_enrichment_due(program_id="xpf", kind="report_run", programs_root=programs_root, now=_NOW)
            assert fired is False

        assert read_alerts("xpf", programs_root=programs_root) == ()

    def test_maybe_alert_enrichment_due_fires_on_the_nth_tick(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        programs_root = tmp_path / "programs"
        self._patch_policy(monkeypatch, nudge_every=5, report_every=3)

        results = [
            maybe_alert_enrichment_due(program_id="xpf", kind="report_run", programs_root=programs_root, now=_NOW)
            for _ in range(3)
        ]

        assert results == [False, False, True]
        alerts = read_alerts("xpf", programs_root=programs_root)
        assert len(alerts) == 1
        assert alerts[0].category == "people_enrichment_due"
        assert "vertex kb people enrich --program xpf" in alerts[0].next_command

    def test_maybe_alert_enrichment_due_kinds_are_independent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        programs_root = tmp_path / "programs"
        self._patch_policy(monkeypatch, nudge_every=2, report_every=100)

        fired_report = maybe_alert_enrichment_due(program_id="xpf", kind="report_run", programs_root=programs_root, now=_NOW)
        fired_nudge_1 = maybe_alert_enrichment_due(program_id="xpf", kind="nudge_run", programs_root=programs_root, now=_NOW)
        fired_nudge_2 = maybe_alert_enrichment_due(program_id="xpf", kind="nudge_run", programs_root=programs_root, now=_NOW)

        assert fired_report is False
        assert fired_nudge_1 is False
        assert fired_nudge_2 is True

    def test_maybe_alert_enrichment_due_never_fires_when_disabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        programs_root = tmp_path / "programs"
        self._patch_policy(monkeypatch, nudge_every=None, report_every=None)

        results = [
            maybe_alert_enrichment_due(program_id="xpf", kind="report_run", programs_root=programs_root, now=_NOW)
            for _ in range(10)
        ]

        assert all(result is False for result in results)
        assert read_alerts("xpf", programs_root=programs_root) == ()

    def test_resolve_enrichment_due_alert_resolves_an_open_alert(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        programs_root = tmp_path / "programs"
        self._patch_policy(monkeypatch, nudge_every=1, report_every=1)
        maybe_alert_enrichment_due(program_id="xpf", kind="report_run", programs_root=programs_root, now=_NOW)

        resolved = resolve_enrichment_due_alert(program_id="xpf", programs_root=programs_root, now=_NOW)

        assert resolved is True
        assert read_alerts("xpf", programs_root=programs_root) == ()

    def test_resolve_enrichment_due_alert_returns_false_when_nothing_open(self, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"

        resolved = resolve_enrichment_due_alert(program_id="xpf", programs_root=programs_root, now=_NOW)

        assert resolved is False

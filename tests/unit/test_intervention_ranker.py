from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.claim_tracker import ClaimAssessment
from src.core.intervention_ranker import rank_brief_interventions
from src.core.models import Confidence
from src.core.models_v2 import ClaimEntry, Contradiction, ContradictionPacket, DataSourceType, DecisionAsk, IncidentEntry, ResolvedContradiction


def test_rank_brief_interventions_prefers_lower_edit_magnitude_for_similar_claims(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_dir = programs_root / "acme" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "edit_patterns.jsonl").write_text(
        "\n".join(
            [
                '{"section_id": "deployment", "author_override_magnitude": 0.85}',
                '{"section_id": "repair", "author_override_magnitude": 0.20}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    as_of = datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)

    interventions = rank_brief_interventions(
        "acme",
        claim_assessments=(
            ClaimAssessment(
                claim=ClaimEntry(
                    id="c-deploy",
                    program_id="acme",
                    edition_id="acme_weekly",
                    issue_number=77,
                    workstream_id="deployment",
                    text="Deployment claim needs review.",
                    entity_refs=("WI:123",),
                    claim_date=date(2026, 5, 19),
                    owner_alias="operator",
                    due_date=date(2026, 5, 24),
                ),
                effective_status="open",
            ),
            ClaimAssessment(
                claim=ClaimEntry(
                    id="c-repair",
                    program_id="acme",
                    edition_id="acme_weekly",
                    issue_number=77,
                    workstream_id="repair",
                    text="Repair claim needs review.",
                    entity_refs=("WI:124",),
                    claim_date=date(2026, 5, 19),
                    owner_alias="operator",
                    due_date=date(2026, 5, 24),
                ),
                effective_status="open",
            ),
        ),
        decision_asks=(
            DecisionAsk(
                id="d-1",
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                text="Need LT decision on contingency scope.",
                entity_refs=(),
                ask_date=date(2026, 5, 5),
                owner_alias="operator",
            ),
        ),
        contradiction_packets=(
            ContradictionPacket(
                work_item_id=123,
                workstream_id="deployment",
                contradictions=(
                    Contradiction(
                        field="target_date",
                        source_a="journal",
                        source_b="ado",
                        summary="Claim date disagrees with current ADO target date.",
                        confidence=Confidence.HIGH,
                        evidence_refs=("c-deploy", "WI:123"),
                    ),
                ),
                confidence=Confidence.HIGH,
                recommended_resolution=ResolvedContradiction(
                    winning_source=DataSourceType.WORKIQ,
                    confidence=Confidence.HIGH,
                    rationale="Owner history shows persistent ADO optimism.",
                    evidence_refs=("c-deploy",),
                ),
                generated_at=as_of,
            ),
        ),
        salience_weights={"deployment": 0.3, "repair": 0.3},
        as_of=as_of,
        programs_root=programs_root,
        incident_entries=(),
        limit=4,
    )

    assert interventions[0].title == "Review contradiction on deployment"
    assert interventions[0].proposal_id == "contradiction-wi-123-review"
    assert interventions[0].confidence is Confidence.HIGH
    assert interventions[0].source_hash.startswith("sha256:")
    assert interventions[1].title == "Stage nudge for decision ask d-1"
    assert interventions[1].proposal_id == "decision-ask-d-1-nudge"
    assert interventions[1].confidence is Confidence.NONE
    assert interventions[2].title == "Review claim c-repair"
    assert interventions[2].confidence is Confidence.NONE
    assert interventions[3].title == "Review claim c-deploy"
    assert interventions[3].confidence is Confidence.NONE


def test_rank_brief_interventions_stages_readiness_review_for_recent_incident_learning(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme" / "journal").mkdir(parents=True, exist_ok=True)
    as_of = datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)

    interventions = rank_brief_interventions(
        "acme",
        claim_assessments=(),
        decision_asks=(),
        contradiction_packets=(),
        salience_weights={"deployment": 0.3},
        as_of=as_of,
        programs_root=programs_root,
        incident_entries=(
            IncidentEntry(
                program_id="acme",
                incident_id="22001",
                signal_id="icm-22001",
                observed_at=datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 5, 20, 15, 5, tzinfo=timezone.utc),
                belief_change_summary="IcM 22001: Deployment capacity rollback exposed hidden coupling.",
                workstream_id="deployment",
                severity=2,
                ado_entity_refs=("WI:123",),
                confidence=Confidence.HIGH,
            ),
        ),
        limit=3,
    )

    assert len(interventions) == 1
    assert interventions[0].title == "Review readiness after incident learning on deployment"
    assert interventions[0].proposal_id == "incident-deployment-wi-123-readiness"
    assert interventions[0].confidence is Confidence.HIGH
    assert interventions[0].command == "vertex readiness fetch --program acme"
    assert "WI:123: Deployment capacity rollback exposed hidden coupling." in interventions[0].evidence_summary


def test_rank_brief_interventions_prioritizes_linked_decision_ask_when_incident_learning_matches_ref(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme" / "journal").mkdir(parents=True, exist_ok=True)
    as_of = datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)

    interventions = rank_brief_interventions(
        "acme",
        claim_assessments=(
            ClaimAssessment(
                claim=ClaimEntry(
                    id="c-1",
                    program_id="acme",
                    edition_id="acme_weekly",
                    issue_number=77,
                    workstream_id="repair",
                    text="Repair claim needs review.",
                    entity_refs=("WI:124",),
                    claim_date=date(2026, 5, 19),
                    owner_alias="operator",
                    due_date=date(2026, 5, 24),
                ),
                effective_status="stale",
            ),
        ),
        decision_asks=(
            DecisionAsk(
                id="d-incident",
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                text="Need LT decision on deployment rollback guardrails.",
                entity_refs=("WI:123",),
                ask_date=date(2026, 5, 5),
                owner_alias="operator",
            ),
        ),
        contradiction_packets=(),
        salience_weights={"deployment": 0.3, "repair": 0.3},
        as_of=as_of,
        programs_root=programs_root,
        incident_entries=(
            IncidentEntry(
                program_id="acme",
                incident_id="22001",
                signal_id="icm-22001",
                observed_at=datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 5, 20, 15, 5, tzinfo=timezone.utc),
                belief_change_summary="IcM 22001: Deployment capacity rollback exposed hidden coupling.",
                workstream_id="deployment",
                severity=2,
                ado_entity_refs=("WI:123",),
                confidence=Confidence.HIGH,
            ),
        ),
        limit=3,
    )

    assert interventions[0].proposal_id == "decision-ask-d-incident-nudge"
    assert interventions[0].title == "Stage nudge for decision ask d-incident"
    assert interventions[0].confidence is Confidence.HIGH
    assert "Recent incident learning: WI:123: Deployment capacity rollback exposed hidden coupling." in interventions[0].evidence_summary
    assert interventions[1].proposal_id == "incident-deployment-wi-123-readiness"
    assert interventions[1].confidence is Confidence.HIGH


def test_rank_brief_interventions_surfaces_recurring_incident_class_proposal(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme" / "journal").mkdir(parents=True, exist_ok=True)
    as_of = datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)

    interventions = rank_brief_interventions(
        "acme",
        claim_assessments=(),
        decision_asks=(),
        contradiction_packets=(),
        salience_weights={"deployment": 0.6},
        as_of=as_of,
        programs_root=programs_root,
        incident_entries=(
            IncidentEntry(
                program_id="acme",
                incident_id="22001",
                signal_id="icm-22001",
                observed_at=datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 5, 20, 15, 5, tzinfo=timezone.utc),
                belief_change_summary="IcM 22001: WI:123 rollout validation regressed under failover.",
                workstream_id="deployment",
                severity=2,
                ado_entity_refs=("WI:123",),
                confidence=Confidence.HIGH,
            ),
            IncidentEntry(
                program_id="acme",
                incident_id="22002",
                signal_id="icm-22002",
                observed_at=datetime(2026, 5, 21, 15, 0, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 5, 21, 15, 5, tzinfo=timezone.utc),
                belief_change_summary="IcM 22002: WI:456 rollout validation regressed after failover.",
                workstream_id="deployment",
                severity=2,
                ado_entity_refs=("WI:456",),
                confidence=Confidence.MEDIUM,
            ),
            IncidentEntry(
                program_id="acme",
                incident_id="22003",
                signal_id="icm-22003",
                observed_at=datetime(2026, 5, 22, 15, 0, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 5, 22, 15, 5, tzinfo=timezone.utc),
                belief_change_summary="IcM 22003: WI:789 rollout validation regressed during failover drills.",
                workstream_id="deployment",
                severity=2,
                ado_entity_refs=("WI:789",),
                confidence=Confidence.MEDIUM,
            ),
        ),
        limit=3,
    )

    assert interventions[0].proposal_id.startswith("incident-class-")
    assert interventions[0].title == "Review recurring incident class on deployment"
    assert "Incident class" in interventions[0].evidence_summary

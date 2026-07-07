from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.analytics_store import AutonomyAuditRecord, append_autonomy_audit_record, replace_contradiction_state
from src.core.ai_proposal_store import append_ai_proposal, build_ai_proposal_id
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import AIProposal, AIProposalStatus, Contradiction, ContradictionPacket, DataSourceType, ResolvedContradiction, WorkstreamSynthesis


runner = CliRunner()


def test_maturity_check_cli_reports_scoped_criteria(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "editions"
    _write_program_files(programs_root, editions_root)

    for issue_number in range(1, 11):
        created_at = datetime(2026, 5, issue_number, 12, 0, tzinfo=timezone.utc)
        status = AIProposalStatus.ACCEPTED if issue_number < 10 else AIProposalStatus.REJECTED
        append_ai_proposal(
            "acme",
            _proposal(
                proposal_id=build_ai_proposal_id("acme", workstream_id=f"networking-{issue_number}", created_at=created_at),
                workstream_id=f"networking-{issue_number}",
                created_at=created_at,
                status=status,
                edition_id="acme_weekly",
                issue_number=issue_number,
            ),
            programs_root=programs_root,
        )

    append_ai_proposal(
        "acme",
        _proposal(
            proposal_id=build_ai_proposal_id(
                "acme",
                workstream_id="legacy-networking",
                created_at=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
            ),
            workstream_id="legacy-networking",
            created_at=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
            status=AIProposalStatus.ACCEPTED,
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr("src.commands.maturity_check.EDITIONS_ROOT", editions_root)
    monkeypatch.setattr("src.commands.maturity_check.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["maturity-check", "--edition", "acme_weekly", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["edition"] == "acme_weekly"
    assert payload["current_maturity_level"] == 1
    assert payload["proposal_confidence_summary"] == "high=10"
    assert payload["scoped_issue_numbers"] == list(range(1, 11))
    criteria = {entry["criterion_id"]: entry for entry in payload["criteria"]}
    assert criteria["L0-1"]["status"] == "passed"
    assert criteria["L1-1"]["status"] == "passed"
    assert criteria["L2-1"]["status"] == "passed"
    assert criteria["L2-2"]["status"] == "pending"
    assert criteria["L2-3"]["status"] == "unavailable"
    assert criteria["L2-4"]["status"] == "passed"
    assert criteria["L3-1"]["status"] == "pending"
    assert criteria["L4-1"]["status"] == "pending"
    assert any("Graph app-only auth" in limitation for limitation in payload["data_limitations"])


def test_maturity_check_reads_persisted_graph_auth_deferral(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "editions"
    _write_program_files(programs_root, editions_root)
    (programs_root / "acme" / "capability_status.yaml").write_text(
        "\n".join(
            (
                "schema_version: '1.0'",
                "capabilities:",
                "  - id: graph_app_only_auth",
                "    status: deferred",
                "    summary: Graph app-only auth is explicitly deferred pending later-wave activation.",
                "    degradation: Graph-backed status sources and L2 governance graduation remain unavailable.",
                "    last_reviewed_on: 2026-05-14",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("src.commands.maturity_check.EDITIONS_ROOT", editions_root)
    monkeypatch.setattr("src.commands.maturity_check.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["maturity-check", "--edition", "acme_weekly", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    criteria = {entry["criterion_id"]: entry for entry in payload["criteria"]}
    assert criteria["L2-3"]["status"] == "deferred"
    assert "later-wave activation" in criteria["L2-3"]["detail"]
    assert any("later-wave activation" in limitation for limitation in payload["data_limitations"])


def test_maturity_check_human_and_csv_include_proposal_confidence_summary(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "editions"
    _write_program_files(programs_root, editions_root)

    append_ai_proposal(
        "acme",
        _proposal(
            proposal_id=build_ai_proposal_id(
                "acme",
                workstream_id="networking-high",
                created_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            ),
            workstream_id="networking-high",
            created_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            status=AIProposalStatus.ACCEPTED,
            edition_id="acme_weekly",
            issue_number=1,
        ),
        programs_root=programs_root,
    )
    append_ai_proposal(
        "acme",
        _proposal(
            proposal_id=build_ai_proposal_id(
                "acme",
                workstream_id="networking-low",
                created_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            ),
            workstream_id="networking-low",
            created_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            status=AIProposalStatus.REJECTED,
            edition_id="acme_weekly",
            issue_number=2,
            confidence=Confidence.LOW,
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr("src.commands.maturity_check.EDITIONS_ROOT", editions_root)
    monkeypatch.setattr("src.commands.maturity_check.PROGRAMS_ROOT", programs_root)

    human_result = runner.invoke(app, ["maturity-check", "--edition", "acme_weekly"])
    csv_result = runner.invoke(app, ["maturity-check", "--edition", "acme_weekly", "--format", "csv"])

    assert human_result.exit_code == 0
    assert "Confidence mix:  high=1, low=1" in human_result.stdout
    assert csv_result.exit_code == 0
    assert "proposal_confidence_summary" in csv_result.stdout
    assert "high=1, low=1" in csv_result.stdout
    assert "l1_1_status" in csv_result.stdout
    assert "l4_2_status" in csv_result.stdout


def test_maturity_check_counts_superseded_proposals_as_overrides(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "editions"
    _write_program_files(programs_root, editions_root)

    for issue_number in range(1, 21):
        created_at = datetime(2026, 5, min(issue_number, 28), 12, 0, tzinfo=timezone.utc)
        append_ai_proposal(
            "acme",
            _proposal(
                proposal_id=build_ai_proposal_id("acme", workstream_id=f"networking-{issue_number}", created_at=created_at),
                workstream_id=f"networking-{issue_number}",
                created_at=created_at,
                status=AIProposalStatus.ACCEPTED,
                edition_id="acme_weekly",
                issue_number=issue_number,
            ),
            programs_root=programs_root,
        )

    for offset in range(3):
        created_at = datetime(2026, 5, 28, 13, offset, tzinfo=timezone.utc)
        append_ai_proposal(
            "acme",
            _proposal(
                proposal_id=build_ai_proposal_id("acme", workstream_id=f"deployment-{offset}", created_at=created_at),
                workstream_id=f"deployment-{offset}",
                created_at=created_at,
                status=AIProposalStatus.SUPERSEDED,
                edition_id="acme_weekly",
                issue_number=20,
            ),
            programs_root=programs_root,
        )

    monkeypatch.setattr("src.commands.maturity_check.EDITIONS_ROOT", editions_root)
    monkeypatch.setattr("src.commands.maturity_check.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["maturity-check", "--edition", "acme_weekly", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    criteria = {entry["criterion_id"]: entry for entry in payload["criteria"]}
    assert criteria["L2-2"]["status"] == "pending"
    assert "Issue 020 recorded 3 overrides" in criteria["L2-2"]["detail"]
    assert "0 issue(s)" in criteria["L2-2"]["detail"]


def test_maturity_check_passes_l3_gates_with_audited_bounded_writes(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "editions"
    _write_program_files(programs_root, editions_root, maturity_level=3)

    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="acme",
            action_id="bounded-write-1",
            level="l3",
            author_alias="operator",
            subject_alias="owner",
            evidence_refs=("WI:1001",),
            policy_rule="ado_comment_apply",
            accepted=True,
            applied_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
            action_type="ado_comment_apply",
            blast_radius="1 comment on 1 work item",
            rollback_mechanism="Delete the authored comment.",
            prior_acceptance_rate=0.92,
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr("src.commands.maturity_check.EDITIONS_ROOT", editions_root)
    monkeypatch.setattr("src.commands.maturity_check.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["maturity-check", "--edition", "acme_weekly", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    criteria = {entry["criterion_id"]: entry for entry in payload["criteria"]}
    assert criteria["L3-1"]["status"] == "passed"
    assert criteria["L3-2"]["status"] == "passed"
    assert "ado_comment_apply" in criteria["L3-1"]["detail"]


def test_maturity_check_l4_requires_contradiction_free_window(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "editions"
    _write_program_files(programs_root, editions_root, maturity_level=4)

    for cycle in range(10):
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id="acme",
                action_id=f"scheduled-write-{cycle}",
                level="l3",
                author_alias="operator",
                subject_alias="owner",
                evidence_refs=(f"WI:{1000 + cycle}",),
                policy_rule="ado_comment_apply",
                accepted=True,
                applied_at=datetime(2026, 5, 1 + cycle, 12, 0, tzinfo=timezone.utc),
                action_type="ado_comment_apply",
                blast_radius="1 comment on 1 work item",
                rollback_mechanism="Delete the authored comment.",
                prior_acceptance_rate=0.95,
            ),
            programs_root=programs_root,
        )

    replace_contradiction_state(
        "acme",
        (
            ContradictionPacket(
                work_item_id=1001,
                workstream_id="ws_demo",
                contradictions=(
                    Contradiction(
                        field="target_date",
                        source_a="ado/target_date",
                        source_b="workiq/signal",
                        summary="WorkIQ implies a later date than ADO.",
                        confidence=Confidence.HIGH,
                        evidence_refs=("WI:1001", "signal-1"),
                    ),
                ),
                confidence=Confidence.HIGH,
                recommended_resolution=ResolvedContradiction(
                    winning_source=DataSourceType.WORKIQ,
                    confidence=Confidence.HIGH,
                    rationale="Prefer fresher corroborated signal evidence.",
                    evidence_refs=("WI:1001", "signal-1"),
                ),
                generated_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
            ),
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr("src.commands.maturity_check.EDITIONS_ROOT", editions_root)
    monkeypatch.setattr("src.commands.maturity_check.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["maturity-check", "--edition", "acme_weekly", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    criteria = {entry["criterion_id"]: entry for entry in payload["criteria"]}
    assert criteria["L4-1"]["status"] == "passed"
    assert criteria["L4-2"]["status"] == "failed"
    assert "contradiction packet" in criteria["L4-2"]["detail"]


def _proposal(
    *,
    proposal_id: str,
    workstream_id: str,
    created_at: datetime,
    status: AIProposalStatus,
    edition_id: str | None = None,
    issue_number: int | None = None,
    confidence: Confidence = Confidence.HIGH,
) -> AIProposal:
    resolved_at = created_at if status is not AIProposalStatus.PENDING else None
    resolved_by = "operator" if resolved_at is not None else None
    return AIProposal(
        id=proposal_id,
        workstream_id=workstream_id,
        synthesis=WorkstreamSynthesis(
            workstream_id=workstream_id,
            overall_assessment="Networking remains the blocking lane.",
            proposed_risk=RiskLevel.HIGH,
            confidence=confidence,
            key_findings=("Target date slipped twice.",),
            evidence_refs=("sig-1",),
            open_questions=("Who owns the final sign-off?",),
            recommended_actions=("Close the servicing checkpoint.",),
        ),
        status=status,
        created_at=created_at,
        resolved_at=resolved_at,
        resolved_by=resolved_by,
        edition_id=edition_id,
        issue_number=issue_number,
    )


def _write_program_files(programs_root: Path, editions_root: Path, *, maturity_level: int = 1) -> None:
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    editions_root.mkdir(parents=True)

    (program_dir / "program.yaml").write_text(
        """
schema_version: '3.0'
id: acme
name: Acme
maturity_level: {maturity_level}
ado:
  organization: your-org
  project: One
kusto:
  enabled: true
m365:
  enabled: true
""".strip().format(maturity_level=maturity_level)
        + "\n",
        encoding="utf-8",
    )
    (program_dir / "workstreams.yaml").write_text("workstreams: []\n", encoding="utf-8")
    (program_dir / "scorecards.yaml").write_text("scorecards: []\n", encoding="utf-8")
    (editions_root / "acme_weekly.yaml").write_text(
        """
schema_version: '2.0'
id: acme_weekly
program_id: acme
name: Acme Weekly
type: detailed
altitude: newsletter
cadence: weekly
""".strip()
        + "\n",
        encoding="utf-8",
    )

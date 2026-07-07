from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from typer.testing import CliRunner

import cli
from src.core.claim_extraction_calibration_store import ClaimExtractionCalibrationRecord, append_claim_extraction_calibration_record
from src.ai.edit_learner import EditPattern, append_edit_patterns
from src.commands.trust import build_trust_graduation_metrics, build_trust_report, render_trust_report
from src.core.analytics_store import AutonomyAuditRecord, append_autonomy_audit_record


runner = CliRunner()
FROZEN_NOW = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)


def test_build_trust_report_matches_golden_fixture(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_trust_workspace(programs_root)

    report = build_trust_report(
        "acme",
        window_issues=10,
        programs_root=programs_root,
        as_of=FROZEN_NOW,
    )
    rendered = render_trust_report(report)
    fixture_path = Path(__file__).resolve().parents[1] / "golden" / "trust_output.txt"

    assert rendered == fixture_path.read_text(encoding="utf-8").rstrip("\n")


def test_trust_command_supports_human_and_json(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_trust_workspace(programs_root)
    monkeypatch.setattr("src.commands.trust.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.trust._utc_now", lambda: FROZEN_NOW)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    human_result = runner.invoke(cli.app, ["trust", "--program", "acme"])
    json_result = runner.invoke(cli.app, ["trust", "--program", "acme", "--format", "json"])

    assert human_result.exit_code == 0
    assert "Trust Calibration - acme" in human_result.stdout
    assert "Blurb generation: override=0.1000 | calibration=0.9000 | samples=3 | trust=L2" in human_result.stdout
    assert "Claim extraction: agreement=0.9000 | avg_difference=1.00 | samples=10 | calibration_samples=12 | trust=L2" in human_result.stdout
    assert "Decision ask escalation: accepted=10/10 (100%) | level=l3 | trust=L3" in human_result.stdout
    assert "Salience-calibration bridge" in human_result.stdout
    assert "repair: slip_modifier=+0.18 | attention_weight=0.31 | Forecast slip pressure is high while editorial attention remains low." in human_result.stdout

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["program_id"] == "acme"
    assert payload["generated_at"] == FROZEN_NOW.isoformat()
    assert payload["editorial"][0]["label"] == "Blurb generation"
    assert payload["editorial"][0]["trust_level"] == "L2"
    assert payload["claim_extraction"][0]["action_type"] == "claim_extraction"
    assert payload["claim_extraction"][0]["average_difference_count"] == 1.0
    assert payload["claim_extraction"][0]["trust_level"] == "L2"
    assert payload["attention_gaps"][0]["bridge_summary"].startswith("Forecast slip pressure is high")


def test_build_trust_report_separates_incident_linked_decision_ask_nudges(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_trust_workspace(programs_root)
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="acme",
            action_id="incident-nudge-1",
            level="l2",
            author_alias="owner",
            subject_alias="priya",
            evidence_refs=("decision_ask:incident-1", "ICM:22001"),
            policy_rule="decision_ask_nudge",
            accepted=True,
            applied_at=datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc),
            action_type="decision_ask_nudge",
            blast_radius="1 draft to 1 recipient",
            rollback_mechanism="Delete draft.",
            prior_acceptance_rate=1.0,
        ),
        programs_root=programs_root,
    )

    report = build_trust_report(
        "acme",
        programs_root=programs_root,
        as_of=FROZEN_NOW,
    )
    rendered = render_trust_report(report)

    assert "- Decision ask nudge: accepted=3/3 (100%) | level=l2 | trust=L2 | last=2026-05-15T09:00:00+00:00" in rendered
    assert "- Incident-linked decision ask nudge: accepted=1/1 (100%) | level=l2 | trust=bootstrap | last=2026-05-16T09:00:00+00:00" in rendered


def test_trust_command_supports_incident_linked_nudge_action_filter(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_trust_workspace(programs_root)
    monkeypatch.setattr("src.commands.trust.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.trust._utc_now", lambda: FROZEN_NOW)
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="acme",
            action_id="incident-nudge-1",
            level="l2",
            author_alias="owner",
            subject_alias="priya",
            evidence_refs=("decision_ask:incident-1", "ICM:22001"),
            policy_rule="decision_ask_nudge",
            accepted=True,
            applied_at=datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc),
            action_type="decision_ask_nudge",
            blast_radius="1 draft to 1 recipient",
            rollback_mechanism="Delete draft.",
            prior_acceptance_rate=1.0,
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        cli.app,
        ["trust", "--program", "acme", "--action", "decision_ask_nudge_incident", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["action_filter"] == "decision_ask_nudge_incident"
    assert payload["autonomy"] == [
        {
            "action_type": "decision_ask_nudge_incident",
            "label": "Incident-linked decision ask nudge",
            "latest_level": "l2",
            "sample_count": 1,
            "accepted_count": 1,
            "acceptance_rate": 1.0,
            "trust_level": "bootstrap",
            "last_applied_at": "2026-05-16T09:00:00+00:00",
        }
    ]


def test_trust_workstream_slice_keeps_incident_linked_nudges_with_workstream_refs(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_trust_workspace(programs_root)
    monkeypatch.setattr("src.commands.trust.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.trust._utc_now", lambda: FROZEN_NOW)
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="acme",
            action_id="incident-nudge-1",
            level="l2",
            author_alias="owner",
            subject_alias="priya",
            evidence_refs=("decision_ask:incident-1", "ICM:22001", "workstream:repair"),
            policy_rule="decision_ask_nudge",
            accepted=True,
            applied_at=datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc),
            action_type="decision_ask_nudge",
            blast_radius="1 draft to 1 recipient",
            rollback_mechanism="Delete draft.",
            prior_acceptance_rate=1.0,
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        cli.app,
        ["trust", "--program", "acme", "--slice", "workstream", "--action", "decision_ask_nudge_incident", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["slice"] == "workstream"
    assert payload["action_filter"] == "decision_ask_nudge_incident"
    assert payload["rows"] == [
        {
            "slice_key": "repair",
            "sample_count": 1,
            "accepted_count": 1,
            "acceptance_rate": 1.0,
            "trust_level": "bootstrap",
        }
    ]


def test_trust_command_supports_workstream_dri_and_time_slices(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_trust_workspace(programs_root)
    monkeypatch.setattr("src.commands.trust.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.trust._utc_now", lambda: FROZEN_NOW)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    workstream_result = runner.invoke(cli.app, ["trust", "--program", "acme", "--slice", "workstream"])
    dri_result = runner.invoke(cli.app, ["trust", "--program", "acme", "--slice", "dri", "--format", "json"])
    time_result = runner.invoke(cli.app, ["trust", "--program", "acme", "--slice", "time", "--window", "8w"])

    assert workstream_result.exit_code == 0
    assert "Slice: workstream" in workstream_result.stdout
    assert "- acme: accepted=10/10 (100%) | trust=L3" in workstream_result.stdout

    assert dri_result.exit_code == 0
    dri_payload = json.loads(dri_result.stdout)
    assert dri_payload["slice"] == "dri"
    assert dri_payload["rows"] == [
        {
            "slice_key": "priya",
            "sample_count": 13,
            "accepted_count": 13,
            "acceptance_rate": 1.0,
            "trust_level": "L3",
        }
    ]

    assert time_result.exit_code == 0
    assert "Slice: time" in time_result.stdout
    assert "Window: 8w" in time_result.stdout
    assert "- 2026-W18: accepted=3/3 (100%) | trust=L2" in time_result.stdout
    assert "- 2026-W19: accepted=7/7 (100%) | trust=L2" in time_result.stdout
    assert "- 2026-W20: accepted=3/3 (100%) | trust=L2" in time_result.stdout


def test_trust_command_supports_action_filter(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_trust_workspace(programs_root)
    monkeypatch.setattr("src.commands.trust.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.trust._utc_now", lambda: FROZEN_NOW)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    human_result = runner.invoke(cli.app, ["trust", "--program", "acme", "--action", "decision_ask_escalation"])
    json_result = runner.invoke(
        cli.app,
        ["trust", "--program", "acme", "--action", "workstream_blurb", "--format", "json"],
    )

    assert human_result.exit_code == 0
    assert "Action filter: decision_ask_escalation" in human_result.stdout
    assert "Editorial\n---------\n- None" in human_result.stdout
    assert "Decision ask escalation: accepted=10/10 (100%) | level=l3 | trust=L3" in human_result.stdout
    assert "Decision ask nudge" not in human_result.stdout

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["action_filter"] == "workstream_blurb"
    assert payload["editorial"] == [
        {
            "task_type": "workstream_blurb",
            "label": "Blurb generation",
            "sample_count": 3,
            "average_override_magnitude": 0.1,
            "calibration_score": 0.9,
            "trust_level": "L2",
        }
    ]
    assert payload["autonomy"] == []


def test_trust_command_supports_claim_extraction_action_filter(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_trust_workspace(programs_root)
    monkeypatch.setattr("src.commands.trust.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.trust._utc_now", lambda: FROZEN_NOW)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    result = runner.invoke(cli.app, ["trust", "--program", "acme", "--action", "claim_extraction", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["editorial"] == []
    assert payload["claim_extraction"] == [
        {
            "action_type": "claim_extraction",
            "label": "Claim extraction",
            "sample_count": 10,
            "calibration_sample_count": 12,
            "agreement_rate": 0.9,
            "average_difference_count": 1.0,
            "trust_level": "L2",
            "last_recorded_at": "2026-05-12T12:00:00+00:00",
        }
    ]
    assert payload["autonomy"] == []


def test_trust_command_supports_graduation_metrics(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_graduation_metrics_workspace(programs_root)
    monkeypatch.setattr("src.commands.trust.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(cli.app, ["trust", "--program", "acme", "--graduation-metrics"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "program_id": "acme",
        "window_issues": 5,
        "rewrite_rate": 0.03,
        "qg_pass_rate": 0.8,
        "proposal_acceptance_rate": 0.8,
        "commitment_leakage_rate": None,
        "source_coverage": {},
        "source_diversity": 0,
        "source_diversity_met": False,
    }


def test_build_trust_graduation_metrics_scopes_to_requested_confirmed_issue_window(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_graduation_metrics_workspace(programs_root)

    report = build_trust_graduation_metrics("acme", window_issues=3, programs_root=programs_root)

    assert report.window_issues == 3
    assert report.rewrite_rate == 0.04
    assert report.qg_pass_rate == 0.7778
    assert report.proposal_acceptance_rate == 0.6667


def _seed_trust_workspace(programs_root: Path) -> None:
    program_dir = programs_root / "acme"
    (program_dir / "journal").mkdir(parents=True, exist_ok=True)
    (program_dir / "_feedback").mkdir(parents=True, exist_ok=True)

    append_edit_patterns(
        "acme",
        (
            EditPattern(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                section_id="repair",
                recorded_at=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
                summary="Small blurb edit.",
                before_excerpt="Draft repair blurb.",
                after_excerpt="Confirmed repair blurb.",
                before_word_count=4,
                after_word_count=4,
                task_type="workstream_blurb",
                prompt_version="workstream_blurb.v1",
                author_override_magnitude=0.05,
            ),
            EditPattern(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=78,
                section_id="repair",
                recorded_at=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
                summary="Small blurb edit.",
                before_excerpt="Draft repair blurb.",
                after_excerpt="Confirmed repair blurb.",
                before_word_count=4,
                after_word_count=4,
                task_type="workstream_blurb",
                prompt_version="workstream_blurb.v1",
                author_override_magnitude=0.1,
            ),
            EditPattern(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=79,
                section_id="repair",
                recorded_at=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
                summary="Small blurb edit.",
                before_excerpt="Draft repair blurb.",
                after_excerpt="Confirmed repair blurb.",
                before_word_count=4,
                after_word_count=4,
                task_type="workstream_blurb",
                prompt_version="workstream_blurb.v1",
                author_override_magnitude=0.15,
            ),
            EditPattern(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                section_id="exec_summary",
                recorded_at=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
                summary="Heavier exec edit.",
                before_excerpt="Draft exec summary.",
                after_excerpt="Confirmed exec summary.",
                before_word_count=3,
                after_word_count=3,
                task_type="exec_summary",
                prompt_version="exec_summary.v1",
                author_override_magnitude=0.25,
            ),
            EditPattern(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=78,
                section_id="exec_summary",
                recorded_at=datetime(2026, 5, 11, 11, 0, tzinfo=timezone.utc),
                summary="Heavier exec edit.",
                before_excerpt="Draft exec summary.",
                after_excerpt="Confirmed exec summary.",
                before_word_count=3,
                after_word_count=3,
                task_type="exec_summary",
                prompt_version="exec_summary.v1",
                author_override_magnitude=0.3,
            ),
            EditPattern(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=79,
                section_id="exec_summary",
                recorded_at=datetime(2026, 5, 12, 11, 0, tzinfo=timezone.utc),
                summary="Heavier exec edit.",
                before_excerpt="Draft exec summary.",
                after_excerpt="Confirmed exec summary.",
                before_word_count=3,
                after_word_count=3,
                task_type="exec_summary",
                prompt_version="exec_summary.v1",
                author_override_magnitude=0.35,
            ),
        ),
        programs_root=programs_root,
    )

    for index in range(3):
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id="acme",
                action_id=f"nudge-{index}",
                level="l2",
                author_alias="owner",
                subject_alias="priya",
                evidence_refs=(f"decision_ask:ask-{index}",),
                policy_rule="decision_ask_nudge",
                accepted=True,
                applied_at=datetime(2026, 5, 13 + index, 9, 0, tzinfo=timezone.utc),
                action_type="decision_ask_nudge",
                blast_radius="1 draft to 1 recipient",
                rollback_mechanism="Delete draft.",
                prior_acceptance_rate=1.0,
            ),
            programs_root=programs_root,
        )

    for index in range(10):
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id="acme",
                action_id=f"escalation-{index}",
                level="l3",
                author_alias="owner",
                subject_alias="priya",
                evidence_refs=(f"decision_ask:ask-{index}", "workstream:acme"),
                policy_rule="decision_ask_escalation",
                accepted=True,
                applied_at=datetime(2026, 5, 1 + index, 8, 0, tzinfo=timezone.utc),
                action_type="decision_ask_escalation",
                blast_radius="1 draft to 2 recipients",
                rollback_mechanism="Delete draft.",
                prior_acceptance_rate=1.0,
            ),
            programs_root=programs_root,
        )

    (program_dir / "_feedback" / "author_salience.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                'updated_at: "2026-05-20T11:00:00+00:00"',
                'author_alias: "owner"',
                'ema_alpha: 0.1',
                'min_weight: 0.2',
                'workstreams:',
                '  repair:',
                '    attention_weight: 0.31',
                '    sample_count: 3',
                '    average_override_magnitude: 0.10',
                '    last_event_at: "2026-05-12T10:00:00+00:00"',
                'dimensions: {}',
                '',
            )
        ),
        encoding="utf-8",
    )
    (program_dir / "_feedback" / "forecast_calibration.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                'updated_at: "2026-05-20T11:00:00+00:00"',
                'since: null',
                'confidence: "high"',
                'workstream_modifiers:',
                '  repair: 0.18',
                'dri_modifiers: {}',
                '',
            )
        ),
        encoding="utf-8",
    )

    for issue_number in range(1, 13):
        append_claim_extraction_calibration_record(
            ClaimExtractionCalibrationRecord(
                program_id="acme",
                issue_number=issue_number,
                recorded_at=datetime(2026, 5, issue_number, 12, 0, tzinfo=timezone.utc),
                mode="calibration",
                ai_claim_count=10,
                regex_claim_count=9,
                shared_claim_count=9,
                ai_only_count=1,
                regex_only_count=0,
                agreement_rate=0.9,
            ),
            programs_root=programs_root,
        )


def _seed_graduation_metrics_workspace(programs_root: Path) -> None:
    program_dir = programs_root / "acme"
    (program_dir / "journal").mkdir(parents=True, exist_ok=True)
    (program_dir / "archive" / "acme_weekly" / "manifests").mkdir(parents=True, exist_ok=True)

    append_edit_patterns(
        "acme",
        (
            _edit_pattern(issue_number=75, recorded_at=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc), override=0.01),
            _edit_pattern(issue_number=76, recorded_at=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc), override=0.02),
            _edit_pattern(issue_number=77, recorded_at=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc), override=0.03),
            _edit_pattern(issue_number=78, recorded_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc), override=0.04),
            _edit_pattern(issue_number=79, recorded_at=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc), override=0.05),
            _edit_pattern(issue_number=74, recorded_at=datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc), override=0.99),
        ),
        programs_root=programs_root,
    )

    qg_payloads = {
        75: {"QG-1": True, "QG-2": True, "QG-3": True},
        76: {"QG-1": True, "QG-2": True, "QG-3": False},
        77: {"QG-1": True, "QG-2": True, "QG-3": True},
        78: {"QG-1": True, "QG-2": True, "QG-3": True},
        79: {"QG-1": False, "QG-2": False, "QG-3": True},
    }
    for issue_number, qg_results in qg_payloads.items():
        _write_confirmed_manifest(
            program_dir / "archive" / "acme_weekly" / "manifests" / f"issue_{issue_number:03d}.json",
            issue_number=issue_number,
            ended_at=datetime(2026, 5, issue_number - 65, 18, 0, tzinfo=timezone.utc),
            qg_results=qg_results,
        )

    accepted_flags = {
        74: False,
        75: True,
        76: True,
        77: False,
        78: True,
        79: True,
        80: True,
    }
    for issue_number, accepted in accepted_flags.items():
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id="acme",
                action_id=f"proposal-{issue_number}",
                level="l2",
                author_alias="owner",
                subject_alias="priya",
                evidence_refs=(f"issue:{issue_number}", f"decision_ask:ask-{issue_number}"),
                policy_rule="decision_ask_nudge",
                accepted=accepted,
                applied_at=datetime(2026, 5, issue_number - 65, 9, 0, tzinfo=timezone.utc),
                action_type="decision_ask_nudge",
                blast_radius="1 draft to 1 recipient",
                rollback_mechanism="Delete draft.",
                prior_acceptance_rate=0.9,
            ),
            programs_root=programs_root,
        )


def _edit_pattern(*, issue_number: int, recorded_at: datetime, override: float) -> EditPattern:
    return EditPattern(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=issue_number,
        section_id="repair",
        recorded_at=recorded_at,
        summary="Calibration edit.",
        before_excerpt="Draft repair blurb.",
        after_excerpt="Confirmed repair blurb.",
        before_word_count=4,
        after_word_count=4,
        task_type="workstream_blurb",
        prompt_version="workstream_blurb.v1",
        author_override_magnitude=override,
    )


def _write_confirmed_manifest(path: Path, *, issue_number: int, ended_at: datetime, qg_results: dict[str, bool]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "manifest_id": f"manifest-{issue_number}",
                "issue_number": issue_number,
                "edition": "acme_weekly",
                "started_at": ended_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "config_hash": "sha256:config",
                "snapshot_hash": "sha256:snapshot",
                "html_hash": "sha256:html",
                "md_hash": "sha256:md",
                "ado_calls": 0,
                "ai_calls": 0,
                "ai_cost_usd": 0.0,
                "freshness_summary": {},
                "qg_results": qg_results,
                "git_sha": None,
                "ai_cost_by_model": {},
                "notes": [],
                "metadata": {},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

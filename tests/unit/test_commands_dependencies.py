from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from typer.testing import CliRunner
import yaml

import cli
from src.commands import dependencies as dependencies_command
from src.core.models import RiskLevel
from src.core.models_v2 import Dependency, DependencyStatus, DependencyType, TrajectoryPoint
from src.core.trajectory import backfill_trajectory_points


runner = CliRunner()


def test_dependencies_scout_writes_and_lists_proposals(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(dependencies_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(dependencies_command, "EDITIONS_ROOT", repo_root / "editions")
    monkeypatch.setattr(dependencies_command, "PROGRAMS_ROOT", (tmp_path / "programs"))

    result = runner.invoke(cli.app, ["dependencies", "scout", "--program", "acme"])

    assert result.exit_code == 0
    assert "medium confidence" in result.stdout
    golden = (Path(__file__).parents[1] / "golden" / "dependencies_scout_output.txt").read_text(encoding="utf-8")
    assert result.stdout == golden

    proposal_path = repo_root / "programs" / "acme" / "_feedback" / "dependency_proposals.yaml"
    assert proposal_path.exists()

    listed = runner.invoke(cli.app, ["dependencies", "list", "--program", "acme", "--format", "json"])
    payload = json.loads(listed.stdout)
    assert payload["proposal_count"] == 1
    assert payload["proposals"][0]["id"] == "dep-proposal-co-mention-101-202"


def test_dependencies_accept_promotes_dependency_and_marks_proposal_accepted(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(dependencies_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(dependencies_command, "EDITIONS_ROOT", repo_root / "editions")
    monkeypatch.setattr(dependencies_command, "PROGRAMS_ROOT", (tmp_path / "programs"))

    scout_result = runner.invoke(cli.app, ["dependencies", "scout", "--program", "acme"])
    assert scout_result.exit_code == 0

    accept_result = runner.invoke(
        cli.app,
        ["dependencies", "accept", "--program", "acme", "--id", "dep-proposal-co-mention-101-202"],
    )

    assert accept_result.exit_code == 0
    dependencies_payload = yaml.safe_load((repo_root / "programs" / "acme" / "dependencies.yaml").read_text(encoding="utf-8"))
    assert dependencies_payload["dependencies"][0]["id"] == "dep-scout-101-202"
    proposals_payload = yaml.safe_load((repo_root / "programs" / "acme" / "_feedback" / "dependency_proposals.yaml").read_text(encoding="utf-8"))
    assert proposals_payload["proposals"][0]["status"] == "accepted"


def test_dependencies_accept_persists_resolution_path(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(dependencies_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(dependencies_command, "EDITIONS_ROOT", repo_root / "editions")
    monkeypatch.setattr(dependencies_command, "PROGRAMS_ROOT", (tmp_path / "programs"))

    scout_result = runner.invoke(cli.app, ["dependencies", "scout", "--program", "acme"])
    assert scout_result.exit_code == 0

    accept_result = runner.invoke(
        cli.app,
        [
            "dependencies",
            "accept",
            "--program",
            "acme",
            "--id",
            "dep-proposal-co-mention-101-202",
            "--resolution-path",
            "intra_storage",
        ],
    )

    assert accept_result.exit_code == 0
    dependencies_payload = yaml.safe_load((repo_root / "programs" / "acme" / "dependencies.yaml").read_text(encoding="utf-8"))
    assert dependencies_payload["dependencies"][0]["resolution_path"] == "intra_storage"


def test_dependencies_dismiss_marks_proposal_dismissed(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(dependencies_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(dependencies_command, "EDITIONS_ROOT", repo_root / "editions")
    monkeypatch.setattr(dependencies_command, "PROGRAMS_ROOT", (tmp_path / "programs"))

    scout_result = runner.invoke(cli.app, ["dependencies", "scout", "--program", "acme"])
    assert scout_result.exit_code == 0

    dismiss_result = runner.invoke(
        cli.app,
        ["dependencies", "dismiss", "--program", "acme", "--id", "dep-proposal-co-mention-101-202"],
    )

    assert dismiss_result.exit_code == 0
    proposals_payload = yaml.safe_load((repo_root / "programs" / "acme" / "_feedback" / "dependency_proposals.yaml").read_text(encoding="utf-8"))
    assert proposals_payload["proposals"][0]["status"] == "dismissed"


def test_dependencies_scout_json_includes_eta_co_movement_proposal(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(dependencies_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(dependencies_command, "EDITIONS_ROOT", repo_root / "editions")
    monkeypatch.setattr(dependencies_command, "PROGRAMS_ROOT", (tmp_path / "programs"))
    backfill_trajectory_points(
        "acme",
        101,
        (
            TrajectoryPoint(date(2026, 5, 1), "Active", "alice", date(2026, 6, 1), RiskLevel.MEDIUM, "Acme\\UD"),
            TrajectoryPoint(date(2026, 5, 5), "Active", "alice", date(2026, 6, 8), RiskLevel.MEDIUM, "Acme\\UD"),
            TrajectoryPoint(date(2026, 5, 20), "Active", "alice", date(2026, 6, 16), RiskLevel.MEDIUM, "Acme\\UD"),
        ),
        programs_root=repo_root / "programs",
    )
    backfill_trajectory_points(
        "acme",
        202,
        (
            TrajectoryPoint(date(2026, 5, 2), "Active", "bob", date(2026, 6, 2), RiskLevel.MEDIUM, "Acme\\Fabrikam"),
            TrajectoryPoint(date(2026, 5, 8), "Active", "bob", date(2026, 6, 10), RiskLevel.MEDIUM, "Acme\\Fabrikam"),
            TrajectoryPoint(date(2026, 5, 24), "Active", "bob", date(2026, 6, 18), RiskLevel.MEDIUM, "Acme\\Fabrikam"),
        ),
        programs_root=repo_root / "programs",
    )

    result = runner.invoke(cli.app, ["dependencies", "scout", "--program", "acme", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    methods = {proposal["detection_method"] for proposal in payload["proposals"]}
    assert "co_mention" in methods
    assert "eta_co_movement" in methods


def test_dependencies_accept_uses_fact_store_projection_for_duplicate_check(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(
        dependencies_command,
        "load_dependency_proposals",
        lambda program_id, programs_root: (
            type(
                "Proposal",
                (),
                {
                    "id": "dep-proposal-1",
                },
            )(),
        ),
    )
    monkeypatch.setattr(
        dependencies_command,
        "dependency_proposal_to_dependency",
        lambda *args, **kwargs: Dependency(
            id="dep-1",
            from_program_id="demo",
            from_workstream_id="ws-demo",
            from_item_id=101,
            from_milestone_id=None,
            to_program_id="shared",
            to_workstream_id="ws-shared",
            to_item_id=202,
            to_milestone_id=None,
            dependency_type=DependencyType.BLOCKS,
            risk_if_broken="Delivery slips.",
            mitigation=None,
            status=DependencyStatus.ACTIVE,
            owner_alias="demo",
            resolution_path=None,
            planned_resolution_date=None,
            schedule_status=None,
        ),
    )
    monkeypatch.setattr(dependencies_command, "load_program_facts", lambda program_id, programs_root=None: sentinel)
    monkeypatch.setattr(
        dependencies_command,
        "project_dependencies",
        lambda snapshot: (
            Dependency(
                id="dep-1",
                from_program_id="demo",
                from_workstream_id="ws-demo",
                from_item_id=101,
                from_milestone_id=None,
                to_program_id="shared",
                to_workstream_id="ws-shared",
                to_item_id=202,
                to_milestone_id=None,
                dependency_type=DependencyType.BLOCKS,
                risk_if_broken="Delivery slips.",
                mitigation=None,
                status=DependencyStatus.ACTIVE,
                owner_alias="demo",
                resolution_path=None,
                planned_resolution_date=None,
                schedule_status=None,
            ),
        ),
    )

    result = runner.invoke(cli.app, ["dependencies", "accept", "--program", "demo", "--id", "dep-proposal-1"])

    assert result.exit_code == 2
    assert "already exists" in result.output


def _seed_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path
    (repo_root / "programs" / "acme" / "archive" / "acme_weekly").mkdir(parents=True, exist_ok=True)
    (repo_root / "programs" / "acme" / "journal").mkdir(parents=True, exist_ok=True)
    (repo_root / "programs" / "acme" / "_feedback").mkdir(parents=True, exist_ok=True)
    (repo_root / "editions").mkdir(parents=True, exist_ok=True)
    (repo_root / "output").mkdir(parents=True, exist_ok=True)

    (repo_root / "programs" / "acme" / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "3.0",
                "id": "acme",
                "name": "Acme",
                "ado": {
                    "organization": "org",
                    "project": "proj",
                    "area_paths": ["Acme\\UD", "Acme\\Fabrikam"],
                    "work_item_types": ["Feature"],
                    "excluded_states": [],
                    "date_window_days": 30,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (repo_root / "programs" / "acme" / "workstreams.yaml").write_text(
        yaml.safe_dump(
            {
                "workstreams": [
                    {"id": "ud", "name": "UD", "area_paths": ["Acme\\UD"]},
                    {"id": "fabrikam", "name": "Fabrikam", "area_paths": ["Acme\\Fabrikam"]},
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (repo_root / "programs" / "acme" / "scorecards.yaml").write_text(yaml.safe_dump({"scorecards": []}, sort_keys=False), encoding="utf-8")
    (repo_root / "editions" / "acme_weekly.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "acme_weekly",
                "program_id": "acme",
                "name": "Acme Weekly",
                "type": "detailed",
                "altitude": "program",
                "cadence": "weekly",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    snapshot_path = repo_root / "programs" / "acme" / "archive" / "acme_weekly" / "snapshots"
    snapshot_path.mkdir(parents=True, exist_ok=True)
    (snapshot_path / "issue_001.snapshot.json").write_text(
        json.dumps(
            {
                "issue_number": 1,
                "generated_at": "2026-05-21T12:00:00+00:00",
                "ado_data_as_of": "2026-05-21T12:00:00+00:00",
                "edition_type": "detailed",
                "items": [
                    {
                        "id": 101,
                        "type": "Feature",
                        "title": "UD chunking",
                        "state": "Active",
                        "assigned_to": None,
                        "area_path": "Acme\\UD",
                        "target_date": None,
                        "risk_level": "medium",
                        "tags": [],
                    },
                    {
                        "id": 202,
                        "type": "Feature",
                        "title": "Fabrikam buildouts",
                        "state": "Active",
                        "assigned_to": None,
                        "area_path": "Acme\\Fabrikam",
                        "target_date": None,
                        "risk_level": "medium",
                        "tags": [],
                    },
                ],
                "scorecards": [],
                "schema_version": "1.0",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (repo_root / "programs" / "acme" / "archive" / "acme_weekly" / "index.json").write_text(
        json.dumps(
            {
                "edition": "acme_weekly",
                "issues": [
                    {
                        "issue_number": 1,
                        "generated_at": "2026-05-21T12:00:00+00:00",
                        "kind": "confirmed",
                        "snapshot_path": str(snapshot_path / "issue_001.snapshot.json"),
                        "html_path": None,
                        "md_path": None,
                        "manifest_path": None,
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (repo_root / "programs" / "acme" / "journal" / "2026-W21.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "sig-1",
                        "ts": "2026-05-20T12:00:00+00:00",
                        "src": "workiq",
                        "prog": "acme",
                        "ws": "ud",
                        "text": "UD and Fabrikam moved together.",
                        "refs": ["WI#101", "WI#202"],
                        "raw_ref": None,
                        "conf": "high",
                        "meta": None,
                        "review_policy": "pending",
                    }
                ),
                json.dumps(
                    {
                        "id": "sig-2",
                        "ts": "2026-05-20T13:00:00+00:00",
                        "src": "workiq",
                        "prog": "acme",
                        "ws": "ud",
                        "text": "Fabrikam and UD moved together again.",
                        "refs": ["WI#202", "WI#101"],
                        "raw_ref": None,
                        "conf": "high",
                        "meta": None,
                        "review_policy": "pending",
                    }
                ),
                json.dumps(
                    {
                        "id": "sig-3",
                        "ts": "2026-05-20T14:00:00+00:00",
                        "src": "workiq",
                        "prog": "acme",
                        "ws": "ud",
                        "text": "Recurring coupling between UD and Fabrikam.",
                        "refs": ["WI#101", "WI#202"],
                        "raw_ref": None,
                        "conf": "high",
                        "meta": None,
                        "review_policy": "pending",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (repo_root / "programs" / "acme" / "journal" / "reviews.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"record_type": "review", "signal_id": "sig-1", "decision": "approved", "reviewed_at": "2026-05-20T15:00:00+00:00", "reviewed_by": "owner"}),
                json.dumps({"record_type": "review", "signal_id": "sig-2", "decision": "approved", "reviewed_at": "2026-05-20T15:01:00+00:00", "reviewed_by": "owner"}),
                json.dumps({"record_type": "review", "signal_id": "sig-3", "decision": "approved", "reviewed_at": "2026-05-20T15:02:00+00:00", "reviewed_by": "owner"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return repo_root

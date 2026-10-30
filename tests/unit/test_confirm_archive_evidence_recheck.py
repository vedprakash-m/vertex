from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from src.commands import confirm as confirm_module
import src.commands.confirm_stages.archive_transaction as archive_transaction
from src.core.draft_artifact_store import load_draft_state
from src.core.edition_resolver import get_program_output_dir
from src.core.exceptions import ConfigError
from src.core.milestone_engine import assess_milestone_health, save_milestones
from src.core.models import RiskLevel
from src.core.models_v2 import Milestone, MilestoneStatus, TrajectoryPoint
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
from src.core.quality_gates.fact_drift import evaluate_program_fact_drift_from_draft
from src.core.store_factory import build_trajectory_store_for_program_id
from src.core.stages.milestone_stage import MilestoneStage
from src.core.trajectory import get_trajectory_path
from tests.unit import test_commands_confirm as confirm_tests
from tests.unit.test_commands_confirm import (
    _isolate_fact_store_db as _isolate_fact_store_db,
    test_confirm_issue_archives_outputs_and_resets_active_state as _exercise_actual_confirm,
)
from tests.unit.test_commands_report import repo_root as repo_root


@pytest.mark.parametrize("change", ["addition", "replacement", "content", "missing_pin"])
def test_forced_confirm_rechecks_fact_pin_after_archive_lock(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    store = ProgramFactStore("acme", db_root=tmp_path / "fact-db")
    original = store.append_fact(
        ProgramFactInput(
            fact_type="decision", scope="program", entity_refs=("PROGRAM:acme",),
            payload={"decision": "original"}, natural_key="baseline",
        ),
        recorded_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )
    lock = archive_transaction.ArchiveLock
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)
    entered = []

    @contextmanager
    def change_facts_after_lock(path: Path):
        with lock(path):
            entered.append(path)
            if change == "missing_pin":
                with store._connect() as connection:
                    connection.execute("DELETE FROM program_fact_snapshot_pins")
            elif change == "content":
                with store._connect() as connection:
                    connection.execute(
                        "UPDATE program_fact_revisions SET payload_json = ? WHERE revision_id = ?",
                        ('{"decision":"late change"}', original.revision.revision_id),
                    )
            else:
                store.append_fact(
                    ProgramFactInput(
                        fact_type="decision", scope="program", entity_refs=("PROGRAM:acme",),
                        payload={"decision": "late change"},
                        natural_key="baseline" if change == "replacement" else "late-decision",
                    )
                )
            yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", change_facts_after_lock)
    with pytest.raises(ConfigError, match="(?i)(drift|changed|content|unavailable)"):
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)

    assert len(entered) == 1
    checkpoints.assert_not_called()
    assert not (tmp_path / "archive" / "acme_weekly" / "index.json").exists()
    assert not tuple((tmp_path / "archive").rglob("*.snapshot.json"))


def test_archive_lock_failure_cannot_write_checkpoint(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)

    lock = MagicMock()
    lock.__enter__.side_effect = ConfigError("archive lock unavailable")
    monkeypatch.setattr(archive_transaction, "ArchiveLock", lambda path: lock)
    with pytest.raises(ConfigError, match="archive lock unavailable"):
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)

    checkpoints.assert_not_called()
    assert not tuple((tmp_path / "archive").rglob("*.snapshot.json"))


def test_actual_confirm_rejects_assessed_trajectory_drift_under_archive_lock(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_contexts = []
    lock_entries = []
    milestone = Milestone(
        id="synthetic-milestone",
        program_id="acme",
        name="Synthetic milestone",
        target_date=date(2026, 6, 30),
        owner_alias="maintainer",
        status=MilestoneStatus.ON_TRACK,
        exit_criteria=("Synthetic validation",),
        linked_workstream_ids=("deployment_readiness",),
        linked_work_item_ids=(900001,),
    )
    initial = TrajectoryPoint(
        date=date(2026, 5, 4), state="Active", assigned_to="Maintainer",
        target_date=date(2026, 5, 20), risk_level=RiskLevel.LOW,
        area_path=r"ExampleProject\Acme\Deployment",
    )
    changed = TrajectoryPoint(
        date=date(2026, 5, 5), state="Active", assigned_to="Maintainer",
        target_date=date(2026, 7, 15), risk_level=RiskLevel.LOW,
        area_path=r"ExampleProject\Acme\Deployment",
    )
    original_generate = confirm_tests.generate_report_draft

    def generate_with_milestone(*args, **kwargs):
        import yaml

        programs_root = kwargs["programs_root"]
        program_path = programs_root / "acme" / "program.yaml"
        program_data = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_data["storage_backend"] = "file"
        program_path.write_text(yaml.safe_dump(program_data, sort_keys=False), encoding="utf-8")
        save_milestones("acme", (milestone,), programs_root=programs_root)
        store = build_trajectory_store_for_program_id("acme", programs_root=programs_root)
        assert store.append("acme", 900001, initial)
        return original_generate(*args, **kwargs)

    original_execute = MilestoneStage.execute

    def capture_report_assessment(self, ctx):
        result = original_execute(self, ctx)
        if result.milestone_assessments:
            report_contexts.append((ctx, result))
        return result

    monkeypatch.setattr(confirm_tests, "generate_report_draft", generate_with_milestone)
    monkeypatch.setattr(MilestoneStage, "execute", capture_report_assessment)
    reports_root, archive_root, _ = confirm_tests._prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    assert report_contexts
    before_ctx, before_result = report_contexts[0]
    assert before_result.milestone_assessments[0].computed_health is MilestoneStatus.ON_TRACK

    draft = load_draft_state("acme_weekly", 1, programs_root=programs_root)
    assert draft is not None
    issue_dir = get_program_output_dir("acme_weekly", programs_root=programs_root) / "issue_001"
    draft_path = issue_dir / "issue_001.draft.json"
    original_lock = archive_transaction.ArchiveLock
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)

    @contextmanager
    def change_after_lock(path: Path):
        with original_lock(path):
            lock_entries.append(path)
            store = build_trajectory_store_for_program_id("acme", programs_root=programs_root)
            assert store.append("acme", 900001, changed)
            current = store.read("acme", 900001)
            after = assess_milestone_health(
                milestone, before_ctx.items, {900001: current}, before_ctx.data_as_of,
            )
            assert after.computed_health is MilestoneStatus.AT_RISK
            fact_gate = evaluate_program_fact_drift_from_draft(
                draft_state=draft, program_id="acme", edition_name="acme_weekly", issue_number=1,
            )
            assert fact_gate.passed is True
            yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", change_after_lock)
    with pytest.raises(ConfigError, match="(?i)(trajectory|milestone|evidence changed)"):
        confirm_module.confirm_issue(
            edition_name="acme_weekly",
            issue_number=1,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
            force=True,
        )

    assert len(lock_entries) == 1
    checkpoints.assert_not_called()
    assert not (archive_root / "acme_weekly" / "index.json").exists()
    assert not tuple(archive_root.rglob("*.snapshot.json"))
    assert draft_path.exists()
    assert get_trajectory_path("acme", 900001, programs_root).is_relative_to(programs_root)

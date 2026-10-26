from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path

import pytest

import src.commands.report as report_module
from src.commands.report import generate_report_draft
from src.commands.report_pipeline.assemble_stage import _pin_program_fact_snapshot
from src.core.edition_resolver import get_program_output_dir
from src.core.exceptions import ConfigError
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_confirm import _isolate_fact_store_db as _isolate_fact_store_db
from tests.unit.test_commands_report import _sample_items
from tests.support.synthetic_report_source import synthetic_report_root as repo_root


@pytest.mark.parametrize("change", ["addition", "replacement", "content"])
def test_ordinary_report_rejects_fact_change_during_acquisition(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    store = ProgramFactStore("acme", db_root=tmp_path / "fact-db")
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    original = store.append_fact(
        ProgramFactInput(
            fact_type="decision", scope="program", entity_refs=("PROGRAM:acme",),
            payload={"decision": "hold"}, natural_key="baseline",
        ), recorded_at=as_of,
    )

    def load_items(bundle, timestamp):
        if change == "content":
            with store._connect() as connection:
                connection.execute(
                    "UPDATE program_fact_revisions SET payload_json = ? WHERE revision_id = ?",
                    ('{"decision":"ship"}', original.revision.revision_id),
                )
        else:
            store.append_fact(
                ProgramFactInput(
                    fact_type="decision", scope="program", entity_refs=("PROGRAM:acme",),
                    payload={"decision": "ship"},
                    natural_key="baseline" if change == "replacement" else "new-decision",
                ), recorded_at=as_of,
            )
        return _sample_items(timestamp), 0

    output_dir = get_program_output_dir("acme_weekly", programs_root=programs_root)
    prior_artifact = output_dir / "issue_001" / "issue_001.html"
    prior_artifact.parent.mkdir(parents=True, exist_ok=True)
    prior_artifact.write_text("previous reviewed artifact", encoding="utf-8")

    with pytest.raises(ConfigError, match="(?i)fact.*(changed|drift)"):
        generate_report_draft(
            edition_name="acme_weekly",
            reports_root=reports_root,
            programs_root=programs_root,
            archive_root=tmp_path / "archive",
            as_of=as_of,
            work_item_loader=load_items,
            open_browser=False,
        )

    assert prior_artifact.read_text(encoding="utf-8") == "previous reviewed artifact"
    assert not (prior_artifact.parent / "issue_001.draft.json").exists()
    with store._connect() as connection:
        pins = connection.execute("SELECT snapshot_id FROM program_fact_snapshot_pins").fetchall()
    assert len(pins) == 1
    if change == "content":
        with pytest.raises(ConfigError, match="content"):
            store.detect_drift(pins[0][0])
    else:
        assert len(store.detect_drift(pins[0][0])) == 1


def test_ordinary_report_persists_the_pre_acquisition_pin(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    store = ProgramFactStore("acme", db_root=tmp_path / "fact-db")
    observed_pins: list[str] = []

    def load_items(bundle, timestamp):
        with store._connect() as connection:
            pins = connection.execute("SELECT snapshot_id FROM program_fact_snapshot_pins").fetchall()
        assert len(pins) == 1
        observed_pins.append(pins[0][0])
        return _sample_items(timestamp), 0

    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
        as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        work_item_loader=load_items,
        open_browser=False,
    )

    output_dir = get_program_output_dir("acme_weekly", programs_root=programs_root)
    draft = json.loads((output_dir / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    assert [draft["program_fact_snapshot"]["snapshot_id"]] == observed_pins
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM program_fact_snapshot_pins").fetchone()[0] == 1


@pytest.mark.parametrize("scope", ["program", "edition", "issue"])
def test_captured_pin_cannot_be_reused_for_another_output(
    tmp_path: Path, scope: str,
) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    pin = store.pin_snapshot(metadata={"edition_name": "acme_weekly", "issue_number": 1})
    if scope == "program":
        pin = replace(pin, program_id="other")
    else:
        pin = replace(
            pin,
            metadata={
                **pin.metadata,
                "edition_name" if scope == "edition" else "issue_number": "other" if scope == "edition" else 2,
            },
        )

    with pytest.raises(ConfigError, match="scope"):
        _pin_program_fact_snapshot(
            "acme", edition_name="acme_weekly", issue_number=1,
            generated_at=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
            db_root=tmp_path, pin=pin,
        )


def test_ordinary_report_cannot_replace_a_missing_captured_pin(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    execute_output = report_module._OutputStage.execute

    def drop_captured_pin(stage, ctx):
        assert ctx.fact_snapshot_pin is not None
        return execute_output(stage, replace(ctx, fact_snapshot_pin=None))

    monkeypatch.setattr(report_module._OutputStage, "execute", drop_captured_pin)

    with pytest.raises(ConfigError, match="baseline is missing"):
        generate_report_draft(
            edition_name="acme_weekly",
            reports_root=reports_root,
            programs_root=programs_root,
            archive_root=tmp_path / "archive",
            as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
            work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
            open_browser=False,
        )

    output_dir = get_program_output_dir("acme_weekly", programs_root=programs_root)
    assert not (output_dir / "issue_001" / "issue_001.draft.json").exists()
    store = ProgramFactStore("acme", db_root=tmp_path / "fact-db")
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM program_fact_snapshot_pins").fetchone()[0] == 1


@pytest.mark.parametrize("program_id", [None, "acme"])
def test_legacy_pin_helper_keeps_optional_program_behavior(
    tmp_path: Path, program_id: str | None,
) -> None:
    root = tmp_path / "fact-db"
    draft_pin = _pin_program_fact_snapshot(
        program_id, edition_name="acme_weekly", issue_number=1,
        generated_at=datetime(2026, 5, 5, 18, tzinfo=timezone.utc), db_root=root,
    )

    if program_id is None:
        assert draft_pin is None
        assert not root.exists()
    else:
        assert draft_pin is not None
        assert draft_pin.program_id == program_id
        assert ProgramFactStore(program_id, db_root=root).detect_drift(draft_pin.snapshot_id) == ()

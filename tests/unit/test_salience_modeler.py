from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.ai.edit_learner import append_edit_patterns, build_edit_patterns
from src.core.feedback.salience_modeler import SalienceEvent, append_salience_event, load_author_salience, refresh_author_salience


def test_refresh_author_salience_writes_cached_yaml_and_audit(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            recorded_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
            draft_exec_summary_text="AI exec summary draft one.",
            confirmed_exec_summary_text="AI exec summary draft one adjusted.",
            draft_workstream_blurbs={
                "deployment": "ETA risk remains elevated and needs escalation.",
                "repair": "Repair is steady but not fully green yet.",
            },
            confirmed_workstream_blurbs={
                "deployment": "Deployment ETA risk remains elevated and needs immediate escalation.",
                "repair": "Repair remains steady but not fully green.",
            },
        ),
        programs_root=programs_root,
    )
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=78,
            recorded_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
            draft_exec_summary_text="",
            confirmed_exec_summary_text="",
            draft_workstream_blurbs={
                "deployment": "Deployment risk remains elevated.",
            },
            confirmed_workstream_blurbs={
                "deployment": "Deployment risk remains elevated and now needs leadership attention.",
            },
        ),
        programs_root=programs_root,
    )

    model, path = refresh_author_salience(
        "acme",
        programs_root=programs_root,
        author_alias="operator",
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )
    loaded = load_author_salience("acme", programs_root=programs_root)

    assert path is not None
    assert path.exists()
    assert loaded == model
    assert [workstream.workstream_id for workstream in model.workstreams] == ["deployment", "repair"]
    assert model.workstreams[0].attention_weight > model.workstreams[1].attention_weight
    assert model.workstreams[0].attention_weight >= 0.2

    audit_path = programs_root / "acme" / "_feedback" / "_audit.jsonl"
    audit_entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert audit_entries[-1]["module"] == "salience_modeler"
    assert audit_entries[-1]["file"] == "author_salience.yaml"


def test_refresh_author_salience_dry_run_skips_cached_write(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            recorded_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
            draft_exec_summary_text="",
            confirmed_exec_summary_text="",
            draft_workstream_blurbs={"deployment": "ETA risk remains elevated."},
            confirmed_workstream_blurbs={"deployment": "Deployment ETA risk remains elevated."},
        ),
        programs_root=programs_root,
    )

    model, path = refresh_author_salience(
        "acme",
        programs_root=programs_root,
        author_alias="operator",
        dry_run=True,
    )

    assert path is None
    assert model.workstreams[0].workstream_id == "deployment"
    assert not (programs_root / "acme" / "_feedback" / "author_salience.yaml").exists()


def test_refresh_author_salience_honors_program_config_defaults(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
                "salience:",
                "  min_weight: 0.4",
                "  ema_alpha: 0.3",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            recorded_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
            draft_exec_summary_text="",
            confirmed_exec_summary_text="",
            draft_workstream_blurbs={"deployment": "ETA risk remains elevated."},
            confirmed_workstream_blurbs={"deployment": "Deployment ETA risk remains elevated and needs escalation."},
        ),
        programs_root=programs_root,
    )

    model, _path = refresh_author_salience(
        "acme",
        programs_root=programs_root,
        author_alias="operator",
    )

    assert model.min_weight == 0.4
    assert model.ema_alpha == 0.3
    assert model.workstreams[0].attention_weight >= 0.4


def test_refresh_author_salience_consumes_salience_events_and_confirmation_weight(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
                "salience:",
                "  min_weight: 0.2",
                "  ema_alpha: 0.1",
                "  confirmation_weight: 2.0",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            recorded_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
            draft_exec_summary_text="",
            confirmed_exec_summary_text="",
            draft_workstream_blurbs={"deployment": "ETA risk remains elevated."},
            confirmed_workstream_blurbs={"deployment": "Deployment ETA risk remains elevated and needs escalation."},
        ),
        programs_root=programs_root,
    )
    append_salience_event(
        "acme",
        SalienceEvent(
            event_id="dismissed-1",
            recorded_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
            anomaly_id="sig-dismissed",
            workstream_id="deployment",
            action="dismissed",
            work_item_id=1234,
        ),
        programs_root=programs_root,
    )
    append_salience_event(
        "acme",
        SalienceEvent(
            event_id="confirmed-1",
            recorded_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
            anomaly_id="sig-dismissed",
            workstream_id="deployment",
            action="confirmed_slip",
            work_item_id=1234,
            confirmed_within_30d=True,
        ),
        programs_root=programs_root,
    )

    model, _path = refresh_author_salience(
        "acme",
        programs_root=programs_root,
        author_alias="operator",
        as_of=datetime(2026, 5, 21, 13, 0, tzinfo=timezone.utc),
    )

    assert len(model.workstreams) == 1
    assert model.workstreams[0].workstream_id == "deployment"
    assert model.workstreams[0].sample_count == 3
    assert model.workstreams[0].average_override_magnitude > 0.0
    assert model.workstreams[0].attention_weight == 0.3906
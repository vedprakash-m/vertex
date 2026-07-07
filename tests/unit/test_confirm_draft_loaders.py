"""Direct coverage for the extracted confirm draft/readiness loaders.

Guards the D-25 / Phase 3 extraction of the read-only loader cluster from
``src/commands/confirm.py`` into
``src/commands/confirm_stages/draft_loaders.py``. These loaders read persisted
draft artifacts and must degrade gracefully (None / {} / BadParameter) on
missing or malformed inputs without ever writing state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer

from src.commands.confirm_stages.draft_loaders import (
    coerce_optional_int,
    load_confirm_overrides,
    load_confirm_review_status,
    load_current_draft_manifest_id,
    load_draft_ai_safety_metadata,
    load_draft_readiness_metadata,
    load_draft_state,
    load_optional_yaml_mapping,
    load_readiness_gate_settings,
)
from src.core.models import ReviewState


def _write_manifest(output_root: Path, edition: str, issue: int, metadata: dict) -> None:
    # Source reads via get_program_output_dir(edition, programs_root=output_root), which
    # resolves to output_root/<edition>/publications/<edition>/ when no edition YAML is staged.
    issue_dir = output_root / edition / "publications" / edition / f"issue_{issue:03d}"
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / f"issue_{issue:03d}.manifest.json").write_text(
        json.dumps({"manifest_id": "m-1", "metadata": metadata}), encoding="utf-8"
    )


def test_coerce_optional_int_variants() -> None:
    assert coerce_optional_int(None) is None
    assert coerce_optional_int(True) is None  # bool is not an int here
    assert coerce_optional_int(7) == 7
    assert coerce_optional_int(3.0) == 3
    assert coerce_optional_int(3.5) is None
    assert coerce_optional_int(" 12 ") == 12
    assert coerce_optional_int("x") is None


def test_load_optional_yaml_mapping(tmp_path: Path) -> None:
    assert load_optional_yaml_mapping(tmp_path / "missing.yaml") == {}
    good = tmp_path / "good.yaml"
    good.write_text("a: 1\nb: two\n", encoding="utf-8")
    assert load_optional_yaml_mapping(good) == {"a": 1, "b": "two"}
    listy = tmp_path / "list.yaml"
    listy.write_text("- 1\n- 2\n", encoding="utf-8")
    assert load_optional_yaml_mapping(listy) == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [unterminated\n", encoding="utf-8")
    assert load_optional_yaml_mapping(bad) == {}


def test_load_draft_state_missing_and_invalid_and_valid(tmp_path: Path) -> None:
    with pytest.raises(typer.BadParameter):
        load_draft_state("acme_weekly", 1, programs_root=tmp_path)
    issue_dir = tmp_path / "acme_weekly" / "publications" / "acme_weekly" / "issue_001"
    issue_dir.mkdir(parents=True)
    draft = issue_dir / "issue_001.draft.json"
    draft.write_text("not json", encoding="utf-8")
    with pytest.raises(typer.BadParameter):
        load_draft_state("acme_weekly", 1, programs_root=tmp_path)
    draft.write_text(json.dumps([1, 2, 3]), encoding="utf-8")  # not a mapping
    with pytest.raises(typer.BadParameter):
        load_draft_state("acme_weekly", 1, programs_root=tmp_path)
    draft.write_text(json.dumps({"items": []}), encoding="utf-8")
    assert load_draft_state("acme_weekly", 1, programs_root=tmp_path) == {"items": []}


def test_load_current_draft_manifest_id(tmp_path: Path) -> None:
    with pytest.raises(typer.BadParameter):
        load_current_draft_manifest_id("acme_weekly", 1, programs_root=tmp_path)
    _write_manifest(tmp_path, "acme_weekly", 1, metadata={})
    assert load_current_draft_manifest_id("acme_weekly", 1, programs_root=tmp_path) == "m-1"
    # missing manifest_id key
    manifest_dir = tmp_path / "acme_weekly" / "publications" / "acme_weekly" / "issue_002"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "issue_002.manifest.json").write_text(json.dumps({"metadata": {}}), encoding="utf-8")
    with pytest.raises(typer.BadParameter):
        load_current_draft_manifest_id("acme_weekly", 2, programs_root=tmp_path)


def test_load_draft_readiness_and_ai_safety_metadata(tmp_path: Path) -> None:
    assert load_draft_readiness_metadata(edition_name="acme_weekly", issue_number=1, programs_root=tmp_path) is None
    _write_manifest(
        tmp_path,
        "acme_weekly",
        1,
        metadata={"draft_readiness": {"ready": True}, "ai_safety": {"scrubbed": True}},
    )
    assert load_draft_readiness_metadata(edition_name="acme_weekly", issue_number=1, programs_root=tmp_path) == {"ready": True}
    assert load_draft_ai_safety_metadata(edition_name="acme_weekly", issue_number=1, programs_root=tmp_path) == {"scrubbed": True}
    # metadata present but without the keys -> None
    _write_manifest(tmp_path, "acme_weekly", 2, metadata={"other": 1})
    assert load_draft_readiness_metadata(edition_name="acme_weekly", issue_number=2, programs_root=tmp_path) is None
    assert load_draft_ai_safety_metadata(edition_name="acme_weekly", issue_number=2, programs_root=tmp_path) is None


def test_load_readiness_gate_settings_program_and_edition(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "editions"
    (programs_root / "acme").mkdir(parents=True)
    editions_root.mkdir(parents=True)
    (programs_root / "acme" / "program.yaml").write_text(
        "readiness:\n  gate: true\n  snapshot_max_age_days: 5\n", encoding="utf-8"
    )
    (editions_root / "acme_weekly.yaml").write_text("readiness_gate: false\n", encoding="utf-8")
    enabled, max_age = load_readiness_gate_settings(
        edition_name="acme_weekly", program_id="acme", editions_root=editions_root, programs_root=programs_root
    )
    assert enabled is True
    assert max_age == 5
    # no program, edition enables gate, edition supplies age
    (editions_root / "fabrikam_weekly.yaml").write_text(
        "readiness_gate: true\nreadiness_snapshot_max_age_days: 9\n", encoding="utf-8"
    )
    enabled2, max_age2 = load_readiness_gate_settings(
        edition_name="fabrikam_weekly", program_id=None, editions_root=editions_root, programs_root=programs_root
    )
    assert enabled2 is True
    assert max_age2 == 9


def test_load_confirm_review_status_defaults_to_pending(tmp_path: Path) -> None:
    status = load_confirm_review_status("acme_weekly", 5, ("ws-a", "ws-b"), tmp_path)
    assert status.issue_number == 5
    section_ids = [section.section_id for section in status.sections]
    assert section_ids == ["exec_summary", "ws:ws-a", "ws:ws-b"]
    assert all(section.state == ReviewState.PENDING for section in status.sections)


def test_load_confirm_overrides_missing_raises(tmp_path: Path) -> None:
    # load_overrides returns None for an empty reports_root, so the bundle is
    # never dereferenced before the BadParameter is raised.
    with pytest.raises(typer.BadParameter):
        load_confirm_overrides("acme_weekly", 1, bundle=None, reports_root=tmp_path)

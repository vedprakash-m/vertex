from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from typer import BadParameter

from src.commands import confirm
from src.commands.confirm_stages import archive_transaction, draft_loaders
from src.core.exceptions import ConfigError
from src.core.manifest_writer import hash_jsonable
from tests.unit.test_commands_confirm import (
    EDITION_NAME,
    _isolate_fact_store_db as _isolate_fact_store_db,
    _prepare_confirmable_weekly_issue,
)
from tests.unit.test_commands_report import repo_root as repo_root


def _change_title(programs_root: Path) -> None:
    path = programs_root / "acme" / "editions" / f"{EDITION_NAME}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["name"] = "Changed configuration title"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize("config_hash", [
    pytest.param(..., id="missing"), None, "", "sha256:bad", 42, "sha256:" + "g" * 64,
])
def test_original_config_hash_is_required_not_invented(
    tmp_path: Path, config_hash: object,
) -> None:
    path = tmp_path / "fixture" / "publications" / "fixture" / "issue_007" / "issue_007.manifest.json"
    path.parent.mkdir(parents=True)
    payload: dict[str, object] = {"manifest_id": "original"}
    if config_hash is not Ellipsis:
        payload["config_hash"] = config_hash
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(BadParameter, match="config.*hash"):
        draft_loaders.load_confirm_draft_manifest("fixture", 7, draft_state={}, programs_root=tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_forced_confirm_rejects_changed_config_before_render_or_extraction(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _change_title(programs_root)
    render = Mock(side_effect=AssertionError("Rendering must not run"))
    extraction = Mock(side_effect=AssertionError("Extraction must not run"))
    monkeypatch.setattr(confirm, "_build_confirm_artifacts", render)
    monkeypatch.setattr(confirm, "_prepare_confirm_claim_extraction_for_v2", extraction)
    with pytest.raises(ConfigError, match="(?i)configuration.*changed"):
        confirm.confirm_issue(
            EDITION_NAME, 1, dry_run=dry_run, force=True, ack_stale_approval=True,
            reports_root=reports_root, archive_root=archive_root, programs_root=programs_root,
        )
    render.assert_not_called()
    extraction.assert_not_called()
    assert not (archive_root / EDITION_NAME / "index.json").exists()


def test_forced_confirm_rechecks_fresh_config_after_archive_lock(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    overrides = programs_root / "acme" / "overrides" / "issue_001.yaml"
    before_overrides = overrides.read_bytes()
    narrative = programs_root / "acme" / "narratives" / "issue_001" / "exec_summary.md"
    before_narrative = narrative.read_bytes()
    lock = archive_transaction.ArchiveLock
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)
    entered = []

    @contextmanager
    def change_after_lock(path: Path):
        with lock(path):
            entered.append(path)
            _change_title(programs_root)
            yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", change_after_lock)
    with pytest.raises(ConfigError, match="(?i)configuration.*changed"):
        confirm.confirm_issue(
            EDITION_NAME, 1, force=True, ack_stale_approval=True,
            reports_root=reports_root, archive_root=archive_root, programs_root=programs_root,
        )
    assert len(entered) == 1
    checkpoints.assert_not_called()
    assert not (archive_root / EDITION_NAME / "index.json").exists()
    assert not tuple(archive_root.rglob("*.snapshot.json"))
    assert overrides.read_bytes() == before_overrides
    assert narrative.read_bytes() == before_narrative


@pytest.mark.parametrize("reformat", [False, True])
def test_unchanged_effective_config_preserves_post_draft_narrative_and_risk_edits(
    repo_root: Path, tmp_path: Path, reformat: bool,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    if reformat:
        path = programs_root / "acme" / "editions" / f"{EDITION_NAME}.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        path.write_text("# Equivalent configuration\n" + yaml.safe_dump(document, sort_keys=True), encoding="utf-8")
    result = confirm.confirm_issue(
        EDITION_NAME, 1, force=True, reports_root=reports_root,
        archive_root=archive_root, programs_root=programs_root,
    )
    assert result.exit_code == 0
    assert result.archive_paths is not None
    assert "Confirmed executive summary." in result.archive_paths.html_path.read_text(encoding="utf-8")
    assert result.manifest.config_hash == hash_jsonable(
        confirm.load_bundle(EDITION_NAME, reports_root=reports_root, programs_root=programs_root).config
    )

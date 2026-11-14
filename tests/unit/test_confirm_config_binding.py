from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from typer import BadParameter

from src.commands import confirm
from src.commands.confirm_stages import archive_transaction, draft_loaders
from src.core.edition_resolver import get_program_output_dir
from src.core.exceptions import ConfigError
from src.core.fact_sor_state import save_fact_sor_state
from src.core.manifest_writer import hash_jsonable
from src.core.models import ReviewState
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore, build_natural_key
from src.core.review_status_store import get_review_status_path, load_review_status, save_review_status
from src.core.snapshot_store import get_archive_root
from tests.unit import test_commands_confirm as confirm_tests
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


def test_forced_confirm_rechecks_program_context_after_archive_lock(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    draft = confirm._load_draft_state(EDITION_NAME, 1, programs_root=programs_root)
    original_bundle = confirm.load_bundle(
        EDITION_NAME, reports_root=reports_root, programs_root=programs_root,
    )
    assert original_bundle.program_context is not None
    assert draft["program_context_sha256"] == hash_jsonable(original_bundle.program_context)
    manifest = confirm._load_confirm_draft_manifest(
        EDITION_NAME, 1, draft_state=draft, programs_root=programs_root,
    )
    review = load_review_status(EDITION_NAME, reports_root, programs_root=programs_root)
    assert review is not None and review.sections
    save_review_status(
        EDITION_NAME,
        replace(
            review,
            sections=tuple(
                replace(
                    section, state=ReviewState.APPROVED, reviewer="Synthetic reviewer",
                    updated_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
                    manifest_id=manifest.manifest_id,
                )
                for section in review.sections
            ),
        ),
        reports_root, programs_root=programs_root,
    )
    program_path = programs_root / "acme" / "program.yaml"
    overrides = programs_root / "acme" / "overrides" / "issue_001.yaml"
    narrative = programs_root / "acme" / "narratives" / "issue_001" / "exec_summary.md"
    review_path = get_review_status_path(EDITION_NAME, reports_root, programs_root=programs_root)
    before_overrides, before_narrative = overrides.read_bytes(), narrative.read_bytes()
    before_review = review_path.read_bytes()
    lock = archive_transaction.ArchiveLock
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    snapshot_writer = Mock(wraps=confirm.write_confirmed)
    archive_writer = Mock(wraps=confirm.write_confirmed_issue)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)
    monkeypatch.setattr(confirm, "write_confirmed", snapshot_writer)
    monkeypatch.setattr(confirm, "write_confirmed_issue", archive_writer)
    entered = []

    @contextmanager
    def change_after_lock(path: Path):
        with lock(path):
            entered.append(path)
            document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
            document["objective"] = "A changed synthetic program objective."
            program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            changed_bundle = confirm.load_bundle(
                EDITION_NAME, reports_root=reports_root, programs_root=programs_root,
            )
            assert hash_jsonable(changed_bundle.config) == hash_jsonable(original_bundle.config)
            assert hash_jsonable(changed_bundle.program_context) != draft["program_context_sha256"]
            yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", change_after_lock)
    with pytest.raises(ConfigError, match="(?i)program context.*changed"):
        confirm.confirm_issue(
            EDITION_NAME, 1, force=True, ack_stale_approval=True,
            reports_root=reports_root, archive_root=archive_root, programs_root=programs_root,
        )
    assert len(entered) == 1
    checkpoints.assert_not_called()
    snapshot_writer.assert_not_called()
    archive_writer.assert_not_called()
    selected_archive = get_archive_root(EDITION_NAME, archive_root, programs_root=programs_root)
    assert not (selected_archive / "index.json").exists()
    assert not tuple(selected_archive.rglob("*.snapshot.json"))
    assert overrides.read_bytes() == before_overrides
    assert narrative.read_bytes() == before_narrative
    assert review_path.read_bytes() == before_review


def test_forced_confirm_rechecks_context_after_evidence_fence_acquisition(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    original_fence = archive_transaction.cooperative_capture_fence
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)

    @contextmanager
    def change_after_fence(*resources: Path):
        with original_fence(*resources):
            document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
            document["objective"] = "A changed synthetic program objective."
            program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            yield

    monkeypatch.setattr(archive_transaction, "cooperative_capture_fence", change_after_fence)
    with pytest.raises(ConfigError, match="(?i)program context.*changed"):
        confirm.confirm_issue(
            EDITION_NAME, 1, force=True, ack_stale_approval=True,
            reports_root=reports_root, archive_root=archive_root, programs_root=programs_root,
        )
    checkpoints.assert_not_called()
    assert not (get_archive_root(EDITION_NAME, archive_root, programs_root=programs_root) / "index.json").exists()


@pytest.mark.parametrize("mode", ["primary", "shadow"])
@pytest.mark.parametrize("context_bound", [True, False], ids=["bound", "legacy-unbound"])
def test_confirm_context_recheck_preserves_nonlegacy_workstream_source(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, mode: str, context_bound: bool,
) -> None:
    stage_workspace = confirm_tests.stage_v2_report_workspace

    def stage_with_distinct_primary_source(*args, **kwargs):
        reports_root = stage_workspace(*args, **kwargs)
        programs_root = reports_root.parent / "programs"
        workstreams_path = programs_root / "acme" / "workstreams.yaml"
        document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
        store = ProgramFactStore("acme")
        for workstream in document["workstreams"]:
            refs = (f"WS:{workstream['id']}",)
            store.append_fact(
                ProgramFactInput(
                    fact_type="workstream.entry", scope="program", entity_refs=refs,
                    payload=workstream,
                    natural_key=build_natural_key("workstream.entry", entity_refs=refs, scope="program"),
                ),
                recorded_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            )
        document["workstreams"].append({
            "id": "legacy-only", "name": "Legacy only", "area_paths": ["Synthetic\\Legacy"],
        })
        workstreams_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        edition_path = programs_root / "acme" / "editions" / f"{EDITION_NAME}.yaml"
        edition = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
        edition.setdefault("ado", {}).pop("area_paths", None)
        edition_path.write_text(yaml.safe_dump(edition, sort_keys=False), encoding="utf-8")
        save_fact_sor_state(
            "acme", mode="legacy", family_modes={"workitem.state": mode},
            recorded_at=datetime(2026, 5, 1, tzinfo=timezone.utc), programs_root=programs_root,
        )
        return reports_root

    monkeypatch.setattr(confirm_tests, "stage_v2_report_workspace", stage_with_distinct_primary_source)
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    original_bundle = confirm.load_bundle(EDITION_NAME, reports_root=reports_root, programs_root=programs_root)
    assert original_bundle.program_context is not None
    names = {workstream.name for workstream in original_bundle.program_context.workstreams}
    assert ("Legacy only" in names) is (mode == "shadow")
    if not context_bound:
        draft_path = get_program_output_dir(EDITION_NAME, programs_root=programs_root) / "issue_001" / "issue_001.draft.json"
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        del draft["program_context_sha256"]
        draft_path.write_text(json.dumps(draft), encoding="utf-8")
    caplog.clear()

    result = confirm.confirm_issue(
        EDITION_NAME, 1, force=True, ack_stale_approval=True,
        reports_root=reports_root, archive_root=archive_root, programs_root=programs_root,
    )

    assert result.exit_code == 0
    assert result.archive_paths is not None and result.archive_paths.index_path.is_file()
    assert not any("Capture fence cannot expand" in record.message for record in caplog.records)


@pytest.mark.parametrize("mode", ["primary", "shadow"])
def test_confirm_rejects_late_source_switch_without_fence_expansion(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, mode: str,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    draft = confirm._load_draft_state(EDITION_NAME, 1, programs_root=programs_root)
    manifest = confirm._load_confirm_draft_manifest(
        EDITION_NAME, 1, draft_state=draft, programs_root=programs_root,
    )
    assert manifest.run_evidence_snapshot is not None
    assert dict(manifest.run_evidence_snapshot.effective_family_modes)["workitem.state"] == "legacy"
    assert draft["program_context_sha256"] is not None
    lock = archive_transaction.ArchiveLock
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)

    @contextmanager
    def switch_after_lock(path: Path):
        with lock(path):
            save_fact_sor_state(
                "acme", mode="legacy", family_modes={"workitem.state": mode},
                recorded_at=datetime(2026, 5, 6, tzinfo=timezone.utc), programs_root=programs_root,
            )
            yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", switch_after_lock)
    caplog.clear()
    with pytest.raises(ConfigError, match="(?i)selectors changed"):
        confirm.confirm_issue(
            EDITION_NAME, 1, force=True, ack_stale_approval=True,
            reports_root=reports_root, archive_root=archive_root, programs_root=programs_root,
        )
    checkpoints.assert_not_called()
    assert not (get_archive_root(EDITION_NAME, archive_root, programs_root=programs_root) / "index.json").exists()
    assert not any("Capture fence cannot expand" in record.message for record in caplog.records)


@pytest.mark.parametrize("legacy_digest", ["missing", "null"])
def test_legacy_draft_remains_compatible_with_late_context_change(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_digest: str,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    draft_path = get_program_output_dir(EDITION_NAME, programs_root=programs_root) / "issue_001" / "issue_001.draft.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    if legacy_digest == "missing":
        del draft["program_context_sha256"]
    else:
        draft["program_context_sha256"] = None
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    program_path = programs_root / "acme" / "program.yaml"
    lock = archive_transaction.ArchiveLock
    entered = []

    @contextmanager
    def change_after_lock(path: Path):
        with lock(path):
            entered.append(path)
            document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
            document["objective"] = "A changed synthetic program objective."
            program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", change_after_lock)
    result = confirm.confirm_issue(
        EDITION_NAME, 1, force=True, ack_stale_approval=True,
        reports_root=reports_root, archive_root=archive_root, programs_root=programs_root,
    )
    assert len(entered) == 1
    assert result.exit_code == 0
    assert result.archive_paths is not None
    assert result.archive_paths.snapshot_path.exists()


@pytest.mark.parametrize(
    "dependency",
    ["load_bundle", "hash_jsonable", "evaluate_program_fact_drift_from_draft", "_validate_pinned_gather_run"],
)
def test_locked_recheck_resolves_dependencies_after_archive_lock(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dependency: str,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    lock = archive_transaction.ArchiveLock
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)

    def reject(*_args, **_kwargs):
        raise ConfigError(f"Late-bound {dependency}")

    @contextmanager
    def replace_dependency_after_lock(path: Path):
        with lock(path):
            monkeypatch.setattr(confirm, dependency, reject)
            yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", replace_dependency_after_lock)
    with pytest.raises(ConfigError, match=f"Late-bound {dependency}"):
        confirm.confirm_issue(
            EDITION_NAME, 1, force=True, ack_stale_approval=True,
            reports_root=reports_root, archive_root=archive_root, programs_root=programs_root,
        )
    checkpoints.assert_not_called()
    assert not tuple(archive_root.rglob("*.snapshot.json"))


@pytest.mark.parametrize("reformat", [False, True])
def test_unchanged_effective_config_preserves_post_draft_narrative_and_risk_edits(
    repo_root: Path, tmp_path: Path, reformat: bool,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    draft = confirm._load_draft_state(EDITION_NAME, 1, programs_root=programs_root)
    original_context = confirm.load_bundle(
        EDITION_NAME, reports_root=reports_root, programs_root=programs_root,
    ).program_context
    assert original_context is not None
    assert draft["program_context_sha256"] == hash_jsonable(original_context)
    if reformat:
        path = programs_root / "acme" / "editions" / f"{EDITION_NAME}.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        path.write_text("# Equivalent configuration\n" + yaml.safe_dump(document, sort_keys=True), encoding="utf-8")
        path = programs_root / "acme" / "program.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        path.write_text("# Equivalent program context\n" + yaml.safe_dump(document, sort_keys=True), encoding="utf-8")
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

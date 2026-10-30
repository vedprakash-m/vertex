from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer import BadParameter

from src.commands import confirm
from src.commands.confirm_stages import draft_loaders
from src.core.manifest_writer import hash_jsonable
from src.core.overrides_store import OverridesDocument
from tests.unit.test_run_evidence_contracts import _capture, _wire


def _manifest(root: Path, payload: object) -> Path:
    path = root / "fixture" / "publications" / "fixture" / "issue_007" / "issue_007.manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, dict):
        payload = {"config_hash": hash_jsonable({}), **payload}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_capture_reads_one_revision_for_identity_and_both_metadata_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = {"manifest_id": "first", "config_hash": hash_jsonable({"revision": "first"}), "metadata": {"ai_safety": {"revision": "first"}, "draft_readiness": {"revision": "first"}}}
    second = {"manifest_id": "second", "config_hash": hash_jsonable({"revision": "second"}), "metadata": {"ai_safety": {"revision": "second"}, "draft_readiness": {"revision": "second"}}}
    path = _manifest(tmp_path, first)
    original = Path.read_text
    reads = []

    def read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        text = original(self, encoding=encoding, errors=errors)
        if self == path:
            reads.append(self)
            path.write_text(json.dumps(second), encoding="utf-8")
        return text

    monkeypatch.setattr(Path, "read_text", read_text)
    captured = draft_loaders.load_confirm_draft_manifest("fixture", 7, draft_state={}, programs_root=tmp_path)
    assert captured.manifest_id == "first"
    assert captured.config_hash == first["config_hash"]
    assert captured.ai_safety == {"revision": "first"}
    assert captured.draft_readiness == {"revision": "first"}
    assert reads == [path]
    with pytest.raises(FrozenInstanceError):
        setattr(captured, "manifest_id", "changed")
    newer = draft_loaders.load_confirm_draft_manifest("fixture", 7, draft_state={}, programs_root=tmp_path)
    assert newer.manifest_id == "second"
    assert newer.config_hash == second["config_hash"]
    assert reads == [path, path]


@pytest.mark.parametrize("payload", [
    [], {"manifest_id": 1}, {"manifest_id": " "}, {"manifest_id": "id", "metadata": []},
    {"manifest_id": "id", "metadata": {"ai_safety": False}},
    {"manifest_id": "id", "metadata": {"draft_readiness": []}},
])
def test_invalid_capture_is_required_and_explicit(tmp_path: Path, payload: object) -> None:
    _manifest(tmp_path, payload)
    with pytest.raises(BadParameter):
        draft_loaders.load_confirm_draft_manifest("fixture", 7, draft_state={}, programs_root=tmp_path)


@pytest.mark.parametrize("metadata", [None, {}, {"ai_safety": None, "draft_readiness": None}])
def test_capture_preserves_optional_legacy_metadata(tmp_path: Path, metadata: object) -> None:
    with pytest.raises(BadParameter):
        draft_loaders.load_confirm_draft_manifest("fixture", 7, draft_state={}, programs_root=tmp_path)
    _manifest(tmp_path, {"manifest_id": "id", "metadata": metadata})
    captured = draft_loaders.load_confirm_draft_manifest("fixture", 7, draft_state={}, programs_root=tmp_path)
    assert captured.manifest_id == "id"
    assert captured.ai_safety is None
    assert captured.draft_readiness is None
    assert captured.milestone_assessments is None
    assert captured.milestone_trajectory_snapshot is None


def test_capture_projects_milestone_assessment_scope_and_trajectory_snapshot(tmp_path: Path) -> None:
    assessments = [
        {"milestone_id": "m1", "linked_work_item_ids": [11, 12]},
        {"milestone_id": "m2", "linked_work_item_ids": []},
    ]
    trajectory_snapshot = {
        "schema_version": "1",
        "program_id": "fixture",
        "backend": "file",
        "items": [],
        "sha256": hash_jsonable({
            "schema_version": "1",
            "program_id": "fixture",
            "backend": "file",
            "items": [],
        }),
    }
    _manifest(tmp_path, {
        "manifest_id": "id",
        "metadata": {
            "milestone_assessments": assessments,
            "milestone_trajectory_snapshot": trajectory_snapshot,
        },
    })
    captured = draft_loaders.load_confirm_draft_manifest("fixture", 7, draft_state={}, programs_root=tmp_path)
    assert captured.milestone_assessments == assessments
    assert captured.milestone_trajectory_snapshot == trajectory_snapshot


def test_trajectory_metadata_projection_handles_absent_and_invalid_payload() -> None:
    assert draft_loaders._project_draft_metadata_value(None, "milestone_assessments", "fixture", 7) is None
    with pytest.raises(BadParameter, match="expected a JSON object"):
        draft_loaders._project_draft_metadata_value(
            {"metadata": []}, "milestone_assessments", "fixture", 7,
        )


def test_confirm_passes_its_captured_manifest_to_artifact_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    _manifest(root, {"manifest_id": "id", "metadata": {"ai_safety": {"enabled": False}}})
    bundle = SimpleNamespace(program=SimpleNamespace(id="fixture"), program_id="fixture", config={})
    monkeypatch.setattr(confirm, "load_bundle", lambda *_args, **_kwargs: bundle)
    monkeypatch.setattr(confirm, "_load_draft_state", lambda *_args, **_kwargs: {})
    override_roots = []

    def load_overrides(*_args, **kwargs):
        override_roots.append(kwargs.get("programs_root"))
        return OverridesDocument(issue_number=7, top_3_now=(), scorecards=())

    monkeypatch.setattr(
        confirm, "_load_confirm_overrides",
        load_overrides,
    )
    captured = []

    def build(**kwargs):
        captured.append(kwargs["draft_manifest"])
        raise RuntimeError("stop before rendering or writes")

    monkeypatch.setattr(confirm, "_build_confirm_artifacts", build)
    with pytest.raises(RuntimeError, match="stop before rendering"):
        confirm.confirm_issue(
            "fixture", 7, dry_run=True, programs_root=root,
            reports_root=tmp_path / "reports", archive_root=tmp_path / "archive",
        )
    assert len(captured) == 1
    assert captured[0].manifest_id == "id"
    assert captured[0].ai_safety == {"enabled": False}
    assert override_roots == [root]


def test_invalid_capture_stops_before_overrides_or_artifact_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    _manifest(root, {"manifest_id": "id", "metadata": {"draft_readiness": False}})
    monkeypatch.setattr(confirm, "load_bundle", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(confirm, "_load_draft_state", lambda *_args, **_kwargs: {})

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Invalid capture must stop before overrides, rendering or downstream effects.")

    monkeypatch.setattr(confirm, "_load_confirm_overrides", forbidden)
    monkeypatch.setattr(confirm, "_build_confirm_artifacts", forbidden)
    with pytest.raises(BadParameter):
        confirm.confirm_issue(
            "fixture", 7, dry_run=True, programs_root=root,
            reports_root=tmp_path / "reports", archive_root=tmp_path / "archive",
        )


def test_direct_artifact_construction_uses_capture_without_rereading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    path = _manifest(root, {"manifest_id": "id", "metadata": {"draft_readiness": {}}})
    captured = draft_loaders.load_confirm_draft_manifest("fixture", 7, draft_state={}, programs_root=root)
    path.write_bytes(b"new-invalid-revision")

    def stop_before_stores(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("stop before other evidence")

    monkeypatch.setattr(confirm, "resolve_edition", stop_before_stores)
    with pytest.raises(RuntimeError, match="stop before other evidence"):
        confirm._build_confirm_artifacts(
            "fixture", 7, SimpleNamespace(), OverridesDocument(issue_number=7, top_3_now=(), scorecards=()),
            {}, tmp_path / "reports", tmp_path / "archive", programs_root=root, draft_manifest=captured,
        )
    with pytest.raises(BadParameter):
        confirm._build_confirm_artifacts(
            "fixture", 7, SimpleNamespace(), OverridesDocument(issue_number=7, top_3_now=(), scorecards=()),
            {}, tmp_path / "reports", tmp_path / "archive", programs_root=root,
        )


def test_standalone_artifacts_forward_selected_root_to_scorecard_history_and_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    programs_root = workspace / "programs"
    reports_root = workspace / "reports"
    archive_root = workspace / "archive"
    manifest_path = _manifest(programs_root, {"manifest_id": "id"})
    draft_manifest = draft_loaders.load_confirm_draft_manifest("fixture", 7, draft_state={}, programs_root=programs_root)
    resolved = SimpleNamespace(
        program=SimpleNamespace(id="fixture", maturity_level=0),
        workstreams=(),
        scorecards=(),
        raw_program={},
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(confirm, "resolve_edition", lambda *_args, **_kwargs: resolved)
    monkeypatch.setattr(confirm, "load_trusted_baseline_issue", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(confirm, "_load_previous_snapshot", lambda **_kwargs: (None, None))
    monkeypatch.setattr(
        confirm, "build_signal_store_for_program_id",
        lambda *_args, **_kwargs: SimpleNamespace(read=lambda *_args, **_kwargs: (), read_reviews=lambda *_args: ()),
    )
    monkeypatch.setattr(confirm, "_build_continuity_deltas", lambda **_kwargs: ())
    monkeypatch.setattr(confirm, "_build_scorecard_packets", lambda *_args, **_kwargs: {})

    def scorecards(**kwargs):
        captured["scorecard_root"] = kwargs.get("programs_root")
        return (), (), ()

    monkeypatch.setattr(confirm, "_build_scorecard_data", scorecards)
    monkeypatch.setattr(confirm, "_build_exec_summary_text", lambda *_args, **_kwargs: "summary")
    monkeypatch.setattr(confirm, "read_archive_index", lambda *_args, **_kwargs: SimpleNamespace(issues=()))
    monkeypatch.setattr(confirm, "_visible_continuity_chapters", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(confirm, "_build_auto_suggested_top_items", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(confirm, "_build_exec_summary_template", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(confirm, "_build_workstream_templates", lambda *_args, **_kwargs: {})

    def load_narratives(*_args, **kwargs):
        captured["narrative_root"] = kwargs.get("programs_root")
        return {}

    monkeypatch.setattr(confirm, "load_narratives", load_narratives)
    monkeypatch.setattr(confirm, "_build_top_items", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(confirm, "_visible_detail_section_ids", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(confirm, "build_bridge_section_roster_ids", lambda **_kwargs: (set(), set()))
    monkeypatch.setattr(confirm, "load_latest_notification_state", lambda **_kwargs: None)

    def history(**kwargs):
        captured["history_root"] = kwargs.get("programs_root")
        return {}

    monkeypatch.setattr(confirm, "build_workstream_narrative_history", history)
    monkeypatch.setattr(confirm, "build_freshness_report", lambda **_kwargs: object())

    class StopAtReview(Exception):
        pass

    def load_review(*_args, **kwargs):
        captured["review_root"] = kwargs.get("programs_root")
        raise StopAtReview

    monkeypatch.setattr(confirm, "_load_confirm_review_status", load_review)

    with pytest.raises(StopAtReview):
        confirm._build_confirm_artifacts(
            "fixture",
            7,
            SimpleNamespace(
                config=SimpleNamespace(
                    ado=SimpleNamespace(date_window_days=30),
                    edition=SimpleNamespace(type="weekly"),
                    layout_mode="detailed",
                ),
                editorial_rules=SimpleNamespace(stale_warn_days=7, stale_block_days=30),
                program_context=SimpleNamespace(workstreams=()),
            ),
            OverridesDocument(issue_number=7, top_3_now=(), scorecards=()),
            {
                "ado_data_as_of": "2026-06-05T12:00:00+00:00",
                "items": [],
                "kusto_sections": [],
                "edition_type": "detailed",
            },
            reports_root,
            archive_root,
            programs_root=programs_root,
            draft_manifest=draft_manifest,
        )

    assert manifest_path.exists()
    assert captured == {
        "scorecard_root": programs_root,
        "narrative_root": programs_root,
        "history_root": programs_root,
        "review_root": programs_root,
    }


@pytest.mark.parametrize("mismatch", ["edition", "issue", "workspace"])
def test_capture_cannot_be_reused_for_another_confirmation_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str,
) -> None:
    root = tmp_path / "programs"
    _manifest(root, {"manifest_id": "id"})
    captured = draft_loaders.load_confirm_draft_manifest("fixture", 7, draft_state={}, programs_root=root)
    selected_workspace = tmp_path / "other" if mismatch == "workspace" else tmp_path

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Mismatched captured evidence must fail before other store reads.")

    monkeypatch.setattr(confirm, "resolve_edition", forbidden)
    with pytest.raises(BadParameter, match="scope"):
        confirm._build_confirm_artifacts(
            "other" if mismatch == "edition" else "fixture", 8 if mismatch == "issue" else 7,
            SimpleNamespace(), OverridesDocument(issue_number=7, top_3_now=(), scorecards=()),
            {}, selected_workspace / "reports", selected_workspace / "archive",
            programs_root=selected_workspace / "programs",
            draft_manifest=captured,
        )


@pytest.mark.parametrize("invalid", [False, True])
def test_capture_checks_supplied_draft_reference_without_another_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: bool,
) -> None:
    capture = replace(_capture(), config_hash=hash_jsonable({}))
    content = asdict(capture)
    content.pop("sha256")
    capture = replace(capture, sha256=hash_jsonable(content))
    payload, draft = _wire(capture)
    payload = json.loads(json.dumps(payload, default=lambda value: value.isoformat()))
    path = _manifest(tmp_path / "programs", {"manifest_id": "original", **payload})
    if invalid:
        draft["run_evidence_snapshot_sha256"] = "other"
    reads = []
    original = Path.read_text

    def read(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self == path:
            reads.append(self)
            path.write_text("changed after capture", encoding="utf-8")
        return value

    draft_reads = []

    def load_draft(*args, **kwargs):
        draft_reads.append(kwargs)
        return draft

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(confirm, "_load_draft_state", load_draft)
    monkeypatch.setattr(confirm, "load_bundle", lambda *args, **kwargs: SimpleNamespace(
        program=SimpleNamespace(id="fixture"), program_id="fixture", config={},
    ))

    def overrides(*args, **kwargs):
        assert not invalid
        return OverridesDocument(issue_number=7, top_3_now=(), scorecards=())

    def build(**kwargs):
        assert kwargs["draft_manifest"].run_evidence_snapshot == capture
        assert kwargs["draft_state"] is draft
        raise RuntimeError("verified immutable original capture")

    monkeypatch.setattr(confirm, "_load_confirm_overrides", overrides)
    monkeypatch.setattr(confirm, "_build_confirm_artifacts", build)
    with pytest.raises(BadParameter if invalid else RuntimeError):
        confirm.confirm_issue(
            "fixture", 7, dry_run=True, programs_root=tmp_path / "programs",
            reports_root=tmp_path / "reports", archive_root=tmp_path / "archive",
        )
    assert reads == [path]
    assert len(draft_reads) == 1


def test_confirm_uses_captured_identity_and_metadata_not_path_based_readers() -> None:
    path = Path(confirm.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"_load_current_draft_manifest_id", "_load_draft_ai_safety_metadata", "_load_draft_readiness_metadata"}
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden
        for node in ast.walk(tree)
    )
    projections = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "deepcopy"
        and len(node.args) == 1 and isinstance(node.args[0], ast.Attribute)
        and isinstance(node.args[0].value, ast.Name) and node.args[0].value.id == "draft_manifest"
    ]
    assert sorted(node.args[0].attr for node in projections) == ["draft_readiness"]
    metadata_assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "manifest_metadata" for target in node.targets)
    ]
    assert len(metadata_assignments) == 1
    metadata = metadata_assignments[0].value
    assert isinstance(metadata, ast.Dict)
    metadata_keys = {key.value for key in metadata.keys if isinstance(key, ast.Constant)}
    assert {"ai_safety", "milestone_assessments", "milestone_trajectory_snapshot"} <= metadata_keys
    assert sum(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "deepcopy"
        and len(node.args) == 1 and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "manifest_metadata"
        for node in ast.walk(tree)
    ) == 2
    assert any(
        isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "current_draft_manifest_id" for target in node.targets)
        and isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "draft_manifest" and node.value.attr == "manifest_id"
        for node in ast.walk(tree)
    )

from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer import BadParameter

from src.commands import confirm
from src.commands.confirm_stages import draft_loaders
from src.core.manifest_writer import hash_jsonable
from src.core.overrides_store import OverridesDocument


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
    captured = draft_loaders.load_confirm_draft_manifest("fixture", 7, programs_root=tmp_path)
    assert captured.manifest_id == "first"
    assert captured.config_hash == first["config_hash"]
    assert captured.ai_safety == {"revision": "first"}
    assert captured.draft_readiness == {"revision": "first"}
    assert reads == [path]
    with pytest.raises(FrozenInstanceError):
        setattr(captured, "manifest_id", "changed")
    newer = draft_loaders.load_confirm_draft_manifest("fixture", 7, programs_root=tmp_path)
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
        draft_loaders.load_confirm_draft_manifest("fixture", 7, programs_root=tmp_path)


@pytest.mark.parametrize("metadata", [None, {}, {"ai_safety": None, "draft_readiness": None}])
def test_capture_preserves_optional_legacy_metadata(tmp_path: Path, metadata: object) -> None:
    with pytest.raises(BadParameter):
        draft_loaders.load_confirm_draft_manifest("fixture", 7, programs_root=tmp_path)
    _manifest(tmp_path, {"manifest_id": "id", "metadata": metadata})
    captured = draft_loaders.load_confirm_draft_manifest("fixture", 7, programs_root=tmp_path)
    assert captured.manifest_id == "id"
    assert captured.ai_safety is None
    assert captured.draft_readiness is None


def test_confirm_passes_its_captured_manifest_to_artifact_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    _manifest(root, {"manifest_id": "id", "metadata": {"ai_safety": {"enabled": False}}})
    bundle = SimpleNamespace(program=SimpleNamespace(id="fixture"), config={})
    monkeypatch.setattr(confirm, "load_bundle", lambda *_args, **_kwargs: bundle)
    monkeypatch.setattr(confirm, "_load_draft_state", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        confirm, "_load_confirm_overrides",
        lambda *_args, **_kwargs: OverridesDocument(issue_number=7, top_3_now=(), scorecards=()),
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
    captured = draft_loaders.load_confirm_draft_manifest("fixture", 7, programs_root=root)
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


@pytest.mark.parametrize("mismatch", ["edition", "issue", "workspace"])
def test_capture_cannot_be_reused_for_another_confirmation_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str,
) -> None:
    root = tmp_path / "programs"
    _manifest(root, {"manifest_id": "id"})
    captured = draft_loaders.load_confirm_draft_manifest("fixture", 7, programs_root=root)
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
    assert sorted(node.args[0].attr for node in projections) == ["ai_safety", "ai_safety", "draft_readiness"]
    assert any(
        isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "current_draft_manifest_id" for target in node.targets)
        and isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "draft_manifest" and node.value.attr == "manifest_id"
        for node in ast.walk(tree)
    )

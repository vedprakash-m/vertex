from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from typer import BadParameter

from src.commands.confirm_stages import draft_loaders
from src.core import draft_artifact_store
from src.core.exceptions import StateError


def _manifest(root: Path, content: bytes) -> Path:
    path = root / "fixture" / "publications" / "fixture" / "issue_007" / "issue_007.manifest.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return path


def _tree(root: Path) -> dict[Path, bytes | None]:
    return {p.relative_to(root): p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


def _metadata(field: str, root: Path) -> dict[str, object] | None:
    loader = (
        draft_loaders.load_draft_ai_safety_metadata if field == "ai_safety"
        else draft_loaders.load_draft_readiness_metadata
    )
    return loader(edition_name="fixture", issue_number=7, programs_root=root)


@pytest.mark.parametrize("field", ["ai_safety", "draft_readiness"])
@pytest.mark.parametrize("content", [b"{PRIVATE-MANIFEST", b"\xff", b"[]", b"null", b"true"])
def test_failed_manifest_cannot_become_missing_safety_or_readiness(
    tmp_path: Path, field: str, content: bytes,
) -> None:
    root = tmp_path / "programs"
    _manifest(root, content)
    before = _tree(tmp_path)
    with pytest.raises(BadParameter) as caught:
        _metadata(field, root)
    assert "manifest" in str(caught.value)
    assert "PRIVATE-MANIFEST" not in str(caught.value)
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("field", ["ai_safety", "draft_readiness"])
@pytest.mark.parametrize("level", ["metadata", "field"])
@pytest.mark.parametrize("value", [[], "PRIVATE-METADATA", 1, False])
def test_present_non_object_metadata_is_invalid_not_absent(
    tmp_path: Path, field: str, level: str, value: object,
) -> None:
    root = tmp_path / "programs"
    metadata = value if level == "metadata" else {field: value}
    _manifest(root, json.dumps({"manifest_id": "m-7", "metadata": metadata}).encode())
    before = _tree(tmp_path)
    with pytest.raises(BadParameter, match="metadata") as caught:
        _metadata(field, root)
    assert "PRIVATE-METADATA" not in str(caught.value)
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("field", ["ai_safety", "draft_readiness"])
@pytest.mark.parametrize("metadata", [None, {}, {"other": {}}, {"ai_safety": None, "draft_readiness": None}])
def test_legacy_null_or_missing_metadata_remains_optional(
    tmp_path: Path, field: str, metadata: object,
) -> None:
    root = tmp_path / "programs"
    assert _metadata(field, root) is None
    _manifest(root, json.dumps({"manifest_id": "m-7", "metadata": metadata}).encode())
    before = _tree(tmp_path)
    assert _metadata(field, root) is None
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("field", ["ai_safety", "draft_readiness"])
@pytest.mark.parametrize("value", [{}, {"enabled": False, "extra": ["preserved"]}])
def test_valid_metadata_maps_are_retained_in_selected_workspace(
    tmp_path: Path, field: str, value: dict[str, object],
) -> None:
    root = tmp_path / "selected" / "programs"
    _manifest(root, json.dumps({"manifest_id": "m-7", "metadata": {field: value}}).encode())
    before = _tree(tmp_path)
    result = _metadata(field, root)
    assert result == value
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("field", ["ai_safety", "draft_readiness"])
def test_manifest_permission_failure_is_not_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    root = tmp_path / "programs"
    path = _manifest(root, b'{"manifest_id":"m-7"}')
    original = Path.read_text

    def read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if self == path:
            raise PermissionError("PRIVATE-ACCESS-DETAIL")
        return original(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(BadParameter) as caught:
        _metadata(field, root)
    assert "PermissionError" in str(caught.value)
    assert "PRIVATE-ACCESS-DETAIL" not in str(caught.value)
    assert path.read_bytes() == b'{"manifest_id":"m-7"}'


@pytest.mark.parametrize("identity", [None, "", " ", 1, 1.5, True, ["id"], {"id": "value"}])
def test_manifest_identity_is_not_coerced_from_invalid_values(tmp_path: Path, identity: object) -> None:
    root = tmp_path / "programs"
    _manifest(root, json.dumps({"manifest_id": identity}).encode())
    with pytest.raises(BadParameter):
        draft_loaders.load_current_draft_manifest_id("fixture", 7, programs_root=root)


@pytest.mark.parametrize("identity", ["m-7", "external-format", " legacy-id "])
def test_valid_manifest_identity_is_not_reformatted(tmp_path: Path, identity: str) -> None:
    root = tmp_path / "programs"
    _manifest(root, json.dumps({"manifest_id": identity}).encode())
    assert draft_loaders.load_current_draft_manifest_id("fixture", 7, programs_root=root) == identity


@pytest.mark.parametrize("content", [b"{PRIVATE-MANIFEST", b"\xff", b"[]", b"null", b"true"])
def test_required_identity_uses_the_same_safe_manifest_failure_boundary(tmp_path: Path, content: bytes) -> None:
    root = tmp_path / "programs"
    _manifest(root, content)
    before = _tree(tmp_path)
    with pytest.raises(BadParameter) as caught:
        draft_loaders.load_current_draft_manifest_id("fixture", 7, programs_root=root)
    assert "manifest" in str(caught.value)
    assert "PRIVATE-MANIFEST" not in str(caught.value)
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("field", ["ai_safety", "draft_readiness"])
def test_absent_metadata_key_remains_optional(tmp_path: Path, field: str) -> None:
    root = tmp_path / "programs"
    _manifest(root, b'{"manifest_id":"m-7"}')
    assert _metadata(field, root) is None


@pytest.mark.parametrize("field", ["ai_safety", "draft_readiness"])
def test_same_edition_in_distinct_workspaces_has_independent_metadata(tmp_path: Path, field: str) -> None:
    for name in ("first", "second"):
        root = tmp_path / name / "programs"
        _manifest(root, json.dumps({"manifest_id": name, "metadata": {field: {"workspace": name}}}).encode())
    for name in ("first", "second"):
        root = tmp_path / name / "programs"
        assert _metadata(field, root) == {"workspace": name}
        assert draft_loaders.load_current_draft_manifest_id("fixture", 7, programs_root=root) == name


@pytest.mark.parametrize("field", ["ai_safety", "draft_readiness", "identity"])
def test_unexpected_manifest_reader_errors_are_not_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    def broken_reader(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unexpected implementation failure")

    monkeypatch.setattr(draft_loaders, "load_optional_draft_manifest", broken_reader)
    with pytest.raises(RuntimeError, match="unexpected implementation"):
        if field == "identity":
            draft_loaders.load_current_draft_manifest_id("fixture", 7, programs_root=tmp_path)
        else:
            _metadata(field, tmp_path)


def test_shared_manifest_reader_is_optional_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    assert draft_artifact_store.load_draft_manifest("fixture", 7, programs_root=root) is None
    path = _manifest(root, b"{}")
    assert draft_artifact_store.load_draft_manifest("fixture", 7, programs_root=root) == {}
    path.write_bytes(b"[]")
    before = _tree(tmp_path)
    with pytest.raises(StateError):
        draft_artifact_store.load_draft_manifest("fixture", 7, programs_root=root)
    assert _tree(tmp_path) == before


def test_every_confirm_manifest_projection_forwards_selected_programs_root() -> None:
    path = Path(__file__).resolve().parents[2] / "src" / "commands" / "confirm.py"
    names = {"_load_confirm_draft_manifest"}
    calls = [
        node for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names
    ]
    assert {node.func.id for node in calls} == names
    for call in calls:
        assert any(
            keyword.arg == "programs_root" and isinstance(keyword.value, ast.Name) and keyword.value.id == "programs_root"
            for keyword in call.keywords
        ), f"Confirm projection at line {call.lineno} must use its selected program root."

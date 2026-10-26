from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from cli import app
import src.commands.kb as kb
from src.core.exceptions import StateError
from src.core.kb_updates import KbUpdateOperation, KbUpdatePlan
from tests.unit.test_kb_updates import _baseline_preview


@pytest.mark.parametrize(("mixed", "drift"), [(False, "before"), (True, "before"), (True, "during-shared")])
def test_kb_update_command_rejects_stale_local_preview_before_shared_write(
    tmp_path, monkeypatch, mixed: bool, drift: str,
) -> None:
    root, preview = _baseline_preview(tmp_path)
    operations = (KbUpdateOperation("workstreams.yaml", "set_fields", "ws", fields=(("name", "New"),)),)
    if mixed:
        operations += (KbUpdateOperation("knowledge/people_directory.yaml", "set_fields", "alice", fields=(("title", "PM"),)),)
    plan = KbUpdatePlan("acme", "synthetic correction", "deterministic", operations)
    monkeypatch.setattr(kb, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(kb, "parse_deterministic_kb_correction", lambda *args, **kwargs: plan)
    monkeypatch.setattr(kb, "shared_registry_is_active", lambda root: mixed)
    monkeypatch.setattr(kb, "prepare_kb_update", lambda *args, **kwargs: preview)
    def shared_patch(**kwargs):
        if kwargs["apply"] and drift == "during-shared":
            preview.changes[1].absolute_path.write_text("concurrent\n", encoding="utf-8")
        return SimpleNamespace(affected_paths=(), changes=(), conflicts=(), generation_id="synthetic")

    shared_writer = Mock(side_effect=shared_patch)
    monkeypatch.setattr(kb, "apply_shared_registry_patch", shared_writer)
    identity = Mock(return_value="synthetic-operator")
    monkeypatch.setattr(kb, "_resolve_operator_principal", identity)
    original_render = kb._render_kb_update_preview

    def render_then_change(value):
        rendered = original_render(value)
        if drift == "before":
            preview.changes[1].absolute_path.write_text("concurrent\n", encoding="utf-8")
        return rendered

    monkeypatch.setattr(kb, "_render_kb_update_preview", render_then_change)
    first_before = preview.changes[0].absolute_path.read_bytes()
    result = CliRunner().invoke(app, ["kb", "update", "synthetic correction", "--program", "acme", "--apply", "--no-ai"])
    assert result.exit_code == 2
    assert "scorecards.yaml" in result.output
    assert "preview" in result.output.lower()
    assert preview.changes[0].absolute_path.read_bytes() == first_before
    assert preview.changes[1].absolute_path.read_text() == "concurrent\n"
    assert not (root / "acme" / "journal").exists()
    assert any(call.kwargs.get("apply") for call in shared_writer.call_args_list) is (drift == "during-shared")
    if drift == "before":
        identity.assert_not_called()
    else:
        identity.assert_called_once()
    assert not isinstance(result.exception, StateError)


@pytest.mark.parametrize("mode", ["local", "mixed", "shared-only"])
def test_kb_update_command_unchanged_application_keeps_existing_routes(tmp_path, monkeypatch, mode: str) -> None:
    root, preview = _baseline_preview(tmp_path)
    operations = ()
    if mode != "shared-only":
        operations += (KbUpdateOperation("workstreams.yaml", "set_fields", "ws", fields=(("name", "New"),)),)
    if mode != "local":
        operations += (KbUpdateOperation("knowledge/people_directory.yaml", "set_fields", "alice", fields=(("title", "PM"),)),)
    plan = KbUpdatePlan("acme", "synthetic correction", "deterministic", operations)
    monkeypatch.setattr(kb, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(kb, "parse_deterministic_kb_correction", lambda *args, **kwargs: plan)
    monkeypatch.setattr(kb, "shared_registry_is_active", lambda root: mode != "local")
    prepare = Mock(return_value=preview)
    monkeypatch.setattr(kb, "prepare_kb_update", prepare)
    shared_writer = Mock(return_value=SimpleNamespace(
        affected_paths=(), changes=(), conflicts=(), generation_id="synthetic",
    ))
    monkeypatch.setattr(kb, "apply_shared_registry_patch", shared_writer)
    monkeypatch.setattr(kb, "_resolve_operator_principal", lambda *args: "synthetic-operator")
    result = CliRunner().invoke(app, ["kb", "update", "synthetic correction", "--program", "acme", "--apply", "--no-ai"])
    assert result.exit_code == 0, result.output
    assert "Applied" in result.output
    assert any(call.kwargs.get("apply") for call in shared_writer.call_args_list) is (mode != "local")
    if mode == "shared-only":
        prepare.assert_not_called()
        assert not (root / "acme" / "journal").exists()
    else:
        for change in preview.changes:
            assert change.absolute_path.read_text(encoding="utf-8") == change.after_text
        assert (root / "acme" / "journal" / "kb_edits.jsonl").exists()

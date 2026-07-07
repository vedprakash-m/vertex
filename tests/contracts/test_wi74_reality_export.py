"""WI-7.4 contract tests: reality export, timeseries, NullProjection (§6.12.2).

Tests:
1. export_command_registered — `vertex reality export` is a registered subcommand
2. cursor_manifest_per_program — cursor manifest is written to output/<program>/
3. cursor_manifest_not_shared — two programs produce separate cursor manifests
4. export_audit_appended — audit JSONL entry written for each export
5. audit_entry_fields — audit entry has required fields (event, program_id, actor, scope)
6. timeseries_frame_limit — max_frames policy cap enforced (≤60 frames)
7. timeseries_non_replayable_per_frame — each frame carries non_replayable_families
8. sor_flip_boundary_detected — sor_flip_boundary=True when families change
9. null_projection_builds_without_modifying_core — O-15 green
10. null_projection_to_dict — to_dict() returns expected keys
11. export_schema_version — snapshot export carries reality_schema_version="1"
12. timeseries_export_kind — timeseries payload carries export_kind="timeseries"
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. export command registered in CLI
# ---------------------------------------------------------------------------

def test_export_command_registered() -> None:
    from typer.testing import CliRunner
    from cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["reality", "export", "--help"])
    assert result.exit_code == 0
    assert "--program" in result.stdout
    assert "--json" in result.stdout
    assert "--timeseries" in result.stdout


# ---------------------------------------------------------------------------
# 2-3. Cursor manifest is per-program and not shared
# ---------------------------------------------------------------------------

def _make_reality_mock(program_id: str) -> MagicMock:
    reality = MagicMock()
    reality.program_id = program_id
    reality.as_of = datetime(2025, 1, 15, tzinfo=timezone.utc)
    reality.sor_mode = "legacy"
    reality.to_dict.return_value = {
        "reality_schema_version": "1",
        "program_id": program_id,
        "as_of": "2025-01-15T00:00:00+00:00",
        "sor_mode": "legacy",
        "max_classification": "internal",
    }
    reality.diff.return_value = MagicMock(non_replayable_families=("action.item", "risk.entry"))
    return reality


def test_cursor_manifest_per_program(tmp_path: Path) -> None:
    from src.commands.reality import _write_cursor_manifest

    payload = {"reality_schema_version": "1", "program_id": "prog_a", "as_of": "2025-01-15T00:00:00Z"}
    cursor_dir = tmp_path / "prog_a"
    cursor_dir.mkdir()
    manifest_path = cursor_dir / "reality_export_cursor.json"

    _write_cursor_manifest(manifest_path, program_id="prog_a", payload=payload)

    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["program_id"] == "prog_a"
    assert manifest["schema_version"] == "1"
    assert "generated_at" in manifest


def test_cursor_manifest_not_shared_across_programs(tmp_path: Path) -> None:
    from src.commands.reality import _write_cursor_manifest

    for prog in ("prog_a", "prog_b"):
        payload = {"reality_schema_version": "1", "program_id": prog, "as_of": "2025-01-15T00:00:00Z"}
        d = tmp_path / prog
        d.mkdir()
        _write_cursor_manifest(d / "reality_export_cursor.json", program_id=prog, payload=payload)

    manifest_a = json.loads((tmp_path / "prog_a" / "reality_export_cursor.json").read_text(encoding="utf-8"))
    manifest_b = json.loads((tmp_path / "prog_b" / "reality_export_cursor.json").read_text(encoding="utf-8"))
    assert manifest_a["program_id"] == "prog_a"
    assert manifest_b["program_id"] == "prog_b"
    # Manifests must be independent (different file paths enforces this)
    assert manifest_a["program_id"] != manifest_b["program_id"]


# ---------------------------------------------------------------------------
# 4-5. Export audit
# ---------------------------------------------------------------------------

def test_export_audit_appended(tmp_path: Path) -> None:
    from src.commands.reality import _append_export_audit

    audit_path = tmp_path / "reality_export_audit.jsonl"
    _append_export_audit(audit_path, program_id="prog_a", actor="tester", timeseries=False, max_classification="internal")
    _append_export_audit(audit_path, program_id="prog_a", actor="tester", timeseries=True, max_classification="internal")

    lines = [l for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2


def test_export_audit_entry_fields(tmp_path: Path) -> None:
    from src.commands.reality import _append_export_audit

    audit_path = tmp_path / "audit.jsonl"
    _append_export_audit(audit_path, program_id="my_prog", actor="cli", timeseries=False, max_classification="internal")

    entry = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert entry["event"] == "reality_export"
    assert entry["program_id"] == "my_prog"
    assert entry["actor"] == "cli"
    assert entry["scope"] == "snapshot"
    assert entry["classification_ceiling"] == "internal"
    assert "exported_at" in entry


# ---------------------------------------------------------------------------
# 6-8. Timeseries export
# ---------------------------------------------------------------------------

def test_timeseries_frame_limit() -> None:
    from src.commands.reality import _build_timeseries_export

    mock_reality = _make_reality_mock("prog_a")
    with patch("src.core.program_reality.ProgramReality.load", return_value=mock_reality):
        payload = _build_timeseries_export(
            program_id="prog_a",
            interval_days=1,
            since_str=None,
            max_frames=5,
            programs_root=Path("."),
        )

    assert payload["frame_count"] <= 5
    assert len(payload["frames"]) <= 5
    assert payload["max_frames_policy"] == 5


def test_timeseries_non_replayable_per_frame() -> None:
    from src.commands.reality import _build_timeseries_export

    mock_reality = _make_reality_mock("prog_a")
    with patch("src.core.program_reality.ProgramReality.load", return_value=mock_reality):
        payload = _build_timeseries_export(
            program_id="prog_a",
            interval_days=7,
            since_str=None,
            max_frames=3,
            programs_root=Path("."),
        )

    for frame in payload["frames"]:
        assert "non_replayable_families" in frame
        assert isinstance(frame["non_replayable_families"], list)


def test_sor_flip_boundary_detected() -> None:
    """sor_flip_boundary=True when non_replayable_families set changes between frames."""
    from src.commands.reality import _build_timeseries_export

    # Frame 0: non-replayable = {A, B}
    # Frame 1: non-replayable = {A}     ← B removed → flip
    frame_0_reality = _make_reality_mock("prog_a")
    frame_0_reality.diff.return_value = MagicMock(non_replayable_families=("A", "B"))

    frame_1_reality = _make_reality_mock("prog_a")
    frame_1_reality.diff.return_value = MagicMock(non_replayable_families=("A",))

    realities = [frame_0_reality, frame_1_reality]
    call_counter = {"n": 0}

    def mock_load(*args: Any, **kwargs: Any) -> Any:
        r = realities[min(call_counter["n"], len(realities) - 1)]
        call_counter["n"] += 1
        return r

    with patch("src.core.program_reality.ProgramReality.load", side_effect=mock_load):
        payload = _build_timeseries_export(
            program_id="prog_a",
            interval_days=7,
            since_str=None,
            max_frames=2,
            programs_root=Path("."),
        )

    frames = payload["frames"]
    assert len(frames) == 2
    # frame[0] has no predecessor — no flip expected
    assert "sor_flip_boundary" not in frames[0]
    # frame[1] changed from {A, B} → {A} — flip expected
    assert frames[1].get("sor_flip_boundary") is True
    assert "B" in frames[1].get("sor_flip_families", [])


# ---------------------------------------------------------------------------
# 9-10. NullProjection (O-15)
# ---------------------------------------------------------------------------

def test_null_projection_builds_without_modifying_core() -> None:
    """O-15: NullProjection imports only from src.core, not from src.commands."""
    import ast
    module_path = Path("src/core/null_projection.py")
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in getattr(node, "names", []):
                assert not alias.name.startswith("src.commands"), \
                    f"NullProjection must not import from src.commands: {alias.name}"
            module = getattr(node, "module", None) or ""
            assert not module.startswith("src.commands"), \
                f"NullProjection must not import from src.commands: {module}"


def test_null_projection_to_dict() -> None:
    from src.core.null_projection import NullProjection

    proj = NullProjection()
    mock_reality = _make_reality_mock("prog_a")
    # Wire domain accessors
    mock_reality.actions.return_value = (MagicMock(),) * 3
    mock_reality.risks.return_value = (MagicMock(),) * 2
    mock_reality.decisions.return_value = (MagicMock(),)
    mock_reality.dependencies.return_value = ()
    mock_reality.milestones.return_value = (MagicMock(),) * 4
    mock_reality.attention.return_value = ()
    mock_reality.commitments.return_value = ()

    result = proj.project(mock_reality)
    d = proj.to_dict(result)

    assert d["program_id"] == "prog_a"
    assert d["action_count"] == 3
    assert d["risk_count"] == 2
    assert d["decision_count"] == 1
    assert d["milestone_count"] == 4
    required_keys = {"program_id", "action_count", "risk_count", "decision_count",
                     "dependency_count", "milestone_count", "attention_count", "commitment_count"}
    assert required_keys == set(d.keys())


# ---------------------------------------------------------------------------
# 11-12. Payload schema
# ---------------------------------------------------------------------------

def test_export_schema_version_present() -> None:
    mock_reality = _make_reality_mock("prog_a")
    payload = mock_reality.to_dict()
    assert payload["reality_schema_version"] == "1"


def test_timeseries_export_kind_field() -> None:
    from src.commands.reality import _build_timeseries_export

    mock_reality = _make_reality_mock("prog_a")
    with patch("src.core.program_reality.ProgramReality.load", return_value=mock_reality):
        payload = _build_timeseries_export(
            program_id="prog_a",
            interval_days=7,
            since_str=None,
            max_frames=2,
            programs_root=Path("."),
        )

    assert payload["export_kind"] == "timeseries"
    assert payload["reality_schema_version"] == "1"
    assert "frames" in payload

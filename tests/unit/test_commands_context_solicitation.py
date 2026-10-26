from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.core.context_gap_store import ContextGapRecord


def _workspace(root: Path) -> tuple[Path, str]:
    programs = root / "programs"
    program = programs / "sample"
    editions = program / "editions"
    editions.mkdir(parents=True)
    (editions / "sample_weekly.yaml").write_text("program_id: sample\n", encoding="utf-8")
    (program / "workstream_registry.yaml").write_text(
        "workstreams:\n  - id: deployment\n    deep_context:\n      what: Existing scope\n",
        encoding="utf-8",
    )
    feedback = program / "_feedback"
    feedback.mkdir()
    gap = ContextGapRecord(
        ts=datetime(2026, 10, 24, tzinfo=timezone.utc), feature="workstream_registry",
        program="sample", lane="deployment", field="deep_context.why",
        severity="quality_degraded", message="Purpose is missing", impact_estimate="high",
    )
    (feedback / "context_gaps.jsonl").write_text(json.dumps(gap.to_json()) + "\n", encoding="utf-8")
    return programs, "sample:workstream_registry:deployment:deep_context.why"


def _args(programs: Path, fingerprint: str) -> list[str]:
    return [
        "context", "solicit", "--edition", "sample_weekly",
        "--gap-fingerprint", fingerprint, "--to", "reviewer@example.com",
        "--programs-root", str(programs),
    ]


def _files(root: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("change", ["foreign", "unsupported", "recipient", "corrupt"])
def test_invalid_solicitation_inputs_are_nonmutating(tmp_path: Path, change: str) -> None:
    from cli import app

    programs, fingerprint = _workspace(tmp_path)
    args = _args(programs, fingerprint)
    if change == "foreign":
        args[args.index(fingerprint)] = fingerprint.replace("sample:", "other:", 1)
    elif change == "unsupported":
        args[args.index(fingerprint)] = fingerprint.replace("deep_context.why", "owner")
    elif change == "recipient":
        args[args.index("reviewer@example.com")] = "reviewer@example.com\r\nBcc: outsider@example.com"
    else:
        (programs / "sample" / "_feedback" / "context_gap_solicitations.jsonl").write_text("{bad\n")
    before = _files(tmp_path)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert _files(tmp_path) == before


def test_installed_solicitation_dry_run_is_nonmutating(tmp_path: Path) -> None:
    from cli import app

    programs, fingerprint = _workspace(tmp_path)
    before = _files(tmp_path)
    result = CliRunner().invoke(app, _args(programs, fingerprint) + ["--dry-run"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "pending" in result.output and "unverified" in result.output
    assert _files(tmp_path) == before


def test_installed_solicitation_creates_pending_bound_packet_and_enforces_cooldown(tmp_path: Path) -> None:
    from cli import app

    programs, fingerprint = _workspace(tmp_path)
    authored = _files(tmp_path)
    result = CliRunner().invoke(app, _args(programs, fingerprint))
    assert result.exit_code == 0, result.output + repr(result.exception)
    program = programs / "sample"
    drafts = list((program / "nudge" / "drafts").glob("*.eml"))
    assert len(drafts) == 1
    raw = drafts[0].read_bytes()
    assert b"X-Unsent: 1" in raw and b"Reference: solicitation-v1-" in raw
    record = json.loads((program / "_feedback" / "context_gap_solicitations.jsonl").read_text())
    assert record["program_id"] == "sample"
    assert record["target_key"] == "deployment"
    assert record["target_field"] == "deep_context.why"
    assert record["current_value"] == "<missing>"
    assert record["current_value_hash"].startswith("intake-v1:")
    assert record["packet_version"] == 1
    assert not (program / "nudge" / "draft_approvals.json").exists()
    assert all(path.read_bytes() == value for path, value in authored.items())
    before = _files(tmp_path)
    repeated = CliRunner().invoke(app, _args(programs, fingerprint))
    assert repeated.exit_code == 2 and "cooldown" in repeated.output.lower()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("args,code", [(["--help"], 0), (["--invalid"], 2), ([], 2)])
def test_installed_solicitation_option_validation(args: list[str], code: int) -> None:
    from cli import app

    result = CliRunner().invoke(app, ["context", "solicit", *args])
    assert result.exit_code == code, result.output + repr(result.exception)
    if code == 0:
        assert "--to" in result.output and "--programs-root" not in result.output


@pytest.mark.parametrize("content", ["[]\n", "{invalid\n", "workstreams: [\n", "workstreams: []\n"])
def test_invalid_registry_or_gap_records_fail_cleanly(tmp_path: Path, content: str) -> None:
    from cli import app

    programs, fingerprint = _workspace(tmp_path)
    if content in {"[]\n", "{invalid\n"}:
        path = programs / "sample" / "_feedback" / "context_gaps.jsonl"
    else:
        path = programs / "sample" / "workstream_registry.yaml"
    path.write_text(content)
    before = _files(tmp_path)
    result = CliRunner().invoke(app, _args(programs, fingerprint))
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert _files(tmp_path) == before


@pytest.mark.parametrize("value,expected", [("null", "<null>"), ('""', ""), ('"line one\\nline two"', "line one\nline two")])
def test_packet_binding_preserves_typed_scalar_baseline(tmp_path: Path, value: str, expected: str) -> None:
    from cli import app

    programs, fingerprint = _workspace(tmp_path)
    (programs / "sample" / "workstream_registry.yaml").write_text(
        f"workstreams:\n  - id: deployment\n    deep_context:\n      why: {value}\n",
    )
    result = CliRunner().invoke(app, _args(programs, fingerprint))
    assert result.exit_code == 0, result.output + repr(result.exception)
    record = json.loads((programs / "sample" / "_feedback" / "context_gap_solicitations.jsonl").read_text())
    assert record["current_value"] == expected


@pytest.mark.parametrize("target", ["program", "drafts", "cooldown"])
def test_solicitation_rejects_resolved_path_escape(tmp_path: Path, target: str) -> None:
    from cli import app

    programs, fingerprint = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    if target == "program":
        program = programs / "sample"
        program.rename(outside / "sample")
        link, destination = program, outside / "sample"
    elif target == "drafts":
        link = programs / "sample" / "nudge" / "drafts"
        link.parent.mkdir()
        destination = outside
    else:
        link = programs / "sample" / "_feedback" / "context_gap_solicitations.jsonl"
        destination = outside / "cooldown.jsonl"
        destination.write_text("")
    try:
        link.symlink_to(destination, target_is_directory=target != "cooldown")
    except OSError as error:
        pytest.skip(f"Local symbolic links unavailable: {error}")
    before = _files(outside)
    result = CliRunner().invoke(app, _args(programs, fingerprint))
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert _files(outside) == before

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner
import pytest

from src.commands import context
from src.core.ncfl_proposal_store import load_proposals
from tests.unit.test_context_gap_reply_import import _intake_workspace


@pytest.mark.parametrize("corruption", [
    "missing", "null", "empty", "legacy-current", "wrong-prefix", "uppercase",
    "short", "nonhex", "trailing-newline", "boolean", "number", "list",
])
def test_installed_manual_diff_rejects_corrupt_reply_baseline_without_effects(
    tmp_path: Path, corruption: str,
) -> None:
    from cli import app
    from src.core.ncfl_proposal_store import get_proposals_path
    from tests.unit.test_context_gap_reply_import import _bound_intake_workspace

    programs, arguments, packet = _bound_intake_workspace(tmp_path)
    runner = CliRunner()
    intake = runner.invoke(app, [
        "context", "import-reply", "--edition", "sample_weekly", "--issue", "1",
        "--source", str(arguments["source"]), "--gap-fingerprint", arguments["gap_fingerprint"],
        "--solicitation-id", packet.id, "--programs-root", str(programs),
    ])
    assert intake.exit_code == 0, intake.output + repr(intake.exception)
    proposal, = load_proposals("sample", programs_root=programs)
    registry = programs / "sample" / "workstream_registry.yaml"
    registry.write_text("workstreams:\n- id: deployment\n  deep_context: {why: Changed}\n")
    path = get_proposals_path("sample", 1, programs_root=programs)
    payload = json.loads(path.read_text(encoding="utf-8"))
    corrupt_values = {
        "null": None, "empty": "",
        "legacy-current": hashlib.sha256(b"Changed").hexdigest(),
        "wrong-prefix": "intake-v2:" + "a" * 64,
        "uppercase": "intake-v1:" + "A" * 64,
        "short": "intake-v1:" + "a" * 63,
        "nonhex": "intake-v1:" + "g" * 64,
        "trailing-newline": "intake-v1:" + "a" * 64 + "\n",
        "boolean": False, "number": 1, "list": [],
    }
    if corruption == "missing":
        payload[0].pop("current_value_hash")
    else:
        payload[0]["current_value_hash"] = corrupt_values[corruption]
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = runner.invoke(app, [
        "context", "manual-diff", "--edition", "sample_weekly", "--issue", "1",
        "--proposal-id", proposal.proposal_id, "--programs-root", str(programs),
    ])
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "canonical intake-v1" in result.output
    assert "--- " not in result.output and "+++ " not in result.output and "@@" not in result.output
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_installed_bound_reply_manual_diff_preserves_pending_origin_and_stale_guard(tmp_path: Path) -> None:
    from cli import app
    from tests.unit.test_context_gap_reply_import import _bound_intake_workspace

    programs, arguments, packet = _bound_intake_workspace(tmp_path)
    runner = CliRunner()
    intake = runner.invoke(app, [
        "context", "import-reply", "--edition", "sample_weekly", "--issue", "1",
        "--source", str(arguments["source"]), "--gap-fingerprint", arguments["gap_fingerprint"],
        "--solicitation-id", packet.id, "--programs-root", str(programs),
    ])
    assert intake.exit_code == 0, intake.output + repr(intake.exception)
    proposal, = load_proposals("sample", programs_root=programs)
    assert proposal.status == "pending" and not proposal.decision_history and not proposal.batch_eligible
    command = [
        "context", "manual-diff", "--edition", "sample_weekly", "--issue", "1",
        "--proposal-id", proposal.proposal_id, "--programs-root", str(programs),
    ]
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    rendered = runner.invoke(app, command)
    assert rendered.exit_code == 0, rendered.output + repr(rendered.exception)
    assert "unverified" in rendered.output and "packet-time" in rendered.output
    assert "+    why:" in rendered.output
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    registry = programs / "sample" / "workstream_registry.yaml"
    registry.write_text("workstreams:\n- id: deployment\n  deep_context: {why: Changed}\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    stale = runner.invoke(app, command)
    assert stale.exit_code == 2, stale.output + repr(stale.exception)
    assert "stale" in stale.output
    assert "--- " not in stale.output and "+++ " not in stale.output
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert load_proposals("sample", programs_root=programs) == (proposal,)


def test_installed_bound_intake_and_stale_replay_are_pending_only(tmp_path: Path) -> None:
    from cli import app
    from tests.unit.test_context_gap_reply_import import _bound_intake_workspace

    programs, arguments, packet = _bound_intake_workspace(tmp_path)
    args = [
        "context", "import-reply", "--edition", "sample_weekly", "--issue", "1",
        "--source", str(arguments["source"]), "--gap-fingerprint", arguments["gap_fingerprint"],
        "--solicitation-id", packet.id, "--programs-root", str(programs),
    ]
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    preview = CliRunner().invoke(app, args + ["--dry-run"])
    assert preview.exit_code == 0, preview.output + repr(preview.exception)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output + repr(result.exception)
    proposal, = load_proposals("sample", programs_root=programs)
    assert proposal.current_value_hash == packet.current_value_hash
    assert "packet-time" in proposal.extraction_method_rationale
    assert proposal.status == "pending" and not proposal.decision_history and not proposal.batch_eligible
    (programs / "sample" / "workstream_registry.yaml").write_text(
        "workstreams:\n- id: deployment\n  deep_context: {why: Changed}\n"
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    stale = CliRunner().invoke(app, args)
    assert stale.exit_code == 2, stale.output + repr(stale.exception)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_import_reply_command_dry_run_and_pending_only(tmp_path: Path, monkeypatch) -> None:
    source, fingerprint = _intake_workspace(tmp_path)
    monkeypatch.setattr(context, "_resolve_program_id", lambda edition, *, programs_root: "sample")
    args = [
        "import-reply", "--edition", "sample_weekly", "--issue", "1",
        "--source", str(source), "--gap-fingerprint", fingerprint, "--solicitation-id", "sol-1",
        "--programs-root", str(tmp_path),
    ]
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    preview = CliRunner().invoke(context.app, args + ["--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert "unverified" in preview.output
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    result = CliRunner().invoke(context.app, args)
    assert result.exit_code == 0, result.output
    assert "pending" in result.output
    assert len(load_proposals("sample", programs_root=tmp_path)) == 1


def test_import_reply_command_rejects_unmatched_reference(tmp_path: Path, monkeypatch) -> None:
    source, fingerprint = _intake_workspace(tmp_path)
    monkeypatch.setattr(context, "_resolve_program_id", lambda edition, *, programs_root: "sample")
    result = CliRunner().invoke(context.app, [
        "import-reply", "--edition", "sample_weekly", "--issue", "1", "--source", str(source),
        "--gap-fingerprint", fingerprint, "--solicitation-id", "other", "--programs-root", str(tmp_path),
    ])
    assert result.exit_code != 0
    assert not (tmp_path / "sample" / "context_proposals").exists()


def test_import_reply_uses_real_selected_edition_root(tmp_path: Path) -> None:
    source, fingerprint = _intake_workspace(tmp_path)
    editions = tmp_path / "sample" / "editions"
    editions.mkdir()
    (editions / "sample_weekly.yaml").write_text("program_id: sample\n")
    result = CliRunner().invoke(context.app, [
        "import-reply", "--edition", "sample_weekly", "--issue", "1", "--source", str(source),
        "--gap-fingerprint", fingerprint, "--solicitation-id", "sol-1",
        "--programs-root", str(tmp_path), "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "sample" / "context_proposals").exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_installed_handler_limits_writes_to_selected_pending_queue(tmp_path: Path, dry_run: bool) -> None:
    from cli import app
    source, fingerprint = _intake_workspace(tmp_path)
    editions = tmp_path / "sample" / "editions"
    editions.mkdir()
    (editions / "sample_weekly.yaml").write_text("program_id: sample\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    args = [
        "context", "import-reply", "--edition", "sample_weekly", "--issue", "1",
        "--source", str(source), "--gap-fingerprint", fingerprint, "--solicitation-id", "sol-1",
        "--programs-root", str(tmp_path),
    ]
    result = CliRunner().invoke(app, args + (["--dry-run"] if dry_run else []))
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "unverified" in result.output
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert all(after[path] == content for path, content in before.items())
    added = set(after) - set(before)
    queue = tmp_path / "sample" / "context_proposals"
    assert added == (set() if dry_run else {
        queue / "issue_001.proposals.json", queue / "_conflict_index.json", queue / "_queue.lock",
    })


@pytest.mark.parametrize("arguments,exit_code", [
    (["--help"], 0), (["--invalid-option"], 2), ([], 2),
])
def test_real_installed_intake_help_and_invalid_options_are_nonmutating(
    tmp_path: Path, arguments: list[str], exit_code: int,
) -> None:
    from cli import app
    result = CliRunner().invoke(app, ["context", "import-reply", *arguments])
    assert result.exit_code == exit_code, result.output + repr(result.exception)
    if arguments == ["--help"]:
        for option in ("--edition", "--issue", "--source", "--gap-fingerprint", "--solicitation-id", "--dry-run"):
            assert option in result.output
        assert "--programs-root" not in result.output
    assert not list(tmp_path.iterdir())


def test_real_installed_intake_invalid_evidence_is_nonmutating(tmp_path: Path) -> None:
    from cli import app
    source, fingerprint = _intake_workspace(tmp_path)
    editions = tmp_path / "sample" / "editions"
    editions.mkdir()
    (editions / "sample_weekly.yaml").write_text("program_id: sample\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = CliRunner().invoke(app, [
        "context", "import-reply", "--edition", "sample_weekly", "--issue", "1",
        "--source", str(source), "--gap-fingerprint", fingerprint,
        "--solicitation-id", "unmatched", "--programs-root", str(tmp_path),
    ])
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "uniquely match" in result.output
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_real_intake_to_manual_diff_then_intervening_edit_is_stale(tmp_path: Path) -> None:
    from cli import app
    source, fingerprint = _intake_workspace(tmp_path)
    editions = tmp_path / "sample" / "editions"
    editions.mkdir()
    (editions / "sample_weekly.yaml").write_text("program_id: sample\n")
    intake = CliRunner().invoke(app, [
        "context", "import-reply", "--edition", "sample_weekly", "--issue", "1",
        "--source", str(source), "--gap-fingerprint", fingerprint,
        "--solicitation-id", "sol-1", "--programs-root", str(tmp_path),
    ])
    assert intake.exit_code == 0, intake.output
    proposal, = load_proposals("sample", programs_root=tmp_path)
    arguments = [
        "manual-diff", "--edition", "sample_weekly", "--issue", "1",
        "--proposal-id", proposal.proposal_id, "--programs-root", str(tmp_path),
    ]
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    rendered = CliRunner().invoke(context.app, arguments)
    assert rendered.exit_code == 0, rendered.output + repr(rendered.exception)
    assert "unverified" in rendered.output
    assert "intake-time baseline" in rendered.output
    assert '+    why: "The exact proposed answer."' in rendered.output
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    registry = tmp_path / "sample" / "workstream_registry.yaml"
    registry.write_text("workstreams:\n- id: lane-a\n  deep_context: {why: Manual repair}\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    stale = CliRunner().invoke(context.app, arguments)
    assert stale.exit_code == 2
    assert "stale" in stale.output
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("separator", ["p", "div", "br"])
def test_installed_html_reply_matches_plaintext_pending_and_dry_run(
    tmp_path: Path, multipart: bool, separator: str,
) -> None:
    from email.message import EmailMessage
    from cli import app
    from tests.unit.test_context_gap_reply_import import _bound_intake_workspace

    programs, arguments, packet = _bound_intake_workspace(tmp_path)
    source = arguments["source"]
    args = [
        "context", "import-reply", "--edition", "sample_weekly", "--issue", "1",
        "--source", str(source), "--gap-fingerprint", arguments["gap_fingerprint"],
        "--solicitation-id", packet.id, "--programs-root", str(programs),
    ]
    runner = CliRunner()
    plain = EmailMessage()
    plain.set_content(f"Purpose & scope.\nSecond line.\nReference: {packet.id}")
    source.write_bytes(plain.as_bytes())
    plain_preview = runner.invoke(app, args + ["--dry-run"])
    assert plain_preview.exit_code == 0, plain_preview.output
    lines = ["Purpose &amp; scope.", "Second line.", f"Reference: {packet.id}"]
    body = ("<br>".join(lines) if separator == "br"
            else "".join(f"<{separator}>{line}</{separator}>" for line in lines))
    html = EmailMessage()
    html.set_content(body, subtype="html")
    if multipart:
        html.make_mixed()
    source.write_bytes(html.as_bytes())
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    preview = runner.invoke(app, args + ["--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert preview.output.splitlines()[-2:] == plain_preview.output.splitlines()[-2:]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    proposal, = load_proposals("sample", programs_root=programs)
    assert proposal.source_value == "Purpose & scope.\nSecond line."
    assert proposal.current_value_hash == packet.current_value_hash
    assert proposal.status == "pending" and not proposal.decision_history and not proposal.batch_eligible
    assert "manual-only" in result.output and "unverified" in result.output
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert all(after[path] == content for path, content in before.items())
    queue = programs / "sample" / "context_proposals"
    assert set(after) - set(before) == {
        queue / "issue_001.proposals.json", queue / "_conflict_index.json", queue / "_queue.lock",
    }


@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("references", [("foreign",), ("matching", "foreign")])
def test_installed_html_reply_rejects_foreign_or_ambiguous_reference_without_writes(
    tmp_path: Path, multipart: bool, references: tuple[str, ...],
) -> None:
    from email.message import EmailMessage
    from cli import app
    from tests.unit.test_context_gap_reply_import import _bound_intake_workspace

    programs, arguments, packet = _bound_intake_workspace(tmp_path)
    message = EmailMessage()
    body = "<p>Proposed answer.</p>" + "".join(
        f"<div>Reference: {packet.id if reference == 'matching' else 'foreign'}</div>"
        for reference in references
    )
    message.set_content(body, subtype="html")
    if multipart:
        message.make_mixed()
    arguments["source"].write_bytes(message.as_bytes())
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = CliRunner().invoke(app, [
        "context", "import-reply", "--edition", "sample_weekly", "--issue", "1",
        "--source", str(arguments["source"]), "--gap-fingerprint", arguments["gap_fingerprint"],
        "--solicitation-id", packet.id, "--programs-root", str(programs),
    ])
    assert result.exit_code == 2, result.output
    assert "unverified Reference" in result.output
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before

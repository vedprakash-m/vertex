"""``vertex rev init-inbox`` — local-import inbox scaffolding (P1-5).

Verifies the command creates the 3-directory atomicity tree
(``rev_inbox/{claimed,processed,quarantine}``) + the program ``_rev/``
checkpoint dir, writes an operator-facing ``README.md`` documenting the
export-import workflow + OA-4 privacy policy, and is idempotent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.commands.rev import app as rev_app

runner = CliRunner()


def test_init_inbox_creates_dirs_and_readme(tmp_path: Path) -> None:
    inbox = tmp_path / "rev_inbox"
    result = runner.invoke(
        rev_app,
        ["init-inbox", "--program", "prog-demo", "--eml-inbox", str(inbox),
         "--programs-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    # 3-directory atomicity tree.
    assert (inbox / "claimed").is_dir()
    assert (inbox / "processed").is_dir()
    assert (inbox / "quarantine").is_dir()
    # Program checkpoint dir (last_cycle.json / cycle_history.jsonl live here).
    assert (tmp_path / "prog-demo" / "_rev").is_dir()
    # Operator-facing README documents the workflow + OA-4 privacy.
    readme = (inbox / "README.md")
    assert readme.is_file()
    text = readme.read_text(encoding="utf-8")
    assert "prog-demo" in text
    assert "OA-4" in text
    assert "vertex rev run" in text
    assert "crash_loop" in text  # crash-loop guard documented


def test_init_inbox_idempotent(tmp_path: Path) -> None:
    inbox = tmp_path / "rev_inbox"
    args = ["init-inbox", "--program", "prog-demo", "--eml-inbox", str(inbox),
            "--programs-root", str(tmp_path)]
    first = runner.invoke(rev_app, args)
    assert first.exit_code == 0
    # Second run must not error; it refreshes the README.
    second = runner.invoke(rev_app, args)
    assert second.exit_code == 0
    assert (inbox / "README.md").is_file()


def test_init_inbox_default_path_under_programs_root(tmp_path: Path) -> None:
    # No --eml-inbox → defaults to programs/<program>/rev_inbox.
    result = runner.invoke(
        rev_app,
        ["init-inbox", "--program", "prog-default", "--programs-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    inbox = tmp_path / "prog-default" / "rev_inbox"
    assert (inbox / "claimed").is_dir()
    assert (inbox / "processed").is_dir()
    assert (inbox / "quarantine").is_dir()
    assert (tmp_path / "prog-default" / "_rev").is_dir()


def test_init_inbox_readme_mentions_export_workflow(tmp_path: Path) -> None:
    inbox = tmp_path / "rev_inbox"
    result = runner.invoke(
        rev_app,
        ["init-inbox", "--program", "prog-abc", "--eml-inbox", str(inbox),
         "--programs-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    text = (inbox / "README.md").read_text(encoding="utf-8")
    # The README must explain where to drop files and the privacy retention.
    assert ".eml" in text
    assert "90 days" in text or "90-day" in text.lower() or "purged" in text.lower()
    assert "ACL" in text or "account" in text.lower()


def test_init_inbox_accepts_ics_inbox_override(tmp_path: Path) -> None:
    """ADF-W3.2 (Section 8.6.4): CLI parity -- ``init-inbox`` must accept the
    same ``--ics-inbox`` override ``rev run`` already accepts, since the
    3-directory atomicity mechanics are format-agnostic."""
    inbox = tmp_path / "cal_inbox"
    result = runner.invoke(
        rev_app,
        ["init-inbox", "--program", "prog-ics", "--ics-inbox", str(inbox),
         "--programs-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert (inbox / "claimed").is_dir()
    assert (inbox / "processed").is_dir()
    assert (inbox / "quarantine").is_dir()
    text = (inbox / "README.md").read_text(encoding="utf-8")
    assert "--ics-inbox" in text


def test_init_inbox_accepts_docs_inbox_override(tmp_path: Path) -> None:
    """ADF-W3.2 (Section 8.6.4): CLI parity -- ``init-inbox`` must accept the
    same ``--docs-inbox`` override ``rev run`` already accepts."""
    inbox = tmp_path / "docs_inbox"
    result = runner.invoke(
        rev_app,
        ["init-inbox", "--program", "prog-docs", "--docs-inbox", str(inbox),
         "--programs-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert (inbox / "claimed").is_dir()
    assert (inbox / "processed").is_dir()
    assert (inbox / "quarantine").is_dir()
    text = (inbox / "README.md").read_text(encoding="utf-8")
    assert "--docs-inbox" in text
    # The printed "next step" command must reference the flag actually used,
    # not silently default back to --eml-inbox.
    assert "--docs-inbox" in result.output
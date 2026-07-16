"""ADF-W3.2: ``vertex rev run --docs-inbox`` CLI wiring.

``src/m365/rev/local_file_enumerator.py``/``local_file_hydrator.py`` (P3-5)
existed with zero CLI callers -- ``rev.py`` only recognized ``--eml-inbox``/
``--ics-inbox``. Verifies the new ``--docs-inbox`` flag actually routes a
real REV cycle through ``LocalFileEnumerator``/``LocalFileHydrator``, not
just that the flag is accepted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.commands.rev import P0_SPIKE_NOTE, app as rev_app

docx = pytest.importorskip("docx", reason="python-docx not installed")

runner = CliRunner()


def _make_docx(path: Path, text: str = "Program status update.") -> Path:
    doc = docx.Document()
    doc.add_paragraph(text)
    doc.save(str(path))
    return path


def test_rev_run_docs_inbox_claims_and_processes_a_real_docx(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    docs_inbox = tmp_path / "rev_docs_inbox"
    docs_inbox.mkdir(parents=True)
    _make_docx(docs_inbox / "status.docx")

    result = runner.invoke(
        rev_app,
        [
            "run",
            "--program", "demo",
            "--mailbox", "tpm@example.com",
            "--docs-inbox", str(docs_inbox),
            "--programs-root", str(programs_root),
        ],
    )

    assert result.exception is None, result.output
    # Command must have taken the LocalFileEnumerator branch, not the
    # P0_SPIKE_NOTE early-exit that fires when no inbox/fixture is recognized.
    assert P0_SPIKE_NOTE not in result.output
    # The file was claimed out of the inbox root (3-directory atomicity) --
    # proves LocalFileEnumerator really ran, not just that the flag parsed.
    assert not (docs_inbox / "status.docx").exists()
    assert (docs_inbox / "claimed" / "status.docx").exists()

    report, _ = json.JSONDecoder().raw_decode(result.output)
    assert report["enumerated"] == 1
    assert report["hydrated"] == 1
    assert report["processed_successfully"] == 1


def test_rev_run_without_any_inbox_or_fixture_prints_spike_note(tmp_path: Path) -> None:
    result = runner.invoke(
        rev_app,
        [
            "run",
            "--program", "demo",
            "--mailbox", "tpm@example.com",
            "--programs-root", str(tmp_path / "programs"),
        ],
    )

    assert result.exit_code == 2
    assert "--docs-inbox" in result.output

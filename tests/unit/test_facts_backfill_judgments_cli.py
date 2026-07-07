"""GAP-34: CLI wiring for ``vertex facts backfill-judgments``.

Verifies the Typer command registered on ``src.commands.facts.app``:
  * ``--dry-run`` discovers judgments and prints them without writing.
  * the non-dry-run path invokes the engine's append (``backfill_program``
    with ``apply=True``) and reports the written count.

Staging mirrors ``tests/unit/test_judgment_backfill.py``: a tiny overrides
YAML is staged under ``<programs_root>/<program>/overrides/issue_NNN.yaml``
with one real judgment plus one ``❓ Needs input`` marker (which must be
skipped).
"""
from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from src.commands.facts import app


runner = CliRunner()


def _write_overrides(
    path: Path,
    *,
    issue_number: int,
    scorecards: dict | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "issue_number": issue_number,
        "top_3_now": [],
        "scorecards": scorecards or {},
    }
    path.write_text(yaml.safe_dump(body), encoding="utf-8")


def test_backfill_judgments_dry_run_exits_zero_and_does_not_write(
    tmp_path: Path,
) -> None:
    """``--dry-run`` parses overrides and prints the discovered judgment(s)
    without invoking the fact-store append path."""
    programs_root = tmp_path / "programs"
    overrides = programs_root / "acme" / "overrides" / "issue_001.yaml"
    _write_overrides(
        overrides,
        issue_number=1,
        scorecards={
            "WS-A": {
                "Deployment Safety": {"risk": "❓ Needs input"},
                "Deployment Velocity": {"risk": "Low"},
            },
        },
    )

    called = {"count": 0}

    def _fail_if_called(*args, **kwargs):  # type: ignore[no-untyped-def]
        called["count"] += 1
        raise AssertionError("append_program_event must not run in dry-run")

    import src.core.judgment_backfill as jb

    real_append = jb.append_program_event
    jb.append_program_event = _fail_if_called  # type: ignore[assignment]
    try:
        result = runner.invoke(
            app,
            [
                "backfill-judgments",
                "--program",
                "acme",
                "--dry-run",
                "--programs-root",
                str(programs_root),
            ],
        )
    finally:
        jb.append_program_event = real_append  # type: ignore[assignment]

    assert result.exit_code == 0, result.stdout
    assert called["count"] == 0
    assert "[dry-run]" in result.stdout
    assert "1 judgment(s)" in result.stdout
    assert "Deployment Velocity" in result.stdout
    assert "Low" in result.stdout
    # The needs-input dimension must NOT appear as a backfilled judgment.
    assert "Deployment Safety" not in result.stdout


def test_backfill_judgments_non_dry_run_invokes_engine_append(
    tmp_path: Path,
) -> None:
    """Non-dry-run calls ``backfill_program`` with ``apply=True``, which in
    turn appends one ``judgment.dimension`` event per discovered judgment to
    the Program Fact Store.  We monkeypatch ``append_program_event`` to
    capture the calls and assert the CLI exits 0 and reports the count."""
    programs_root = tmp_path / "programs"
    overrides = programs_root / "acme" / "overrides" / "issue_010.yaml"
    _write_overrides(
        overrides,
        issue_number=10,
        scorecards={
            "WS-A": {
                "Deployment Safety": {"risk": "Low"},
                "Deployment Velocity": {"risk": "Medium"},
            },
        },
    )

    calls: list[dict] = []

    def _fake_append(program_id, event, *, recorded_at=None, home_root=None, db_root=None):  # type: ignore[no-untyped-def]
        calls.append(
            {
                "program_id": program_id,
                "event": event,
            }
        )
        return None

    import src.core.judgment_backfill as jb

    real_append = jb.append_program_event
    jb.append_program_event = _fake_append  # type: ignore[assignment]
    try:
        result = runner.invoke(
            app,
            [
                "backfill-judgments",
                "--program",
                "acme",
                "--programs-root",
                str(programs_root),
            ],
        )
    finally:
        jb.append_program_event = real_append  # type: ignore[assignment]

    assert result.exit_code == 0, result.stdout
    assert len(calls) == 2
    for call in calls:
        assert call["program_id"] == "acme"
        assert call["event"].fact_type == "judgment.dimension"
        assert "|10|" in call["event"].natural_key
    assert "written=2" in result.stdout
    assert "Backfill complete" in result.stdout
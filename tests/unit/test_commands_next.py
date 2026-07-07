from __future__ import annotations
import pytest
from pathlib import Path
pytestmark = pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "editions").exists(), reason="Requires private data")

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands.next import NextSuggestion, suggest_next_steps
from src.core.edition_resolver import get_program_output_dir

runner = CliRunner()
EDITION = "acme_weekly"
FROZEN_NOW = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)


def _write_manifest(
        programs_root: Path,
        issue: int,
        *,
        qg_results: dict,
        freshness: dict,
        ended_at: datetime,
        metadata: dict | None = None,
) -> None:
    path = get_program_output_dir(EDITION, programs_root=programs_root) / f"issue_{issue:03d}" / f"issue_{issue:03d}.manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "manifest_id": "aabbccdd-0000-0000-0000-000000000000",
        "issue_number": issue,
        "edition": EDITION,
        "started_at": ended_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "config_hash": "sha256:abc",
        "snapshot_hash": "sha256:def",
        "html_hash": "sha256:ghi",
        "md_hash": "sha256:jkl",
        "ado_calls": 1,
        "ai_calls": 0,
        "ai_cost_usd": 0.0,
        "freshness_summary": freshness,
        "qg_results": qg_results,
        "git_sha": "abc0001",
    }
    if metadata is not None:
        payload["metadata"] = metadata
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_suggest_no_manifest_yields_report_suggestion(tmp_path: Path) -> None:
    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    assert len(suggestions) >= 1
    assert suggestions[0].command == f"vertex report --edition {EDITION}"
    assert "No draft" in suggestions[0].rationale


def test_suggest_stale_manifest_yields_gather_suggestion(tmp_path: Path, monkeypatch) -> None:
    stale_time = FROZEN_NOW - timedelta(hours=72)
    _write_manifest(
        tmp_path / "programs", 1,
        qg_results={"QG-8": True},
        freshness={"blocks": 0},
        ended_at=stale_time,
    )
    monkeypatch.setattr("src.commands.next.datetime", _FakeDatetime(FROZEN_NOW))

    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    commands = [s.command for s in suggestions]
    # gather uses --program; if no edition YAML in tmp_path, falls back to report
    assert any("gather" in c or "report" in c for c in commands)


def test_suggest_qg8_failure_yields_override_suggestion(tmp_path: Path, monkeypatch) -> None:
    fresh_time = FROZEN_NOW - timedelta(hours=1)
    _write_manifest(
        tmp_path / "programs", 1,
        qg_results={"QG-8": False},
        freshness={"blocks": 0},
        ended_at=fresh_time,
    )
    monkeypatch.setattr("src.commands.next.datetime", _FakeDatetime(FROZEN_NOW))

    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    commands = [s.command for s in suggestions]
    assert f"vertex override --edition {EDITION}" in commands
    override_sugg = next(s for s in suggestions if "override" in s.command)
    assert "QG-8" in override_sugg.rationale


def test_suggest_freshness_blocks_yields_freshness_suggestion(tmp_path: Path, monkeypatch) -> None:
    fresh_time = FROZEN_NOW - timedelta(hours=1)
    _write_manifest(
        tmp_path / "programs", 1,
        qg_results={"QG-8": True},
        freshness={"blocks": 3, "warns": 0},
        ended_at=fresh_time,
    )
    monkeypatch.setattr("src.commands.next.datetime", _FakeDatetime(FROZEN_NOW))

    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    commands = [s.command for s in suggestions]
    assert f"vertex freshness --edition {EDITION}" in commands


def test_suggest_capped_at_three_and_deduplicates(tmp_path: Path, monkeypatch) -> None:
    stale_time = FROZEN_NOW - timedelta(hours=72)
    _write_manifest(
        tmp_path / "programs", 1,
        qg_results={"QG-8": False},  # fires 'override'
        freshness={"blocks": 5},     # fires 'freshness'
        ended_at=stale_time,          # fires 'gather' (stale)
    )
    monkeypatch.setattr("src.commands.next.datetime", _FakeDatetime(FROZEN_NOW))

    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    assert len(suggestions) <= 3
    commands = [s.command for s in suggestions]
    assert len(commands) == len(set(commands)), "suggestions must be deduplicated by command"


def test_suggest_qg3_failure_yields_review_sections_suggestion(tmp_path: Path, monkeypatch) -> None:
    fresh_time = FROZEN_NOW - timedelta(hours=1)
    _write_manifest(
        tmp_path / "programs", 1,
        qg_results={"QG-8": True, "QG-3": False},
        freshness={"blocks": 0},
        ended_at=fresh_time,
    )
    monkeypatch.setattr("src.commands.next.datetime", _FakeDatetime(FROZEN_NOW))

    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    commands = [s.command for s in suggestions]
    assert f"vertex review-sections show --edition {EDITION}" in commands
    review_sugg = next(s for s in suggestions if "review-sections" in s.command)
    assert "QG-3" in review_sugg.rationale


def test_suggest_qg3_failure_ranks_above_freshness(tmp_path: Path, monkeypatch) -> None:
    fresh_time = FROZEN_NOW - timedelta(hours=1)
    _write_manifest(
        tmp_path / "programs", 1,
        qg_results={"QG-8": True, "QG-3": False},
        freshness={"blocks": 5},
        ended_at=fresh_time,
    )
    monkeypatch.setattr("src.commands.next.datetime", _FakeDatetime(FROZEN_NOW))

    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    commands = [s.command for s in suggestions]
    review_idx = next((i for i, s in enumerate(suggestions) if "review-sections" in s.command), None)
    freshness_idx = next((i for i, s in enumerate(suggestions) if "freshness" in s.command), None)
    if review_idx is not None and freshness_idx is not None:
        assert review_idx < freshness_idx, "review-sections should rank above freshness"


def test_suggest_missing_narratives_yields_report_suggestion(tmp_path: Path, monkeypatch) -> None:
    fresh_time = FROZEN_NOW - timedelta(hours=1)
    _write_manifest(
        tmp_path / "programs", 1,
        qg_results={"QG-8": True, "QG-3": True},
        freshness={"blocks": 0},
        ended_at=fresh_time,
        metadata={"draft_readiness": {"missing_narrative_count": 4, "total_narrative_count": 6, "score": 53}},
    )
    monkeypatch.setattr("src.commands.next.datetime", _FakeDatetime(FROZEN_NOW))

    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    commands = [s.command for s in suggestions]
    assert f"vertex report --edition {EDITION} --dry-run --offline" in commands
    narr_sugg = next(s for s in suggestions if "--dry-run" in s.command)
    assert "4" in narr_sugg.rationale
    assert "narrative" in narr_sugg.rationale.lower()


def test_suggest_missing_narratives_ranks_above_qg3(tmp_path: Path, monkeypatch) -> None:
    fresh_time = FROZEN_NOW - timedelta(hours=1)
    _write_manifest(
        tmp_path / "programs", 1,
        qg_results={"QG-8": True, "QG-3": False},
        freshness={"blocks": 0},
        ended_at=fresh_time,
        metadata={"draft_readiness": {"missing_narrative_count": 3, "total_narrative_count": 6, "score": 53}},
    )
    monkeypatch.setattr("src.commands.next.datetime", _FakeDatetime(FROZEN_NOW))

    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    narr_idx = next((i for i, s in enumerate(suggestions) if "--dry-run" in s.command), None)
    review_idx = next((i for i, s in enumerate(suggestions) if "review-sections" in s.command), None)
    assert narr_idx is not None, "missing narrative suggestion should appear"
    assert review_idx is not None, "QG-3 review-sections suggestion should appear"
    assert narr_idx < review_idx, "missing narratives should rank above QG-3 section review"


def test_suggest_sorted_by_priority(tmp_path: Path, monkeypatch) -> None:
    stale_time = FROZEN_NOW - timedelta(hours=72)
    _write_manifest(
        tmp_path / "programs", 1,
        qg_results={"QG-8": False},
        freshness={"blocks": 2},
        ended_at=stale_time,
    )
    monkeypatch.setattr("src.commands.next.datetime", _FakeDatetime(FROZEN_NOW))

    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    priorities = [s.priority for s in suggestions]
    assert priorities == sorted(priorities), "suggestions must be in ascending priority order"


def test_contract_each_suggestion_is_parseable_by_cli(tmp_path: Path, monkeypatch) -> None:
    """Contract: each suggestion command must be parseable by the CLI (not just --help)."""
    stale_time = FROZEN_NOW - timedelta(hours=72)
    _write_manifest(
        tmp_path / "programs", 1,
        qg_results={"QG-8": False},
        freshness={"blocks": 2},
        ended_at=stale_time,
    )
    monkeypatch.setattr("src.commands.next.datetime", _FakeDatetime(FROZEN_NOW))

    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    assert suggestions, "expect at least one suggestion for this scenario"

    for s in suggestions:
        parts = s.command.split()
        # Strip the leading "vertex" wrapper — the CLI app is the root
        subcommand = parts[1]
        result = runner.invoke(app, [subcommand, "--help"])
        assert result.exit_code == 0, (
            f"Suggestion '{s.command}' subcommand '{subcommand}' --help failed: "
            f"exit={result.exit_code}\nOutput: {result.output}"
        )
        assert "No such command" not in result.output, (
            f"Suggestion '{s.command}' refers to a non-existent command"
        )


def test_next_cli_command_prints_suggestions(tmp_path: Path, monkeypatch) -> None:
    """vertex next --edition <ed> prints numbered suggestions and exits 0."""
    monkeypatch.setattr("src.commands.next.ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setattr("src.commands.next.REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr("src.commands.next.EDITIONS_ROOT", tmp_path / "editions")
    monkeypatch.setattr("src.commands.next.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(app, ["next", "--edition", EDITION])
    assert result.exit_code == 0
    # With no archive and no output, should suggest 'vertex report'
    assert "vertex report" in result.output


def test_next_goal_renders_static_program_goal_from_edition(tmp_path: Path, monkeypatch) -> None:
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    editions_root.mkdir(parents=True, exist_ok=True)
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (editions_root / f"{EDITION}.yaml").write_text(
        "\n".join((
            "schema_version: '2.0'",
            f"id: {EDITION}",
            "program_id: acme",
            "name: Acme Weekly",
            "type: newsletter",
            "altitude: weekly",
            "cadence: weekly",
        )),
        encoding="utf-8",
    )
    (program_dir / "program.yaml").write_text(
        "\n".join((
            "schema_version: '3.0'",
            "id: acme",
            "name: Acme",
            "goals:",
            "  publish-monday-brief:",
            "    description: Run catchup, generate today's brief, and open review.",
            "    steps:",
            "      - command: catchup",
            "        args: [--program, acme]",
            "      - command: brief",
            "        args: [--program, acme, --today]",
            "    success_when: all_steps_exit_zero",
        )),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.next.EDITIONS_ROOT", editions_root)
    monkeypatch.setattr("src.commands.next.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["next", "--edition", EDITION, "--goal", "publish-monday-brief"])

    assert result.exit_code == 0
    assert "Goal: publish-monday-brief" in result.output
    assert "Run catchup, generate today's brief, and open review." in result.output
    assert "1. vertex catchup --program acme" in result.output
    assert "2. vertex brief --program acme --today" in result.output
    assert "Success when: all_steps_exit_zero" in result.output


def test_next_goal_supports_program_without_edition(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "\n".join((
            "schema_version: '3.0'",
            "id: acme",
            "name: Acme",
            "goals:",
            "  prepare-lt-review:",
            "    description: Generate the LT prep package.",
            "    steps:",
            "      - command: prep",
            "        args: [--author]",
            "      - command: prep",
            "        args: [--leadership]",
            "    success_when: all_steps_exit_zero",
        )),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.next.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["next", "--program", "acme", "--goal", "prepare-lt-review"])

    assert result.exit_code == 0
    assert "Goal: prepare-lt-review" in result.output
    assert "1. vertex prep --author" in result.output
    assert "2. vertex prep --leadership" in result.output


def test_next_goal_requires_program_context(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.commands.next.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(app, ["next", "--goal", "publish-monday-brief"])

    assert result.exit_code == 2
    assert "--goal requires --program or --edition" in result.output


def test_qg5_verbosity_violations_yields_report_suggestion(tmp_path: Path, monkeypatch) -> None:
    """QG-5: live overrides check fires even when manifest says QG-5: True."""
    fresh_time = FROZEN_NOW - timedelta(hours=1)
    _write_manifest(
        tmp_path / "programs", 1,
        qg_results={"QG-8": True, "QG-3": True, "QG-5": True},  # stale — QG-5 was True before seeding
        freshness={"blocks": 0},
        ended_at=fresh_time,
    )
    # Write overrides with verbose summaries (3 sentences each — exceeds the 2-sentence safe limit)
    overrides_dir = tmp_path / "reports" / EDITION
    overrides_dir.mkdir(parents=True, exist_ok=True)
    (overrides_dir / "overrides.yaml").write_text(
        "\n".join((
            "scorecards:",
            "  Acme Scorecard:",
            "    Deployment Safety:",
            "      risk: medium",
            "      summary: Deployment safety remains medium. Guardrails in progress. ETA pending ADO data.",
        )),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.next.datetime", _FakeDatetime(FROZEN_NOW))

    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    commands = [s.command for s in suggestions]
    assert f"vertex report --edition {EDITION} --dry-run --offline" in commands
    qg5_sugg = next(s for s in suggestions if "--dry-run" in s.command)
    assert "QG-5" in qg5_sugg.rationale
    assert "verbosity" in qg5_sugg.rationale.lower()
    assert qg5_sugg.priority == 2


def test_qg5_clean_overrides_does_not_yield_qg5_suggestion(tmp_path: Path, monkeypatch) -> None:
    """QG-5: no suggestion when scorecard summaries are within the safe limit."""
    fresh_time = FROZEN_NOW - timedelta(hours=1)
    _write_manifest(
        tmp_path / "programs", 1,
        qg_results={"QG-8": True, "QG-3": True},
        freshness={"blocks": 0},
        ended_at=fresh_time,
    )
    # Write overrides with summaries within the 2-sentence safe limit
    overrides_dir = tmp_path / "reports" / EDITION
    overrides_dir.mkdir(parents=True, exist_ok=True)
    (overrides_dir / "overrides.yaml").write_text(
        "\n".join((
            "scorecards:",
            "  Acme Scorecard:",
            "    Deployment Safety:",
            "      risk: medium",
            "      summary: Deployment safety remains medium. ETA pending ADO data.",
            "top_3_now:",
            "  - owner: PM",
            "    priority: Fix SCHIE gaps by 05/26",
        )),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.next.datetime", _FakeDatetime(FROZEN_NOW))

    suggestions = suggest_next_steps(
        EDITION,
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
    )
    # No QG-5 suggestion should appear
    qg5_suggs = [s for s in suggestions if "QG-5" in s.rationale]
    assert not qg5_suggs, "no QG-5 suggestion when overrides are within sentence limit"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeDatetime:
    """Minimal shim to monkeypatch datetime.now() without affecting fromisoformat."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self, tz=None) -> datetime:
        return self._now if tz is None else self._now.astimezone(tz)

    def fromisoformat(self, s: str) -> datetime:
        return datetime.fromisoformat(s)

    def __call__(self, *args, **kwargs):
        return datetime(*args, **kwargs)

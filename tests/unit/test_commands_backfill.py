from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from zipfile import ZipFile

from src.ai.ai_mode import AIMode, set_ai_mode
from typer.testing import CliRunner
import yaml

from cli import app
from src.ai.backfill_extractor import ExtractedDimensionRisk, ExtractedNewsletterIssue, ExtractedWorkstreamBlurb, ExtractedWritingStyleSample
from src.core.ledger.candidate_store import load_pending_candidates
from src.core.ledger.discovery_candidate_builders import candidate_from_import_line
from src.m365.backfill_m365 import DiscoveredM365Source
from tests.support.report_test_setup import stage_v2_report_workspace


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def _stage_backfill_workspace(repo_root: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    return reports_root, tmp_path / "programs" / "acme", tmp_path / "publications"


def _enable_m365(program_root: Path) -> None:
    program_path = program_root / "program.yaml"
    payload = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    payload.setdefault("m365", {})["enabled"] = True
    program_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def test_backfill_cli_dry_run_discovers_offline_sources(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, _program_root, output_root = _stage_backfill_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    (tmp_path / "docs" / "newsletters" / "acme_newsletters").mkdir(parents=True)
    (tmp_path / "backfill" / "transcripts").mkdir(parents=True)
    (tmp_path / "docs" / "newsletters" / "acme_newsletters" / "Adventure-Acme Program Update _ Issue 51 _ February 10, 2025.eml").write_text("<html>Issue 51</html>", encoding="utf-8")
    (tmp_path / "backfill" / "transcripts" / "acme-weekly.vtt").write_text("WEBVTT", encoding="utf-8")

    monkeypatch.setattr("src.commands.backfill.REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.commands.backfill.REPORTS_ROOT", reports_root)

    class _FakeExtractor:
        def extract_newsletters(self, source_paths):
            return (
                ExtractedNewsletterIssue(
                    source_path=str(source_paths[0]),
                    issue_number=51,
                    issue_date="2025-02-10",
                    edition_type="detailed",
                    title="Program Hygiene | Issue 51 | 2025-02-10",
                    executive_summary="Velocity improved, but SCHIE remains the gating risk.",
                    scorecard_dimensions=(
                        ExtractedDimensionRisk(
                            scorecard_name="Acme Adventure/XIO 100% Ramp Readiness",
                            dimension_name="Deployment Velocity",
                            risk="medium",
                        ),
                    ),
                    workstream_blurbs=(
                        ExtractedWorkstreamBlurb(
                            workstream_name="Deployment Velocity",
                            summary="Risk reduced from High to Medium after rollout fixes.",
                        ),
                    ),
                    style_sample=ExtractedWritingStyleSample(
                        executive_summary_paragraphs=("Velocity improved, but SCHIE remains the gating risk.",),
                        workstream_blurbs=("Risk reduced from High to Medium after rollout fixes.",),
                        risk_framing_examples=("Risk reduced from High to Medium after rollout fixes.",),
                    ),
                    structural_notes=("Executive summary followed by workstream sections.",),
                    prompt_version="backfill_extractor.v1",
                ),
            )

    monkeypatch.setattr("src.commands.backfill._build_backfill_extractor", lambda: _FakeExtractor())

    result = runner.invoke(app, ["backfill", "--edition", EDITION_NAME, "--source", "offline", "--dry-run"])

    assert result.exit_code == 0
    assert "VERTEX BACKFILL" in result.stdout
    assert "Source mode: offline" in result.stdout
    assert "Program: acme" in result.stdout
    assert "- acme_newsletters: 1" in result.stdout
    assert "- transcripts: 1" in result.stdout
    assert "Newsletter extraction:" in result.stdout
    assert "Issue 051 · Program Hygiene | Issue 51 | 2025-02-10" in result.stdout
    assert "next_step: vertex discover candidates --program acme --source backfill_import" in result.stdout
    assert "Dry run: no backfill summary written." in result.stdout
    assert not (programs_root / "acme" / "publications" / EDITION_NAME / "backfill").exists()


def test_backfill_cli_writes_m365_summary_after_confirmation(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, program_root, output_root = _stage_backfill_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    _enable_m365(program_root)
    (program_root / "backfill_config.yaml").write_text(
        """
newsletters:
  search_strategy: "m365"
  directions:
    - question: "Find all Acme newsletter emails"
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr("src.commands.backfill.REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.commands.backfill.REPORTS_ROOT", reports_root)

    class _FakeBackfiller:
        def discover_all(self, config, *, since=None):
            return {
                "newsletters": (
                    DiscoveredM365Source(
                        category="newsletters",
                        label="Issue 051",
                        question="Find all Acme newsletter emails",
                        source_id="mail-1",
                        permalink="https://outlook.office.com/mail/1",
                        summary="Past issue",
                    ),
                ),
                "feedback": (),
                "meetings": (),
                "people_intelligence": (),
            }

    monkeypatch.setattr("src.commands.backfill._build_m365_backfiller", lambda: _FakeBackfiller())

    result = runner.invoke(app, ["backfill", "--edition", EDITION_NAME, "--source", "m365"], input="y\n")

    assert result.exit_code == 0
    assert "Reading backfill_config.yaml..." in result.stdout
    assert "Backfill summary:" in result.stdout
    assert "Backfill data:" in result.stdout
    assert (programs_root / "acme" / "publications" / EDITION_NAME / "backfill" / "backfill.summary.md").exists()
    summary_json = (programs_root / "acme" / "publications" / EDITION_NAME / "backfill" / "backfill.summary.json").read_text(encoding="utf-8")
    assert '"source_mode": "m365"' in summary_json
    assert '"total_sources": 1' in summary_json


def test_backfill_cli_requires_backfill_config_for_m365_mode(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, program_root, output_root = _stage_backfill_workspace(repo_root, tmp_path)
    _enable_m365(program_root)

    monkeypatch.setattr("src.commands.backfill.REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.commands.backfill.REPORTS_ROOT", reports_root)

    result = runner.invoke(app, ["backfill", "--edition", EDITION_NAME, "--source", "m365"])

    assert result.exit_code == 2
    assert "Missing backfill_config.yaml" in result.stdout


def test_backfill_cli_defaults_since_from_program_backfill_max_days(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, program_root, output_root = _stage_backfill_workspace(repo_root, tmp_path)
    _enable_m365(program_root)
    program_path = program_root / "program.yaml"
    program_payload = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    program_payload.setdefault("gather", {})["backfill_max_days"] = 21
    program_path.write_text(yaml.safe_dump(program_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (program_root / "backfill_config.yaml").write_text(
        """
newsletters:
  search_strategy: "m365"
  directions:
    - question: "Find all Acme newsletter emails"
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr("src.commands.backfill.REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.commands.backfill.REPORTS_ROOT", reports_root)

    captured: dict[str, object] = {}

    class _FakeDate(date):
        @classmethod
        def today(cls) -> "_FakeDate":
            return cls(2026, 5, 23)

    class _FakeBackfiller:
        def discover_all(self, config, *, since=None):
            captured["since"] = since
            return {
                "newsletters": (
                    DiscoveredM365Source(
                        category="newsletters",
                        label="Issue 051",
                        question="Find all Acme newsletter emails",
                        source_id="mail-1",
                        permalink="https://outlook.office.com/mail/1",
                        summary="Past issue",
                    ),
                ),
                "feedback": (),
                "meetings": (),
                "people_intelligence": (),
            }

    monkeypatch.setattr("src.commands.backfill.date", _FakeDate)
    monkeypatch.setattr("src.commands.backfill._build_m365_backfiller", lambda: _FakeBackfiller())

    result = runner.invoke(app, ["backfill", "--edition", EDITION_NAME, "--source", "m365", "--dry-run"])

    assert result.exit_code == 0
    assert "Since: 2026-05-02" in result.stdout
    assert captured["since"] == date(2026, 5, 2)


def test_backfill_cli_writes_offline_extracted_issue_summary_after_confirmation(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, _program_root, output_root = _stage_backfill_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    (tmp_path / "docs" / "newsletters" / "acme_newsletters").mkdir(parents=True)
    (tmp_path / "docs" / "newsletters" / "acme_newsletters" / "Adventure-Acme Program Update _ Issue 51 _ February 10, 2025.eml").write_text("<html><body>Issue 51</body></html>", encoding="utf-8")

    monkeypatch.setattr("src.commands.backfill.REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.commands.backfill.REPORTS_ROOT", reports_root)

    class _FakeExtractor:
        def extract_newsletters(self, source_paths):
            return (
                ExtractedNewsletterIssue(
                    source_path=str(source_paths[0]),
                    issue_number=51,
                    issue_date="2025-02-10",
                    edition_type="detailed",
                    title="Program Hygiene | Issue 51 | 2025-02-10",
                    executive_summary="Velocity improved, but SCHIE remains the gating risk.",
                    scorecard_dimensions=(
                        ExtractedDimensionRisk(
                            scorecard_name="Acme Adventure/XIO 100% Ramp Readiness",
                            dimension_name="Deployment Velocity",
                            risk="medium",
                        ),
                    ),
                    workstream_blurbs=(
                        ExtractedWorkstreamBlurb(
                            workstream_name="Deployment Velocity",
                            summary="Risk reduced from High to Medium after rollout fixes.",
                        ),
                    ),
                    style_sample=ExtractedWritingStyleSample(
                        executive_summary_paragraphs=("Velocity improved, but SCHIE remains the gating risk.",),
                        workstream_blurbs=("Risk reduced from High to Medium after rollout fixes.",),
                        risk_framing_examples=("Risk reduced from High to Medium after rollout fixes.",),
                    ),
                    structural_notes=("Executive summary followed by workstream sections.",),
                    prompt_version="backfill_extractor.v1",
                ),
            )

    monkeypatch.setattr("src.commands.backfill._build_backfill_extractor", lambda: _FakeExtractor())

    result = runner.invoke(app, ["backfill", "--edition", EDITION_NAME, "--source", "offline"], input="y\n")

    assert result.exit_code == 0
    assert "Backfill summary:" in result.stdout
    assert "Backfill data:" in result.stdout

    summary_md_path = programs_root / "acme" / "publications" / EDITION_NAME / "backfill" / "backfill.summary.md"
    summary_json_path = programs_root / "acme" / "publications" / EDITION_NAME / "backfill" / "backfill.summary.json"
    import_jsonl_path = programs_root / "acme" / "publications" / EDITION_NAME / "backfill" / "newsletter.discovery_import.jsonl"
    assert summary_md_path.exists()
    assert summary_json_path.exists()
    assert import_jsonl_path.exists()

    summary_markdown = summary_md_path.read_text(encoding="utf-8")
    summary_payload = json.loads(summary_json_path.read_text(encoding="utf-8"))
    import_rows = [line for line in import_jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert "## Newsletter extraction" in summary_markdown
    assert "### Issue 051" in summary_markdown
    assert "Velocity improved, but SCHIE remains the gating risk." in summary_markdown
    assert "Candidate export rows: `2`" in summary_markdown
    assert "Next governed import step:" in summary_markdown
    assert summary_payload["newsletter_extraction"]["processed_files"] == 1
    assert summary_payload["program_id"] == "acme"
    assert summary_payload["newsletter_extraction"]["scorecard_dimension_count"] == 1
    assert summary_payload["newsletter_extraction"]["workstream_blurb_count"] == 1
    assert summary_payload["newsletter_extraction"]["extracted_issues"][0]["issue_number"] == 51
    assert summary_payload["newsletter_extraction"]["extracted_issues"][0]["title"] == "Program Hygiene | Issue 51 | 2025-02-10"
    assert summary_payload["newsletter_candidate_export"]["candidate_count"] == 2
    assert summary_payload["newsletter_candidate_export"]["event_counts"] == {
        "artifact.published.v1": 1,
        "risk.raised.v1": 1,
    }
    assert len(import_rows) == 2
    parsed_candidates = [
        candidate_from_import_line(line, program="acme", batch_id="batch-1", pipeline="newsletter_backfill")
        for line in import_rows
    ]
    assert [candidate.proposed_event_type for candidate in parsed_candidates] == [
        "artifact.published.v1",
        "risk.raised.v1",
    ]
    assert parsed_candidates[1].proposed_payload["severity"] == "medium"


def test_backfill_export_can_be_staged_via_discover_backfill_import(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, _program_root, output_root = _stage_backfill_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    (tmp_path / "docs" / "newsletters" / "acme_newsletters").mkdir(parents=True)
    (tmp_path / "docs" / "newsletters" / "acme_newsletters" / "Adventure-Acme Program Update _ Issue 51 _ February 10, 2025.eml").write_text("<html><body>Issue 51</body></html>", encoding="utf-8")

    monkeypatch.setattr("src.commands.backfill.REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.commands.backfill.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)

    class _FakeExtractor:
        def extract_newsletters(self, source_paths):
            return (
                ExtractedNewsletterIssue(
                    source_path=str(source_paths[0]),
                    issue_number=51,
                    issue_date="2025-02-10",
                    edition_type="detailed",
                    title="Program Hygiene | Issue 51 | 2025-02-10",
                    executive_summary="Velocity improved, but SCHIE remains the gating risk.",
                    scorecard_dimensions=(
                        ExtractedDimensionRisk(
                            scorecard_name="Acme Adventure/XIO 100% Ramp Readiness",
                            dimension_name="Deployment Velocity",
                            risk="medium",
                        ),
                    ),
                    workstream_blurbs=(
                        ExtractedWorkstreamBlurb(
                            workstream_name="Deployment Velocity",
                            summary="Risk reduced from High to Medium after rollout fixes.",
                        ),
                    ),
                    style_sample=ExtractedWritingStyleSample(
                        executive_summary_paragraphs=("Velocity improved, but SCHIE remains the gating risk.",),
                        workstream_blurbs=("Risk reduced from High to Medium after rollout fixes.",),
                        risk_framing_examples=("Risk reduced from High to Medium after rollout fixes.",),
                    ),
                    structural_notes=("Executive summary followed by workstream sections.",),
                    prompt_version="backfill_extractor.v1",
                ),
            )

    monkeypatch.setattr("src.commands.backfill._build_backfill_extractor", lambda: _FakeExtractor())

    backfill_result = runner.invoke(app, ["backfill", "--edition", EDITION_NAME, "--source", "offline"], input="y\n")
    assert backfill_result.exit_code == 0

    import_jsonl_path = programs_root / "acme" / "publications" / EDITION_NAME / "backfill" / "newsletter.discovery_import.jsonl"
    discover_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "backfill_import",
            "--input-jsonl",
            str(import_jsonl_path),
            "--programs-root",
            str(tmp_path / "programs"),
            "--format",
            "json",
        ],
    )

    assert discover_result.exit_code == 0
    payload = json.loads(discover_result.stdout)
    assert payload["pipeline"] == "backfill_import"
    assert payload["candidate_count"] == 2


def test_backfill_cli_discovers_lt_deck_artifact_export(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, _program_root, output_root = _stage_backfill_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    (tmp_path / "docs" / "Monthly_LT_Review" / "2026").mkdir(parents=True)
    _write_minimal_pptx(
        tmp_path / "docs" / "Monthly_LT_Review" / "2026" / "2026-03-31- Acme LT Update.pptx",
        [["Acme LT Update", "General status with no structured markers"]],
    )

    monkeypatch.setattr("src.commands.backfill.REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.commands.backfill.REPORTS_ROOT", reports_root)

    result = runner.invoke(app, ["backfill", "--edition", EDITION_NAME, "--source", "offline", "--dry-run"])

    assert result.exit_code == 0
    assert "- lt_decks: 1" in result.stdout
    assert "LT deck candidate export:" in result.stdout
    assert "next_step: vertex discover candidates --program acme --source backfill_import" in result.stdout


def test_lt_deck_backfill_export_can_be_staged_via_discover_backfill_import(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, _program_root, output_root = _stage_backfill_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    (tmp_path / "docs" / "Monthly_LT_Review" / "2026").mkdir(parents=True)
    _write_minimal_pptx(
        tmp_path / "docs" / "Monthly_LT_Review" / "2026" / "2026-03-31- Acme LT Update.pptx",
        [["Acme LT Update", "General status with no structured markers"]],
    )

    monkeypatch.setattr("src.commands.backfill.REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.commands.backfill.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)

    result = runner.invoke(app, ["backfill", "--edition", EDITION_NAME, "--source", "offline"], input="y\n")
    assert result.exit_code == 0

    import_jsonl_path = programs_root / "acme" / "publications" / EDITION_NAME / "backfill" / "lt_deck.discovery_import.jsonl"
    assert import_jsonl_path.exists()
    rows = [line for line in import_jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    parsed_artifact_candidate = candidate_from_import_line(
        rows[0],
        program="acme",
        batch_id="batch-1",
        pipeline="lt_deck_backfill",
    )
    assert parsed_artifact_candidate.entity_resolution[0].resolved_entity_id == parsed_artifact_candidate.proposed_payload["artifact_id"]

    discover_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "backfill_import",
            "--input-jsonl",
            str(import_jsonl_path),
            "--programs-root",
            str(tmp_path / "programs"),
            "--format",
            "json",
        ],
    )

    assert discover_result.exit_code == 0
    payload = json.loads(discover_result.stdout)
    assert payload["candidate_count"] == 1
    pending = load_pending_candidates("acme", programs_root=tmp_path / "programs")
    assert any(
        candidate.proposed_event_type == "artifact.published.v1"
        and candidate.proposed_payload.get("location") == "docs/Monthly_LT_Review/2026/2026-03-31- Acme LT Update.pptx"
        and candidate.proposed_payload.get("artifact_kind") == "lt_deck"
        for candidate in pending
    )


def test_lt_deck_backfill_export_includes_structured_marker_candidates(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, _program_root, output_root = _stage_backfill_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    deck_path = tmp_path / "docs" / "Monthly_LT_Review" / "2026" / "2026-03-31- Acme LT Update.pptx"
    deck_path.parent.mkdir(parents=True)
    _write_minimal_pptx(
        deck_path,
        [
            [
                "Acme LT Update",
                "Decision: LT approved ramp hold | decision_id=decision:lt-ramp-hold | title=Ramp hold | decided_by=person:alice | forum=LT",
                "Risk: SCHIE remains unstable | risk_id=risk:schie | title=SCHIE risk | severity=high",
            ],
        ],
    )

    monkeypatch.setattr("src.commands.backfill.REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.commands.backfill.REPORTS_ROOT", reports_root)

    result = runner.invoke(app, ["backfill", "--edition", EDITION_NAME, "--source", "offline"], input="y\n")
    assert result.exit_code == 0

    import_jsonl_path = programs_root / "acme" / "publications" / EDITION_NAME / "backfill" / "lt_deck.discovery_import.jsonl"
    rows = [json.loads(line) for line in import_jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert [row["proposed_event_type"] for row in rows] == [
        "artifact.published.v1",
        "decision.made.v1",
        "risk.raised.v1",
    ]
    assert rows[1]["source_ref"]["slide_number"] == 1
    assert rows[1]["proposed_payload"]["decision_id"] == "decision:lt-ramp-hold"
    assert rows[2]["proposed_payload"]["severity"] == "high"


def test_lt_deck_structured_export_can_be_staged_via_discover_backfill_import(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, _program_root, output_root = _stage_backfill_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    deck_path = tmp_path / "docs" / "Monthly_LT_Review" / "2026" / "2026-03-31- Acme LT Update.pptx"
    deck_path.parent.mkdir(parents=True)
    _write_minimal_pptx(
        deck_path,
        [
            [
                "Acme LT Update",
                "Decision: LT approved ramp hold | decision_id=decision:lt-ramp-hold | title=Ramp hold | decided_by=person:alice | forum=LT",
                "Risk: SCHIE remains unstable | risk_id=risk:schie | title=SCHIE risk | severity=high",
            ],
        ],
    )

    monkeypatch.setattr("src.commands.backfill.REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.commands.backfill.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)

    result = runner.invoke(app, ["backfill", "--edition", EDITION_NAME, "--source", "offline"], input="y\n")
    assert result.exit_code == 0

    import_jsonl_path = programs_root / "acme" / "publications" / EDITION_NAME / "backfill" / "lt_deck.discovery_import.jsonl"
    discover_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "backfill_import",
            "--input-jsonl",
            str(import_jsonl_path),
            "--programs-root",
            str(tmp_path / "programs"),
            "--format",
            "json",
        ],
    )

    assert discover_result.exit_code == 0
    payload = json.loads(discover_result.stdout)
    assert payload["candidate_count"] == 3
    pending = load_pending_candidates("acme", programs_root=tmp_path / "programs")
    assert any(candidate.proposed_event_type == "decision.made.v1" for candidate in pending)
    assert any(candidate.proposed_event_type == "risk.raised.v1" for candidate in pending)


def test_backfill_cli_warns_and_continues_on_corrupt_lt_deck(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, _program_root, output_root = _stage_backfill_workspace(repo_root, tmp_path)
    deck_dir = tmp_path / "docs" / "Monthly_LT_Review" / "2026"
    deck_dir.mkdir(parents=True)
    (deck_dir / "2026-03-31- Broken LT Update.pptx").write_text("broken", encoding="utf-8")
    _write_minimal_pptx(
        deck_dir / "2026-04-30- Valid LT Update.pptx",
        [["Valid LT Update", "Decision: Hold course | decision_id=decision:hold-course | title=Hold course | decided_by=person:alice | forum=LT"]],
    )

    monkeypatch.setattr("src.commands.backfill.REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.commands.backfill.REPORTS_ROOT", reports_root)

    result = runner.invoke(app, ["backfill", "--edition", EDITION_NAME, "--source", "offline", "--dry-run"])

    assert result.exit_code == 0
    assert "candidate_export_rows: 3" in result.stdout
    assert "Skipped structured LT deck extraction for 2026-03-31- Broken LT Update.pptx" in result.stdout


def test_backfill_cli_recovers_valid_lt_deck_markers_despite_bad_lines(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, _program_root, output_root = _stage_backfill_workspace(repo_root, tmp_path)
    deck_dir = tmp_path / "docs" / "Monthly_LT_Review" / "2026"
    deck_dir.mkdir(parents=True)
    _write_minimal_pptx(
        deck_dir / "2026-03-31- Mixed LT Update.pptx",
        [[
            "Mixed LT Update",
            "Risk:",
            "Decision: Hold course | decision_id=decision:hold-course | title=Hold course | decided_by=person:alice | forum=LT",
        ]],
    )

    monkeypatch.setattr("src.commands.backfill.REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.commands.backfill.REPORTS_ROOT", reports_root)

    result = runner.invoke(app, ["backfill", "--edition", EDITION_NAME, "--source", "offline", "--dry-run"])

    assert result.exit_code == 0
    assert "candidate_export_rows: 2" in result.stdout
    assert "Mixed LT Update.pptx slide 1 (Mixed LT Update): Risk: marker is empty." in result.stdout


def test_build_backfill_extractor_passes_trace_context_to_runtime_builder(monkeypatch) -> None:
    seen_trace_contexts: list[object] = []

    def _fake_from_environment(*, trace_context=None):
        seen_trace_contexts.append(trace_context)
        class _FakeExtractor:
            def extract_newsletters(self, source_paths):
                del source_paths
                return ()

        return _FakeExtractor()

    monkeypatch.setattr("src.commands.backfill.BackfillExtractor.from_environment", _fake_from_environment)

    trace_context = __import__("src.commands.backfill", fromlist=["_build_backfill_trace_context"])._build_backfill_trace_context(
        edition_name=EDITION_NAME,
    )
    extractor = __import__("src.commands.backfill", fromlist=["_build_default_backfill_extractor"])._build_default_backfill_extractor(
        trace_context=trace_context,
    )

    assert extractor is not None
    assert seen_trace_contexts == [trace_context]
    assert trace_context.edition == EDITION_NAME
    assert trace_context.caller == "src.commands.backfill._extract_offline_newsletters"
    assert trace_context.metadata["task_type"] == "newsletter_backfill_extraction"


def test_build_default_backfill_extractor_returns_disabled_extractor_without_calling_builder(monkeypatch) -> None:
    import src.commands.backfill as backfill_mod

    monkeypatch.setattr(
        backfill_mod,
        "_build_backfill_extractor",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("_build_backfill_extractor should not be called")),
    )
    trace_context = backfill_mod._build_backfill_trace_context(edition_name=EDITION_NAME)

    set_ai_mode(AIMode.DISABLED)
    try:
        extractor = backfill_mod._build_default_backfill_extractor(trace_context=trace_context)
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert isinstance(extractor, backfill_mod.BackfillExtractor)


def test_extract_offline_newsletters_skips_cleanly_when_ai_disabled(monkeypatch, tmp_path: Path) -> None:
    import src.commands.backfill as backfill_mod

    newsletter_path = tmp_path / "backfill" / "emails" / "issue_051.html"
    newsletter_path.parent.mkdir(parents=True)
    newsletter_path.write_text("<html>Issue 51</html>", encoding="utf-8")

    categories = [
        backfill_mod.BackfillCategorySummary(
            category="prior_emails",
            count=1,
            items=(
                backfill_mod.DiscoveredBackfillItem(
                    label="Issue 51",
                    reference="backfill/emails/issue_051.html",
                    source_id=None,
                    permalink=None,
                    origin="offline",
                ),
            ),
        )
    ]
    monkeypatch.setattr(
        backfill_mod,
        "_build_backfill_extractor",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("_build_backfill_extractor should not be called")),
    )

    set_ai_mode(AIMode.DISABLED)


def _write_minimal_pptx(path: Path, slides: list[list[str]]) -> None:
    with ZipFile(path, "w") as archive:
        for index, lines in enumerate(slides, start=1):
            xml_lines = "".join(f"<a:p><a:r><a:t>{_xml_escape(line)}</a:t></a:r></a:p>" for line in lines)
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                (
                    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                    "<p:sld xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\" "
                    "xmlns:p=\"http://schemas.openxmlformats.org/presentationml/2006/main\">"
                    f"<p:cSld><p:spTree><p:sp><p:txBody>{xml_lines}</p:txBody></p:sp></p:spTree></p:cSld>"
                    "</p:sld>"
                ),
            )


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
    try:
        extraction, warnings = backfill_mod._extract_offline_newsletters(
            edition_name=EDITION_NAME,
            categories=categories,
            repo_root=tmp_path,
            newsletter_extractor_factory=None,
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert extraction is None
    assert warnings == ("Newsletter extraction skipped: invocation AI is disabled by --no-ai / AIMode.DISABLED.",)


def test_extract_offline_newsletters_dedupes_reply_forward_variants(monkeypatch, tmp_path: Path) -> None:
    import src.commands.backfill as backfill_mod

    newsletters_dir = tmp_path / "docs" / "newsletters" / "acme_newsletters"
    newsletters_dir.mkdir(parents=True)
    primary = newsletters_dir / "Adventure-Acme Program Update _ Issue 61 _ June 12, 2025.eml"
    reply = newsletters_dir / "Re_ Adventure-Acme Program Update _ Issue 61 _ June 12, 2025.eml"
    forward = newsletters_dir / "Fw_ Adventure-Acme Program Update _ Issue 61 _ June 12, 2025.eml"
    for path in (primary, reply, forward):
        path.write_text("<html>Issue 61</html>", encoding="utf-8")

    categories = [
        backfill_mod.BackfillCategorySummary(
            category="acme_newsletters",
            count=3,
            items=tuple(
                backfill_mod.DiscoveredBackfillItem(
                    label=path.name,
                    reference=backfill_mod._relative_to_root(path, tmp_path),
                    source_id=None,
                    permalink=None,
                    origin="acme_newsletters",
                )
                for path in (primary, reply, forward)
            ),
        )
    ]

    paths = backfill_mod._dedupe_newsletter_source_paths((primary, reply, forward))

    assert paths == (primary,)


def test_newsletter_candidate_export_falls_back_to_artifact_rows_without_ai_extraction(tmp_path: Path) -> None:
    import src.commands.backfill as backfill_mod

    newsletters_dir = tmp_path / "docs" / "newsletters" / "acme_newsletters"
    newsletters_dir.mkdir(parents=True)
    issue_path = newsletters_dir / "Adventure-Acme Program Update _ Issue 51 _ February 10, 2025.eml"
    issue_path.write_text("<html>Issue 51</html>", encoding="utf-8")

    categories = [
        backfill_mod.BackfillCategorySummary(
            category="acme_newsletters",
            count=1,
            items=(
                backfill_mod.DiscoveredBackfillItem(
                    label=issue_path.name,
                    reference=backfill_mod._relative_to_root(issue_path, tmp_path),
                    source_id=None,
                    permalink=None,
                    origin="acme_newsletters",
                ),
            ),
        )
    ]

    rows, warnings = backfill_mod._build_newsletter_candidate_export_rows(
        categories=categories,
        extracted_issues=(),
        repo_root=tmp_path,
        newsletter_source_categories=frozenset(c.category for c in categories),
    )

    assert warnings == []
    assert len(rows) == 1
    assert rows[0]["proposed_event_type"] == "artifact.published.v1"
    assert rows[0]["proposed_payload"]["artifact_id"] == "published_artifact:issue-051"
    assert rows[0]["source_ref"]["issue_number"] == 51

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner
import yaml

from cli import app
from src.ai.ai_mode import AIMode, set_ai_mode
from src.ai.onboard_assistant import StyleSuggestions, StructureSuggestions, SuggestedDimension, SuggestedScorecard
from src.commands import onboard as onboard_module
from src.commands.onboard import OnboardResult, OnboardValidationResult, _build_default_onboard_assistant, _build_onboard_trace_context
from src.commands.onboard import _draft_from_existing_edition, _prompt_supported_archetype, _write_yaml
from src.commands.onboard import _run_onboard_validation, run_onboard_create, run_onboard_migrate_v3, run_onboard_update
from src.core.assumption_tracker import load_assumptions
from src.core.config_loader import load_bundle_with_mode
from src.core.exceptions import AuthError
from src.core.models_v2 import Workstream


runner = CliRunner()


class _FakeOnboardAssistant:
    def suggest_area_paths(self, *, program_name: str, organization: str, project: str, api_timeout_seconds: int) -> tuple[str, ...]:
        assert program_name == "Storage Demo"
        assert organization == "your-org"
        assert project == "One"
        assert api_timeout_seconds == 30
        return (r"One\Storage\AI",)

    def suggest_scorecards(
        self,
        *,
        program_name: str,
        objective: str,
        edition_type: str,
        organization: str,
        project: str,
        area_paths: tuple[str, ...],
        work_item_types: tuple[str, ...],
        excluded_states: tuple[str, ...],
        date_window_days: int,
        api_timeout_seconds: int,
    ) -> StructureSuggestions:
        assert program_name == "Storage Demo"
        assert objective == "Provide a concise weekly readiness view."
        assert edition_type == "detailed"
        assert organization == "your-org"
        assert project == "One"
        assert area_paths == (r"One\Storage\AI",)
        assert work_item_types == ("Feature", "Risk", "Scenario", "Key Result")
        assert excluded_states == ("Removed", "Cut")
        assert date_window_days == 14
        assert api_timeout_seconds == 30
        return StructureSuggestions(
            scorecards=(
                SuggestedScorecard(
                    name="Delivery Scorecard",
                    dimensions=(
                        SuggestedDimension(
                            name="Deployment Velocity",
                            description="Release health.",
                            ado_filter="area_path contains 'AI' AND type eq 'Feature'",
                        ),
                        SuggestedDimension(
                            name="Reliability",
                            description="Operational safety.",
                            ado_filter="tag contains 'Safety'",
                        ),
                    ),
                ),
            ),
            prompt_version="onboard_structure_assistant.v1",
        )

    def analyze_style_sample(self, sample_paragraph: str) -> StyleSuggestions:
        assert "SCHIE" in sample_paragraph
        return StyleSuggestions(
            voice="Confident but honest.",
            structure="Wins first, then risks.",
            risk_framing_improving="Quantify the before and after.",
            risk_framing_stuck="State blocker, action, and ETA.",
            risk_framing_escalation="Name the ask, owner, and deadline.",
            risk_framing_new_risk="Introduce context before severity.",
            preferred_patterns=(
                "Metric moved from {before} -> {after}.",
                "Blocked on {team}; mitigation: {action} by {date}",
            ),
            prompt_version="onboard_style_assistant.v1",
        )


def _stub_onboard_validation(
    monkeypatch,
    tmp_path: Path,
    issue_number: int = 1,
    exit_code: int = 0,
) -> None:
    def _fake_validation(edition_name: str, reports_root: Path) -> OnboardValidationResult:
        del reports_root
        output_dir = tmp_path / "output" / edition_name
        return OnboardValidationResult(
            issue_number=issue_number,
            exit_code=exit_code,
            html_path=output_dir / f"issue_{issue_number:03d}.html",
            md_path=output_dir / f"issue_{issue_number:03d}.md",
            manifest_path=output_dir / f"issue_{issue_number:03d}.manifest.json",
        )

    monkeypatch.setattr("src.commands.onboard._run_onboard_validation", _fake_validation)


def _prepare_v2_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    reports_root = tmp_path / "reports"
    # The bootstrap wizard collects program_id "storage_demo" (the 2nd CLI input), so
    # onboard writes editions under programs/storage_demo/editions/.
    editions_root = tmp_path / "programs" / "storage_demo" / "editions"
    programs_root = tmp_path / "programs"
    reports_root.mkdir(parents=True, exist_ok=True)
    return reports_root, editions_root, programs_root


def _load_v2_bundle(tmp_path: Path, edition_name: str):
    reports_root = tmp_path / "reports"
    editions_root = tmp_path / "programs" / "storage_demo" / "editions"
    programs_root = tmp_path / "programs"
    return load_bundle_with_mode(
        edition_name,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _expected_charter_scaffold() -> dict:
    return {
        "scope_statement": None,
        "success_criteria": [],
        "assumptions": [],
        "constraints": [],
        "stakeholder_register": [],
    }


def _expected_raci_scaffold() -> dict:
    return {
        "responsible": [],
        "accountable": None,
        "consulted": [],
        "informed": [],
    }


def _rich_cli_inputs() -> list[str]:
    return [
        "Storage Demo",
        "storage_demo",
        "Provide a concise weekly readiness view.",
        "Helps leadership spot delivery gaps early.",
        "Ramp readiness gating",
        "1",
        "SCHIE commitments",
        "Acme Ramp",
        "Ramp cannot proceed until gaps close.",
        "Storage Demo Weekly",
        "weekly",
        "Alice Writer",
        "alice@example.com",
        "monday",
        "09:00",
        "America/Los_Angeles",
        "c",
        "",
        "",
        r"One\Storage\Demo",
        "",
        "",
        "",
        "",
        "c",
        "A",
        "1",
        "Delivery Scorecard",
        "2",
        "Delivery",
        "Release health",
        "area_path contains 'Demo' AND type eq 'Feature'",
        "Reliability",
        "Operational safety",
        "tag contains 'Safety'",
        "c",
        "1",
        "Platform",
        "platform, core",
        r"One\Storage\Demo",
        "owner@example.com",
        "backup@example.com",
        "Platform execution lane.",
        "Core execution lane for the program.",
        "Carried risk since the prior issue.",
        "high",
        "Awaiting SCHIE dependency closure.",
        "1",
        "Lead PM",
        "exec_summary, scorecard",
        "1",
        "Jordan Lee",
        "Director",
        "ramp timeline, commitment risk",
        "Decision-oriented.",
        "verbosity",
        "1",
        "Isaiah Gregory",
        "Platform, OS",
        "Needs editing for exec audience",
        "America/Los_Angeles",
        "Sebastian Rios",
        "c",
        "1",
        "SLA",
        "Service Level Agreement",
        "avoid blame, no surprises",
        "Confident but honest.",
        "Wins first, then risks.",
        "Quantify the before and after.",
        "State blocker, action, and ETA.",
        "Name the ask, owner, and deadline.",
        "Introduce context before severity.",
        "1",
        "Blocked on {team}; mitigation: {action} by {date}",
        "concern",
        "2",
        "SCHIE Gaps",
        "",
        "Deployment Velocity",
        "strong",
        "c",
        "y",
        "",
    ]


def _base_cli_inputs(*, archetype_choice: str) -> list[str]:
    return [
        "Storage Demo",
        "storage_demo",
        "Provide a concise weekly readiness view.",
        "Helps leadership spot delivery gaps early.",
        "",
        "0",
        "Storage Demo Weekly",
        "weekly",
        "Alice Writer",
        "alice@example.com",
        "monday",
        "09:00",
        "America/Los_Angeles",
        "c",
        "",
        "",
        r"One\Storage\Demo",
        "",
        "",
        "",
        "",
        "c",
        archetype_choice,
        "1",
        "Delivery Scorecard",
        "2",
        "Delivery",
        "Release health",
        "area_path contains 'Demo' AND type eq 'Feature'",
        "Reliability",
        "Operational safety",
        "tag contains 'Safety'",
        "c",
        "1",
        "Platform",
        "platform, core",
        r"One\Storage\Demo",
        "owner@example.com",
        "backup@example.com",
        "Platform execution lane.",
        "",
        "",
        "",
        "",
        "1",
        "Lead PM",
        "exec_summary, scorecard",
        "0",
        "0",
        "c",
        "1",
        "SLA",
        "Service Level Agreement",
        "avoid blame, no surprises",
        "",
        "",
        "",
        "",
        "",
        "",
        "0",
        "",
        "0",
        "c",
        "y",
        "",
    ]


def _seed_existing_v2_edition(
    tmp_path: Path,
    *,
    edition_name: str,
    program_id: str,
    edition_type: str,
) -> tuple[Path, Path, Path]:
    reports_root, _editions_root, programs_root = _prepare_v2_roots(tmp_path)
    program_dir = programs_root / program_id
    knowledge_dir = program_dir / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    # Editions live under their program's editions/ directory (programs/<id>/editions/),
    # matching the edition's program_id field so the resolver and onboard can find them.
    editions_root = program_dir / "editions"
    (editions_root / f"{edition_name}.yaml").parent.mkdir(parents=True, exist_ok=True)
    (editions_root / f"{edition_name}.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": edition_name,
                "program_id": program_id,
                "name": "Existing Weekly | Issue {issue_number} | {date}",
                "type": edition_type,
                "altitude": "escalation" if edition_type == "narrative" else "helicopter",
                "cadence": "weekly",
                "send_day": "monday",
                "send_time_local": "08:30",
                "timezone": "America/Los_Angeles",
                "brand_name": "Existing Brand",
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": program_id,
                "name": "Existing Program",
                "objective": "Old objective.",
                "mission": "Existing mission.",
                "current_phase": "Ramp stabilization",
                "pillars": ["Existing Scorecard"],
                "glossary": {"SKU": "Stock Keeping Unit"},
                "people": [
                    {
                        "email": "author@example.com",
                        "display_name": "Existing Author",
                        "role": "PM",
                        "workstreams": ["Platform"],
                    },
                    {
                        "email": "owner@example.com",
                        "display_name": "Owner Name",
                        "role": "Lead",
                        "workstreams": ["Platform"],
                    },
                ],
                "leadership_readers": [
                    {
                        "name": "Executive Reader",
                        "role": "PM Lead",
                        "cares_about": ["accuracy"],
                        "prefers": "Lead with wins + deltas.",
                        "pet_peeves": ["verbosity"],
                    }
                ],
                "recurring_themes": ["Existing Scorecard"],
                "writing_style": {
                    "voice": "Confident but honest.",
                    "structure": "Wins first, then risks.",
                    "risk_framing": {"stuck": "Preserve this stuck pattern."},
                    "preferred_patterns": ["Preserve this pattern"],
                },
                "tone_calibration": {
                    "overall": "concern",
                    "per_theme_override": {"Existing Scorecard": "strong"},
                },
                "key_dependencies": [
                    {
                        "from_item": "SCHIE commitments",
                        "to_item": "Ramp",
                        "impact": "Ramp waits for SCHIE closure",
                    }
                ],
                "author_defaults": {
                    "display_name": "Existing Author",
                    "email": "author@example.com",
                },
                "distribution_defaults": {
                    "to": ["team@example.com"],
                    "cc": ["lead@example.com"],
                    "channels": ["email"],
                },
                "ado": {
                    "organization": "your-org",
                    "project": "One",
                    "area_paths": [r"One\Storage\Existing"],
                    "work_item_types": ["Feature", "Risk"],
                    "excluded_states": ["Removed"],
                    "date_window_days": 21,
                    "api_timeout_seconds": 45,
                },
                "ai": {
                    "enabled": True,
                    "budget_usd_per_run": 0.75,
                    "blurb_deployment": "existing-blurb",
                    "exec_summary_deployment": "existing-exec",
                    "temperature": 0.4,
                },
                "kusto": {
                    "enabled": True,
                },
                "m365": {
                    "enabled": True,
                    "prefer_agency": False,
                },
                "logging": {
                    "level": "DEBUG",
                    "json": True,
                },
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    (program_dir / "workstreams.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [
                    {
                        "id": "platform",
                        "name": "Platform",
                        "aliases": ["platform"],
                        "area_paths": [r"One\Storage\Existing"],
                        "dri_email": "owner@example.com",
                        "alternate_owner": "backup@example.com",
                        "description": "Existing workstream.",
                        "why_it_matters": "Existing why-it-matters.",
                        "history_summary": "Existing history summary.",
                        "leadership_sensitivity": "critical",
                        "current_blocker": "Awaiting platform sign-off.",
                        "extra_workstream_field": "keep-workstream",
                    }
                ],
                "workstream_owners": [
                    {
                        "name": "Isaiah Gregory",
                        "areas": ["Platform"],
                        "style_note": "Preserve this style note",
                        "timezone": "America/Los_Angeles",
                        "alternate": "Sebastian Rios",
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    (program_dir / "scorecards.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "scorecards": [
                    {
                        "name": "Existing Scorecard",
                        "extra_field": "preserve-me",
                        "dimensions": [
                            {
                                "name": "Existing Dimension",
                                "description": "Existing description",
                                "ado_filter": "area_path contains 'Existing'",
                                "workstream_id": "platform",
                                "extra_dimension_field": "keep-me",
                            }
                        ],
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    (program_dir / "editorial_rules.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "stale_warn_days": 21,
                "stale_block_days": 42,
                "banned_phrases": [
                    "due to",
                    "caused by",
                    "led to",
                    "resulted in",
                    "because of",
                    "delve",
                    "tapestry",
                    "furthermore",
                    "crucial",
                    "testament",
                    "in conclusion",
                    "leverage",
                    "existing extra",
                ],
                "banned_openings": ["Avoid this opening"],
                "verbosity": {
                    "workstream_blurb_max_sentences": 4,
                    "workstream_blurb_max_words": 75,
                    "exec_bullet_max_words": 30,
                    "exec_max_bullets": 4,
                    "scorecard_summary_max_sentences": 4,
                },
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    (program_dir / "review.yaml").write_text(
        yaml.safe_dump(
            {
                "reviewers": [{"name": "Lead PM", "sections": ["exec_summary"]}],
                "required": True,
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    (knowledge_dir / "people_directory.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "sensitivity": "internal",
                "people": [
                    {
                        "alias": "author",
                        "email": "author@example.com",
                        "display_name": "Existing Author",
                        "team_ids": ["platform"],
                    },
                    {
                        "alias": "owner",
                        "email": "owner@example.com",
                        "display_name": "Owner Name",
                        "title": "Lead",
                        "team_ids": ["platform"],
                    },
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    (knowledge_dir / "teams.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "teams": [
                    {
                        "id": "platform",
                        "name": "Platform",
                        "area_paths": [r"One\Storage\Existing"],
                        "programs": [program_id],
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    (knowledge_dir / "products.yaml").write_text(
        yaml.safe_dump({"schema_version": "1.0", "products": []}, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    (knowledge_dir / "golden_queries.yaml").write_text(
        yaml.safe_dump({"schema_version": "1.0", "queries": []}, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return reports_root, editions_root, programs_root


def test_onboard_cli_bootstraps_new_edition(monkeypatch, tmp_path: Path) -> None:
    reports_root, editions_root, programs_root = _prepare_v2_roots(tmp_path)
    monkeypatch.setattr("src.commands.onboard.REPORTS_ROOT", reports_root)
    _stub_onboard_validation(monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        ["onboard", "--edition", "demo_newsletter"],
        input="\n".join(_rich_cli_inputs()),
    )

    assert result.exit_code == 0, result.stdout
    assert "Onboarding complete for demo_newsletter." in result.stdout
    assert "Validation dry-run issue 1 passed (exit code 0)." in result.stdout

    edition_path = editions_root / "demo_newsletter.yaml"
    program_dir = programs_root / "storage_demo"
    readme_path = program_dir / "README-demo_newsletter.md"
    assert edition_path.exists()
    assert (program_dir / "program.yaml").exists()
    assert (program_dir / "workstreams.yaml").exists()
    assert (program_dir / "scorecards.yaml").exists()
    assert readme_path.exists()

    load_result = load_bundle_with_mode(
        "demo_newsletter",
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    assert load_result.mode == "v2"
    bundle = load_result.bundle

    edition_doc = _read_yaml(edition_path)
    program_doc = _read_yaml(program_dir / "program.yaml")
    workstreams_doc = _read_yaml(program_dir / "workstreams.yaml")
    scorecards_doc = _read_yaml(program_dir / "scorecards.yaml")
    milestones_doc = _read_yaml(program_dir / "milestones.yaml")
    risk_doc = _read_yaml(program_dir / "risk_register.yaml")
    escalation_doc = _read_yaml(program_dir / "escalation_rules.yaml")
    decisions_doc = _read_yaml(program_dir / "decisions.yaml")
    people_directory_doc = _read_yaml(program_dir / "knowledge" / "people_directory.yaml")
    readme_text = readme_path.read_text(encoding="utf-8")

    assert edition_doc["program_id"] == "storage_demo"
    assert edition_doc["type"] == "detailed"
    assert edition_doc["altitude"] == "helicopter"
    assert program_doc["charter"] == _expected_charter_scaffold()
    assert program_doc["communication_plan"] == [
        {
            "edition": "demo_newsletter",
            "audience": "Storage Demo",
            "channel": "email",
            "cadence": "weekly",
            "owner": "alice",
        }
    ]
    assert workstreams_doc["workstreams"][0]["id"] == "platform"
    assert workstreams_doc["workstreams"][0]["raci"] == _expected_raci_scaffold()
    assert scorecards_doc["scorecards"][0]["dimensions"][0]["workstream_id"] == "platform"
    assert milestones_doc == {"schema_version": "1.0", "milestones": []}
    assert risk_doc == {"schema_version": "1.0", "risks": []}
    assert escalation_doc["schema_version"] == "1.0"
    assert decisions_doc == {"schema_version": "1.0", "decisions": []}
    assert tuple(rule["name"] for rule in escalation_doc["rules"]) == (
        "consecutive_high",
        "milestone_at_risk",
        "unresolved_ask",
    )
    assert people_directory_doc["people"][0]["alias"] == "alice"
    assert "editions/demo_newsletter.yaml" in readme_text
    assert "vertex draft --edition demo_newsletter --dry-run" in readme_text

    assert bundle.config.edition.name == "demo_newsletter"
    assert bundle.config.edition.type == "detailed"
    assert bundle.config.edition.cadence == "weekly"
    assert bundle.config.ado.area_paths == (r"One\Storage\Demo",)
    assert bundle.config.scorecards[0].dimensions[0].ado_filter == "area_path contains 'Demo' AND type eq 'Feature'"
    assert bundle.program_context is not None
    assert bundle.program_context.program_name == "Storage Demo"
    assert bundle.program_context.current_phase == "Ramp readiness gating"
    assert bundle.program_context.key_dependency_chain[0].source == "SCHIE commitments"
    assert bundle.program_context.workstreams[0].alternate_owner == "backup@example.com"
    assert bundle.program_context.workstreams[0].why_it_matters == "Core execution lane for the program."
    assert bundle.program_context.workstreams[0].history_summary == "Carried risk since the prior issue."
    assert bundle.program_context.workstreams[0].leadership_sensitivity == "high"
    assert bundle.program_context.workstreams[0].current_blocker == "Awaiting SCHIE dependency closure."
    assert bundle.program_context.glossary["SLA"] == "Service Level Agreement"
    assert bundle.program_context.leadership_readers[0].name == "Jordan Lee"
    assert bundle.program_context.leadership_readers[0].cares_about == ("ramp timeline", "commitment risk")
    assert bundle.program_context.workstream_owners[0].name == "Isaiah Gregory"
    assert bundle.program_context.workstream_owners[0].areas == ("Platform", "OS")
    assert bundle.program_context.writing_style is not None
    assert bundle.program_context.writing_style.voice == "Confident but honest."
    assert bundle.program_context.writing_style.risk_framing["stuck"] == "State blocker, action, and ETA."
    assert bundle.program_context.writing_style.preferred_patterns == ("Blocked on {team}; mitigation: {action} by {date}",)
    assert bundle.program_context.recurring_themes == ("SCHIE Gaps", "Deployment Velocity")
    assert bundle.program_context.tone_calibration is not None
    assert bundle.program_context.tone_calibration.overall == "concern"
    assert bundle.program_context.tone_calibration.per_theme_override["Deployment Velocity"] == "strong"
    assert "avoid blame" in bundle.editorial_rules.banned_phrases
    assert bundle.review.required is True
    assert bundle.review.reviewers[0].name == "Lead PM"


def test_onboard_cli_passes_ai_flag_to_create(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _fake_run_onboard_create(
        edition_name: str,
        reports_root: Path | None = None,
        ai_enabled: bool = False,
        assistant=None,
        register_shared: bool = False,
    ) -> OnboardResult:
        del assistant, register_shared
        captured["edition_name"] = edition_name
        captured["reports_root"] = reports_root
        captured["ai_enabled"] = ai_enabled
        edition_path = tmp_path / "programs" / "acme" / "editions" / f"{edition_name}.yaml"
        program_dir = tmp_path / "programs" / "demo"
        return OnboardResult(
            edition_name=edition_name,
            program_id="demo",
            edition_path=edition_path,
            program_dir=program_dir,
            program_path=program_dir / "program.yaml",
            workstreams_path=program_dir / "workstreams.yaml",
            scorecards_path=program_dir / "scorecards.yaml",
            editorial_rules_path=program_dir / "editorial_rules.yaml",
            review_path=program_dir / "review.yaml",
        )

    monkeypatch.setattr("src.commands.onboard.run_onboard_create", _fake_run_onboard_create)

    result = runner.invoke(app, ["onboard", "--edition", "ai_newsletter", "--ai"])

    assert result.exit_code == 0
    assert captured["edition_name"] == "ai_newsletter"
    assert captured["ai_enabled"] is True


def test_build_onboard_assistant_passes_trace_context_to_runtime_builder(monkeypatch, tmp_path: Path) -> None:
    seen_trace_contexts: list[object] = []

    def _fake_from_environment(*, trace_context=None):
        seen_trace_contexts.append(trace_context)
        return _FakeOnboardAssistant()

    monkeypatch.setattr("src.commands.onboard.OnboardAssistant.from_environment", _fake_from_environment)

    trace_context = _build_onboard_trace_context(
        edition_name="ai_newsletter",
        reports_root=tmp_path / "reports",
    )
    assistant = _build_default_onboard_assistant(trace_context=trace_context)

    assert isinstance(assistant, _FakeOnboardAssistant)
    assert seen_trace_contexts == [trace_context]
    assert trace_context.edition == "ai_newsletter"
    assert trace_context.caller == "src.commands.onboard._resolve_onboard_assistant"
    assert trace_context.metadata["task_type"] == "onboarding_ai_assistance"


def test_resolve_onboard_assistant_returns_none_when_invocation_ai_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        onboard_module,
        "_build_default_onboard_assistant",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("_build_default_onboard_assistant should not be called")),
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        assistant = onboard_module._resolve_onboard_assistant(
            ai_enabled=True,
            edition_name="ai_newsletter",
            reports_root=tmp_path / "reports",
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert assistant is None


def test_onboard_cli_routes_migrate_v3_to_migration_path(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _fake_run_onboard_migrate_v3(
        edition_name: str,
        reports_root: Path | None = None,
    ) -> OnboardResult:
        captured["edition_name"] = edition_name
        captured["reports_root"] = reports_root
        edition_path = tmp_path / "programs" / "acme" / "editions" / f"{edition_name}.yaml"
        program_dir = tmp_path / "programs" / "demo"
        return OnboardResult(
            edition_name=edition_name,
            program_id="demo",
            edition_path=edition_path,
            program_dir=program_dir,
            program_path=program_dir / "program.yaml",
            workstreams_path=program_dir / "workstreams.yaml",
            scorecards_path=program_dir / "scorecards.yaml",
            editorial_rules_path=program_dir / "editorial_rules.yaml",
            review_path=program_dir / "review.yaml",
        )

    monkeypatch.setattr("src.commands.onboard.run_onboard_migrate_v3", _fake_run_onboard_migrate_v3)

    result = runner.invoke(app, ["onboard", "--update", "existing_newsletter", "--migrate-v3"])

    assert result.exit_code == 0
    assert captured["edition_name"] == "existing_newsletter"


def test_onboard_cli_routes_migrate_deps_alias_to_migration_path(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _fake_run_onboard_migrate_v3(
        edition_name: str,
        reports_root: Path | None = None,
    ) -> OnboardResult:
        captured["edition_name"] = edition_name
        captured["reports_root"] = reports_root
        edition_path = tmp_path / "programs" / "acme" / "editions" / f"{edition_name}.yaml"
        program_dir = tmp_path / "programs" / "demo"
        return OnboardResult(
            edition_name=edition_name,
            program_id="demo",
            edition_path=edition_path,
            program_dir=program_dir,
            program_path=program_dir / "program.yaml",
            workstreams_path=program_dir / "workstreams.yaml",
            scorecards_path=program_dir / "scorecards.yaml",
            editorial_rules_path=program_dir / "editorial_rules.yaml",
            review_path=program_dir / "review.yaml",
        )

    monkeypatch.setattr("src.commands.onboard.run_onboard_migrate_v3", _fake_run_onboard_migrate_v3)

    result = runner.invoke(app, ["onboard", "--update", "existing_newsletter", "--migrate-deps"])

    assert result.exit_code == 0
    assert captured["edition_name"] == "existing_newsletter"


def test_run_onboard_create_ai_prefills_ado_structure_and_style_suggestions(monkeypatch, tmp_path: Path) -> None:
    reports_root, editions_root, programs_root = _prepare_v2_roots(tmp_path)

    monkeypatch.setattr("src.commands.onboard._review_stage", lambda edition_name, draft: "continue")
    monkeypatch.setattr("src.commands.onboard.typer.confirm", lambda message, default=True: True)
    _stub_onboard_validation(monkeypatch, tmp_path, issue_number=7, exit_code=2)

    required_values = {
        "Program name": "Storage Demo",
        "One-sentence program objective (real-world outcome, not the update)": "Provide a concise weekly readiness view.",
        "Program mission (why it matters to leadership and execution)": "Helps leadership spot delivery gaps early.",
        "Primary edition title": "Storage Demo Weekly",
        "Author display name": "Alice Writer",
        "Author email": "alice@example.com",
        "Workstream name": "Platform",
        "DRI email": "owner@example.com",
        "ADO project": "One",
    }
    optional_values = {
        "Sample paragraph for AI style analysis (optional)": "Velocity improved from 62% to 81%, but SCHIE remains the gating risk for ramp readiness.",
        "Tone calibration (overall)": "concern",
    }
    csv_values = {
        "Aliases (comma separated)": ("platform",),
    }
    int_values = {
        "How many key dependencies should be recorded?": 0,
        "How many scorecards?": 1,
        "How many dimensions?": 2,
        "How many workstreams?": 1,
        "How many reviewers should be seeded?": 0,
        "How many leadership readers should be seeded?": 0,
        "How many workstream owner profiles should be seeded?": 0,
        "How many glossary entries?": 0,
        "How many preferred patterns?": 2,
        "How many recurring themes?": 0,
    }

    def _fake_prompt_required(prompt_text: str, default: str | None = None) -> str:
        if prompt_text in required_values:
            return required_values[prompt_text]
        if default is not None:
            return default
        raise AssertionError(f"Unexpected required prompt without default: {prompt_text}")

    def _fake_prompt_optional(prompt_text: str, default: str | None = None) -> str | None:
        if prompt_text in optional_values:
            return optional_values[prompt_text]
        return default

    def _fake_prompt_csv(prompt_text: str, default: tuple[str, ...] = (), minimum: int = 0) -> tuple[str, ...]:
        del minimum
        return csv_values.get(prompt_text, default)

    def _fake_prompt_int(prompt_text: str, default: int, minimum: int, maximum: int) -> int:
        del minimum, maximum
        return int_values.get(prompt_text, default)

    monkeypatch.setattr("src.commands.onboard._prompt_required", _fake_prompt_required)
    monkeypatch.setattr("src.commands.onboard._prompt_optional", _fake_prompt_optional)
    monkeypatch.setattr("src.commands.onboard._prompt_csv", _fake_prompt_csv)
    monkeypatch.setattr("src.commands.onboard._prompt_int", _fake_prompt_int)
    monkeypatch.setattr("src.commands.onboard._prompt_choice", lambda prompt_text, choices, default: default)
    monkeypatch.setattr("src.commands.onboard._prompt_supported_archetype", lambda default_edition_type=None: "detailed")

    result = run_onboard_create(
        edition_name="ai_newsletter",
        reports_root=reports_root,
        ai_enabled=True,
        assistant=_FakeOnboardAssistant(),
    )

    assert result.edition_name == "ai_newsletter"
    assert result.edition_path == editions_root / "ai_newsletter.yaml"
    assert result.readme_path == programs_root / "storage_demo" / "README-ai_newsletter.md"
    assert result.validation is not None
    assert result.validation.issue_number == 7
    assert result.validation.exit_code == 2
    assert result.validation.html_path == tmp_path / "output" / "ai_newsletter" / "issue_007.html"

    load_result = _load_v2_bundle(tmp_path, "ai_newsletter")
    assert load_result.mode == "v2"
    bundle = load_result.bundle
    readme_text = result.readme_path.read_text(encoding="utf-8")

    assert "program.yaml" in readme_text
    assert "issue 7 completed with warnings (exit code 2)." in readme_text
    assert bundle.config.ado.area_paths == (r"One\Storage\AI",)
    assert bundle.config.scorecards[0].name == "Delivery Scorecard"
    assert bundle.config.scorecards[0].dimensions[0].name == "Deployment Velocity"
    assert bundle.config.scorecards[0].dimensions[0].ado_filter == "area_path contains 'AI' AND type eq 'Feature'"
    assert bundle.program_context is not None
    assert bundle.program_context.writing_style is not None
    assert bundle.program_context.writing_style.voice == "Confident but honest."
    assert bundle.program_context.writing_style.structure == "Wins first, then risks."
    assert bundle.program_context.writing_style.risk_framing["stuck"] == "State blocker, action, and ETA."
    assert bundle.program_context.writing_style.preferred_patterns == (
        "Metric moved from {before} -> {after}.",
        "Blocked on {team}; mitigation: {action} by {date}",
    )
    assert bundle.program_context.tone_calibration is not None
    assert bundle.program_context.tone_calibration.overall == "concern"


def test_onboard_cli_bootstraps_new_narrative_edition(monkeypatch, tmp_path: Path) -> None:
    reports_root, editions_root, programs_root = _prepare_v2_roots(tmp_path)
    monkeypatch.setattr("src.commands.onboard.REPORTS_ROOT", reports_root)
    _stub_onboard_validation(monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        ["onboard", "--edition", "narrative_newsletter"],
        input="\n".join(_base_cli_inputs(archetype_choice="B")),
    )

    assert result.exit_code == 0, result.stdout
    load_result = load_bundle_with_mode(
        "narrative_newsletter",
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    assert load_result.mode == "v2"
    assert load_result.bundle.config.edition.type == "narrative"
    assert _read_yaml(editions_root / "narrative_newsletter.yaml")["altitude"] == "escalation"


def test_onboard_cli_bootstraps_new_condensed_edition(monkeypatch, tmp_path: Path) -> None:
    reports_root, editions_root, programs_root = _prepare_v2_roots(tmp_path)
    monkeypatch.setattr("src.commands.onboard.REPORTS_ROOT", reports_root)
    _stub_onboard_validation(monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        ["onboard", "--edition", "condensed_newsletter"],
        input="\n".join(_base_cli_inputs(archetype_choice="C")),
    )

    assert result.exit_code == 0, result.stdout
    load_result = load_bundle_with_mode(
        "condensed_newsletter",
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    assert load_result.mode == "v2"
    assert load_result.bundle.config.edition.type == "condensed"
    assert _read_yaml(editions_root / "condensed_newsletter.yaml")["altitude"] == "street"


def test_prompt_supported_archetype_accepts_narrative_choice(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.onboard.typer.prompt", lambda *args, **kwargs: "B")

    assert _prompt_supported_archetype() == "narrative"


def test_prompt_supported_archetype_accepts_condensed_choice(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.onboard.typer.prompt", lambda *args, **kwargs: "C")

    assert _prompt_supported_archetype() == "condensed"


def test_prompt_supported_archetype_accepts_deck_choice(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.onboard.typer.prompt", lambda *args, **kwargs: "D")

    assert _prompt_supported_archetype() == "deck"


def test_draft_from_existing_edition_allows_narrative(tmp_path: Path) -> None:
    reports_root, _editions_root, _programs_root = _seed_existing_v2_edition(
        tmp_path,
        edition_name="narrative_newsletter",
        program_id="narrative_program",
        edition_type="narrative",
    )

    draft = _draft_from_existing_edition("narrative_newsletter", reports_root)

    assert draft.identity is not None
    assert draft.identity.program_id == "narrative_program"
    assert draft.structure is not None
    assert draft.structure.edition_type == "narrative"


def test_draft_from_existing_edition_loads_workstreams_from_program_facts(monkeypatch, tmp_path: Path) -> None:
    reports_root, _editions_root, _programs_root = _seed_existing_v2_edition(
        tmp_path,
        edition_name="fact_backed_newsletter",
        program_id="fact_backed_program",
        edition_type="narrative",
    )
    monkeypatch.setattr(
        "src.commands.onboard.load_current_workstreams",
        lambda program_id, programs_root: (
            Workstream(
                id="fact-workstream",
                name="Fact-backed workstream",
                aliases=("Fact Alias",),
                area_paths=(r"Area\Fact",),
                dri_email="fact@example.com",
                alternate_owner="Fact Alternate",
                description="Fact description",
                why_it_matters="Fact why it matters",
                history_summary="Fact history",
                leadership_sensitivity="high",
                current_blocker="Fact blocker",
            ),
        ),
    )

    draft = _draft_from_existing_edition("fact_backed_newsletter", reports_root)

    assert draft.people is not None
    assert len(draft.people.workstreams) == 1
    workstream = draft.people.workstreams[0]
    assert workstream.name == "Fact-backed workstream"
    assert workstream.aliases == ("Fact Alias",)
    assert workstream.area_paths == (r"Area\Fact",)
    assert workstream.dri_email == "fact@example.com"
    assert workstream.alternate_owner == "Fact Alternate"
    assert workstream.description == "Fact description"
    assert workstream.why_it_matters == "Fact why it matters"
    assert workstream.history_summary == "Fact history"
    assert workstream.leadership_sensitivity == "high"
    assert workstream.current_blocker == "Fact blocker"


def test_onboard_update_preserves_unmodeled_v2_fields(monkeypatch, tmp_path: Path) -> None:
    reports_root, editions_root, programs_root = _seed_existing_v2_edition(
        tmp_path,
        edition_name="existing_newsletter",
        program_id="existing_program",
        edition_type="detailed",
    )
    monkeypatch.setattr("src.commands.onboard._review_stage", lambda edition_name, draft: "continue")
    monkeypatch.setattr("src.commands.onboard.typer.confirm", lambda message, default=True: True)
    _stub_onboard_validation(monkeypatch, tmp_path, issue_number=4)

    def _fake_prompt_required(prompt_text: str, default: str | None = None) -> str:
        if prompt_text == "One-sentence program objective (real-world outcome, not the update)":
            return "Updated objective."
        if default is not None:
            return default
        raise AssertionError(f"Unexpected required prompt without default: {prompt_text}")

    monkeypatch.setattr("src.commands.onboard._prompt_required", _fake_prompt_required)
    monkeypatch.setattr("src.commands.onboard._prompt_optional", lambda prompt_text, default=None: default)
    monkeypatch.setattr("src.commands.onboard._prompt_csv", lambda prompt_text, default=(), minimum=0: default)
    monkeypatch.setattr("src.commands.onboard._prompt_int", lambda prompt_text, default, minimum, maximum: default)
    monkeypatch.setattr("src.commands.onboard._prompt_choice", lambda prompt_text, choices, default: default)
    monkeypatch.setattr("src.commands.onboard._prompt_supported_archetype", lambda default_edition_type=None: default_edition_type or "detailed")

    result = run_onboard_update(
        edition_name="existing_newsletter",
        reports_root=reports_root,
    )

    assert result.edition_name == "existing_newsletter"
    assert result.program_id == "existing_program"
    assert (programs_root / "existing_program" / "README-existing_newsletter.md").exists()

    load_result = load_bundle_with_mode(
        "existing_newsletter",
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    assert load_result.mode == "v2"
    bundle = load_result.bundle
    edition_doc = _read_yaml(editions_root / "existing_newsletter.yaml")
    program_doc = _read_yaml(programs_root / "existing_program" / "program.yaml")
    workstreams_doc = _read_yaml(programs_root / "existing_program" / "workstreams.yaml")
    scorecards_doc = _read_yaml(programs_root / "existing_program" / "scorecards.yaml")
    editorial_doc = _read_yaml(programs_root / "existing_program" / "editorial_rules.yaml")
    people_directory_doc = _read_yaml(programs_root / "existing_program" / "knowledge" / "people_directory.yaml")

    assert bundle.program_context is not None
    assert bundle.program_context.objective == "Updated objective."
    assert edition_doc["brand_name"] == "Existing Brand"
    assert program_doc["distribution_defaults"]["to"] == ["team@example.com"]
    assert program_doc["ai"]["blurb_deployment"] == "existing-blurb"
    assert program_doc["logging"]["level"] == "DEBUG"
    assert scorecards_doc["scorecards"][0]["extra_field"] == "preserve-me"
    assert scorecards_doc["scorecards"][0]["dimensions"][0]["extra_dimension_field"] == "keep-me"
    assert program_doc["current_phase"] == "Ramp stabilization"
    assert program_doc["key_dependencies"][0]["from_item"] == "SCHIE commitments"
    assert program_doc["recurring_themes"] == ["Existing Scorecard"]
    assert workstreams_doc["workstreams"][0]["extra_workstream_field"] == "keep-workstream"
    assert workstreams_doc["workstreams"][0]["why_it_matters"] == "Existing why-it-matters."
    assert workstreams_doc["workstreams"][0]["history_summary"] == "Existing history summary."
    assert workstreams_doc["workstreams"][0]["leadership_sensitivity"] == "critical"
    assert workstreams_doc["workstreams"][0]["current_blocker"] == "Awaiting platform sign-off."
    assert program_doc["people"][1]["display_name"] == "Owner Name"
    assert program_doc["leadership_readers"][0]["name"] == "Executive Reader"
    assert workstreams_doc["workstream_owners"][0]["style_note"] == "Preserve this style note"
    assert program_doc["writing_style"]["preferred_patterns"] == ["Preserve this pattern"]
    assert program_doc["writing_style"]["risk_framing"]["stuck"] == "Preserve this stuck pattern."
    assert bundle.program_context.recurring_themes == ("Existing Scorecard",)
    assert program_doc["tone_calibration"]["per_theme_override"]["Existing Scorecard"] == "strong"
    assert program_doc["charter"] == _expected_charter_scaffold()
    assert program_doc["communication_plan"] == [
        {
            "edition": "existing_newsletter",
            "audience": "Existing Program",
            "channel": "email",
            "cadence": "weekly",
            "owner": "author",
        }
    ]
    assert workstreams_doc["workstreams"][0]["raci"] == _expected_raci_scaffold()
    assert editorial_doc["stale_warn_days"] == 21
    assert people_directory_doc["people"][1]["title"] == "Lead"


def test_onboard_update_preserves_existing_charter_and_raci_fields(monkeypatch, tmp_path: Path) -> None:
    reports_root, _editions_root, programs_root = _seed_existing_v2_edition(
        tmp_path,
        edition_name="existing_newsletter",
        program_id="existing_program",
        edition_type="detailed",
    )
    monkeypatch.setattr("src.commands.onboard._review_stage", lambda edition_name, draft: "continue")
    monkeypatch.setattr("src.commands.onboard.typer.confirm", lambda message, default=True: True)
    _stub_onboard_validation(monkeypatch, tmp_path, issue_number=4)

    program_path = programs_root / "existing_program" / "program.yaml"
    workstreams_path = programs_root / "existing_program" / "workstreams.yaml"

    program_doc = _read_yaml(program_path)
    program_doc["charter"] = {
        "scope_statement": "Deliver ramp readiness without slipping LT review.",
        "assumptions": ["Partner approvals remain on the current track."],
    }
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

    workstreams_doc = _read_yaml(workstreams_path)
    workstreams_doc["workstreams"][0]["raci"] = {
        "responsible": ["owner_alias"],
        "accountable": "ved_alias",
    }
    workstreams_path.write_text(yaml.safe_dump(workstreams_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

    monkeypatch.setattr("src.commands.onboard._prompt_required", lambda prompt_text, default=None: default or "Updated objective.")
    monkeypatch.setattr("src.commands.onboard._prompt_optional", lambda prompt_text, default=None: default)
    monkeypatch.setattr("src.commands.onboard._prompt_csv", lambda prompt_text, default=(), minimum=0: default)
    monkeypatch.setattr("src.commands.onboard._prompt_int", lambda prompt_text, default, minimum, maximum: default)
    monkeypatch.setattr("src.commands.onboard._prompt_choice", lambda prompt_text, choices, default: default)
    monkeypatch.setattr("src.commands.onboard._prompt_supported_archetype", lambda default_edition_type=None: default_edition_type or "detailed")

    run_onboard_update(
        edition_name="existing_newsletter",
        reports_root=reports_root,
    )

    updated_program_doc = _read_yaml(program_path)
    updated_workstreams_doc = _read_yaml(workstreams_path)

    assert updated_program_doc["charter"] == {
        "scope_statement": "Deliver ramp readiness without slipping LT review.",
        "assumptions": ["Partner approvals remain on the current track."],
        "success_criteria": [],
        "constraints": [],
        "stakeholder_register": [],
    }
    assert updated_workstreams_doc["workstreams"][0]["raci"] == {
        "responsible": ["owner_alias"],
        "accountable": "ved_alias",
        "consulted": [],
        "informed": [],
    }


def test_onboard_update_preserves_existing_communication_plan(monkeypatch, tmp_path: Path) -> None:
    reports_root, _editions_root, programs_root = _seed_existing_v2_edition(
        tmp_path,
        edition_name="existing_newsletter",
        program_id="existing_program",
        edition_type="detailed",
    )
    monkeypatch.setattr("src.commands.onboard._review_stage", lambda edition_name, draft: "continue")
    monkeypatch.setattr("src.commands.onboard.typer.confirm", lambda message, default=True: True)
    _stub_onboard_validation(monkeypatch, tmp_path, issue_number=4)

    program_path = programs_root / "existing_program" / "program.yaml"
    program_doc = _read_yaml(program_path)
    program_doc["communication_plan"] = [
        {
            "edition": "existing_daily",
            "audience": "Existing Daily Audience",
            "channel": "email",
            "cadence": "daily",
            "owner": "custom_owner",
        }
    ]
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

    extra_edition_path = programs_root / "existing_program" / "editions" / "existing_daily.yaml"
    extra_edition_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "existing_daily",
                "program_id": "existing_program",
                "name": "Existing Daily",
                "type": "condensed",
                "altitude": "street",
                "cadence": "daily",
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("src.commands.onboard._prompt_required", lambda prompt_text, default=None: default or "Updated objective.")
    monkeypatch.setattr("src.commands.onboard._prompt_optional", lambda prompt_text, default=None: default)
    monkeypatch.setattr("src.commands.onboard._prompt_csv", lambda prompt_text, default=(), minimum=0: default)
    monkeypatch.setattr("src.commands.onboard._prompt_int", lambda prompt_text, default, minimum, maximum: default)
    monkeypatch.setattr("src.commands.onboard._prompt_choice", lambda prompt_text, choices, default: default)
    monkeypatch.setattr("src.commands.onboard._prompt_supported_archetype", lambda default_edition_type=None: default_edition_type or "detailed")

    run_onboard_update(
        edition_name="existing_newsletter",
        reports_root=reports_root,
    )

    updated_program_doc = _read_yaml(program_path)

    assert updated_program_doc["communication_plan"] == [
        {
            "edition": "existing_daily",
            "audience": "Existing Daily Audience",
            "channel": "email",
            "cadence": "daily",
            "owner": "custom_owner",
        }
    ]


def test_write_yaml_creates_backup_when_overwriting_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "program.yaml"
    target.write_text(
        yaml.safe_dump({"name": "before", "schema_version": "1.0"}, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )

    _write_yaml(target, {"name": "after", "schema_version": "1.0"})

    backup_path = target.with_suffix(f"{target.suffix}.bak")

    assert _read_yaml(target) == {"name": "after", "schema_version": "1.0"}
    assert _read_yaml(backup_path) == {"name": "before", "schema_version": "1.0"}


def test_run_onboard_migrate_v3_scaffolds_files_and_seeds_charter_assumptions(monkeypatch, tmp_path: Path) -> None:
    reports_root, _editions_root, programs_root = _seed_existing_v2_edition(
        tmp_path,
        edition_name="existing_newsletter",
        program_id="existing_program",
        edition_type="detailed",
    )
    _stub_onboard_validation(monkeypatch, tmp_path, issue_number=5)

    program_path = programs_root / "existing_program" / "program.yaml"
    program_document = _read_yaml(program_path)
    program_document["charter"] = {
        "assumptions": [
            "Partner freeze date will stay stable through Q4.",
            "Schema sign-off will not require a new LT review.",
        ]
    }
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")

    first_result = run_onboard_migrate_v3(
        edition_name="existing_newsletter",
        reports_root=reports_root,
    )
    second_result = run_onboard_migrate_v3(
        edition_name="existing_newsletter",
        reports_root=reports_root,
    )

    program_dir = programs_root / "existing_program"
    assert first_result.program_dir == program_dir
    assert second_result.program_dir == program_dir

    milestones_doc = _read_yaml(program_dir / "milestones.yaml")
    risk_doc = _read_yaml(program_dir / "risk_register.yaml")
    escalation_doc = _read_yaml(program_dir / "escalation_rules.yaml")
    decisions_doc = _read_yaml(program_dir / "decisions.yaml")
    dependencies_doc = _read_yaml(program_dir / "dependencies.yaml")
    migrated_program_doc = _read_yaml(program_dir / "program.yaml")
    migrated_workstreams_doc = _read_yaml(program_dir / "workstreams.yaml")

    assert milestones_doc == {"schema_version": "1.0", "milestones": []}
    assert risk_doc == {"schema_version": "1.0", "risks": []}
    assert decisions_doc == {"schema_version": "1.0", "decisions": []}
    assert tuple(rule["name"] for rule in escalation_doc["rules"]) == (
        "consecutive_high",
        "milestone_at_risk",
        "unresolved_ask",
    )
    assert dependencies_doc["schema_version"] == "1.0"
    assert dependencies_doc["dependencies"][0]["from_workstream_id"] == "SCHIE commitments"
    assert dependencies_doc["dependencies"][0]["to_workstream_id"] == "Ramp"
    assert dependencies_doc["dependencies"][0]["risk_if_broken"] == "Ramp waits for SCHIE closure"
    assert migrated_program_doc["key_dependencies"][0]["from_item"] == "SCHIE commitments"
    assert migrated_program_doc["charter"] == {
        "scope_statement": None,
        "success_criteria": [],
        "assumptions": [
            "Partner freeze date will stay stable through Q4.",
            "Schema sign-off will not require a new LT review.",
        ],
        "constraints": [],
        "stakeholder_register": [],
    }
    assert migrated_program_doc["communication_plan"] == [
        {
            "edition": "existing_newsletter",
            "audience": "Existing Program",
            "channel": "email",
            "cadence": "weekly",
            "owner": "author",
        }
    ]
    assert migrated_workstreams_doc["workstreams"][0]["raci"] == _expected_raci_scaffold()

    assumptions = load_assumptions("existing_program", programs_root=programs_root)

    assert tuple(entry.text for entry in assumptions) == (
        "Partner freeze date will stay stable through Q4.",
        "Schema sign-off will not require a new LT review.",
    )
    assert all(entry.status.value == "unvalidated" for entry in assumptions)


def test_run_onboard_validation_returns_message_for_expected_report_failures(monkeypatch, tmp_path: Path) -> None:
    def _raise_auth_error(**kwargs):
        del kwargs
        raise AuthError("ADO auth failed")

    monkeypatch.setattr("src.commands.report.generate_report_draft", _raise_auth_error)

    validation = _run_onboard_validation("demo_newsletter", reports_root=tmp_path)

    assert validation.message == "Automatic validation dry-run could not complete: ADO auth failed"

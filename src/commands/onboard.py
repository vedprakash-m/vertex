from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import inspect
import os
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import typer
import yaml

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.onboard_assistant import OnboardAssistant, OnboardAssistantError, StyleSuggestions
from src.ai.llm_trace import AITraceContext, use_trace_context
from src.core.assumption_tracker import save_assumptions
from src.core.config_loader import REPORTS_ROOT, load_bundle
from src.core.decision_register import save_decisions
from src.core.edition_resolver import find_edition_yaml, resolve_edition_paths
from src.core.exceptions import VertexError
from src.core.models_v2 import Assumption, AssumptionStatus, Dependency
from src.core.operator_identity import capture_operator_identity
from src.core.people_registry_writer import (
    OnboardingPerson,
    OnboardingProgramGroup,
    register_onboarding_facts,
    shared_registry_is_active,
)
from src.core.policy_evaluator import build_default_escalation_rules_document
from src.core.program_fact_store import (
    load_current_workstreams,
    load_program_facts,
    project_assumptions,
    project_dependencies,
)
from src.core.risk_register_engine import save_risk_register
from src.core.workstream_documents import save_workstreams_document


_EDITION_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")
_CADENCES = ("daily", "weekly", "biweekly", "monthly")
_SEND_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_BASE_WORK_ITEM_TYPES = ("Feature", "Risk", "Scenario", "Key Result")
_BASE_EXCLUDED_STATES = ("Removed", "Cut")
_BASE_BANNED_PHRASES = (
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
)
_BASE_BANNED_OPENINGS = ("This week", "As mentioned", "It should be noted")
_DEFAULT_REVIEW_SECTIONS = ("exec_summary", "scorecard")


@dataclass(frozen=True, slots=True)
class DependencyStage:
    source: str
    target: str
    impact: str


@dataclass(frozen=True, slots=True)
class IdentityStage:
    program_name: str
    program_id: str
    objective: str
    mission: str
    newsletter_title: str
    cadence: str
    author_display_name: str
    author_email: str
    send_day: str | None
    send_time_local: str | None
    timezone: str | None
    current_phase: str | None = None
    key_dependency_chain: tuple[DependencyStage, ...] = ()


@dataclass(frozen=True, slots=True)
class ADOStage:
    organization: str
    project: str
    area_paths: tuple[str, ...]
    work_item_types: tuple[str, ...]
    excluded_states: tuple[str, ...]
    date_window_days: int
    api_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class DimensionStage:
    name: str
    description: str | None
    ado_filter: str
    workstream_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScorecardStage:
    name: str
    dimensions: tuple[DimensionStage, ...]


@dataclass(frozen=True, slots=True)
class StructureStage:
    edition_type: str
    scorecards: tuple[ScorecardStage, ...]


@dataclass(frozen=True, slots=True)
class WorkstreamStage:
    name: str
    aliases: tuple[str, ...]
    area_paths: tuple[str, ...]
    dri_email: str
    alternate_owner: str | None
    description: str | None
    why_it_matters: str | None = None
    history_summary: str | None = None
    leadership_sensitivity: str | None = None
    current_blocker: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewerStage:
    name: str
    sections: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LeadershipReaderStage:
    name: str
    role: str | None
    cares_about: tuple[str, ...]
    prefers: str | None
    pet_peeves: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkstreamOwnerStage:
    name: str
    areas: tuple[str, ...]
    style_note: str | None
    timezone: str | None
    alternate: str | None


@dataclass(frozen=True, slots=True)
class PeopleStage:
    workstreams: tuple[WorkstreamStage, ...]
    reviewers: tuple[ReviewerStage, ...]
    leadership_readers: tuple[LeadershipReaderStage, ...] = ()
    workstream_owners: tuple[WorkstreamOwnerStage, ...] = ()


@dataclass(frozen=True, slots=True)
class StyleStage:
    glossary: tuple[tuple[str, str], ...]
    extra_banned_phrases: tuple[str, ...]
    voice: str | None = None
    structure: str | None = None
    risk_framing_improving: str | None = None
    risk_framing_stuck: str | None = None
    risk_framing_escalation: str | None = None
    risk_framing_new_risk: str | None = None
    preferred_patterns: tuple[str, ...] = ()
    tone_overall: str | None = None
    recurring_themes: tuple[str, ...] = ()
    per_theme_overrides: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class OnboardDraft:
    identity: IdentityStage | None = None
    ado: ADOStage | None = None
    structure: StructureStage | None = None
    people: PeopleStage | None = None
    style: StyleStage | None = None


@dataclass(frozen=True, slots=True)
class OnboardDocuments:
    edition: dict[str, Any]
    program: dict[str, Any]
    workstreams: dict[str, Any]
    scorecards: dict[str, Any]
    editorial_rules: dict[str, Any]
    review: dict[str, Any]
    people_directory: dict[str, Any]
    teams: dict[str, Any]
    products: dict[str, Any]
    golden_queries: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OnboardPaths:
    repo_root: Path
    reports_root: Path
    editions_root: Path
    programs_root: Path
    edition_path: Path
    program_dir: Path
    knowledge_dir: Path


@dataclass(frozen=True, slots=True)
class OnboardValidationResult:
    issue_number: int | None = None
    exit_code: int | None = None
    html_path: Path | None = None
    md_path: Path | None = None
    manifest_path: Path | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class OnboardResult:
    edition_name: str
    program_id: str
    edition_path: Path
    program_dir: Path
    program_path: Path
    workstreams_path: Path
    scorecards_path: Path
    editorial_rules_path: Path
    review_path: Path
    readme_path: Path | None = None
    validation: OnboardValidationResult | None = None
    shared_registry_transaction_id: str | None = None


def onboard_command(
    edition: str | None = typer.Option(None, "--edition", help="New edition id, for example fabrikam_weekly."),
    update: str | None = typer.Option(None, "--update", help="Existing edition name to update."),
    migrate_v3: bool = typer.Option(False, "--migrate-v3", help="Scaffold V3 program-model files for an existing edition without running the interactive wizard."),
    migrate_deps: bool = typer.Option(False, "--migrate-deps", help="Compatibility alias for --migrate-v3 when migrating legacy dependencies into dependencies.yaml."),
    register_shared: bool = typer.Option(
        False,
        "--register-shared",
        help="Register onboarding people and workstream groups in the canonical shared registry.",
    ),
    ai: bool = typer.Option(False, "--ai", help="Use AI-assisted suggestions during onboarding when available."),
) -> None:
    run_migration = migrate_v3 or migrate_deps
    if (edition is None) == (update is None):
        raise typer.BadParameter("Specify exactly one of --edition or --update.")
    if run_migration and update is None:
        raise typer.BadParameter("--migrate-v3/--migrate-deps requires --update <edition>.")

    result = (
        run_onboard_migrate_v3(edition_name=update)
        if run_migration and update is not None
        else run_onboard_update(
            edition_name=update,
            ai_enabled=ai,
            register_shared=register_shared,
        )
        if update is not None
        else run_onboard_create(
            edition_name=edition or "",
            ai_enabled=ai,
            register_shared=register_shared,
        )
    )
    typer.echo(f"{'Onboarding migration' if run_migration else 'Onboarding'} complete for {result.edition_name}.")
    typer.echo(f"Edition config: {result.edition_path}")
    typer.echo(f"Program directory: {result.program_dir}")
    typer.echo(f"Program config: {result.program_path}")
    typer.echo(f"Workstreams: {result.workstreams_path}")
    typer.echo(f"Scorecards: {result.scorecards_path}")
    typer.echo(f"Editorial rules: {result.editorial_rules_path}")
    typer.echo(f"Review config: {result.review_path}")
    if result.readme_path is not None:
        typer.echo(f"README: {result.readme_path}")
    if result.shared_registry_transaction_id is not None:
        typer.echo(f"Shared registry transaction: {result.shared_registry_transaction_id}")
    if result.validation is not None:
        _print_validation_summary(result.validation)
    typer.echo(f"Re-run `vertex draft --edition {result.edition_name} --dry-run` after edits to validate the edition again.")


def run_onboard_create(
    edition_name: str,
    reports_root: Path | None = None,
    ai_enabled: bool = False,
    assistant: OnboardAssistant | None = None,
    register_shared: bool = False,
) -> OnboardResult:
    resolved_reports_root = reports_root or REPORTS_ROOT
    _validate_edition_name(edition_name)
    repo_root = resolved_reports_root.parent
    programs_root = repo_root / "programs"
    edition_path = find_edition_yaml(edition_name, programs_root=programs_root)
    legacy_report_dir = resolved_reports_root / edition_name
    if edition_path.exists() or legacy_report_dir.exists():
        existing_path = edition_path if edition_path.exists() else legacy_report_dir
        raise typer.BadParameter(f"Edition '{edition_name}' already exists at {existing_path}.")

    typer.echo(f"VERTEX ONBOARDING WIZARD — {edition_name}")
    typer.echo("=" * 46)

    resolved_assistant = assistant if assistant is not None else _resolve_onboard_assistant(
        ai_enabled,
        edition_name=edition_name,
        reports_root=resolved_reports_root,
    )
    draft = _collect_onboard_draft(edition_name, OnboardDraft(), assistant=resolved_assistant)
    if draft.identity is None:
        raise typer.BadParameter("Program identity is required before onboarding can continue.")
    paths = _resolve_onboard_paths(
        edition_name=edition_name,
        program_id=draft.identity.program_id,
        reports_root=resolved_reports_root,
    )
    if paths.program_dir.exists():
        raise typer.BadParameter(
            f"Program '{draft.identity.program_id}' already exists at {paths.program_dir}. Use --update for an existing V2 edition instead of creating a new program scaffold."
        )
    documents = _ensure_optional_authoring_scaffolds(
        _build_documents(edition_name, draft),
        edition_name=edition_name,
        paths=paths,
    )
    use_shared_registry = register_shared or shared_registry_is_active(paths.programs_root)
    typer.echo()
    typer.echo("FINAL YAML PREVIEW")
    typer.echo("-" * 18)
    _print_documents(documents, shared_factual=use_shared_registry)

    if not typer.confirm("Write these files?", default=True):
        typer.echo("Onboarding cancelled before writing files.")
        raise typer.Exit(code=0)

    return _finalize_onboarding(
        edition_name=edition_name,
        paths=paths,
        documents=documents,
        draft=draft,
        register_shared=register_shared,
    )


def run_onboard_update(
    edition_name: str,
    reports_root: Path | None = None,
    ai_enabled: bool = False,
    assistant: OnboardAssistant | None = None,
    register_shared: bool = False,
) -> OnboardResult:
    resolved_reports_root = reports_root or REPORTS_ROOT
    _validate_edition_name(edition_name)
    paths = _resolve_existing_onboard_paths(edition_name=edition_name, reports_root=resolved_reports_root)

    existing_documents = _load_existing_documents(paths)
    draft = _draft_from_existing_edition(edition_name, reports_root=resolved_reports_root)

    typer.echo(f"VERTEX ONBOARDING WIZARD — {edition_name} (update)")
    typer.echo("=" * 55)

    resolved_assistant = assistant if assistant is not None else _resolve_onboard_assistant(
        ai_enabled,
        edition_name=edition_name,
        reports_root=resolved_reports_root,
    )
    updated_draft = _collect_onboard_draft(edition_name, draft, assistant=resolved_assistant)
    use_shared_registry = register_shared or shared_registry_is_active(paths.programs_root)
    documents = _ensure_optional_authoring_scaffolds(
        _merge_existing_documents(
            existing_documents=existing_documents,
            generated_documents=_build_documents(edition_name, updated_draft),
            edition_name=edition_name,
            merge_factual=not use_shared_registry,
        ),
        edition_name=edition_name,
        paths=paths,
    )
    typer.echo()
    typer.echo("FINAL YAML PREVIEW")
    typer.echo("-" * 18)
    _print_documents(documents)

    if not typer.confirm("Write these files?", default=True):
        typer.echo("Onboarding cancelled before writing files.")
        raise typer.Exit(code=0)

    return _finalize_onboarding(
        edition_name=edition_name,
        paths=paths,
        documents=documents,
        draft=updated_draft,
        register_shared=register_shared,
    )


def run_onboard_migrate_v3(
    edition_name: str,
    reports_root: Path | None = None,
) -> OnboardResult:
    resolved_reports_root = reports_root or REPORTS_ROOT
    _validate_edition_name(edition_name)
    paths = _resolve_existing_onboard_paths(edition_name=edition_name, reports_root=resolved_reports_root)
    program_document = _read_yaml(paths.program_dir / "program.yaml")
    program_id = str(program_document.get("id") or paths.program_dir.name)

    _scaffold_migrate_v3_files(
        program_id=program_id,
        program_document=program_document,
        paths=paths,
    )

    validation = _run_onboard_validation(edition_name=edition_name, reports_root=resolved_reports_root)
    return _build_onboard_result(
        edition_name=edition_name,
        program_id=program_id,
        paths=paths,
        validation=validation,
    )


def _collect_onboard_draft(
    edition_name: str,
    initial_draft: OnboardDraft,
    assistant: OnboardAssistant | None = None,
) -> OnboardDraft:
    draft = initial_draft
    draft = replace(draft, identity=_collect_identity_stage(edition_name, draft))
    draft = replace(draft, ado=_collect_ado_stage(edition_name, draft, assistant=assistant))
    draft = replace(draft, structure=_collect_structure_stage(edition_name, draft, assistant=assistant))
    draft = replace(draft, people=_collect_people_stage(edition_name, draft))
    draft = replace(draft, structure=_resolve_structure_workstream_ids(draft.structure, draft.people))
    return replace(draft, style=_collect_style_stage(edition_name, draft, assistant=assistant))


def _collect_identity_stage(edition_name: str, draft: OnboardDraft) -> IdentityStage:
    while True:
        typer.echo()
        typer.echo("STAGE 1 — PROGRAM IDENTITY")
        typer.echo("-" * 28)
        current = draft.identity
        program_name = _prompt_required("Program name", default=(current.program_name or None) if current else None)
        default_program_id = current.program_id if current else _default_program_id(program_name, edition_name)
        program_id = _prompt_required("Program id", default=default_program_id)
        _validate_identifier(program_id, "Program id")
        objective = _prompt_required(
            "One-sentence program objective (real-world outcome, not the update)",
            default=(current.objective or None) if current else None,
        )
        mission = _prompt_required(
            "Program mission (why it matters to leadership and execution)",
            default=(current.mission or None) if current else None,
        )
        current_phase = _prompt_optional("Current phase", default=current.current_phase if current else None)
        dependency_count = _prompt_int(
            "How many key dependencies should be recorded?",
            default=(len(current.key_dependency_chain) if current else 0),
            minimum=0,
            maximum=20,
        )
        key_dependency_chain: list[DependencyStage] = []
        for dependency_index in range(dependency_count):
            existing_dependency = current.key_dependency_chain[dependency_index] if current and dependency_index < len(current.key_dependency_chain) else None
            typer.echo()
            typer.echo(f"Dependency {dependency_index + 1}/{dependency_count}")
            source = _prompt_required("Dependency source", default=existing_dependency.source if existing_dependency else None)
            target = _prompt_required("Dependency target", default=existing_dependency.target if existing_dependency else None)
            impact = _prompt_required("Dependency impact", default=existing_dependency.impact if existing_dependency else None)
            key_dependency_chain.append(DependencyStage(source=source, target=target, impact=impact))
        newsletter_title = _prompt_required(
            "Primary edition title",
            default=(current.newsletter_title or None) if current else None,
        )
        cadence = _prompt_choice("Cadence", _CADENCES, default=current.cadence if current else "weekly")
        author_display_name = _prompt_required("Author display name", default=(current.author_display_name or None) if current else None)
        author_email = _prompt_required("Author email", default=(current.author_email or None) if current else None)
        send_day = (
            _prompt_choice("Send day", _SEND_DAYS, default=current.send_day if current and current.send_day else "monday")
            if cadence in {"weekly", "biweekly"}
            else None
        )
        send_time_local = _prompt_optional("Send time local", default=current.send_time_local if current else "09:00")
        timezone = _prompt_optional("Timezone", default=current.timezone if current else "America/Los_Angeles")
        stage = IdentityStage(
            program_name=program_name,
            program_id=program_id,
            objective=objective,
            mission=mission,
            newsletter_title=newsletter_title,
            cadence=cadence,
            author_display_name=author_display_name,
            author_email=author_email,
            send_day=send_day,
            send_time_local=send_time_local,
            timezone=timezone,
            current_phase=current_phase,
            key_dependency_chain=tuple(key_dependency_chain),
        )
        if _review_stage(edition_name, OnboardDraft(identity=stage)) == "continue":
            return stage


def _collect_ado_stage(
    edition_name: str,
    draft: OnboardDraft,
    assistant: OnboardAssistant | None = None,
) -> ADOStage:
    while True:
        typer.echo()
        typer.echo("STAGE 2 — ADO SCOPE")
        typer.echo("-" * 21)
        current = draft.ado
        organization = _prompt_required("ADO organization", default=(current.organization if current else "your-org"))
        project = _prompt_required("ADO project", default=(current.project if current else "your-project"))
        area_path_defaults = current.area_paths if current else ()
        if assistant is not None and not area_path_defaults and draft.identity is not None:
            try:
                area_path_defaults = assistant.suggest_area_paths(
                    program_name=draft.identity.program_name,
                    organization=organization,
                    project=project,
                    api_timeout_seconds=current.api_timeout_seconds if current else 30,
                )
                if area_path_defaults:
                    typer.echo(f"AI suggested area paths: {', '.join(area_path_defaults)}")
            except OnboardAssistantError as error:
                typer.echo(f"AI area-path suggestion skipped: {error}")
        area_paths = _prompt_csv("Area paths (comma separated)", default=area_path_defaults, minimum=1)
        work_item_types = _prompt_csv(
            "Work item types (comma separated)",
            default=current.work_item_types if current else _BASE_WORK_ITEM_TYPES,
            minimum=1,
        )
        excluded_states = _prompt_csv(
            "Excluded states (comma separated)",
            default=current.excluded_states if current else _BASE_EXCLUDED_STATES,
        )
        date_window_days = _prompt_int(
            "Date window days",
            default=current.date_window_days if current else 14,
            minimum=1,
            maximum=365,
        )
        api_timeout_seconds = _prompt_int(
            "ADO API timeout seconds",
            default=current.api_timeout_seconds if current else 30,
            minimum=1,
            maximum=600,
        )
        stage = ADOStage(
            organization=organization,
            project=project,
            area_paths=area_paths,
            work_item_types=work_item_types,
            excluded_states=excluded_states,
            date_window_days=date_window_days,
            api_timeout_seconds=api_timeout_seconds,
        )
        if _review_stage(edition_name, OnboardDraft(identity=draft.identity, ado=stage)) == "continue":
            return stage


def _collect_structure_stage(
    edition_name: str,
    draft: OnboardDraft,
    assistant: OnboardAssistant | None = None,
) -> StructureStage:
    while True:
        typer.echo()
        typer.echo("STAGE 3 — EDITION STRUCTURE")
        typer.echo("-" * 32)
        current = draft.structure
        edition_type = _prompt_supported_archetype(default_edition_type=current.edition_type if current else None)
        default_scorecards = current.scorecards if current and current.scorecards else ()
        if assistant is not None and not default_scorecards and draft.identity is not None and draft.ado is not None:
            try:
                structure_suggestions = assistant.suggest_scorecards(
                    program_name=draft.identity.program_name,
                    objective=draft.identity.objective,
                    edition_type=edition_type,
                    organization=draft.ado.organization,
                    project=draft.ado.project,
                    area_paths=draft.ado.area_paths,
                    work_item_types=draft.ado.work_item_types,
                    excluded_states=draft.ado.excluded_states,
                    date_window_days=draft.ado.date_window_days,
                    api_timeout_seconds=draft.ado.api_timeout_seconds,
                )
                default_scorecards = tuple(
                    ScorecardStage(
                        name=scorecard.name,
                        dimensions=tuple(
                            DimensionStage(
                                name=dimension.name,
                                description=dimension.description,
                                ado_filter=dimension.ado_filter,
                            )
                            for dimension in scorecard.dimensions
                        ),
                    )
                    for scorecard in structure_suggestions.scorecards
                )
                if default_scorecards:
                    typer.echo(f"AI suggested {len(default_scorecards)} scorecard(s) from the current ADO scope.")
            except OnboardAssistantError as error:
                typer.echo(f"AI structure suggestion skipped: {error}")
        scorecard_count = _prompt_int(
            "How many scorecards?",
            default=(len(default_scorecards) if default_scorecards else 1),
            minimum=1,
            maximum=5,
        )
        scorecards: list[ScorecardStage] = []
        for scorecard_index in range(scorecard_count):
            existing_scorecard = default_scorecards[scorecard_index] if scorecard_index < len(default_scorecards) else None
            typer.echo()
            typer.echo(f"Scorecard {scorecard_index + 1}/{scorecard_count}")
            name = _prompt_required("Scorecard name", default=existing_scorecard.name if existing_scorecard else None)
            dimension_count = _prompt_int(
                "How many dimensions?",
                default=(len(existing_scorecard.dimensions) if existing_scorecard and existing_scorecard.dimensions else 1),
                minimum=1,
                maximum=20,
            )
            dimensions: list[DimensionStage] = []
            for dimension_index in range(dimension_count):
                existing_dimension = (
                    existing_scorecard.dimensions[dimension_index]
                    if existing_scorecard and dimension_index < len(existing_scorecard.dimensions)
                    else None
                )
                typer.echo(f"  Dimension {dimension_index + 1}/{dimension_count}")
                dimension_name = _prompt_required(
                    "  Dimension name",
                    default=existing_dimension.name if existing_dimension else None,
                )
                description = _prompt_optional(
                    "  Dimension description",
                    default=existing_dimension.description if existing_dimension else None,
                )
                ado_filter = _prompt_required(
                    "  ADO filter",
                    default=existing_dimension.ado_filter if existing_dimension else None,
                )
                dimensions.append(
                    DimensionStage(
                        name=dimension_name,
                        description=description,
                        ado_filter=ado_filter,
                    )
                )
            scorecards.append(ScorecardStage(name=name, dimensions=tuple(dimensions)))
        stage = StructureStage(edition_type=edition_type, scorecards=tuple(scorecards))
        if _review_stage(
            edition_name,
            OnboardDraft(identity=draft.identity, ado=draft.ado, structure=stage),
        ) == "continue":
            return stage


def _collect_people_stage(edition_name: str, draft: OnboardDraft) -> PeopleStage:
    if draft.identity is None or draft.ado is None or draft.structure is None:
        raise typer.BadParameter("Identity, ADO scope, and structure must be collected before Stage 4.")
    while True:
        typer.echo()
        typer.echo("STAGE 4 — PEOPLE & REVIEW")
        typer.echo("-" * 26)
        current = draft.people
        workstream_count = _prompt_int(
            "How many workstreams?",
            default=(len(current.workstreams) if current and current.workstreams else 1),
            minimum=1,
            maximum=20,
        )
        workstreams: list[WorkstreamStage] = []
        for workstream_index in range(workstream_count):
            existing_workstream = current.workstreams[workstream_index] if current and workstream_index < len(current.workstreams) else None
            typer.echo()
            typer.echo(f"Workstream {workstream_index + 1}/{workstream_count}")
            name = _prompt_required("Workstream name", default=existing_workstream.name if existing_workstream else None)
            aliases = _prompt_csv(
                "Aliases (comma separated)",
                default=existing_workstream.aliases if existing_workstream else (),
            )
            area_paths = _prompt_csv(
                "Workstream area paths (comma separated)",
                default=existing_workstream.area_paths if existing_workstream else draft.ado.area_paths,
                minimum=1,
            )
            dri_email = _prompt_required(
                "DRI email",
                default=existing_workstream.dri_email if existing_workstream else draft.identity.author_email,
            )
            alternate_owner = _prompt_optional(
                "Backup owner email",
                default=existing_workstream.alternate_owner if existing_workstream else None,
            )
            description = _prompt_optional(
                "Workstream description",
                default=existing_workstream.description if existing_workstream else None,
            )
            why_it_matters = _prompt_optional(
                "Why it matters",
                default=existing_workstream.why_it_matters if existing_workstream else None,
            )
            history_summary = _prompt_optional(
                "History summary",
                default=existing_workstream.history_summary if existing_workstream else None,
            )
            leadership_sensitivity = _prompt_optional(
                "Leadership sensitivity",
                default=existing_workstream.leadership_sensitivity if existing_workstream else None,
            )
            current_blocker = _prompt_optional(
                "Current blocker",
                default=existing_workstream.current_blocker if existing_workstream else None,
            )
            workstreams.append(
                WorkstreamStage(
                    name=name,
                    aliases=aliases,
                    area_paths=area_paths,
                    dri_email=dri_email,
                    alternate_owner=alternate_owner,
                    description=description,
                    why_it_matters=why_it_matters,
                    history_summary=history_summary,
                    leadership_sensitivity=leadership_sensitivity,
                    current_blocker=current_blocker,
                )
            )

        reviewer_count = _prompt_int(
            "How many reviewers should be seeded?",
            default=(len(current.reviewers) if current else 0),
            minimum=0,
            maximum=10,
        )
        reviewers: list[ReviewerStage] = []
        for reviewer_index in range(reviewer_count):
            existing_reviewer = current.reviewers[reviewer_index] if current and reviewer_index < len(current.reviewers) else None
            typer.echo()
            typer.echo(f"Reviewer {reviewer_index + 1}/{reviewer_count}")
            reviewer_name = _prompt_required("Reviewer name", default=existing_reviewer.name if existing_reviewer else None)
            sections = _prompt_csv(
                "Reviewer sections (comma separated)",
                default=existing_reviewer.sections if existing_reviewer else _DEFAULT_REVIEW_SECTIONS,
                minimum=1,
            )
            reviewers.append(ReviewerStage(name=reviewer_name, sections=sections))

        leadership_reader_count = _prompt_int(
            "How many leadership readers should be seeded?",
            default=(len(current.leadership_readers) if current else 0),
            minimum=0,
            maximum=10,
        )
        leadership_readers: list[LeadershipReaderStage] = []
        for leadership_index in range(leadership_reader_count):
            existing_reader = (
                current.leadership_readers[leadership_index]
                if current and leadership_index < len(current.leadership_readers)
                else None
            )
            typer.echo()
            typer.echo(f"Leadership reader {leadership_index + 1}/{leadership_reader_count}")
            reader_name = _prompt_required(
                "Leadership reader name",
                default=existing_reader.name if existing_reader else None,
            )
            role = _prompt_optional(
                "Leadership reader role",
                default=existing_reader.role if existing_reader else None,
            )
            cares_about = _prompt_csv(
                "Cares about (comma separated)",
                default=existing_reader.cares_about if existing_reader else (),
            )
            prefers = _prompt_optional(
                "Prefers",
                default=existing_reader.prefers if existing_reader else None,
            )
            pet_peeves = _prompt_csv(
                "Pet peeves (comma separated)",
                default=existing_reader.pet_peeves if existing_reader else (),
            )
            leadership_readers.append(
                LeadershipReaderStage(
                    name=reader_name,
                    role=role,
                    cares_about=cares_about,
                    prefers=prefers,
                    pet_peeves=pet_peeves,
                )
            )

        workstream_owner_count = _prompt_int(
            "How many workstream owner profiles should be seeded?",
            default=(len(current.workstream_owners) if current else 0),
            minimum=0,
            maximum=20,
        )
        workstream_owners: list[WorkstreamOwnerStage] = []
        for owner_index in range(workstream_owner_count):
            existing_owner = current.workstream_owners[owner_index] if current and owner_index < len(current.workstream_owners) else None
            typer.echo()
            typer.echo(f"Workstream owner {owner_index + 1}/{workstream_owner_count}")
            owner_name = _prompt_required("Owner name", default=existing_owner.name if existing_owner else None)
            areas = _prompt_csv(
                "Areas (comma separated)",
                default=existing_owner.areas if existing_owner else (),
            )
            style_note = _prompt_optional(
                "Style note",
                default=existing_owner.style_note if existing_owner else None,
            )
            timezone = _prompt_optional(
                "Timezone",
                default=existing_owner.timezone if existing_owner else None,
            )
            alternate = _prompt_optional(
                "Alternate owner name",
                default=existing_owner.alternate if existing_owner else None,
            )
            workstream_owners.append(
                WorkstreamOwnerStage(
                    name=owner_name,
                    areas=areas,
                    style_note=style_note,
                    timezone=timezone,
                    alternate=alternate,
                )
            )

        stage = PeopleStage(
            workstreams=tuple(workstreams),
            reviewers=tuple(reviewers),
            leadership_readers=tuple(leadership_readers),
            workstream_owners=tuple(workstream_owners),
        )
        if _review_stage(
            edition_name,
            OnboardDraft(
                identity=draft.identity,
                ado=draft.ado,
                structure=draft.structure,
                people=stage,
            ),
        ) == "continue":
            return stage


def _collect_style_stage(
    edition_name: str,
    draft: OnboardDraft,
    assistant: OnboardAssistant | None = None,
) -> StyleStage:
    while True:
        typer.echo()
        typer.echo("STAGE 5 — EDITORIAL DEFAULTS")
        typer.echo("-" * 30)
        current = draft.style
        suggested_style: StyleSuggestions | None = None
        if assistant is not None:
            sample_paragraph = _prompt_optional("Sample paragraph for AI style analysis (optional)")
            if sample_paragraph:
                try:
                    suggested_style = assistant.analyze_style_sample(sample_paragraph)
                    typer.echo("AI suggested style defaults from the sample paragraph.")
                except OnboardAssistantError as error:
                    typer.echo(f"AI style suggestion skipped: {error}")
        glossary_count = _prompt_int(
            "How many glossary entries?",
            default=(len(current.glossary) if current else 0),
            minimum=0,
            maximum=50,
        )
        glossary: list[tuple[str, str]] = []
        for glossary_index in range(glossary_count):
            existing_entry = current.glossary[glossary_index] if current and glossary_index < len(current.glossary) else None
            typer.echo()
            typer.echo(f"Glossary entry {glossary_index + 1}/{glossary_count}")
            term = _prompt_required("Term", default=existing_entry[0] if existing_entry else None)
            definition = _prompt_required("Definition", default=existing_entry[1] if existing_entry else None)
            glossary.append((term, definition))
        extra_banned_phrases = _prompt_csv(
            "Additional banned phrases (comma separated)",
            default=current.extra_banned_phrases if current else (),
        )
        voice = _prompt_optional(
            "Writing voice",
            default=current.voice if current and current.voice else (suggested_style.voice if suggested_style else None),
        )
        structure = _prompt_optional(
            "Preferred structure",
            default=current.structure if current and current.structure else (suggested_style.structure if suggested_style else None),
        )
        risk_framing_improving = _prompt_optional(
            "Risk framing (improving)",
            default=(
                current.risk_framing_improving
                if current and current.risk_framing_improving
                else (suggested_style.risk_framing_improving if suggested_style else None)
            ),
        )
        risk_framing_stuck = _prompt_optional(
            "Risk framing (stuck)",
            default=(
                current.risk_framing_stuck
                if current and current.risk_framing_stuck
                else (suggested_style.risk_framing_stuck if suggested_style else None)
            ),
        )
        risk_framing_escalation = _prompt_optional(
            "Risk framing (escalation)",
            default=(
                current.risk_framing_escalation
                if current and current.risk_framing_escalation
                else (suggested_style.risk_framing_escalation if suggested_style else None)
            ),
        )
        risk_framing_new_risk = _prompt_optional(
            "Risk framing (new risk)",
            default=(
                current.risk_framing_new_risk
                if current and current.risk_framing_new_risk
                else (suggested_style.risk_framing_new_risk if suggested_style else None)
            ),
        )
        preferred_pattern_defaults = (
            current.preferred_patterns
            if current and current.preferred_patterns
            else (suggested_style.preferred_patterns if suggested_style else ())
        )
        preferred_pattern_count = _prompt_int(
            "How many preferred patterns?",
            default=len(preferred_pattern_defaults),
            minimum=0,
            maximum=20,
        )
        preferred_patterns: list[str] = []
        for pattern_index in range(preferred_pattern_count):
            existing_pattern = preferred_pattern_defaults[pattern_index] if pattern_index < len(preferred_pattern_defaults) else None
            typer.echo()
            typer.echo(f"Preferred pattern {pattern_index + 1}/{preferred_pattern_count}")
            preferred_patterns.append(_prompt_required("Pattern", default=existing_pattern if existing_pattern else None))
        tone_overall = _prompt_optional(
            "Tone calibration (overall)",
            default=current.tone_overall if current else None,
        )
        current_theme_overrides = {theme: tone for theme, tone in (current.per_theme_overrides if current else ())}
        recurring_theme_count = _prompt_int(
            "How many recurring themes?",
            default=(len(current.recurring_themes) if current else 0),
            minimum=0,
            maximum=20,
        )
        recurring_themes: list[str] = []
        per_theme_overrides: list[tuple[str, str]] = []
        for theme_index in range(recurring_theme_count):
            existing_theme = current.recurring_themes[theme_index] if current and theme_index < len(current.recurring_themes) else None
            typer.echo()
            typer.echo(f"Recurring theme {theme_index + 1}/{recurring_theme_count}")
            theme_name = _prompt_required("Theme name", default=existing_theme if existing_theme else None)
            recurring_themes.append(theme_name)
            tone_override = _prompt_optional(
                "Tone override",
                default=current_theme_overrides.get(existing_theme) if existing_theme else None,
            )
            if tone_override:
                per_theme_overrides.append((theme_name, tone_override))

        ordered_recurring_themes = _dedupe_preserve_order(tuple(recurring_themes))
        ordered_theme_overrides: dict[str, str] = {}
        for theme_name, tone_override in per_theme_overrides:
            if theme_name in ordered_theme_overrides:
                continue
            ordered_theme_overrides[theme_name] = tone_override
        stage = StyleStage(
            glossary=tuple(glossary),
            extra_banned_phrases=extra_banned_phrases,
            voice=voice,
            structure=structure,
            risk_framing_improving=risk_framing_improving,
            risk_framing_stuck=risk_framing_stuck,
            risk_framing_escalation=risk_framing_escalation,
            risk_framing_new_risk=risk_framing_new_risk,
            preferred_patterns=tuple(preferred_patterns),
            tone_overall=tone_overall,
            recurring_themes=ordered_recurring_themes,
            per_theme_overrides=tuple(ordered_theme_overrides.items()),
        )
        if _review_stage(
            edition_name,
            OnboardDraft(
                identity=draft.identity,
                ado=draft.ado,
                structure=draft.structure,
                people=draft.people,
                style=stage,
            ),
        ) == "continue":
            return stage


def _review_stage(edition_name: str, draft: OnboardDraft) -> str:
    typer.echo()
    typer.echo("Current YAML preview")
    typer.echo("-" * 20)
    _print_documents(_build_documents(edition_name, draft))
    while True:
        choice = typer.prompt("Continue, re-enter, or abort? [C/R/A]", default="C").strip().upper()
        if choice == "C":
            return "continue"
        if choice == "R":
            return "retry"
        if choice == "A":
            typer.echo("Onboarding cancelled before writing files.")
            raise typer.Exit(code=0)
        typer.echo("Enter C, R, or A.")


def _resolve_structure_workstream_ids(
    structure: StructureStage | None,
    people: PeopleStage | None,
) -> StructureStage | None:
    if structure is None or people is None or not people.workstreams:
        return structure

    workstream_options = tuple(
        (_make_identifier(workstream.name), workstream.name, workstream.aliases)
        for workstream in people.workstreams
    )
    known_ids = {workstream_id for workstream_id, _, _ in workstream_options}
    if not structure.scorecards:
        return structure

    resolved_scorecards: list[ScorecardStage] = []
    for scorecard in structure.scorecards:
        resolved_dimensions: list[DimensionStage] = []
        for dimension in scorecard.dimensions:
            if dimension.workstream_id in known_ids:
                resolved_dimensions.append(dimension)
                continue
            guessed_workstream_id = _guess_workstream_id(scorecard.name, dimension.name, workstream_options)
            if guessed_workstream_id is None:
                guessed_workstream_id = _prompt_workstream_assignment(scorecard.name, dimension.name, workstream_options)
            resolved_dimensions.append(replace(dimension, workstream_id=guessed_workstream_id))
        resolved_scorecards.append(replace(scorecard, dimensions=tuple(resolved_dimensions)))
    return replace(structure, scorecards=tuple(resolved_scorecards))


def _guess_workstream_id(
    scorecard_name: str,
    dimension_name: str,
    workstream_options: tuple[tuple[str, str, tuple[str, ...]], ...],
) -> str | None:
    if len(workstream_options) == 1:
        return workstream_options[0][0]

    haystack = f"{scorecard_name} {dimension_name}".lower()
    matches: list[str] = []
    for workstream_id, workstream_name, aliases in workstream_options:
        tokens = [workstream_id.replace("_", " "), workstream_name.lower()]
        tokens.extend(alias.lower().replace("_", " ") for alias in aliases)
        if any(token and token in haystack for token in tokens):
            matches.append(workstream_id)
    if len(matches) == 1:
        return matches[0]
    return None


def _prompt_workstream_assignment(
    scorecard_name: str,
    dimension_name: str,
    workstream_options: tuple[tuple[str, str, tuple[str, ...]], ...],
) -> str:
    option_labels = ", ".join(f"{workstream_id}={workstream_name}" for workstream_id, workstream_name, _ in workstream_options)
    default_workstream_id = workstream_options[0][0]
    while True:
        raw_value = typer.prompt(
            f"Workstream for {scorecard_name} / {dimension_name} [{option_labels}]",
            default=default_workstream_id,
            show_default=True,
        ).strip()
        normalized_value = raw_value.lower()
        for workstream_id, workstream_name, aliases in workstream_options:
            alias_set = {alias.lower() for alias in aliases}
            if normalized_value in {workstream_id, workstream_name.lower(), *alias_set}:
                return workstream_id
        typer.echo(f"Enter one of: {option_labels}")


def _draft_from_existing_edition(edition_name: str, reports_root: Path) -> OnboardDraft:
    repo_root = reports_root.parent
    programs_root = repo_root / "programs"
    resolved_paths = resolve_edition_paths(
        edition_name,
        programs_root=programs_root,
    )
    if resolved_paths is None:
        raise typer.BadParameter(
            f"Edition '{edition_name}' does not exist under programs/<program>/editions/. Onboard update currently targets V2 editions only."
        )

    edition_doc = _read_yaml(resolved_paths.edition_path)
    program_doc = _read_yaml(resolved_paths.program_dir / "program.yaml")
    workstreams_doc = _read_yaml(resolved_paths.program_dir / "workstreams.yaml", required=False)
    current_workstreams = load_current_workstreams(
        resolved_paths.program_id,
        programs_root=programs_root,
    )
    scorecards_doc = _read_yaml(resolved_paths.program_dir / "scorecards.yaml")
    review_doc = _read_yaml(resolved_paths.program_dir / "review.yaml", required=False)

    bundle = load_bundle(
        edition_name,
        reports_root=reports_root,
        programs_root=programs_root,
    )
    program_context = bundle.program_context
    author_defaults = _mapping(program_doc.get("author_defaults"))
    ado_doc = _mapping(program_doc.get("ado"))
    writing_style_doc = _mapping(program_doc.get("writing_style"))
    tone_calibration_doc = _mapping(program_doc.get("tone_calibration"))

    identity = IdentityStage(
        program_name=str(program_doc.get("name") or (program_context.program_name if program_context is not None else edition_name)),
        program_id=resolved_paths.program_id,
        objective=str(program_doc.get("objective") or (program_context.objective if program_context is not None else "")),
        mission=str(program_doc.get("mission") or (program_context.mission if program_context is not None else "")),
        newsletter_title=_extract_newsletter_title(str(edition_doc.get("name") or bundle.config.edition.title)),
        cadence=str(edition_doc.get("cadence") or bundle.config.edition.cadence),
        author_display_name=str(author_defaults.get("display_name") or bundle.config.author.display_name),
        author_email=str(author_defaults.get("email") or bundle.config.author.email),
        send_day=_optional_str(edition_doc.get("send_day")) or bundle.config.edition.send_day,
        send_time_local=_optional_str(edition_doc.get("send_time_local")) or bundle.config.edition.send_time_local,
        timezone=_optional_str(edition_doc.get("timezone")) or bundle.config.edition.timezone,
        current_phase=_optional_str(program_doc.get("current_phase")) or (program_context.current_phase if program_context is not None else None),
        key_dependency_chain=tuple(
            DependencyStage(
                source=str(dependency.get("from_item", "")).strip(),
                target=str(dependency.get("to_item", "")).strip(),
                impact=str(dependency.get("impact", "")).strip(),
            )
            for dependency in program_doc.get("key_dependencies", [])
            if isinstance(dependency, dict)
            and str(dependency.get("from_item", "")).strip()
            and str(dependency.get("to_item", "")).strip()
            and str(dependency.get("impact", "")).strip()
        ),
    )
    ado = ADOStage(
        organization=str(ado_doc.get("organization") or bundle.config.ado.organization),
        project=str(ado_doc.get("project") or bundle.config.ado.project),
        area_paths=tuple(str(path) for path in ado_doc.get("area_paths", bundle.config.ado.area_paths) if str(path).strip()),
        work_item_types=tuple(str(item) for item in ado_doc.get("work_item_types", bundle.config.ado.work_item_types) if str(item).strip()),
        excluded_states=tuple(str(item) for item in ado_doc.get("excluded_states", bundle.config.ado.excluded_states) if str(item).strip()),
        date_window_days=int(ado_doc.get("date_window_days", bundle.config.ado.date_window_days)),
        api_timeout_seconds=int(ado_doc.get("api_timeout_seconds", bundle.config.ado.api_timeout_seconds or 30)),
    )
    structure = StructureStage(
        edition_type=str(edition_doc.get("type") or bundle.config.edition.type),
        scorecards=tuple(
            ScorecardStage(
                name=str(scorecard.get("name", "")).strip(),
                dimensions=tuple(
                    DimensionStage(
                        name=str(dimension.get("name", "")).strip(),
                        description=_optional_str(dimension.get("description")),
                        ado_filter=str(dimension.get("ado_filter", "")).strip(),
                        workstream_id=_optional_str(dimension.get("workstream_id")),
                    )
                    for dimension in scorecard.get("dimensions", [])
                    if isinstance(dimension, dict) and str(dimension.get("name", "")).strip()
                ),
            )
            for scorecard in scorecards_doc.get("scorecards", [])
            if isinstance(scorecard, dict) and str(scorecard.get("name", "")).strip()
        ),
    )
    people = PeopleStage(
        workstreams=tuple(
            WorkstreamStage(
                name=workstream.name,
                aliases=workstream.aliases,
                area_paths=workstream.area_paths,
                dri_email=str(workstream.dri_email or bundle.config.author.email),
                alternate_owner=workstream.alternate_owner,
                description=workstream.description,
                why_it_matters=workstream.why_it_matters,
                history_summary=workstream.history_summary,
                leadership_sensitivity=workstream.leadership_sensitivity,
                current_blocker=workstream.current_blocker,
            )
            for workstream in current_workstreams
            if workstream.name.strip()
        ),
        reviewers=tuple(
            ReviewerStage(
                name=str(reviewer.get("name", "")).strip(),
                sections=tuple(str(section) for section in reviewer.get("sections", []) if str(section).strip()),
            )
            for reviewer in review_doc.get("reviewers", [])
            if isinstance(reviewer, dict) and str(reviewer.get("name", "")).strip()
        ),
        leadership_readers=tuple(
            LeadershipReaderStage(
                name=str(reader.get("name", "")).strip(),
                role=_optional_str(reader.get("role")),
                cares_about=tuple(str(item) for item in reader.get("cares_about", []) if str(item).strip()),
                prefers=_optional_str(reader.get("prefers")),
                pet_peeves=tuple(str(item) for item in reader.get("pet_peeves", []) if str(item).strip()),
            )
            for reader in program_doc.get("leadership_readers", [])
            if isinstance(reader, dict) and str(reader.get("name", "")).strip()
        ),
        workstream_owners=tuple(
            WorkstreamOwnerStage(
                name=str(owner.get("name", "")).strip(),
                areas=tuple(str(area) for area in owner.get("areas", []) if str(area).strip()),
                style_note=_optional_str(owner.get("style_note")),
                timezone=_optional_str(owner.get("timezone")),
                alternate=_optional_str(owner.get("alternate")),
            )
            for owner in workstreams_doc.get("workstream_owners", [])
            if isinstance(owner, dict) and str(owner.get("name", "")).strip()
        ),
    )
    per_theme_overrides = _mapping(tone_calibration_doc.get("per_theme_override"))
    style = StyleStage(
        glossary=tuple((str(term), str(definition)) for term, definition in _mapping(program_doc.get("glossary")).items()),
        extra_banned_phrases=tuple(
            phrase
            for phrase in bundle.editorial_rules.banned_phrases
            if phrase.strip().lower() not in {base.lower() for base in _BASE_BANNED_PHRASES}
        ),
        voice=_optional_str(writing_style_doc.get("voice")),
        structure=_optional_str(writing_style_doc.get("structure")),
        risk_framing_improving=_optional_str(_mapping(writing_style_doc.get("risk_framing")).get("improving")),
        risk_framing_stuck=_optional_str(_mapping(writing_style_doc.get("risk_framing")).get("stuck")),
        risk_framing_escalation=_optional_str(_mapping(writing_style_doc.get("risk_framing")).get("escalation")),
        risk_framing_new_risk=_optional_str(_mapping(writing_style_doc.get("risk_framing")).get("new_risk")),
        preferred_patterns=tuple(str(pattern) for pattern in writing_style_doc.get("preferred_patterns", []) if str(pattern).strip()),
        tone_overall=_optional_str(tone_calibration_doc.get("overall")),
        recurring_themes=tuple(str(theme) for theme in program_doc.get("recurring_themes", []) if str(theme).strip()),
        per_theme_overrides=tuple(
            (str(theme), str(tone))
            for theme, tone in per_theme_overrides.items()
            if str(theme).strip() and str(tone).strip()
        ),
    )
    return OnboardDraft(identity=identity, ado=ado, structure=structure, people=people, style=style)


def _build_documents(edition_name: str, draft: OnboardDraft) -> OnboardDocuments:
    program_id = draft.identity.program_id if draft.identity is not None else _default_program_id("", edition_name)
    workstream_records = [
        _compact(
            {
                "id": _make_identifier(workstream.name),
                "name": workstream.name,
                "aliases": list(workstream.aliases),
                "area_paths": list(workstream.area_paths),
                "dri_email": workstream.dri_email,
                "alternate_owner": workstream.alternate_owner,
                "description": workstream.description,
                "why_it_matters": workstream.why_it_matters,
                "history_summary": workstream.history_summary,
                "leadership_sensitivity": workstream.leadership_sensitivity,
                "current_blocker": workstream.current_blocker,
            }
        )
        for workstream in (draft.people.workstreams if draft.people else ())
    ]
    scorecard_records = [
        {
            "name": scorecard.name,
            "dimensions": [
                _compact(
                    {
                        "name": dimension.name,
                        "description": dimension.description,
                        "ado_filter": dimension.ado_filter,
                        "workstream_id": dimension.workstream_id,
                    }
                )
                for dimension in scorecard.dimensions
            ],
        }
        for scorecard in (draft.structure.scorecards if draft.structure else ())
    ]
    editorial_rules = {
        "schema_version": "1.0",
        "stale_warn_days": 14,
        "stale_block_days": 30,
        "banned_phrases": list(_dedupe_preserve_order(_BASE_BANNED_PHRASES + (draft.style.extra_banned_phrases if draft.style else ()))),
        "banned_openings": list(_BASE_BANNED_OPENINGS),
        "verbosity": {
            "workstream_blurb_max_sentences": 3,
            "workstream_blurb_max_words": 60,
            "exec_bullet_max_words": 25,
            "exec_max_bullets": 3,
            "scorecard_summary_max_sentences": 3,
        },
    }
    review = {
        "reviewers": [
            {
                "name": reviewer.name,
                "sections": list(reviewer.sections),
            }
            for reviewer in (draft.people.reviewers if draft.people else ())
        ],
        "required": bool(draft.people and draft.people.reviewers),
    }
    return OnboardDocuments(
        edition=_compact(
            {
                "schema_version": "2.0",
                "id": edition_name,
                "program_id": program_id,
                "name": f"{draft.identity.newsletter_title} | Issue {{issue_number}} | {{date}}" if draft.identity else None,
                "type": draft.structure.edition_type if draft.structure else None,
                "altitude": _default_altitude_for_edition_type(draft.structure.edition_type) if draft.structure else None,
                "cadence": draft.identity.cadence if draft.identity else None,
                "send_day": draft.identity.send_day if draft.identity else None,
                "send_time_local": draft.identity.send_time_local if draft.identity else None,
                "timezone": draft.identity.timezone if draft.identity else None,
            }
        ),
        program=_compact(
            {
                "schema_version": "3.0",
                "id": program_id,
                "name": draft.identity.program_name if draft.identity else None,
                "objective": draft.identity.objective if draft.identity else None,
                "mission": draft.identity.mission if draft.identity else None,
                "current_phase": draft.identity.current_phase if draft.identity else None,
                "pillars": [scorecard.name for scorecard in (draft.structure.scorecards if draft.structure else ())],
                "glossary": {key: value for key, value in (draft.style.glossary if draft.style else ())},
                "people": _build_program_people(draft),
                "leadership_readers": [
                    _compact(
                        {
                            "name": reader.name,
                            "role": reader.role,
                            "cares_about": list(reader.cares_about),
                            "prefers": reader.prefers,
                            "pet_peeves": list(reader.pet_peeves),
                        }
                    )
                    for reader in (draft.people.leadership_readers if draft.people else ())
                ],
                "recurring_themes": list(draft.style.recurring_themes) if draft.style else [],
                "writing_style": _compact(
                    {
                        "voice": draft.style.voice if draft.style else None,
                        "structure": draft.style.structure if draft.style else None,
                        "risk_framing": _compact(
                            {
                                "improving": draft.style.risk_framing_improving if draft.style else None,
                                "stuck": draft.style.risk_framing_stuck if draft.style else None,
                                "escalation": draft.style.risk_framing_escalation if draft.style else None,
                                "new_risk": draft.style.risk_framing_new_risk if draft.style else None,
                            }
                        ),
                        "preferred_patterns": list(draft.style.preferred_patterns) if draft.style else [],
                    }
                ),
                "tone_calibration": _compact(
                    {
                        "overall": draft.style.tone_overall if draft.style else None,
                        "per_theme_override": {
                            theme_name: tone_override
                            for theme_name, tone_override in (draft.style.per_theme_overrides if draft.style else ())
                        },
                    }
                ),
                "key_dependencies": [
                    {
                        "from_item": dependency.source,
                        "to_item": dependency.target,
                        "impact": dependency.impact,
                    }
                    for dependency in (draft.identity.key_dependency_chain if draft.identity else ())
                ],
                "author_defaults": {
                    "display_name": draft.identity.author_display_name if draft.identity else None,
                    "email": draft.identity.author_email if draft.identity else None,
                },
                "distribution_defaults": {
                    "to": [draft.identity.author_email] if draft.identity else [],
                    "cc": [],
                    "channels": ["email"],
                },
                "communication_plan": [
                    _compact(
                        {
                            "edition": edition_name,
                            "audience": draft.identity.program_name if draft.identity else None,
                            "channel": "email",
                            "cadence": draft.identity.cadence if draft.identity else None,
                            "owner": _alias_from_email(draft.identity.author_email) if draft.identity else None,
                        }
                    )
                ],
                "ado": {
                    "organization": draft.ado.organization if draft.ado else None,
                    "project": draft.ado.project if draft.ado else None,
                    "area_paths": list(draft.ado.area_paths) if draft.ado else [],
                    "work_item_types": list(draft.ado.work_item_types) if draft.ado else [],
                    "excluded_states": list(draft.ado.excluded_states) if draft.ado else [],
                    "date_window_days": draft.ado.date_window_days if draft.ado else None,
                    "api_timeout_seconds": draft.ado.api_timeout_seconds if draft.ado else None,
                },
                "ai": {
                    "enabled": False,
                    "budget_usd_per_run": 0.5,
                    "temperature": 0.2,
                },
                "kusto": {
                    "enabled": False,
                },
                "m365": {
                    "enabled": False,
                    "prefer_agency": True,
                },
                "logging": {
                    "level": "INFO",
                    "json": False,
                },
            }
        ),
        workstreams=_compact(
            {
                "schema_version": "2.0",
                "workstreams": workstream_records,
                "workstream_owners": [
                    _compact(
                        {
                            "name": owner.name,
                            "areas": list(owner.areas),
                            "style_note": owner.style_note,
                            "timezone": owner.timezone,
                            "alternate": owner.alternate,
                        }
                    )
                    for owner in (draft.people.workstream_owners if draft.people else ())
                ],
            }
        ),
        scorecards=_compact(
            {
                "schema_version": "2.0",
                "scorecards": scorecard_records,
            }
        ),
        editorial_rules=_compact(editorial_rules),
        review=_compact(review),
        people_directory=_build_people_directory_document(draft, program_id=program_id),
        teams=_build_teams_document(draft, program_id=program_id),
        products={"schema_version": "1.0", "products": []},
        golden_queries={"schema_version": "1.0", "queries": []},
    )


def _build_program_people(draft: OnboardDraft) -> list[dict[str, Any]]:
    if draft.identity is None:
        return []
    workstream_names = [workstream.name for workstream in (draft.people.workstreams if draft.people else ())]
    email_to_person: dict[str, dict[str, Any]] = {
        draft.identity.author_email: {
            "email": draft.identity.author_email,
            "display_name": draft.identity.author_display_name,
            "role": "Author",
            "workstreams": workstream_names,
        }
    }
    for workstream in draft.people.workstreams if draft.people else ():
        owner_entry = email_to_person.get(workstream.dri_email)
        if owner_entry is None:
            email_to_person[workstream.dri_email] = {
                "email": workstream.dri_email,
                "display_name": None,
                "role": "Owner",
                "workstreams": [workstream.name],
            }
        elif workstream.name not in owner_entry["workstreams"]:
            owner_entry["workstreams"].append(workstream.name)

        if not workstream.alternate_owner:
            continue
        backup_entry = email_to_person.get(workstream.alternate_owner)
        if backup_entry is None:
            email_to_person[workstream.alternate_owner] = {
                "email": workstream.alternate_owner,
                "display_name": None,
                "role": "Backup",
                "workstreams": [workstream.name],
            }
        elif workstream.name not in backup_entry["workstreams"]:
            backup_entry["workstreams"].append(workstream.name)

    return [_compact(entry) for entry in email_to_person.values()]


def _build_people_directory_document(draft: OnboardDraft, *, program_id: str) -> dict[str, Any]:
    people_directory_entries: list[dict[str, Any]] = []
    workstream_ids_by_name = {
        workstream.name: _make_identifier(workstream.name)
        for workstream in (draft.people.workstreams if draft.people else ())
    }
    for person in _build_program_people(draft):
        email = _optional_str(person.get("email"))
        if email is None:
            continue
        alias = _alias_from_email(email)
        team_ids = [workstream_ids_by_name[name] for name in person.get("workstreams", []) if name in workstream_ids_by_name]
        people_directory_entries.append(
            _compact(
                {
                    "alias": alias,
                    "email": email,
                    "display_name": _optional_str(person.get("display_name")),
                    "team_ids": list(_dedupe_preserve_order(tuple(team_ids))),
                    "org_chain": [program_id],
                }
            )
        )
    return {
        "schema_version": "1.0",
        "sensitivity": "internal",
        "people": people_directory_entries,
    }


def _onboarding_people(draft: OnboardDraft) -> tuple[OnboardingPerson, ...]:
    workstream_ids_by_name = {
        workstream.name: _make_identifier(workstream.name)
        for workstream in (draft.people.workstreams if draft.people else ())
    }
    return tuple(
        OnboardingPerson(
            alias=_alias_from_email(email),
            email=email,
            display_name=_optional_str(person.get("display_name")),
            team_ids=tuple(
                workstream_ids_by_name[name]
                for name in person.get("workstreams", [])
                if name in workstream_ids_by_name
            ),
        )
        for person in _build_program_people(draft)
        if (email := _optional_str(person.get("email"))) is not None
    )


def _onboarding_program_groups(draft: OnboardDraft) -> tuple[OnboardingProgramGroup, ...]:
    return tuple(
        OnboardingProgramGroup(
            id=_make_identifier(workstream.name),
            name=workstream.name,
            area_paths=workstream.area_paths,
        )
        for workstream in (draft.people.workstreams if draft.people else ())
    )


def _build_teams_document(draft: OnboardDraft, *, program_id: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "teams": [
            _compact(
                {
                    "id": _make_identifier(workstream.name),
                    "name": workstream.name,
                    "area_paths": list(workstream.area_paths),
                    "programs": [program_id],
                }
            )
            for workstream in (draft.people.workstreams if draft.people else ())
        ],
    }


def _print_documents(documents: OnboardDocuments, *, shared_factual: bool = False) -> None:
    document_pairs = [
        ("editions/<edition>.yaml", documents.edition),
        ("program.yaml", documents.program),
        ("workstreams.yaml", documents.workstreams),
        ("scorecards.yaml", documents.scorecards),
        ("editorial_rules.yaml", documents.editorial_rules),
        ("review.yaml", documents.review),
        ("knowledge/products.yaml", documents.products),
        ("knowledge/golden_queries.yaml", documents.golden_queries),
    ]
    if shared_factual:
        typer.echo("shared registry factual registration")
        typer.echo("  people and workstream groups will be written as typed shared registry records")
    else:
        document_pairs[6:6] = [
            ("knowledge/people_directory.yaml", documents.people_directory),
            ("knowledge/teams.yaml", documents.teams),
        ]
    for label, document in document_pairs:
        typer.echo(label)
        typer.echo(_dump_yaml(document), nl=False)


def _write_yaml(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(f"{path.suffix}.bak"))
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(_dump_yaml(document), encoding="utf-8")
    os.replace(temp_path, path)


def _dump_yaml(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=False)


def _scaffold_migrate_v3_files(
    *,
    program_id: str,
    program_document: dict[str, Any],
    paths: OnboardPaths,
) -> None:
    edition_document = _read_yaml(paths.edition_path)
    changed_program_document = _ensure_program_charter_scaffold(program_document)
    changed_program_document = (
        _ensure_program_communication_plan_scaffold(
            program_document,
            edition_document=edition_document,
            edition_name=paths.edition_path.stem,
            paths=paths,
        )
        or changed_program_document
    )
    if changed_program_document:
        _write_yaml(paths.program_dir / "program.yaml", program_document)

    workstreams_document = _read_yaml(paths.program_dir / "workstreams.yaml", required=False)
    if _ensure_workstream_raci_scaffolds(workstreams_document):
        save_workstreams_document(program_id=program_id, document=workstreams_document, programs_root=paths.programs_root)

    _ensure_program_store_scaffolds(program_id, paths)
    _ensure_dependencies_migration(program_id, paths)
    _ensure_assumption_seed(program_id, program_document, paths)


def _ensure_optional_authoring_scaffolds(
    documents: OnboardDocuments,
    *,
    edition_name: str,
    paths: OnboardPaths,
) -> OnboardDocuments:
    program_document = deepcopy(documents.program)
    workstreams_document = deepcopy(documents.workstreams)
    _ensure_program_charter_scaffold(program_document)
    _ensure_program_communication_plan_scaffold(
        program_document,
        edition_document=documents.edition,
        edition_name=edition_name,
        paths=paths,
    )
    _ensure_workstream_raci_scaffolds(workstreams_document)
    return replace(documents, program=program_document, workstreams=workstreams_document)


def _ensure_program_communication_plan_scaffold(
    program_document: dict[str, Any],
    *,
    edition_document: dict[str, Any],
    edition_name: str,
    paths: OnboardPaths,
) -> bool:
    if "communication_plan" in program_document:
        return False

    program_id = _optional_str(program_document.get("id")) or paths.program_dir.name
    matching_editions = _matching_program_edition_names(program_id, editions_root=paths.editions_root)
    if matching_editions != (edition_name,):
        return False

    author_defaults = _mapping(program_document.get("author_defaults"))
    owner_alias = _optional_str(author_defaults.get("email"))
    program_document["communication_plan"] = [
        _compact(
            {
                "edition": edition_name,
                "audience": _optional_str(program_document.get("name")),
                "channel": "email",
                "cadence": _optional_str(edition_document.get("cadence")),
                "owner": _alias_from_email(owner_alias) if owner_alias else None,
            }
        )
    ]
    return True


def _matching_program_edition_names(program_id: str, *, editions_root: Path) -> tuple[str, ...]:
    matching_editions: list[str] = []
    for edition_path in sorted(editions_root.glob("*.yaml"), key=lambda item: item.name.lower()):
        raw_edition = _read_yaml(edition_path, required=False)
        if (_optional_str(raw_edition.get("program_id")) or "") != program_id:
            continue
        matching_editions.append(edition_path.stem)
    return tuple(matching_editions)


def _ensure_program_charter_scaffold(program_document: dict[str, Any]) -> bool:
    if "charter" not in program_document:
        program_document["charter"] = {
            "scope_statement": None,
            "success_criteria": [],
            "assumptions": [],
            "constraints": [],
            "stakeholder_register": [],
        }
        return True

    charter = program_document.get("charter")
    if not isinstance(charter, dict):
        return False

    changed = False
    if "scope_statement" not in charter:
        charter["scope_statement"] = None
        changed = True
    for key in ("success_criteria", "assumptions", "constraints", "stakeholder_register"):
        if key not in charter:
            charter[key] = []
            changed = True
    return changed


def _ensure_workstream_raci_scaffolds(workstreams_document: dict[str, Any]) -> bool:
    workstreams = workstreams_document.get("workstreams")
    if not isinstance(workstreams, list):
        return False

    changed = False
    for workstream in workstreams:
        if not isinstance(workstream, dict):
            continue
        if "raci" not in workstream:
            workstream["raci"] = {
                "responsible": [],
                "accountable": None,
                "consulted": [],
                "informed": [],
            }
            changed = True
            continue

        raci = workstream.get("raci")
        if not isinstance(raci, dict):
            continue

        for key in ("responsible", "consulted", "informed"):
            if key not in raci:
                raci[key] = []
                changed = True
        if "accountable" not in raci:
            raci["accountable"] = None
            changed = True
    return changed


def _ensure_milestones_scaffold(paths: OnboardPaths) -> None:
    path = paths.program_dir / "milestones.yaml"
    if path.exists():
        return
    _write_yaml(path, {"schema_version": "1.0", "milestones": []})


def _ensure_risk_register_scaffold(program_id: str, paths: OnboardPaths) -> None:
    path = paths.program_dir / "risk_register.yaml"
    if path.exists():
        return
    save_risk_register(program_id, (), programs_root=paths.programs_root)


def _ensure_escalation_rules_scaffold(paths: OnboardPaths) -> None:
    path = paths.program_dir / "escalation_rules.yaml"
    if path.exists():
        return
    _write_yaml(path, build_default_escalation_rules_document())


def _ensure_decisions_scaffold(program_id: str, paths: OnboardPaths) -> None:
    path = paths.program_dir / "decisions.yaml"
    if path.exists():
        return
    save_decisions(program_id, (), programs_root=paths.programs_root)


def _ensure_program_store_scaffolds(program_id: str, paths: OnboardPaths) -> None:
    _ensure_milestones_scaffold(paths)
    _ensure_risk_register_scaffold(program_id, paths)
    _ensure_escalation_rules_scaffold(paths)
    _ensure_decisions_scaffold(program_id, paths)


def _ensure_dependencies_migration(program_id: str, paths: OnboardPaths) -> None:
    path = paths.program_dir / "dependencies.yaml"
    if path.exists():
        return
    dependencies = project_dependencies(
        load_program_facts(
            program_id,
            db_root=paths.programs_root.parent,
            programs_root=paths.programs_root,
            fact_types=("dependency.link",),
        )
    )
    if not dependencies:
        return
    _write_yaml(
        path,
        {
            "schema_version": "1.0",
            "dependencies": [_dependency_to_record(dependency) for dependency in dependencies],
        },
    )


def _ensure_assumption_seed(program_id: str, program_document: dict[str, Any], paths: OnboardPaths) -> None:
    seeded_texts = _charter_assumption_texts(program_document)
    if not seeded_texts:
        return

    existing_entries = list(
        project_assumptions(
            load_program_facts(
                program_id,
                db_root=paths.programs_root.parent,
                programs_root=paths.programs_root,
                fact_types=("assumption.entry",),
            )
        )
    )
    existing_texts = {_normalize_seed_text(entry.text) for entry in existing_entries}
    identified_date = datetime.now(timezone.utc).date()
    added = False
    for text in seeded_texts:
        normalized = _normalize_seed_text(text)
        if normalized in existing_texts:
            continue
        existing_entries.append(
            Assumption(
                id=str(uuid4()),
                program_id=program_id,
                text=text,
                validation_method=None,
                validation_due=None,
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias=None,
                identified_date=identified_date,
                entity_refs=(),
            )
        )
        existing_texts.add(normalized)
        added = True

    if added or not (paths.program_dir / "assumptions.yaml").exists():
        save_assumptions(program_id, tuple(existing_entries), programs_root=paths.programs_root)


def _charter_assumption_texts(program_document: dict[str, Any]) -> tuple[str, ...]:
    charter = program_document.get("charter")
    if not isinstance(charter, dict):
        return ()
    raw_values = charter.get("assumptions")
    if not isinstance(raw_values, list):
        return ()
    return _dedupe_preserve_order(
        tuple(value.strip() for value in raw_values if isinstance(value, str) and value.strip())
    )


def _normalize_seed_text(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _dependency_to_record(dependency: Dependency) -> dict[str, Any]:
    return _compact(
        {
            "id": dependency.id,
            "from_program_id": dependency.from_program_id,
            "from_workstream_id": dependency.from_workstream_id,
            "from_item_id": dependency.from_item_id,
            "from_milestone_id": dependency.from_milestone_id,
            "to_program_id": dependency.to_program_id,
            "to_workstream_id": dependency.to_workstream_id,
            "to_item_id": dependency.to_item_id,
            "to_milestone_id": dependency.to_milestone_id,
            "dependency_type": dependency.dependency_type.value,
            "risk_if_broken": dependency.risk_if_broken,
            "mitigation": dependency.mitigation,
            "status": dependency.status.value,
            "owner_alias": dependency.owner_alias,
        }
    )


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items() if item is not None and item != ()}
    if isinstance(value, list):
        return [_compact(item) for item in value if item is not None and item != ()]
    return value


def _dedupe_preserve_order(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(normalized)
    return tuple(ordered)


def _validate_edition_name(edition_name: str) -> None:
    _validate_identifier(edition_name, "Edition name")


def _validate_identifier(value: str, label: str) -> None:
    if not _EDITION_NAME_PATTERN.fullmatch(value):
        raise typer.BadParameter(f"{label} must use lowercase letters, numbers, and underscores only.")


def _prompt_required(prompt_text: str, default: str | None = None) -> str:
    while True:
        value = _prompt(prompt_text, default=default).strip()
        if value:
            return value
        typer.echo("A value is required.")


def _prompt_optional(prompt_text: str, default: str | None = None) -> str | None:
    if default is None:
        value = typer.prompt(prompt_text, default="", show_default=False).strip()
        return value or None
    value = _prompt(prompt_text, default=default).strip()
    return value or None


def _prompt(prompt_text: str, default: str | None = None) -> str:
    if default is None:
        return typer.prompt(prompt_text)
    return typer.prompt(prompt_text, default=default, show_default=True)


def _prompt_choice(prompt_text: str, choices: tuple[str, ...], default: str) -> str:
    normalized_choices = {choice.lower(): choice for choice in choices}
    while True:
        value = _prompt(prompt_text, default=default).strip().lower()
        if value in normalized_choices:
            return normalized_choices[value]
        typer.echo(f"Enter one of: {', '.join(choices)}")


def _prompt_supported_archetype(default_edition_type: str | None = None) -> str:
    if default_edition_type == "focused":
        default_choice = "F"
    elif default_edition_type == "deck":
        default_choice = "D"
    elif default_edition_type == "condensed":
        default_choice = "C"
    elif default_edition_type == "narrative":
        default_choice = "B"
    else:
        default_choice = "A"
    while True:
        choice = typer.prompt(
            "Archetype [A=scorecard-driven/detailed, B=narrative, C=daily-digest/condensed, D=deck, F=focused]",
            default=default_choice,
            show_default=True,
        ).strip().upper()
        if choice == "A":
            return "detailed"
        if choice == "B":
            return "narrative"
        if choice == "C":
            return "condensed"
        if choice == "D":
            return "deck"
        if choice == "F":
            return "focused"
        typer.echo("Enter A, B, C, D, or F.")


def _prompt_csv(prompt_text: str, default: tuple[str, ...] = (), minimum: int = 0) -> tuple[str, ...]:
    default_text = ", ".join(default)
    while True:
        raw_value = _prompt(prompt_text, default=default_text if default else None).strip()
        values = tuple(part.strip() for part in raw_value.split(",") if part.strip())
        if len(values) >= minimum:
            return values
        typer.echo(f"Enter at least {minimum} value(s).")


def _prompt_int(prompt_text: str, default: int, minimum: int, maximum: int) -> int:
    while True:
        raw_value = typer.prompt(prompt_text, default=str(default), show_default=True).strip()
        try:
            parsed = int(raw_value)
        except ValueError:
            typer.echo("Enter a whole number.")
            continue
        if parsed < minimum or parsed > maximum:
            typer.echo(f"Enter a value between {minimum} and {maximum}.")
            continue
        return parsed


def _extract_newsletter_title(title: str) -> str:
    suffix = " | Issue {issue_number} | {date}"
    return title[: -len(suffix)] if title.endswith(suffix) else title


def _default_program_id(program_name: str, edition_name: str) -> str:
    candidate = _make_identifier(program_name)
    if candidate:
        return candidate
    fallback = _strip_edition_suffix(edition_name)
    candidate = _make_identifier(fallback)
    return candidate or edition_name


def _strip_edition_suffix(edition_name: str) -> str:
    for suffix in ("_lt_deck", "_weekly", "_daily", "_biweekly", "_monthly", "_deck", "_newsletter"):
        if edition_name.endswith(suffix) and len(edition_name) > len(suffix):
            return edition_name[: -len(suffix)]
    return edition_name


def _make_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _alias_from_email(email: str) -> str:
    local_part = email.split("@", 1)[0]
    return _make_identifier(local_part)


def _default_altitude_for_edition_type(edition_type: str) -> str:
    if edition_type == "condensed":
        return "street"
    if edition_type == "narrative":
        return "escalation"
    if edition_type == "deck":
        return "satellite"
    return "helicopter"


def _resolve_onboard_paths(*, edition_name: str, program_id: str, reports_root: Path) -> OnboardPaths:
    repo_root = reports_root.parent
    programs_root = repo_root / "programs"
    program_dir = programs_root / program_id
    editions_root = program_dir / "editions"
    return OnboardPaths(
        repo_root=repo_root,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
        edition_path=editions_root / f"{edition_name}.yaml",
        program_dir=program_dir,
        knowledge_dir=program_dir / "knowledge",
    )


def _resolve_existing_onboard_paths(*, edition_name: str, reports_root: Path) -> OnboardPaths:
    repo_root = reports_root.parent
    programs_root = repo_root / "programs"
    resolved_paths = resolve_edition_paths(
        edition_name,
        programs_root=programs_root,
    )
    if resolved_paths is None:
        legacy_report_dir = reports_root / edition_name
        if legacy_report_dir.exists():
            raise typer.BadParameter(
                f"Edition '{edition_name}' exists only at {legacy_report_dir}. Onboard update now targets V2 editions under programs/<program>/editions/; migrate this edition before using --update."
            )
        raise typer.BadParameter(f"Edition '{edition_name}' does not exist under programs/<program>/editions/.")
    return OnboardPaths(
        repo_root=repo_root,
        reports_root=reports_root,
        editions_root=resolved_paths.program_dir / "editions",
        programs_root=programs_root,
        edition_path=resolved_paths.edition_path,
        program_dir=resolved_paths.program_dir,
        knowledge_dir=resolved_paths.knowledge_dir,
    )


def _load_existing_documents(paths: OnboardPaths) -> OnboardDocuments:
    return OnboardDocuments(
        edition=_read_yaml(paths.edition_path),
        program=_read_yaml(paths.program_dir / "program.yaml"),
        workstreams=_read_yaml(paths.program_dir / "workstreams.yaml", required=False),
        scorecards=_read_yaml(paths.program_dir / "scorecards.yaml"),
        editorial_rules=_read_yaml(paths.program_dir / "editorial_rules.yaml"),
        review=_read_yaml(paths.program_dir / "review.yaml"),
        people_directory=_read_yaml(paths.knowledge_dir / "people_directory.yaml", required=False),
        teams=_read_yaml(paths.knowledge_dir / "teams.yaml", required=False),
        products=_read_yaml(paths.knowledge_dir / "products.yaml", required=False),
        golden_queries=_read_yaml(paths.knowledge_dir / "golden_queries.yaml", required=False),
    )


def _read_yaml(path: Path, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise typer.BadParameter(f"Missing required onboarding file: {path}")
        return {}
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document, dict):
        raise typer.BadParameter(f"Expected a mapping in {path}")
    return document


def _merge_existing_documents(
    existing_documents: OnboardDocuments,
    generated_documents: OnboardDocuments,
    edition_name: str,
    merge_factual: bool = True,
) -> OnboardDocuments:
    return OnboardDocuments(
        edition=_merge_edition_document(existing_documents.edition, generated_documents.edition, edition_name),
        program=_merge_program_document(existing_documents.program, generated_documents.program),
        workstreams=_merge_workstreams_document(existing_documents.workstreams, generated_documents.workstreams),
        scorecards=_merge_scorecards_document(existing_documents.scorecards, generated_documents.scorecards),
        editorial_rules=_merge_editorial_rules_document(existing_documents.editorial_rules, generated_documents.editorial_rules),
        review=_merge_review_document(existing_documents.review, generated_documents.review),
        people_directory=(
            _merge_people_directory_document(existing_documents.people_directory, generated_documents.people_directory)
            if merge_factual
            else generated_documents.people_directory
        ),
        teams=(
            _merge_teams_document(existing_documents.teams, generated_documents.teams)
            if merge_factual
            else generated_documents.teams
        ),
        products=existing_documents.products if existing_documents.products else generated_documents.products,
        golden_queries=existing_documents.golden_queries if existing_documents.golden_queries else generated_documents.golden_queries,
    )


def _merge_edition_document(existing: dict[str, Any], generated: dict[str, Any], edition_name: str) -> dict[str, Any]:
    merged = _merge_owned_section(
        existing,
        generated,
        {"schema_version", "id", "program_id", "name", "type", "altitude", "cadence", "send_day", "send_time_local", "timezone"},
    )
    merged["id"] = edition_name
    return _compact(merged)


def _merge_program_document(existing: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(existing)
    merged["schema_version"] = generated.get("schema_version", "3.0")
    for key in (
        "id",
        "name",
        "objective",
        "mission",
        "current_phase",
        "pillars",
        "glossary",
        "recurring_themes",
        "key_dependencies",
        "author_defaults",
        "ado",
    ):
        if key in generated:
            merged[key] = deepcopy(generated[key])
    merged["people"] = _merge_people_records(existing.get("people", []), generated.get("people", []))
    merged["leadership_readers"] = _merge_named_records(
        existing_records=existing.get("leadership_readers", []),
        generated_records=generated.get("leadership_readers", []),
        key_name="name",
        owned_keys={"name", "role", "cares_about", "prefers", "pet_peeves"},
    )
    if "writing_style" in existing or "writing_style" in generated:
        merged["writing_style"] = _merge_owned_section(
            _mapping(existing.get("writing_style")),
            _mapping(generated.get("writing_style")),
            {"voice", "structure", "risk_framing", "preferred_patterns"},
        )
    if "tone_calibration" in existing or "tone_calibration" in generated:
        merged["tone_calibration"] = _merge_owned_section(
            _mapping(existing.get("tone_calibration")),
            _mapping(generated.get("tone_calibration")),
            {"overall", "per_theme_override"},
        )
    if "distribution_defaults" not in merged:
        merged["distribution_defaults"] = deepcopy(generated.get("distribution_defaults", {}))
    if "ai" not in merged:
        merged["ai"] = deepcopy(generated.get("ai", {}))
    if "kusto" not in merged:
        merged["kusto"] = deepcopy(generated.get("kusto", {}))
    if "m365" not in merged:
        merged["m365"] = deepcopy(generated.get("m365", {}))
    if "logging" not in merged:
        merged["logging"] = deepcopy(generated.get("logging", {}))
    return _compact(merged)


def _merge_workstreams_document(existing: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(existing)
    merged["schema_version"] = generated.get("schema_version", "2.0")
    merged["workstreams"] = _merge_named_records(
        existing_records=existing.get("workstreams", []),
        generated_records=generated.get("workstreams", []),
        key_name="id",
        owned_keys={
            "id",
            "name",
            "aliases",
            "area_paths",
            "dri_email",
            "alternate_owner",
            "description",
            "why_it_matters",
            "history_summary",
            "leadership_sensitivity",
            "current_blocker",
        },
    )
    merged["workstream_owners"] = _merge_named_records(
        existing_records=existing.get("workstream_owners", []),
        generated_records=generated.get("workstream_owners", []),
        key_name="name",
        owned_keys={"name", "areas", "style_note", "timezone", "alternate"},
    )
    return _compact(merged)


def _merge_scorecards_document(existing: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(existing)
    merged["schema_version"] = generated.get("schema_version", "2.0")
    merged["scorecards"] = _merge_named_records(
        existing_records=existing.get("scorecards", []),
        generated_records=generated.get("scorecards", []),
        key_name="name",
        owned_keys={"name", "dimensions"},
        child_list_key="dimensions",
        child_key_name="name",
        child_owned_keys={"name", "description", "ado_filter", "workstream_id"},
    )
    return _compact(merged)


def _merge_people_directory_document(existing: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(existing)
    merged["schema_version"] = generated.get("schema_version", "1.0")
    if "sensitivity" not in merged:
        merged["sensitivity"] = generated.get("sensitivity", "internal")
    merged["people"] = _merge_named_records(
        existing_records=existing.get("people", []),
        generated_records=generated.get("people", []),
        key_name="alias",
        owned_keys={"alias", "email", "display_name", "team_ids", "org_chain"},
    )
    return _compact(merged)


def _merge_teams_document(existing: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(existing)
    merged["schema_version"] = generated.get("schema_version", "1.0")
    merged["teams"] = _merge_named_records(
        existing_records=existing.get("teams", []),
        generated_records=generated.get("teams", []),
        key_name="id",
        owned_keys={"id", "name", "area_paths", "programs"},
    )
    return _compact(merged)


def _merge_editorial_rules_document(existing: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(existing)
    merged["schema_version"] = generated["schema_version"]
    merged["banned_phrases"] = deepcopy(generated.get("banned_phrases", []))
    if "stale_warn_days" not in merged:
        merged["stale_warn_days"] = generated.get("stale_warn_days", 14)
    if "stale_block_days" not in merged:
        merged["stale_block_days"] = generated.get("stale_block_days", 30)
    if "banned_openings" not in merged:
        merged["banned_openings"] = deepcopy(generated.get("banned_openings", []))
    if "verbosity" not in merged:
        merged["verbosity"] = deepcopy(generated.get("verbosity", {}))
    return _compact(merged)


def _merge_review_document(existing: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(existing)
    merged["reviewers"] = _merge_named_records(
        existing_records=existing.get("reviewers", []),
        generated_records=generated.get("reviewers", []),
        key_name="name",
        owned_keys={"name", "sections"},
    )
    merged["required"] = generated.get("required", False)
    return _compact(merged)


def _merge_named_records(
    existing_records: Any,
    generated_records: Any,
    key_name: str,
    owned_keys: set[str],
    child_list_key: str | None = None,
    child_key_name: str | None = None,
    child_owned_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    existing_by_key = {
        str(record.get(key_name)): record
        for record in existing_records
        if isinstance(record, dict) and isinstance(record.get(key_name), str)
    }
    merged_records: list[dict[str, Any]] = []
    for record in generated_records:
        if not isinstance(record, dict):
            continue
        key_value = record.get(key_name)
        existing_record = existing_by_key.get(str(key_value), {}) if isinstance(key_value, str) else {}
        merged_record = _merge_owned_section(_mapping(existing_record), record, owned_keys)
        if child_list_key is not None and child_key_name is not None and child_owned_keys is not None:
            merged_record[child_list_key] = _merge_named_records(
                existing_records=_mapping(existing_record).get(child_list_key, []),
                generated_records=record.get(child_list_key, []),
                key_name=child_key_name,
                owned_keys=child_owned_keys,
            )
        merged_records.append(_compact(merged_record))
    return merged_records


def _merge_people_records(existing_people: Any, generated_people: Any) -> list[dict[str, Any]]:
    existing_by_email = {
        str(person.get("email")): person
        for person in existing_people
        if isinstance(person, dict) and isinstance(person.get("email"), str)
    }
    merged_people: list[dict[str, Any]] = []
    for person in generated_people:
        if not isinstance(person, dict) or not isinstance(person.get("email"), str):
            continue
        email = person["email"]
        merged_person = deepcopy(existing_by_email.get(email, {}))
        merged_person["email"] = email
        if person.get("display_name"):
            merged_person["display_name"] = person["display_name"]
        if not merged_person.get("role") and person.get("role"):
            merged_person["role"] = person["role"]
        elif email == person.get("email") and person.get("role") == "Author":
            merged_person["role"] = person["role"]
        merged_person["workstreams"] = deepcopy(person.get("workstreams", []))
        merged_people.append(_compact(merged_person))
    return merged_people


def _merge_owned_section(existing: dict[str, Any], generated: dict[str, Any], owned_keys: set[str]) -> dict[str, Any]:
    merged = deepcopy(existing)
    for key in owned_keys:
        merged.pop(key, None)
    merged.update(deepcopy(generated))
    return merged


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _resolve_onboard_assistant(
    ai_enabled: bool,
    *,
    edition_name: str,
    reports_root: Path,
) -> OnboardAssistant | None:
    if not ai_enabled:
        return None
    if get_ai_mode() == AIMode.DISABLED:
        return None
    try:
        assistant = _build_default_onboard_assistant(
            trace_context=_build_onboard_trace_context(
                edition_name=edition_name,
                reports_root=reports_root,
            )
        )
    except OnboardAssistantError as error:
        typer.echo(f"AI suggestions unavailable: {error}")
        return None
    typer.echo("AI suggestions enabled for Stage 2, 3, and 5 where available.")
    return assistant


def _build_onboard_assistant(*, trace_context: AITraceContext | None = None) -> OnboardAssistant:
    # D-20: bind the trace context to the process-level ContextVar so any
    # nested helper (rate-limit scope, cost-guard construction, trace-file
    # write path) that doesn't take an explicit `trace_context=` arg still
    # picks it up. The explicit kwarg below still wins, so this is
    # behavior-preserving.
    with use_trace_context(trace_context):
        return OnboardAssistant.from_environment(trace_context=trace_context)


def _build_onboard_trace_context(*, edition_name: str, reports_root: Path) -> AITraceContext:
    current_time = datetime.now(timezone.utc)
    return AITraceContext(
        edition=edition_name,
        run_id=f"{edition_name}:onboard:{current_time.strftime('%Y%m%dT%H%M%SZ')}",
        caller="src.commands.onboard._resolve_onboard_assistant",
        metadata={
            "edition_name": edition_name,
            "task_type": "onboarding_ai_assistance",
            "run_budget_usd": 0.5,
        },
    )


def _build_default_onboard_assistant(*, trace_context: AITraceContext) -> OnboardAssistant:
    if "trace_context" in inspect.signature(_build_onboard_assistant).parameters:
        return _build_onboard_assistant(trace_context=trace_context)
    return _build_onboard_assistant()


def _write_documents(
    paths: OnboardPaths,
    edition_name: str,
    documents: OnboardDocuments,
    *,
    write_factual: bool,
) -> None:
    paths.editions_root.mkdir(parents=True, exist_ok=True)
    paths.programs_root.mkdir(parents=True, exist_ok=True)
    paths.program_dir.mkdir(parents=True, exist_ok=True)
    paths.knowledge_dir.mkdir(parents=True, exist_ok=True)
    # Canonical program-directory layout (specs/declutter.md §5, Phase 2-A):
    # runtime/ holds platform-internal T-3 files, docs/ holds one-time human
    # documents (T-8), summaries/ holds the active rolling summary store.
    # Onboarding these here means a program created mid-Phase-1-migration is
    # born canonical and needs no runtime migration (closes R-3).
    for child in (
        "journal", "trajectories", "summaries", "overrides", "narratives",
        "runtime", "docs",
    ):
        (paths.program_dir / child).mkdir(parents=True, exist_ok=True)
    (paths.program_dir / "archive" / edition_name).mkdir(parents=True, exist_ok=True)

    _write_yaml(paths.edition_path, documents.edition)
    _write_yaml(paths.program_dir / "program.yaml", documents.program)
    save_workstreams_document(program_id=paths.program_dir.name, document=documents.workstreams, programs_root=paths.programs_root)
    _write_yaml(paths.program_dir / "scorecards.yaml", documents.scorecards)
    _write_yaml(paths.program_dir / "editorial_rules.yaml", documents.editorial_rules)
    _write_yaml(paths.program_dir / "review.yaml", documents.review)
    if write_factual:
        _write_yaml(paths.knowledge_dir / "people_directory.yaml", documents.people_directory)
        _write_yaml(paths.knowledge_dir / "teams.yaml", documents.teams)
    _write_yaml(paths.knowledge_dir / "products.yaml", documents.products)
    _write_yaml(paths.knowledge_dir / "golden_queries.yaml", documents.golden_queries)


def _finalize_onboarding(
    edition_name: str,
    paths: OnboardPaths,
    documents: OnboardDocuments,
    draft: OnboardDraft,
    register_shared: bool = False,
) -> OnboardResult:
    program_id = draft.identity.program_id if draft.identity is not None else _default_program_id("", edition_name)
    use_shared_registry = register_shared or shared_registry_is_active(paths.programs_root)
    _write_documents(paths, edition_name, documents, write_factual=not use_shared_registry)
    shared_registry_transaction_id: str | None = None
    if use_shared_registry:
        identity = capture_operator_identity("vertex-onboard-register-shared")
        if not identity.principal:
            raise typer.BadParameter("Could not resolve an authenticated OS/service principal for shared registry registration.")
        shared_result = register_onboarding_facts(
            program_id=program_id,
            people=_onboarding_people(draft),
            groups=_onboarding_program_groups(draft),
            programs_root=paths.programs_root,
            actor=identity.principal,
            reason=f"vertex onboard {'--register-shared ' if register_shared else ''}{edition_name}",
            source_ref=f"edition:{edition_name}",
            apply=True,
        )
        shared_registry_transaction_id = shared_result.transaction_id
    _ensure_program_store_scaffolds(program_id, paths)
    validation = _run_onboard_validation(edition_name=edition_name, reports_root=paths.reports_root)
    readme_path = _write_readme(paths.program_dir, edition_name=edition_name, draft=draft, validation=validation)
    return _build_onboard_result(
        edition_name=edition_name,
        program_id=program_id,
        paths=paths,
        readme_path=readme_path,
        validation=validation,
        shared_registry_transaction_id=shared_registry_transaction_id,
    )


def _write_readme(
    program_dir: Path,
    edition_name: str,
    draft: OnboardDraft,
    validation: OnboardValidationResult,
) -> Path:
    readme_path = program_dir / f"README-{edition_name}.md"
    readme_path.write_text(
        _build_readme_markdown(edition_name=edition_name, draft=draft, validation=validation),
        encoding="utf-8",
    )
    return readme_path


def _build_readme_markdown(
    edition_name: str,
    draft: OnboardDraft,
    validation: OnboardValidationResult,
) -> str:
    identity = draft.identity
    ado = draft.ado
    structure = draft.structure
    people = draft.people
    style = draft.style

    lines = [
        f"# {edition_name}",
        "",
        "This V2 Vertex scaffold was generated by `vertex onboard`.",
        "",
        "## Current Setup",
    ]

    if identity is not None:
        lines.extend(
            [
                f"- Program id: {identity.program_id}",
                f"- Program name: {identity.program_name}",
                f"- Primary edition title: {identity.newsletter_title}",
                f"- Cadence: {identity.cadence}",
                f"- Author: {identity.author_display_name} <{identity.author_email}>",
                f"- Objective: {identity.objective}",
            ]
        )
        if identity.current_phase:
            lines.append(f"- Current phase: {identity.current_phase}")

    if structure is not None:
        lines.append(f"- Archetype: {structure.edition_type}")
        lines.append(f"- Altitude: {_default_altitude_for_edition_type(structure.edition_type)}")
        lines.append(f"- Scorecards configured: {len(structure.scorecards)}")
        for scorecard in structure.scorecards:
            dimension_names = ", ".join(dimension.name for dimension in scorecard.dimensions) or "None"
            lines.append(f"- Scorecard {scorecard.name}: {dimension_names}")

    if ado is not None:
        lines.extend(
            [
                f"- ADO scope: {ado.organization}/{ado.project}",
                f"- Area paths: {_format_csv(ado.area_paths)}",
                f"- Work item types: {_format_csv(ado.work_item_types)}",
                f"- Excluded states: {_format_csv(ado.excluded_states)}",
            ]
        )

    if people is not None:
        lines.append(f"- Workstreams configured: {len(people.workstreams)}")
        for workstream in people.workstreams:
            lines.append(f"- Workstream {workstream.name}: DRI {workstream.dri_email}")
        lines.append(f"- Reviewers seeded: {len(people.reviewers)}")
        lines.append(f"- Leadership readers seeded: {len(people.leadership_readers)}")
        lines.append(f"- Workstream owner profiles seeded: {len(people.workstream_owners)}")

    if style is not None:
        lines.append(f"- Glossary entries: {len(style.glossary)}")
        lines.append(f"- Additional banned phrases: {len(style.extra_banned_phrases)}")
        if style.voice:
            lines.append(f"- Voice guidance: {style.voice}")
        if style.structure:
            lines.append(f"- Structure guidance: {style.structure}")
        if style.recurring_themes:
            lines.append(f"- Recurring themes: {_format_csv(style.recurring_themes)}")
        if style.tone_overall:
            lines.append(f"- Overall tone calibration: {style.tone_overall}")

    lines.extend(
        [
            "",
            "## Files",
            f"- `editions/{edition_name}.yaml` declares the edition id, cadence, archetype, and altitude.",
            "- `program.yaml` stores program metadata, charter, communication plan, defaults, leadership readers, and writing guidance.",
            "- `workstreams.yaml` stores workstream definitions and workstream-owner profiles.",
            "- `scorecards.yaml` maps scorecard dimensions to workstreams.",
            "- `knowledge/people_directory.yaml`, `knowledge/teams.yaml`, `knowledge/products.yaml`, and `knowledge/golden_queries.yaml` seed the V2 knowledge layer.",
            "- `editorial_rules.yaml` controls freshness thresholds, banned phrases, banned openings, and verbosity limits.",
            "- `review.yaml` controls reviewer assignments and whether review approval is required.",
            "",
            "## How To Modify",
            f"- Edit `editions/{edition_name}.yaml` when cadence, archetype, or edition-level overrides change.",
            "- Edit `program.yaml` when the program objective, charter, communication plan, ADO defaults, audience, or style guidance changes.",
            "- Edit `workstreams.yaml` and `scorecards.yaml` when workstream ownership or scorecard mapping changes.",
            "- Edit the `knowledge/` YAML files when people, teams, products, or saved queries change.",
            "- Edit `editorial_rules.yaml` when editorial bans or freshness thresholds change.",
            "- Edit `review.yaml` when the review workflow changes.",
            "",
            "## Validation",
            f"- Onboarding automatically ran `vertex draft --edition {edition_name} --dry-run`.",
        ]
    )

    if validation.message is not None:
        lines.append(f"- Validation status: {validation.message}")
    elif validation.issue_number is not None and validation.exit_code is not None:
        lines.append(
            f"- Validation status: issue {validation.issue_number} {_describe_validation_exit_code(validation.exit_code)} (exit code {validation.exit_code})."
        )
        if validation.html_path is not None:
            lines.append(f"- Draft HTML: {validation.html_path}")
        if validation.md_path is not None:
            lines.append(f"- Draft Markdown: {validation.md_path}")
        if validation.manifest_path is not None:
            lines.append(f"- Draft manifest: {validation.manifest_path}")

    lines.append(f"- Re-run `vertex draft --edition {edition_name} --dry-run` after any config edits.")
    lines.append("")
    return "\n".join(lines)


def _run_onboard_validation(edition_name: str, reports_root: Path) -> OnboardValidationResult:
    from src.commands.report import generate_report_draft

    try:
        artifacts = generate_report_draft(
            edition_name=edition_name,
            reports_root=reports_root,
            open_browser=True,
        )
    except (VertexError, OSError) as error:
        return OnboardValidationResult(
            message=f"Automatic validation dry-run could not complete: {error}",
        )

    return OnboardValidationResult(
        issue_number=artifacts.issue_number,
        exit_code=artifacts.exit_code,
        html_path=artifacts.html_path,
        md_path=artifacts.md_path,
        manifest_path=artifacts.manifest_path,
    )


def _format_csv(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "None"


def _describe_validation_exit_code(exit_code: int) -> str:
    if exit_code == 0:
        return "passed"
    if exit_code == 2:
        return "completed with warnings"
    if exit_code == 3:
        return "is blocked"
    return "finished"


def _print_validation_summary(validation: OnboardValidationResult) -> None:
    if validation.message is not None:
        typer.echo(validation.message)
        return

    if validation.issue_number is None or validation.exit_code is None:
        return

    typer.echo(
        f"Validation dry-run issue {validation.issue_number} {_describe_validation_exit_code(validation.exit_code)} (exit code {validation.exit_code})."
    )
    if validation.html_path is not None:
        typer.echo(f"Draft HTML: {validation.html_path}")
    if validation.md_path is not None:
        typer.echo(f"Draft Markdown: {validation.md_path}")
    if validation.manifest_path is not None:
        typer.echo(f"Draft manifest: {validation.manifest_path}")
    if validation.exit_code == 3 and validation.html_path is None:
        typer.echo("Validation blockers prevented draft artifacts from being written.")


def _build_onboard_result(
    edition_name: str,
    program_id: str,
    paths: OnboardPaths,
    readme_path: Path | None = None,
    validation: OnboardValidationResult | None = None,
    shared_registry_transaction_id: str | None = None,
) -> OnboardResult:
    return OnboardResult(
        edition_name=edition_name,
        program_id=program_id,
        edition_path=paths.edition_path,
        program_dir=paths.program_dir,
        program_path=paths.program_dir / "program.yaml",
        workstreams_path=paths.program_dir / "workstreams.yaml",
        scorecards_path=paths.program_dir / "scorecards.yaml",
        editorial_rules_path=paths.program_dir / "editorial_rules.yaml",
        review_path=paths.program_dir / "review.yaml",
        readme_path=readme_path,
        validation=validation,
        shared_registry_transaction_id=shared_registry_transaction_id,
    )

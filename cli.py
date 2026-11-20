from __future__ import annotations

import logging
import os
import sys
import uuid
from copy import copy
from enum import Enum, EnumMeta
from functools import partial
from inspect import getattr_static
from pathlib import Path

try:
    if os.environ.get("VERTEX_NO_DOTENV") != "1":
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

import click
import typer
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption

from src.ai.ai_mode import AIMode, set_ai_mode
from src.ai.intent_router import IntentRouterError, _resolve_ask_edition
from src.ai.tiered_router import use_invocation_sink
from src.commands.actions import app as actions_app
from src.commands.ado import app as ado_app
from src.commands.ai_proposals import app as ai_proposals_app
from src.commands.admin_baseline import admin_baseline_command
from src.commands.admin_fact_store_flip import fact_store_flip_command
from src.commands.admin_fact_store_migrate import migrate_legacy_state_command
from src.commands.admin_legacy_cutoff import bootstrap_legacy_cutoff_command
from src.commands.admin_metrics_rollup import admin_metrics_rollup_command
from src.commands.admin_platform_proof import admin_platform_proof_command
from src.commands.admin_s7_position import admin_s7_position_command
from src.commands.admin_reconcile import reconcile_command as admin_reconcile_command
from src.commands.admin_upgrade_state import upgrade_state_command as admin_upgrade_state_command
from src.commands.archive_journals import archive_journals_command
from src.commands.archive_signing import (
    admin_archive_signing_command,
    archive_verify_command,
)
from src.commands.ask import _require_ask_scope, ask_command
from src.commands.actuate import app as actuate_app
from src.commands.auth import app as auth_app
from src.commands.assertion import app as assertion_app
from src.commands.audit import app as audit_app
from src.commands.apply_proposals import apply_proposals_command
from src.commands.assumptions import app as assumptions_app
from src.commands.claims import app as claims_app
from src.commands.cockpit import app as cockpit_app
from src.commands.decisions import app as decisions_app
from src.commands.dependencies import app as dependencies_app
from src.commands.discover import app as discover_app
from src.commands.entity_aliases import app as entity_aliases_app
from src.commands.backup import backup_command
from src.commands.backfill import backfill_command
from src.commands.bootstrap import bootstrap_command
from src.commands.bridge_status import bridge_status_command
from src.commands.brief import brief_command
from src.commands.calibration import app as calibration_app
from src.commands.catchup import build_catchup_event_builder, catchup_command
from src.commands.capture_lt_deck import capture_lt_deck_command
from src.commands.commitment import app as commitment_app
from src.commands.confirm import _preflight_confirm_workspace, confirm_command
from src.commands.context import app as context_app
from src.commands.config import app as config_app
from src.commands.connectors import app as connectors_app
from src.commands.deck_companion import deck_companion_command
from src.commands.diff import _preflight_diff_workspace, diff_command
from src.commands.context_diff import context_diff as _context_diff_cmd
from src.commands.db import app as db_app, maybe_run_scheduled_compaction, use_compaction_programs_root
from src.commands.doctor import doctor_command
from src.commands.edit import _preflight_edit_workspace, edit_command
from src.commands.editor import app as editor_app
from src.commands.escalate import escalate_command
from src.commands.evidence import evidence_command
from src.commands.enrich import enrich_command
from src.commands.facts import app as facts_app
from src.commands.freshness import _preflight_freshness_workspace, freshness_command
from src.commands.fleet import fleet_command
from src.commands.gather import gather_command
from src.commands.history import history_command
from src.commands.hypothesis import app as hypothesis_app
from src.commands.prefetch import prefetch_command
from src.commands.index import app as index_app
from src.commands.inspect import app as inspect_app
from src.commands.ingest_update import ingest_update_command
from src.commands.integration import app as integration_app
from src.commands.investigate import investigate_command
from src.commands.kb import app as kb_app
from src.commands.knowledge import app as knowledge_app
from src.commands.ledger import app as ledger_app
from src.commands.list import app as list_app
from src.commands.manifest import manifest_command
from src.commands.meeting_close import meeting_close_command
from src.commands.metric import app as metric_app
from src.commands.maturity_check import maturity_check_command
from src.commands.migrate import migrate_command
from src.commands.milestones import app as milestones_app
from src.commands.notify import notify_command
from src.commands.notifications import notifications_command
from src.commands.nudge import nudge_command
from src.commands.next import next_command
from src.commands.observation import app as observation_app
from src.commands.onboard import onboard_command
from src.commands.owner_pack import owner_pack_command
from src.commands.policy import app as policy_app
from src.commands.override import _preflight_override_workspace, override_command
from src.commands.prep import _preflight_prep_workspace, prep_command
from src.commands.privacy import privacy_app
from src.commands.observability import observability_app, alerts_app
from src.commands.decision_brief import _preflight_decision_brief_workspace, decision_brief_command
from src.commands.decision_brief_pilot import app as decision_brief_pilot_app
from src.commands.program_synthesizer_pilot import app as program_synthesizer_pilot_app
from src.commands.propose import propose_command
from src.commands.published_baseline import published_baseline_command
from src.commands.rollback import rollback_command
from src.commands.review_proposals import review_proposals_command
from src.commands.publish_gate import publish_gate_command
from src.commands.probe_ado import probe_ado
from src.commands.setup import setup_command
from src.commands.quickstart import quickstart_command
from src.commands.readiness import app as readiness_app
from src.commands.registry import app as registry_app
from src.commands.reality import app as reality_app
from src.commands.reconcile import reconcile_command
from src.commands.report import _preflight_report_workspace, apply_overrides_command, report_command
from src.commands.review_debrief import review_debrief_command
from src.commands.review_full import _preflight_review_full_workspace, review_full_command
from src.commands.review_sections import _preflight_review_sections_workspace, app as review_sections_app
from src.commands.rev import app as rev_app
from src.commands.risks import app as risks_app
from src.commands.salience import app as salience_app
from src.commands.signals import app as signals_app
from src.commands.storage import app as storage_app
from src.commands.skip_issue import skip_issue
from src.commands.status import status_command
from src.commands.stubs import register_phase_stubs
from src.commands.hints import hints_command
from src.commands.summarize import summarize_command
from src.commands.synthesize import synthesize_command
from src.commands.triage import _preflight_triage_workspace, triage_command
from src.commands.trust import trust_command, trust_bootstrap_command
from src.commands.vitality import vitality_command
from src.commands.watch import watch_command
from src.core.catchup_runner import maybe_catchup
from src.core.edition_resolver import PROGRAMS_ROOT, resolve_edition_paths
from src.core.adf_config import load_arch_data_fix
from src.core.measurement_store import TierDecisionSinkConfig, make_tier_decision_sink
from src.core.workspace_paths import is_bare_identifier


_CLICK_UNSET = getattr(click.core, "UNSET", None)
_TYPER_CHOICE = getattr(typer.main, "TyperChoice", click.Choice)
_TYPER_PATH = getattr(typer.models, "TyperPath", click.Path)


def _safe_startup_value(value: object) -> bool:
    if value is None or value is _CLICK_UNSET:
        return True
    value_type = type(value)
    if any(value_type is allowed for allowed in (str, int, float, bool, bytes, type(Path()))):
        return True
    if type(value_type) is EnumMeta and issubclass(value_type, Enum):
        return (
            getattr_static(value_type, "__str__", None) is Enum.__str__
            and (
                getattr_static(value_type, "__getattribute__", None) is object.__getattribute__
                or (
                    issubclass(value_type, str)
                    and getattr_static(value_type, "__getattribute__", None) is str.__getattribute__
                )
            )
            and getattr_static(value_type, "value", None) is getattr_static(Enum, "value")
            and getattr_static(value, "__class__", None) is getattr_static(object, "__class__")
            and type(getattr_static(value, "_name_", None)) is str
            and type(getattr_static(value, "_value_", None)) is str
        )
    if type(value) is tuple or type(value) is list:
        return all(
            type(item) is not tuple and type(item) is not list and _safe_startup_value(item)
            for item in value
        )
    return False


def _safe_startup_type(param_type: click.ParamType) -> bool:
    if any(type(param_type) is allowed for allowed in (
        click.types.StringParamType, click.types.IntParamType, click.types.FloatParamType,
        click.types.BoolParamType, click.types.UUIDParameterType,
    )):
        return True
    if (
        (type(param_type) is click.IntRange or type(param_type) is click.FloatRange)
        and isinstance(param_type, (click.IntRange, click.FloatRange))
    ):
        return all(
            bound is None or type(bound) is int or type(bound) is float
            for bound in (param_type.min, param_type.max)
        )
    if (
        (type(param_type) is click.Choice or type(param_type) is _TYPER_CHOICE)
        and isinstance(param_type, click.Choice)
    ):
        return (type(param_type.choices) is tuple or type(param_type.choices) is list) and all(
            any(type(choice) is allowed for allowed in (str, int, float, bool))
            for choice in param_type.choices
        )
    if (
        (type(param_type) is click.Path or type(param_type) is _TYPER_PATH)
        and isinstance(param_type, click.Path)
    ):
        return any(param_type.type is allowed for allowed in (None, str, bytes, Path))
    if type(param_type) is click.DateTime:
        return (type(param_type.formats) is tuple or type(param_type.formats) is list) and all(
            type(value) is str for value in param_type.formats
        )
    if type(param_type) is click.Tuple:
        return all(type(item) is not click.Tuple and _safe_startup_type(item) for item in param_type.types)
    return False


def _ask_startup_scope(edition: object, program: object) -> tuple[str, str | None] | None:
    try:
        _require_ask_scope(edition, program)
    except typer.BadParameter:
        return None
    if isinstance(program, str) and program.strip():
        return program.strip(), None
    if isinstance(edition, str):
        try:
            paths = _resolve_ask_edition(edition.strip(), programs_root=PROGRAMS_ROOT)
        except IntentRouterError:
            return None
        return paths.program_id, edition.strip()
    return None


def _parsed_startup_scope(
    parent: click.Context, name: str, command: click.Command, args: list[str],
) -> tuple[str | None, str | None] | None:
    if parent.default_map is not None or parent.token_normalize_func is not None:
        return None
    program: str | None = None
    edition: str | None = None
    while True:
        if (
            not any(type(command) is allowed for allowed in (click.Command, click.Group, TyperCommand, TyperGroup))
            or command.context_class is not click.Context
            or type(command.context_settings) is not dict
            or command.context_settings.get("default_map") is not None
            or command.context_settings.get("token_normalize_func") is not None
        ):
            return None
        for parameter in command.params:
            if (
                not any(type(parameter) is allowed for allowed in (click.Option, click.Argument, TyperOption, TyperArgument))
                or parameter.callback is not None
                or not _safe_startup_type(parameter.type)
            ):
                return None
            if parameter.name in {"program", "edition"} and (
                type(parameter.type) is not click.types.StringParamType
                or not parameter.expose_value or parameter.multiple or parameter.nargs != 1
            ):
                return None
        settings = dict(command.context_settings)
        settings.update(info_name=name, parent=parent, default_map={})
        probe = command.context_class(command, **settings)
        try:
            options, remaining, _ = command.make_parser(probe).parse_args(list(args))
        except click.UsageError:
            return None
        if options.get("help") is True:
            return None, None
        for parameter in command.params:
            value = options.get(parameter.name, _CLICK_UNSET) if parameter.name is not None else _CLICK_UNSET
            if value is None or value is _CLICK_UNSET:
                default = parameter.get_default(probe, call=False)
                if not _safe_startup_value(default):
                    return None
                if parameter.required and (default is None or default is _CLICK_UNSET):
                    return None
                if parameter.name in {"program", "edition"} and (
                    parameter.envvar is not None or probe.auto_envvar_prefix is not None
                    or (isinstance(parameter, click.Option) and parameter.prompt is not None)
                    or not (
                        default is None or default is _CLICK_UNSET
                        or (type(default) is str and default == "")
                    )
                ):
                    return None
                continue
            if not _safe_startup_value(value):
                return None
            if parameter.name not in {"program", "edition"}:
                continue
            if type(value) is not str or not is_bare_identifier(value):
                return None
            if parameter.name == "program":
                if program is not None and program != value:
                    return None
                program = value
            else:
                if edition is not None and edition != value:
                    return None
                edition = value
        if not isinstance(command, click.Group):
            return None if remaining and not probe.allow_extra_args else (program, edition)
        if command.chain:
            return None
        if not remaining:
            return (program, edition) if command.invoke_without_command else None
        name, *args = remaining
        child = command.get_command(probe, name)
        if child is None:
            return None
        parent, command = probe, child


class VertexRootGroup(TyperGroup):
    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        edition = ctx.params.get("edition")
        global_edition = ["--edition", edition] if isinstance(edition, str) and edition else []
        ctx.meta["vertex_invocation_argv"] = [*global_edition, *args]
        ctx.meta["vertex_command_argv"] = list(args)
        try:
            resolved = super().resolve_command(ctx, args)
            if resolved[0] != "ask":
                ctx.meta["vertex_startup_scope"] = (
                    _parsed_startup_scope(ctx, resolved[0], resolved[1], resolved[2])
                    if resolved[0] is not None and resolved[1] is not None else None
                )
            report = resolved[1]
            if (
                resolved[0] in {"report", "draft"}
                and type(report) is TyperCommand
                and getattr_static(report.callback, "__wrapped__", None) is report_command
                and report.context_class is click.Context
                and type(report.context_settings) is dict
                and report.context_settings.get("default_map") is None
                and report.context_settings.get("token_normalize_func") is None
                and ctx.default_map is None
                and ctx.token_normalize_func is None
                and all(
                    type(parameter) is TyperOption
                    and parameter.callback is None
                    and _safe_startup_type(parameter.type)
                    and _safe_startup_value(parameter.default)
                    for parameter in report.params
                )
            ):
                settings = dict(report.context_settings)
                settings.update(info_name=resolved[0], parent=ctx)
                probe = click.Context(report, **settings)
                try:
                    options, _, _ = report.make_parser(probe).parse_args(list(resolved[2]))
                except click.UsageError:
                    pass  # The actual invocation reports the parse error.
                else:
                    ctx.meta["vertex_startup_dry_run"] = options.get("dry_run") is True
                    ctx.meta["report_conflicting_send"] = (
                        options.get("dry_run") is True
                        and options.get("send_draft") is True
                        and options.get("help") is not True
                    )
            if resolved[0] == "context" and resolved[2] and resolved[2][0] in {
                "import-reply", "solicit", "extract", "verify-reply-origin",
            }:
                ctx.meta["offline_context_command"] = True
            elif resolved[0] == "cockpit" and (
                resolved[2][:1] == ["exceptions"] or resolved[2][:2] == ["--", "exceptions"]
            ):
                ctx.meta["offline_cockpit_exceptions"] = True
            elif resolved[0] == "admin" and resolved[2][:2] == ["metric", "history"]:
                ctx.meta["offline_metric_history_command"] = True
            elif resolved[0] == "doctor" and resolved[1] is not None and any(
                arg == "--rev-health" or arg.startswith("--rev-health=") for arg in resolved[2]
            ):
                flag_options = {
                    option
                    for parameter in resolved[1].params
                    if isinstance(parameter, click.Option) and parameter.is_flag
                    for option in (*parameter.opts, *parameter.secondary_opts)
                }
                # Normalize invalid flag assignments only for this probe; the real parser reports them.
                probe_args = [
                    arg.split("=", 1)[0] if arg.partition("=")[0] in flag_options else arg
                    for arg in resolved[2]
                ]
                probe = resolved[1].make_context(
                    "doctor", probe_args, parent=ctx, resilient_parsing=True,
                    help_option_names=[], ignore_unknown_options=True, allow_extra_args=True,
                )
                with probe:
                    ctx.meta["offline_rev_health_command"] = probe.params.get("rev_health") is True
            elif resolved[0] in {"migrate", "enrich"} and resolved[1] is not None and (
                resolved[0] == "enrich" or any(
                    arg == "--dry-run" or arg.startswith("--dry-run=") for arg in resolved[2]
                )
            ):
                command_name = "migrate" if resolved[0] == "migrate" else "enrich"
                probe_args = [
                    "--dry-run" if arg.startswith("--dry-run=") else arg
                    for arg in resolved[2]
                ]
                probe = resolved[1].make_context(
                    command_name, probe_args, parent=ctx, resilient_parsing=True,
                    help_option_names=[], ignore_unknown_options=True, allow_extra_args=True,
                )
                with probe:
                    key = "offline_migrate_dry_run" if command_name == "migrate" else "enrich_dry_run"
                    ctx.meta[key] = probe.params.get("dry_run") is True
                    if command_name == "enrich":
                        ctx.meta["enrich_json_output"] = probe.params.get("output_format") == "json"
            elif resolved[0] == "ask" and resolved[1] is not None:
                probe_command = copy(resolved[1])
                probe_command.params = [copy(parameter) for parameter in resolved[1].params]
                for parameter in probe_command.params:
                    if isinstance(parameter.type, click.Path):
                        parameter.type = click.STRING
                probe_command.params.append(click.Option(["--help"], is_flag=True))
                try:
                    probe = probe_command.make_context(
                        "ask", list(resolved[2]), parent=ctx,
                        help_option_names=[], ignore_unknown_options=True, allow_extra_args=True,
                    )
                except click.ClickException:
                    ctx.meta["ask_skip_startup"] = True
                else:
                    with probe:
                        scope = _ask_startup_scope(
                            probe.params.get("edition"), probe.params.get("program")
                        )
                        ctx.meta["ask_skip_startup"] = probe.params.get("help") is True or scope is None
                        if scope is not None:
                            ctx.meta["ask_startup_program"], ctx.meta["ask_startup_edition"] = scope
            elif resolved[0] == "nudge" and resolved[1] is not None:
                probe_command = copy(resolved[1])
                probe_command.params = [copy(parameter) for parameter in resolved[1].params]
                for parameter in probe_command.params:
                    if isinstance(parameter.type, click.Path):
                        parameter.type = click.STRING
                try:
                    probe = probe_command.make_context(
                        "nudge", list(resolved[2]), parent=ctx,
                        help_option_names=[], ignore_unknown_options=True, allow_extra_args=True,
                    )
                except click.ClickException:
                    # The actual invocation reports parse errors without preceding startup writes.
                    ctx.meta["offline_nudge_command"] = True
                else:
                    with probe:
                        ctx.meta["offline_nudge_command"] = any(
                            probe.get_parameter_source(name) is click.core.ParameterSource.COMMANDLINE
                            for name in ("approve_draft", "mark_sent")
                        )
            if (
                "vertex_startup_dry_run" not in ctx.meta
                and resolved[0] not in {"report", "draft"}
                and resolved[1] is not None
                and any(arg == "--dry-run" or arg.startswith("--dry-run=") for arg in resolved[2])
            ):
                ctx.meta["vertex_startup_dry_run"] = _probe_dry_run_flag(
                    ctx, resolved[0], resolved[1], resolved[2]
                )
            return resolved
        except click.ClickException:
            ask_group = self.commands.get("ask")
            if ask_group is None or not args:
                raise
            if args[0].startswith("-"):
                raise
            edition = ctx.params.get("edition")
            routed_args: list[str] = []
            if isinstance(edition, str) and edition.strip():
                routed_args.extend(["--edition", edition.strip()])
            routed_args.append(" ".join(args).strip())
            scope = _ask_startup_scope(edition, None)
            ctx.meta["ask_skip_startup"] = scope is None
            if scope is not None:
                ctx.meta["ask_startup_program"], ctx.meta["ask_startup_edition"] = scope
            return "ask", ask_group, routed_args


app = typer.Typer(
    help="Vertex hybrid journal automation CLI.",
    cls=VertexRootGroup,
    invoke_without_command=True,
)
admin_app = typer.Typer(help="Vertex operator and debug commands.")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    skip_issue_flag: bool = typer.Option(False, "--skip-issue", help="Record the next issue as intentionally skipped."),
    reason: str | None = typer.Option(None, "--reason", help="Required with --skip-issue."),
    no_catchup: bool = typer.Option(False, "--no-catchup", help="Skip the session-start catchup scan for this invocation."),
    edition: str = typer.Option("", "--edition", help="Default edition for natural-language routing (e.g. 'acme_weekly')."),
) -> None:
    ctx.with_resource(use_invocation_sink(None))
    if (
        ctx.invoked_subcommand == "ask" and ctx.meta.get("ask_skip_startup") is True
    ) or (
        ctx.invoked_subcommand == "cockpit" and ctx.meta.get("offline_cockpit_exceptions") is True
    ) or (
        ctx.invoked_subcommand in {"report", "draft"} and ctx.meta.get("report_conflicting_send") is True
    ):
        if skip_issue_flag:
            raise typer.BadParameter("--skip-issue cannot be combined with a subcommand.")
        return
    _set_invocation_ai_mode(sys.argv[1:])
    if ctx.invoked_subcommand is not None:
        if skip_issue_flag:
            raise typer.BadParameter("--skip-issue cannot be combined with a subcommand.")
        if ctx.invoked_subcommand in {"setup", "onboard"}:
            return
        if ctx.invoked_subcommand in {"report", "draft", "apply-overrides"}:
            _preflight_report_workspace()
        elif ctx.invoked_subcommand == "confirm":
            _preflight_confirm_workspace()
        elif ctx.invoked_subcommand == "freshness":
            _preflight_freshness_workspace()
        elif ctx.invoked_subcommand == "triage":
            _preflight_triage_workspace()
        elif ctx.invoked_subcommand == "prep":
            _preflight_prep_workspace()
        elif ctx.invoked_subcommand == "review-full":
            _preflight_review_full_workspace()
        elif ctx.invoked_subcommand == "review-sections":
            _preflight_review_sections_workspace()
        elif ctx.invoked_subcommand == "diff":
            _preflight_diff_workspace()
        elif ctx.invoked_subcommand == "edit":
            _preflight_edit_workspace()
        elif ctx.invoked_subcommand == "override":
            _preflight_override_workspace()
        elif ctx.invoked_subcommand in {"decision-brief", "decision-brief-pilot"}:
            _preflight_decision_brief_workspace()
        elif ctx.invoked_subcommand == "config" or (
            ctx.invoked_subcommand == "context" and ctx.meta.get("offline_context_command") is True
        ) or (
            ctx.invoked_subcommand == "admin" and ctx.meta.get("offline_metric_history_command") is True
        ) or (
            ctx.invoked_subcommand == "nudge" and ctx.meta.get("offline_nudge_command") is True
        ) or (
            ctx.invoked_subcommand == "doctor" and ctx.meta.get("offline_rev_health_command") is True
        ) or (
            ctx.invoked_subcommand == "migrate" and ctx.meta.get("offline_migrate_dry_run") is True
        ):
            return
        _maybe_register_tier_decision_sink(ctx)
        if (
            ctx.meta.get("vertex_startup_dry_run") is True
            or (ctx.invoked_subcommand == "enrich" and ctx.meta.get("enrich_dry_run") is True)
        ):
            return
        _maybe_run_scheduled_db_maintenance(ctx)
        _maybe_run_session_catchup(ctx, no_catchup=no_catchup)
        return
    if not skip_issue_flag:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)
    if reason is None or not reason.strip():
        raise typer.BadParameter("--reason is required with --skip-issue.")
    edition_name, issue_number, index_path = skip_issue(reason.strip())
    typer.echo(f"Skipped issue {issue_number:03d} for {edition_name}.")
    typer.echo(f"Archive index: {index_path}")
    raise typer.Exit(code=0)


def _maybe_run_session_catchup(ctx: typer.Context, *, no_catchup: bool) -> None:
    if no_catchup:
        return
    if ctx.invoked_subcommand in {None, "catchup", "watch"}:
        return
    if not _stdout_supports_interactive_catchup():
        return
    program_id = _program_for_startup(ctx)
    if program_id is None:
        return
    emit = (
        partial(typer.echo, err=True)
        if ctx.invoked_subcommand == "enrich" and ctx.meta.get("enrich_json_output") is True
        else typer.echo
    )
    maybe_catchup(
        program_id,
        programs_root=PROGRAMS_ROOT,
        emit=emit,
        event_builder=build_catchup_event_builder(program_id, programs_root=PROGRAMS_ROOT),
    )


def _maybe_register_tier_decision_sink(ctx: typer.Context) -> None:
    """ADF-W0.7: persist every AI routing decision made during this invocation.

    Registers a durable JSONL sink (``programs/<id>/runtime/tier_decisions.jsonl``)
    for this invocation. Best-effort: a sink-setup failure must never block
    a CLI command (INV-ADF-5 governs missing *provider* telemetry, not this
    registration step, so failures here are swallowed after logging).
    """
    if ctx.invoked_subcommand in {None, "admin"}:
        return
    program_id = _program_for_startup(ctx)
    if program_id is None:
        return
    try:
        if ctx.invoked_subcommand == "ask":
            selected_edition = ctx.meta.get("ask_startup_edition")
            edition_id = selected_edition if isinstance(selected_edition, str) else None
        elif "vertex_startup_scope" in ctx.meta:
            scope = ctx.meta["vertex_startup_scope"]
            edition_id = scope[1] if isinstance(scope, tuple) else None
        else:
            edition_id = _extract_option(_startup_argv(ctx), "--edition")
        if ctx.meta.get("vertex_tier_sink_registered") is True:
            return
        try:
            execution_mode = load_arch_data_fix(program_id, programs_root=PROGRAMS_ROOT).mode.value
        except Exception:
            execution_mode = "off"
        sink_config = TierDecisionSinkConfig(
            program_id=program_id,
            edition_id=edition_id,
            run_id=uuid.uuid4().hex,
            execution_mode=execution_mode,
            programs_root=PROGRAMS_ROOT,
        )
        ctx.with_resource(use_invocation_sink(make_tier_decision_sink(sink_config)))
        ctx.meta["vertex_tier_sink_registered"] = True
    except Exception:  # pragma: no cover - registration must never break the CLI
        logging.getLogger(__name__).warning("tier decision sink registration failed", exc_info=True)


def _probe_dry_run_flag(
    ctx: click.Context,
    command_name: str,
    command: click.Command,
    cmd_args: list[str],
) -> bool:
    """Safely check whether --dry-run was passed as an option flag (not an option value)."""
    probe_args = ["--dry-run" if arg.startswith("--dry-run=") else arg for arg in cmd_args]
    try:
        probe = command.make_context(
            command_name,
            probe_args,
            parent=ctx,
            resilient_parsing=True,
            help_option_names=[],
            ignore_unknown_options=True,
            allow_extra_args=True,
        )
    except click.ClickException:
        return False
    with probe:
        return probe.params.get("dry_run") is True


def _effective_startup_programs_root() -> Path:
    configured = os.environ.get("VERTEX_PROGRAMS_ROOT")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return PROGRAMS_ROOT


def _maybe_run_scheduled_db_maintenance(ctx: typer.Context) -> None:
    """Best-effort scheduled reality-store compaction for this invocation.

    A compaction failure (e.g. a stale/partial local db schema) must never
    block a CLI command, so exceptions are swallowed after logging, matching
    the same best-effort contract as ``_maybe_register_tier_decision_sink``.
    """
    command_argv = getattr(ctx, "meta", {}).get("vertex_command_argv")
    if _should_skip_scheduled_db_maintenance(
        ctx, command_argv if isinstance(command_argv, list) else sys.argv[1:],
    ):
        return
    program_id = _program_for_startup(ctx)
    if program_id is None:
        return
    try:
        with use_compaction_programs_root(_effective_startup_programs_root()):
            maybe_run_scheduled_compaction(program_id)
    except Exception:  # pragma: no cover - maintenance must never break the CLI
        logging.getLogger(__name__).warning("scheduled db maintenance failed", exc_info=True)


def _stdout_supports_interactive_catchup() -> bool:
    isatty = getattr(sys.stdout, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except Exception:
        return False


def _set_invocation_ai_mode(argv: list[str]) -> None:
    if any(token == "--no-ai" or token.startswith("--no-ai=") for token in argv):
        set_ai_mode(AIMode.DISABLED)
        return
    set_ai_mode(AIMode.ACTIVE)


def _should_skip_scheduled_db_maintenance(ctx: typer.Context, argv: list[str]) -> bool:
    if ctx.invoked_subcommand is None:
        return True
    if len(argv) >= 2 and argv[0] == "admin" and argv[1] == "db":
        return True
    return False


def _resolve_program_for_session_catchup(argv: list[str]) -> str | None:
    program = _extract_option(argv, "--program")
    if program is not None:
        return program if is_bare_identifier(program) else None
    edition = _extract_option(argv, "--edition")
    if edition is None or not is_bare_identifier(edition):
        return None
    resolved = resolve_edition_paths(edition, programs_root=PROGRAMS_ROOT)
    if resolved is None or not is_bare_identifier(resolved.program_id):
        return None
    return resolved.program_id


def _warn_startup_scope_unavailable(ctx: typer.Context) -> None:
    if ctx.meta.get("vertex_startup_scope_warning") is not True:
        logging.getLogger(__name__).warning("startup scope unavailable; scoped startup effects skipped")
        ctx.meta["vertex_startup_scope_warning"] = True


def _program_for_startup(ctx: typer.Context) -> str | None:
    meta = getattr(ctx, "meta", {})
    if ctx.invoked_subcommand == "ask":
        program = meta.get("ask_startup_program")
        return program if isinstance(program, str) and is_bare_identifier(program) else None
    if "vertex_startup_scope" in meta:
        if "vertex_startup_program" in meta:
            cached = meta["vertex_startup_program"]
            return cached if isinstance(cached, str) else None
        meta["vertex_startup_program"] = None
        scope = meta["vertex_startup_scope"]
        if not isinstance(scope, tuple) or len(scope) != 2:
            _warn_startup_scope_unavailable(ctx)
            return None
        program, edition = scope
        if (
            (program is not None and (not isinstance(program, str) or not is_bare_identifier(program)))
            or (edition is not None and (not isinstance(edition, str) or not is_bare_identifier(edition)))
        ):
            _warn_startup_scope_unavailable(ctx)
            return None
        resolved = resolve_edition_paths(edition, programs_root=PROGRAMS_ROOT) if edition is not None else None
        if edition is not None and (
            resolved is None or not is_bare_identifier(resolved.program_id)
        ):
            _warn_startup_scope_unavailable(ctx)
            return None
        if program is not None and resolved is not None and program != resolved.program_id:
            _warn_startup_scope_unavailable(ctx)
            return None
        selected = program if program is not None else resolved.program_id if resolved is not None else None
        meta["vertex_startup_program"] = selected
        return selected
    return _resolve_program_for_session_catchup(_startup_argv(ctx))


def _startup_argv(ctx: typer.Context) -> list[str]:
    argv = getattr(ctx, "meta", {}).get("vertex_invocation_argv")
    return argv if isinstance(argv, list) else sys.argv[1:]


def _extract_option(argv: list[str], option_name: str) -> str | None:
    for index, token in enumerate(argv):
        if token == option_name and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith(option_name + "="):
            return token.split("=", 1)[1]
    return None


app.add_typer(list_app, name="list")
app.add_typer(actions_app, name="actions")
app.add_typer(actuate_app, name="actuate")
app.add_typer(ado_app, name="ado")
app.add_typer(ai_proposals_app, name="ai-proposals")
app.add_typer(admin_app, name="admin")
app.add_typer(assertion_app, name="assertion")
app.add_typer(assumptions_app, name="assumptions")
app.add_typer(claims_app, name="claims")
app.add_typer(cockpit_app, name="cockpit")
app.add_typer(calibration_app, name="calibration")
app.add_typer(commitment_app, name="commitment")
app.add_typer(context_app, name="context")
app.add_typer(config_app, name="config")
app.add_typer(decision_brief_pilot_app, name="decision-brief-pilot")
app.add_typer(program_synthesizer_pilot_app, name="program-synthesizer-pilot")
app.add_typer(connectors_app, name="connectors")
app.add_typer(audit_app, name="audit")
app.command("context-diff")(_context_diff_cmd)
app.add_typer(decisions_app, name="decisions")
app.add_typer(dependencies_app, name="dependencies")
app.add_typer(discover_app, name="discover")
app.add_typer(editor_app, name="editor")
app.add_typer(entity_aliases_app, name="entity-aliases")
app.add_typer(facts_app, name="facts")
app.add_typer(kb_app, name="kb")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(ledger_app, name="ledger")
app.add_typer(inspect_app, name="inspect")
app.add_typer(integration_app, name="integration")
app.add_typer(index_app, name="index")
app.add_typer(milestones_app, name="milestones")
hypothesis_app.command("quickstart")(quickstart_command)
app.add_typer(hypothesis_app, name="hypothesis")
app.add_typer(observation_app, name="observation")
app.add_typer(policy_app, name="policy")
app.add_typer(privacy_app, name="privacy")
app.add_typer(observability_app, name="observability")
app.add_typer(alerts_app, name="alerts")
app.add_typer(readiness_app, name="readiness")
app.add_typer(registry_app, name="registry")
app.add_typer(reality_app, name="reality")
app.add_typer(review_sections_app, name="review-sections")
app.add_typer(rev_app, name="rev")
app.add_typer(risks_app, name="risks")
app.add_typer(salience_app, name="salience")
app.add_typer(signals_app, name="signals")
app.add_typer(storage_app, name="storage")
admin_app.add_typer(assertion_app, name="assertion")
admin_app.add_typer(auth_app, name="auth")
admin_app.add_typer(db_app, name="db")
admin_app.add_typer(metric_app, name="metric")
admin_app.command("doctor")(doctor_command)
admin_app.command("notifications")(notifications_command)
admin_app.command("baseline")(admin_baseline_command)
admin_app.command("platform-proof")(admin_platform_proof_command)
admin_app.command("s7-position")(admin_s7_position_command)
admin_app.command("reconcile")(admin_reconcile_command)
admin_app.command("migrate-legacy-state")(migrate_legacy_state_command)
admin_app.command("bootstrap-legacy-cutoff")(bootstrap_legacy_cutoff_command)
admin_app.command("fact-store-flip")(fact_store_flip_command)
admin_app.command("archive-signing")(admin_archive_signing_command)
admin_app.command("upgrade-state")(admin_upgrade_state_command)
admin_app.command("metrics-rollup")(admin_metrics_rollup_command)
app.command("archive-journals")(archive_journals_command)
app.command("archive-verify")(archive_verify_command)
app.command("ask")(ask_command)
app.command("apply-proposals")(apply_proposals_command)
app.command("apply-overrides")(apply_overrides_command)
app.command("backup")(backup_command)
app.command("backfill")(backfill_command)
app.command("bootstrap")(bootstrap_command)
app.command("bridge-status")(bridge_status_command)
app.command("brief")(brief_command)
app.command("catchup")(catchup_command)
app.command("capture-lt-deck")(capture_lt_deck_command)
app.command("confirm")(confirm_command)
app.command("deck-companion")(deck_companion_command)
app.command("diff")(diff_command)
app.command("doctor")(doctor_command)
app.command("edit")(edit_command)
app.command("enrich")(enrich_command)
app.command("escalate")(escalate_command)
app.command("evidence")(evidence_command)
app.command("freshness")(freshness_command)
app.command("fleet")(fleet_command)
app.command("gather")(gather_command)
app.command("prefetch")(prefetch_command)
app.command("history")(history_command)
app.command("investigate")(investigate_command)
app.command("ingest-update")(ingest_update_command)
app.command("manifest")(manifest_command)
app.command("meeting-close")(meeting_close_command)
app.command("maturity-check")(maturity_check_command)
app.command("migrate")(migrate_command)
app.command("notify")(notify_command)
app.command("nudge")(nudge_command)
app.command("next")(next_command)
app.command("onboard")(onboard_command)
app.command("setup")(setup_command)
app.command("owner-pack")(owner_pack_command)
app.command("override")(override_command)
app.command("prep")(prep_command)
app.command("decision-brief")(decision_brief_command)
app.command("propose")(propose_command)
app.command("published-baseline")(published_baseline_command)
app.command("rollback")(rollback_command)
app.command("review-proposals")(review_proposals_command)
app.command("publish-gate")(publish_gate_command)
app.command("draft")(report_command)
app.command("reconcile")(reconcile_command)
app.command("report")(report_command)
app.command("review-debrief")(review_debrief_command)
app.command("review-full")(review_full_command)
app.command("probe-ado")(probe_ado)
app.command("status")(status_command)
app.command("summarize")(summarize_command)
app.command("synthesize")(synthesize_command)
app.command("triage")(triage_command)
app.command("trust")(trust_command)
app.command("trust-bootstrap")(trust_bootstrap_command)
app.command("hints")(hints_command)
app.command("vitality")(vitality_command)
app.command("watch")(watch_command)
register_phase_stubs(app)


if __name__ == "__main__":
    app()

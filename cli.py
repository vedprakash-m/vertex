from __future__ import annotations

import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

import click
import typer
from typer.core import TyperGroup

from src.ai.ai_mode import AIMode, set_ai_mode
from src.commands.actions import app as actions_app
from src.commands.ado import app as ado_app
from src.commands.admin_baseline import admin_baseline_command
from src.commands.admin_fact_store_flip import fact_store_flip_command
from src.commands.admin_fact_store_migrate import migrate_legacy_state_command
from src.commands.admin_platform_proof import admin_platform_proof_command
from src.commands.admin_s7_position import admin_s7_position_command
from src.commands.admin_reconcile import reconcile_command as admin_reconcile_command
from src.commands.admin_upgrade_state import upgrade_state_command as admin_upgrade_state_command
from src.commands.archive_journals import archive_journals_command
from src.commands.archive_signing import (
    admin_archive_signing_command,
    archive_verify_command,
)
from src.commands.ask import ask_command
from src.commands.actuate import app as actuate_app
from src.commands.auth import app as auth_app
from src.commands.assertion import app as assertion_app
from src.commands.audit import app as audit_app
from src.commands.apply_proposals import apply_proposals_command
from src.commands.assumptions import app as assumptions_app
from src.commands.claims import app as claims_app
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
from src.commands.confirm import confirm_command
from src.commands.context import app as context_app
from src.commands.config import app as config_app
from src.commands.connectors import app as connectors_app
from src.commands.deck_companion import deck_companion_command
from src.commands.diff import diff_command
from src.commands.context_diff import context_diff as _context_diff_cmd
from src.commands.db import app as db_app, maybe_run_scheduled_compaction
from src.commands.doctor import doctor_command
from src.commands.edit import edit_command
from src.commands.editor import app as editor_app
from src.commands.escalate import escalate_command
from src.commands.evidence import evidence_command
from src.commands.enrich import enrich_command
from src.commands.facts import app as facts_app
from src.commands.freshness import freshness_command
from src.commands.fleet import fleet_command
from src.commands.gather import gather_command
from src.commands.history import history_command
from src.commands.hypothesis import app as hypothesis_app
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
from src.commands.override import override_command
from src.commands.prep import prep_command
from src.commands.privacy import privacy_app
from src.commands.observability import observability_app, alerts_app
from src.commands.decision_brief import decision_brief_command
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
from src.commands.report import apply_overrides_command, report_command
from src.commands.review_debrief import review_debrief_command
from src.commands.review_full import review_full_command
from src.commands.review_sections import app as review_sections_app
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
from src.commands.triage import triage_command
from src.commands.trust import trust_command, trust_bootstrap_command
from src.commands.vitality import vitality_command
from src.commands.watch import watch_command
from src.core.catchup_runner import maybe_catchup
from src.core.edition_resolver import PROGRAMS_ROOT, resolve_edition_paths


class VertexRootGroup(TyperGroup):
    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.ClickException:
            ask_group = self.commands.get("ask")
            if ask_group is None or not args:
                raise
            if args[0].startswith("-"):
                raise
            edition = ctx.params.get("edition")
            routed_args: list[str] = []
            if isinstance(edition, str) and edition.strip() and edition.strip():
                routed_args.extend(["--edition", edition.strip()])
            routed_args.append(" ".join(args).strip())
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
    _set_invocation_ai_mode(sys.argv[1:])
    if ctx.invoked_subcommand is not None:
        if skip_issue_flag:
            raise typer.BadParameter("--skip-issue cannot be combined with a subcommand.")
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
    program_id = _resolve_program_for_session_catchup(sys.argv[1:])
    if program_id is None:
        return
    maybe_catchup(
        program_id,
        programs_root=PROGRAMS_ROOT,
        emit=typer.echo,
        event_builder=build_catchup_event_builder(program_id, programs_root=PROGRAMS_ROOT),
    )


def _maybe_run_scheduled_db_maintenance(ctx: typer.Context) -> None:
    if _should_skip_scheduled_db_maintenance(ctx, sys.argv[1:]):
        return
    program_id = _resolve_program_for_session_catchup(sys.argv[1:])
    if program_id is None:
        return
    maybe_run_scheduled_compaction(program_id)


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
        return program
    edition = _extract_option(argv, "--edition")
    if edition is None:
        return None
    resolved = resolve_edition_paths(edition, programs_root=PROGRAMS_ROOT)
    if resolved is None:
        return None
    return resolved.program_id


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
app.add_typer(admin_app, name="admin")
app.add_typer(assertion_app, name="assertion")
app.add_typer(assumptions_app, name="assumptions")
app.add_typer(claims_app, name="claims")
app.add_typer(calibration_app, name="calibration")
app.add_typer(commitment_app, name="commitment")
app.add_typer(context_app, name="context")
app.add_typer(config_app, name="config")
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
admin_app.command("fact-store-flip")(fact_store_flip_command)
admin_app.command("archive-signing")(admin_archive_signing_command)
admin_app.command("upgrade-state")(admin_upgrade_state_command)
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

from __future__ import annotations

import csv
from datetime import datetime, timezone
import inspect
from io import StringIO
import json
import os
from pathlib import Path
from typing import Any

import typer

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.client import AIClientError
from src.ai.deployment_fallback import FallbackAIClient, LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext, use_trace_context
from src.ai.provider import LLMProvider
from src.core.config_loader import PROGRAMS_ROOT
from src.core.exceptions import ConfigError
from src.core.kb_changelog import build_kb_changelog_report, render_kb_changelog_report
from src.core.kb_updates import KbUpdatePlan, apply_kb_update, parse_deterministic_kb_correction
from src.core.kb_updates import parse_kb_update_operations, prepare_kb_update, read_program_kb_documents
from src.core.kb_updates import supported_kb_paths
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.profile_encryption import decrypt_people_profiles_file, encrypt_people_profiles_file, inspect_people_profiles_file


app = typer.Typer(help="Knowledge base diagnostics and history.")
profiles_app = typer.Typer(help="Protect or unwrap sensitive people profile files.")

_KB_UPDATE_PROMPT_VERSION = "kb_update_plan.v1"


@app.command("changelog")
def kb_changelog_command(
    program: str = typer.Option(..., "--program", help="Program id."),
    since: str = typer.Option(..., "--since", help="ISO week in YYYY-Www format."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    repo_root = Path(PROGRAMS_ROOT).resolve().parents[0]
    try:
        report = build_kb_changelog_report(
            program_id=program,
            since_week=since,
            repo_root=repo_root,
        )
    except (ConfigError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    except RuntimeError as error:
        raise typer.BadParameter(f"Unable to read git history: {error}") from error

    if format == "human":
        typer.echo(render_kb_changelog_report(report), nl=False)
    else:
        typer.echo(render_kb_changelog_output(report, format=format), nl=False)
    raise typer.Exit(code=0)


def render_kb_changelog_output(report: Any, *, format: str) -> str:
    payload = _build_kb_changelog_payload(report)
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("program_id", "since_week", "since_date", "commit_sha", "committed_at", "alias", "change_type", "before", "after"))
        if payload["entries"]:
            for entry in payload["entries"]:  # type: ignore[attr-defined]
                writer.writerow(
                    (
                        payload["program_id"],
                        payload["since_week"],
                        payload["since_date"],
                        entry["commit_sha"],
                        entry["committed_at"],
                        entry["alias"],
                        entry["change_type"],
                        entry["before"],
                        entry["after"],
                    )
                )
        else:
            writer.writerow((payload["program_id"], payload["since_week"], payload["since_date"], None, None, None, None, None, None))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def _build_kb_changelog_payload(report: Any) -> dict[str, object]:
    return {
        "entry_count": len(report.entries),
        "entries": [
            {
                "after": entry.after,
                "alias": entry.alias,
                "before": entry.before,
                "change_type": entry.change_type,
                "commit_sha": entry.commit_sha,
                "committed_at": entry.committed_at.isoformat(),
            }
            for entry in report.entries
        ],
        "program_id": report.program_id,
        "since_date": report.since_date.isoformat(),
        "since_week": report.since_week,
    }


@app.command("update")
def kb_update_command(
    correction: str = typer.Argument(..., help="Natural-language KB correction."),
    program: str | None = typer.Option(None, "--program", help="Program id. Inferred when only one program exists."),
    apply: bool = typer.Option(False, "--apply", help="Write the validated KB change."),
    ai: bool = typer.Option(True, "--ai/--no-ai", help="Allow AI planning when deterministic parsing is insufficient."),
) -> None:
    programs_root = Path(PROGRAMS_ROOT)
    resolved_program = _resolve_program_id(program, programs_root)

    plan = parse_deterministic_kb_correction(correction, program_id=resolved_program)
    if plan is None and ai:
        plan = _plan_kb_update_with_ai(
            correction=correction,
            program_id=resolved_program,
            programs_root=programs_root,
        )
    if plan is None:
        raise typer.BadParameter(
            "Unable to interpret the correction. Supported deterministic patterns include setting person title/email/display name, "
            "adding or removing person team_ids, and setting workstream pm/eng/alternate owners. Configure "
            "AZURE_OPENAI_KB_DEPLOYMENT, VERTEX_AI_DEPLOYMENT, or AZURE_OPENAI_DEPLOYMENT to enable AI planning for broader corrections; "
            f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE}"
        )

    try:
        preview = prepare_kb_update(plan, programs_root=programs_root)
    except (ConfigError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error

    typer.echo(_render_kb_update_preview(preview), nl=False)

    if not apply:
        typer.echo("\nPreview only. Re-run with --apply to write the change.")
        raise typer.Exit(code=0)

    result = apply_kb_update(preview, programs_root=programs_root)
    typer.echo(f"\nApplied {len(result.preview.changes)} file(s). Audit log: {result.audit_path}")
    raise typer.Exit(code=0)


def _resolve_program_id(program: str | None, programs_root: Path) -> str:
    if program is not None:
        return program
    candidates = tuple(
        path.name
        for path in sorted(programs_root.iterdir(), key=lambda item: item.name.lower())
        if path.is_dir() and (path / "program.yaml").exists()
    ) if programs_root.exists() else ()
    if len(candidates) == 1:
        return candidates[0]
    raise typer.BadParameter("Provide --program when multiple programs exist.")


def _build_kb_update_client(*, trace_context: AITraceContext | None = None) -> LLMProvider | None:
    if get_ai_mode() == AIMode.DISABLED:
        return None
    deployments = resolve_ai_deployments_for_feature(
        feature_name="default",
        primary_candidates=(),
        backup_candidates=(),
        primary_fallback_envs=("AZURE_OPENAI_KB_DEPLOYMENT", "VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
        backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
    )
    if not deployments:
        return None
    try:
        # D-20: bind the trace context to the process-level ContextVar so
        # any nested helper that doesn't take an explicit `trace_context=`
        # arg still picks it up. The explicit kwarg below still wins, so
        # this is behavior-preserving.
        with use_trace_context(trace_context):
            return FallbackAIClient(
                deployments=deployments,
                temperature=0.0,
                budget_usd=0.25,
                trace_context=trace_context,
            )
    except (AIClientError, RuntimeError):
        return None


def _plan_kb_update_with_ai(
    *,
    correction: str,
    program_id: str,
    programs_root: Path,
) -> KbUpdatePlan | None:
    client = _build_default_kb_update_client(
        trace_context=_build_kb_update_trace_context(
            program_id=program_id,
            programs_root=programs_root,
        )
    )
    if client is None:
        return None

    documents = read_program_kb_documents(program_id, programs_root=programs_root)
    prompt = _build_kb_update_prompt(correction=correction, program_id=program_id, documents=documents)
    try:
        raw_text = str(
            client.chat(
                "You convert knowledge-base corrections into strict JSON plans. Respond with JSON only.",
                prompt,
                max_tokens=900,
                prompt_version=_KB_UPDATE_PROMPT_VERSION,
            )
        ).strip()
        # D-26: route the raw model output through the shared safety pipeline so a
        # prompt-injected correction cannot smuggle malicious instructions into the
        # knowledge base. We validate (injection detection raises AIPipelineError)
        # but parse the original JSON, since PII-scrubbing the structured envelope
        # would corrupt it and KB content legitimately contains aliases/emails.
        process_generated_text(raw_text)
        payload = json.loads(raw_text)
        operations = parse_kb_update_operations(payload)
        return KbUpdatePlan(
            program_id=program_id,
            correction=correction,
            planner="ai",
            operations=operations,
        )
    except (AIClientError, AIPipelineError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _build_kb_update_trace_context(*, program_id: str, programs_root: Path) -> AITraceContext:
    current_time = datetime.now(timezone.utc)
    return AITraceContext(
        edition=program_id,
        run_id=f"{program_id}:kb:update:{current_time.strftime('%Y%m%dT%H%M%SZ')}",
        caller="src.commands.kb._plan_kb_update_with_ai",
        metadata={
            "program_id": program_id,
            "task_type": "kb_update_plan",
            "run_budget_usd": 0.25,
        },
    )


def _build_default_kb_update_client(*, trace_context: AITraceContext) -> LLMProvider | None:
    if get_ai_mode() == AIMode.DISABLED:
        return None
    if "trace_context" in inspect.signature(_build_kb_update_client).parameters:
        return _build_kb_update_client(trace_context=trace_context)
    return _build_kb_update_client()


def _build_kb_update_prompt(
    *,
    correction: str,
    program_id: str,
    documents: dict[str, dict[str, Any]],
) -> str:
    rendered_documents = json.dumps(documents, indent=2, sort_keys=True)
    supported_paths = ", ".join(supported_kb_paths())
    return (
        f"Program: {program_id}\n"
        f"Correction: {correction}\n"
        f"Supported paths: {supported_paths}\n"
        "Output JSON with this shape only:\n"
        "{\n"
        '  "operations": [\n'
        "    {\n"
        '      "path": "knowledge/people_directory.yaml",\n'
        '      "action": "set_fields" | "add_list_value" | "remove_list_value" | "remove_entry",\n'
        '      "match_value": "entry id/alias",\n'
        '      "fields": {"field": "value"},\n'
        '      "field": "list_field_name",\n'
        '      "value": "list item value"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Use set_fields for scalar or whole-list replacements. Use add/remove_list_value only for list membership edits.\n"
        "Do not invent unsupported paths. Do not include markdown, explanations, or comments.\n"
        f"Current documents:\n{rendered_documents}"
    )


def _render_kb_update_preview(preview: Any) -> str:
    lines = [
        f"KB update preview: {preview.program_id}",
        f"Planner: {preview.planner}",
        f"Validation: {preview.validation_summary}",
        "",
        preview.diff,
    ]
    return "\n".join(line for line in lines if line != "")


@profiles_app.command("encrypt")
def kb_profiles_encrypt_command(
    program: str = typer.Option(..., "--program", help="Program id."),
    scope: str = typer.Option("active", "--scope", help="Target active, shared, or program-scoped people_profiles.yaml."),
) -> None:
    target_path = _resolve_profiles_path(program=program, scope=scope)
    try:
        before = inspect_people_profiles_file(target_path)
        after = encrypt_people_profiles_file(target_path)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error

    relative_path = _render_relative_path(target_path)
    if before.storage == "encrypted":
        typer.echo(
            f"Sensitive profiles are already encrypted at rest in {relative_path} ({after.profile_count} entr{'y' if after.profile_count == 1 else 'ies'})."
        )
    else:
        typer.echo(
            f"Encrypted {after.profile_count} sensitive profile entr{'y' if after.profile_count == 1 else 'ies'} in {relative_path}."
        )
    raise typer.Exit(code=0)


@profiles_app.command("decrypt")
def kb_profiles_decrypt_command(
    program: str = typer.Option(..., "--program", help="Program id."),
    scope: str = typer.Option("active", "--scope", help="Target active, shared, or program-scoped people_profiles.yaml."),
) -> None:
    target_path = _resolve_profiles_path(program=program, scope=scope)
    try:
        before = inspect_people_profiles_file(target_path)
        after = decrypt_people_profiles_file(target_path)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error

    relative_path = _render_relative_path(target_path)
    if before.storage == "plaintext":
        typer.echo(
            f"Sensitive profiles are already plaintext in {relative_path} ({after.profile_count} entr{'y' if after.profile_count == 1 else 'ies'})."
        )
    else:
        typer.echo(
            f"Decrypted {after.profile_count} sensitive profile entr{'y' if after.profile_count == 1 else 'ies'} in {relative_path}."
        )
    raise typer.Exit(code=0)


def _resolve_profiles_path(*, program: str, scope: str) -> Path:
    programs_root = Path(PROGRAMS_ROOT)
    normalized_scope = scope.strip().lower()
    if normalized_scope not in {"active", "program", "shared"}:
        raise typer.BadParameter("--scope must be one of: active, program, shared.")

    program_path = programs_root / program / "knowledge" / "people_profiles.yaml"
    shared_path = get_shared_knowledge_root(programs_root) / "people_profiles.yaml"
    if normalized_scope == "program":
        return program_path
    if normalized_scope == "shared":
        return shared_path
    return shared_path if shared_path.exists() else program_path


def _render_relative_path(path: Path) -> str:
    repo_root = Path(PROGRAMS_ROOT).resolve().parent
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return str(path)


app.add_typer(profiles_app, name="profiles")

"""Doctor output presentation helpers.

Extracted from ``src/commands/doctor.py`` (Phase 3 doctor decomposition). This
module owns the pure presentation surface — per-flag tip text, the JSON/CSV
payload shape, and rendered output — with no side effects, no I/O, and no
monkeypatch seams. ``doctor.py`` re-exports these symbols so existing imports
(``_build_doctor_payload``, ``render_doctor_output``) keep working.
"""

from __future__ import annotations

import csv
import json
from io import StringIO
from typing import cast

import typer

from src.commands.doctor_checks.models import DoctorReport


def doctor_tip(
    *,
    check_auth: bool,
    operator_gates: bool,
    platform_readiness: bool,
    kb: bool,
    ids: bool,
    cadence: bool,
    channels: bool,
    privacy: bool,
    kusto: bool,
    milestones: bool,
    dependencies: bool,
    actions: bool,
    risks: bool,
    escalations: bool,
    decisions: bool,
    assumptions: bool,
    readiness: bool,
    semantic_index: bool,
    metric_bindings: bool,
    consistency: bool,
    checkpoints: bool,
    storage: bool,
    flip_status: bool = False,
    flip_parity: bool = False,
    fact_parity: bool = False,
    confirm_readiness: bool = False,
    adapter_cert: bool = False,
    charts: bool,
    source_waivers: bool = False,
    watch_sources: bool,
    catchup_log: bool,
    nudge: bool = False,
    circuit_breakers: bool,
    context: bool = False,
) -> str:
    if check_auth:
        return "Tip: Re-run `vertex doctor --check-auth` after rotating ADO, Graph, or Agency CLI credentials."
    if operator_gates:
        return "Tip: Re-run `vertex doctor --operator-gates --edition <name>` after any operator action that changes M365 IDs, Kusto validation state, checkpoint inventory, or rollback drill evidence."
    if platform_readiness:
        return "Tip: Re-run `vertex doctor --platform-readiness` after any new proof log entry, second-program confirm, adapter activation, or fleet-shape change."
    if kb:
        return "Tip: Re-run `vertex doctor --kb` after editing programs/, editions/, or knowledge/ files."
    if ids:
        return "Tip: Re-run `vertex doctor --ids` after editing scorecards.yaml, chapter_contract.yaml, slice_contracts.yaml, workstream_registry.yaml, or workstreams.yaml."
    if cadence:
        return "Tip: Re-run `vertex doctor --cadence` after confirming an issue or editing communication_plan in program.yaml."
    if channels:
        return "Tip: Re-run `vertex doctor --channels --edition <name>` after any full gather to inspect completeness, skipped flags, and transcript coverage gaps."
    if privacy:
        return "Tip: Re-run `vertex doctor --privacy` after rotating secrets, cleaning journal files, or changing people_profiles.yaml handling."
    if kusto:
        return "Tip: Re-run `vertex doctor --kusto` after editing Kusto query definitions or changing cluster access."
    if milestones:
        return "Tip: Re-run `vertex doctor --milestones` after editing milestones.yaml, workstreams.yaml, or people_directory.yaml."
    if dependencies:
        return "Tip: Re-run `vertex doctor --dependencies` after editing dependencies.yaml, workstreams.yaml, or milestones.yaml."
    if actions:
        return "Tip: Re-run `vertex doctor --actions` after editing actions.jsonl or related program references."
    if risks:
        return "Tip: Re-run `vertex doctor --risks` after editing risk_register.yaml or related program references."
    if escalations:
        return "Tip: Re-run `vertex doctor --escalations` after editing escalation_rules.yaml or reviewing escalation_state.json."
    if decisions:
        return "Tip: Re-run `vertex doctor --decisions` after editing decisions.yaml or linked decision references."
    if assumptions:
        return "Tip: Re-run `vertex doctor --assumptions` after editing assumptions.yaml or linked milestone/risk references."
    if readiness:
        return "Tip: Re-run `vertex doctor --readiness --edition <name>` after editing readiness.yaml, toggling readiness.gate, or refreshing the snapshot with `vertex readiness fetch`."
    if semantic_index:
        return "Tip: Re-run `vertex doctor --semantic-index --edition <name>` after confirm, archive backfills, or `vertex index rebuild`."
    if metric_bindings:
        return "Tip: Re-run `vertex admin doctor --metric-bindings --edition <name>` after editing metric bindings, changing Kusto schemas, or validating bindings with `vertex admin metric validate`."
    if consistency:
        return "Tip: Re-run `vertex doctor --consistency --edition <name>` after confirm, baseline correction, or recovery work."
    if checkpoints:
        return "Tip: Re-run `vertex doctor --checkpoints --edition <name>` after any non-dry-run confirm or rollback rehearsal."
    if storage:
        return "Tip: Re-run `vertex doctor --storage --edition <name>` after journal archival, retention-policy changes, or SQLite maintenance work."
    if flip_status:
        return "Tip: Re-run `vertex doctor --flip-status --edition <name>` after fact-store backfills, parity checks, or source-of-record changes."
    if flip_parity:
        return "Tip: Re-run `vertex doctor --flip-parity --edition <name> --issue <n>` after backfills or any mutable-state migration that can change fact parity."
    if fact_parity:
        return "Tip: Re-run `vertex doctor --fact-parity --edition <name>` after running `vertex facts dual-read-log --program <id>` to add parity cycles."
    if confirm_readiness:
        return "Tip: Re-run `vertex doctor --confirm-readiness --edition <name>` after resolving blockers (overrides, gather freshness, archive integrity)."
    if adapter_cert:
        return "Tip: Re-run `vertex doctor --adapter-cert --edition <name>` after enabling new UIL channels (VERTEX_UIL_KUSTO/TEAMS/ICM env flags) or after certifying an adapter with `vertex facts dual-read-log`."
    if charts:
        return "Tip: Re-run `vertex doctor --charts --edition <name>` after changing chart cache TTL, attachment targets, or renderer module config."
    if source_waivers:
        return "Tip: Re-run `vertex doctor --source-waivers` after editing programs/<id>/source_waivers.yaml or vertex/policies/source_waivers.schema.yaml."
    if watch_sources:
        return "Tip: Re-run `vertex doctor --watch-sources --source <name>` after changing watch source config, Agency availability, or Kusto query definitions."
    if catchup_log:
        return "Tip: Re-run `vertex doctor --catchup-log` after a failed or stale session-start catchup to inspect the latest recorded event."
    if nudge:
        return "Tip: Re-run `vertex doctor --nudge --edition <name>` after editing nudge edition config, migrating state, or changing section criteria."
    if circuit_breakers:
        return "Tip: Re-run `vertex doctor --edition <name> --circuit-breakers` after repeated ADO failures or after resetting breaker state."

    if context:
        return "Tip: Re-run `vertex doctor --context --edition <name>` after editing any program file."
    return "Tip: Run `vertex doctor --fix` to auto-repair common issues."


def build_doctor_payload(*, report: DoctorReport, tip: str | None) -> dict[str, object]:
    return {
        "checks": [
            {
                "detail": check.detail,
                "label": check.label,
                **({"metadata": check.metadata} if check.metadata else {}),
                "status": check.status,
            }
            for check in report.checks
        ],
        "edition": report.edition,
        "failures": report.failures,
        "overall": report.overall,
        "tip": tip,
        "warnings": report.warnings,
    }


def render_doctor_output(payload: dict[str, object], *, format: str) -> str:
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        columns = ("edition", "overall", "warnings", "failures", "label", "status", "detail", "metadata_json", "tip")
        writer.writerow(columns)
        checks = cast(list[dict[str, object]], payload["checks"])
        if checks:
            for check in checks:
                writer.writerow(
                    [
                        payload["edition"],
                        payload["overall"],
                        payload["warnings"],
                        payload["failures"],
                        check["label"],
                        check["status"],
                        check["detail"],
                        json.dumps(check.get("metadata"), sort_keys=True) if check.get("metadata") is not None else "",
                        payload["tip"],
                    ]
                )
        else:
            writer.writerow([payload["edition"], payload["overall"], payload["warnings"], payload["failures"], None, None, None, "", payload["tip"]])
        return buffer.getvalue()
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    raise typer.BadParameter("Human doctor output is rendered directly by the command.")

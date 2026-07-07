from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import DoctorCheck
from src.core.discovery_intent import SourceCandidateStatus, SourceIntentStatus, SourceRefKind
from src.core.m365_registry_store import load_m365_registry
from src.core.source_candidate_store import SourceCandidateStore
from src.core.program_paths import resolve_channel_registry_path_for_read
from src.m365.agency_bridge import AgencyCapabilities
from src.m365.discovery_diagnostics import classify_missing_id_discovery_status


def summarize_missing_id_diagnostics(diagnostics: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for diagnostic in diagnostics:
        code = str(diagnostic.get("status") or "").strip()
        if not code:
            continue
        counts[code] = counts.get(code, 0) + 1

    fragments: list[str] = []
    for code, label in (
        ("no_candidates_found", "completed discovery but still returned no durable-ID candidates"),
        ("runtime_blocked", "are currently blocked by discovery runtime/auth failures"),
        ("tool_unavailable", "cannot be searched because the required WorkIQ tool is unavailable"),
        ("workiq_unavailable", "cannot be searched because WorkIQ MCP is unavailable"),
        ("agency_cli_unavailable", "cannot be searched because Agency CLI is unavailable"),
        ("discovery_inactive", "have not been probed because WorkIQ discovery was inactive on the latest gather"),
        ("not_probed_yet", "are still awaiting the first active discovery pass"),
    ):
        count = counts.get(code, 0)
        if count > 0:
            fragments.append(f"{count} artifact(s) {label}")
    return "; ".join(fragments)


def build_missing_id_diagnostics(
    *,
    missing_id_artifacts: list[dict[str, Any]],
    m365_discovery: dict[str, Any] | None,
    agency_caps: AgencyCapabilities,
) -> list[dict[str, Any]]:
    discovery_entry = m365_discovery or {}
    runtime_error = str(discovery_entry.get("discovery_last_error") or "").strip() or None
    first_discovery_completed_at = str(discovery_entry.get("first_discovery_completed_at") or "").strip() or None
    available_tools = {tool.strip() for tool in agency_caps.server_tools.get("workiq", ()) if tool.strip()}

    diagnostics: list[dict[str, Any]] = []
    for artifact in missing_id_artifacts:
        artifact_type = str(artifact.get("artifact_type") or "").strip() or "teams_chat"
        status = classify_missing_id_discovery_status(
            artifact_type=artifact_type,
            discovery_active=bool(discovery_entry.get("active")),
            first_discovery_completed_at=first_discovery_completed_at,
            agency_available=agency_caps.available,
            has_workiq=agency_caps.has_workiq,
            workiq_cli_available=agency_caps.has_workiq_cli,
            available_tools=available_tools,
            runtime_error=runtime_error,
        )
        diagnostics.append(
            {
                "artifact_id": str(artifact.get("artifact_id") or "").strip(),
                "artifact_type": artifact_type,
                "inferred_workstream": artifact.get("inferred_workstream"),
                "status": status.code,
                "detail": status.detail,
            }
        )
    return diagnostics


def source_ref_kind_for_artifact_type(artifact_type: str) -> SourceRefKind | None:
    normalized = artifact_type.strip().lower()
    if normalized == "meeting_series":
        return SourceRefKind.MEETING_SERIES
    if normalized == "email_thread":
        return SourceRefKind.EMAIL_THREAD
    if normalized == "teams_channel":
        return SourceRefKind.TEAMS_CHANNEL
    if normalized == "teams_chat":
        return SourceRefKind.TEAMS_CHAT
    return None


def source_channel_for_ref_kind(ref_kind: SourceRefKind) -> str:
    return "email" if ref_kind is SourceRefKind.EMAIL_THREAD else "teams"


def build_missing_id_action_categories(
    *,
    program_id: str,
    programs_root: Path,
    missing_id_artifacts: list[dict[str, Any]],
    artifact_diagnostics: list[dict[str, Any]],
    load_m365_registry_fn: Callable[..., Any] = load_m365_registry,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    registry_artifacts = {
        artifact.artifact_id: artifact
        for artifact in load_m365_registry_fn(program_id, programs_root).artifacts
    }
    diagnostics_by_id = {
        str(entry.get("artifact_id") or "").strip(): entry
        for entry in artifact_diagnostics
    }
    store_path = resolve_channel_registry_path_for_read(program_id, programs_root=programs_root)
    candidate_store = SourceCandidateStore(store_path, program_id, ensure_schema=False) if store_path.exists() else None
    candidate_store_has_schema = candidate_store.has_discovery_schema() if candidate_store is not None else False
    current_time = as_of or datetime.now(timezone.utc)
    action_categories: list[dict[str, Any]] = []

    for artifact in missing_id_artifacts:
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        artifact_type = str(artifact.get("artifact_type") or "").strip()
        diagnostic = diagnostics_by_id.get(artifact_id, {})
        category = "operator-seed-required"
        next_command = "vertex registry set-id --program <program> --artifact-id <artifact> --series-id/--thread-id <id>"
        intent_id: str | None = None
        intent_status: str | None = None
        derived_state: str | None = None
        candidate_count = 0
        best_candidate_confidence: float | None = None
        artifact_record = registry_artifacts.get(artifact_id)
        ref_kind = source_ref_kind_for_artifact_type(artifact_type)

        if candidate_store is not None and candidate_store_has_schema and artifact_record is not None and ref_kind is not None:
            display_name = artifact_record.display_name or artifact_record.artifact_id
            intent = candidate_store.get_intent_by_name(
                workstream_id=artifact_record.inferred_workstream,
                ref_kind=ref_kind,
                display_name=display_name,
            )
            if intent is not None:
                intent_id = intent.intent_id
                intent_status = intent.status.value
                derived_state = candidate_store.derive_intent_state(intent.intent_id, as_of=current_time)
                candidates = candidate_store.list_candidates_for_intent(intent.intent_id)
                candidate_count = len(candidates)
                best_candidate_confidence = max((candidate.confidence for candidate in candidates), default=None)
                pending_candidates = [
                    candidate for candidate in candidates
                    if candidate.status == SourceCandidateStatus.PENDING
                ]
                high_confidence_pending = [
                    candidate for candidate in pending_candidates if candidate.confidence >= 0.85
                ]
                review_pending = [
                    candidate for candidate in pending_candidates if candidate.confidence >= 0.75
                ]
                if intent.status in {
                    SourceIntentStatus.SUPPRESSED,
                    SourceIntentStatus.RETIRED,
                    SourceIntentStatus.SUPERSEDED,
                }:
                    category = "config-mismatch"
                    next_command = f"vertex integration explain-source --program {program_id} --intent-id {intent.intent_id}"
                elif derived_state in {
                    SourceIntentStatus.AUTH_BLOCKED.value,
                    SourceIntentStatus.OUT_OF_IDENTITY_SCOPE.value,
                }:
                    category = "auth-admin-required"
                    next_command = f"vertex integration explain-source --program {program_id} --intent-id {intent.intent_id}"
                elif len(high_confidence_pending) == 1:
                    category = "auto-resolvable"
                    next_command = f"vertex integration discover --program {program_id} --channel {source_channel_for_ref_kind(ref_kind)} --force"
                elif len(review_pending) > 1 or derived_state in {
                    SourceIntentStatus.AMBIGUOUS.value,
                    SourceIntentStatus.CANDIDATE_FOUND.value,
                }:
                    category = "pm-decision-required"
                    next_command = f"vertex integration explain-source --program {program_id} --intent-id {intent.intent_id}"
                elif derived_state == SourceIntentStatus.SEARCHING.value:
                    category = "auto-resolvable"
                    next_command = f"vertex integration discover --program {program_id} --channel {source_channel_for_ref_kind(ref_kind)} --force"
                elif derived_state in {
                    SourceIntentStatus.DECLARED.value,
                    SourceIntentStatus.NO_CANDIDATES.value,
                } and candidate_count == 0:
                    category = "operator-seed-required"
                    next_command = f"vertex integration seed-id --program {program_id} --intent-id {intent.intent_id} --ref-id <series-or-thread-id> --pm-alias <alias>"

        if intent_id is None:
            raw_status = str(diagnostic.get("status") or "").strip()
            if raw_status in {"runtime_blocked", "tool_unavailable", "workiq_unavailable", "agency_cli_unavailable"}:
                category = "auth-admin-required"
                next_command = "vertex doctor --operator-gates --edition <name>"
            elif candidate_store is not None and not candidate_store_has_schema:
                category = "auto-resolvable"
                next_command = f"vertex integration schema-migrate --program {program_id}"
            elif raw_status in {"discovery_inactive", "not_probed_yet"}:
                category = "auto-resolvable"
                next_command = f"vertex integration discover --program {program_id} --channel teams --force"
            elif raw_status == "no_candidates_found":
                category = "source-absent"
                next_command = f"vertex integration explain-source --program {program_id} --ref-id {artifact_id}"

        action_categories.append(
            {
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "category": category,
                "next_command": next_command,
                "intent_id": intent_id,
                "intent_status": intent_status,
                "derived_state": derived_state,
                "candidate_count": candidate_count,
                "best_candidate_confidence": best_candidate_confidence,
            }
        )
    return action_categories


def operator_gate_m365_ids_check(
    *,
    program_id: str,
    programs_root: Path,
    edition_name: str,
    registry_review: dict[str, Any] | None,
    m365_discovery: dict[str, Any] | None,
    agency_caps: AgencyCapabilities,
    load_m365_registry_fn: Callable[..., Any] = load_m365_registry,
) -> DoctorCheck:
    commands = [
        f"vertex doctor --operator-gates --edition {edition_name}",
        "vertex registry discover-ids --program <program> --limit 10",
        "vertex registry set-id --program <program> --artifact-id <artifact> --series-id <id>",
        "vertex registry set-id --program <program> --artifact-id <artifact> --thread-id <id>",
    ]
    if registry_review is None:
        return DoctorCheck(
            "Gate:M365 IDs",
            "ok",
            "M365 is not enabled for this program, so no durable-ID seeding gate is active.",
            metadata={
                "owner": ["pm", "operator"],
                "commands": commands,
                "evidence_to_gather": [],
                "decisions": [],
                "llm_support": "Not needed unless M365 is later enabled.",
            },
        )

    missing_ids = list(registry_review.get("missing_id_ids") or [])
    missing_id_artifacts = list(registry_review.get("missing_id_artifacts") or [])
    artifact_diagnostics = build_missing_id_diagnostics(
        missing_id_artifacts=missing_id_artifacts,
        m365_discovery=m365_discovery,
        agency_caps=agency_caps,
    )
    artifact_action_categories = build_missing_id_action_categories(
        program_id=program_id,
        programs_root=programs_root,
        missing_id_artifacts=missing_id_artifacts,
        artifact_diagnostics=artifact_diagnostics,
        load_m365_registry_fn=load_m365_registry_fn,
    )
    action_category_counts: dict[str, int] = {}
    for entry in artifact_action_categories:
        category = str(entry.get("category") or "").strip()
        if not category:
            continue
        action_category_counts[category] = action_category_counts.get(category, 0) + 1
    first_discovery_completed_at = str((m365_discovery or {}).get("first_discovery_completed_at") or "").strip() or None
    discovery_summary = summarize_missing_id_diagnostics(artifact_diagnostics)
    detail = (
        f"{len(missing_ids)} PM-confirmed artifact(s) still need durable series_id/thread_id."
        if missing_ids
        else "All PM-confirmed M365 artifacts already carry durable series_id/thread_id values."
    )
    if missing_ids and first_discovery_completed_at:
        detail = f"{detail} First active discovery completed at {first_discovery_completed_at}."
    if missing_ids and discovery_summary:
        detail = f"{detail} {discovery_summary}."
    if missing_ids and action_category_counts:
        category_summary = ", ".join(
            f"{count} {category}"
            for category, count in sorted(action_category_counts.items())
        )
        detail = f"{detail} Action categories: {category_summary}."
    if missing_ids:
        detail = (
            f"{detail} Run `vertex registry discover-ids --program <program> --limit 10` after restoring any missing access; "
            "if discovery still returns no candidates, gather the canonical Teams/OWA link or numeric meeting code and seed it with `vertex registry set-id`."
        )
    return DoctorCheck(
        "Gate:M365 IDs",
        "fail" if missing_ids else "ok",
        detail,
        metadata={
            "owner": ["pm", "operator"],
            "commands": commands,
            "artifact_ids": missing_ids,
            "artifact_diagnostics": artifact_diagnostics,
            "artifact_action_categories": artifact_action_categories,
            "action_category_counts": action_category_counts,
            "first_discovery_completed_at": first_discovery_completed_at,
            "discovery_last_error": (m365_discovery or {}).get("discovery_last_error"),
            "evidence_to_gather": [
                "Canonical Teams/OWA meeting or chat links for each PM-confirmed artifact",
                "Bare meeting code or thread ID after normalization",
            ],
            "decisions": [
                "Confirm which real meeting/chat maps to each registry artifact before seeding the ID.",
            ],
            "llm_support": "Can normalize pasted Teams/OWA links, compare candidate IDs against registry titles, and explain confidence tradeoffs; operator must choose the final mapping.",
        },
    )

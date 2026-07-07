"""Seed-plan helpers for the integration command (D-13).

Extracted from the ``integration.py`` god module (§28.4 strangler fig): the
SeedPlanEntry value object and the logic that derives, deduplicates, renders,
and serializes the manual durable-ID seeding plan for unresolved source
intents. ``_collect_seed_plan_entries`` receives its SourceCandidateStore by
argument (no global state). ``integration.py`` re-imports these so its attribute
surface and call sites are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from src.core.discovery_intent import SourceIntentStatus, SourceRefKind
from src.core.source_candidate_store import SourceCandidateStore


@dataclass(frozen=True, slots=True)
class SeedPlanEntry:
    intent_id: str
    workstream_id: str
    ref_kind: str
    display_name: str
    derived_state: str
    latest_attempt_outcome: str | None
    latest_attempt_reason: str | None
    latest_attempted_at: str | None
    required_ref_field: str
    acceptable_seed_inputs: tuple[str, ...]
    graph_request_path: str
    seed_command: str
    evidence_hint: str


def _collect_seed_plan_entries(
    program: str,
    *,
    candidate_store: SourceCandidateStore,
) -> tuple[SeedPlanEntry, ...]:
    supported_manual_seed_kinds = {
        SourceRefKind.MEETING_SERIES,
        SourceRefKind.TEAMS_CHAT,
        SourceRefKind.TEAMS_CHANNEL,
    }
    # Local concept — not a migration target for TERMINAL_WORK_ITEM_STATES.
    unresolved_terminal_states = {
        SourceIntentStatus.DECLARED.value,
        SourceIntentStatus.SEARCHING.value,
        SourceIntentStatus.NO_CANDIDATES.value,
        SourceIntentStatus.CANDIDATE_FOUND.value,
        SourceIntentStatus.AMBIGUOUS.value,
        SourceIntentStatus.AUTH_BLOCKED.value,
        SourceIntentStatus.OUT_OF_IDENTITY_SCOPE.value,
    }
    entries_by_key: dict[tuple[str, str, str], SeedPlanEntry] = {}
    as_of = datetime.now(timezone.utc)
    for intent in candidate_store.list_intents():
        if intent.ref_kind not in supported_manual_seed_kinds:
            continue
        derived_state = candidate_store.derive_intent_state(intent.intent_id, as_of=as_of)
        if derived_state not in unresolved_terminal_states:
            continue
        latest_attempt = next(iter(candidate_store.get_attempts(intent.intent_id, exclude_expired=False)), None)
        required_ref_field, acceptable_seed_inputs, graph_request_path, evidence_hint = _seed_plan_lookup_hints(
            intent.ref_kind,
            display_name=intent.display_name,
        )
        entry = SeedPlanEntry(
            intent_id=intent.intent_id,
            workstream_id=intent.workstream_id,
            ref_kind=intent.ref_kind.value,
            display_name=intent.display_name,
            derived_state=derived_state,
            latest_attempt_outcome=latest_attempt.outcome.value if latest_attempt is not None else None,
            latest_attempt_reason=latest_attempt.reason if latest_attempt is not None else None,
            latest_attempted_at=latest_attempt.attempted_at.isoformat() if latest_attempt is not None else None,
            required_ref_field=required_ref_field,
            acceptable_seed_inputs=acceptable_seed_inputs,
            graph_request_path=graph_request_path,
            seed_command=(
                f"vertex integration seed-id --program {program} --intent-id {intent.intent_id} "
                "--ref-id <series-or-thread-id> --pm-alias <alias>"
            ),
            evidence_hint=evidence_hint,
        )
        dedupe_key = (
            entry.workstream_id,
            entry.display_name.strip().lower(),
            _seed_plan_ref_kind_group(intent.ref_kind),
        )
        existing = entries_by_key.get(dedupe_key)
        if existing is None or _seed_plan_ref_kind_priority(entry.ref_kind) < _seed_plan_ref_kind_priority(existing.ref_kind):
            entries_by_key[dedupe_key] = entry
    entries = sorted(entries_by_key.values(), key=lambda entry: (entry.workstream_id, entry.ref_kind, entry.display_name.lower()))
    return tuple(entries)


def _seed_plan_ref_kind_group(ref_kind: SourceRefKind) -> str:
    if ref_kind in {SourceRefKind.TEAMS_CHAT, SourceRefKind.TEAMS_CHANNEL}:
        return "teams_conversation"
    return ref_kind.value


def _seed_plan_ref_kind_priority(ref_kind_value: str) -> int:
    order = {
        SourceRefKind.MEETING_SERIES.value: 0,
        SourceRefKind.TEAMS_CHAT.value: 1,
        SourceRefKind.TEAMS_CHANNEL.value: 2,
    }
    return order.get(ref_kind_value, 99)


def _seed_plan_lookup_hints(
    ref_kind: SourceRefKind,
    *,
    display_name: str,
) -> tuple[str, tuple[str, ...], str, str]:
    encoded_display_name = quote(display_name)
    if ref_kind == SourceRefKind.MEETING_SERIES:
        return (
            "series_id",
            ("seriesMasterId", "Teams join URL", "numeric meeting code"),
            (
                "/v1.0/me/events?$top=100&$select=id,subject,webLink,joinWebUrl,onlineMeeting,seriesMasterId"
                f"&$search=\"{encoded_display_name}\""
            ),
            "Look up the recurring series or a recent occurrence, then seed the recurring seriesMasterId.",
        )
    if ref_kind in {SourceRefKind.TEAMS_CHAT, SourceRefKind.TEAMS_CHANNEL}:
        return (
            "thread_id",
            ("chat id", "conversation/thread id", "Teams chat permalink"),
            "/v1.0/chats?$top=100&$select=id,topic,webUrl",
            "Open the canonical Teams chat or channel thread and seed its durable chat/thread identifier.",
        )
    return (
        "thread_id",
        ("conversationId", "threadId", "Outlook message permalink"),
        (
            "/v1.0/me/messages?$top=100&$select=id,subject,conversationId,webLink"
            f"&$search=\"{encoded_display_name}\""
        ),
        "Find the canonical Outlook thread and seed the stable conversation/thread identifier.",
    )


def _seed_plan_entry_payload(entry: SeedPlanEntry) -> dict[str, Any]:
    return {
        "intent_id": entry.intent_id,
        "workstream_id": entry.workstream_id,
        "ref_kind": entry.ref_kind,
        "display_name": entry.display_name,
        "derived_state": entry.derived_state,
        "latest_attempt_outcome": entry.latest_attempt_outcome,
        "latest_attempt_reason": entry.latest_attempt_reason,
        "latest_attempted_at": entry.latest_attempted_at,
        "required_ref_field": entry.required_ref_field,
        "acceptable_seed_inputs": list(entry.acceptable_seed_inputs),
        "graph_request_path": entry.graph_request_path,
        "seed_command": entry.seed_command,
        "evidence_hint": entry.evidence_hint,
    }


def _render_seed_plan(entries: Sequence[SeedPlanEntry], *, program: str) -> str:
    if not entries:
        return f"No unresolved source intents currently require manual ID seeding for {program}."

    lines = [f"Found {len(entries)} unresolved source intents that may need manual ID seeding for {program}:"]
    for index, entry in enumerate(entries, start=1):
        lines.append(f"{index}. {entry.workstream_id} | {entry.ref_kind} | {entry.display_name}")
        lines.append(f"   Intent: {entry.intent_id}")
        lines.append(f"   State: {entry.derived_state}")
        if entry.latest_attempt_outcome is not None:
            attempted_at = entry.latest_attempted_at or "unknown time"
            lines.append(f"   Latest attempt: {entry.latest_attempt_outcome} at {attempted_at}")
        if entry.latest_attempt_reason:
            lines.append(f"   Reason: {entry.latest_attempt_reason}")
        lines.append(f"   Need: {entry.required_ref_field}")
        lines.append(f"   Acceptable seed inputs: {', '.join(entry.acceptable_seed_inputs)}")
        lines.append(f"   Lookup hint: {entry.evidence_hint}")
        lines.append(f"   Graph/REST starting point: GET {entry.graph_request_path}")
        lines.append(f"   Seed command: {entry.seed_command}")
    return "\n".join(lines)

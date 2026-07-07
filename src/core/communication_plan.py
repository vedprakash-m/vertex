from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommunicationPlanEntry:
    edition: str
    audience: str | None = None
    channel: str | None = None
    cadence: str | None = None
    owner: str | None = None


def load_communication_plan_entries(raw_program: dict[str, object]) -> tuple[CommunicationPlanEntry, ...]:
    plan = raw_program.get("communication_plan")
    if not isinstance(plan, list):
        return ()

    entries: list[CommunicationPlanEntry] = []
    for raw_entry in plan:
        if not isinstance(raw_entry, dict):
            continue
        edition = _normalize_optional_text(raw_entry.get("edition"))
        if edition is None:
            continue
        entries.append(
            CommunicationPlanEntry(
                edition=edition,
                audience=_normalize_optional_text(raw_entry.get("audience")),
                channel=_normalize_optional_text(raw_entry.get("channel")),
                cadence=_normalize_optional_text(raw_entry.get("cadence")),
                owner=_normalize_optional_text(raw_entry.get("owner")),
            )
        )
    return tuple(entries)


def describe_communication_plan_entry(entry: CommunicationPlanEntry) -> str | None:
    parts: list[str] = []
    if entry.audience is not None:
        parts.append(entry.audience)
    if entry.channel is not None:
        parts.append(f"via {entry.channel}")
    if entry.owner is not None:
        parts.append(f"owner {entry.owner}")
    if not parts:
        return None
    return "; ".join(parts)


def _normalize_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    return normalized or None
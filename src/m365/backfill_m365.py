from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.core.backfill_loader import BackfillConfig, BackfillDirection
from src.core.exceptions import StateError
from src.m365.agency_bridge import AgencyBridge


@dataclass(frozen=True, slots=True)
class DiscoveredM365Source:
    category: str
    label: str
    question: str
    source_id: str | None
    permalink: str | None
    summary: str | None


class M365Backfiller:
    """Discovers M365 backfill candidates from user-provided search directions."""

    def __init__(self, bridge: AgencyBridge) -> None:
        self._bridge = bridge

    def discover_all(
        self,
        config: BackfillConfig,
        *,
        since: date | None = None,
    ) -> dict[str, tuple[DiscoveredM365Source, ...]]:
        capabilities = self._bridge.probe()
        if not capabilities.available or not capabilities.has_workiq:
            raise StateError("Agency CLI WorkIQ access is unavailable. Use '--source offline' or install/enable Agency CLI.")

        return {
            "newsletters": self._discover_group("newsletters", config.newsletters.directions, since=since),
            "feedback": self._discover_group("feedback", config.feedback.directions, since=since),
            "meetings": self._discover_group("meetings", config.meetings.directions, since=since),
            "people_intelligence": self._discover_group(
                "people_intelligence",
                config.people_intelligence.directions,
                since=since,
            ),
        }

    def _discover_group(
        self,
        category: str,
        directions: tuple[BackfillDirection, ...],
        *,
        since: date | None,
    ) -> tuple[DiscoveredM365Source, ...]:
        discoveries: list[DiscoveredM365Source] = []
        for direction in directions:
            question = _direction_question(direction, since=since)
            payload = self._bridge.ask_workiq(question)
            discoveries.extend(_parse_payload(category=category, question=question, payload=payload))
        return _dedupe(discoveries)


def _direction_question(direction: BackfillDirection, *, since: date | None) -> str:
    if direction.question is not None:
        question = direction.question
    else:
        segments = []
        if direction.source is not None:
            segments.append(direction.source)
        if direction.filter is not None:
            segments.append(direction.filter)
        if direction.date_range is not None:
            segments.append(direction.date_range)
        if direction.description is not None:
            segments.append(direction.description)
        question = " | ".join(segments)
    if since is not None:
        question = f"{question} since {since.isoformat()}"
    return question.strip()


def _parse_payload(
    *,
    category: str,
    question: str,
    payload: dict[str, object] | None,
) -> list[DiscoveredM365Source]:
    if payload is None:
        return []

    records: list[dict[str, object]] = []
    for key in ("emails", "items", "results", "messages", "meetings", "threads"):
        value = payload.get(key)
        if isinstance(value, list):
            records.extend(item for item in value if isinstance(item, dict))

    if records:
        return [
            DiscoveredM365Source(
                category=category,
                label=_record_label(record),
                question=question,
                source_id=_optional_string(record.get("id") or record.get("messageId") or record.get("meetingId")),
                permalink=_optional_string(record.get("webUrl") or record.get("url") or record.get("link")),
                summary=_record_summary(record),
            )
            for record in records
        ]

    response = _optional_string(payload.get("response") or payload.get("summary"))
    if response is None:
        return []
    conversation_id = _optional_string(payload.get("conversationId"))
    return [
        DiscoveredM365Source(
            category=category,
            label=_truncate(response),
            question=question,
            source_id=conversation_id,
            permalink=None,
            summary=response,
        )
    ]


def _record_label(record: dict[str, object]) -> str:
    for key in ("subject", "title", "name", "bodyPreview", "preview", "snippet"):
        label = _optional_string(record.get(key))
        if label is not None:
            return _truncate(label)
    return "WorkIQ discovery result"


def _record_summary(record: dict[str, object]) -> str | None:
    for key in ("preview", "snippet", "bodyPreview", "description"):
        summary = _optional_string(record.get(key))
        if summary is not None:
            return _truncate(summary, limit=200)
    return None


def _dedupe(discoveries: list[DiscoveredM365Source]) -> tuple[DiscoveredM365Source, ...]:
    seen: dict[tuple[str, str, str], DiscoveredM365Source] = {}
    for discovery in discoveries:
        key = (
            discovery.category,
            discovery.source_id or discovery.permalink or discovery.label,
            discovery.question,
        )
        seen[key] = discovery
    return tuple(seen.values())


def _truncate(value: str, *, limit: int = 120) -> str:
    stripped = value.strip().replace("\n", " ")
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 3].rstrip() + "..."


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
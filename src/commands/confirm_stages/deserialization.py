"""Pure deserialization helpers for confirm draft state.

Extracted from ``src/commands/confirm.py`` (D-25 / Phase 3). These functions
convert persisted draft-state payload dictionaries back into core domain
objects (``WorkItem``, ``KustoSectionData``, ``Revision``, ``Comment``). They
are pure: no I/O, no global state, no mutation of inputs — which is why they are
safe to lift out of the confirm transaction module ahead of the riskier write
path. ``confirm.py`` imports the three entry points it uses
(``deserialize_items``, ``deserialize_kusto_sections``,
``parse_datetime_required``); the remaining helpers are internal to this module.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, cast

from src.core.models import Comment, Revision, RiskLevel, WorkItem
from src.core.view_models import KustoMetric, KustoSectionData, KustoTableCell


def deserialize_items(items_payload: tuple[dict[str, Any], ...]) -> tuple[WorkItem, ...]:
    return tuple(deserialize_work_item(item) for item in items_payload)


def deserialize_kusto_sections(payload: tuple[dict[str, Any], ...]) -> tuple[KustoSectionData, ...]:
    return tuple(
        KustoSectionData(
            section_id=str(section["section_id"]),
            title=str(section["title"]),
            query_id=str(section["query_id"]),
            render_mode=cast(Literal["table", "metric_highlight", "chart_image"], str(section["render_mode"])),
            source_label=str(section["source_label"]),
            confidence=str(section["confidence"]),
            columns=tuple(str(column) for column in section.get("columns", [])),
            rows=tuple(
                tuple(
                    KustoTableCell(text=str(cell.get("text", "")), href=cell.get("href"))
                    for cell in row
                )
                for row in section.get("rows", [])
            ),
            metrics=tuple(
                KustoMetric(label=str(metric["label"]), value=str(metric["value"]))
                for metric in section.get("metrics", [])
            ),
            image_data_url=section.get("image_data_url"),
            reference_url=section.get("reference_url"),
            caveats=tuple(str(caveat) for caveat in section.get("caveats", [])),
            message=section.get("message"),
            is_degraded=bool(section.get("is_degraded", False)),
        )
        for section in payload
    )


def deserialize_work_item(payload: dict[str, Any]) -> WorkItem:
    return WorkItem(
        id=int(payload["id"]),
        type=str(payload["type"]),
        title=str(payload["title"]),
        state=str(payload["state"]),
        assigned_to=optional_string(payload.get("assigned_to")),
        assigned_to_email=optional_string(payload.get("assigned_to_email")),
        area_path=str(payload["area_path"]),
        iteration_path=str(payload["iteration_path"]),
        target_date=parse_date_value(payload.get("target_date")),
        risk_level=RiskLevel.from_string(str(payload["risk_level"])),
        tags=[str(tag) for tag in payload.get("tags", [])],
        custom_fields=dict(payload.get("custom_fields", {})),
        revisions=[deserialize_revision(revision) for revision in payload.get("revisions", [])],
        comments=[deserialize_comment(comment) for comment in payload.get("comments", [])],
        fetched_at=parse_datetime_required(payload["fetched_at"]),
        risk_assessment=optional_string(payload.get("risk_assessment")),
        risk_assessment_comment=optional_string(payload.get("risk_assessment_comment")),
    )


def deserialize_revision(payload: dict[str, Any]) -> Revision:
    return Revision(
        work_item_id=int(payload["work_item_id"]),
        rev_number=int(payload["rev_number"]),
        changed_by=str(payload["changed_by"]),
        changed_by_email=str(payload["changed_by_email"]),
        changed_date=parse_datetime_required(payload["changed_date"]),
        fields_changed={
            str(field_name): (values[0], values[1])
            for field_name, values in payload.get("fields_changed", {}).items()
        },
    )


def deserialize_comment(payload: dict[str, Any]) -> Comment:
    return Comment(
        work_item_id=int(payload["work_item_id"]),
        comment_id=int(payload["comment_id"]),
        created_by=str(payload["created_by"]),
        created_by_email=str(payload["created_by_email"]),
        created_date=parse_datetime_required(payload["created_date"]),
        text=str(payload["text"]),
    )


def parse_datetime_required(value: Any) -> datetime:
    return datetime.fromisoformat(str(value))


def parse_date_value(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(str(value))


def optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)

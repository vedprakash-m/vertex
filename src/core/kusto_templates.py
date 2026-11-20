from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
import re
from typing import TypeVar

from src.core.exceptions import ConfigError


_T = TypeVar("_T")
_TEMPLATE_PATTERN = re.compile(r"\{([a-z_]+)\}")
_SUPPORTED_TEMPLATE_VARIABLES = frozenset({
    "program_id", "area_path", "date_range", "date_from", "date_to", "current_iteration_path",
})


class ExactWindowQueryError(ConfigError):
    """The selected query cannot bind the required retrospective window."""


@dataclass(frozen=True, slots=True)
class KustoTemplateContext:
    program_id: str
    area_paths: tuple[str, ...] = ()
    date_window_days: int | None = None
    current_iteration_path: str | None = None
    date_from: date | datetime | None = None
    date_to: date | datetime | None = None
    require_exact_window: bool = False


def render_kusto_query(query: _T, *, context: KustoTemplateContext) -> _T:
    query_id = getattr(query, "id", "<unknown>")
    engine = getattr(query, "engine", "kusto")
    query_text_field = "wiql" if engine == "wiql" and getattr(query, "wiql", None) else "kql"
    query_text = getattr(query, query_text_field, "")
    placeholders = set(_TEMPLATE_PATTERN.findall(query_text))
    if context.require_exact_window and (
        "date_range" in placeholders or not {"date_from", "date_to"} <= placeholders
    ):
        raise ExactWindowQueryError(
            f"Query '{query_id}' does not declare both {{date_from}} and {{date_to}} "
            "for the required exact retrospective window."
        )
    if not placeholders:
        return query

    unsupported = sorted(placeholders - _SUPPORTED_TEMPLATE_VARIABLES)
    if unsupported:
        joined = ", ".join(f"{{{name}}}" for name in unsupported)
        raise ConfigError(f"Query '{query_id}' uses unsupported template variable(s): {joined}.")

    replacements: dict[str, str] = {"program_id": context.program_id}
    if "date_range" in placeholders:
        if context.date_window_days is None:
            raise ConfigError(
                f"Query '{query_id}' uses {{date_range}} but the program does not define ado.date_window_days."
            )
        replacements["date_range"] = f"{context.date_window_days}d"
    exact_placeholders = placeholders & {"date_from", "date_to"}
    if exact_placeholders:
        if exact_placeholders != {"date_from", "date_to"}:
            raise ExactWindowQueryError(
                f"Query '{query_id}' must declare {{date_from}} and {{date_to}} together."
            )
        replacements["date_from"] = _render_exact_window_bound(
            context.date_from, field="date_from", query_id=query_id
        )
        replacements["date_to"] = _render_exact_window_bound(
            context.date_to, field="date_to", query_id=query_id
        )
        start = datetime.fromisoformat(replacements["date_from"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(replacements["date_to"].replace("Z", "+00:00"))
        if start > end:
            raise ExactWindowQueryError(
                f"Query '{query_id}' has an invalid exact retrospective window."
            )
    if "area_path" in placeholders:
        if not context.area_paths:
            raise ConfigError(
                f"Query '{query_id}' uses {{area_path}} but the program does not define any ado.area_paths."
            )
        if len(context.area_paths) != 1:
            raise ConfigError(
                f"Query '{query_id}' uses {{area_path}} but the program defines {len(context.area_paths)} ado.area_paths; author the query explicitly for multiple paths."
            )
        replacements["area_path"] = context.area_paths[0]
    if "current_iteration_path" in placeholders and context.current_iteration_path is not None:
        replacements["current_iteration_path"] = context.current_iteration_path

    rendered = query_text
    for name, value in replacements.items():
        rendered = rendered.replace(f"{{{name}}}", value)
    return replace(query, **{query_text_field: rendered})  # type: ignore[type-var]


def _render_exact_window_bound(
    value: date | datetime | None, *, field: str, query_id: str
) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ExactWindowQueryError(
                f"Query '{query_id}' {field} must include timezone information."
            )
        instant = value.astimezone(timezone.utc)
    elif isinstance(value, date):
        instant = datetime.combine(value, time.min, timezone.utc)
    else:
        raise ExactWindowQueryError(
            f"Query '{query_id}' requires an exact {field} snapshot bound."
        )
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")
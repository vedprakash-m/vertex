from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import TypeVar

from src.core.exceptions import ConfigError


_T = TypeVar("_T")
_TEMPLATE_PATTERN = re.compile(r"\{([a-z_]+)\}")
_SUPPORTED_TEMPLATE_VARIABLES = frozenset({"program_id", "area_path", "date_range", "current_iteration_path"})


@dataclass(frozen=True, slots=True)
class KustoTemplateContext:
    program_id: str
    area_paths: tuple[str, ...] = ()
    date_window_days: int | None = None
    current_iteration_path: str | None = None


def render_kusto_query(query: _T, *, context: KustoTemplateContext) -> _T:
    query_id = getattr(query, "id", "<unknown>")
    engine = getattr(query, "engine", "kusto")
    query_text_field = "wiql" if engine == "wiql" and getattr(query, "wiql", None) else "kql"
    query_text = getattr(query, query_text_field, "")
    placeholders = set(_TEMPLATE_PATTERN.findall(query_text))
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
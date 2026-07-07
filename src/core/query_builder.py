from __future__ import annotations

# Adapted from Shiproom src/ado/query_builder.py

from datetime import datetime, timezone


def _escape(value: str) -> str:
    return value.replace("'", "''")


class ODataFilter:
    def __init__(self) -> None:
        self._conditions: list[str] = []

    def raw(self, expression: str) -> "ODataFilter":
        self._conditions.append(expression)
        return self

    def in_area_paths(self, area_paths: list[str] | tuple[str, ...]) -> "ODataFilter":
        if not area_paths:
            return self
        conditions = [f"startswith(Area/AreaPath, '{_escape(path)}')" for path in area_paths]
        self._conditions.append(f"( {' or '.join(conditions)} )")
        return self

    def in_work_item_types(self, work_item_types: list[str] | tuple[str, ...]) -> "ODataFilter":
        if not work_item_types:
            return self
        conditions = [f"WorkItemType eq '{_escape(item_type)}'" for item_type in work_item_types]
        self._conditions.append(f"( {' or '.join(conditions)} )")
        return self

    def date_ge(self, field_name: str, value: datetime) -> "ODataFilter":
        normalized = value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._conditions.append(f"{field_name} ge {normalized}")
        return self

    def not_in_states(self, states: list[str] | tuple[str, ...]) -> "ODataFilter":
        if not states:
            return self
        conditions = [f"State eq '{_escape(state)}'" for state in states]
        self._conditions.append(f"not ( {' or '.join(conditions)} )")
        return self

    def build(self) -> str:
        return " and ".join(self._conditions)


def build_odata_filter(
    area_paths: list[str] | tuple[str, ...],
    work_item_types: list[str] | tuple[str, ...],
    since: datetime,
    states_excluded: list[str] | tuple[str, ...],
) -> str:
    return (
        ODataFilter()
        .in_area_paths(area_paths)
        .in_work_item_types(work_item_types)
        .date_ge("ChangedDate", since)
        .not_in_states(states_excluded)
        .build()
    )

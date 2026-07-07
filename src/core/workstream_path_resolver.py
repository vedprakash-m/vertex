from __future__ import annotations

from collections.abc import Iterable


def resolve_workstream_id_loose_longest(area_path: str | None, workstreams: Iterable[object]) -> str | None:
    if area_path is None:
        return None
    normalized_area = area_path.lower()
    matches = [
        workstream
        for workstream in workstreams
        if any(normalized_area.startswith(prefix.lower()) for prefix in _area_paths(workstream))
    ]
    if not matches:
        return None
    # Sort by longest prefix first. On a tie (multiple workstreams share the same area path),
    # prefer the root/parent workstream (shorter ID = less specific) so the result is stable
    # and semantically correct: items without a more specific area-path match belong to the
    # parent workstream, not an arbitrary sub-workstream.
    matches.sort(
        key=lambda workstream: (
            max(len(prefix) for prefix in _area_paths(workstream)),
            -len(_workstream_id(workstream) or ""),  # longer ID = higher specificity = lower priority on tie
        ),
        reverse=True,
    )
    return _workstream_id(matches[0])


def resolve_workstream_id_strict_longest(area_path: str | None, workstreams: Iterable[object]) -> str | None:
    if area_path is None:
        return None
    normalized_area = _normalize_area_path(area_path)
    if not normalized_area:
        return None
    best_match: tuple[int, str] | None = None
    for workstream in workstreams:
        for prefix in _area_paths(workstream):
            normalized_prefix = _normalize_area_path(prefix)
            if not normalized_prefix:
                continue
            if normalized_area == normalized_prefix or normalized_area.startswith(f"{normalized_prefix}\\"):
                workstream_id = _workstream_id(workstream)
                if workstream_id is None:
                    continue
                candidate = (len(normalized_prefix), workstream_id)
                if best_match is None or candidate[0] > best_match[0]:
                    best_match = candidate
    return None if best_match is None else best_match[1]


def _area_paths(workstream: object) -> tuple[str, ...]:
    raw_value = getattr(workstream, "area_paths", ())
    if not isinstance(raw_value, (tuple, list)):
        return ()
    return tuple(value for value in raw_value if isinstance(value, str))


def _workstream_id(workstream: object) -> str | None:
    value = getattr(workstream, "id", None)
    return value if isinstance(value, str) else None


def _normalize_area_path(value: str) -> str:
    return value.strip().lower().rstrip("\\")

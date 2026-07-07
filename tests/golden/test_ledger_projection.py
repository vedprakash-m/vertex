from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.ledger.event_log import EventEnvelope
from src.core.ledger.program_views import canonical_projection_dump, project_events_to_sqlite


GOLDEN_DIR = Path(__file__).resolve().parent / "ledger"


class GoldenFileMismatchError(AssertionError):
    pass


def _load_text(path: Path) -> str | None:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _save_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _compare_with_golden(path: Path, actual: str, update: bool) -> None:
    golden = _load_text(path)
    if update or golden is None:
        _save_text(path, actual)
        if golden is None:
            pytest.skip(f"Created new golden file: {path.name}")
        return

    if actual != golden:
        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=path.name,
                tofile="actual",
            )
        )
        raise GoldenFileMismatchError(
            f"Output does not match golden file: {path.name}\n\nDiff:\n{diff}"
        )


def _load_events() -> tuple[EventEnvelope, ...]:
    fixture_path = GOLDEN_DIR / "program_golden_log.jsonl"
    events = []
    for line in fixture_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        events.append(EventEnvelope.from_dict(json.loads(line)))
    return tuple(events)


def _render_projection(
    tmp_path: Path,
    *,
    as_of: datetime | None = None,
    knowledge_as_of: datetime | None = None,
) -> dict[str, list[dict[str, object]]]:
    projection_path = tmp_path / "golden_projection.sqlite3"
    project_events_to_sqlite(
        "acme",
        _load_events(),
        projection_path=projection_path,
        as_of=as_of,
        knowledge_as_of=knowledge_as_of,
    )
    return canonical_projection_dump(projection_path)


def _to_canonical_json(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def test_qg_dm_2_projection_golden(update_golden: bool, tmp_path: Path) -> None:
    # QG-DM-2: committed golden ledger log -> canonical projection dump.
    actual = _to_canonical_json(_render_projection(tmp_path))
    _compare_with_golden(GOLDEN_DIR / "program_golden_projection.json", actual, update_golden)


def test_qg_dm_2_bitemporal_slices_golden(update_golden: bool, tmp_path: Path) -> None:
    checkpoints = (
        {
            "name": "2021_world_time_current_knowledge",
            "as_of": datetime(2021, 12, 31, tzinfo=timezone.utc),
            "knowledge_as_of": datetime(2026, 12, 31, tzinfo=timezone.utc),
        },
        {
            "name": "2021_world_time_prior_knowledge",
            "as_of": datetime(2021, 12, 31, tzinfo=timezone.utc),
            "knowledge_as_of": datetime(2025, 12, 31, tzinfo=timezone.utc),
        },
        {
            "name": "2025_period_slice",
            "as_of": datetime(2025, 3, 31, tzinfo=timezone.utc),
            "knowledge_as_of": datetime(2025, 3, 31, tzinfo=timezone.utc),
        },
    )
    rendered = []
    for checkpoint in checkpoints:
        rendered.append(
            {
                "gate_id": "QG-DM-2",
                "checkpoint": checkpoint["name"],
                "projection": _render_projection(
                    tmp_path,
                    as_of=checkpoint["as_of"],
                    knowledge_as_of=checkpoint["knowledge_as_of"],
                ),
            }
        )
    actual = _to_canonical_json(rendered)
    _compare_with_golden(GOLDEN_DIR / "program_golden_bitemporal_slices.json", actual, update_golden)


def test_qg_dm_2_golden_log_order_independence(tmp_path: Path) -> None:
    events = _load_events()
    forward_path = tmp_path / "forward.sqlite3"
    reverse_path = tmp_path / "reverse.sqlite3"
    project_events_to_sqlite("acme", events, projection_path=forward_path)
    project_events_to_sqlite("acme", tuple(reversed(events)), projection_path=reverse_path)
    assert canonical_projection_dump(forward_path) == canonical_projection_dump(reverse_path)
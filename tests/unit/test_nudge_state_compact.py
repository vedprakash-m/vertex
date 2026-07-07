from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "nudge_state_compact.py"


_spec = importlib.util.spec_from_file_location("nudge_state_compact", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

compact_payload = _module.compact_payload
compact_state_file = _module.compact_state_file

from src.core.nudge_models import NUDGE_STATE_SCHEMA_VERSION  # noqa: E402


def test_compact_payload_removes_legacy_numeric_keys_and_preserves_prefixed_entries() -> None:
    compacted, removed = compact_payload(
        {
            "schema_version": "1.0",
            "901001": "2026-05-18T12:30:00+00:00",
            "item:901001": "2026-05-18T12:29:00+00:00",
            "freshness:901001": "2026-05-18T12:28:00+00:00",
            "owner:priya@example.com": "2026-05-18T12:32:00+00:00",
        }
    )

    assert removed == 1
    assert "901001" not in compacted
    assert compacted["schema_version"] == NUDGE_STATE_SCHEMA_VERSION
    assert compacted["item:901001"] == "2026-05-18T12:29:00+00:00"
    assert compacted["freshness:901001"] == "2026-05-18T12:28:00+00:00"
    assert compacted["owner:priya@example.com"] == "2026-05-18T12:32:00+00:00"


def test_compact_state_file_materializes_prefixed_entries_before_removing_legacy_key(tmp_path: Path) -> None:
    state_path = tmp_path / "nudge_state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "901001": "2026-05-18T12:30:00+00:00",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    result = compact_state_file(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert result.changed is True
    assert result.legacy_keys_removed == 1
    assert "901001" not in payload
    assert payload["item:901001"] == "2026-05-18T12:30:00+00:00"
    assert payload["freshness:901001"] == "2026-05-18T12:30:00+00:00"
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "nudge_state_migrate.py"


_spec = importlib.util.spec_from_file_location("nudge_state_migrate", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

migrate_payload = _module.migrate_payload
migrate_state_file = _module.migrate_state_file

from src.core.nudge_models import NUDGE_STATE_SCHEMA_VERSION  # noqa: E402


def test_migrate_payload_rewrites_legacy_numeric_keys_to_prefixed_namespaces() -> None:
    migrated, rewritten = migrate_payload(
        {
            "schema_version": "1.0",
            "901001": "2026-05-18T12:30:00+00:00",
            "item:901002": "2026-05-18T12:31:00+00:00",
            "freshness:901001": "2026-05-18T12:29:00+00:00",
            "owner:priya@example.com": "2026-05-18T12:32:00+00:00",
        }
    )

    assert rewritten == 1
    assert "901001" not in migrated
    assert migrated["schema_version"] == NUDGE_STATE_SCHEMA_VERSION
    assert migrated["item:901001"] == "2026-05-18T12:30:00+00:00"
    assert migrated["freshness:901001"] == "2026-05-18T12:29:00+00:00"
    assert migrated["item:901002"] == "2026-05-18T12:31:00+00:00"
    assert migrated["owner:priya@example.com"] == "2026-05-18T12:32:00+00:00"


def test_migrate_state_file_writes_dual_prefixed_keys_and_removes_legacy_key(tmp_path: Path) -> None:
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

    result = migrate_state_file(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert result.changed is True
    assert result.legacy_keys_rewritten == 1
    assert "901001" not in payload
    assert payload["item:901001"] == "2026-05-18T12:30:00+00:00"
    assert payload["freshness:901001"] == "2026-05-18T12:30:00+00:00"

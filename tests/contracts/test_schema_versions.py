from __future__ import annotations
from pathlib import Path

import json
from datetime import datetime, timezone

import pytest
import yaml

from src.core.nudge_models import NUDGE_STATE_SCHEMA_VERSION
from src.core.nudge_state_store import record_nudge_state


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAM_ROOT = REPO_ROOT / "programs" / "acme"
_PROGRAMS_EXIST = (PROGRAM_ROOT / "kpis.yaml").exists() and (PROGRAM_ROOT / "gather_state.json").exists()


@pytest.mark.skipif(not _PROGRAMS_EXIST, reason="Requires programs/ data")
def test_nova_schema_versions_match_contract() -> None:
    kpis_payload = yaml.safe_load((PROGRAM_ROOT / "kpis.yaml").read_text(encoding="utf-8")) or {}
    gather_payload = json.loads((PROGRAM_ROOT / "gather_state.json").read_text(encoding="utf-8"))

    assert kpis_payload.get("schema_version") == "1.0"
    assert gather_payload.get("schema_version") == "2.0"


def test_nudge_state_store_writes_schema_versioned_provenance_payload(tmp_path: Path) -> None:
    state_path = tmp_path / "nudge_state.json"

    record_nudge_state(
        state_path,
        item_ids=(901001,),
        triggered_at=datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc),
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert payload.get("schema_version") == NUDGE_STATE_SCHEMA_VERSION
    # Canonical item: key format (not bare numeric)
    assert payload.get("item:901001") == {
        "triggered_at": "2026-05-18T12:30:00+00:00",
        "origin": "generated",
        "run_id": None,
    }
    assert "901001" not in payload

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import portalocker
from typing import Any

from src.core.discovery_intent import SourceIntent


def append_intent_decision_log(
    program: str,
    *,
    programs_root: Path,
    payload: dict[str, Any],
) -> None:
    log_path = programs_root / program / "source_intent_decisions.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)


def intent_decision_payload(
    *,
    ts: datetime,
    intent: SourceIntent,
    action: str,
    actor_alias: str,
    old_status: str,
    new_status: str,
    reason: str | None,
    candidate_id: str | None = None,
    ref_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ts": ts.isoformat(),
        "intent_id": intent.intent_id,
        "workstream_id": intent.workstream_id,
        "action": action,
        "pm_alias": actor_alias,
        "ref_kind": intent.ref_kind.value,
        "old_status": old_status,
        "new_status": new_status,
        "reason": reason,
    }
    if candidate_id is not None:
        payload["candidate_id"] = candidate_id
    if ref_id is not None:
        payload["ref_id"] = ref_id
    return payload

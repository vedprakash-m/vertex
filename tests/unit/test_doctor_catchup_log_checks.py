from __future__ import annotations

import json

from src.commands.doctor_checks.catchup_log_checks import read_catchup_log_entries


def test_read_catchup_log_entries_ignores_non_catchup_events(tmp_path) -> None:
    usage_log = tmp_path / "demo" / "_feedback" / "usage_log.jsonl"
    usage_log.parent.mkdir(parents=True)
    usage_log.write_text(
        json.dumps({"event": "other_event", "reason": "skip"}) + "\n" + json.dumps({"event": "catchup_failed", "reason": "keep"}) + "\n",
        encoding="utf-8",
    )

    entries = read_catchup_log_entries("demo", programs_root=tmp_path)

    assert entries == ({"event": "catchup_failed", "recorded_at": "", "reason": "keep"},)

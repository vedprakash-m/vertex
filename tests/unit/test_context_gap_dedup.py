"""Tests for context_gap_store dedup feature (Phase 1 §8.1, deduplicate=True)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.context_gap_store import (
    _has_recent_gap,
    append_context_gap,
    load_context_gaps,
)


class TestContextGapDedup:
    def test_append_writes_once(self, tmp_path: Path):
        programs_root = tmp_path / "programs"
        append_context_gap(
            feature="nudge",
            program="nova",
            lane="priority",
            field="deadline_milestone_id",
            message="milestone not found",
            programs_root=programs_root,
        )
        gaps = load_context_gaps("nova", programs_root=programs_root)
        assert len(gaps) == 1

    def test_duplicate_within_window_suppressed(self, tmp_path: Path):
        programs_root = tmp_path / "programs"
        kwargs = dict(
            feature="nudge",
            program="nova",
            lane="priority",
            field="deadline_milestone_id",
            message="milestone not found",
            programs_root=programs_root,
        )
        append_context_gap(**kwargs)
        append_context_gap(**kwargs)  # duplicate — should be suppressed
        gaps = load_context_gaps("nova", programs_root=programs_root)
        assert len(gaps) == 1

    def test_different_field_not_suppressed(self, tmp_path: Path):
        programs_root = tmp_path / "programs"
        base = dict(feature="nudge", program="nova", lane="priority", programs_root=programs_root)
        append_context_gap(**base, field="deadline_milestone_id", message="m1")
        append_context_gap(**base, field="action_due_policy", message="m2")
        gaps = load_context_gaps("nova", programs_root=programs_root)
        assert len(gaps) == 2

    def test_different_lane_not_suppressed(self, tmp_path: Path):
        programs_root = tmp_path / "programs"
        base = dict(feature="nudge", program="nova", field="deadline_milestone_id", programs_root=programs_root)
        append_context_gap(**base, lane="priority", message="msg")
        append_context_gap(**base, lane="post_ramp", message="msg")
        gaps = load_context_gaps("nova", programs_root=programs_root)
        assert len(gaps) == 2

    def test_different_feature_not_suppressed(self, tmp_path: Path):
        programs_root = tmp_path / "programs"
        base = dict(program="nova", lane=None, field="somefield", programs_root=programs_root)
        append_context_gap(**base, feature="nudge", message="msg")
        append_context_gap(**base, feature="report", message="msg")
        gaps = load_context_gaps("nova", programs_root=programs_root)
        assert len(gaps) == 2

    def test_deduplicate_false_always_writes(self, tmp_path: Path):
        programs_root = tmp_path / "programs"
        kwargs = dict(
            feature="nudge",
            program="nova",
            lane=None,
            field="test",
            message="msg",
            programs_root=programs_root,
            deduplicate=False,
        )
        append_context_gap(**kwargs)
        append_context_gap(**kwargs)
        gaps = load_context_gaps("nova", programs_root=programs_root)
        assert len(gaps) == 2

    @pytest.mark.parametrize("deduplicate", [False, True])
    def test_append_preserves_unterminated_existing_record(self, tmp_path: Path, deduplicate: bool):
        programs_root = tmp_path / "programs"
        base = dict(feature="nudge", program="nova", lane="priority", programs_root=programs_root)
        path = append_context_gap(**base, field="first", message="original \u03a9")
        original = path.read_bytes().rstrip(b"\r\n")
        path.write_bytes(original)

        append_context_gap(**base, field="second", message="new", deduplicate=deduplicate)

        assert path.read_bytes().startswith(original + b"\n")
        gaps = load_context_gaps("nova", programs_root=programs_root)
        assert [(gap.field, gap.message) for gap in gaps] == [
            ("first", "original \u03a9"), ("second", "new"),
        ]

    def test_no_existing_file_no_recent_gap(self, tmp_path: Path):
        path = tmp_path / "programs" / "nova" / "_feedback" / "context_gaps.jsonl"
        since = datetime.now(timezone.utc).timestamp() - 86400
        assert _has_recent_gap(path, "nudge", None, "test", since_ts=since) is False

    def test_has_recent_gap_detects_match(self, tmp_path: Path):
        programs_root = tmp_path / "programs"
        append_context_gap(
            feature="nudge",
            program="nova",
            lane="s1",
            field="deadline",
            message="m",
            programs_root=programs_root,
            deduplicate=False,
        )
        path = programs_root / "nova" / "_feedback" / "context_gaps.jsonl"
        since = datetime.now(timezone.utc).timestamp() - 86400
        assert _has_recent_gap(path, "nudge", "s1", "deadline", since_ts=since) is True

    def test_has_recent_gap_no_match_different_field(self, tmp_path: Path):
        programs_root = tmp_path / "programs"
        append_context_gap(
            feature="nudge",
            program="nova",
            lane=None,
            field="field_a",
            message="m",
            programs_root=programs_root,
            deduplicate=False,
        )
        path = programs_root / "nova" / "_feedback" / "context_gaps.jsonl"
        since = datetime.now(timezone.utc).timestamp() - 86400
        assert _has_recent_gap(path, "nudge", None, "field_b", since_ts=since) is False


def test_dedup_check_and_append_are_atomic_under_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C.166: a concurrent writer committing between check and append must not yield a duplicate."""
    import json

    import src.core.context_gap_store as store

    programs_root = tmp_path / "programs"
    path = programs_root / "nova" / "_feedback" / "context_gaps.jsonl"
    concurrent = {
        "ts": datetime.now(timezone.utc).isoformat(), "feature": "nudge", "program": "nova",
        "lane": "priority", "field": "deadline", "severity": "quality_degraded",
        "message": "other writer", "impact_estimate": "medium",
    }
    real_lock = store.portalocker.lock
    injected = {"done": False}

    def lock_after_concurrent_commit(handle, flags):
        if not injected["done"]:
            injected["done"] = True
            with open(path, "ab") as other:
                other.write((json.dumps(concurrent) + "\n").encode("utf-8"))
        return real_lock(handle, flags)

    monkeypatch.setattr(store.portalocker, "lock", lock_after_concurrent_commit)
    append_context_gap(
        feature="nudge", program="nova", lane="priority", field="deadline",
        message="this writer", programs_root=programs_root,
    )
    assert len(load_context_gaps("nova", programs_root=programs_root)) == 1

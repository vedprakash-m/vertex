"""Tests for --mark-sent and --sent-at CLI options in vertex nudge."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.exceptions import Exit as ClickExit

from src.commands.nudge import _approval_index_path, _cmd_approve_draft, _cmd_import_sent, _cmd_mark_sent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_nudge_paths(programs_root: Path, program_id: str = "nova"):
    """Create nudge paths rooted inside programs_root/program_id (matching real layout)."""
    base = programs_root / program_id / "nudge"
    np = MagicMock()
    np.drafts_dir = base / "drafts"
    np.published_eml_dir = base / "published_eml"
    np.published_eml_index_path = base / "published_eml" / "index.json"
    np.audit_path = base / "nudge_audit.jsonl"
    np.audit_lock_path = base / "nudge_audit.jsonl.lock"
    np.state_path = base / "nudge_state.json"
    return np


def _setup(tmp_path: Path, run_id: str, program_id: str = "nova"):
    programs_root = tmp_path / "programs"
    np = _make_nudge_paths(programs_root, program_id)
    np.drafts_dir.mkdir(parents=True, exist_ok=True)
    draft = np.drafts_dir / f"{run_id}.eml"
    draft.write_text("MIME-Version: 1.0\nSubject: Test\n\nBody", encoding="utf-8")
    return programs_root, np


# ---------------------------------------------------------------------------
# _cmd_mark_sent — basic
# ---------------------------------------------------------------------------


class TestCmdMarkSent:
    def test_copies_draft_to_published(self, tmp_path: Path):
        programs_root, np = _setup(tmp_path, "run-001")

        with patch("src.commands.nudge._append_audit_fail_loud"):
            _cmd_mark_sent("nova", draft_ref="run-001", nudge_paths=np, programs_root=programs_root)

        dest = np.published_eml_dir / "run-001.eml"
        assert dest.exists()

    def test_writes_index_with_timestamps(self, tmp_path: Path):
        programs_root, np = _setup(tmp_path, "run-002")

        with patch("src.commands.nudge._append_audit_fail_loud"):
            _cmd_mark_sent("nova", draft_ref="run-002", nudge_paths=np, programs_root=programs_root)

        index = json.loads(np.published_eml_index_path.read_text(encoding="utf-8"))
        assert isinstance(index, list) and len(index) == 1
        entry = index[0]
        assert "marked_sent_at" in entry
        assert "claimed_sent_at" in entry

    def test_sent_at_override_stored_in_claimed_sent_at(self, tmp_path: Path):
        programs_root, np = _setup(tmp_path, "run-003")
        override_ts = datetime(2026, 6, 20, 9, 0, 0, tzinfo=timezone.utc)

        with patch("src.commands.nudge._append_audit_fail_loud"):
            _cmd_mark_sent(
                "nova", draft_ref="run-003", nudge_paths=np,
                programs_root=programs_root, sent_at_override=override_ts,
            )

        index = json.loads(np.published_eml_index_path.read_text(encoding="utf-8"))
        entry = index[0]
        assert "2026-06-20" in entry["claimed_sent_at"]
        # marked_sent_at reflects when --mark-sent was run; claimed_sent_at is the override
        assert "claimed_sent_at" in entry

    def test_draft_ref_accepts_run_id_without_eml_suffix(self, tmp_path: Path):
        programs_root, np = _setup(tmp_path, "run-004")

        with patch("src.commands.nudge._append_audit_fail_loud"):
            _cmd_mark_sent("nova", draft_ref="run-004", nudge_paths=np, programs_root=programs_root)

        assert (np.published_eml_dir / "run-004.eml").exists()

    def test_missing_draft_raises_exit(self, tmp_path: Path):
        programs_root = tmp_path / "programs"
        np = _make_nudge_paths(programs_root)
        np.drafts_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises((SystemExit, ClickExit)):
            _cmd_mark_sent("nova", draft_ref="nonexistent", nudge_paths=np, programs_root=programs_root)

    def test_default_claimed_sent_at_is_now_utc(self, tmp_path: Path):
        programs_root, np = _setup(tmp_path, "run-005")
        before = datetime.now(timezone.utc)

        with patch("src.commands.nudge._append_audit_fail_loud"):
            _cmd_mark_sent("nova", draft_ref="run-005", nudge_paths=np, programs_root=programs_root)

        after = datetime.now(timezone.utc)
        index = json.loads(np.published_eml_index_path.read_text(encoding="utf-8"))
        claimed_ts = datetime.fromisoformat(index[0]["claimed_sent_at"].replace("Z", "+00:00"))
        assert before <= claimed_ts <= after

    def test_sent_at_none_does_not_change_default_behavior(self, tmp_path: Path):
        programs_root, np = _setup(tmp_path, "run-006")
        before = datetime.now(timezone.utc)

        with patch("src.commands.nudge._append_audit_fail_loud"):
            _cmd_mark_sent(
                "nova", draft_ref="run-006", nudge_paths=np,
                programs_root=programs_root, sent_at_override=None,
            )

        after = datetime.now(timezone.utc)
        index = json.loads(np.published_eml_index_path.read_text(encoding="utf-8"))
        claimed_ts = datetime.fromisoformat(index[0]["claimed_sent_at"].replace("Z", "+00:00"))
        assert before <= claimed_ts <= after

    def test_mark_sent_blocks_when_approval_is_required_but_missing(self, tmp_path: Path):
        programs_root, np = _setup(tmp_path, "run-007")

        with patch("src.commands.nudge._approval_required_for_manifest", return_value=True), patch(
            "src.commands.nudge._append_audit_fail_loud"
        ):
            with pytest.raises((SystemExit, ClickExit)):
                _cmd_mark_sent("nova", draft_ref="run-007", nudge_paths=np, programs_root=programs_root)

    def test_mark_sent_succeeds_when_required_approval_exists(self, tmp_path: Path):
        programs_root, np = _setup(tmp_path, "run-008")

        with patch("src.commands.nudge._append_audit"), patch("src.commands.nudge.append_nudge_event"):
            _cmd_approve_draft("nova", draft_ref="run-008", nudge_paths=np, programs_root=programs_root)

        with patch("src.commands.nudge._approval_required_for_manifest", return_value=True), patch(
            "src.commands.nudge._append_audit_fail_loud"
        ), patch("src.commands.nudge.append_nudge_event"):
            _cmd_mark_sent("nova", draft_ref="run-008", nudge_paths=np, programs_root=programs_root)

        assert (np.published_eml_dir / "run-008.eml").exists()


class TestDraftApproval:
    def test_approve_draft_writes_approval_index(self, tmp_path: Path):
        programs_root, np = _setup(tmp_path, "run-approve-001")

        with patch("src.commands.nudge._append_audit"), patch("src.commands.nudge.append_nudge_event"):
            _cmd_approve_draft("nova", draft_ref="run-approve-001", nudge_paths=np, programs_root=programs_root)

        approval_index = _approval_index_path(np)
        payload = json.loads(approval_index.read_text(encoding="utf-8"))
        assert len(payload) == 1
        assert payload[0]["filename"] == "run-approve-001.eml"
        assert "content_hash" in payload[0]
        assert "approved_at" in payload[0]


class TestImportSent:
    def test_import_sent_updates_state_and_index(self, tmp_path: Path):
        programs_root = tmp_path / "programs"
        np = _make_nudge_paths(programs_root)
        np.published_eml_dir.mkdir(parents=True, exist_ok=True)
        published = np.published_eml_dir / "run-import-001.eml"
        published.write_text(
            "To: owner1@microsoft.com\nCc: tpm@microsoft.com\nSubject: Imported\n\nBody",
            encoding="utf-8",
        )

        with patch("src.commands.nudge._append_audit"), patch("src.commands.nudge.append_nudge_event"):
            _cmd_import_sent("nova", published_ref="run-import-001", nudge_paths=np, programs_root=programs_root)

        state_payload = json.loads(np.state_path.read_text(encoding="utf-8"))
        assert state_payload["schema_version"] == "1.2"

        index_payload = json.loads(np.published_eml_index_path.read_text(encoding="utf-8"))
        assert len(index_payload) == 1
        assert index_payload[0]["filename"] == "run-import-001.eml"
        assert index_payload[0]["note"] == "imported from published_eml"

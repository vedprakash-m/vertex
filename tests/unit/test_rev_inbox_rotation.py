"""P2-14 ``processed/`` directory rotation tests.

Exercises ``rotate_processed_dir`` (Zone-A pure) directly + the
``vertex rev rotate-processed`` CLI command + the automatic end-of-cycle
rotation wired into ``run_rev_cycle``.

Retention rule: a file is rotated to ``processed/archive/`` when its mtime is
older than ``max_age_days`` **or** it is among the oldest surplus beyond
``max_count``. Sidecar JSONL rotation (grounding_missed / attachment_denied) is
already covered by ``append_jsonl_line(max_bytes=...)`` and is not re-tested
here.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from src.core.rev.inbox_rotation import (
    DEFAULT_PROCESSED_MAX_AGE_DAYS,
    rotate_processed_dir,
)
from src.commands.rev import app as rev_app

runner = CliRunner()
NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)
_NOW_EPOCH = NOW.timestamp()


def _touch(processed: Path, name: str, *, age_days: float = 0) -> Path:
    p = processed / name
    p.write_text("x", encoding="utf-8")
    if age_days:
        ts = _NOW_EPOCH - age_days * 86400
        os.utime(p, (ts, ts))
    return p


class TestRotateProcessedDir:
    def test_age_rotation_moves_old_files_to_archive(self, tmp_path: Path) -> None:
        processed = tmp_path / "processed"
        processed.mkdir()
        _touch(processed, "old1.eml", age_days=DEFAULT_PROCESSED_MAX_AGE_DAYS + 5)
        _touch(processed, "old2.eml", age_days=DEFAULT_PROCESSED_MAX_AGE_DAYS + 1)
        _touch(processed, "fresh.eml", age_days=1)
        moved = rotate_processed_dir(processed, now_epoch=_NOW_EPOCH)
        assert moved == 2
        archive = processed / "archive"
        assert (archive / "old1.eml").is_file()
        assert (archive / "old2.eml").is_file()
        # Fresh file stays in processed/.
        assert (processed / "fresh.eml").is_file()

    def test_count_rotation_moves_oldest_surplus(self, tmp_path: Path) -> None:
        processed = tmp_path / "processed"
        processed.mkdir()
        # 5 files, all fresh; max_count=3 → 2 oldest surplus rotated.
        # age_days=5-i → f0 oldest (age 5), f4 newest (age 1).
        for i in range(5):
            _touch(processed, f"f{i}.eml", age_days=5 - i)
        moved = rotate_processed_dir(
            processed, max_age_days=10_000, max_count=3, now_epoch=_NOW_EPOCH,
        )
        assert moved == 2
        archive = processed / "archive"
        # Oldest two (f0, f1) rotated; newest three stay.
        assert (archive / "f0.eml").is_file()
        assert (archive / "f1.eml").is_file()
        assert (processed / "f2.eml").is_file()
        assert (processed / "f3.eml").is_file()
        assert (processed / "f4.eml").is_file()

    def test_no_rotation_when_within_bounds(self, tmp_path: Path) -> None:
        processed = tmp_path / "processed"
        processed.mkdir()
        _touch(processed, "a.eml", age_days=1)
        _touch(processed, "b.eml", age_days=2)
        moved = rotate_processed_dir(processed, now_epoch=_NOW_EPOCH)
        assert moved == 0
        assert not (processed / "archive").exists()

    def test_missing_dir_returns_zero(self, tmp_path: Path) -> None:
        moved = rotate_processed_dir(tmp_path / "nope")
        assert moved == 0

    def test_archive_subdir_excluded_from_count(self, tmp_path: Path) -> None:
        """Files already in archive/ must not be counted toward max_count."""
        processed = tmp_path / "processed"
        processed.mkdir()
        archive = processed / "archive"
        archive.mkdir()
        (archive / "already.eml").write_text("x", encoding="utf-8")
        # 2 fresh files in processed/ + 1 in archive/; max_count=500 → none rotated.
        _touch(processed, "a.eml", age_days=1)
        _touch(processed, "b.eml", age_days=2)
        moved = rotate_processed_dir(processed, now_epoch=_NOW_EPOCH)
        assert moved == 0

    def test_name_collision_timestamped(self, tmp_path: Path) -> None:
        """A previously-rotated file with the same name must not be overwritten."""
        processed = tmp_path / "processed"
        processed.mkdir()
        archive = processed / "archive"
        archive.mkdir()
        (archive / "dup.eml").write_text("original", encoding="utf-8")
        _touch(processed, "dup.eml", age_days=DEFAULT_PROCESSED_MAX_AGE_DAYS + 10)
        moved = rotate_processed_dir(processed, now_epoch=_NOW_EPOCH)
        assert moved == 1
        # Original archived file preserved; new arrival timestamped.
        assert (archive / "dup.eml").read_text() == "original"
        archived_new = [p for p in archive.iterdir() if p.name.startswith("dup.") and p.name != "dup.eml"]
        assert len(archived_new) == 1

    def test_invalid_args_raise(self, tmp_path: Path) -> None:
        processed = tmp_path / "processed"
        processed.mkdir()
        import pytest

        with pytest.raises(ValueError):
            rotate_processed_dir(processed, max_age_days=0)
        with pytest.raises(ValueError):
            rotate_processed_dir(processed, max_count=0)


class TestRotateProcessedCli:
    def test_cli_rotates_and_reports_count(self, tmp_path: Path) -> None:
        inbox = tmp_path / "rev_inbox"
        processed = inbox / "processed"
        processed.mkdir(parents=True)
        _touch(processed, "old.eml", age_days=DEFAULT_PROCESSED_MAX_AGE_DAYS + 5)
        result = runner.invoke(
            rev_app,
            ["rotate-processed", "--program", "p-cli", "--eml-inbox", str(inbox),
             "--max-age-days", "1", "--programs-root", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "Rotated 1" in result.output
        assert (processed / "archive" / "old.eml").is_file()

    def test_cli_default_inbox_path(self, tmp_path: Path) -> None:
        # No --eml-inbox → defaults to programs/<program>/rev_inbox.
        program_id = "p-default"
        processed = tmp_path / program_id / "rev_inbox" / "processed"
        processed.mkdir(parents=True)
        _touch(processed, "old.eml", age_days=DEFAULT_PROCESSED_MAX_AGE_DAYS + 5)
        result = runner.invoke(
            rev_app,
            ["rotate-processed", "--program", program_id,
             "--max-age-days", "1", "--programs-root", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert (processed / "archive" / "old.eml").is_file()

    def test_cli_accepts_ics_inbox_override(self, tmp_path: Path) -> None:
        """ADF-W3.2 (Section 8.6.4): CLI parity -- ``rotate-processed`` must
        accept the same ``--ics-inbox`` override ``rev run``/``init-inbox``
        already accept; rotation itself is format-agnostic."""
        inbox = tmp_path / "cal_inbox"
        processed = inbox / "processed"
        processed.mkdir(parents=True)
        _touch(processed, "old.ics", age_days=DEFAULT_PROCESSED_MAX_AGE_DAYS + 5)
        result = runner.invoke(
            rev_app,
            ["rotate-processed", "--program", "p-ics", "--ics-inbox", str(inbox),
             "--max-age-days", "1", "--programs-root", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "Rotated 1" in result.output
        assert (processed / "archive" / "old.ics").is_file()

    def test_cli_accepts_docs_inbox_override(self, tmp_path: Path) -> None:
        """ADF-W3.2 (Section 8.6.4): CLI parity -- ``rotate-processed`` must
        accept the same ``--docs-inbox`` override ``rev run``/``init-inbox``
        already accept."""
        inbox = tmp_path / "docs_inbox"
        processed = inbox / "processed"
        processed.mkdir(parents=True)
        _touch(processed, "old.docx", age_days=DEFAULT_PROCESSED_MAX_AGE_DAYS + 5)
        result = runner.invoke(
            rev_app,
            ["rotate-processed", "--program", "p-docs", "--docs-inbox", str(inbox),
             "--max-age-days", "1", "--programs-root", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "Rotated 1" in result.output
        assert (processed / "archive" / "old.docx").is_file()


class TestAutoRotationInCycle:
    """The pipeline rotates processed/ automatically at the end of each cycle."""

    def test_cycle_auto_rotates_old_processed_file(self, tmp_path: Path) -> None:
        # Use the full EML local-import path so the enumerator exposes processed_dir().
        from src.ai.rev.extractor import DeterministicRevExtractor
        from src.ai.rev.verification import run_layered_verification
        from src.core.models_v2 import REV_PROFILE_SEARCH_HYDRATE, RevRetrievalProfile
        from src.core.rev.entity_types import EntityType
        from src.core.rev.governor import BudgetLimits
        from src.core.rev.pipeline import RevPipelineDeps, run_rev_cycle
        from src.core.rev.prompt_shields import LocalOnlyPromptShields
        from src.core.rev.query_planner import RetrievalIntent
        from src.m365.rev.eml_enumerator import EmlEnumerator
        from src.m365.rev.eml_hydrator import EmlHydrator

        program_id = "p-auto-rot"
        inbox = tmp_path / program_id / "rev_inbox"
        processed = inbox / "processed"
        processed.mkdir(parents=True)
        # A stale file left from a prior cycle, beyond the 90d window.
        stale = processed / "stale.eml"
        stale.write_text("x", encoding="utf-8")
        old_ts = time.time() - (DEFAULT_PROCESSED_MAX_AGE_DAYS + 5) * 86400
        os.utime(stale, (old_ts, old_ts))

        mailbox = type("M", (), {"tenant_id": "t", "principal_mailbox": "u@x.com", "container": "inbox"})()
        deps = RevPipelineDeps(
            enumerator=EmlEnumerator(inbox_root=inbox, mailbox_tenant_id="t",
                                      principal_mailbox="u@x.com", container="inbox"),
            hydrator=EmlHydrator(mailbox_tenant_id="t", principal_mailbox="u@x.com",
                                 container="inbox"),
            shields=LocalOnlyPromptShields(),
            extractor=DeterministicRevExtractor(),
            verifier=lambda **kw: run_layered_verification(**kw).effective_state,
        )
        intent = RetrievalIntent(entity_type=EntityType.MESSAGE, limit=25)
        run_rev_cycle(
            program_id=program_id,
            intent=intent,
            deps=deps,
            profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
            mailbox_tenant_id="t", mailbox_principal="u@x.com", mailbox_container="inbox",
            correlation_id="auto-rot",
            programs_root=tmp_path,
            budget_limits=BudgetLimits(),
            set_at=NOW,
        )
        # The stale file was rotated to processed/archive/ by the cycle.
        assert (processed / "archive" / "stale.eml").is_file()
        assert not (processed / "stale.eml").exists()
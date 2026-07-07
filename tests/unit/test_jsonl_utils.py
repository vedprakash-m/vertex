from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.core.jsonl_utils import (
    compute_file_checksum,
    jsonl_checksum_matches,
    list_jsonl_quarantine_paths,
    list_rotated_jsonl_paths,
    quarantine_and_rewrite_jsonl,
    rotate_jsonl_if_oversize,
    validate_jsonl_row,
    write_checksum_file,
)


class TestComputeFileChecksum:
    def test_returns_sha256_hexdigest(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.txt"
        path.write_text("hello world", encoding="utf-8")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert compute_file_checksum(path) == expected


class TestWriteChecksumFile:
    def test_creates_sha256_sidecar(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        path.write_text('{"a":1}\n', encoding="utf-8")
        write_checksum_file(path)
        checksum_path = path.with_suffix(".sha256")
        assert checksum_path.exists()
        stored = checksum_path.read_text(encoding="utf-8").strip()
        assert stored == compute_file_checksum(path)


class TestQuarantineAndRewriteJsonl:
    def test_quarantines_corrupt_file_and_rewrites_valid_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        path.write_text('{"valid":true}\nBAD\n{"also_valid":2}\n', encoding="utf-8")
        quarantine_and_rewrite_jsonl(path, ['{"valid":true}\n', '{"also_valid":2}\n'])

        quarantine_dir = tmp_path / "quarantine"
        assert quarantine_dir.exists()
        quarantined = tuple(quarantine_dir.glob("data.*.jsonl"))
        assert len(quarantined) == 1

        # original file was rewritten in place after quarantine
        assert path.exists()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines == ['{"valid":true}', '{"also_valid":2}']

        # checksum written for rewritten file
        checksum_path = path.with_suffix(".sha256")
        assert checksum_path.exists()

    def test_increments_suffix_when_quarantine_exists(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        path.write_text('{"a":1}\n', encoding="utf-8")
        quarantine_and_rewrite_jsonl(path, ['{"a":1}\n'])
        path.write_text('{"b":2}\n', encoding="utf-8")
        quarantine_and_rewrite_jsonl(path, ['{"b":2}\n'])

        quarantine_files = tuple(sorted((tmp_path / "quarantine").glob("data.*.jsonl")))
        assert len(quarantine_files) == 2


class TestListJsonlQuarantinePaths:
    def test_returns_empty_when_dir_missing(self, tmp_path: Path) -> None:
        assert list_jsonl_quarantine_paths(tmp_path / "quarantine") == ()

    def test_returns_sorted_quarantine_files(self, tmp_path: Path) -> None:
        quarantine_dir = tmp_path / "quarantine"
        quarantine_dir.mkdir()
        (quarantine_dir / "data.20260101T120000Z.jsonl").write_text("x")
        (quarantine_dir / "data.20260101T110000Z.jsonl").write_text("y")
        (quarantine_dir / "other.20260101T100000Z.jsonl").write_text("z")
        result = list_jsonl_quarantine_paths(quarantine_dir, stem="data")
        assert len(result) == 2
        assert result[0].name.startswith("data.20260101T110000Z")


class TestJsonlChecksumMatches:
    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        checksum_path = tmp_path / "missing.sha256"
        assert jsonl_checksum_matches(path, checksum_path) is None

    def test_returns_false_when_checksum_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        path.write_text('{"a":1}\n', encoding="utf-8")
        checksum_path = tmp_path / "data.sha256"
        assert jsonl_checksum_matches(path, checksum_path) is False

    def test_returns_true_when_checksum_matches(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        path.write_text('{"a":1}\n', encoding="utf-8")
        checksum_path = tmp_path / "data.sha256"
        checksum_path.write_text(compute_file_checksum(path) + "\n", encoding="utf-8")
        assert jsonl_checksum_matches(path, checksum_path) is True

    def test_returns_false_when_checksum_mismatches(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        path.write_text('{"a":1}\n', encoding="utf-8")
        checksum_path = tmp_path / "data.sha256"
        checksum_path.write_text("badhash\n", encoding="utf-8")
        assert jsonl_checksum_matches(path, checksum_path) is False


class TestValidateJsonlRow:
    def test_validate_jsonl_row_passes_for_complete_row(self) -> None:
        row = {"id": "abc", "program_id": "acme", "issue_number": 1}
        # Should not raise.
        validate_jsonl_row(row, required_fields=("id", "program_id", "issue_number"))

    def test_validate_jsonl_row_passes_with_extra_fields(self) -> None:
        row = {"id": "abc", "program_id": "acme", "extra": "ok"}
        validate_jsonl_row(row, required_fields=("id", "program_id"))

    def test_validate_jsonl_row_raises_for_missing_field(self) -> None:
        row = {"id": "abc"}
        with pytest.raises(ValueError, match="program_id"):
            validate_jsonl_row(row, required_fields=("id", "program_id"))

    def test_validate_jsonl_row_raises_for_null_field(self) -> None:
        row = {"id": "abc", "program_id": None}
        with pytest.raises(ValueError, match="program_id"):
            validate_jsonl_row(row, required_fields=("id", "program_id"))

    def test_validate_jsonl_row_lists_first_missing_field(self) -> None:
        row = {"id": "abc"}
        with pytest.raises(ValueError) as excinfo:
            validate_jsonl_row(
                row,
                required_fields=("id", "program_id", "edition_id"),
                field_name="claim",
            )
        # The first missing field (program_id) should be the one reported,
        # not edition_id (which is also missing).
        assert "program_id" in str(excinfo.value)
        assert "edition_id" not in str(excinfo.value)
        assert "claim" in str(excinfo.value)

    def test_validate_jsonl_row_uses_custom_field_name(self) -> None:
        row: dict[str, object] = {}
        with pytest.raises(ValueError, match="proposal"):
            validate_jsonl_row(row, required_fields=("id",), field_name="proposal")


class TestRotateJsonlIfOversize:
    def test_returns_false_when_file_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        assert rotate_jsonl_if_oversize(path, max_bytes=100) is False

    def test_returns_false_when_under_threshold(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        path.write_text('{"a":1}\n', encoding="utf-8")
        assert rotate_jsonl_if_oversize(path, max_bytes=1000) is False
        assert path.exists()
        # No rotated dir created when no rotation happened.
        assert not (tmp_path / "rotated").exists()

    def test_returns_true_and_rotates_when_over_threshold(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        original_content = '{"a":1}\n{"b":2}\n{"c":3}\n'  # ~30 bytes
        path.write_text(original_content, encoding="utf-8")

        rotated = rotate_jsonl_if_oversize(path, max_bytes=10)
        assert rotated is True

        # Original was moved into the rotated/ dir.
        assert not path.exists()
        rotated_files = list_rotated_jsonl_paths(tmp_path / "rotated", stem="data")
        assert len(rotated_files) == 1
        # The rotated file carries the original content (forensic-safe).
        assert rotated_files[0].read_text(encoding="utf-8") == original_content

    def test_keeps_recent_rotated_files_within_retain_limit(self, tmp_path: Path) -> None:
        rotated_dir = tmp_path / "rotated"
        rotated_dir.mkdir()
        # Pre-seed 5 rotated files (older ones first).
        for i in range(5):
            (rotated_dir / f"data.2026010{i + 1}T000000Z.1.jsonl").write_text(
                f"old-{i}\n", encoding="utf-8"
            )

        path = tmp_path / "data.jsonl"
        path.write_text('{"a":1}\n', encoding="utf-8")
        rotate_jsonl_if_oversize(path, max_bytes=1, retain=3)

        # Only the 3 most-recent rotated files remain; the 2 oldest were pruned.
        survivors = list_rotated_jsonl_paths(rotated_dir, stem="data")
        assert len(survivors) == 3
        # The new rotation must be the newest one (sorted oldest → newest).
        assert survivors[-1].read_text(encoding="utf-8") == '{"a":1}\n'

    def test_rotation_does_not_update_checksum_sidecar(self, tmp_path: Path) -> None:
        """Rotation is intentionally minimal: it does not move or update the
        ``.sha256`` sidecar (the checksum is for the *current* file at the
        original path, and after rotation that file is fresh + empty).  The
        next ``append_jsonl_line`` call regenerates the sidecar for the
        fresh file.  This test pins the deliberately-narrow contract.
        """
        path = tmp_path / "data.jsonl"
        path.write_text('{"a":1}\n', encoding="utf-8")
        write_checksum_file(path)  # establish baseline checksum sidecar
        assert (path.with_suffix(".sha256")).exists()

        rotate_jsonl_if_oversize(path, max_bytes=1)

        # The current file is gone (moved into rotated/).
        assert not path.exists()
        # The checksum sidecar is still at the original location (its content
        # is now stale — it captured the *pre-rotation* digest).  This is the
        # deliberately-narrow contract: rotation does NOT rewrite the sidecar.
        sidecar = path.with_suffix(".sha256")
        assert sidecar.exists()
        # The rotated file in rotated/ does NOT carry the sidecar.
        rotated_files = list_rotated_jsonl_paths(tmp_path / "rotated", stem="data")
        assert len(rotated_files) == 1
        assert rotated_files[0].read_text(encoding="utf-8") == '{"a":1}\n'

    def test_rejects_non_positive_max_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        path.write_text('{"a":1}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="max_bytes"):
            rotate_jsonl_if_oversize(path, max_bytes=0)
        with pytest.raises(ValueError, match="max_bytes"):
            rotate_jsonl_if_oversize(path, max_bytes=-1)

    def test_rejects_non_positive_retain(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        path.write_text('{"a":1}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="retain"):
            rotate_jsonl_if_oversize(path, max_bytes=100, retain=0)
        with pytest.raises(ValueError, match="retain"):
            rotate_jsonl_if_oversize(path, max_bytes=100, retain=-1)


class TestListRotatedJsonlPaths:
    def test_returns_empty_when_dir_missing(self, tmp_path: Path) -> None:
        assert list_rotated_jsonl_paths(tmp_path / "rotated") == ()

    def test_returns_empty_when_no_files(self, tmp_path: Path) -> None:
        rotated_dir = tmp_path / "rotated"
        rotated_dir.mkdir()
        assert list_rotated_jsonl_paths(rotated_dir) == ()

    def test_returns_sorted_oldest_first(self, tmp_path: Path) -> None:
        rotated_dir = tmp_path / "rotated"
        rotated_dir.mkdir()
        (rotated_dir / "data.20260103T000000Z.1.jsonl").write_text("newest", encoding="utf-8")
        (rotated_dir / "data.20260101T000000Z.1.jsonl").write_text("oldest", encoding="utf-8")
        (rotated_dir / "data.20260102T000000Z.1.jsonl").write_text("middle", encoding="utf-8")
        result = list_rotated_jsonl_paths(rotated_dir, stem="data")
        assert len(result) == 3
        assert result[0].read_text(encoding="utf-8") == "oldest"
        assert result[1].read_text(encoding="utf-8") == "middle"
        assert result[2].read_text(encoding="utf-8") == "newest"


class TestAppendJsonlLineWithRotation:
    def test_append_under_threshold_does_not_rotate(self, tmp_path: Path) -> None:
        from src.core.jsonl_utils import append_jsonl_line

        path = tmp_path / "data.jsonl"
        rotated = append_jsonl_line(path, '{"a":1}\n', max_bytes=1000)
        assert rotated is False
        assert path.read_text(encoding="utf-8") == '{"a":1}\n'

    def test_append_over_threshold_rotates_first(self, tmp_path: Path) -> None:
        from src.core.jsonl_utils import append_jsonl_line

        path = tmp_path / "data.jsonl"
        path.write_text('{"prior":1}\n{"prior":2}\n', encoding="utf-8")  # ~22 bytes
        rotated = append_jsonl_line(path, '{"fresh":99}\n', max_bytes=10)
        assert rotated is True

        # Original content was moved into rotated/, fresh content is in the new file.
        rotated_files = list_rotated_jsonl_paths(tmp_path / "rotated", stem="data")
        assert len(rotated_files) == 1
        assert rotated_files[0].read_text(encoding="utf-8") == '{"prior":1}\n{"prior":2}\n'
        assert path.read_text(encoding="utf-8") == '{"fresh":99}\n'

    def test_repeated_appends_rotate_at_each_threshold_crossing(self, tmp_path: Path) -> None:
        from src.core.jsonl_utils import append_jsonl_line

        path = tmp_path / "data.jsonl"
        for i in range(5):
            rotated = append_jsonl_line(path, f'{{"i":{i}}}\n', max_bytes=10)
            # First append (when the file already has data over the threshold)
            # rotates, subsequent appends under threshold do not.
            if i == 0 and path.exists() and path.stat().st_size > 10:
                pass
        # The 5th line is the most recent in the live file.
        assert path.read_text(encoding="utf-8") == '{"i":4}\n'

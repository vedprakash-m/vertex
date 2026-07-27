"""Unit tests for scripts/check_backlog_citations.py (BL-K1, specs/backlog.md).

Covers the behavior changes made across the backlog's lifecycle:
specs/backlog.md was never validated (it may legitimately not exist in a
fresh clone); specs/bklg.md was briefly the tracked canonical mirror but
was untracked/gitignored again on 2026-07-27 (BL-K1 follow-up) and so is
now treated identically -- neither may exist in a fresh clone/CI checkout.
KNOWN_FUTURE_PATHS separately lets a handful of documented
not-built-yet/no-longer-exists paths pass without disabling the checker
for every real backlog reference.
"""

from __future__ import annotations

from scripts.check_backlog_citations import KNOWN_FUTURE_PATHS, VALIDATED_PREFIXES, _validate_path


def test_specs_backlog_md_is_not_a_validated_prefix() -> None:
    """specs/backlog.md is the gitignored working copy -- it may not exist
    in a fresh clone, so a citation to it must never be flagged either way."""
    assert not any(prefix.startswith("specs/backlog.md") for prefix in VALIDATED_PREFIXES)
    assert _validate_path("specs/backlog.md") is None


def test_specs_bklg_md_is_not_a_validated_prefix() -> None:
    """specs/bklg.md is gitignored (2026-07-27, BL-K1 follow-up) -- like
    specs/backlog.md, it may not exist in a fresh clone, so a citation to
    it must never be flagged either way."""
    assert not any(prefix.startswith("specs/bklg.md") for prefix in VALIDATED_PREFIXES)
    assert _validate_path("specs/bklg.md") is None


def test_known_future_paths_never_flagged() -> None:
    for path in KNOWN_FUTURE_PATHS:
        assert _validate_path(path) is None, f"{path} should be exempted via KNOWN_FUTURE_PATHS"


def test_an_unknown_missing_src_path_is_still_flagged() -> None:
    """The exception list must not become a blanket bypass -- a genuinely
    missing, non-exempted path under a validated prefix must still fail."""
    result = _validate_path("src/this_file_does_not_exist_anywhere.py")
    assert result == "src/this_file_does_not_exist_anywhere.py"


def test_a_real_existing_path_is_not_flagged() -> None:
    assert _validate_path("scripts/check_backlog_citations.py") is None

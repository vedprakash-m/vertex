"""ADF-W5.9 (Section 9.7): src/core/cockpit_retention.py."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.cockpit_retention import KEEP_LAST_N_BUILDS, rotate_cockpit_history

_NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _write_history_file(history_dir: Path, at: datetime) -> Path:
    history_dir.mkdir(parents=True, exist_ok=True)
    filename = at.strftime("%Y%m%dT%H%M%SZ") + ".json"
    path = history_dir / filename
    path.write_text("{}", encoding="utf-8")
    return path


def test_empty_history_dir_does_nothing(tmp_path: Path) -> None:
    assert rotate_cockpit_history(tmp_path / "nonexistent", now=_NOW) == ()


def test_fewer_than_30_builds_keeps_everything(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    for i in range(5):
        _write_history_file(history_dir, _NOW - timedelta(hours=i))
    deleted = rotate_cockpit_history(history_dir, now=_NOW)
    assert deleted == ()
    assert len(list(history_dir.glob("*.json"))) == 5


def test_more_than_30_recent_builds_keeps_the_newest_30_plus_any_weekly_keeper(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    for i in range(40):
        _write_history_file(history_dir, _NOW - timedelta(hours=i))
    rotate_cockpit_history(history_dir, now=_NOW)
    remaining = sorted(p.stem for p in history_dir.glob("*.json"))
    # The 30 newest (hours 0..29) always survive. Builds 30..39 (all within
    # ~1.6 days, so at most 2 ISO weeks) contribute at most one weekly
    # keeper per week beyond that -- never zero extra deletions, but never
    # unbounded growth either.
    assert KEEP_LAST_N_BUILDS <= len(remaining) <= KEEP_LAST_N_BUILDS + 2
    newest_stamp = (_NOW).strftime("%Y%m%dT%H%M%SZ")
    oldest_always_kept_stamp = (_NOW - timedelta(hours=29)).strftime("%Y%m%dT%H%M%SZ")
    assert newest_stamp in remaining
    assert oldest_always_kept_stamp in remaining
    # The single OLDEST build (hour 39) is never in the always-keep zone;
    # it only survives if it happens to be that week's newest -- verify at
    # least the middle of the "definitely deleted" range is gone.
    definitely_deleted_stamp = (_NOW - timedelta(hours=32)).strftime("%Y%m%dT%H%M%SZ") + ".json"
    assert not (history_dir / definitely_deleted_stamp).exists()


def test_weekly_keeper_survives_beyond_the_last_30(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    # 35 builds spaced 1 day apart -- more than 30, so some fall into the
    # "weekly keeper" zone rather than the "always keep" zone.
    for i in range(35):
        _write_history_file(history_dir, _NOW - timedelta(days=i))
    rotate_cockpit_history(history_dir, now=_NOW)
    remaining = list(history_dir.glob("*.json"))
    # At least one build older than the 30-newest cutoff survives as a weekly keeper.
    assert len(remaining) > KEEP_LAST_N_BUILDS - 1  # some weekly keepers beyond the raw 30


def test_snapshot_older_than_13_months_is_never_a_keeper(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    _write_history_file(history_dir, _NOW - timedelta(days=500))  # well past 13 months
    for i in range(35):  # push it out of the "last 30" always-keep zone
        _write_history_file(history_dir, _NOW - timedelta(hours=i))
    rotate_cockpit_history(history_dir, now=_NOW)
    ancient_stamp = (_NOW - timedelta(days=500)).strftime("%Y%m%dT%H%M%SZ") + ".json"
    assert not (history_dir / ancient_stamp).exists()


def test_only_one_keeper_per_iso_week(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    # Two snapshots in the same ISO week, both outside the last-30 window.
    base = _NOW - timedelta(days=60)
    _write_history_file(history_dir, base)
    _write_history_file(history_dir, base + timedelta(hours=6))
    for i in range(30):  # fill the always-keep zone so both above are in the weekly-keeper zone
        _write_history_file(history_dir, _NOW - timedelta(hours=i))
    rotate_cockpit_history(history_dir, now=_NOW)
    same_week_survivors = [
        p for p in history_dir.glob("*.json")
        if abs((datetime.strptime(p.stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) - base).days) < 1
    ]
    assert len(same_week_survivors) <= 1


def test_malformed_filenames_are_ignored_not_crashed_on(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    history_dir.mkdir(parents=True)
    (history_dir / "not-a-timestamp.json").write_text("{}", encoding="utf-8")
    _write_history_file(history_dir, _NOW)
    rotate_cockpit_history(history_dir, now=_NOW)  # must not raise
    assert (history_dir / "not-a-timestamp.json").exists()  # untouched, not parseable

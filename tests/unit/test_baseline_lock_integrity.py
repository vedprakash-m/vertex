from __future__ import annotations

import errno
from pathlib import Path
import traceback

import pytest

from src.core import archive_store, baseline_lock, overrides_store, snapshot_store
from src.core.baseline_lock import (
    BaselineLockError,
    assert_issue_unlocked,
    find_baseline_path,
    is_issue_locked,
    locked_issues_for_baseline,
)
from src.core.overrides_store import OverridesDocument
from tests.unit.test_archive_store import _build_manifest, _build_snapshot
from tests.unit.test_archive_workspace_context import EDITION, _bytes, _seed, _workspace


PRIVATE_VALUE = "PRIVATE_BASELINE_VALUE_7391"
STAT_FAILURES = {
    "stat_loop": errno.ELOOP,
    "stat_badfd": errno.EBADF,
    "stat_notdir": errno.ENOTDIR,
    "stat_denied": errno.EACCES,
}
BAD_BASELINES = {
    "syntax": f"notes: {PRIVATE_VALUE}\ntrusted_issue_number: [78\n".encode(),
    "sequence": b"- 78\n",
    "empty_sequence": b"[]\n",
    "scalar": b"false\n",
    "locked_string": b"locked_issues: '78'\n",
    "locked_integer": b"locked_issues: 78\n",
    "trusted_float": b"trusted_issue_number: 78.5\n",
    "trusted_bool": b"trusted_issue_number: true\n",
    "locked_float": b"locked_issues: [78.5]\n",
    "locked_bool": b"locked_issues: [true]\n",
    "locked_null_element": b"locked_issues: [null]\n",
    "trusted_text": f"trusted_issue_number: {PRIVATE_VALUE}\n".encode(),
    "locked_text": f"locked_issues: [78, {PRIVATE_VALUE}]\n".encode(),
    "invalid_utf8": b"\xff\n",
    "invalid_timestamp": b"unused: 2042-13-01\n",
    "duplicate_trusted": b"trusted_issue_number: 78\ntrusted_issue_number: null\n",
    "duplicate_locks": b"locked_issues: [78]\nlocked_issues: []\n",
    "duplicate_nested_merge": (
        b"defaults: &defaults {trusted_issue_number: 78, trusted_issue_number: null}\n"
        b"<<: *defaults\n"
    ),
    "duplicate_nested_other": (
        f"notes: {{{PRIVATE_VALUE}: a, {PRIVATE_VALUE}: b}}\n".encode()
        + b"trusted_issue_number: 77\n"
    ),
    "duplicate_merge": b"defaults: &d {locked_issues: [78]}\n<<: *d\n<<: {locked_issues: []}\n",
}


@pytest.mark.parametrize("case", tuple(BAD_BASELINES))
@pytest.mark.parametrize("operation", ("list", "is_locked", "assert_unlocked"))
def test_invalid_lock_state_never_becomes_unlocked(
    tmp_path: Path, case: str, operation: str,
) -> None:
    baseline = tmp_path / "trusted_baseline.yaml"
    baseline.write_bytes(BAD_BASELINES[case])

    with pytest.raises(BaselineLockError, match="Cannot determine trusted baseline lock state") as caught:
        if operation == "list":
            locked_issues_for_baseline(baseline)
        elif operation == "is_locked":
            is_issue_locked(78, baseline_path=baseline)
        else:
            assert_issue_unlocked(78, target_path=tmp_path / "overrides" / "issue_078.yaml", artifact="overrides")

    assert PRIVATE_VALUE not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert PRIVATE_VALUE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("content", ("", "null\n", "{}\n", "trusted_issue_number: null\nlocked_issues: null\n"))
def test_legacy_empty_baseline_remains_unlocked(tmp_path: Path, content: str) -> None:
    baseline = tmp_path / "trusted_baseline.yaml"
    baseline.write_text(content, encoding="utf-8")
    assert locked_issues_for_baseline(baseline) == frozenset()
    assert not is_issue_locked(78, baseline_path=baseline)
    assert_issue_unlocked(78, target_path=tmp_path / "overrides" / "issue_078.yaml", artifact="overrides")


def test_missing_baseline_and_numeric_string_ids_remain_compatible(tmp_path: Path) -> None:
    baseline = tmp_path / "trusted_baseline.yaml"
    assert locked_issues_for_baseline(baseline) == frozenset()
    assert not is_issue_locked(78, baseline_path=baseline)
    baseline.write_text("trusted_issue_number: '77'\nlocked_issues: [78, '79']\n", encoding="utf-8")
    assert locked_issues_for_baseline(baseline) == frozenset({77, 78, 79})
    assert not is_issue_locked(80, baseline_path=baseline)


def test_aliases_and_yaml_merge_precedence_remain_compatible(tmp_path: Path) -> None:
    baseline = tmp_path / "trusted_baseline.yaml"
    baseline.write_text(
        "first: &first {trusted_issue_number: 77, locked_issues: [78]}\n"
        "second: &second {trusted_issue_number: 80, locked_issues: [81]}\n"
        "<<: [*first, *second]\n"
        "trusted_issue_number: '79'\n",
        encoding="utf-8",
    )
    assert locked_issues_for_baseline(baseline) == frozenset({78, 79})
    assert not is_issue_locked(77, baseline_path=baseline)


def test_invalid_state_does_not_chain_an_active_outer_exception(tmp_path: Path) -> None:
    baseline = tmp_path / "trusted_baseline.yaml"
    baseline.write_text("trusted_issue_number: [78\n", encoding="utf-8")
    try:
        raise RuntimeError(PRIVATE_VALUE)
    except RuntimeError:
        with pytest.raises(BaselineLockError) as caught:
            locked_issues_for_baseline(baseline)
    assert caught.value.__context__ is None
    assert PRIVATE_VALUE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("error_type", (RuntimeError, ValueError))
def test_unexpected_reader_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception],
) -> None:
    baseline = tmp_path / "trusted_baseline.yaml"
    baseline.write_text("{}\n", encoding="utf-8")

    def broken_reader(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        raise error_type("unexpected reader failure")

    monkeypatch.setattr(Path, "read_text", broken_reader)
    with pytest.raises(error_type, match="unexpected reader failure"):
        locked_issues_for_baseline(baseline)


@pytest.mark.parametrize("error_number", tuple(STAT_FAILURES.values()))
@pytest.mark.parametrize("operation", ("list", "is_locked", "find", "assert_unlocked"))
def test_baseline_stat_failure_is_not_treated_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int, operation: str,
) -> None:
    baseline = tmp_path / "trusted_baseline.yaml"
    baseline.write_text("trusted_issue_number: 78\n", encoding="utf-8")
    target = tmp_path / "overrides" / "issue_078.yaml"
    original_stat = Path.stat

    def broken_stat(path: Path, *, follow_symlinks: bool = True):
        if path == baseline:
            raise OSError(error_number, PRIVATE_VALUE)
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", broken_stat)
    with pytest.raises(BaselineLockError, match="Cannot determine trusted baseline lock state") as caught:
        if operation == "list":
            locked_issues_for_baseline(baseline)
        elif operation == "is_locked":
            is_issue_locked(78, baseline_path=baseline)
        elif operation == "find":
            find_baseline_path(target)
        else:
            assert_issue_unlocked(78, target_path=target, artifact="overrides")
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert PRIVATE_VALUE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("operation", ("is_locked", "assert_unlocked"))
def test_baseline_disappearance_after_discovery_is_not_treated_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    baseline = tmp_path / "trusted_baseline.yaml"
    baseline.write_text("trusted_issue_number: 78\n", encoding="utf-8")
    target = tmp_path / "overrides" / "issue_078.yaml"
    original_stat = Path.stat
    original_read = Path.read_text
    stat_calls = 0

    def vanished_stat(path: Path, *, follow_symlinks: bool = True):
        nonlocal stat_calls
        if path == baseline:
            stat_calls += 1
            if stat_calls > 1:
                raise FileNotFoundError(errno.ENOENT, PRIVATE_VALUE)
        return original_stat(path, follow_symlinks=follow_symlinks)

    def vanished_read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == baseline:
            raise FileNotFoundError(errno.ENOENT, PRIVATE_VALUE)
        return original_read(path, encoding=encoding, errors=errors)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "stat", vanished_stat)
        patched.setattr(Path, "read_text", vanished_read)
        with pytest.raises(BaselineLockError, match="Cannot determine trusted baseline lock state") as caught:
            if operation == "is_locked":
                is_issue_locked(78, baseline_path=baseline)
            else:
                assert_issue_unlocked(78, target_path=target, artifact="overrides")
    assert stat_calls == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert PRIVATE_VALUE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("error_type", (RuntimeError, ValueError))
def test_unexpected_baseline_stat_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception],
) -> None:
    baseline = tmp_path / "trusted_baseline.yaml"
    baseline.write_text("trusted_issue_number: 78\n", encoding="utf-8")
    original_stat = Path.stat

    def broken_stat(path: Path, *, follow_symlinks: bool = True):
        if path == baseline:
            raise error_type("unexpected stat failure")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", broken_stat)
    with pytest.raises(error_type, match="unexpected stat failure"):
        locked_issues_for_baseline(baseline)


def test_baseline_stat_error_does_not_chain_an_active_outer_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "trusted_baseline.yaml"
    baseline.write_text("trusted_issue_number: 78\n", encoding="utf-8")
    original_stat = Path.stat

    def broken_stat(path: Path, *, follow_symlinks: bool = True):
        if path == baseline:
            raise OSError(errno.ELOOP, PRIVATE_VALUE)
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", broken_stat)
    try:
        raise RuntimeError(PRIVATE_VALUE)
    except RuntimeError:
        with pytest.raises(BaselineLockError) as caught:
            locked_issues_for_baseline(baseline)
    assert caught.value.__context__ is None
    assert PRIVATE_VALUE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("operation", ("list", "is_locked", "assert_unlocked"))
def test_observed_locked_baseline_is_checked_once_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    baseline = tmp_path / "trusted_baseline.yaml"
    baseline.write_text("trusted_issue_number: 78\n", encoding="utf-8")
    target = tmp_path / "overrides" / "issue_078.yaml"
    original_stat = Path.stat
    stat_calls = 0

    def counted_stat(path: Path, *, follow_symlinks: bool = True):
        nonlocal stat_calls
        if path == baseline:
            stat_calls += 1
            if stat_calls > 1:
                raise AssertionError("baseline existence was checked twice")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", counted_stat)
    if operation == "list":
        assert locked_issues_for_baseline(baseline) == frozenset({78})
    elif operation == "is_locked":
        assert is_issue_locked(78, baseline_path=baseline)
    else:
        with pytest.raises(BaselineLockError, match="hardlocked"):
            assert_issue_unlocked(78, target_path=target, artifact="overrides")
    assert stat_calls == 1


def test_missing_issue_short_circuits_baseline_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "trusted_baseline.yaml"
    original_stat = Path.stat

    def forbidden_stat(path: Path, *, follow_symlinks: bool = True):
        if path == baseline:
            raise AssertionError("baseline should not be checked")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", forbidden_stat)
    assert not is_issue_locked(None, baseline_path=baseline)
    assert not is_issue_locked(78, baseline_path=None)
    assert_issue_unlocked(None, target_path=tmp_path / "overrides" / "issue_078.yaml", artifact="overrides")


def test_baseline_discovery_stops_at_root_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tmp_path.anchor)
    observed: list[Path] = []

    def missing_baseline(path: Path) -> bool:
        observed.append(path)
        return False

    monkeypatch.setattr(Path, "is_dir", lambda path: path == root)
    monkeypatch.setattr(baseline_lock, "_baseline_exists", missing_baseline)
    assert find_baseline_path(root) is None
    assert observed == [root / "trusted_baseline.yaml"]


@pytest.mark.parametrize(
    "case", (
        "syntax", "sequence", "locked_string", "trusted_text", "invalid_utf8", "unreadable", "disappeared",
        "invalid_timestamp", "duplicate_trusted", "duplicate_locks", "duplicate_nested_merge", "duplicate_merge",
        *STAT_FAILURES, "disappeared_after_lookup",
    ),
)
@pytest.mark.parametrize("writer", ("snapshot", "archive", "overrides", "reset"))
def test_selected_writers_refuse_invalid_baseline_without_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, writer: str,
) -> None:
    selected, archive, decoy = _workspace(tmp_path)
    _seed(selected)
    _seed(decoy, 77)
    reports = tmp_path / "legacy" / "output"
    document = OverridesDocument(issue_number=78, top_3_now=(), scorecards=())
    overrides_store.save_overrides(EDITION, document, reports, programs_root=selected)
    baseline = selected / "acme" / "trusted_baseline.yaml"
    baseline.write_bytes(BAD_BASELINES.get(case, b"trusted_issue_number: 78\n"))
    (decoy / "acme" / "trusted_baseline.yaml").write_text("trusted_issue_number: 77\n", encoding="utf-8")
    if case in {"unreadable", "disappeared"}:
        original_read_text = Path.read_text

        def deny_baseline(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == baseline:
                error = PermissionError if case == "unreadable" else FileNotFoundError
                raise error(PRIVATE_VALUE)
            return original_read_text(path, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", deny_baseline)
    before = _bytes(tmp_path)
    before_dirs = {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_dir()}
    original_stat = Path.stat
    original_read = Path.read_text
    stat_calls = 0

    def broken_stat(path: Path, *, follow_symlinks: bool = True):
        nonlocal stat_calls
        if path == baseline:
            stat_calls += 1
            if case in STAT_FAILURES:
                raise OSError(STAT_FAILURES[case], PRIVATE_VALUE)
            if stat_calls > 1:
                raise FileNotFoundError(errno.ENOENT, PRIVATE_VALUE)
        return original_stat(path, follow_symlinks=follow_symlinks)

    def vanished_read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == baseline:
            raise FileNotFoundError(errno.ENOENT, PRIVATE_VALUE)
        return original_read(path, encoding=encoding, errors=errors)

    with monkeypatch.context() as patched:
        if case in STAT_FAILURES or case == "disappeared_after_lookup":
            patched.setattr(Path, "stat", broken_stat)
        if case == "disappeared_after_lookup":
            patched.setattr(Path, "read_text", vanished_read)
        with pytest.raises(BaselineLockError, match="Cannot determine trusted baseline lock state") as caught:
            if writer == "snapshot":
                snapshot_store.write_confirmed(
                    EDITION, 78, _build_snapshot(), archive_root=archive, programs_root=selected,
                )
            elif writer == "archive":
                archive_store.write_confirmed_issue(
                    EDITION, 78, _build_snapshot(), "replacement", "replacement", _build_manifest(),
                    archive_root=archive, programs_root=selected,
                )
            elif writer == "overrides":
                overrides_store.save_overrides(EDITION, document, reports, programs_root=selected)
            else:
                overrides_store.reset_overrides_for_next_issue(EDITION, 78, (), reports, programs_root=selected)
    if case in STAT_FAILURES or case == "disappeared_after_lookup":
        assert stat_calls == 1
    assert PRIVATE_VALUE not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert PRIVATE_VALUE not in "".join(traceback.format_exception(caught.value))
    assert _bytes(tmp_path) == before
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_dir()} == before_dirs

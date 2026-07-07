from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.core.file_stores import FileSignalStore, FileTrajectoryStore
from src.core.models_v2 import SignalReviewDecision
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core import store_factory


def test_build_signal_store_for_program_id_uses_foreign_program_storage_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        store_factory,
        "load_program",
        lambda program_id, programs_root: SimpleNamespace(storage_backend="sqlite"),
    )

    store = store_factory.build_signal_store_for_program_id("fabrikam", programs_root=tmp_path)

    assert isinstance(store, SQLiteSignalStore)


def test_build_trajectory_store_for_program_id_uses_foreign_program_storage_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        store_factory,
        "load_program",
        lambda program_id, programs_root: SimpleNamespace(storage_backend="file"),
    )

    store = store_factory.build_trajectory_store_for_program_id("fabrikam", programs_root=tmp_path)

    assert isinstance(store, FileTrajectoryStore)


def test_read_signal_review_log_for_program_id_routes_to_sqlite_reader_for_foreign_program(monkeypatch, tmp_path) -> None:
    expected = (
        SignalReviewDecision(
            signal_id="sig-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
            reviewed_by="reviewer",
            note="sqlite-backed",
        ),
    )
    monkeypatch.setattr(
        store_factory,
        "load_program",
        lambda program_id, programs_root: SimpleNamespace(storage_backend="sqlite"),
    )
    monkeypatch.setattr(store_factory, "read_sqlite_signal_review_log", lambda program_id, programs_root: expected)

    reviews = store_factory.read_signal_review_log_for_program_id("fabrikam", programs_root=tmp_path)

    assert reviews == expected


def test_read_signal_review_log_for_program_id_filters_file_review_entries_for_foreign_program(monkeypatch, tmp_path) -> None:
    expected = SignalReviewDecision(
        signal_id="sig-2",
        decision="rejected",
        reviewed_at=datetime(2026, 5, 22, 13, 0, tzinfo=timezone.utc),
        reviewed_by="reviewer",
        note="file-backed",
    )
    marker = SimpleNamespace(signal_id="sig-2")
    monkeypatch.setattr(
        store_factory,
        "load_program",
        lambda program_id, programs_root: SimpleNamespace(storage_backend="file"),
    )
    monkeypatch.setattr(store_factory, "read_review_log", lambda program_id, programs_root: (marker, expected))

    reviews = store_factory.read_signal_review_log_for_program_id("fabrikam", programs_root=tmp_path)

    assert reviews == (expected,)


def test_build_program_store_defaults_to_file_when_program_is_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(store_factory, "load_program", lambda program_id, programs_root: None)

    signal_store = store_factory.build_signal_store_for_program_id("fabrikam", programs_root=tmp_path)
    trajectory_store = store_factory.build_trajectory_store_for_program_id("fabrikam", programs_root=tmp_path)

    assert isinstance(signal_store, FileSignalStore)
    assert isinstance(trajectory_store, FileTrajectoryStore)
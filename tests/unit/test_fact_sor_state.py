from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from threading import Event, Thread

import pytest
import yaml

from src.core.exceptions import ConfigError
from src.core.fact_sor_state import load_fact_sor_state, save_fact_sor_state


def test_save_and_load_fact_sor_state_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 6, 7, 12, 30, tzinfo=timezone.utc)

    save_fact_sor_state(
        "acme",
        mode="primary",
        recorded_at=recorded_at,
        recorded_by="operator",
        programs_root=programs_root,
    )

    loaded = load_fact_sor_state("acme", programs_root=programs_root)

    assert loaded is not None
    assert loaded.mode == "primary"
    assert loaded.recorded_at == recorded_at
    assert loaded.recorded_by == "operator"


@pytest.mark.parametrize("existing", [False, True])
def test_read_during_fact_sor_write_never_observes_partial_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool,
) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 6, 7, 12, 30, tzinfo=timezone.utc)
    if existing:
        save_fact_sor_state("acme", mode="legacy", recorded_at=recorded_at, programs_root=programs_root)

    writing, resume = Event(), Event()
    errors: list[Exception] = []
    original_write = Path.write_text

    def pause_write(path: Path, data: str, *args, **kwargs) -> int:
        with path.open("w", encoding="utf-8") as stream:
            stream.write("schema_version:")
            stream.flush()
            writing.set()
            if not resume.wait(10):
                raise TimeoutError("reader did not finish")
            stream.seek(0)
            stream.truncate()
            return stream.write(data)

    def save() -> None:
        try:
            save_fact_sor_state("acme", mode="primary", recorded_at=recorded_at, programs_root=programs_root)
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(Path, "write_text", pause_write)
    writer = Thread(target=save)
    writer.start()
    try:
        assert writing.wait(10)
        observed = load_fact_sor_state("acme", programs_root=programs_root)
        assert (observed.mode if observed else None) == ("legacy" if existing else None)
    finally:
        resume.set()
        writer.join(10)
        monkeypatch.setattr(Path, "write_text", original_write)
    assert not writer.is_alive()
    assert not errors
    result = load_fact_sor_state("acme", programs_root=programs_root)
    assert result is not None and result.mode == "primary"


@pytest.mark.parametrize("error_type", [OSError, PermissionError])
def test_fact_sor_replace_failure_preserves_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[OSError],
) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 6, 7, 12, 30, tzinfo=timezone.utc)
    save_fact_sor_state("acme", mode="legacy", recorded_at=recorded_at, programs_root=programs_root)

    def fail_replace(*args: object) -> None:
        raise error_type("replace failed")

    monkeypatch.setattr("src.core.fact_sor_state.os.replace", fail_replace)
    with pytest.raises(error_type, match="replace failed"):
        save_fact_sor_state("acme", mode="primary", recorded_at=recorded_at, programs_root=programs_root)
    result = load_fact_sor_state("acme", programs_root=programs_root)
    assert result is not None and result.mode == "legacy"


@pytest.mark.skipif(os.name != "nt", reason="Windows replacement sharing contention")
def test_fact_sor_reader_retries_transient_replacement_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    save_fact_sor_state(
        "acme", mode="legacy", recorded_at=datetime(2026, 6, 7, tzinfo=timezone.utc),
        programs_root=programs_root,
    )
    original_read = Path.read_text
    attempts = 0

    def contended_read(path: Path, *args, **kwargs) -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(13, "sharing contention")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", contended_read)
    state = load_fact_sor_state("acme", programs_root=programs_root)
    assert state is not None and state.mode == "legacy"
    assert attempts == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows replacement sharing contention")
def test_fact_sor_reader_surfaces_persistent_permission_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    save_fact_sor_state(
        "acme", mode="legacy", recorded_at=datetime(2026, 6, 7, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    def denied_read(path: Path, *args, **kwargs) -> str:
        raise PermissionError(13, "access denied")

    monkeypatch.setattr(Path, "read_text", denied_read)
    with pytest.raises(PermissionError, match="access denied"):
        load_fact_sor_state("acme", programs_root=programs_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows replacement sharing contention")
def test_fact_sor_writer_retries_transient_replacement_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 6, 7, tzinfo=timezone.utc)
    save_fact_sor_state("acme", mode="legacy", recorded_at=recorded_at, programs_root=programs_root)
    original_replace = os.replace
    attempts = 0

    def contended_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(13, "sharing contention")
        original_replace(source, destination)

    monkeypatch.setattr("src.core.fact_sor_state.os.replace", contended_replace)
    save_fact_sor_state("acme", mode="primary", recorded_at=recorded_at, programs_root=programs_root)
    state = load_fact_sor_state("acme", programs_root=programs_root)
    assert state is not None and state.mode == "primary"
    assert attempts == 3


def test_load_fact_sor_state_rejects_non_string_mode(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "acme" / "fact_store_sor.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "mode": 1,
                "recorded_at": "2026-06-07T12:30:00+00:00",
                "recorded_by": "operator",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="fact-store SoR mode must be a string"):
        load_fact_sor_state("acme", programs_root=programs_root)


def test_load_fact_sor_state_rejects_non_string_recorded_by(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "acme" / "fact_store_sor.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "mode": "primary",
                "recorded_at": "2026-06-07T12:30:00+00:00",
                "recorded_by": 123,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="fact-store SoR recorded_by must be a string"):
        load_fact_sor_state("acme", programs_root=programs_root)


def test_load_fact_sor_state_rejects_non_string_recorded_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "acme" / "fact_store_sor.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "mode": "primary",
                "recorded_at": 123,
                "recorded_by": "operator",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="fact-store SoR recorded_at must be a string"):
        load_fact_sor_state("acme", programs_root=programs_root)
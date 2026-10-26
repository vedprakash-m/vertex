from __future__ import annotations

import pytest

from scripts.quarantine_synthetic_ai_telemetry import quarantine_path, quarantine_synthetic_records


def test_cli_requires_explicit_sidecar_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import quarantine_synthetic_ai_telemetry as quarantine

    def unexpected_read(*args, **kwargs):
        pytest.fail("Missing paths must not read or mutate implicit program state.")

    monkeypatch.setattr(quarantine, "quarantine_synthetic_records", unexpected_read)
    with pytest.raises(SystemExit) as error:
        quarantine.main([])
    assert error.value.code == 2


def test_quarantine_moves_only_synthetic_records_and_is_idempotent(tmp_path) -> None:
    sidecar = tmp_path / "ai_telemetry.jsonl"
    live = '{"feature":"real","deployment_id":"prod"}\n'
    synthetic = '{"feature":"unit-test","deployment_id":"trace-model"}\n'
    sidecar.write_text(live + synthetic, encoding="utf-8")

    assert quarantine_synthetic_records(sidecar) == (1, 1)
    assert sidecar.read_text(encoding="utf-8") == live
    assert quarantine_path(sidecar).read_text(encoding="utf-8") == synthetic
    assert quarantine_synthetic_records(sidecar) == (0, 0)


def test_check_mode_reports_without_changing_sidecar(tmp_path) -> None:
    sidecar = tmp_path / "ai_telemetry.jsonl"
    content = '{"feature":"unit-test","deployment_id":"trace-model"}\n'
    sidecar.write_text(content, encoding="utf-8")

    assert quarantine_synthetic_records(sidecar, check_only=True) == (1, 0)
    assert sidecar.read_text(encoding="utf-8") == content
    assert not quarantine_path(sidecar).exists()

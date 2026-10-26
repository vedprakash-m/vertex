from pathlib import Path
from unittest.mock import Mock

import pytest

import src.commands.doctor as doctor
import src.commands.doctor_checks.metric_binding_checks as metric_checks
from src.core.metric_models import MetricDefinition
from src.core.reality_store import RealityStore, get_program_reality_db_path
from tests.unit.test_commands_report import repo_root as repo_root


@pytest.mark.parametrize("definitions", [None, {}])
def test_metric_doctor_definition_authority(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    definitions: dict[str, MetricDefinition] | None,
) -> None:
    load_definitions = Mock(wraps=metric_checks.load_metric_definition_map)
    monkeypatch.setattr(metric_checks, "load_metric_definition_map", load_definitions)

    report = doctor.run_doctor(
        edition_name="acme_weekly",
        metric_bindings=True,
        programs_root=repo_root / "programs",
        reality_db_root=repo_root / "vertex-db",
        metric_definitions=definitions,
    )

    assert report.checks[0].metadata["active_binding_count"] == 0
    if definitions is None:
        assert load_definitions.call_args.kwargs["metrics_root"] == repo_root / "knowledge" / "metrics"
    else:
        load_definitions.assert_not_called()


@pytest.mark.parametrize("configured_file", [False, True])
@pytest.mark.parametrize("explicit_root", [False, True])
def test_metric_doctor_keeps_database_authority(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_file: bool,
    explicit_root: bool,
) -> None:
    configured = repo_root / ("configured.sqlite3" if configured_file else "configured-db")
    monkeypatch.setenv("VERTEX_DB_PATH", str(configured))
    override = repo_root / "explicit-db" if explicit_root else None
    expected_path = get_program_reality_db_path("acme", db_root=override)
    stores: list[RealityStore] = []

    def capture_store(program_id: str, *, db_root: Path | None) -> RealityStore:
        store = RealityStore(program_id, db_root=db_root)
        stores.append(store)
        return store

    monkeypatch.setattr(metric_checks, "RealityStore", capture_store)

    report = doctor.run_doctor(
        edition_name="acme_weekly",
        metric_bindings=True,
        programs_root=repo_root / "programs",
        reality_db_root=override,
        metric_definitions={},
    )

    assert report.checks[0].metadata["active_binding_count"] == 0
    assert len(stores) == 1
    assert stores[0].db_path == expected_path
    assert expected_path.is_file()

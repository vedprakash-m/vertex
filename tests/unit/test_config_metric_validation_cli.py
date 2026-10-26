from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.commands import config


def _seed(root: Path) -> Path:
    program = root / "fixture"
    program.mkdir(parents=True)
    (program / "program.yaml").write_text("schema_version: '3.0'\nid: fixture\nname: Fixture\n", encoding="utf-8")
    catalog = root.parent / "knowledge" / "metrics"
    catalog.mkdir(parents=True)
    (catalog / "catalog.yaml").write_text(
        "metrics:\n  - id: demo.count\n    title: Count\n    unit: count\n    aggregation: last\n", encoding="utf-8",
    )
    (program / "kpis.yaml").write_text(
        "kpis:\n  - id: count-query\n    metric_id: demo.count\n    engine: wiql\n"
        "    wiql: SELECT [System.Id] FROM WorkItems\n    result_column: Count\n", encoding="utf-8",
    )
    return program


@pytest.mark.parametrize("broken", [False, True])
def test_config_metric_bindings_are_actually_wired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: bool,
) -> None:
    root = tmp_path / "programs"
    program = _seed(root)
    if broken:
        path = program / "kpis.yaml"
        path.write_text(path.read_text().replace("metric_id: demo.count", "metric_id: missing"), encoding="utf-8")
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture", "--format", "json"])
    assert result.exit_code == (1 if broken else 0), result.output + repr(result.exception)
    payload = json.loads(result.stdout)
    metrics = payload["metric_validation"]
    assert metrics["scope"] == "offline_metric_query_bindings"
    assert metrics["metric_count"] == 1
    assert bool(metrics["errors"]) is broken
    assert metrics["live_validated"] is False


@pytest.mark.parametrize("content", ["null", "", "[]", "metrics: false", "secret: [PRIVATE-YAML", "date: 2026-13-01"])
def test_present_invalid_metric_catalogs_fail_not_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str,
) -> None:
    root = tmp_path / "programs"
    _seed(root)
    (tmp_path / "knowledge" / "metrics" / "catalog.yaml").write_text(content, encoding="utf-8")
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture", "--format", "json"])
    assert result.exit_code == 1, result.output + repr(result.exception)
    payload = json.loads(result.stdout)
    assert payload["metric_validation"]["status"] == "fail"
    assert "PRIVATE-YAML" not in result.stdout + result.stderr


def test_metric_catalog_bare_entry_list_is_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "programs"
    _seed(root)
    path = tmp_path / "knowledge" / "metrics" / "catalog.yaml"
    path.write_text("- id: demo.count\n  title: Count\n  unit: count\n  aggregation: last\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture", "--format", "json"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert not json.loads(result.stdout)["metric_validation"]["errors"]


def test_metric_catalog_scan_failure_is_not_successful_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    _seed(root)
    catalog = tmp_path / "knowledge" / "metrics"
    original = Path.iterdir

    def iterate(self: Path):
        if self == catalog:
            raise PermissionError("PRIVATE-ACCESS")
        return original(self)

    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(Path, "iterdir", iterate)
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture", "--format", "json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["metric_validation"]["status"] == "fail"
    assert "PRIVATE-ACCESS" not in result.stdout + result.stderr


def test_legacy_program_without_metric_configuration_is_not_certified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    program = root / "fixture"
    program.mkdir(parents=True)
    (program / "program.yaml").write_text("schema_version: '3.0'", encoding="utf-8")
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["metric_validation"]["status"] == "not_configured"
    assert payload["references_validated"] is False


def test_disappearing_observed_metric_catalog_is_an_explicit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    _seed(root)
    catalog = tmp_path / "knowledge" / "metrics" / "catalog.yaml"
    original = Path.read_text

    def read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if self == catalog:
            raise FileNotFoundError("PRIVATE-RACE")
        return original(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(Path, "read_text", read_text)
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture", "--format", "json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["metric_validation"]["status"] == "fail"
    assert "PRIVATE-RACE" not in result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["shared", "fallback", "shared-malformed"])
def test_metric_queries_use_effective_shared_or_local_knowledge_without_failed_read_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    root = tmp_path / "programs"
    program = _seed(root)
    body = (
        "schema_version: '1.0'\nqueries:\n  - id: secondary\n    metric_id: demo.count\n"
        "    engine: wiql\n    wiql: SELECT [System.Id] FROM WorkItems\n    result_column: Count\n"
    )
    local = program / "knowledge" / "golden_queries.yaml"
    local.parent.mkdir()
    local.write_text(body.replace("demo.count", "missing") if mode == "shared" else body, encoding="utf-8")
    shared = tmp_path / "knowledge" / "golden_queries.yaml"
    if mode != "fallback":
        shared.write_text("secret: [PRIVATE-QUERY" if mode == "shared-malformed" else body, encoding="utf-8")
    (tmp_path / "knowledge" / "metrics" / "readme.txt").write_text("Not a metric catalog", encoding="utf-8")
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture", "--format", "json"])
    payload = json.loads(result.stdout)
    if mode == "shared-malformed":
        assert result.exit_code == 1
        assert payload["metric_validation"]["status"] == "fail"
        assert "PRIVATE-QUERY" not in result.stdout
    else:
        assert result.exit_code == 0, result.output + repr(result.exception)
        assert payload["metric_validation"]["query_count"] == 2
        assert not payload["metric_validation"]["errors"]


def test_matching_schema_versions_do_not_hide_metric_scope_limitations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    program = _seed(root)
    for name in ("readiness", "template_contract", "slice_contracts", "chapter_contract"):
        (program / f"{name}.yaml").write_text("schema_version: '1.0'", encoding="utf-8")
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "Config schema validation: WARN" in result.stdout
    assert "Offline metric bindings: WARN" in result.stdout

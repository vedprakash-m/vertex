from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

import src.core.kusto_rendering as kusto_rendering
from src.commands.report import generate_report_draft
from src.core.config_loader import KustoQuerySettings, KustoSettings, load_report_config
from src.core.config_loader_v2 import load_edition_bundle
from src.core.exceptions import ConfigError
from src.core.kusto_rendering import build_offline_kpi_chart_sections
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.support.synthetic_report_source import build_synthetic_report_source

_GOLDEN = (
    'schema_version: "1.0"\n'
    "queries:\n"
    "  - id: golden-a\n"
    "    engine: kusto\n"
    "    cluster: https://c.kusto.windows.net\n"
    "    database: db\n"
    "    kql: T | take 1\n"
    "    section: Golden A\n"
)

_KPIS = (
    'schema_version: "1.0"\n'
    "kpis:\n"
    "  - id: kpi-one\n"
    "    cluster: https://c.kusto.windows.net\n"
    "    database: db\n"
    "    kql: T | take 1\n"
    "    section: KPI One\n"
    "    required: true\n"
    "  - id: kpi-two\n"
    "    cluster: https://c.kusto.windows.net\n"
    "    database: db\n"
    "    kql: T | take 2\n"
    "    section: KPI Two\n"
    "    chart_cache_ttl_hours: 12\n"
)


def _write_fixture(tmp_path: Path, *, kpi_charts: str | None, kpis: str = _KPIS) -> tuple[Path, Path]:
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    program_dir = programs_root / "demo"
    for path in (editions_root, program_dir, knowledge_root):
        path.mkdir(parents=True)
    edition = (
        'schema_version: "2.0"\nid: demo_weekly\nprogram_id: demo\nname: Demo Weekly\n'
        "type: detailed\naltitude: helicopter\ncadence: weekly\n"
    )
    if kpi_charts is not None:
        edition += f"kpi_charts: {kpi_charts}\n"
    (editions_root / "demo_weekly.yaml").write_text(edition, encoding="utf-8")
    (program_dir / "program.yaml").write_text(
        'schema_version: "2.0"\nid: demo\nname: Demo\nado:\n  organization: org\n  project: proj\n'
        "  area_paths: [proj]\n  work_item_types: [Bug]\n  date_window_days: 7\n",
        encoding="utf-8",
    )
    (program_dir / "workstreams.yaml").write_text(
        'schema_version: "2.0"\nworkstreams:\n  - id: ws_demo\n    name: Demo WS\n', encoding="utf-8"
    )
    (program_dir / "scorecards.yaml").write_text(
        'schema_version: "2.0"\nscorecards:\n  - name: S\n    dimensions:\n      - name: D\n        workstream_id: ws_demo\n',
        encoding="utf-8",
    )
    (program_dir / "kpis.yaml").write_text(kpis, encoding="utf-8")
    (knowledge_root / "people_directory.yaml").write_text('schema_version: "1.0"\npeople: []\n', encoding="utf-8")
    (knowledge_root / "people_profiles.yaml").write_text('schema_version: "1.0"\nprofiles: []\n', encoding="utf-8")
    (knowledge_root / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (knowledge_root / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (knowledge_root / "golden_queries.yaml").write_text(_GOLDEN, encoding="utf-8")
    return editions_root, programs_root


def _queries(tmp_path: Path, kpi_charts: str | None, **kwargs: str) -> list[dict]:
    editions_root, programs_root = _write_fixture(tmp_path, kpi_charts=kpi_charts, **kwargs)
    bundle = load_edition_bundle("demo_weekly", editions_root=editions_root, programs_root=programs_root)
    assert bundle is not None
    return bundle.config_document["kusto"]["queries"]


def test_kpi_charts_follow_golden_queries_in_declared_order(tmp_path: Path) -> None:
    queries = _queries(tmp_path, "[kpi-two, kpi-one]")

    assert [query["id"] for query in queries] == ["golden-a", "kpi-two", "kpi-one"]
    kpi_two, kpi_one = queries[1], queries[2]
    assert kpi_two["render_as"] == "chart_image" and kpi_two["kpi_chart"] is True
    assert kpi_two["chart_blocks_publish"] is False
    assert kpi_two["chart_cache_ttl_hours"] == 12
    assert kpi_one["chart_blocks_publish"] is True
    assert kpi_one["chart_cache_ttl_hours"] == 26
    assert "kpi_chart" not in queries[0]


def test_kpi_charts_absent_keeps_golden_queries_only(tmp_path: Path) -> None:
    assert [query["id"] for query in _queries(tmp_path, None)] == ["golden-a"]


@pytest.mark.parametrize(
    ("kpi_charts", "message"),
    [
        ("[kpi-one, kpi-one]", "Duplicate"),
        ("[golden-a]", "Duplicate"),
        ("[missing-kpi]", "missing-kpi"),
        ("kpi-one", "kpi_charts"),
        ("[1]", "kpi_charts"),
    ],
)
def test_invalid_kpi_charts_are_config_errors(tmp_path: Path, kpi_charts: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        _queries(tmp_path, kpi_charts)


def test_kpi_chart_required_must_be_boolean(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="required"):
        _queries(tmp_path, "[kpi-one]", kpis=_KPIS.replace("required: true", 'required: "yes"'))


def test_kpi_chart_flag_survives_report_parser(tmp_path: Path) -> None:
    editions_root, programs_root = _write_fixture(tmp_path, kpi_charts="[kpi-one]")

    config = load_report_config("demo_weekly", editions_root=editions_root, programs_root=programs_root)

    by_id = {query.id: query for query in config.kusto.queries}
    assert by_id["kpi-one"].kpi_chart is True and by_id["kpi-one"].chart_blocks_publish is True
    assert by_id["golden-a"].kpi_chart is False


def _settings(*queries: KustoQuerySettings) -> KustoSettings:
    return KustoSettings(enabled=True, queries=queries)


def _query(query_id: str, *, kpi_chart: bool, required: bool = False) -> KustoQuerySettings:
    return KustoQuerySettings(
        id=query_id,
        cluster="https://c.kusto.windows.net",
        database="db",
        kql="T",
        section=query_id.title(),
        render_as="chart_image",
        confidence="medium",
        kusto_section_validates_slice=False,
        caveats=(),
        reference_url=None,
        chart_blocks_publish=required,
        kpi_chart=kpi_chart,
    )


def test_offline_kpi_charts_render_unavailable_in_order() -> None:
    sections = build_offline_kpi_chart_sections(
        _settings(_query("golden-a", kpi_chart=False), _query("kpi-two", kpi_chart=True), _query("kpi-one", kpi_chart=True, required=True))
    )

    assert [section.query_id for section in sections] == ["kpi-two", "kpi-one"]
    assert all(section.message == "Unavailable" and section.is_degraded for section in sections)
    assert [section.chart_blocks_publish for section in sections] == [False, False]


def test_offline_kpi_charts_absent_when_kusto_disabled() -> None:
    settings = KustoSettings(enabled=False, queries=(_query("kpi-one", kpi_chart=True),))

    assert build_offline_kpi_chart_sections(settings) == ()


@pytest.mark.parametrize(
    ("edition_type", "offline", "chart_enabled", "global_charts"),
    [
        ("detailed", True, True, True),
        ("narrative", True, True, True),
        ("lookback", True, True, True),
        ("lookback", True, False, True),
        ("lookback", True, True, False),
        ("lookback", True, None, True),
        ("lookback", False, True, True),
    ],
)
def test_report_kpi_chart_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edition_type: str, offline: bool,
    chart_enabled: bool | None, global_charts: bool,
) -> None:
    from src.commands.confirm_stages.deserialization import deserialize_kusto_sections
    from src.core.archive_store import write_confirmed_issue
    from src.core.models import RiskLevel
    from tests.unit.test_commands_report import (
        _lookback_snapshot, _manifest, _sample_items, _snapshot_item_from_work_item,
    )

    monkeypatch.setattr(kusto_rendering, "_VERTEX_CHARTS_ENABLED", global_charts)
    source = build_synthetic_report_source(tmp_path / "synthetic-source")
    workspace = tmp_path / "workspace"
    reports = stage_v2_report_workspace(source, workspace)
    programs = workspace / "programs"
    archive = workspace / "archive"
    program_dir = programs / "acme"
    monkeypatch.setenv("VERTEX_DB_PATH", str(workspace / "vertex-db"))
    disable_kusto_in_report_copy(reports)

    generate_report_draft(
        "acme_weekly", issue_number=77, programs_root=programs, reports_root=reports,
        archive_root=archive, dry_run=True,
        work_item_loader=lambda _bundle, timestamp: (_sample_items(timestamp)[:1], 0),
        open_browser=False,
    )

    program_path = program_dir / "program.yaml"
    program = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program["kusto"]["enabled"] = True
    program_path.write_text(yaml.safe_dump(program), encoding="utf-8")
    edition_path = program_dir / "editions" / "acme_weekly.yaml"
    edition = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    edition["type"] = edition_type
    edition["layout_mode"] = "dashboard"
    if chart_enabled is None:
        edition.pop("chart", None)
    else:
        edition["chart"] = {"enabled": chart_enabled}
    edition["kpi_charts"] = ["kpi-optional", "kpi-required"]
    edition_path.write_text(yaml.safe_dump(edition), encoding="utf-8")
    kpi_path = program_dir / "kpis.yaml"
    kpi = yaml.safe_load(kpi_path.read_text(encoding="utf-8"))["kpis"][0]
    kpi_path.write_text(yaml.safe_dump({
        "schema_version": "1.0",
        "kpis": [
            dict(kpi, id="kpi-required", section="Required KPI", required=True),
            dict(kpi, id="kpi-optional", section="Optional KPI", required=False),
            dict(kpi, id="kpi-unselected", section="Unselected KPI"),
        ],
    }), encoding="utf-8")
    if edition_type == "lookback":
        now = datetime.now(timezone.utc)
        for issue_number in (1, 2):
            as_of = now - timedelta(days=3 - issue_number)
            item = _snapshot_item_from_work_item(_sample_items(as_of)[0], risk_level=RiskLevel.LOW)
            write_confirmed_issue(
                edition="acme_weekly", issue_number=issue_number,
                snapshot=_lookback_snapshot(issue_number=issue_number, as_of=as_of, items=(item,), scorecard_risks={}),
                html_body="<html>synthetic</html>", markdown_body="synthetic",
                manifest=_manifest(issue_number, as_of, edition="acme_weekly"), archive_root=archive,
            )
    archive_roots = (archive, program_dir / "archive")
    archive_before = {path: path.read_bytes() for root in archive_roots for path in root.rglob("*") if path.is_file()}
    work_items = Mock(side_effect=AssertionError("Offline report called ADO"))
    kusto = Mock(side_effect=AssertionError("Offline report called Kusto"))

    result = generate_report_draft(
        "acme_weekly", issue_number=77, programs_root=programs, reports_root=reports,
        archive_root=archive, dry_run=True, offline=offline,
        work_item_loader=work_items, kusto_query_executor=kusto, open_browser=False,
    )

    draft = json.loads(
        (program_dir / "publications" / "acme_weekly" / "issue_077" / "issue_077.draft.json")
        .read_text(encoding="utf-8")
    )
    sections = [section for section in draft["kusto_sections"] if section["query_id"].startswith("kpi-")]
    work_items.assert_not_called()
    kusto.assert_not_called()
    assert {path: path.read_bytes() for root in archive_roots for path in root.rglob("*") if path.is_file()} == archive_before
    if not offline:
        assert sections == []
        assert result.kusto_sections == ()
        assert "Optional KPI" not in result.html_body and "Required KPI" not in result.html_body
        assert "QG-22" not in result.manifest.qg_results
        assert draft["chart_gate_evidence_sha256"] is None
        return

    assert [section["query_id"] for section in sections] == ["kpi-optional", "kpi-required"]
    assert [section.query_id for section in result.kusto_sections if section.query_id.startswith("kpi-")] == [
        "kpi-optional", "kpi-required",
    ]
    assert all(section["message"] == "Unavailable" and section["is_degraded"] for section in sections)
    assert all(not section["chart_blocks_publish"] for section in sections)
    restored = deserialize_kusto_sections(tuple(sections))
    assert [(section.title, section.message, section.is_degraded, section.chart_blocks_publish) for section in restored] == [
        ("Optional KPI", "Unavailable", True, False), ("Required KPI", "Unavailable", True, False),
    ]
    optional_at = result.html_body.index("Optional KPI")
    required_at = result.html_body.index("Required KPI", optional_at)
    assert "Unavailable" in result.html_body[optional_at:required_at]
    assert "Unavailable" in result.html_body[required_at:]
    assert "Unselected KPI" not in result.html_body
    if not chart_enabled or not global_charts:
        assert all(gate not in result.manifest.qg_results for gate in ("QG-20", "QG-21", "QG-22"))
        assert "chart_freshness_evidence" not in result.manifest.metadata
        assert draft["chart_gate_evidence_sha256"] is None
    else:
        assert all(result.manifest.qg_results[gate] is True for gate in ("QG-20", "QG-21", "QG-22"))
        if edition_type != "lookback":
            from src.commands.confirm_stages.draft_loaders import load_confirm_draft_manifest

            assert result.manifest.metadata["chart_freshness_evidence"] == ()
            assert draft["chart_gate_evidence_sha256"]
            captured = load_confirm_draft_manifest("acme_weekly", 77, draft_state=draft, programs_root=programs)
            assert captured.chart_freshness_evidence == ()
        else:
            assert draft["chart_gate_evidence_sha256"] is None

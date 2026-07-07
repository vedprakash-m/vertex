from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.commands.doctor import run_doctor
from src.commands.doctor_checks.chart_checks import cadence_hours_for_string, run_charts_doctor


def test_cadence_hours_for_string_supports_named_and_every_n_day_cadences() -> None:
    assert cadence_hours_for_string("daily") == 24
    assert cadence_hours_for_string("weekly") == 168
    assert cadence_hours_for_string("every 3 days") == 72
    assert cadence_hours_for_string("unknown cadence") is None


def test_run_charts_doctor_surfaces_ttl_target_exec_summary_and_renderer_issues(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.commands.doctor_checks.chart_checks.resolve_edition",
        lambda edition_name, **kwargs: SimpleNamespace(
            program=SimpleNamespace(id="demo"),
            raw_program={"charts": {}},
            edition=SimpleNamespace(cadence="weekly"),
            workstreams=[SimpleNamespace(id="known")],
        ),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.chart_checks.load_kpi_queries",
        lambda program_id, **kwargs: [
            SimpleNamespace(
                id="ttl-fail",
                chart_config={"type": "line"},
                chart_cache_ttl_hours=24,
                attachment=SimpleNamespace(target="workstream:missing"),
                chart_renderer_id="demo-renderer",
            ),
            SimpleNamespace(
                id="exec-one",
                chart_config={"type": "line"},
                chart_cache_ttl_hours=200,
                attachment=SimpleNamespace(target="exec_summary"),
                chart_renderer_id="demo::shared",
            ),
            SimpleNamespace(
                id="exec-two",
                chart_config={"type": "line"},
                chart_cache_ttl_hours=200,
                attachment=SimpleNamespace(target="exec_summary"),
                chart_renderer_id="demo::shared",
            ),
        ],
    )
    monkeypatch.setattr("src.commands.doctor_checks.chart_checks.load_chart_schema_support", lambda: None)

    report = run_charts_doctor(
        edition_name="demo_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
    )

    labels = [check.label for check in report.checks]
    assert "Chart Cache TTL" in labels
    assert "Chart Attachment Target" in labels
    assert "Chart Exec-Summary Uniqueness" in labels
    assert "Chart Renderer ID Namespace" in labels
    assert "Chart Renderer ID Uniqueness" in labels


def test_run_doctor_charts_uses_extracted_chart_module(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "editions"
    reports_root = tmp_path / "reports"
    programs_root.mkdir(parents=True)
    editions_root.mkdir(parents=True)
    reports_root.mkdir(parents=True)

    monkeypatch.setattr(
        "src.commands.doctor_checks.chart_checks.resolve_edition",
        lambda edition_name, **kwargs: SimpleNamespace(
            program=SimpleNamespace(id="demo"),
            raw_program={"charts": None},
            edition=SimpleNamespace(cadence="weekly"),
            workstreams=[],
        ),
    )

    report = run_doctor(
        charts=True,
        edition_name="demo_weekly",
        programs_root=programs_root,
        editions_root=editions_root,
        reports_root=reports_root,
    )

    assert report.checks[0].label == "Charts"
    assert report.checks[0].detail == "Chart pipeline is disabled for this program."

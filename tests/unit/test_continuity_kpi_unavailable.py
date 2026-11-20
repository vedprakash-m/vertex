from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader
from jinja2 import select_autoescape

from src.core.view_models import ContinuityRenderData


def test_continuity_renders_selected_degraded_kpis_as_unavailable() -> None:
    templates = Path(__file__).resolve().parents[2] / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(templates)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
    )
    rendered = environment.get_template(
        "partials/continuity_unavailable_kpis.j2"
    ).render(
        kusto_sections=(
            SimpleNamespace(
                title="Deployment velocity",
                query_id="deployment-velocity",
                source_label="Selected KPI",
                message="Unavailable",
                is_degraded=True,
            ),
            SimpleNamespace(
                title="Available metric",
                query_id="available-metric",
                source_label="Selected KPI",
                message=None,
                is_degraded=False,
            ),
        )
    )

    assert "KPI data unavailable" in rendered
    assert "Deployment velocity" in rendered
    assert "Unavailable" in rendered
    assert "Available metric" not in rendered
    assert "deployment-velocity" in rendered


def test_continuity_template_places_unavailable_kpis_after_exec_summary() -> None:
    templates = Path(__file__).resolve().parents[2] / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(templates)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
    )
    section = SimpleNamespace(
        title="<script>Deployment velocity</script>",
        query_id="deployment-velocity",
        source_label="Selected KPI",
        message="Unavailable",
        is_degraded=True,
    )
    continuity = ContinuityRenderData(
        brand_name=None,
        brand_header_url=None,
        edition_intro=None,
        cadence_note="",
        scorecard_bands=(),
        jump_links=(),
        chapters=(),
    )
    health = SimpleNamespace(
        needs_input_count=0,
        total_count=1,
        health_state="Low",
        trajectory="stable",
        leadership_ask="",
        risk_register_summary=None,
        milestone_summary=None,
        telemetry_summary=None,
    )

    rendered = environment.get_template("archetypes/continuity.j2").render(
        title="Continuity",
        subtitle="",
        preheader="",
        header_label=None,
        continuity=continuity,
        health=health,
        report=SimpleNamespace(exec_summary_text="Current summary"),
        edition=SimpleNamespace(
            manifest_id="synthetic",
            qg_status="",
            generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        manifest=None,
        kusto_sections=(section,),
        milestone_rows=(),
        hidden_render_sections=frozenset(),
        show_footer=False,
    )

    assert rendered.index("Executive Summary") < rendered.index("KPI data unavailable")
    assert "<script>Deployment velocity</script>" not in rendered
    assert "&lt;script&gt;Deployment velocity&lt;/script&gt;" in rendered
    assert "width:680px" in rendered and "width:640px" in rendered

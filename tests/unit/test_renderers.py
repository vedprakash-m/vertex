from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from src.core.html_renderer import HTMLRenderer, RenderContext, build_render_payload
from src.core.models import AttributionTier, Confidence, DeltaKind, DeltaSet, DimensionRisk, EditionType
from src.core.models import EvidencePacket, FreshnessItem, FreshnessReport, ItemDelta, PersonProfile, ProgramContext, ReportData
from src.core.models import ReviewSection, ReviewState, ReviewStatus, RiskLevel, RunManifest, ScorecardDelta
from src.core.models import ScorecardEvidencePacket, WorkItem, Workstream
from src.core.template_contract_loader import TemplateFamilyContract, TemplateSectionRule
from src.core.teams_renderer import TeamsRenderer
from src.core.view_models import Citation, EditionMeta, HealthSummary, KpiTile, KustoMetric, KustoSectionData, KustoTableCell
from src.core.view_models import ScorecardData, Top3Item, WorkstreamData


def _evidence(work_item_id: int) -> EvidencePacket:
    return EvidencePacket(
        work_item_id=work_item_id,
        revisions=(),
        comments=(),
        enrichments=(),
        confidence=Confidence.MEDIUM,
        tier=AttributionTier.TIER2,
        summary_for_reviewer=f"Evidence for {work_item_id}",
    )


def _work_item(work_item_id: int, title: str, risk_level: RiskLevel, target_date: date) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=title,
        state="Active",
        assigned_to="Vertex Maintainer",
        assigned_to_email="maintainer@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=target_date,
        risk_level=risk_level,
        tags=["acme"],
        custom_fields={},
    )


def _render_context() -> RenderContext:
    generated_at = datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc)
    ado_data_as_of = datetime(2026, 5, 5, 8, 45, tzinfo=timezone.utc)
    first_item = _work_item(101, "Deployment rollout tooling", RiskLevel.MEDIUM, date(2026, 5, 12))
    second_item = _work_item(102, "Safety validation runbook", RiskLevel.LOW, date(2026, 5, 18))

    deltas = DeltaSet(
        issue_number=78,
        previous_issue_number=77,
        new_items=(
            ItemDelta(
                work_item_id=102,
                kind=DeltaKind.NEW,
                field_changes={"id": (None, "102")},
                old_risk=None,
                new_risk=RiskLevel.LOW,
                old_eta=None,
                new_eta=date(2026, 5, 18),
                evidence=_evidence(102),
            ),
        ),
        closed_items=(),
        risk_changes=(
            ItemDelta(
                work_item_id=101,
                kind=DeltaKind.RISK_UP,
                field_changes={"risk_level": ("low", "medium")},
                old_risk=RiskLevel.LOW,
                new_risk=RiskLevel.MEDIUM,
                old_eta=date(2026, 5, 10),
                new_eta=date(2026, 5, 12),
                evidence=_evidence(101),
            ),
        ),
        eta_changes=(),
        unchanged_count=0,
    )

    program = ProgramContext(
        program_name="Program Hygiene",
        mission="Ramp readiness",
        pillars=("Safety", "Velocity"),
        workstreams=(
            Workstream(
                name="Deployment",
                aliases=("Acme",),
                area_paths=("One\\Adventure\\Acme",),
                dri_email="maintainer@example.com",
                description="Deployment workstream",
            ),
        ),
        glossary={},
        people=(
            PersonProfile(
                email="maintainer@example.com",
                display_name="Vertex Maintainer",
                role="PM",
                workstreams=("Deployment",),
            ),
        ),
    )

    review_status = ReviewStatus(
        issue_number=78,
        sections=(
            ReviewSection(
                section_id="exec_summary",
                state=ReviewState.APPROVED,
                reviewer="lead@example.com",
                note=None,
                updated_at=generated_at,
            ),
        ),
    )

    dimension = DimensionRisk(
        name="Deployment Velocity",
        risk=RiskLevel.MEDIUM,
        summary="Velocity regressed after the last rollout window.",
        evidence=_evidence(101),
    )

    report = ReportData(
        issue_number=78,
        edition=EditionType.DETAILED,
        generated_at=generated_at,
        ado_data_as_of=ado_data_as_of,
        program=program,
        items=(first_item, second_item),
        deltas=deltas,
        scorecard=(dimension,),
        scorecard_deltas=(
            ScorecardDelta(
                dimension="Deployment Velocity",
                old_risk=RiskLevel.LOW,
                new_risk=RiskLevel.MEDIUM,
                delta_kind=DeltaKind.RISK_UP,
                summary="Velocity regressed after the last rollout window.",
            ),
        ),
        exec_summary_text="Deployment velocity regressed this week while safety readiness improved after the validation runbook refresh.",
        workstream_blurbs={"deployment": "Deployment velocity regressed this week after the last rollout window."},
        freshness=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        hygiene_warnings=(),
        review_status=review_status,
        manifest_id="12345678-1234-5678-1234-567812345678",
    )

    edition = EditionMeta(
        edition="acme_weekly",
        issue_number=78,
        generated_at=generated_at,
        ado_data_as_of=ado_data_as_of,
        manifest_id=report.manifest_id,
        qg_status="PASS",
    )

    scorecard = ScorecardData(scorecard_name="Acme Ramp Readiness", dimensions=(dimension,))
    citation = Citation(
        work_item_id=101,
        title=first_item.title,
        ado_url="https://dev.azure.com/your-org/One/_workitems/edit/101",
        tier=AttributionTier.TIER2,
    )
    workstream = WorkstreamData(
        section_id="deployment",
        title="Deployment",
        blurb="Deployment velocity regressed this week after the last rollout window.",
        dependency_cascades=(),
        items=(first_item, second_item),
        citations=(citation,),
        review_state=ReviewState.APPROVED,
    )
    kusto_section = KustoSectionData(
        section_id="kusto-fleet-health",
        title="Fleet Health",
        query_id="fleet-health",
        render_mode="metric_highlight",
        source_label="adx.contoso.net/fleet",
        confidence="high",
        columns=("Snapshot", "HealthyPct", "Nodes"),
        rows=((KustoTableCell(text="Current"), KustoTableCell(text="99.1"), KustoTableCell(text="1231")),),
        metrics=(
            KustoMetric(label="Healthy Pct", value="99.1"),
            KustoMetric(label="Nodes", value="1231"),
        ),
        image_data_url=None,
        reference_url="https://dash.example/fleet",
        caveats=("Synthetic fixture",),
        message=None,
        is_degraded=False,
    )
    manifest = RunManifest(
        manifest_id=report.manifest_id,
        issue_number=78,
        edition="acme_weekly",
        started_at=generated_at,
        ended_at=generated_at,
        config_hash="sha256:config",
        snapshot_hash="sha256:snapshot",
        html_hash="sha256:html",
        md_hash="sha256:md",
        ado_calls=3,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
        git_sha="abcdef0",
    )

    return RenderContext(
        title="Program Hygiene | Issue 78 | May 05, 2026",
        subtitle="~4 min read | Detailed Edition | Data as of May 05 08:45 UTC",
        preheader="Focused edition highlighting readiness deltas.",
        report=report,
        edition_meta=edition,
        health=HealthSummary(
            overall_risk=RiskLevel.MEDIUM,
            high_count=0,
            medium_count=1,
            low_count=1,
            done_count=0,
            total_count=2,
            delta_direction="degraded",
            prior_counts={"high": 0, "medium": 0, "low": 2, "done": 0},
        ),
        top_items=(
            Top3Item(
                item_type="risk",
                text="Deployment velocity needs LT attention before the next rollout.",
                owner="Vertex Maintainer",
                ado_link="https://dev.azure.com/your-org/One/_workitems/edit/101",
                anchor="deployment",
            ),
        ),
        scorecards=(scorecard,),
        kusto_sections=(kusto_section,),
        workstreams=(workstream,),
        exec_summary_citations=(citation,),
        manifest=manifest,
        prior_date_label="May 01",
        changes_url="https://dev.azure.com/your-org/One/_queries/changed",
        item_urls={
            101: "https://dev.azure.com/your-org/One/_workitems/edit/101",
            102: "https://dev.azure.com/your-org/One/_workitems/edit/102",
        },
        scorecard_packets={
            "Acme Ramp Readiness": {
                "Deployment Velocity": ScorecardEvidencePacket(
                    dimension_name="Deployment Velocity",
                    dimension_description="Rollout speed and execution health",
                    total_items=2,
                    items_by_risk={"medium": 1, "low": 1},
                    stale_items=(),
                    stale_count=0,
                    overdue_items=(),
                    overdue_count=0,
                    blocked_items=(),
                    blocked_count=0,
                    unowned_items=(),
                    unowned_count=0,
                    high_activity_items=(101,),
                    prior_confirmed_risk=RiskLevel.LOW,
                    author_risk=None,
                    ado_query_url="https://dev.azure.com/your-org/One/_queries/deployment-velocity",
                    item_links=(
                        "https://dev.azure.com/your-org/One/_workitems/edit/101",
                        "https://dev.azure.com/your-org/One/_workitems/edit/102",
                    ),
                    item_ids=(101, 102),
                ),
            },
        },
        scorecard_deltas={
            "Acme Ramp Readiness": {
                "Deployment Velocity": report.scorecard_deltas[0],
            },
        },
        scorecard_urls={"Acme Ramp Readiness": "https://dev.azure.com/your-org/One/_queries/acme-scorecard"},
        workstream_urls={"deployment": "https://dev.azure.com/your-org/One/_queries/deployment"},
    )


def test_html_renderer_renders_detailed_newsletter(repo_root) -> None:
    renderer = HTMLRenderer("acme_weekly")
    rendered = renderer.render(_render_context())

    assert "DECISIONS &amp; SIGNALS" in rendered
    assert "WHAT CHANGED" in rendered
    assert "Acme Ramp Readiness scorecard (Risk levels)" in rendered
    assert "Fleet Health" in rendered
    assert "Deployment velocity regressed this week" in rendered
    assert 'width="680"' in rendered
    assert rendered.index("DECISIONS &amp; SIGNALS") < rendered.index("Acme Ramp Readiness scorecard (Risk levels)")
    assert rendered.index("Acme Ramp Readiness scorecard (Risk levels)") < rendered.index("WHAT CHANGED")
    assert rendered.index("WHAT CHANGED") < rendered.index("Executive Summary")
    assert rendered.index("Executive Summary") < rendered.index('<a id="deployment"></a>')
    assert rendered.index('<a id="deployment"></a>') < rendered.index('<a id="kusto-fleet-health"></a>')


def test_html_renderer_renders_narrative_newsletter_without_scorecards() -> None:
    renderer = HTMLRenderer("acme_weekly")
    context = _render_context()
    narrative_context = replace(context, report=replace(context.report, edition=EditionType.NARRATIVE))

    rendered = renderer.render(narrative_context)

    assert "DECISIONS &amp; SIGNALS" in rendered
    assert "WHAT CHANGED" in rendered
    assert "Acme Ramp Readiness" not in rendered
    assert '<a id="deployment"></a>' in rendered
    assert "Deployment velocity regressed this week" in rendered
    assert "dimensions at High risk" not in rendered
    assert rendered.index("Deployment velocity regressed this week") < rendered.index("Executive Summary")


def test_html_renderer_renders_condensed_digest_without_scorecards_or_workstreams() -> None:
    renderer = HTMLRenderer("acme_weekly")
    context = _render_context()
    condensed_context = replace(context, report=replace(context.report, edition=EditionType.CONDENSED))

    rendered = renderer.render(condensed_context)

    assert "Daily Digest" in rendered
    assert "Change Summary" in rendered
    assert "Top 3 Summary" in rendered
    assert "Acme Ramp Readiness" not in rendered
    assert "Fleet Health" not in rendered
    assert "Executive Summary" not in rendered
    assert "Deployment velocity regressed this week" not in rendered


def test_html_renderer_warns_when_digest_template_is_selected() -> None:
    renderer = HTMLRenderer("acme_weekly")
    context = _render_context()
    payload = build_render_payload(context)

    with pytest.warns(DeprecationWarning, match=r"digest\.j2 is deprecated"):
        rendered = renderer.render_fragment("archetypes/digest.j2", **payload)

    assert "Daily Digest" in rendered


def test_html_renderer_renders_focused_newsletter_without_baseline_changes() -> None:
    renderer = HTMLRenderer("acme_weekly")
    context = _render_context()
    focused_contract = TemplateFamilyContract(
        name="focused",
        order=(
            "health",
            "top_3",
            "scorecards:all",
            "exec_summary",
            "selected_changes",
            "workstreams:all",
            "provenance",
        ),
        mandatory=("health", "scorecards:all", "exec_summary", "provenance"),
        optional=("top_3", "selected_changes", "workstreams:all"),
        rules={"selected_changes": TemplateSectionRule(render_only_if="baseline_available")},
    )
    no_baseline_context = replace(
        context,
        report=replace(
            context.report,
            edition=EditionType.FOCUSED,
            deltas=replace(context.report.deltas, previous_issue_number=None),
        ),
        template_contract=focused_contract,
        prior_date_label=None,
    )

    rendered = renderer.render(no_baseline_context)

    assert "DECISIONS &amp; SIGNALS" in rendered
    assert "WHAT CHANGED" not in rendered
    assert "Acme Ramp Readiness scorecard (Risk levels)" in rendered
    assert "Executive Summary" in rendered


def test_html_renderer_renders_focused_newsletter_changes_after_summary_when_baseline_exists() -> None:
    renderer = HTMLRenderer("acme_weekly")
    context = _render_context()
    focused_contract = TemplateFamilyContract(
        name="focused",
        order=(
            "health",
            "top_3",
            "scorecards:all",
            "exec_summary",
            "selected_changes",
            "workstreams:all",
            "provenance",
        ),
        mandatory=("health", "scorecards:all", "exec_summary", "provenance"),
        optional=("top_3", "selected_changes", "workstreams:all"),
        rules={"selected_changes": TemplateSectionRule(render_only_if="baseline_available")},
    )
    focused_context = replace(
        context,
        report=replace(context.report, edition=EditionType.FOCUSED),
        template_contract=focused_contract,
    )

    rendered = renderer.render(focused_context)

    assert rendered.index("Executive Summary") < rendered.index("WHAT CHANGED")


def test_html_renderer_focused_newsletter_calls_out_unchanged_dimensions() -> None:
    renderer = HTMLRenderer("acme_weekly")
    context = _render_context()
    focused_contract = TemplateFamilyContract(
        name="focused",
        order=(
            "health",
            "top_3",
            "scorecards:all",
            "exec_summary",
            "selected_changes",
            "workstreams:all",
            "provenance",
        ),
        mandatory=("health", "scorecards:all", "exec_summary", "provenance"),
        optional=("top_3", "selected_changes", "workstreams:all"),
        rules={"selected_changes": TemplateSectionRule(render_only_if="baseline_available")},
    )
    second_dimension = replace(
        context.scorecards[0].dimensions[0],
        name="Deployment Safety",
        summary="Safety checks remain stable.",
    )
    focused_scorecard = replace(
        context.scorecards[0],
        dimensions=(context.scorecards[0].dimensions[0], second_dimension),
    )
    focused_context = replace(
        context,
        report=replace(context.report, edition=EditionType.FOCUSED),
        scorecards=(focused_scorecard,),
        workstreams=(
            replace(context.workstreams[0], section_id="acme-ramp-readiness-deployment-velocity"),
        ),
        template_contract=focused_contract,
        scorecard_packets={
            "Acme Ramp Readiness": {
                "Deployment Velocity": context.scorecard_packets["Acme Ramp Readiness"]["Deployment Velocity"],
                "Deployment Safety": replace(
                    context.scorecard_packets["Acme Ramp Readiness"]["Deployment Velocity"],
                    dimension_name="Deployment Safety",
                ),
            },
        },
    )

    rendered = renderer.render(focused_context)

    assert "1 dimension unchanged - see Issue 77 (May 01) for details." in rendered


def test_scorecard_partial_uses_row_fallback_when_mobile_safe_mode_is_forced() -> None:
    renderer = HTMLRenderer("acme_weekly")
    context = _render_context()
    second_dimension = replace(
        context.scorecards[0].dimensions[0],
        name="Deployment Safety",
        summary="Safety checks remain stable.",
    )
    row_scorecard = replace(
        context.scorecards[0],
        dimensions=(context.scorecards[0].dimensions[0], second_dimension),
    )
    row_context = replace(
        context,
        scorecards=(row_scorecard,),
        scorecard_packets={
            "Acme Ramp Readiness": {
                "Deployment Velocity": context.scorecard_packets["Acme Ramp Readiness"]["Deployment Velocity"],
                "Deployment Safety": replace(
                    context.scorecard_packets["Acme Ramp Readiness"]["Deployment Velocity"],
                    dimension_name="Deployment Safety",
                ),
            },
        },
        mobile_safe_scorecards="row",
    )
    payload = build_render_payload(row_context)

    rendered = renderer.render_fragment("partials/scorecard.j2", **{**payload, "sc": row_scorecard})

    assert 'width="42%"' in rendered


def test_nav_bar_fragment_includes_accessible_jump_link_labels() -> None:
    renderer = HTMLRenderer("acme_weekly")
    payload = build_render_payload(_render_context())

    rendered = renderer.render_fragment("partials/nav_bar.j2", **payload)

    assert 'aria-label="Jump to Health section"' in rendered
    assert 'aria-label="Jump to Decisions section"' in rendered
    assert 'aria-label="Jump to Scorecards section"' in rendered
    assert 'aria-label="Jump to Changes section"' in rendered
    assert 'aria-label="Jump to Details section"' in rendered


def test_workstream_risk_chip_includes_accessible_label() -> None:
    renderer = HTMLRenderer("acme_weekly")
    rendered = renderer.render_fragment("partials/risk_chip.j2", level=RiskLevel.MEDIUM)

    assert 'aria-label="Medium risk"' in rendered


@pytest.mark.parametrize("flag", [False, True])
def test_type_scale_v2_flag_controls_typography_partial(flag: bool) -> None:
    renderer = HTMLRenderer("acme_weekly")
    context = replace(_render_context(), type_scale_v2=flag)
    rendered = renderer.render(context)
    marker = "Typography v2"
    if flag:
        assert marker in rendered, "type_scale_v2=True should include the typography_v2 partial"
    else:
        assert marker not in rendered, "type_scale_v2=False should not include the typography_v2 partial"


def test_workstream_partial_renders_markdown_bullets_links_and_blocked_label() -> None:
    from src.core.jinja_filters import configure_ado_web_url
    configure_ado_web_url(org="your-org", project="your-project")
    renderer = HTMLRenderer("acme_weekly")
    context = _render_context()
    schie_workstream = replace(
        context.workstreams[0],
        section_id="acme-adventure-xio-100-ramp-readiness-schie-gaps",
        title="SCHIE Gaps",
        risk=RiskLevel.HIGH,
        eta_label="05/15",
        blurb=(
            "SCHIE is the diagnostics and repairs parity lane for Acme.\n\n"
            "- Blocked on ADO#36923425\n"
            "- Fast follow on ADO#3393076"
        ),
        significant_findings=(
            "ADO#36923425: Target slipped 2 times in the last 90 days; current target 2026-05-15.",
            "ADO#36928928: Linked work: Task ADO#36930000 - Validate SCHIE parity.",
        ),
        ado_query_url="https://example.invalid/query",
        edit_path="narratives/issue_077/ws_nova-adventure-xio-100-ramp-readiness-schie-gaps.md",
    )
    payload = build_render_payload(replace(context, workstreams=(schie_workstream,), is_dry_run=True))

    rendered = renderer.render_fragment("partials/workstream.j2", **{**payload, "ws": schie_workstream})
    teams_rendered = TeamsRenderer("acme_weekly").render(replace(context, workstreams=(schie_workstream,)))

    assert "Blocked" in rendered
    assert 'aria-label="High risk"' in rendered
    assert "ETA 05/15" in rendered
    assert "<ul" in rendered
    assert 'href="https://dev.azure.com/your-org/your-project/_workitems/edit/36923425"' in rendered
    assert "ADO Findings" in rendered
    assert "Target slipped 2 times" in rendered
    assert "View evidence in ADO" not in rendered
    assert "Edit:" not in rendered
    assert "ADO findings:" in teams_rendered
    assert "ETA 05/15" in teams_rendered
    assert "Target slipped 2 times" in teams_rendered


def test_workstream_partial_renders_kpi_tiles() -> None:
    renderer = HTMLRenderer("acme_weekly")
    context = _render_context()
    kpi_workstream = replace(
        context.workstreams[0],
        kpi_tiles=(
            KpiTile(
                query_id="acme-deployment-velocity",
                label="Deploy P50 (hrs)",
                value="4.2",
                unit=None,
                trend=None,
                confidence="high",
                as_of=datetime(2026, 5, 5, 8, 45, tzinfo=timezone.utc),
                source_signal_id="signal-1",
            ),
            KpiTile(
                query_id="acme-fleet-health",
                label="Fleet Healthy %",
                value="99.1",
                unit=None,
                trend=None,
                confidence="medium",
                as_of=datetime(2026, 5, 5, 8, 45, tzinfo=timezone.utc),
                source_signal_id="signal-2",
            ),
        ),
    )
    payload = build_render_payload(replace(context, workstreams=(kpi_workstream,), is_dry_run=True))

    rendered = renderer.render_fragment("partials/workstream.j2", **{**payload, "ws": kpi_workstream})

    assert "Deploy P50 (hrs)" in rendered
    assert ">4.2<" in rendered
    assert "Fleet Healthy %" in rendered
    assert ">99.1<" in rendered


def test_custom_filters_are_registered_and_partials_render_independently() -> None:
    renderer = HTMLRenderer("acme_weekly")
    context = _render_context()
    payload = renderer.render_fragment(
        "partials/risk_chip.j2",
        level=RiskLevel.HIGH,
    )

    assert "High" in payload
    inline = renderer.environment.from_string("{{ level | risk_label }}")
    assert inline.render(level=RiskLevel.MEDIUM) == "Medium"

    render_payload = renderer.environment.globals.copy()
    render_payload.update(renderer.render_fragment.__self__.environment.globals)
    base_context = renderer.environment.globals.copy()
    base_context.update({})
    payload = __import__("src.core.html_renderer", fromlist=["build_render_payload"]).build_render_payload(context)
    partial_inputs = {
        "partials/nav_bar.j2": payload,
        "partials/health_banner.j2": payload,
        "partials/top_3_now.j2": payload,
        "partials/what_changed.j2": payload,
        "partials/scorecard.j2": {**payload, "sc": context.scorecards[0]},
        "partials/kusto_section.j2": {**payload, "ks": context.kusto_sections[0]},
        "partials/exec_summary.j2": payload,
        "partials/workstream.j2": {**payload, "ws": context.workstreams[0]},
        "partials/risk_chip.j2": {"level": RiskLevel.HIGH},
        "partials/delta_badge.j2": {"kind": DeltaKind.RISK_UP, "old": RiskLevel.LOW, "new": RiskLevel.MEDIUM},
        "partials/verify_chip.j2": {"evidence": context.scorecards[0].dimensions[0].evidence},
        "partials/provenance_footer.j2": payload,
    }

    for template_name, template_context in partial_inputs.items():
        rendered = renderer.render_fragment(template_name, **template_context)
        assert rendered.strip()


def test_build_render_payload_ranks_what_changed_rows_by_spec_priority() -> None:
    context = _render_context()
    items = (
        _work_item(201, "New high blocker", RiskLevel.HIGH, date(2026, 5, 9)),
        _work_item(202, "Decision-linked issue", RiskLevel.MEDIUM, date(2026, 5, 10)),
        _work_item(203, "Freshness block", RiskLevel.HIGH, date(2026, 5, 11)),
        _work_item(204, "Risk increase", RiskLevel.MEDIUM, date(2026, 5, 12)),
        _work_item(205, "ETA slip", RiskLevel.MEDIUM, date(2026, 5, 20)),
        _work_item(206, "Chronic high", RiskLevel.HIGH, date(2026, 5, 13)),
        _work_item(207, "Stale evidence", RiskLevel.MEDIUM, date(2026, 5, 14)),
        _work_item(208, "Risk reduction", RiskLevel.LOW, date(2026, 5, 15)),
        _work_item(209, "Completed item", RiskLevel.DONE, date(2026, 5, 16)),
    )
    deltas = DeltaSet(
        issue_number=78,
        previous_issue_number=77,
        new_items=(
            ItemDelta(
                work_item_id=201,
                kind=DeltaKind.NEW,
                field_changes={"id": (None, "201")},
                old_risk=None,
                new_risk=RiskLevel.HIGH,
                old_eta=None,
                new_eta=date(2026, 5, 9),
                evidence=_evidence(201),
            ),
        ),
        closed_items=(
            ItemDelta(
                work_item_id=209,
                kind=DeltaKind.CLOSED,
                field_changes={"state": ("Active", "Done")},
                old_risk=RiskLevel.LOW,
                new_risk=RiskLevel.DONE,
                old_eta=date(2026, 5, 16),
                new_eta=date(2026, 5, 16),
                evidence=_evidence(209),
            ),
        ),
        risk_changes=(
            ItemDelta(
                work_item_id=202,
                kind=DeltaKind.RISK_UP,
                field_changes={"risk_level": ("low", "medium")},
                old_risk=RiskLevel.LOW,
                new_risk=RiskLevel.MEDIUM,
                old_eta=date(2026, 5, 10),
                new_eta=date(2026, 5, 10),
                evidence=_evidence(202),
            ),
            ItemDelta(
                work_item_id=203,
                kind=DeltaKind.RISK_UP,
                field_changes={"risk_level": ("medium", "high")},
                old_risk=RiskLevel.MEDIUM,
                new_risk=RiskLevel.HIGH,
                old_eta=date(2026, 5, 11),
                new_eta=date(2026, 5, 11),
                evidence=_evidence(203),
            ),
            ItemDelta(
                work_item_id=204,
                kind=DeltaKind.RISK_UP,
                field_changes={"risk_level": ("low", "medium")},
                old_risk=RiskLevel.LOW,
                new_risk=RiskLevel.MEDIUM,
                old_eta=date(2026, 5, 12),
                new_eta=date(2026, 5, 12),
                evidence=_evidence(204),
            ),
            ItemDelta(
                work_item_id=208,
                kind=DeltaKind.RISK_DOWN,
                field_changes={"risk_level": ("medium", "low")},
                old_risk=RiskLevel.MEDIUM,
                new_risk=RiskLevel.LOW,
                old_eta=date(2026, 5, 15),
                new_eta=date(2026, 5, 15),
                evidence=_evidence(208),
            ),
        ),
        eta_changes=(
            ItemDelta(
                work_item_id=205,
                kind=DeltaKind.ETA_CHANGED,
                field_changes={"target_date": ("2026-05-09", "2026-05-20")},
                old_risk=RiskLevel.MEDIUM,
                new_risk=RiskLevel.MEDIUM,
                old_eta=date(2026, 5, 9),
                new_eta=date(2026, 5, 20),
                evidence=_evidence(205),
            ),
        ),
        unchanged_count=0,
    )

    packet = ScorecardEvidencePacket(
        dimension_name="Deployment Velocity",
        dimension_description="Rollout speed and execution health",
        total_items=4,
        items_by_risk={"high": 1, "medium": 2, "low": 1},
        stale_items=(207,),
        stale_count=1,
        overdue_items=(),
        overdue_count=0,
        blocked_items=(),
        blocked_count=0,
        unowned_items=(),
        unowned_count=0,
        high_activity_items=(206, 207),
        prior_confirmed_risk=RiskLevel.HIGH,
        author_risk=None,
        ado_query_url="https://dev.azure.com/your-org/One/_queries/deployment-velocity",
        item_links=tuple(f"https://dev.azure.com/your-org/One/_workitems/edit/{item_id}" for item_id in (206, 207)),
        item_ids=(206, 207),
        derived_risk=RiskLevel.HIGH,
        streak_count=3,
        is_stale_dimension=True,
    )
    scorecard = ScorecardData(
        scorecard_name="Acme Ramp Readiness",
        dimensions=(
            DimensionRisk(
                name="Deployment Velocity",
                risk=RiskLevel.HIGH,
                summary="Sustained pressure remains in deployment velocity.",
                evidence=_evidence(206),
            ),
        ),
    )

    payload = build_render_payload(
        replace(
            context,
            report=replace(
                context.report,
                items=items,
                deltas=deltas,
                freshness=FreshnessReport(
                    issue_number=78,
                    items=(
                        FreshnessItem(
                            work_item_id=203,
                            rule_id="FR-45",
                            severity="block",
                            message="Awaiting response from the owner.",
                            suggested_fix=None,
                        ),
                    ),
                    blocks=1,
                    warns=0,
                    infos=0,
                ),
            ),
            top_items=(
                Top3Item(
                    item_type="decision",
                    text="Decision-linked issue needs sponsor confirmation.",
                    owner="Vertex Maintainer",
                    ado_link="https://dev.azure.com/your-org/One/_workitems/edit/202",
                    anchor="deployment",
                ),
            ),
            scorecards=(scorecard,),
            item_urls={item.id: f"https://dev.azure.com/your-org/One/_workitems/edit/{item.id}" for item in items},
            scorecard_packets={"Acme Ramp Readiness": {"Deployment Velocity": packet}},
        )
    )

    assert [row["work_item_id"] for row in payload["delta_rows"]] == [
        "201",
        "203",
        "202",
        "204",
        "205",
        "206",
        "207",
        "208",
        "209",
    ]


def test_teams_renderer_renders_markdown_sections() -> None:
    renderer = TeamsRenderer("acme_weekly")
    rendered = renderer.render(_render_context())

    assert "# Program Hygiene | Issue 78 | May 05, 2026" in rendered
    assert "## Decisions & Signals" in rendered
    assert "## Acme Ramp Readiness" in rendered
    assert "## What Changed" in rendered
    assert "## Executive Summary" in rendered
    assert "## Deployment" in rendered
    assert "## Fleet Health" in rendered
    assert rendered.index("## Program Health") < rendered.index("## Decisions & Signals")
    assert rendered.index("## Decisions & Signals") < rendered.index("## Acme Ramp Readiness")
    assert rendered.index("## Acme Ramp Readiness") < rendered.index("## What Changed")
    assert rendered.index("## What Changed") < rendered.index("## Executive Summary")
    assert rendered.index("## Executive Summary") < rendered.index("## Deployment")
    assert rendered.index("## Deployment") < rendered.index("## Fleet Health")
    assert "Manifest 12345678" in rendered


def test_renderers_group_fleet_parity_kusto_sections_once() -> None:
    context = _render_context()
    fleet_delta = KustoSectionData(
        section_id="kusto-acme-vs-fabric-p50-delta",
        title="Fleet Parity",
        query_id="acme-vs-fabric-p50-delta",
        render_mode="metric_highlight",
        source_label="azcore/XKulfiTelemetry",
        confidence="medium",
        columns=("DeltaMins",),
        rows=(),
        metrics=(KustoMetric(label="Acme vs Fabric STG P50 Delta (mins)", value="12"),),
        image_data_url=None,
        reference_url="https://dataexplorer.azure.com/",
        caveats=(),
        message=None,
        is_degraded=False,
    )
    fleet_gaps = KustoSectionData(
        section_id="kusto-acme-fabric-parity-gap-count",
        title="Fleet Parity",
        query_id="acme-fabric-parity-gap-count",
        render_mode="metric_highlight",
        source_label="1es/AzureDevOps",
        confidence="medium",
        columns=("OpenFabricParityGaps",),
        rows=(),
        metrics=(KustoMetric(label="Fabric Parity Gaps", value="3"),),
        image_data_url=None,
        reference_url="https://dev.azure.com/your-org/your-project/One/",
        caveats=(),
        message=None,
        is_degraded=False,
    )

    grouped_context = replace(context, kusto_sections=(fleet_delta, fleet_gaps, context.kusto_sections[0]))
    payload = build_render_payload(grouped_context)

    fleet_sections = [section for section in payload["ordered_sections"] if section.kind == "kusto_group"]
    assert len(fleet_sections) == 1
    assert fleet_sections[0].anchor_id == "fleet-parity"
    assert [section.query_id for section in fleet_sections[0].kusto_group_sections] == [
        "acme-vs-fabric-p50-delta",
        "acme-fabric-parity-gap-count",
    ]

    rendered = TeamsRenderer("acme_weekly").render(grouped_context)
    assert rendered.count("## Fleet Parity") == 1
    assert "Acme vs Fabric STG P50 Delta (mins)" in rendered
    assert "Fabric Parity Gaps" in rendered


def test_teams_renderer_hides_markdown_provenance_footer_when_removed() -> None:
    renderer = TeamsRenderer("acme_weekly")
    rendered = renderer.render(replace(_render_context(), hidden_render_sections=frozenset({"provenance-status"})))

    assert "Manifest 12345678" not in rendered
    assert "Status pass" not in rendered.lower()


# UX-2: reviewer pane vertical rhythm — .review-list--compact class in base.reviewer.j2
def test_ux2_reviewer_section_card_css_uses_16px_padding() -> None:
    import pathlib
    template_src = (pathlib.Path(__file__).parents[2] / "templates" / "base.reviewer.j2").read_text(encoding="utf-8")
    assert "padding: 16px;" in template_src, "UX-2: .section-card should use 16px padding"
    assert "margin-bottom: 16px;" in template_src, "UX-2: .section-card should have 16px bottom margin"


# UX-3: risk-band color in scorecard tile border-top
def test_ux3_scorecard_tile_has_risk_band_border_top() -> None:
    renderer = HTMLRenderer("acme_weekly")
    context = _render_context()
    payload = build_render_payload(context)
    rendered = renderer.render_fragment("partials/scorecard.j2", **{**payload, "sc": context.scorecards[0]})
    assert "border-top:3px solid" in rendered, "UX-3: scorecard tile should have a risk-band border-top"


# UX-4: dependency lifecycle compact mode class in base.reviewer.j2 template
def test_ux4_reviewer_dependency_compact_mode_css_present() -> None:
    import pathlib
    template_src = (pathlib.Path(__file__).parents[2] / "templates" / "base.reviewer.j2").read_text(encoding="utf-8")
    assert "review-list--compact" in template_src, "UX-4: compact class must be defined in reviewer template"
    assert "review-list | length >= 10" in template_src or "dependency_lifecycle_rows | length >= 10" in template_src, \
        "UX-4: compact mode must trigger at ≥10 dependency rows"


# UX-5: provenance footer includes 8-char manifest hash
def test_ux5_provenance_footer_includes_8char_manifest_hash() -> None:
    renderer = HTMLRenderer("acme_weekly")
    payload = build_render_payload(_render_context())
    rendered = renderer.render_fragment("partials/provenance_footer.j2", **payload)
    # manifest_id in fixture is "12345678-1234-5678-1234-567812345678", first 8 chars = "12345678"
    assert "#12345678" in rendered, "UX-5: provenance footer should contain 8-char manifest hash prefixed with #"


def test_html_renderer_renders_workstream_source_footnote() -> None:
    context = _render_context()
    workstream = replace(
        context.workstreams[0],
        source_footnote="Signal sources: ADO tracking (2026-06-18); Acme Weekly standup transcript (2026-06-16).",
    )

    rendered = HTMLRenderer("acme_weekly").render(replace(context, workstreams=(workstream,)))

    assert "Signal sources: ADO tracking (2026-06-18); Acme Weekly standup transcript (2026-06-16)." in rendered


def test_teams_summary_uses_trend_description() -> None:
    chart_section = KustoSectionData(
        section_id="kusto-deploy-velocity",
        title="Deployment Velocity",
        query_id="deploy-velocity",
        render_mode="chart",
        source_label="adx.contoso.net/deploy",
        confidence="high",
        columns=("Date", "P50"),
        rows=(),
        metrics=(),
        image_data_url=None,
        reference_url="https://dash.example/deploy",
        caveats=(),
        message="P50 improved by 12% WoW",
        is_degraded=False,
        chart_png_base64="ZmFrZQ==",
        chart_png_size_bytes=4,
    )
    chart_no_message = replace(chart_section, message=None)

    renderer = TeamsRenderer("acme_weekly")

    with_message = renderer.render(replace(_render_context(), kusto_sections=(chart_section,)))
    assert "Deployment Velocity: P50 improved by 12% WoW. [View chart →](https://dash.example/deploy)" in with_message

    without_message = renderer.render(replace(_render_context(), kusto_sections=(chart_no_message,)))
    assert "Deployment Velocity. [View chart →](https://dash.example/deploy)" in without_message

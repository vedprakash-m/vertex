from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from src.core.delta_engine import build_deltas
from src.core.html_renderer import HTMLRenderer, RenderContext, build_render_payload
from src.core.models import AttributionTier, Confidence, DeltaKind, DeltaSet, DimensionRisk, EditionType
from src.core.models import EvidencePacket, FreshnessItem, FreshnessReport, ItemDelta, PersonProfile, ProgramContext, ReportData
from src.core.models import ReviewSection, ReviewState, ReviewStatus, RiskLevel, RunManifest, ScorecardDelta
from src.core.models import ScorecardEvidencePacket, Snapshot, SnapshotItem, WorkItem, Workstream
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
    assert 'href="#changes" aria-label="Jump to Changes section"' not in rendered
    assert "Acme Ramp Readiness scorecard (Risk levels)" in rendered
    assert "Executive Summary" in rendered


@pytest.mark.parametrize(
    ("name", "email", "expected"),
    [
        ("First Owner", None, "Unassigned \u2192 First Owner"),
        (None, "first@example.com", "Unassigned \u2192 first@example.com"),
        (None, None, None),
    ],
)
@pytest.mark.parametrize(
    "risk",
    [RiskLevel.BLOCKED, RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW, RiskLevel.DONE, RiskLevel.UNKNOWN],
)
def test_html_renderer_renders_first_owner_assignment_from_snapshot(
    name: str | None, email: str | None, expected: str | None, risk: RiskLevel,
) -> None:
    context = _render_context()
    current = replace(context.report.items[0], assigned_to=name, assigned_to_email=email, risk_level=risk)
    previous = SnapshotItem(
        id=current.id, type=current.type, title=current.title, state=current.state,
        assigned_to=None, area_path=current.area_path, target_date=current.target_date,
        risk_level=current.risk_level, tags=current.tags,
    )
    snapshot = Snapshot(
        issue_number=77, generated_at=context.report.generated_at,
        ado_data_as_of=context.report.ado_data_as_of, edition_type=EditionType.DETAILED,
        items=(previous,), scorecards=(),
    )
    deltas = build_deltas((current,), snapshot, issue_number=78, previous_issue_number=77)
    report = replace(context.report, items=(current,), deltas=deltas)

    rendered = HTMLRenderer("acme_weekly").render(replace(context, report=report))

    if expected is None:
        assert deltas.owner_changes == ()
        assert "WHAT CHANGED" not in rendered
    else:
        assert len(deltas.owner_changes) == 1
        assert expected in rendered
        assert "ADO#101" in rendered


def test_html_renderer_orders_blocked_owner_change_before_high() -> None:
    context = _render_context()
    items = (
        replace(context.report.items[0], assigned_to="Blocked Owner", assigned_to_email=None, risk_level=RiskLevel.BLOCKED),
        replace(context.report.items[1], assigned_to="High Owner", assigned_to_email=None, risk_level=RiskLevel.HIGH),
    )
    previous_items = tuple(
        SnapshotItem(
            id=item.id, type=item.type, title=item.title, state=item.state,
            assigned_to=None, area_path=item.area_path, target_date=item.target_date,
            risk_level=item.risk_level, tags=item.tags,
        )
        for item in items
    )
    snapshot = Snapshot(
        issue_number=77, generated_at=context.report.generated_at,
        ado_data_as_of=context.report.ado_data_as_of, edition_type=EditionType.DETAILED,
        items=previous_items, scorecards=(),
    )
    deltas = build_deltas(items, snapshot, issue_number=78, previous_issue_number=77)
    report = replace(context.report, items=items, deltas=deltas)

    rendered = HTMLRenderer("acme_weekly").render(
        replace(context, report=report, top_items=(), scorecards=(), scorecard_packets={})
    )

    assert rendered.index("Unassigned \u2192 Blocked Owner") < rendered.index("Unassigned \u2192 High Owner")
    assert "ADO#101" in rendered and "ADO#102" in rendered


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
    assert 'href="#changes" aria-label="Jump to Changes section"' in rendered
    assert '<a id="changes"></a>' in rendered


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


@pytest.mark.parametrize("edition", [EditionType.DETAILED, EditionType.FOCUSED, EditionType.LOOKBACK])
@pytest.mark.parametrize(
    ("hidden", "label"),
    [
        ("health", "Health"),
        ("top_3", "Decisions"),
        ("selected_changes", "Changes"),
        ("scorecard:acme-ramp-readiness", "Scorecards"),
        ("workstream:deployment", "Details"),
    ],
)
def test_navigation_does_not_link_to_hidden_sections(
    edition: EditionType, hidden: str, label: str,
) -> None:
    context = _render_context()
    context = replace(
        context,
        report=replace(context.report, edition=edition),
        hidden_render_sections=frozenset({hidden}),
    )
    renderer = HTMLRenderer("acme_weekly")
    rendered = renderer.render(context)
    navigation = renderer.render_fragment("partials/nav_bar.j2", **build_render_payload(context))

    assert f'aria-label="Jump to {label} section"' not in navigation
    assert f">{label}</td>" in navigation
    if hidden == "selected_changes":
        assert '<a id="changes"></a>' not in rendered


@pytest.mark.parametrize("edition", [EditionType.DETAILED, EditionType.FOCUSED, EditionType.LOOKBACK])
def test_navigation_links_to_first_rendered_scorecard_and_chapter(edition: EditionType) -> None:
    context = _render_context()
    second_scorecard = replace(context.scorecards[0], scorecard_name="Second Scorecard")
    second_workstream = replace(
        context.workstreams[0],
        section_id="second",
        edit_path=r"narratives\issue_078\chapter_second.md",
    )
    contract = TemplateFamilyContract(
        name="selected",
        order=("health", "top_3", "scorecard:second-scorecard", "selected_changes", "workstream:second"),
        mandatory=(),
        optional=(),
        rules={},
    )
    context = replace(
        context,
        report=replace(context.report, edition=edition),
        scorecards=(*context.scorecards, second_scorecard),
        workstreams=(*context.workstreams, second_workstream),
        template_contract=contract,
    )

    rendered = HTMLRenderer("acme_weekly").render(context)

    assert 'href="#second-scorecard" aria-label="Jump to Scorecards section"' in rendered
    assert '<a id="second-scorecard"></a>' in rendered
    assert 'href="#chapter-second" aria-label="Jump to Details section"' in rendered
    assert '<a id="chapter-second"></a>' in rendered


@pytest.mark.parametrize("edition", [EditionType.DETAILED, EditionType.FOCUSED, EditionType.LOOKBACK])
@pytest.mark.parametrize("visible", [False, True])
def test_scorecard_detail_links_follow_rendered_chapter(
    edition: EditionType, visible: bool,
) -> None:
    context = _render_context()
    section_id = "acme-ramp-readiness-deployment-velocity"
    workstream = replace(
        context.workstreams[0],
        section_id=section_id,
        edit_path=rf"narratives\issue_078\chapter_{section_id}.md",
    )
    context = replace(
        context,
        report=replace(context.report, edition=edition),
        workstreams=(workstream,),
        hidden_render_sections=frozenset() if visible else frozenset({f"workstream:{section_id}"}),
    )

    rendered = HTMLRenderer("acme_weekly").render(context)

    assert (f'<a id="chapter-{section_id}"></a>' in rendered) is visible
    assert rendered.count(f'href="#chapter-{section_id}"') == (2 if visible else 0)
    assert f'href="#{section_id}"' not in rendered


@pytest.mark.parametrize("edition", [EditionType.DETAILED, EditionType.FOCUSED, EditionType.LOOKBACK])
@pytest.mark.parametrize("visible", [False, True])
@pytest.mark.parametrize("chapter", [False, True])
@pytest.mark.parametrize("suggested", [False, True])
def test_decision_workstream_links_follow_rendered_sections(
    edition: EditionType, visible: bool, chapter: bool, suggested: bool,
) -> None:
    context = _render_context()
    workstream = replace(
        context.workstreams[0],
        edit_path=r"narratives\issue_078\chapter_deployment.md" if chapter else None,
    )
    item = replace(context.top_items[0], suggested=suggested)
    context = replace(
        context,
        report=replace(context.report, edition=edition),
        workstreams=(workstream,),
        top_items=() if suggested else (item,),
        auto_suggestions=(item,) if suggested else (),
        is_dry_run=suggested,
        hidden_render_sections=frozenset() if visible else frozenset({"workstream:deployment"}),
    )
    renderer = HTMLRenderer("acme_weekly")
    decision_strip = renderer.render_fragment("partials/top_3_now.j2", **build_render_payload(context))
    rendered = renderer.render(context)
    anchor = "chapter-deployment" if chapter else "deployment"

    assert (f'href="#{anchor}"' in decision_strip) is visible
    assert (f'<a id="{anchor}"></a>' in rendered) is visible
    if chapter or not visible:
        assert 'href="#deployment"' not in decision_strip
    assert f'href="{item.ado_link}"' in decision_strip


@pytest.mark.parametrize("chapter", [False, True])
def test_narrative_decision_link_follows_its_rendered_workstream(chapter: bool) -> None:
    context = _render_context()
    workstream = replace(
        context.workstreams[0],
        edit_path=r"narratives\issue_078\chapter_deployment.md" if chapter else None,
    )
    context = replace(
        context,
        report=replace(context.report, edition=EditionType.NARRATIVE),
        workstreams=(workstream,),
        hidden_render_sections=frozenset({"workstream:deployment"}),
    )
    rendered = HTMLRenderer("acme_weekly").render(context)
    anchor = "chapter-deployment" if chapter else "deployment"

    assert f'href="#{anchor}"' in rendered
    assert f'<a id="{anchor}"></a>' in rendered


@pytest.mark.parametrize("edition", [EditionType.DETAILED, EditionType.FOCUSED, EditionType.LOOKBACK])
@pytest.mark.parametrize("visible", [False, True])
def test_decision_link_recognizes_authored_chapter_anchor(edition: EditionType, visible: bool) -> None:
    context = _render_context()
    workstream = replace(
        context.workstreams[0],
        edit_path=r"narratives\issue_078\chapter_deployment.md",
    )
    item = replace(context.top_items[0], anchor="chapter-deployment")
    context = replace(
        context,
        report=replace(context.report, edition=edition),
        workstreams=(workstream,),
        top_items=(item,),
        hidden_render_sections=frozenset() if visible else frozenset({"workstream:deployment"}),
    )
    renderer = HTMLRenderer("acme_weekly")
    decision_strip = renderer.render_fragment("partials/top_3_now.j2", **build_render_payload(context))
    rendered = renderer.render(context)

    assert ('href="#chapter-deployment"' in decision_strip) is visible
    assert ('<a id="chapter-deployment"></a>' in rendered) is visible


@pytest.mark.parametrize("custom_anchor", ["custom-target", "chapter-custom"])
def test_decision_link_omits_empty_workstream_but_preserves_custom_anchors(custom_anchor: str) -> None:
    context = _render_context()
    empty_workstream = replace(context.workstreams[0], blurb="", items=())
    context = replace(context, workstreams=(empty_workstream,))
    renderer = HTMLRenderer("acme_weekly")

    omitted = renderer.render_fragment("partials/top_3_now.j2", **build_render_payload(context))
    custom = replace(context.top_items[0], anchor=custom_anchor)
    preserved = renderer.render_fragment(
        "partials/top_3_now.j2",
        **build_render_payload(replace(context, top_items=(custom,))),
    )

    assert 'href="#deployment"' not in omitted
    assert f'href="#{custom_anchor}"' in preserved


def test_decision_link_omits_workstream_excluded_by_template_contract() -> None:
    context = _render_context()
    contract = TemplateFamilyContract(
        name="selected",
        order=("health", "top_3"),
        mandatory=(),
        optional=(),
        rules={},
    )
    context = replace(context, template_contract=contract)

    rendered = HTMLRenderer("acme_weekly").render(context)

    assert 'href="#deployment"' not in rendered
    assert '<a id="deployment"></a>' not in rendered


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
    exampleteam_workstream = replace(
        context.workstreams[0],
        section_id="acme-adventure-syn-a-100-ramp-readiness-exampleteam-gaps",
        title="EXAMPLETEAM Gaps",
        risk=RiskLevel.HIGH,
        eta_label="05/15",
        blurb=(
            "EXAMPLETEAM is the diagnostics and repairs parity lane for Acme.\n\n"
            "- Blocked on ADO#900108\n"
            "- Fast follow on ADO#900103"
        ),
        significant_findings=(
            "ADO#900108: Target slipped 2 times in the last 90 days; current target 2026-05-15.",
            "ADO#900109: Linked work: Task ADO#36930000 - Validate EXAMPLETEAM parity.",
        ),
        ado_query_url="https://example.invalid/query",
        edit_path="narratives/issue_077/ws_nova-adventure-syn-a-100-ramp-readiness-exampleteam-gaps.md",
    )
    payload = build_render_payload(replace(context, workstreams=(exampleteam_workstream,), is_dry_run=True))

    rendered = renderer.render_fragment("partials/workstream.j2", **{**payload, "ws": exampleteam_workstream})
    teams_rendered = TeamsRenderer("acme_weekly").render(replace(context, workstreams=(exampleteam_workstream,)))

    assert "Blocked" in rendered
    assert 'aria-label="High risk"' in rendered
    assert "ETA 05/15" in rendered
    assert "<ul" in rendered
    assert 'href="https://dev.azure.com/your-org/your-project/_workitems/edit/900108"' in rendered
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
        source_label="azcore/ExampleDb2",
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


def _render_chart_placement(placement, section, tmp_path, repo_root):
    from src.core.stages.render_stage import _route_chart_placements

    context = replace(_render_context(), kusto_sections=())
    if placement in {"standalone_registry", "standalone_legacy"}:
        target = "standalone"
    elif placement in {"workstream_registry", "workstream_legacy"}:
        target = f"workstream:{context.workstreams[0].section_id}"
    elif placement == "executive":
        target = "exec_summary"
    else:
        raise AssertionError(f"Unknown chart placement: {placement}")
    sections, executive, workstreams = _route_chart_placements(
        (replace(section, section_placement=target),), context.workstreams,
    )
    context = replace(context, kusto_sections=sections, exec_summary_chart=executive, workstreams=workstreams)
    return HTMLRenderer("acme_weekly", reports_root=tmp_path, templates_root=repo_root / "templates").render(context)


def _chart_images(rendered):
    from html.parser import HTMLParser

    class ImageParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.images = []

        def handle_starttag(self, tag, attrs):
            if tag == "img":
                self.images.append(dict(attrs))

    parser = ImageParser()
    parser.feed(rendered)
    return [image for image in parser.images if (image.get("src") or "").startswith("data:image/png;base64,")]


def _chart_section(placement, *, title="Deployment", message=None):
    return replace(
        _render_context().kusto_sections[0],
        title=title,
        render_mode="chart_image" if placement.endswith("legacy") else "chart",
        message=message,
        image_data_url="data:image/png;base64,ZmFrZQ==",
        chart_png_base64="ZmFrZQ==",
        chart_png_size_bytes=4,
        captured_at=None,
        cache_captured_at=None,
        is_degraded=False,
    )


@pytest.mark.parametrize(
    "placement",
    ["standalone_registry", "standalone_legacy", "workstream_registry", "workstream_legacy", "executive"],
)
@pytest.mark.parametrize("message", [None, "", "P50 improved by 12%"])
def test_chart_alt_includes_message_without_changing_empty_fallback(placement, message, tmp_path, repo_root):
    section = _chart_section(placement, message=message)
    images = _chart_images(_render_chart_placement(placement, section, tmp_path, repo_root))

    assert len(images) == 1
    expected = "Deployment chart" + (f": {message}" if message else "")
    assert images[0]["alt"] == expected


@pytest.mark.parametrize(
    "placement",
    ["standalone_registry", "standalone_legacy", "workstream_registry", "workstream_legacy", "executive"],
)
@pytest.mark.parametrize("marked_safe", [False, True])
def test_chart_alt_and_kusto_text_are_literal_and_attribute_safe(placement, marked_safe, tmp_path, repo_root):
    from markupsafe import Markup

    title = 'Latency " onerror="unexpected & <em>peak</em>'
    message = 'Trend " onerror="unexpected & <script>sentinel</script> Δ'
    if marked_safe:
        title, message = Markup(title), Markup(message)
    section = _chart_section(placement, title=title, message=message)

    rendered = _render_chart_placement(placement, section, tmp_path, repo_root)
    images = _chart_images(rendered)

    assert len(images) == 1
    assert images[0]["alt"] == f"{title} chart: {message}"
    assert "onerror" not in images[0]
    assert "<em>peak</em>" not in rendered
    assert "<script>sentinel</script>" not in rendered


@pytest.mark.parametrize("render_mode", ["metric_highlight", "table"])
@pytest.mark.parametrize("marked_safe", [False, True])
def test_kusto_non_chart_title_and_message_are_literal(render_mode, marked_safe, tmp_path, repo_root):
    from markupsafe import Markup

    title = "<em>Latency</em> & load"
    message = "<script>sentinel</script> & trend"
    if marked_safe:
        title, message = Markup(title), Markup(message)
    section = replace(
        _render_context().kusto_sections[0],
        render_mode=render_mode,
        title=title,
        message=message,
    )

    rendered = _render_chart_placement("standalone_registry", section, tmp_path, repo_root)

    assert "&lt;em&gt;Latency&lt;/em&gt; &amp; load" in rendered
    assert "&lt;script&gt;sentinel&lt;/script&gt; &amp; trend" in rendered
    assert "<em>Latency</em>" not in rendered
    assert "<script>sentinel</script>" not in rendered


@pytest.mark.parametrize("placement", ["standalone_registry", "workstream_registry", "executive"])
@pytest.mark.parametrize("cached", [False, True])
def test_chart_alt_timestamp_uses_existing_utc_formatter(placement, cached, tmp_path, repo_root):
    from datetime import timedelta

    observed = datetime(2026, 6, 18, 19, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    cached_at = datetime(2026, 6, 17, 7, 45, tzinfo=timezone.utc)
    section = replace(
        _chart_section(placement, message="Trend stable"),
        captured_at=observed,
        cache_captured_at=cached_at if cached else None,
        is_degraded=cached,
    )

    rendered = _render_chart_placement(placement, section, tmp_path, repo_root)

    assert _chart_images(rendered)[0]["alt"] == "Deployment chart: Trend stable"
    if cached:
        assert "Jun 17 2026, 07:45 UTC" in rendered
        assert "live data unavailable" in rendered
        assert "Jun 18 2026, 14:00 UTC" not in rendered
    else:
        assert "Jun 18 2026, 14:00 UTC" in rendered
        assert "live data unavailable" not in rendered


@pytest.mark.parametrize(
    "placement",
    ["standalone_registry", "standalone_legacy", "workstream_registry", "workstream_legacy", "executive"],
)
def test_chart_alt_missing_image_does_not_create_image(placement, tmp_path, repo_root):
    section = replace(_chart_section(placement, message="No chart"), image_data_url=None, chart_png_base64=None)

    assert _chart_images(_render_chart_placement(placement, section, tmp_path, repo_root)) == []


@pytest.mark.parametrize(
    ("placement", "title"),
    [
        ("standalone_registry", "Synthetic"),
        ("standalone_registry", "Fleet Parity"),
        ("workstream_registry", "Synthetic"),
        ("executive", "Synthetic"),
    ],
)
def test_chart_alt_real_registry_result_reaches_html(placement, title, tmp_path, repo_root, monkeypatch):
    from src.core.chart_renderer_registry import ChartRendererRegistry
    from src.core.config_loader import KustoQuerySettings, KustoSettings
    from src.core.kusto_rendering import build_kusto_sections
    import src.core.kusto_rendering as rendering

    pytest.importorskip("matplotlib")
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", True)
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section=title, render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line", "x_axis": "Day", "y_axes": ["Value"]},
    )
    rows = [{"Day": "one", "Value": 1.0}, {"Day": "two", "Value": 2.0}]
    observed = datetime(2026, 6, 18, 14, 30, tzinfo=timezone.utc)
    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), lambda _: rows,
        chart_registry=ChartRendererRegistry(), observed_at=observed,
    )
    assert warnings == ()
    assert observations[0].execution_state == "success"
    assert sections[0].message

    rendered = _render_chart_placement(placement, sections[0], tmp_path, repo_root)

    assert _chart_images(rendered)[0]["alt"] == f"{title} chart: {sections[0].message}"
    assert "Jun 18 2026, 14:30 UTC" in rendered


def _fleet_chart(placement, message):
    return replace(
        _chart_section(placement, title="Fleet Parity", message=message),
        section_id="kusto-fleet-chart",
        query_id="fleet-chart",
        rows=(),
        metrics=(),
        reference_url="https://example.com/chart",
    )


def _fleet_group_context(chart, edition=EditionType.DETAILED, mixed=True):
    context = _render_context()
    metric = replace(
        context.kusto_sections[0],
        section_id="kusto-fleet-metric",
        title="Fleet Parity",
        query_id="fleet-metric",
        rows=(),
        metrics=(KustoMetric(label="Fleet count", value="7"),),
        message="Fleet metric summary",
    )
    sections = (metric, chart) if mixed else (chart,)
    return replace(context, report=replace(context.report, edition=edition), kusto_sections=sections)


@pytest.mark.parametrize("edition", [EditionType.DETAILED, EditionType.FOCUSED, EditionType.LOOKBACK])
@pytest.mark.parametrize("placement", ["standalone_registry", "standalone_legacy"])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("marked_safe", [False, True])
def test_grouped_kusto_chart_renders_escaped_description(
    edition, placement, mixed, marked_safe, tmp_path, repo_root,
):
    from markupsafe import Markup

    message = 'Trend " onerror="unexpected & <script>sentinel</script>'
    if marked_safe:
        message = Markup(message)
    chart = _fleet_chart(placement, message)
    context = _fleet_group_context(chart, edition, mixed)
    payload = build_render_payload(context)
    groups = [section for section in payload["ordered_sections"] if section.kind == "kusto_group"]
    assert len(groups) == 1
    assert groups[0].anchor_id == "fleet-parity"
    assert groups[0].kusto_group_sections[-1] == chart

    renderer = HTMLRenderer("acme_weekly", reports_root=tmp_path, templates_root=repo_root / "templates")
    rendered = renderer.render(context)
    images = _chart_images(rendered)
    assert len(images) == 1
    assert images[0]["alt"] == f"Fleet Parity chart: {message}"
    assert "onerror" not in images[0]
    assert "<script>sentinel</script>" not in rendered
    assert "&lt;script&gt;sentinel&lt;/script&gt;" in rendered
    assert rendered.count('id="fleet-parity"') == 1
    assert "Query fleet-chart" in rendered
    if placement == "standalone_registry":
        assert '<a href="https://example.com/chart" target="_blank"' in rendered
    if mixed:
        assert "Fleet count" in rendered
        assert "Fleet metric summary" in rendered


@pytest.mark.parametrize("edition", [EditionType.DETAILED, EditionType.LOOKBACK])
@pytest.mark.parametrize("placement", ["standalone_registry", "standalone_legacy"])
@pytest.mark.parametrize("message", [None, ""])
def test_grouped_kusto_chart_empty_description_keeps_alt(edition, placement, message, tmp_path, repo_root):
    context = _fleet_group_context(_fleet_chart(placement, message), edition)
    rendered = HTMLRenderer("acme_weekly", reports_root=tmp_path, templates_root=repo_root / "templates").render(context)
    assert _chart_images(rendered)[0]["alt"] == "Fleet Parity chart"


@pytest.mark.parametrize("edition", [EditionType.DETAILED, EditionType.LOOKBACK])
def test_grouped_kusto_multiple_charts_keep_their_own_content(edition, tmp_path, repo_root):
    first = _fleet_chart("standalone_registry", "First trend")
    second = replace(
        _fleet_chart("standalone_legacy", "Second trend"),
        section_id="kusto-fleet-other-chart",
        query_id="fleet-other-chart",
        image_data_url="data:image/png;base64,c2Vjb25k",
        reference_url="https://example.com/other",
    )
    context = _fleet_group_context(first, edition)
    context = replace(context, kusto_sections=context.kusto_sections + (second,))
    html = HTMLRenderer("acme_weekly", reports_root=tmp_path, templates_root=repo_root / "templates").render(context)
    images = _chart_images(html)
    assert [image["alt"] for image in images] == [
        "Fleet Parity chart: First trend",
        "Fleet Parity chart: Second trend",
    ]
    assert [image["src"] for image in images] == [
        "data:image/png;base64,ZmFrZQ==",
        "data:image/png;base64,c2Vjb25k",
    ]
    teams = TeamsRenderer("acme_weekly", reports_root=tmp_path, templates_root=repo_root / "templates").render(context)
    assert "Fleet Parity: First trend. [View chart →](https://example.com/chart)" in teams
    assert "Fleet Parity: Second trend. [View chart →](https://example.com/other)" in teams


@pytest.mark.parametrize("edition", [EditionType.DETAILED, EditionType.LOOKBACK])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("has_image", [False, True])
def test_grouped_kusto_chart_preserves_registry_status(edition, cached, has_image, tmp_path, repo_root):
    chart = replace(
        _fleet_chart("standalone_registry", "Stable"),
        chart_png_base64="ZmFrZQ==" if has_image else None,
        captured_at=datetime(2026, 6, 18, 14, 30, tzinfo=timezone.utc),
        cache_captured_at=datetime(2026, 6, 17, 7, 45, tzinfo=timezone.utc) if cached else None,
        is_degraded=cached,
    )
    context = _fleet_group_context(chart, edition)
    rendered = HTMLRenderer("acme_weekly", reports_root=tmp_path, templates_root=repo_root / "templates").render(context)
    assert len(_chart_images(rendered)) == int(has_image)
    assert ("No data available for this period." in rendered) == (not has_image)
    if cached:
        assert "Jun 17 2026, 07:45 UTC" in rendered
        assert "live data unavailable" in rendered
    elif has_image:
        assert "Jun 18 2026, 14:30 UTC" in rendered
        assert "live data unavailable" not in rendered


@pytest.mark.parametrize("edition", [EditionType.DETAILED, EditionType.LOOKBACK])
def test_grouped_kusto_legacy_missing_image_preserves_message_and_reference(edition, tmp_path, repo_root):
    chart = replace(_fleet_chart("standalone_legacy", "Unavailable"), image_data_url=None)
    context = _fleet_group_context(chart, edition)
    rendered = HTMLRenderer("acme_weekly", reports_root=tmp_path, templates_root=repo_root / "templates").render(context)
    assert _chart_images(rendered) == []
    assert "Unavailable" in rendered
    assert "fleet-chart dashboard" in rendered
    assert "No data available for this period." not in rendered


@pytest.mark.parametrize("placement", ["standalone_registry", "standalone_legacy"])
@pytest.mark.parametrize("message", [None, "Trend stable"])
@pytest.mark.parametrize("reference_url", [None, "https://example.com/chart"])
@pytest.mark.parametrize("has_image", [False, True])
def test_grouped_kusto_chart_teams_summary(placement, message, reference_url, has_image, tmp_path, repo_root):
    chart = replace(
        _fleet_chart(placement, message),
        reference_url=reference_url,
        image_data_url="data:image/png;base64,ZmFrZQ==" if has_image else None,
        chart_png_base64="ZmFrZQ==" if has_image else None,
    )
    renderer = TeamsRenderer("acme_weekly", reports_root=tmp_path, templates_root=repo_root / "templates")
    rendered = renderer.render(_fleet_group_context(chart))
    if placement == "standalone_legacy" and not has_image:
        assert "[View chart" not in rendered
    else:
        summary = "Fleet Parity" + (f": {message}" if message else "")
        assert f"{summary}. [View chart →]({reference_url or '#'})" in rendered


@pytest.mark.parametrize("edition", [EditionType.DETAILED, EditionType.LOOKBACK])
@pytest.mark.parametrize("order_entry", ["kusto-group:fleet-parity", "kusto:all"])
def test_grouped_kusto_chart_retains_group_order_and_visibility(edition, order_entry, tmp_path, repo_root):
    chart = _fleet_chart("standalone_registry", "Group-only chart")
    contract = TemplateFamilyContract(
        name="synthetic", order=(order_entry,), mandatory=(), optional=(), rules={},
    )
    context = replace(_fleet_group_context(chart, edition), template_contract=contract)
    sections = build_render_payload(context)["ordered_sections"]
    assert [(section.kind, section.anchor_id) for section in sections] == [("kusto_group", "fleet-parity")]
    html = HTMLRenderer("acme_weekly", reports_root=tmp_path, templates_root=repo_root / "templates")
    teams = TeamsRenderer("acme_weekly", reports_root=tmp_path, templates_root=repo_root / "templates")
    assert len(_chart_images(html.render(context))) == 1
    assert "Group-only chart" in teams.render(context)

    hidden = replace(context, hidden_render_sections=frozenset({"fleet-parity"}))
    assert build_render_payload(hidden)["ordered_sections"] == ()
    assert _chart_images(html.render(hidden)) == []
    assert "Group-only chart" not in teams.render(hidden)


@pytest.mark.parametrize("metric_count", [3, 4, 6])
@pytest.mark.parametrize("chart_first", [False, True])
def test_grouped_kusto_three_or_more_metrics_and_chart_render(metric_count, chart_first, tmp_path, repo_root):
    context = _fleet_group_context(_fleet_chart("standalone_registry", "Stable"))
    metric = replace(
        context.kusto_sections[0],
        metrics=tuple(KustoMetric(label=f"Metric {index}", value=str(index)) for index in range(metric_count)),
    )
    chart = context.kusto_sections[1]
    context = replace(context, kusto_sections=(chart, metric) if chart_first else (metric, chart))
    rendered = HTMLRenderer("acme_weekly", reports_root=tmp_path, templates_root=repo_root / "templates").render(context)
    assert len(_chart_images(rendered)) == 1
    for index in range(metric_count):
        assert f"Metric {index}" in rendered


@pytest.mark.parametrize("marked_safe", [False, True])
def test_grouped_kusto_non_chart_message_is_literal(marked_safe, tmp_path, repo_root):
    from markupsafe import Markup

    context = _fleet_group_context(_fleet_chart("standalone_registry", None))
    message = "<script>sentinel</script> & metric"
    if marked_safe:
        message = Markup(message)
    metric = replace(context.kusto_sections[0], message=message)
    context = replace(context, kusto_sections=(metric,))
    rendered = HTMLRenderer("acme_weekly", reports_root=tmp_path, templates_root=repo_root / "templates").render(context)
    assert "&lt;script&gt;sentinel&lt;/script&gt; &amp; metric" in rendered
    assert "<script>sentinel</script>" not in rendered

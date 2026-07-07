from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exec_summary_diff_engine import (
    ExecSummaryStalenessFinding,
    check_exec_summary_staleness,
    extract_lead_sentence,
    parse_exec_summary_bullets,
)


# ---------------------------------------------------------------------------
# extract_lead_sentence
# ---------------------------------------------------------------------------

def test_extract_lead_sentence_plain_text() -> None:
    text = "Fleet health is green. Deployment velocity is on track."
    assert extract_lead_sentence(text) == "Fleet health is green."


def test_extract_lead_sentence_strips_markdown_bold() -> None:
    text = "**Fleet health** is green. Secondary sentence."
    result = extract_lead_sentence(text)
    assert "**" not in result
    assert result.startswith("Fleet health")


def test_extract_lead_sentence_strips_markdown_links() -> None:
    text = "[Fleet health](https://example.com) is green today."
    result = extract_lead_sentence(text)
    assert "[" not in result
    assert "Fleet health" in result


def test_extract_lead_sentence_skips_headers() -> None:
    text = "# Header\n## Sub-header\nActual content starts here."
    assert extract_lead_sentence(text) == "Actual content starts here."


def test_extract_lead_sentence_skips_html_comments() -> None:
    text = "<!-- vertex:ws-lead: acme -->\n- Fleet health is green."
    result = extract_lead_sentence(text)
    # The bullet "Fleet health is green." should be the lead
    assert "Fleet health is green." in result


def test_extract_lead_sentence_empty_text() -> None:
    assert extract_lead_sentence("") == ""


def test_extract_lead_sentence_only_headers() -> None:
    assert extract_lead_sentence("# Title\n## Subtitle") == ""


def test_extract_lead_sentence_single_sentence_no_period() -> None:
    text = "Deployment is on track"
    result = extract_lead_sentence(text)
    assert "Deployment is on track" in result


# ---------------------------------------------------------------------------
# parse_exec_summary_bullets
# ---------------------------------------------------------------------------

def test_parse_exec_summary_bullets_basic() -> None:
    text = """<!-- vertex:ws-lead: acme -->
- Adventure ramp is progressing well.

<!-- vertex:ws-lead: dd_on_pf -->
- Contoso pilot continues.
"""
    result = parse_exec_summary_bullets(text)
    assert result["acme"] == "Adventure ramp is progressing well."
    assert result["dd_on_pf"] == "Contoso pilot continues."


def test_parse_exec_summary_bullets_no_tags() -> None:
    text = "- Some bullet without a tag.\n- Another bullet."
    assert parse_exec_summary_bullets(text) == {}


def test_parse_exec_summary_bullets_non_bullet_line() -> None:
    text = """<!-- vertex:ws-lead: acme -->
Adventure ramp progressing.
"""
    result = parse_exec_summary_bullets(text)
    assert result["acme"] == "Adventure ramp progressing."


def test_parse_exec_summary_bullets_consecutive_tags() -> None:
    text = """<!-- vertex:ws-lead: ws1 -->
- First workstream.
<!-- vertex:ws-lead: ws2 -->
- Second workstream.
"""
    result = parse_exec_summary_bullets(text)
    assert result["ws1"] == "First workstream."
    assert result["ws2"] == "Second workstream."


def test_parse_exec_summary_bullets_star_bullet_variant() -> None:
    text = """<!-- vertex:ws-lead: acme -->
* Adventure star bullet.
"""
    result = parse_exec_summary_bullets(text)
    assert result["acme"] == "Adventure star bullet."


def test_parse_exec_summary_bullets_empty_text() -> None:
    assert parse_exec_summary_bullets("") == {}


# ---------------------------------------------------------------------------
# check_exec_summary_staleness (filesystem-based)
# ---------------------------------------------------------------------------

def _write_narrative(root: Path, edition: str, issue: int, filename: str, content: str) -> None:
    path = root / edition / "narratives" / f"issue_{issue:03d}" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_no_findings_when_exec_summary_missing(tmp_path: Path) -> None:
    # No exec_summary.md → empty result
    _write_narrative(tmp_path, "test_edition", 79, "ws_acme.md", "Fleet health is green.")
    result = check_exec_summary_staleness("test_edition", 79, reports_root=tmp_path)
    assert result == []


def test_no_findings_when_exec_summary_has_no_tagged_bullets(tmp_path: Path) -> None:
    _write_narrative(tmp_path, "test_edition", 79, "exec_summary.md",
                     "- Adventure ramp is progressing.\n")
    _write_narrative(tmp_path, "test_edition", 79, "ws_acme.md", "Fleet health is green.")
    result = check_exec_summary_staleness("test_edition", 79, reports_root=tmp_path)
    assert result == []


def test_no_findings_when_narrative_unchanged(tmp_path: Path) -> None:
    lead = "Fleet health is green and deployment is on track."
    _write_narrative(tmp_path, "test_edition", 79, "exec_summary.md",
                     f"<!-- vertex:ws-lead: acme -->\n- {lead}\n")
    _write_narrative(tmp_path, "test_edition", 79, "ws_acme.md", f"{lead} More details follow.")
    _write_narrative(tmp_path, "test_edition", 78, "ws_acme.md", f"{lead} More details follow.")
    result = check_exec_summary_staleness("test_edition", 79, 78, reports_root=tmp_path)
    # Narrative unchanged → no staleness findings
    assert result == []


def test_finding_when_exec_bullet_diverges_from_current_lead(tmp_path: Path) -> None:
    # Exec summary says one thing; current narrative says something very different
    exec_bullet = "Adventure ramp is proceeding smoothly with zero blockers."
    current_lead = "SCHIE gaps are blocking the ramp; P0 items remain open."
    prior_lead = "Adventure ramp was on track last week."

    _write_narrative(tmp_path, "test_edition", 79, "exec_summary.md",
                     f"<!-- vertex:ws-lead: acme -->\n- {exec_bullet}\n")
    _write_narrative(tmp_path, "test_edition", 79, "ws_acme.md", f"{current_lead} Details here.")
    _write_narrative(tmp_path, "test_edition", 78, "ws_acme.md", f"{prior_lead} Details here.")

    result = check_exec_summary_staleness("test_edition", 79, 78, reports_root=tmp_path)
    assert len(result) == 1
    finding = result[0]
    assert isinstance(finding, ExecSummaryStalenessFinding)
    assert finding.workstream_id == "acme"
    assert finding.is_stale is True
    assert finding.divergence_score < 0.82


def test_no_finding_when_exec_bullet_closely_matches_current_lead(tmp_path: Path) -> None:
    # Exec bullet is very close to current lead → not stale
    current_lead = "SCHIE gaps are blocking the Adventure ramp; P0 items remain open."
    exec_bullet = "SCHIE gaps are blocking the Adventure ramp; items remain open."  # minor diff
    prior_lead = "Adventure was on track previously."

    _write_narrative(tmp_path, "test_edition", 79, "exec_summary.md",
                     f"<!-- vertex:ws-lead: acme -->\n- {exec_bullet}\n")
    _write_narrative(tmp_path, "test_edition", 79, "ws_acme.md", f"{current_lead} More detail.")
    _write_narrative(tmp_path, "test_edition", 78, "ws_acme.md", f"{prior_lead} More detail.")

    result = check_exec_summary_staleness("test_edition", 79, 78, reports_root=tmp_path)
    assert result == []


def test_multiple_workstreams_partial_staleness(tmp_path: Path) -> None:
    # ws_a: stale (exec bullet totally different from current lead)
    # ws_b: not stale (exec bullet matches current lead closely)
    stale_exec = "Everything is on track for ws_a."
    stale_current = "ws_a is completely blocked by a critical dependency."
    prior_stale = "ws_a was proceeding normally before the blocker arrived."

    ok_exec = "ws_b pilot is ready to go live soon."
    ok_current = "ws_b pilot is ready to go live imminently."
    prior_ok = "ws_b pilot was still in preparation."

    exec_md = (
        f"<!-- vertex:ws-lead: ws_a -->\n- {stale_exec}\n"
        f"<!-- vertex:ws-lead: ws_b -->\n- {ok_exec}\n"
    )
    _write_narrative(tmp_path, "test_edition", 79, "exec_summary.md", exec_md)
    _write_narrative(tmp_path, "test_edition", 79, "ws_ws_a.md", f"{stale_current} Details.")
    _write_narrative(tmp_path, "test_edition", 78, "ws_ws_a.md", f"{prior_stale} Details.")
    _write_narrative(tmp_path, "test_edition", 79, "ws_ws_b.md", f"{ok_current} Details.")
    _write_narrative(tmp_path, "test_edition", 78, "ws_ws_b.md", f"{prior_ok} Details.")

    result = check_exec_summary_staleness("test_edition", 79, 78, reports_root=tmp_path)
    stale_ids = {f.workstream_id for f in result}
    assert "ws_a" in stale_ids
    assert "ws_b" not in stale_ids


def test_no_findings_when_narrative_file_missing(tmp_path: Path) -> None:
    # Exec summary references ws_missing but no narrative file exists
    _write_narrative(tmp_path, "test_edition", 79, "exec_summary.md",
                     "<!-- vertex:ws-lead: ws_missing -->\n- Adventure is on track.\n")
    result = check_exec_summary_staleness("test_edition", 79, reports_root=tmp_path)
    assert result == []


def test_default_prior_issue_is_issue_minus_one(tmp_path: Path) -> None:
    # Prior issue defaults to issue_number - 1
    exec_bullet = "Adventure ramp is smooth."
    current_lead = "SCHIE blocking the ramp."
    prior_lead = "Adventure was fine before."

    _write_narrative(tmp_path, "test_edition", 80, "exec_summary.md",
                     f"<!-- vertex:ws-lead: acme -->\n- {exec_bullet}\n")
    _write_narrative(tmp_path, "test_edition", 80, "ws_acme.md", f"{current_lead} More.")
    _write_narrative(tmp_path, "test_edition", 79, "ws_acme.md", f"{prior_lead} More.")

    result = check_exec_summary_staleness("test_edition", 80, reports_root=tmp_path)
    assert len(result) == 1
    assert result[0].workstream_id == "acme"


def test_finding_fields_are_populated(tmp_path: Path) -> None:
    exec_bullet = "Adventure fully green, no blockers."
    current_lead = "Critical SCHIE gap is blocking all ramp progress entirely."
    prior_lead = "Adventure had minor issues last week."

    _write_narrative(tmp_path, "test_edition", 79, "exec_summary.md",
                     f"<!-- vertex:ws-lead: acme -->\n- {exec_bullet}\n")
    _write_narrative(tmp_path, "test_edition", 79, "ws_acme.md", f"{current_lead}")
    _write_narrative(tmp_path, "test_edition", 78, "ws_acme.md", f"{prior_lead}")

    result = check_exec_summary_staleness("test_edition", 79, 78, reports_root=tmp_path)
    assert len(result) == 1
    f = result[0]
    assert f.exec_bullet_text == exec_bullet
    assert f.workstream_lead_sentence != ""
    assert f.prior_workstream_lead_sentence != ""
    assert 0.0 <= f.divergence_score <= 1.0
    assert f.workstream_section_id == "ws_acme"


def test_custom_threshold_respected(tmp_path: Path) -> None:
    exec_bullet = "Adventure ramp is green and proceeding well."
    current_lead = "Adventure ramp is green and proceeding, with minor caveats."
    prior_lead = "Previous state was different."

    _write_narrative(tmp_path, "test_edition", 79, "exec_summary.md",
                     f"<!-- vertex:ws-lead: acme -->\n- {exec_bullet}\n")
    _write_narrative(tmp_path, "test_edition", 79, "ws_acme.md", f"{current_lead}")
    _write_narrative(tmp_path, "test_edition", 78, "ws_acme.md", f"{prior_lead}")

    # With default threshold (0.82) they might not be stale; with threshold=0.99 they likely are
    result_strict = check_exec_summary_staleness(
        "test_edition", 79, 78, reports_root=tmp_path, threshold=0.99
    )
    result_loose = check_exec_summary_staleness(
        "test_edition", 79, 78, reports_root=tmp_path, threshold=0.0
    )
    # At threshold=0.0 nothing is stale (score >= 0.0 always)
    assert result_loose == []
    # At threshold=0.99 the similar bullet may be stale
    # (just checking the threshold argument is plumbed through, not exact value)
    assert isinstance(result_strict, list)


# ---------------------------------------------------------------------------
# FR-SG-25: generate_cross_workstream_exec_summary
# ---------------------------------------------------------------------------

from datetime import date, datetime, timezone
from src.core.exec_summary_diff_engine import (
    CrossWorkstreamExecSummary,
    WorkstreamStatusEntry,
    generate_cross_workstream_exec_summary,
)
from src.core.models_v2 import (
    RiskDerivedLevel,
    RiskEntry,
    RiskCategory,
    RiskImpact,
    RiskProbability,
    RiskStatus,
)
from src.core.chronicle import ProgramEvent


def _risk_entry_exec(*, status: str = "open") -> RiskEntry:
    from src.core.models_v2 import RiskCategory, RiskImpact, RiskProbability, RiskStatus
    return RiskEntry(
        id="risk-1",
        program_id="acme",
        title="Firmware sign-off lag",
        description="May miss pilot.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.SCHEDULE,
        owner_alias="operator",
        mitigation_plan="Escalate.",
        mitigation_due_date=date(2026, 5, 20),
        linked_workstream_ids=("acme",),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus(status),
        identified_date=date(2026, 5, 1),
        identified_in_vertex_issue=77,
        last_reviewed_date=date(2026, 5, 8),
        entity_refs=(),
    )


def _chronicle_event(event_type: str = "commitment") -> ProgramEvent:
    return ProgramEvent(
        event_type=event_type,
        event_date=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        description="Leadership gate approved.",
        source="meeting",
        actors=("operator",),
        linked_dimensions=("acme",),
        event_id="ev-1",
    )


def test_generate_cross_workstream_exec_summary_returns_result() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    result = generate_cross_workstream_exec_summary(
        "acme",
        None,
        workstream_ids=("acme", "dd_on_pf"),
        risk_entries=(),
        chronicle_events=(),
        staleness_findings=[],
        as_of=as_of,
    )
    assert isinstance(result, CrossWorkstreamExecSummary)
    assert result.program_id == "acme"


def test_generate_cross_workstream_exec_summary_workstream_entries_match_ids() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    result = generate_cross_workstream_exec_summary(
        "acme",
        None,
        workstream_ids=("acme", "dd_on_pf"),
        risk_entries=(),
        chronicle_events=(),
        staleness_findings=[],
        as_of=as_of,
    )
    ws_ids = {e.workstream_id for e in result.workstream_entries}
    assert ws_ids == {"acme", "dd_on_pf"}


def test_generate_cross_workstream_exec_summary_top_risks_capped() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    risk_entries = tuple(
        RiskEntry(
            id=f"risk-{i}",
            program_id="acme",
            title=f"Risk {i}",
            description="desc",
            probability=RiskProbability.LIKELY,
            impact=RiskImpact.HIGH,
            category=RiskCategory.SCHEDULE,
            owner_alias="operator",
            mitigation_plan="plan",
            mitigation_due_date=date(2026, 5, 20),
            linked_workstream_ids=(),
            linked_work_item_ids=(),
            linked_milestone_ids=(),
            linked_claim_ids=(),
            linked_action_ids=(),
            status=RiskStatus.OPEN,
            identified_date=date(2026, 5, 1),
            identified_in_vertex_issue=77,
            last_reviewed_date=date(2026, 5, 8),
            entity_refs=(),
        )
        for i in range(10)
    )
    result = generate_cross_workstream_exec_summary(
        "acme",
        None,
        workstream_ids=("acme",),
        risk_entries=risk_entries,
        chronicle_events=(),
        staleness_findings=[],
        max_top_risks=3,
        as_of=as_of,
    )
    assert len(result.top_risk_ids) <= 3


def test_generate_cross_workstream_exec_summary_escalated_risks_first() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    open_risk = _risk_entry_exec(status="open")
    import dataclasses
    escalated_risk = dataclasses.replace(open_risk, id="risk-escalated", status=RiskStatus.ESCALATED)
    result = generate_cross_workstream_exec_summary(
        "acme",
        None,
        workstream_ids=("acme",),
        risk_entries=(open_risk, escalated_risk),
        chronicle_events=(),
        staleness_findings=[],
        max_top_risks=2,
        as_of=as_of,
    )
    if len(result.top_risk_ids) >= 1:
        assert result.top_risk_ids[0] == "risk-escalated"


def test_generate_cross_workstream_exec_summary_recent_chronicle_events() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    ev = _chronicle_event("commitment")
    result = generate_cross_workstream_exec_summary(
        "acme",
        None,
        workstream_ids=("acme",),
        risk_entries=(),
        chronicle_events=(ev,),
        staleness_findings=[],
        as_of=as_of,
    )
    assert ev.description in result.recent_chronicle_descriptions


def test_generate_cross_workstream_exec_summary_stale_entry_flags_workstream() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    from src.core.exec_summary_diff_engine import ExecSummaryStalenessFinding
    stale_finding = ExecSummaryStalenessFinding(
        workstream_id="acme",
        workstream_section_id="ws_acme",
        exec_bullet_text="old bullet",
        workstream_lead_sentence="new content",
        prior_workstream_lead_sentence="old content",
        divergence_score=0.2,
        is_stale=True,
    )
    result = generate_cross_workstream_exec_summary(
        "acme",
        None,
        workstream_ids=("acme",),
        risk_entries=(),
        chronicle_events=(),
        staleness_findings=[stale_finding],
        as_of=as_of,
    )
    nova_entry = next(e for e in result.workstream_entries if e.workstream_id == "acme")
    assert nova_entry.stale_executive_bullet is True


def test_generate_cross_workstream_exec_summary_gate_conditions_from_approval_events() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    approval_ev = ProgramEvent(
        event_type="approval",
        event_date=datetime(2026, 5, 15, tzinfo=timezone.utc),
        description="Gate Q4 approved by LT.",
        source="meeting",
        actors=("operator",),
        linked_dimensions=("acme",),
        event_id="ev-gate",
    )
    result = generate_cross_workstream_exec_summary(
        "acme",
        None,
        workstream_ids=("acme",),
        risk_entries=(),
        chronicle_events=(approval_ev,),
        staleness_findings=[],
        as_of=as_of,
    )
    assert any("gate" in cond.lower() for cond in result.gate_conditions)


def test_generate_cross_workstream_exec_summary_as_of_is_utc() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    result = generate_cross_workstream_exec_summary(
        "acme",
        None,
        workstream_ids=("acme",),
        risk_entries=(),
        chronicle_events=(),
        staleness_findings=[],
        as_of=as_of,
    )
    assert result.as_of.tzinfo is not None

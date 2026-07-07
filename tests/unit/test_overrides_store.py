from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.core.models import ConfirmedDimension, RiskLevel
from src.core.overrides_store import DimensionOverride, OverridesDocument, ScorecardOverrides, Top3NowEntry
from src.core.overrides_store import archive_overrides, load_overrides, merge_overrides, reset_overrides_for_next_issue
from src.core.overrides_store import save_overrides
from src.core.exceptions import ConfigError


EDITION_NAME = "acme_weekly"


def test_merge_overrides_preserves_adds_and_marks_removed(tmp_path: Path) -> None:
    existing = OverridesDocument(
        issue_number=77,
        top_3_now=(
            Top3NowEntry(
                type="decision",
                text="Need LT call.",
                owner="Priya Mehta",
                ado_link="https://dev.azure.com/your-org/One/_workitems/edit/123",
                anchor="schie-gaps",
            ),
        ),
        scorecards=(
            ScorecardOverrides(
                name="Acme Adventure/XIO 100% Ramp Readiness",
                dimensions=(
                    DimensionOverride(
                        name="Deployment Velocity",
                        risk=RiskLevel.LOW,
                        label="Velocity",
                        note="carry",
                        eta=date(2026, 5, 15),
                        hide_details=True,
                    ),
                    DimensionOverride(name="Removed Dimension", risk=RiskLevel.HIGH),
                ),
            ),
        ),
    )

    merged, stats = merge_overrides(
        issue_number=78,
        expected_scorecards={
            "Acme Adventure/XIO 100% Ramp Readiness": (
                "Deployment Velocity",
                "Deployment Safety",
            ),
        },
        existing=existing,
    )

    reports_root = tmp_path / "reports"
    saved_path = save_overrides(EDITION_NAME, merged, reports_root)
    loaded = load_overrides(EDITION_NAME, reports_root)

    assert stats.preserved_count == 1
    assert stats.added_count == 1
    assert stats.removed_count == 1
    assert merged.issue_number == 78
    assert merged.top_3_now == existing.top_3_now
    assert merged.scorecards[0].dimensions[0].risk == RiskLevel.LOW
    assert merged.scorecards[0].dimensions[0].label == "Velocity"
    assert merged.scorecards[0].dimensions[0].eta == date(2026, 5, 15)
    assert merged.scorecards[0].dimensions[0].hide_details is True
    assert merged.scorecards[0].dimensions[1].risk is None
    assert merged.removed_dimensions[0].dimension_name == "Removed Dimension"
    assert loaded is not None
    assert loaded.scorecards[0].dimensions[0].label == "Velocity"
    assert loaded.scorecards[0].dimensions[0].eta == date(2026, 5, 15)
    assert loaded.scorecards[0].dimensions[0].hide_details is True
    assert loaded.scorecards[0].dimensions[1].risk is None
    content = saved_path.read_text(encoding="utf-8")
    assert "label: Velocity" in content
    assert "eta:" in content
    assert "2026-05-15" in content
    assert "hide_details: true" in content
    assert "❓ Needs input" in content
    assert "# Active Vertex overrides.yaml" in content
    assert "# REMOVED — dimension no longer in config" in content


def test_reset_and_archive_overrides_seed_blank_risks_for_next_issue(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    archive_root = tmp_path / "archive"
    document = reset_overrides_for_next_issue(
        edition=EDITION_NAME,
        next_issue_number=79,
        confirmed_dimensions=(
            ConfirmedDimension(
                scorecard_name="Acme Readiness",
                name="Deployment Velocity",
                risk=RiskLevel.MEDIUM,
                prior_risk=RiskLevel.LOW,
                item_count=3,
                ado_query_url="https://dev.azure.com/your-org/One/_queries/1",
            ),
        ),
        reports_root=reports_root,
    )

    archive_path = archive_overrides(
        edition=EDITION_NAME,
        issue_number=79,
        document=document,
        archive_root=archive_root,
    )

    assert document.issue_number == 79
    assert document.top_3_now == ()
    assert document.scorecards[0].dimensions[0].risk is None
    assert archive_path == archive_root / EDITION_NAME / "overrides" / "issue_079.yaml"
    assert "❓ Needs input" in archive_path.read_text(encoding="utf-8")


def test_governance_and_decisions_roundtrip(tmp_path: Path) -> None:
    from src.core.overrides_store import GovernanceState, DecisionRecord
    from src.core.decision_register import read_governance_decisions_from_overrides
    from src.core.models_v2 import DecisionStatus
    
    gov = GovernanceState(
        dfd_date=date(2026, 6, 3),
        dfd_history=(date(2026, 3, 31), date(2026, 6, 3)),
        escalation_active=True,
        escalation_workstreams=("acme-adventure-xio-100-ramp-readiness-bios",),
        lt_commitment="PF LT committed to deliver P0 items",
        lt_commitment_date=date(2026, 5, 22),
    )
    
    dec = DecisionRecord(
        id="contoso-023-prod-gate",
        workstream="contoso-pilot-readiness",
        type="gate",
        statement="PF LT committed to UD upgrade-alert intervention.",
        source_type="meeting",
        source_ref="series-1234",
        owner="Chris Chen",
        status="active",
        effective_date=date(2026, 5, 29),
        resolved_date=date(2026, 6, 3),
    )
    
    document = OverridesDocument(
        issue_number=80,
        top_3_now=(),
        scorecards=(),
        governance=gov,
        decisions=(dec,),
    )
    
    reports_root = tmp_path / "reports"
    saved_path = save_overrides(EDITION_NAME, document, reports_root)
    loaded = load_overrides(EDITION_NAME, reports_root)
    
    assert loaded is not None
    assert loaded.issue_number == 80
    assert loaded.governance.dfd_date == date(2026, 6, 3)
    assert loaded.governance.dfd_history == (date(2026, 3, 31), date(2026, 6, 3))
    assert loaded.governance.escalation_active is True
    assert loaded.governance.escalation_workstreams == ("acme-adventure-xio-100-ramp-readiness-bios",)
    assert loaded.governance.lt_commitment == "PF LT committed to deliver P0 items"
    assert loaded.governance.lt_commitment_date == date(2026, 5, 22)
    
    assert len(loaded.decisions) == 1
    assert loaded.decisions[0].id == "contoso-023-prod-gate"
    assert loaded.decisions[0].workstream == "contoso-pilot-readiness"
    assert loaded.decisions[0].type == "gate"
    assert loaded.decisions[0].status == "active"
    
    # Test adapter
    dec_entries = read_governance_decisions_from_overrides(loaded, program_id="demo")
    assert len(dec_entries) == 1
    assert dec_entries[0].id == "contoso-023-prod-gate"
    assert dec_entries[0].program_id == "demo"
    assert dec_entries[0].title == "[GATE] Decision contoso-023-prod-gate"
    assert dec_entries[0].status == DecisionStatus.DECIDED
    assert dec_entries[0].decision == "PF LT committed to UD upgrade-alert intervention."
    assert dec_entries[0].decided_by == "Chris Chen"
    assert dec_entries[0].decision_date == date(2026, 5, 29)
    assert dec_entries[0].review_by == date(2026, 6, 3)
    
    content = saved_path.read_text(encoding="utf-8")
    assert "governance:" in content
    assert "dfd_date: '2026-06-03'" in content or "dfd_date: 2026-06-03" in content
    assert "escalation_active: true" in content
    assert "decisions:" in content
    assert "id: contoso-023-prod-gate" in content


# ---------------------------------------------------------------------------
# FR-SG-18: DimensionOverride provenance fields roundtrip
# ---------------------------------------------------------------------------

def test_dimension_override_provenance_fields_roundtrip(tmp_path: Path) -> None:
    """FR-SG-18: owner, reason, review_date, expiry_date survive save/load."""
    document = OverridesDocument(
        issue_number=81,
        top_3_now=(),
        scorecards=(
            ScorecardOverrides(
                name="Acme Readiness",
                dimensions=(
                    DimensionOverride(
                        name="Deployment Velocity",
                        risk=RiskLevel.MEDIUM,
                        owner="jsmith",
                        reason="Waiting for firmware sign-off",
                        review_date=date(2026, 5, 20),
                        expiry_date=date(2026, 6, 1),
                    ),
                ),
            ),
        ),
    )

    reports_root = tmp_path / "reports"
    saved_path = save_overrides(EDITION_NAME, document, reports_root)
    loaded = load_overrides(EDITION_NAME, reports_root)

    assert loaded is not None
    dim = loaded.scorecards[0].dimensions[0]
    assert dim.owner == "jsmith"
    assert dim.reason == "Waiting for firmware sign-off"
    assert dim.review_date == date(2026, 5, 20)
    assert dim.expiry_date == date(2026, 6, 1)

    content = saved_path.read_text(encoding="utf-8")
    assert "owner: jsmith" in content
    assert "reason:" in content
    assert "review_date:" in content
    assert "expiry_date:" in content


def test_dimension_override_provenance_fields_optional(tmp_path: Path) -> None:
    """FR-SG-18: provenance fields are optional — existing overrides without them still load."""
    document = OverridesDocument(
        issue_number=82,
        top_3_now=(),
        scorecards=(
            ScorecardOverrides(
                name="Acme Readiness",
                dimensions=(
                    DimensionOverride(
                        name="Deployment Velocity",
                        risk=RiskLevel.LOW,
                    ),
                ),
            ),
        ),
    )

    reports_root = tmp_path / "reports"
    save_overrides(EDITION_NAME, document, reports_root)
    loaded = load_overrides(EDITION_NAME, reports_root)

    assert loaded is not None
    dim = loaded.scorecards[0].dimensions[0]
    assert dim.owner is None
    assert dim.reason is None
    assert dim.review_date is None
    assert dim.expiry_date is None


def test_dimension_override_provenance_null_dates_roundtrip(tmp_path: Path) -> None:
    """FR-SG-18: review_date and expiry_date absent in YAML → None after load."""
    document = OverridesDocument(
        issue_number=83,
        top_3_now=(),
        scorecards=(
            ScorecardOverrides(
                name="Acme Readiness",
                dimensions=(
                    DimensionOverride(
                        name="Deployment Velocity",
                        risk=RiskLevel.HIGH,
                        owner="operator",
                        reason="Escalation active",
                    ),
                ),
            ),
        ),
    )

    reports_root = tmp_path / "reports"
    save_overrides(EDITION_NAME, document, reports_root)
    loaded = load_overrides(EDITION_NAME, reports_root)

    assert loaded is not None
    dim = loaded.scorecards[0].dimensions[0]
    assert dim.owner == "operator"
    assert dim.reason == "Escalation active"
    assert dim.review_date is None
    assert dim.expiry_date is None


def test_load_overrides_rejects_numeric_string_issue_number(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    path = reports_root / EDITION_NAME / "overrides.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("issue_number: '1'\nscorecards: {}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="issue_number must be an integer"):
        load_overrides(EDITION_NAME, reports_root)


def test_load_overrides_rejects_non_string_focused_include_entry(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    path = reports_root / EDITION_NAME / "overrides.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("issue_number: 1\nfocused_include:\n  - 1\nscorecards: {}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="focused_include entries must be strings"):
        load_overrides(EDITION_NAME, reports_root)


def test_load_overrides_rejects_non_string_escalation_workstream(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    path = reports_root / EDITION_NAME / "overrides.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "issue_number: 1\ngovernance:\n  escalation_workstreams:\n    - 1\nscorecards: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="governance.escalation_workstreams entries must be strings"):
        load_overrides(EDITION_NAME, reports_root)


def test_load_overrides_rejects_non_string_top_3_owner(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    path = reports_root / EDITION_NAME / "overrides.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "issue_number: 1\ntop_3_now:\n  - type: decision\n    text: Need LT call\n    owner: 1\nscorecards: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="top_3_now.owner must be a string"):
        load_overrides(EDITION_NAME, reports_root)

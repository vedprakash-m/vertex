from __future__ import annotations

from datetime import datetime, timezone

from src.core.ado_signal_extractor import ADOSignalExtractor
from src.core.integration_types import ADOHydrationOutput
from src.core.models import Comment, Revision, RiskLevel, WorkItem


def _work_item() -> WorkItem:
    return WorkItem(
        id=101,
        type="Feature",
        title="Deployment",
        state="Active",
        assigned_to="Owner",
        assigned_to_email="owner@example.com",
        area_path="One\\Demo",
        iteration_path="One\\Iteration",
        target_date=None,
        risk_level=RiskLevel.UNKNOWN,
        tags=["RAMPP1"],
        custom_fields={"workstream_ids": ("ws-a", "ws-b")},
        revisions=[
            Revision(
                work_item_id=101,
                rev_number=2,
                changed_by="Owner",
                changed_by_email="owner@example.com",
                changed_date=datetime(2026, 5, 24, 10, tzinfo=timezone.utc),
                fields_changed={"System.State": ("Active", "Closed")},
            )
        ],
        comments=[
            Comment(
                work_item_id=101,
                comment_id=7,
                created_by="Owner",
                created_by_email="owner@example.com",
                created_date=datetime(2026, 5, 24, 11, tzinfo=timezone.utc),
                text="Ready for review",
            )
        ],
        fetched_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
    )


def test_ado_signal_extractor_fans_out_per_workstream() -> None:
    result = ADOSignalExtractor().extract(ADOHydrationOutput(work_items=(_work_item(),), freshness_items=()), "demo")

    assert [signal.id for signal in result.signals] == [
        "ado/revision/101/2/state/ws-a",
        "ado/revision/101/2/state/ws-b",
        "ado/comment/101/7/ws-a",
        "ado/comment/101/7/ws-b",
    ]
    assert {signal.workstream_id for signal in result.signals} == {"ws-a", "ws-b"}
    assert {signal.entity_refs for signal in result.signals} == {
        ("ado:101", "WI:101", "WS:ws-a"),
        ("ado:101", "WI:101", "WS:ws-b"),
    }
    assert result.trajectory_points[0].state == "Active"


def test_ado_signal_extractor_emits_freshness_signals_for_unowned_items() -> None:
    """Unowned items in freshness_items produce 'ado/freshness' signals per workstream."""
    unowned = WorkItem(
        id=202,
        type="Task",
        title="Unowned task",
        state="Active",
        assigned_to=None,  # no owner → eligible for freshness signal
        assigned_to_email=None,
        area_path="One\\Demo",
        iteration_path="One\\Iteration",
        target_date=None,
        risk_level=RiskLevel.UNKNOWN,
        tags=[],
        custom_fields={"workstream_ids": ("ws-c",)},
        fetched_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
    )
    result = ADOSignalExtractor().extract(
        ADOHydrationOutput(work_items=(), freshness_items=(unowned,)),
        "demo",
    )

    freshness_signals = [s for s in result.signals if "/freshness/" in s.id]
    assert len(freshness_signals) == 1
    assert freshness_signals[0].workstream_id == "ws-c"
    assert "ado:202" in freshness_signals[0].entity_refs
    assert "WS:ws-c" in freshness_signals[0].entity_refs
    assert freshness_signals[0].metadata["finding_type"] == "unowned"


def test_ado_signal_extractor_3_workstream_fanout() -> None:
    """3 workstreams → 3 signals per event (revision + comment fan-out to all 3)."""
    item = WorkItem(
        id=303,
        type="Feature",
        title="3-ws feature",
        state="Active",
        assigned_to="Owner",
        assigned_to_email="owner@example.com",
        area_path="One\\Demo",
        iteration_path="One\\Iteration",
        target_date=None,
        risk_level=RiskLevel.UNKNOWN,
        tags=[],
        custom_fields={"workstream_ids": ("ws-x", "ws-y", "ws-z")},
        revisions=[
            Revision(
                work_item_id=303,
                rev_number=1,
                changed_by="Owner",
                changed_by_email="owner@example.com",
                changed_date=datetime(2026, 5, 24, 10, tzinfo=timezone.utc),
                fields_changed={"System.State": ("Active", "Closed")},
            )
        ],
        comments=[],
        fetched_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
    )
    result = ADOSignalExtractor().extract(ADOHydrationOutput(work_items=(item,), freshness_items=()), "demo")

    # 3 workstreams × 1 revision event = 3 revision signals
    revision_signals = [s for s in result.signals if "/revision/" in s.id]
    assert len(revision_signals) == 3
    assert {s.workstream_id for s in revision_signals} == {"ws-x", "ws-y", "ws-z"}
    # IDs are deterministic and workstream-suffixed
    assert {s.id for s in revision_signals} == {
        "ado/revision/303/1/state/ws-x",
        "ado/revision/303/1/state/ws-y",
        "ado/revision/303/1/state/ws-z",
    }


def test_ado_signal_entity_refs_include_both_formats() -> None:
    """UIL ADO signals must emit both ado:{id} and WI:{id} for backward compat.

    Downstream consumers (ado_reconcile, action_extractor_basic, altitude_guard)
    filter for WI: prefix. They must find it even when the UIL path is active.
    """
    result = ADOSignalExtractor().extract(ADOHydrationOutput(work_items=(_work_item(),), freshness_items=()), "demo")

    for signal in result.signals:
        refs = signal.entity_refs
        # Both formats present
        assert f"ado:{_work_item().id}" in refs, f"ado: ref missing in {refs}"
        assert f"WI:{_work_item().id}" in refs, f"WI: ref missing in {refs}"
        assert f"WS:{signal.workstream_id}" in refs, f"WS: ref missing in {refs}"
        # WI: format is readable by legacy consumers
        wi_ids = {int(r.split(":", 1)[1]) for r in refs if r.upper().startswith("WI:")}
        assert _work_item().id in wi_ids


def test_ado_signal_extractor_extracts_pr_signals() -> None:
    from src.core.ado_pr_client import PullRequestSummary
    active_pr = PullRequestSummary(
        pr_id=123,
        title="Fix BIOS Gen8 for WI:12345",
        status="active",
        created_by="Alice Testowner",
        target_ref="refs/heads/main",
        source_ref="refs/heads/feature/gen8",
        url="https://weburl/123",
        created_at=datetime(2026, 5, 24, 10, tzinfo=timezone.utc),
        merged_at=None,
        repository_id="repo-adventure",
        workstream_ids=("ws-x",)
    )
    merged_pr = PullRequestSummary(
        pr_id=124,
        title="Implement Wingtip speedup for bug 23456",
        status="completed",
        created_by="Bob Testdev",
        target_ref="refs/heads/release/66",
        source_ref="refs/heads/feature/wingtip",
        url="https://weburl/124",
        created_at=datetime(2026, 5, 24, 10, tzinfo=timezone.utc),
        merged_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
        repository_id="repo-wingtip",
        workstream_ids=("ws-y",)
    )
    
    result = ADOSignalExtractor().extract(
        ADOHydrationOutput(
            work_items=(),
            freshness_items=(),
            pull_requests=(active_pr, merged_pr),
        ),
        "demo"
    )
    
    pr_signals = [s for s in result.signals if "ado/pr/" in s.id]
    assert len(pr_signals) == 2
    
    # Check active PR signal
    active_sig = next(s for s in pr_signals if s.metadata["pr_id"] == 123)
    assert active_sig.metadata["kind"] == "PR_ACTIVE"
    assert active_sig.workstream_id == "ws-x"
    assert active_sig.timestamp == datetime(2026, 5, 24, 10, tzinfo=timezone.utc)
    assert active_sig.entity_refs == ("pr:123", "ado/pr:123", "WS:ws-x", "WI:12345")
    
    # Check merged PR signal
    merged_sig = next(s for s in pr_signals if s.metadata["pr_id"] == 124)
    assert merged_sig.metadata["kind"] == "PR_MERGED"
    assert merged_sig.workstream_id == "ws-y"
    assert merged_sig.timestamp == datetime(2026, 5, 24, 12, tzinfo=timezone.utc)
    assert merged_sig.entity_refs == ("pr:124", "ado/pr:124", "WS:ws-y", "WI:23456")

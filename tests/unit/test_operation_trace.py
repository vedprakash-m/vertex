"""ADF-W2.12: unit tests for src/core/operation_trace.py."""

from __future__ import annotations

from pathlib import Path

from src.core.operation_trace import (
    REF_TYPE_ACTUATION_INTENT,
    REF_TYPE_CONTEXT_MANIFEST,
    REF_TYPE_FACT,
    REF_TYPE_OUTPUT,
    REF_TYPE_RECEIPT,
    REF_TYPE_SOURCE,
    load_operation_trace,
    record_trace_link,
)


def test_missing_correlation_id_returns_none(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert load_operation_trace("xpf", "corr-missing", programs_root=programs_root) is None


def test_single_link_aggregates_into_one_field(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_trace_link(
        program_id="xpf",
        correlation_id="corr-1",
        workflow_id="weekly_report",
        run_id="run-1",
        stage="acquisition",
        ref_type=REF_TYPE_SOURCE,
        ref_id="source-ado-1",
        programs_root=programs_root,
    )
    trace = load_operation_trace("xpf", "corr-1", programs_root=programs_root)
    assert trace is not None
    assert trace.workflow_id == "weekly_report"
    assert trace.run_id == "run-1"
    assert trace.source_refs == ("source-ado-1",)
    assert trace.fact_refs == ()
    assert trace.context_manifest_ref is None


def test_multiple_stages_aggregate_into_the_matching_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    calls = [
        ("acquisition", REF_TYPE_SOURCE, "source-ado-1"),
        ("acquisition", REF_TYPE_SOURCE, "source-kusto-1"),
        ("fact", REF_TYPE_FACT, "fact-rev-42"),
        ("context", REF_TYPE_CONTEXT_MANIFEST, "manifest-hash-abc"),
        ("release", REF_TYPE_ACTUATION_INTENT, "intent-1"),
        ("render", REF_TYPE_OUTPUT, "output-newsletter-xpf-issue-100"),
        ("receipt", REF_TYPE_RECEIPT, "receipt-1"),
    ]
    for stage, ref_type, ref_id in calls:
        record_trace_link(
            program_id="xpf",
            correlation_id="corr-2",
            workflow_id="weekly_report",
            run_id="run-2",
            stage=stage,
            ref_type=ref_type,
            ref_id=ref_id,
            programs_root=programs_root,
        )

    trace = load_operation_trace("xpf", "corr-2", programs_root=programs_root)
    assert trace is not None
    assert trace.source_refs == ("source-ado-1", "source-kusto-1")
    assert trace.fact_refs == ("fact-rev-42",)
    assert trace.context_manifest_ref == "manifest-hash-abc"
    assert trace.actuation_intent_refs == ("intent-1",)
    assert trace.output_refs == ("output-newsletter-xpf-issue-100",)
    assert trace.receipt_refs == ("receipt-1",)


def test_context_manifest_ref_is_most_recent_write_wins(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    for manifest_ref in ("manifest-v1", "manifest-v2"):
        record_trace_link(
            program_id="xpf",
            correlation_id="corr-3",
            workflow_id="weekly_report",
            run_id="run-3",
            stage="context",
            ref_type=REF_TYPE_CONTEXT_MANIFEST,
            ref_id=manifest_ref,
            programs_root=programs_root,
        )
    trace = load_operation_trace("xpf", "corr-3", programs_root=programs_root)
    assert trace is not None
    assert trace.context_manifest_ref == "manifest-v2"


def test_duplicate_ref_id_for_same_ref_type_is_not_duplicated(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    for _ in range(2):
        record_trace_link(
            program_id="xpf",
            correlation_id="corr-4",
            workflow_id="weekly_report",
            run_id="run-4",
            stage="fact",
            ref_type=REF_TYPE_FACT,
            ref_id="fact-1",
            programs_root=programs_root,
        )
    trace = load_operation_trace("xpf", "corr-4", programs_root=programs_root)
    assert trace is not None
    assert trace.fact_refs == ("fact-1",)


def test_different_correlation_ids_do_not_leak_into_each_other(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_trace_link(
        program_id="xpf",
        correlation_id="corr-a",
        workflow_id="weekly_report",
        run_id="run-a",
        stage="fact",
        ref_type=REF_TYPE_FACT,
        ref_id="fact-a",
        programs_root=programs_root,
    )
    record_trace_link(
        program_id="xpf",
        correlation_id="corr-b",
        workflow_id="weekly_report",
        run_id="run-b",
        stage="fact",
        ref_type=REF_TYPE_FACT,
        ref_id="fact-b",
        programs_root=programs_root,
    )
    trace_a = load_operation_trace("xpf", "corr-a", programs_root=programs_root)
    trace_b = load_operation_trace("xpf", "corr-b", programs_root=programs_root)
    assert trace_a is not None and trace_a.fact_refs == ("fact-a",)
    assert trace_b is not None and trace_b.fact_refs == ("fact-b",)


def test_parent_event_id_is_carried_through(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_trace_link(
        program_id="xpf",
        correlation_id="corr-5",
        workflow_id="weekly_report",
        run_id="run-5",
        stage="fact",
        ref_type=REF_TYPE_FACT,
        ref_id="fact-5",
        parent_event_id="parent-event-99",
        programs_root=programs_root,
    )
    trace = load_operation_trace("xpf", "corr-5", programs_root=programs_root)
    assert trace is not None
    assert trace.parent_event_id == "parent-event-99"


def test_edition_id_is_caller_supplied_not_derived_from_the_ledger(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_trace_link(
        program_id="xpf",
        correlation_id="corr-6",
        workflow_id="weekly_report",
        run_id="run-6",
        stage="fact",
        ref_type=REF_TYPE_FACT,
        ref_id="fact-6",
        programs_root=programs_root,
    )
    trace_without = load_operation_trace("xpf", "corr-6", programs_root=programs_root)
    trace_with = load_operation_trace("xpf", "corr-6", edition_id="xpf_weekly", programs_root=programs_root)
    assert trace_without is not None and trace_without.edition_id is None
    assert trace_with is not None and trace_with.edition_id == "xpf_weekly"


def test_realistic_context_to_release_chain_using_shaped_refs(tmp_path: Path) -> None:
    """Demonstrates the mechanism against refs shaped exactly like what
    ADF-W2.7's ContextManifest.context_hash and ADF-W2.9's
    ProgramSynthesisOutcome.ai_run_id actually produce, without modifying
    either module -- OperationTrace propagation is an orchestration
    concern, not something a pure compiler/generator function needs to
    know about internally."""
    programs_root = tmp_path / "programs"
    context_hash = "a3f5c9d1e2b4"  # shaped like a sha256-prefix content hash
    ai_run_id = "9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c"  # shaped like uuid4().hex

    record_trace_link(
        program_id="xpf",
        correlation_id="corr-7",
        workflow_id="weekly_report",
        run_id="run-7",
        stage="context",
        ref_type=REF_TYPE_CONTEXT_MANIFEST,
        ref_id=context_hash,
        programs_root=programs_root,
    )
    record_trace_link(
        program_id="xpf",
        correlation_id="corr-7",
        workflow_id="weekly_report",
        run_id="run-7",
        stage="release",
        ref_type=REF_TYPE_ACTUATION_INTENT,
        ref_id=ai_run_id,
        programs_root=programs_root,
    )

    trace = load_operation_trace("xpf", "corr-7", programs_root=programs_root)
    assert trace is not None
    assert trace.context_manifest_ref == context_hash
    assert trace.actuation_intent_refs == (ai_run_id,)

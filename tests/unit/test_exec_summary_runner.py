from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.ai.ai_mode import AIMode
from src.ai.client import BudgetExceeded
from src.ai.exec_summary_drafter import (
    POLICY_VERSION,
    PROMPT_VERSION,
    ExecSummaryDraftError,
    draft_exec_summary,
    draft_exec_summary_via_runner,
)
from src.ai.structured_feature_runner import RunnerContext
from src.commands import report_ai
from src.core.config_loader import EditorialRules, NarrativeProgramContext, VerbositySettings
from src.core.context_compiler import CompiledContext, ContextCompileRequest, DeterministicContextCompiler, ValidationContext
from src.core.exceptions import StateError
from src.core.models import (
    AttributionTier,
    Confidence,
    DeltaKind,
    DeltaSet,
    EditionType,
    EvidencePacket,
    ItemDelta,
    RiskLevel,
    WorkItem,
)
from src.core.ledger.event_log import read_events


def _runner_context(program_id: str = "demo") -> RunnerContext:
    return RunnerContext(
        program_id=program_id,
        run_id="synthetic-exec-run",
        policy_version=POLICY_VERSION,
        snapshot_id="synthetic-snapshot",
        principal="synthetic-reviewer",
        classification="public",
    )


class _StructuredClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls: list[tuple[str, str, str | None]] = []
        self.deployment = "synthetic-deployment"

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser,
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ):
        del max_tokens
        self.calls.append((system, user, prompt_version))
        return parser({"text": self.response_text})


class _BudgetErrorClient:
    deployment = "synthetic-deployment"

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
        del system, user, parser, max_tokens, prompt_version
        raise BudgetExceeded("synthetic budget ceiling hit")


def _editorial_rules(*, banned_phrases: tuple[str, ...] = ()) -> EditorialRules:
    return EditorialRules(
        schema_version="1.0",
        stale_warn_days=14,
        stale_block_days=30,
        banned_phrases=banned_phrases,
        banned_openings=(),
        verbosity=VerbositySettings(
            workstream_blurb_max_sentences=3,
            workstream_blurb_max_words=60,
            exec_bullet_max_words=None,
            exec_max_bullets=None,
            scorecard_summary_max_sentences=None,
        ),
    )


def _sample_inputs() -> tuple[tuple[WorkItem, ...], DeltaSet]:
    item = WorkItem(
        id=101,
        type="Feature",
        title="Auth gateway cutover",
        state="Active",
        assigned_to="Operator",
        assigned_to_email="operator@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="FY26\\Sprint 20",
        target_date=date(2026, 6, 1),
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={},
    )
    evidence = EvidencePacket(
        work_item_id=101,
        revisions=(),
        comments=(),
        enrichments=(),
        confidence=Confidence.MEDIUM,
        tier=AttributionTier.TIER1,
        summary_for_reviewer="Evidence summary for #101.",
    )
    delta = ItemDelta(
        work_item_id=101,
        kind=DeltaKind.RISK_UP,
        field_changes={},
        old_risk=RiskLevel.MEDIUM,
        new_risk=RiskLevel.HIGH,
        old_eta=None,
        new_eta=None,
        evidence=evidence,
    )
    deltas = DeltaSet(
        issue_number=2,
        previous_issue_number=1,
        new_items=(),
        closed_items=(),
        risk_changes=(delta,),
        eta_changes=(),
        owner_changes=(),
        unchanged_count=0,
    )
    return (item,), deltas


def _read_audit_rows(program_id: str, programs_root: Path) -> list[dict[str, object]]:
    return [
        dict(event.payload)
        for event in read_events(program_id, programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1"
    ]


def test_runner_preserves_parity_and_compiles_bounded_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items, deltas = _sample_inputs()
    rules = _editorial_rules()
    context_obj = NarrativeProgramContext(
        schema_version="1.0",
        program_name="Adventure Demo",
        objective="Deliver identity cutover safely.",
        mission="Deliver identity platform.",
        pillars=(),
        glossary={},
        workstreams=(),
        people=(),
    )
    response_text = "Auth gateway cutover [#101] moved to high risk pending cutover validation."

    legacy_root = tmp_path / "legacy"
    runner_root = tmp_path / "runner"
    legacy_client = _StructuredClient(response_text)
    runner_client = _StructuredClient(response_text)

    legacy_draft = draft_exec_summary(
        client=legacy_client,
        items=items,
        deltas=deltas,
        editorial_rules=rules,
        program_id="demo",
        edition_type=EditionType.DETAILED,
        program_context=context_obj,
        supplemental_context=("synthetic-supplemental-marker",),
        programs_root=legacy_root,
    )

    captures: list[CompiledContext] = []
    validations: list[ValidationContext] = []
    original_compile = DeterministicContextCompiler.compile

    def _capture_compile(
        self: DeterministicContextCompiler,
        request: ContextCompileRequest,
        *,
        validation_context: ValidationContext,
    ) -> CompiledContext:
        compiled = original_compile(self, request, validation_context=validation_context)
        captures.append(compiled)
        validations.append(validation_context)
        return compiled

    monkeypatch.setattr(DeterministicContextCompiler, "compile", _capture_compile)

    runner_draft = draft_exec_summary_via_runner(
        context=_runner_context("demo"),
        client=runner_client,
        items=items,
        deltas=deltas,
        editorial_rules=rules,
        program_id="demo",
        edition_type=EditionType.DETAILED,
        program_context=context_obj,
        supplemental_context=("synthetic-supplemental-marker",),
        programs_root=runner_root,
    )

    assert runner_draft == legacy_draft
    assert runner_draft is not None
    assert runner_draft.cited_work_item_ids == (101,)
    assert runner_draft.ai_confidence == Confidence.MEDIUM
    assert runner_draft.prompt_version == PROMPT_VERSION
    assert len(captures) == 1
    assert validations[0].run_id == "synthetic-exec-run"
    assert runner_client.calls == [(captures[0].prompt_text, "", PROMPT_VERSION)]
    assert "synthetic-supplemental-marker" in captures[0].prompt_text
    assert hashlib.sha256(captures[0].prompt_text.encode("utf-8")).hexdigest() == captures[0].manifest.context_hash

    release_rows = _read_audit_rows("demo", runner_root)
    assert len(release_rows) == 1
    assert release_rows[0]["terminal"] == "released"
    assert isinstance(release_rows[0].get("released_content_hash"), str)


def test_runner_cache_hit_verifies_release_hash_and_tamper_fails_closed(tmp_path: Path) -> None:
    items, deltas = _sample_inputs()
    rules = _editorial_rules()
    client = _StructuredClient("Auth gateway cutover [#101] moved to high risk.")

    first = draft_exec_summary_via_runner(
        context=_runner_context("demo"),
        client=client,
        items=items,
        deltas=deltas,
        editorial_rules=rules,
        program_id="demo",
        programs_root=tmp_path,
    )
    second = draft_exec_summary_via_runner(
        context=_runner_context("demo"),
        client=client,
        items=items,
        deltas=deltas,
        editorial_rules=rules,
        program_id="demo",
        programs_root=tmp_path,
    )

    assert first is not None and second == first
    assert len(client.calls) == 1

    release_rows = _read_audit_rows("demo", tmp_path)
    assert [row["terminal"] for row in release_rows] == ["released", "released"]
    assert release_rows[0]["ai_run_id"] != release_rows[1]["ai_run_id"]

    cache_db = tmp_path / "demo" / "runtime" / "ai_released_result_cache.sqlite3"
    with sqlite3.connect(cache_db) as connection:
        connection.execute(
            "UPDATE released_result_cache SET payload = ?",
            (json.dumps({"text": "Tampered claim [#101]."}, sort_keys=True),),
        )
        connection.commit()

    with pytest.raises(StateError):
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=client,
            items=items,
            deltas=deltas,
            editorial_rules=rules,
            program_id="demo",
            programs_root=tmp_path,
        )
    assert len(client.calls) == 1


def test_runner_context_and_rejection_contracts(tmp_path: Path) -> None:
    items, deltas = _sample_inputs()
    rules = _editorial_rules(banned_phrases=("forbidden phrase",))
    client = _StructuredClient("Auth gateway cutover [#101] has a forbidden phrase.")

    with pytest.raises(StateError):
        draft_exec_summary_via_runner(
            context=_runner_context("other-program"),
            client=client,
            items=items,
            deltas=deltas,
            editorial_rules=rules,
            program_id="demo",
            programs_root=tmp_path,
        )
    with pytest.raises(StateError):
        draft_exec_summary_via_runner(
            context=RunnerContext(
                program_id="demo",
                run_id="r1",
                policy_version="wrong.v0",
                snapshot_id="s1",
                principal="reviewer",
                classification="public",
            ),
            client=client,
            items=items,
            deltas=deltas,
            editorial_rules=rules,
            program_id="demo",
            programs_root=tmp_path,
        )
    assert client.calls == []

    with pytest.raises(ExecSummaryDraftError, match="ban-list"):
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=client,
            items=items,
            deltas=deltas,
            editorial_rules=rules,
            program_id="demo",
            programs_root=tmp_path,
        )
    release_rows = _read_audit_rows("demo", tmp_path)
    assert release_rows[-1]["terminal"] == "rejected"

    with pytest.raises(BudgetExceeded):
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=_BudgetErrorClient(),
            items=items,
            deltas=deltas,
            editorial_rules=_editorial_rules(),
            program_id="demo",
            programs_root=tmp_path,
        )
    release_rows = _read_audit_rows("demo", tmp_path)
    assert release_rows[-1]["terminal"] == "discarded"


def test_runner_returns_none_when_no_ranked_changes(tmp_path: Path) -> None:
    items, _ = _sample_inputs()
    empty_deltas = DeltaSet(
        issue_number=2,
        previous_issue_number=1,
        new_items=(),
        closed_items=(),
        risk_changes=(),
        eta_changes=(),
        owner_changes=(),
        unchanged_count=0,
    )
    client = _StructuredClient("Unused")

    result = draft_exec_summary_via_runner(
        context=_runner_context("demo"),
        client=client,
        items=items,
        deltas=empty_deltas,
        editorial_rules=_editorial_rules(),
        program_id="demo",
        programs_root=tmp_path,
    )
    assert result is None
    assert client.calls == []


def test_report_ai_opt_in_structured_runner_and_context_pinning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items, deltas = _sample_inputs()
    monkeypatch.setenv("VERTEX_OPERATOR_PRINCIPAL", "spoofed-principal")
    with pytest.raises(StateError, match="native OS principal"):
        report_ai.build_exec_summary_runner_context("demo", programs_root=tmp_path)

    monkeypatch.delenv("VERTEX_OPERATOR_PRINCIPAL", raising=False)
    monkeypatch.delenv("VERTEX_OPERATOR_MACHINE", raising=False)
    pinned_context = report_ai.build_exec_summary_runner_context("demo", programs_root=tmp_path)
    assert pinned_context.program_id == "demo"
    assert pinned_context.policy_version == POLICY_VERSION
    assert pinned_context.principal and pinned_context.principal.strip()
    assert len(pinned_context.snapshot_id) == 64

    client = _StructuredClient("Auth gateway cutover [#101] moved to high risk.")
    monkeypatch.setattr(report_ai, "get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr(report_ai, "_report_ai_budget_per_client", lambda _bundle: 1.0)
    monkeypatch.setattr(report_ai, "_resolve_report_ai_deployments", lambda **_kwargs: ("primary", None))
    monkeypatch.setattr(report_ai, "_exec_ai_context_lines", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(report_ai, "_program_synthesis_exec_summary_context_lines", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(report_ai, "load_approved_evidence_by_lane", lambda *_args, **_kwargs: {})

    bundle = SimpleNamespace(
        config=SimpleNamespace(
            ai=SimpleNamespace(
                budget_usd_per_run=1.0,
                temperature=0.2,
                exec_summary_deployment="primary",
                exec_summary_backup_deployment=None,
                blurb_deployment="primary",
                blurb_backup_deployment=None,
            )
        ),
        editorial_rules=_editorial_rules(),
    )
    ai_context = SimpleNamespace(program_id="demo", programs_root=tmp_path, approved_signals=(), workstreams=())

    result = report_ai._synthesize_v2_ai_content(
        bundle=bundle,
        edition_name="demo_weekly",
        issue_number=1,
        started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=items,
        evidence_by_item={},
        deltas=deltas,
        scorecards=(),
        scorecard_packets={},
        overrides_document=None,
        continuity_chapters=(),
        current_exec_summary_text="",
        loaded_exec_summary_text="",
        current_workstream_blurbs={},
        visible_section_ids=set(),
        ai_program_context=None,
        ai_context=ai_context,
        create_ai_client=lambda **_kwargs: client,
        draft_exec_summary_runner=Mock(side_effect=AssertionError("legacy runner should not be called")),
        generate_workstream_blurb_runner=Mock(),
        iter_ai_generated_sections=lambda **_kwargs: (),
        relevant_item_deltas=lambda *_args: (),
        use_structured_runner=True,
    )

    assert "Auth gateway cutover" in result.exec_summary_text
    assert result.prompt_versions == {"exec_summary": PROMPT_VERSION}
    assert result.ai_confidences == {"exec_summary": "medium"}
    assert len(client.calls) == 1


def test_runner_error_and_abstention_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.ai.exec_summary_drafter as drafter

    items, deltas = _sample_inputs()
    rules = _editorial_rules()

    # 1. Corrupt ledger raises StateError before provider call.
    monkeypatch.setattr(drafter, "read_events", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad log")))
    with pytest.raises(StateError, match="release audit"):
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=_StructuredClient("Unused"),
            items=items,
            deltas=deltas,
            editorial_rules=rules,
            program_id="demo",
            programs_root=tmp_path,
        )
    monkeypatch.undo()

    # 2. Outbound request bounds rejection raises ExecSummaryDraftError + DISCARDED.
    oversized_root = tmp_path / "oversized"
    with pytest.raises(ExecSummaryDraftError, match="AISchemaGateway"):
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=_StructuredClient("Unused"),
            items=items,
            deltas=deltas,
            editorial_rules=rules,
            program_id="demo",
            supplemental_context=("x" * 200_001,),
            programs_root=oversized_root,
        )
    assert _read_audit_rows("demo", oversized_root)[-1]["terminal"] == "discarded"

    # 3. Provider returning non-dict or oversized dict rejects cleanly.
    bad_type_client = SimpleNamespace(
        deployment="synthetic-deployment",
        structured=lambda *args, **kwargs: ["not-a-dict"],
    )
    bad_root = tmp_path / "bad_type"
    with pytest.raises(ExecSummaryDraftError, match="must be an object"):
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=bad_type_client,  # type: ignore[arg-type]
            items=items,
            deltas=deltas,
            editorial_rules=rules,
            program_id="demo",
            programs_root=bad_root,
        )
    assert _read_audit_rows("demo", bad_root)[-1]["terminal"] == "rejected"

    oversized_resp_client = SimpleNamespace(
        deployment="synthetic-deployment",
        structured=lambda *args, **kwargs: {"text": "x" * 200_001},
    )
    with pytest.raises(ExecSummaryDraftError, match="AISchemaGateway rejected the response"):
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=oversized_resp_client,  # type: ignore[arg-type]
            items=items,
            deltas=deltas,
            editorial_rules=rules,
            program_id="demo",
            programs_root=bad_root,
        )
    assert _read_audit_rows("demo", bad_root)[-1]["terminal"] == "rejected"

    # 4. Ordinary provider exception propagates for report_ai deployment fallback.
    failing_client = SimpleNamespace(
        deployment="synthetic-deployment",
        structured=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("transient error")),
    )
    with pytest.raises(RuntimeError, match="transient error"):
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=failing_client,  # type: ignore[arg-type]
            items=items,
            deltas=deltas,
            editorial_rules=rules,
            program_id="demo",
            programs_root=bad_root,
        )

    # 5. Empty processed pipeline output returns None and records rejected.
    monkeypatch.setattr(
        drafter,
        "process_generated_text",
        lambda *_args, **_kwargs: SimpleNamespace(text="", cited_work_item_ids=()),
    )
    empty_root = tmp_path / "empty_text"
    assert (
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=_StructuredClient("Unverified claim."),
            items=items,
            deltas=deltas,
            editorial_rules=rules,
            program_id="demo",
            programs_root=empty_root,
        )
        is None
    )
    assert _read_audit_rows("demo", empty_root)[-1]["terminal"] == "rejected"
    monkeypatch.undo()

    # 6. Frontier-ineligible policy abstains cleanly with DISCARDED audit.
    abstain_root = tmp_path / "abstain"
    policy = drafter.load_ai_feature_policy("exec_summary_drafter")
    monkeypatch.setattr(
        drafter,
        "load_ai_feature_policy",
        lambda _feature: SimpleNamespace(
            max_tokens=policy.max_tokens,
            frontier_eligible=False,
        ),
    )
    assert (
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=_StructuredClient("Unused"),
            items=items,
            deltas=deltas,
            editorial_rules=rules,
            program_id="demo",
            programs_root=abstain_root,
        )
        is None
    )
    assert _read_audit_rows("demo", abstain_root)[-1]["terminal"] == "discarded"


def test_build_exec_summary_runner_context_failure_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VERTEX_OPERATOR_PRINCIPAL", raising=False)
    monkeypatch.delenv("VERTEX_OPERATOR_MACHINE", raising=False)

    monkeypatch.setattr(
        report_ai,
        "capture_operator_identity",
        lambda *_args, **_kwargs: SimpleNamespace(principal="   "),
    )
    with pytest.raises(StateError, match="native OS principal"):
        report_ai.build_exec_summary_runner_context("demo", programs_root=tmp_path)
    monkeypatch.undo()

    class _PinFailStore:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def pin_snapshot(self, **_kwargs):
            raise StateError("db unavailable")

    monkeypatch.setattr(report_ai, "ProgramFactStore", _PinFailStore)
    with pytest.raises(StateError, match="could not be pinned"):
        report_ai.build_exec_summary_runner_context("demo", programs_root=tmp_path)

    class _BadDigestStore:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def pin_snapshot(self, **_kwargs):
            return SimpleNamespace(program_id="demo", snapshot_id="s1", content_sha256="not-hex")

        def detect_drift(self, _snapshot_id: str) -> tuple[str, ...]:
            return ()

    monkeypatch.setattr(report_ai, "ProgramFactStore", _BadDigestStore)
    with pytest.raises(StateError, match="does not match"):
        report_ai.build_exec_summary_runner_context("demo", programs_root=tmp_path)

    class _DriftStore:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def pin_snapshot(self, **_kwargs):
            return SimpleNamespace(program_id="demo", snapshot_id="s1", content_sha256="a" * 64)

        def detect_drift(self, _snapshot_id: str) -> tuple[str, ...]:
            return ("table_changed",)

    monkeypatch.setattr(report_ai, "ProgramFactStore", _DriftStore)
    with pytest.raises(StateError, match="changed before exec summary execution"):
        report_ai.build_exec_summary_runner_context("demo", programs_root=tmp_path)


def test_runner_cache_replay_and_audit_failure_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.ai.exec_summary_drafter as drafter

    items, deltas = _sample_inputs()
    base_rules = _editorial_rules()
    client = _StructuredClient("Auth gateway cutover [#101] moved to high risk.")

    # Populate released cache under base editorial rules.
    assert (
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=client,
            items=items,
            deltas=deltas,
            editorial_rules=base_rules,
            program_id="demo",
            programs_root=tmp_path,
        )
        is not None
    )

    # 1. Non-dict JSON in SQLite cache fails deserialize cleanly.
    cache_db = tmp_path / "demo" / "runtime" / "ai_released_result_cache.sqlite3"
    saved_row = None
    with sqlite3.connect(cache_db) as connection:
        saved_row = connection.execute(
            "SELECT cache_key, payload, released_content_hash FROM released_result_cache"
        ).fetchone()
        connection.execute("UPDATE released_result_cache SET payload = ?", ("123",))
        connection.commit()

    with pytest.raises(StateError):
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=client,
            items=items,
            deltas=deltas,
            editorial_rules=base_rules,
            program_id="demo",
            programs_root=tmp_path,
        )

    # Restore valid cached row and test tightening editorial ban-list against cached replay.
    assert saved_row is not None
    cache_key, valid_payload, valid_hash = saved_row
    # Re-populate cleanly via runner, then verify audit OSError on replay fails closed.
    assert (
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=client,
            items=items,
            deltas=deltas,
            editorial_rules=base_rules,
            program_id="demo",
            programs_root=tmp_path,
        )
        is not None
    )

    monkeypatch.setattr(
        drafter,
        "released_content_matches",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("audit unreadable")),
    )
    with pytest.raises(StateError, match="release audit could not be verified"):
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=client,
            items=items,
            deltas=deltas,
            editorial_rules=base_rules,
            program_id="demo",
            programs_root=tmp_path,
        )
    monkeypatch.undo()

    # 2. Tighten editorial rules on cache replay (monkeypatched ban-list) fails closed.
    orig_validate = drafter._validate_editorial_rules

    def _reject_replay(text: str, rules: EditorialRules, *, edition_type: EditionType | None) -> None:
        del text, rules, edition_type
        raise ExecSummaryDraftError("ban-list rule tightened after cache")

    monkeypatch.setattr(drafter, "_validate_editorial_rules", _reject_replay)
    with pytest.raises(StateError, match="semantic validation"):
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=client,
            items=items,
            deltas=deltas,
            editorial_rules=base_rules,
            program_id="demo",
            programs_root=tmp_path,
        )
    monkeypatch.setattr(drafter, "_validate_editorial_rules", orig_validate)
    monkeypatch.undo()

    # 3. Lifecycle audit failure propagates instead of being swallowed as unavailable.
    lifecycle_root = tmp_path / "lifecycle_fail"
    orig_lifecycle = drafter.record_ai_run_lifecycle

    def _fail_on_responded(**kwargs: object) -> None:
        if kwargs.get("state") == drafter.AIRunState.RESPONDED:
            raise OSError("disk full")
        orig_lifecycle(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(drafter, "record_ai_run_lifecycle", _fail_on_responded)
    with pytest.raises(OSError, match="disk full"):
        draft_exec_summary_via_runner(
            context=_runner_context("demo"),
            client=_StructuredClient("Auth gateway cutover [#101] moved to high risk."),
            items=items,
            deltas=deltas,
            editorial_rules=base_rules,
            program_id="demo",
            programs_root=lifecycle_root,
        )

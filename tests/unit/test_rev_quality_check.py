"""P2-3 REV quality-floor regression gate tests.

Exercises ``compute_quality_report`` + ``render_report_human`` +
``verify_judge_independence`` (Zone-A pure) + the ``scripts/rev_quality_check.py``
wrapper ``main()``. Candidates are staged via real ``run_rev_cycle`` calls so the
proposed_event_type + grounding flags reflect the actual pipeline, then a
labeled corpus is authored and the G-floor metrics + gates are asserted.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.ledger.candidate_store import load_pending_candidates
from src.core.rev.quality_metrics import (
    G_ACCEPT_PREC_FLOOR,
    G_XTRACT_PREC_FLOOR,
    activation_denominator_plan,
    compute_quality_report,
    minimum_successes_for_wilson_floor,
    minimum_total_for_perfect_wilson_floor,
    render_report_human,
    verify_judge_independence,
    wilson_ci,
)

NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


def _stage(program_id: str, programs_root: Path, bodies: list[str]) -> list:
    """Stage one candidate per body via real cycles; return the staged candidates."""
    from src.ai.rev.extractor import DeterministicRevExtractor
    from src.ai.rev.verification import run_layered_verification
    from src.core.models_v2 import REV_PROFILE_SEARCH_HYDRATE, RevRetrievalProfile
    from src.core.rev.entity_types import EntityType
    from src.core.rev.governor import BudgetLimits
    from src.core.rev.pipeline import RevPipelineDeps, run_rev_cycle
    from src.core.rev.prompt_shields import LocalOnlyPromptShields
    from src.core.rev.query_planner import RetrievalIntent
    from src.m365.rev import FakeRevGraphClient, GraphMessage
    from src.m365.rev.enumerators import CollectionSearchEnumerator, MailboxContext
    from src.m365.rev.hydrator import MailHydrator

    msgs = tuple(
        GraphMessage(
            message_id=f"msg-{i}",
            subject=f"signal {i}",
            sender="owner@example.com",
            received_at="2026-06-23T10:00:00Z",
            unique_body=b, body=b,
            conversation_id=f"conv-{i}", etag=f"e{i}", immutable_id=f"i{i}",
        )
        for i, b in enumerate(bodies)
    )
    graph = FakeRevGraphClient(msgs)
    mailbox = MailboxContext(tenant_id="t", principal_mailbox="u@x.com", container="inbox")
    deps = RevPipelineDeps(
        enumerator=CollectionSearchEnumerator(graph, mailbox),
        hydrator=MailHydrator(graph, mailbox),
        shields=LocalOnlyPromptShields(),
        extractor=DeterministicRevExtractor(),
        verifier=lambda **kw: run_layered_verification(**kw).effective_state,
    )
    run_rev_cycle(
        program_id=program_id,
        intent=RetrievalIntent(entity_type=EntityType.MESSAGE, limit=25),
        deps=deps,
        profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
        mailbox_tenant_id="t", mailbox_principal="u@x.com", mailbox_container="inbox",
        correlation_id="qc", programs_root=programs_root,
        budget_limits=BudgetLimits(), set_at=NOW,
    )
    return list(load_pending_candidates(program_id, programs_root=programs_root))


def _write_corpus(programs_root: Path, program_id: str, rows: list[dict]) -> Path:
    qdir = programs_root / program_id / "_quality"
    qdir.mkdir(parents=True, exist_ok=True)
    path = qdir / "rev_labeled_corpus.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


def _label_rows(cands: list, expected_map: dict[str, str], label: str = "accept") -> list[dict]:
    """expected_map: candidate_id → expected_event_type."""
    return [
        {"candidate_id": c.candidate_id, "expected_event_type": expected_map[c.candidate_id], "label": label}
        for c in cands
    ]


class TestQualityReportPassing:
    def test_all_correct_labels_pass_gates(self, tmp_path: Path) -> None:
        # v2.22 (ADR-0006 R2): deployment.completed bodies now stage as
        # deployment.completed.v1 (faithful type), not milestone.completed.v1.
        bodies = [f"The rollout deployment completed on 2026-06-{20+i:02d} without issues." for i in range(5)]
        cands = _stage("p-qc-pass", tmp_path, bodies)
        assert len(cands) == 5
        proposed = {c.candidate_id: c.proposed_event_type for c in cands}
        # All staged as deployment.completed.v1.
        assert all(v == "deployment.completed.v1" for v in proposed.values())
        rows = _label_rows(cands, proposed, label="accept")
        _write_corpus(tmp_path, "p-qc-pass", rows)

        report = compute_quality_report(program_id="p-qc-pass", programs_root=tmp_path)
        assert report.n_matched_candidate == 5
        assert report.g_xtract_prec == 1.0  # all correct
        assert report.g_accept_prec == 1.0  # all accept + grounded (cycles vault evidence)
        assert report.ok is True
        assert not report.failures
        # Per-type recall for deployment.completed.v1 with N=5 → recall 1.0 ≥ 0.50.
        pt = [p for p in report.per_type if p.event_type == "deployment.completed.v1"][0]
        assert pt.n == 5 and not pt.insufficient_sample and pt.recall == 1.0


class TestQualityReportFailing:
    def test_extraction_precision_below_floor_fails(self, tmp_path: Path) -> None:
        bodies = [f"The rollout deployment completed on 2026-06-{20+i:02d}." for i in range(5)]
        cands = _stage("p-qc-fail1", tmp_path, bodies)
        # Label 2 correctly, 3 with a wrong expected type → xtract-prec 2/5 = 0.4 < 0.80.
        proposed = {c.candidate_id: c.proposed_event_type for c in cands}
        cids = list(proposed)
        expected_map = dict(proposed)
        for cid in cids[2:]:
            expected_map[cid] = "some.wrong.type.v1"
        rows = _label_rows(cands, expected_map, label="accept")
        _write_corpus(tmp_path, "p-qc-fail1", rows)

        report = compute_quality_report(program_id="p-qc-fail1", programs_root=tmp_path)
        assert report.g_xtract_prec < G_XTRACT_PREC_FLOOR
        assert report.ok is False
        assert any("G-xtract-prec" in f for f in report.failures)

    def test_per_type_recall_below_floor_fails(self, tmp_path: Path) -> None:
        # 5 candidates expected as milestone.completed.v1: 2 proposed correctly
        # (milestone.completed.v1) + 3 proposed with a wrong type → recall 2/5.
        # v2.22 (ADR-0006 R2): rollback bodies now extract as deployment.rollback.v1
        # (faithful type), so we use a mismatched expected type to drive recall down.
        bodies = [
            "The rollout deployment completed on 2026-06-20.",
            "The rollout deployment completed on 2026-06-21.",
            "The rollout deployment completed on 2026-06-22.",
            "The rollout deployment completed on 2026-06-23.",
            "The rollout deployment completed on 2026-06-24.",
        ]
        cands = _stage("p-qc-fail2", tmp_path, bodies)
        assert len(cands) == 5
        # Expected = milestone.completed.v1 for all; proposed = deployment.completed.v1
        # (R2) → type mismatch on all 5 → recall[milestone.completed.v1] = 0/5 = 0.0.
        expected_map = {c.candidate_id: "milestone.completed.v1" for c in cands}
        rows = _label_rows(cands, expected_map, label="accept")
        _write_corpus(tmp_path, "p-qc-fail2", rows)

        report = compute_quality_report(program_id="p-qc-fail2", programs_root=tmp_path)
        pt = [p for p in report.per_type if p.event_type == "milestone.completed.v1"][0]
        assert pt.n == 5 and not pt.insufficient_sample
        assert pt.recall == 0.0  # 0 of 5 correct (all proposed as deployment.completed.v1)
        assert any("recall[milestone.completed.v1]" in f for f in report.failures)
        assert report.ok is False

    def test_no_corpus_fails(self, tmp_path: Path) -> None:
        report = compute_quality_report(program_id="p-none", programs_root=tmp_path)
        assert report.ok is False
        assert any("no labeled corpus" in f for f in report.failures)

    def test_empty_corpus_fails(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, "p-empty", [])
        report = compute_quality_report(program_id="p-empty", programs_root=tmp_path)
        assert report.ok is False
        assert any("empty" in f for f in report.failures)


class TestInsufficientSample:
    def test_low_n_type_marked_insufficient_not_gated(self, tmp_path: Path) -> None:
        # 2 candidates of one type, both labeled correctly → N=2 < 5 →
        # insufficient_sample_for_gate; recall not gated. xtract-prec=1.0 passes.
        # v2.22 (ADR-0006 R2): deployment bodies stage as deployment.completed.v1.
        bodies = ["Deployment completed 2026-06-20.", "Deployment completed 2026-06-21."]
        cands = _stage("p-qc-low", tmp_path, bodies)
        proposed = {c.candidate_id: c.proposed_event_type for c in cands}
        rows = _label_rows(cands, proposed, label="accept")  # both correct
        _write_corpus(tmp_path, "p-qc-low", rows)

        report = compute_quality_report(program_id="p-qc-low", programs_root=tmp_path)
        pt = [p for p in report.per_type if p.event_type == "deployment.completed.v1"][0]
        assert pt.insufficient_sample is True
        assert pt.n == 2
        assert not any("recall[deployment.completed.v1]" in f for f in report.failures)
        assert report.g_xtract_prec == 1.0
        assert report.ok is True  # only insufficient-sample type → no recall gate, xtract passes


class TestWilsonDenominatorPlan:
    def test_wilson_ci_lower_bound_for_perfect_n30_is_below_80_percent(self) -> None:
        low, high = wilson_ci(30, 30)

        assert round(low, 4) == 0.8865
        assert high == 1.0

    def test_minimum_total_for_perfect_floor(self) -> None:
        assert minimum_total_for_perfect_wilson_floor(floor=0.80) == 16
        assert minimum_total_for_perfect_wilson_floor(floor=0.85) == 22

    def test_minimum_successes_for_data_floor_can_be_impossible(self) -> None:
        assert minimum_successes_for_wilson_floor(total=30, floor=0.80) == 29
        assert minimum_successes_for_wilson_floor(total=30, floor=0.85) == 30
        assert minimum_successes_for_wilson_floor(total=5, floor=0.80) is None

    def test_activation_denominator_plan_serializes_floor_guidance(self) -> None:
        plan = activation_denominator_plan(data_floor=30)
        by_metric = {row.metric: row for row in plan}

        assert by_metric["g_xtract_prec_ci_low"].min_total_if_perfect == 16
        assert by_metric["g_accept_prec_ci_low"].min_total_if_perfect == 22
        assert by_metric["critical_recall_ci_low"].floor == 0.60
        assert by_metric["g_xtract_prec_ci_low"].to_dict()["min_successes_at_data_floor"] == 29
        assert by_metric["g_accept_prec_ci_low"].to_dict()["min_successes_at_data_floor"] == 30


class TestKappa:
    def test_kappa_computed_from_dual_annotations(self, tmp_path: Path) -> None:
        bodies = [f"Deployment completed 2026-06-{20+i:02d}." for i in range(5)]
        cands = _stage("p-qc-kappa", tmp_path, bodies)
        proposed = {c.candidate_id: c.proposed_event_type for c in cands}
        # All accept + second_label accept → perfect agreement → kappa 1.0.
        rows = [
            {"candidate_id": c.candidate_id, "expected_event_type": proposed[c.candidate_id],
             "label": "accept", "second_label": "accept"}
            for c in cands
        ]
        _write_corpus(tmp_path, "p-qc-kappa", rows)
        report = compute_quality_report(program_id="p-qc-kappa", programs_root=tmp_path)
        assert report.kappa is not None
        assert report.kappa == 1.0
        assert report.kappa_n == 5

    def test_kappa_absent_without_second_label(self, tmp_path: Path) -> None:
        bodies = ["Deployment completed 2026-06-20."]
        cands = _stage("p-qc-nokappa", tmp_path, bodies)
        proposed = {c.candidate_id: c.proposed_event_type for c in cands}
        rows = _label_rows(cands, proposed, label="accept")
        _write_corpus(tmp_path, "p-qc-nokappa", rows)
        report = compute_quality_report(program_id="p-qc-nokappa", programs_root=tmp_path)
        assert report.kappa is None


class TestJudgeIndependence:
    def test_same_deployment_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "dep-a")
        monkeypatch.setenv("VERTEX_AI_JUDGE_DEPLOYMENT", "dep-a")
        ok, msg = verify_judge_independence()
        assert ok is False
        assert "differs" not in msg.lower() or "must use a different" in msg.lower()

    def test_missing_judge_deployment_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "dep-a")
        monkeypatch.delenv("VERTEX_AI_JUDGE_DEPLOYMENT", raising=False)
        ok, msg = verify_judge_independence()
        assert ok is False

    def test_distinct_deployments_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "dep-a")
        monkeypatch.setenv("VERTEX_AI_JUDGE_DEPLOYMENT", "dep-b")
        ok, msg = verify_judge_independence()
        assert ok is True


class TestScriptWrapper:
    def test_script_main_pass_exit_zero(self, tmp_path: Path) -> None:
        from scripts.rev_quality_check import main as qc_main

        bodies = [f"Deployment completed 2026-06-{20+i:02d}." for i in range(5)]
        cands = _stage("p-script-pass", tmp_path, bodies)
        proposed = {c.candidate_id: c.proposed_event_type for c in cands}
        _write_corpus(tmp_path, "p-script-pass", _label_rows(cands, proposed, label="accept"))
        rc = qc_main(["--program", "p-script-pass", "--programs-root", str(tmp_path), "--json"])
        assert rc == 0

    def test_script_output_writes_metrics_json(self, tmp_path: Path) -> None:
        from scripts.rev_quality_check import main as qc_main

        bodies = [f"Deployment completed 2026-06-{20+i:02d}." for i in range(5)]
        cands = _stage("p-script-output", tmp_path, bodies)
        proposed = {c.candidate_id: c.proposed_event_type for c in cands}
        _write_corpus(tmp_path, "p-script-output", _label_rows(cands, proposed, label="accept"))
        output = tmp_path / "p-script-output" / "_quality" / "rev_quality_metrics.json"

        rc = qc_main([
            "--program",
            "p-script-output",
            "--programs-root",
            str(tmp_path),
            "--output",
            str(output),
        ])

        assert rc == 0
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["program_id"] == "p-script-output"
        assert payload["g_xtract_prec"] == 1.0

    def test_script_main_fail_exit_one(self, tmp_path: Path) -> None:
        from scripts.rev_quality_check import main as qc_main

        # No corpus → fail.
        rc = qc_main(["--program", "p-script-fail", "--programs-root", str(tmp_path)])
        assert rc == 1

    def test_script_human_report_rendered(self, tmp_path: Path, capsys) -> None:
        from scripts.rev_quality_check import main as qc_main

        bodies = [f"Deployment completed 2026-06-{20+i:02d}." for i in range(5)]
        cands = _stage("p-script-human", tmp_path, bodies)
        proposed = {c.candidate_id: c.proposed_event_type for c in cands}
        _write_corpus(tmp_path, "p-script-human", _label_rows(cands, proposed, label="accept"))
        rc = qc_main(["--program", "p-script-human", "--programs-root", str(tmp_path)])
        out = capsys.readouterr().out
        assert "REV Quality-Floor Report" in out
        assert "G-xtract-prec" in out
        assert rc == 0

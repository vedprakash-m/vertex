from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import cockpit as cockpit_module
from src.commands import context as context_module
from src.core.communication_budget import (
    CommunicationBudgetFinding,
    communication_budget_policy_from_edition,
    evaluate_communication_budget,
)
from src.core.exceptions import ConfigError, StateError
from src.core.exception_queue import ExceptionItem, decorate_exception_queue
from src.core.models import NotifyPreview
from src.core.models_v2 import Signal
from src.core.sprint_plan import (
    SprintBacklogItem,
    SprintCapacityInput,
    SprintCompletedPointsInput,
    _decimal_value,
    _int_value,
    _integer_tuple,
    plan_sprint,
)
from src.core.vertical_artifact_evaluation import (
    QualificationStatus,
    evaluate_vertical_artifact_protocol,
)


NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def _artifact(*, cost: float = 1.0, review: float = 10.0) -> dict[str, Any]:
    return {
        "slices": [
            {
                "kind": kind,
                "facts": [{
                    "fact_id": f"{kind}-1",
                    "value": {"status": "blocked"},
                    "evidence_refs": ["WI:1"],
                }],
            }
            for kind in ("status", "dependency", "meeting-close")
        ],
        "metrics": {
            "cost_usd": {"value": cost, "evidence_refs": ["cost:1"]},
            "review_seconds": {"value": review, "evidence_refs": ["timer:1"]},
        },
    }


def _qualified_external() -> dict[str, Any]:
    return {
        name: {"status": "verified", "evidence_refs": [f"{name}:1"]}
        for name in (
            "d9_decision",
            "owner",
            "blind_audit_protocol",
            "live_cycle",
            "real_program",
        )
    }


@pytest.mark.parametrize("output_format", ["html", "json", "protocol"])
def test_exception_formats_cover_static_protocol_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output_format: str,
) -> None:
    root = tmp_path / "programs"
    (root / "acme").mkdir(parents=True)
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(cockpit_module, "get_risk_register_path", lambda *_a, **_k: root / "risk.yaml")
    monkeypatch.setattr(cockpit_module, "has_action_history", lambda *_a, **_k: False)
    monkeypatch.setattr(cockpit_module, "load_risk_register", lambda *_a, **_k: ())
    monkeypatch.setattr(cockpit_module, "load_dispatch_exceptions", lambda *_a, **_k: ())

    result = CliRunner().invoke(
        app,
        [
            "cockpit",
            "exceptions",
            "--program",
            "acme",
            "--as-of",
            "2026-11-21",
            "--format",
            output_format,
        ],
    )

    assert result.exit_code == 0, result.output
    if output_format != "protocol":
        assert "acme" in result.output
    if output_format == "html":
        assert "<main" in result.output
    else:
        assert json.loads(result.output)["schema_version"] == "1"


def test_exception_revalidation_covers_success_missing_stale_and_invalid_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = tmp_path / "programs"
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(cockpit_module, "load_risk_register", lambda *_a, **_k: ())
    monkeypatch.setattr(cockpit_module, "has_action_history", lambda *_a, **_k: False)
    monkeypatch.setattr(cockpit_module, "load_dispatch_exceptions", lambda *_a, **_k: ())
    item = ExceptionItem("overdue_action", "action-1", 1, 2, "Overdue.", "actions.jsonl#action-1")
    decorated = decorate_exception_queue(
        (item,), program_id="acme", as_of=date(2026, 11, 21), extended=False,
    )[0]
    monkeypatch.setattr(cockpit_module, "build_exception_queue", lambda *_a, **_k: (item,))
    runner = CliRunner()
    arguments = [
        "cockpit", "exception", "--program", "acme", "--kind", "overdue_action",
        "--subject", "action-1", "--as-of", "2026-11-21",
    ]

    success = runner.invoke(app, arguments + ["--expect-fingerprint", decorated.fingerprint])
    stale = runner.invoke(app, arguments + ["--expect-fingerprint", "stale"])
    missing = runner.invoke(
        app,
        arguments[:5] + ["missing"] + arguments[6:] + ["--expect-fingerprint", decorated.fingerprint],
    )
    invalid = runner.invoke(
        app,
        arguments[:-1] + ["20261121", "--expect-fingerprint", decorated.fingerprint],
    )

    assert success.exit_code == 0 and json.loads(success.output)["kind"] == "overdue_action"
    assert stale.exit_code == 1 and "fingerprint changed" in stale.output
    assert missing.exit_code == 1 and "no longer present" in missing.output
    assert invalid.exit_code == 2 and "YYYY-MM-DD" in invalid.output


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, "baseline"),
        ({"baseline": {"slices": "bad"}, "candidate": {}}, "slices must be a list"),
        (
            {
                "baseline": {"slices": [None], "metrics": {}},
                "candidate": {"slices": [], "metrics": {}},
            },
            "entries must identify a kind",
        ),
        (
            {
                "baseline": {"slices": [], "metrics": "bad"},
                "candidate": {"slices": [], "metrics": {}},
            },
            "metrics must be an object",
        ),
    ],
)
def test_vertical_protocol_rejects_malformed_shapes(payload: object, expected: str) -> None:
    report = evaluate_vertical_artifact_protocol(
        payload if isinstance(payload, dict) else {},  # type: ignore[arg-type]
    )
    assert report.qualification_status is QualificationStatus.NOT_QUALIFIED
    assert expected in " ".join(report.schema_errors)


def test_vertical_protocol_covers_duplicate_missing_nonfinite_and_unsupported_rows() -> None:
    baseline = _artifact()
    candidate = _artifact()
    baseline["slices"][0]["facts"][0]["evidence_refs"] = []
    candidate["slices"][0]["facts"].append({**candidate["slices"][0]["facts"][0]})
    candidate["slices"][0]["facts"].append({
        "fact_id": "unsupported", "value": "new", "evidence_refs": [],
    })
    duplicate_kind_payload = {
        "baseline": baseline,
        "candidate": {
            **candidate,
            "slices": [*candidate["slices"], {"kind": "status", "facts": []}],
        },
        "external_evidence": _qualified_external(),
    }
    duplicate_kind_report = evaluate_vertical_artifact_protocol(duplicate_kind_payload)
    candidate["metrics"]["cost_usd"]["value"] = float("nan")
    candidate["metrics"]["review_seconds"]["evidence_refs"] = []
    del baseline["slices"][1]
    report = evaluate_vertical_artifact_protocol({
        "baseline": baseline,
        "candidate": candidate,
        "external_evidence": _qualified_external(),
    })

    assert report.qualification_status is QualificationStatus.NOT_QUALIFIED
    errors = " ".join(report.schema_errors)
    assert "missing baseline slice" in errors
    assert "duplicate kind" in " ".join(duplicate_kind_report.schema_errors)
    assert "duplicate fact_id" in errors
    assert "finite numbers" in errors
    assert "missing candidate metric evidence" in errors
    assert "unsupported" in next(
        row.unsupported_fact_ids for row in report.slices if row.kind == "status"
    )


def test_readiness_vertical_evaluate_cli_covers_qualified_unqualified_and_invalid(
    tmp_path: Path,
) -> None:
    protocol = tmp_path / "vertical.json"
    qualified = {
        "baseline": _artifact(cost=2.0, review=20.0),
        "candidate": _artifact(cost=1.0, review=10.0),
        "external_evidence": _qualified_external(),
    }
    runner = CliRunner()
    protocol.write_text(json.dumps(qualified), encoding="utf-8")
    success = runner.invoke(
        app, ["readiness", "vertical-evaluate", "--protocol", str(protocol)]
    )
    qualified["external_evidence"] = {}
    protocol.write_text(json.dumps(qualified), encoding="utf-8")
    unqualified = runner.invoke(
        app, ["readiness", "vertical-evaluate", "--protocol", str(protocol)]
    )
    protocol.write_text("{", encoding="utf-8")
    invalid = runner.invoke(
        app, ["readiness", "vertical-evaluate", "--protocol", str(protocol)]
    )

    assert success.exit_code == 0
    assert unqualified.exit_code == 1
    assert invalid.exit_code == 2
    assert "not_qualified" in unqualified.output
    assert "Invalid vertical protocol" in invalid.output


def test_close_reply_command_covers_success_and_bound_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.context_gap_reply_import import ContextGapReplyImportError

    monkeypatch.setattr(
        context_module, "_resolve_program_id", lambda *_a, **_k: "acme"
    )
    proposal = SimpleNamespace(proposal_id="reply-1")
    monkeypatch.setattr(
        "src.core.context_gap_reply_import.close_context_gap_reply",
        lambda **_kwargs: proposal,
    )
    runner = CliRunner()
    arguments = [
        "context", "close-reply", "--edition", "weekly", "--issue", "1",
        "--proposal-id", "reply-1", "--reason", "Manual edit verified.",
    ]
    success = runner.invoke(app, arguments + ["--dry-run"])

    monkeypatch.setattr(
        "src.core.context_gap_reply_import.close_context_gap_reply",
        lambda **_kwargs: (_ for _ in ()).throw(
            ContextGapReplyImportError("closure refused")
        ),
    )
    refused = runner.invoke(app, arguments)

    assert success.exit_code == 0 and "Dry run" in success.output
    assert refused.exit_code == 2 and "closure refused" in refused.output


def test_reply_closure_validation_covers_refusal_and_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.context_gap_reply_import import (
        ContextGapReplyImportError,
        close_context_gap_reply,
        verify_context_reply_origin,
    )
    from src.core.context_gap_store import load_context_gap_resolutions
    from src.core.ncfl_proposal_store import update_proposal_status
    from tests.unit.test_context_gap_reply_import import _origin_workspace

    with pytest.raises(ContextGapReplyImportError, match="positive issue"):
        close_context_gap_reply(
            program_id="../bad",
            issue_number=0,
            proposal_id="reply",
            actor="operator",
            reason=" ",
            programs_root=tmp_path,
        )

    programs, arguments, _packet, proposal = _origin_workspace(tmp_path, monkeypatch)
    base = dict(
        program_id="sample",
        issue_number=1,
        proposal_id=proposal.proposal_id,
        actor="operator",
        reason="Manual edit verified.",
        programs_root=programs,
    )
    with pytest.raises(ContextGapReplyImportError, match="accepted proposal"):
        close_context_gap_reply(**base)
    with pytest.raises(ContextGapReplyImportError, match="retained reply"):
        close_context_gap_reply(**{**base, "proposal_id": "missing"})

    verify_context_reply_origin(
        program_id="sample",
        issue_number=1,
        proposal_id=proposal.proposal_id,
        actor="display-label",
        reason="Origin checked.",
        programs_root=programs,
    )
    update_proposal_status(
        "sample",
        proposal_id=proposal.proposal_id,
        issue_number=1,
        new_status="accepted",
        actor="approver",
        programs_root=programs,
    )
    with pytest.raises(ContextGapReplyImportError, match="does not match"):
        close_context_gap_reply(**base)

    registry = programs / "sample" / "workstream_registry.yaml"
    registry.write_text(
        "workstreams:\n- id: deployment\n  deep_context:\n    why: The exact proposed answer.\n",
        encoding="utf-8",
    )
    (programs / "sample" / "review.yaml").write_text(
        "reply_origin_verifier_principals: []\n", encoding="utf-8"
    )
    with pytest.raises(ContextGapReplyImportError, match="revoked or absent"):
        close_context_gap_reply(**base)

    (programs / "sample" / "review.yaml").write_text(
        "reply_origin_verifier_principals: [synthetic-reviewer]\n",
        encoding="utf-8",
    )
    closed = close_context_gap_reply(**base, dry_run=True)
    assert closed.proposal_id == proposal.proposal_id
    assert load_context_gap_resolutions("sample", programs_root=programs) == ()


def test_report_email_fallbacks_and_notify_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.commands import report_email

    refs = NotifyPreview(("lead@example.com",), (), "Report", "<p>ADO#12</p>", "ADO#12", ())
    counted = NotifyPreview(
        ("lead@example.com",), (), "8 items need your update", "<p>Review</p>", "Review", ()
    )
    empty = NotifyPreview(
        ("lead@example.com",), (), "No indexed asks", "<p>Review</p>", "Review", ()
    )
    assert report_email._notify_material_asks(refs) == ("ADO#12",)
    assert len(report_email._notify_material_asks(counted)) == 8
    assert report_email._notify_material_asks(empty) == ()

    finding = CommunicationBudgetFinding(
        "within_cooldown", "lead@example.com", "recent"
    )
    monkeypatch.setattr(
        report_email,
        "assess_notify_communication_budgets",
        lambda **_kwargs: (),
    )
    assert report_email.enforce_notify_communication_budgets(
        edition_name="weekly",
        previews=(),
        dri_summaries=(),
        programs_root=Path("."),
        now=NOW,
    ) == ()
    monkeypatch.setattr(
        report_email,
        "assess_notify_communication_budgets",
        lambda **_kwargs: (finding,),
    )
    with pytest.raises(StateError, match="blocks notification send"):
        report_email.enforce_notify_communication_budgets(
            edition_name="weekly",
            previews=(),
            dri_summaries=(),
            programs_root=Path("."),
            now=NOW,
        )
    clean = evaluate_communication_budget(
        audience_id="lead@example.com",
        asks=(),
        prior_sends=(),
        cooldown_days=0,
        now=NOW,
    )
    assert clean == ()
    assert finding.code == "within_cooldown"


def test_sprint_plan_validation_and_adapter_helpers() -> None:
    with pytest.raises(ConfigError, match="negative"):
        plan_sprint(
            iteration_path="sprint",
            backlog=(),
            capacity=SprintCapacityInput(Decimal("-1"), "capacity"),
            completed_sprints=(),
        )
    with pytest.raises(ConfigError, match="positive"):
        plan_sprint(
            iteration_path="sprint",
            backlog=(SprintBacklogItem(1, "bad", Decimal("0"), (), "WI:1"),),
            capacity=SprintCapacityInput(Decimal("1"), "capacity"),
            completed_sprints=(),
        )
    with pytest.raises(ConfigError, match="unique"):
        plan_sprint(
            iteration_path="sprint",
            backlog=(
                SprintBacklogItem(1, "a", Decimal("1"), (), "WI:1"),
                SprintBacklogItem(1, "b", Decimal("1"), (), "WI:1"),
            ),
            capacity=SprintCapacityInput(Decimal("1"), "capacity"),
            completed_sprints=(),
        )
    blocked = plan_sprint(
        iteration_path="sprint",
        backlog=(
            SprintBacklogItem(1, "first", Decimal("1"), (), "WI:1"),
            SprintBacklogItem(2, "second", Decimal("1"), (1,), "WI:2"),
        ),
        capacity=SprintCapacityInput(Decimal("80"), "capacity"),
        completed_sprints=(
            SprintCompletedPointsInput("prior", Decimal("1.5"), ("sprint:prior",)),
        ),
    )
    assert blocked.selected_story_points == Decimal("1")
    assert blocked.deferred[0].reason == "over_point_capacity"
    assert _decimal_value(True) is None
    assert _decimal_value("nan") is None
    assert _decimal_value("not-a-number") is None
    assert _int_value(True) is None
    assert _int_value("x") is None
    assert _integer_tuple(None) == ()
    assert _integer_tuple(("x", 2, False, 3)) == (2, 3)


def test_sprint_plan_telemetry_formats_valid_invalid_and_unavailable() -> None:
    from src.core.telemetry_summary import _format_sprint_plan_summary

    base = Signal(
        id="signal",
        timestamp=NOW,
        source="ado",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=(),
        text="sprint",
        raw_ref=None,
        confidence="low",  # type: ignore[arg-type]
        metadata={},
    )
    assert _format_sprint_plan_summary(base) is None
    assert _format_sprint_plan_summary(replace(base, metadata={"sprint_plan": []})) is None
    malformed = replace(
        base, metadata={"sprint_plan": {"selected": "bad", "deferred": []}}
    )
    assert _format_sprint_plan_summary(malformed) is None
    unavailable = replace(
        base,
        metadata={
            "sprint_plan": {
                "selected": [],
                "deferred": [],
                "point_capacity": None,
                "selected_story_points": "0",
            }
        },
    )
    assert _format_sprint_plan_summary(unavailable).startswith("plan unavailable")
    valid = replace(
        unavailable,
        metadata={
            "sprint_plan": {
                "selected": [{"work_item_id": 1}],
                "deferred": [],
                "point_capacity": "3",
                "selected_story_points": "2",
            }
        },
    )
    assert _format_sprint_plan_summary(valid) == (
        "plan 1 selected / 2pt against 3pt capacity; 0 deferred"
    )


def test_communication_policy_invalid_shapes_fail_closed() -> None:
    with pytest.raises(StateError, match="must be a mapping"):
        communication_budget_policy_from_edition(
            {"communication_budget": []}, default_cooldown_days=14,
        )
    with pytest.raises(StateError, match="max_asks"):
        communication_budget_policy_from_edition(
            {"communication_budget": {"max_asks": True}},
            default_cooldown_days=14,
        )
    with pytest.raises(StateError, match="cooldown_days"):
        communication_budget_policy_from_edition(
            {"communication_budget": {"cooldown_days": -1}},
            default_cooldown_days=14,
        )


def test_context_gap_resolution_validation_covers_shape_and_timestamp() -> None:
    from src.core.context_gap_store import (
        ContextGapRecordError,
        ContextGapResolution,
    )

    with pytest.raises(ContextGapRecordError, match="resolution record"):
        ContextGapResolution.from_json({})
    with pytest.raises(ContextGapRecordError, match="timestamp"):
        ContextGapResolution.from_json({
            "resolved_at": "not-a-time",
            "program": "acme",
            "gap_fingerprint": "acme:doctor:lane:deep_context.why",
            "proposal_id": "reply-1",
            "solicitation_id": "solicitation-1",
            "proposal_digest": "a" * 64,
            "source_sha256": "b" * 64,
            "verified_principal": "reviewer",
            "closed_by": "reviewer",
            "closure_reason": "Manual edit verified.",
            "target_store": "workstream_registry",
            "target_key": "lane",
            "target_field": "deep_context.why",
            "applied_value_sha256": "c" * 64,
        })


def test_lifecycle_validation_refuses_unknown_and_naive_inputs(
    tmp_path: Path,
) -> None:
    from src.core.models_v2 import LifecycleEventKind
    from src.core.risk_register_engine import (
        _write_lifecycle_event,
        record_risk_lifecycle,
    )

    root = tmp_path / "programs"
    with pytest.raises(ValueError, match="Unknown risk"):
        record_risk_lifecycle(
            "acme",
            "missing",
            LifecycleEventKind.REVISIT,
            actor="operator",
            occurred_at=NOW,
            programs_root=root,
        )
    common = dict(
        program_id="acme",
        fact_type="risk.lifecycle",
        object_id="risk-1",
        kind=LifecycleEventKind.REVISIT,
        actor="operator",
        note=None,
        effective=None,
        next_review_at=None,
        evidence_refs=(),
        programs_root=Path(root),
    )
    with pytest.raises(ValueError, match="occurred_at"):
        _write_lifecycle_event(
            **common, occurred_at=datetime(2026, 11, 21, 12, 0)
        )
    with pytest.raises(ValueError, match="actor"):
        _write_lifecycle_event(**{**common, "actor": " "}, occurred_at=NOW)
    with pytest.raises(ValueError, match="next_review_at"):
        _write_lifecycle_event(
            **{
                **common,
                "next_review_at": datetime(2026, 11, 22, 12, 0),
            },
            occurred_at=NOW,
        )


def test_vertical_fact_parser_covers_duplicate_and_invalid_entries() -> None:
    from src.core.vertical_artifact_evaluation import _facts

    errors: list[str] = []
    assert _facts(
        {"facts": "bad"}, "candidate", "status", errors
    ) == {}
    parsed = _facts(
        {
            "facts": [
                None,
                {"fact_id": "fact-1", "value": 1},
                {"fact_id": "fact-1", "value": 2},
            ]
        },
        "candidate",
        "status",
        errors,
    )
    assert parsed["fact-1"]["value"] == 2
    assert "entries must identify fact_id" in " ".join(errors)
    assert "duplicate fact_id" in " ".join(errors)

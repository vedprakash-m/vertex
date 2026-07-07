from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from cli import app
from src.core.analytics_store import get_program_autonomy_audit_path
from src.core.claim_tracker import append_decision_ask, load_latest_claim_statuses, load_open_decision_asks
from src.core.decision_register import load_decisions, save_decisions
from src.core.incident_journal_store import append_incident_entry
from src.core.knowledge_store import KnowledgeStore
from src.core.models import Confidence
from src.core.models_v2 import DecisionAsk, DecisionEntry, DecisionStatus, IncidentEntry, PersonDirectory


runner = CliRunner()


def test_decisions_add_and_list_cli(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("USERNAME", "demo")

    add_result = runner.invoke(
        app,
        [
            "decisions",
            "add",
            "--program",
            "demo",
            "--title",
            "Choose rollout path",
            "--context",
            "Two rollout options remain.",
            "--decision",
            "Proceed with the guarded rollout.",
        ],
    )
    list_result = runner.invoke(app, ["decisions", "list", "--program", "demo"])

    assert add_result.exit_code == 0
    assert "Added decision" in add_result.stdout
    assert list_result.exit_code == 0
    assert "DECISION REGISTER" in list_result.stdout
    assert "Choose rollout path" in list_result.stdout


def test_decisions_add_persists_review_by(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("USERNAME", "demo")

    result = runner.invoke(
        app,
        [
            "decisions",
            "add",
            "--program",
            "demo",
            "--title",
            "Choose rollout path",
            "--context",
            "Two rollout options remain.",
            "--decision",
            "Proceed with the guarded rollout.",
            "--review-by",
            "2026-05-15",
        ],
    )

    decisions = load_decisions("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert decisions[0].review_by == date(2026, 5, 15)


def test_decisions_list_cli_json(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    save_decisions(
        "demo",
        (
            DecisionEntry(
                id="decision-1",
                program_id="demo",
                title="Choose rollout path",
                context="Two rollout options remain.",
                decision="Proceed with the guarded rollout.",
                rationale=None,
                alternatives_considered=("Delay rollout",),
                decided_by="demo",
                decision_date=date(2026, 5, 1),
                status=DecisionStatus.DECIDED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=("action-demo-1",),
                workstream_id="ws-demo",
                entity_refs=("WI:1001",),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["decisions", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["program_id"] == "demo"
    assert payload["decisions"][0]["id"] == "decision-1"
    assert payload["decisions"][0]["decision_date"] == "2026-05-01"


def test_decisions_list_cli_csv(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    save_decisions(
        "demo",
        (
            DecisionEntry(
                id="decision-1",
                program_id="demo",
                title="Choose rollout path",
                context="Two rollout options remain.",
                decision="Proceed with the guarded rollout.",
                rationale=None,
                alternatives_considered=("Delay rollout",),
                decided_by="demo",
                decision_date=date(2026, 5, 1),
                status=DecisionStatus.DECIDED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=("action-demo-1",),
                workstream_id="ws-demo",
                entity_refs=("WI:1001",),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["decisions", "list", "--program", "demo", "--format", "csv"])
    rows = list(csv.DictReader(result.stdout.splitlines()))

    assert result.exit_code == 0
    assert rows[0]["id"] == "decision-1"
    assert rows[0]["linked_action_ids"] == "action-demo-1"


def test_decisions_list_uses_fact_store_projection(monkeypatch) -> None:
    sentinel = object()
    observed: list[object] = []
    monkeypatch.setattr("src.commands.decisions.load_program_facts", lambda program_id, programs_root=None: sentinel)
    monkeypatch.setattr(
        "src.commands.decisions.project_decision_entries",
        lambda snapshot: observed.append(snapshot) or (
            DecisionEntry(
                id="decision-1",
                program_id="demo",
                title="Choose rollout path",
                context="Two rollout options remain.",
                decision="Proceed with the guarded rollout.",
                rationale=None,
                alternatives_considered=(),
                decided_by="demo",
                decision_date=date(2026, 5, 1),
                status=DecisionStatus.DECIDED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id=None,
                entity_refs=(),
                review_by=None,
            ),
        ),
    )

    result = runner.invoke(app, ["decisions", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert observed == [sentinel]
    assert payload["decisions"][0]["id"] == "decision-1"


def test_decisions_add_resolves_linked_decision_ask(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("USERNAME", "demo")
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=7,
            text="Need leadership call on rollout path.",
            entity_refs=("WI:1001",),
            ask_date=date(2026, 5, 1),
            owner_alias="demo",
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "decisions",
            "add",
            "--program",
            "demo",
            "--title",
            "Choose rollout path",
            "--context",
            "Two rollout options remain.",
            "--decision",
            "Proceed with the guarded rollout.",
            "--linked-claim",
            "ask-1",
        ],
    )

    statuses = load_latest_claim_statuses("demo", programs_root=programs_root)
    decisions = load_decisions("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert decisions[0].linked_claim_id == "ask-1"
    assert statuses["ask-1"].new_status == "resolved"


def test_decisions_supersede_updates_status(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    save_decisions(
        "demo",
        (
            DecisionEntry(
                id="decision-1",
                program_id="demo",
                title="Choose rollout path",
                context="Two rollout options remain.",
                decision="Proceed with the guarded rollout.",
                rationale=None,
                alternatives_considered=(),
                decided_by="demo",
                decision_date=date(2026, 5, 1),
                status=DecisionStatus.DECIDED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id=None,
                entity_refs=(),
            ),
            DecisionEntry(
                id="decision-2",
                program_id="demo",
                title="Choose revised rollout path",
                context="Guardrails changed.",
                decision="Proceed with the revised guarded rollout.",
                rationale=None,
                alternatives_considered=(),
                decided_by="demo",
                decision_date=date(2026, 5, 2),
                status=DecisionStatus.DECIDED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id=None,
                entity_refs=(),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        ["decisions", "supersede", "--program", "demo", "--id", "decision-1", "--superseded-by", "decision-2"],
    )

    decisions = {entry.id: entry for entry in load_decisions("demo", programs_root=programs_root)}

    assert result.exit_code == 0
    assert decisions["decision-1"].status is DecisionStatus.SUPERSEDED
    assert decisions["decision-1"].superseded_by == "decision-2"


def test_decisions_resolve_updates_review_by(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    save_decisions(
        "demo",
        (
            DecisionEntry(
                id="decision-1",
                program_id="demo",
                title="Choose rollout path",
                context="Two rollout options remain.",
                decision="Proceed with the guarded rollout.",
                rationale=None,
                alternatives_considered=(),
                decided_by="demo",
                decision_date=date(2026, 5, 1),
                status=DecisionStatus.PROPOSED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id=None,
                entity_refs=(),
                review_by=None,
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "decisions",
            "resolve",
            "--program",
            "demo",
            "--id",
            "decision-1",
            "--review-by",
            "2026-05-20",
        ],
    )

    decisions = load_decisions("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert decisions[0].status is DecisionStatus.DECIDED
    assert decisions[0].review_by == date(2026, 5, 20)


def test_decisions_aging_lists_open_decision_debt(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=7,
            text="Need leadership call on rollout path.",
            entity_refs=("WI:1001",),
            ask_date=date(2026, 4, 15),
            owner_alias="demo",
        ),
        programs_root=programs_root,
    )
    append_incident_entry(
        IncidentEntry(
            program_id="demo",
            incident_id="22001",
            signal_id="incident-signal-1",
            observed_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
            belief_change_summary="IcM 22001: Rollout path is blocked until leadership confirms sequencing.",
            workstream_id="repair",
            severity=2,
            ado_entity_refs=("WI:1001",),
            confidence=Confidence.HIGH,
        ),
        programs_root=programs_root,
    )
    monkeypatch.setattr(
        "src.commands.decisions.datetime",
        SimpleNamespace(
            now=lambda tz=None: datetime(2026, 5, 10, 12, 0, tzinfo=tz or timezone.utc),
            combine=datetime.combine,
            min=datetime.min,
        ),
    )

    human_result = runner.invoke(app, ["decisions", "aging", "--program", "demo"])
    json_result = runner.invoke(app, ["decisions", "aging", "--program", "demo", "--format", "json"])
    csv_result = runner.invoke(app, ["decisions", "aging", "--program", "demo", "--format", "csv"])

    assert human_result.exit_code == 0
    assert "DECISION DEBT - demo (1)" in human_result.stdout
    assert "ask-1 | escalate |" in human_result.stdout
    assert "Incident-linked: WI:1001: Rollout path is blocked until leadership confirms sequencing. Source: IcM 22001." in human_result.stdout
    assert "Approve: vertex escalate --edition demo_weekly --decision-ask ask-1 --dry-run" in human_result.stdout

    payload = json.loads(json_result.stdout)
    assert json_result.exit_code == 0
    assert payload["decision_debt"][0]["id"] == "ask-1"
    assert payload["decision_debt"][0]["entity_refs"] == ["WI:1001"]


def test_decisions_aging_keeps_repeated_incident_learning_summary_with_shared_synthesizer(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=7,
            text="Need leadership call on rollout path.",
            entity_refs=("WI:1001",),
            ask_date=date(2026, 4, 15),
            owner_alias="demo",
        ),
        programs_root=programs_root,
    )
    for incident_id, signal_id, summary in (
        ("22001", "incident-signal-1", "IcM 22001: WI:1001 rollout validation regressed under failover."),
        ("22002", "incident-signal-2", "IcM 22002: WI:1001 rollout validation regressed under failover again."),
    ):
        append_incident_entry(
            IncidentEntry(
                program_id="demo",
                incident_id=incident_id,
                signal_id=signal_id,
                observed_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
                belief_change_summary=summary,
                workstream_id="repair",
                severity=2,
                ado_entity_refs=("WI:1001",),
                confidence=Confidence.HIGH,
            ),
            programs_root=programs_root,
        )
    monkeypatch.setattr(
        "src.commands.decisions.datetime",
        SimpleNamespace(
            now=lambda tz=None: datetime(2026, 5, 10, 12, 0, tzinfo=tz or timezone.utc),
            combine=datetime.combine,
            min=datetime.min,
        ),
    )

    human_result = runner.invoke(app, ["decisions", "aging", "--program", "demo"])
    json_result = runner.invoke(app, ["decisions", "aging", "--program", "demo", "--format", "json"])
    csv_result = runner.invoke(app, ["decisions", "aging", "--program", "demo", "--format", "csv"])

    assert human_result.exit_code == 0
    assert "repeated across 2 incident learnings" in human_result.stdout
    assert "high confidence" in human_result.stdout
    payload = json.loads(json_result.stdout)
    assert json_result.exit_code == 0
    assert payload["decision_debt"][0]["incident_refs"] == ["IcM 22001", "IcM 22002"]
    assert payload["decision_debt"][0]["incident_summary"] == (
        "WI:1001: repeated across 2 incident learnings. "
        "WI:1001 rollout validation regressed under failover; WI:1001 rollout validation regressed under failover again. "
        "Source: IcM 22001, IcM 22002. (high confidence)"
    )
    assert payload["decision_debt"][0]["inactive_days"] >= 21
    assert payload["decision_debt"][0]["lifecycle_stage"] == "escalate"
    assert payload["decision_debt"][0]["command"] == "vertex escalate --edition demo_weekly --decision-ask ask-1 --dry-run"

    rows = list(csv.DictReader(csv_result.stdout.splitlines()))
    assert csv_result.exit_code == 0
    assert rows[0]["id"] == "ask-1"
    assert rows[0]["entity_refs"] == "WI:1001"
    assert rows[0]["incident_refs"] == "IcM 22001|IcM 22002"
    assert rows[0]["incident_summary"] == (
        "WI:1001: repeated across 2 incident learnings. "
        "WI:1001 rollout validation regressed under failover; WI:1001 rollout validation regressed under failover again. "
        "Source: IcM 22001, IcM 22002. (high confidence)"
    )
    assert int(rows[0]["inactive_days"]) >= 21
    assert rows[0]["lifecycle_stage"] == "escalate"


def test_decisions_nudge_cli_previews_decision_ask_email(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=7,
            text="Need leadership call on rollout path.",
            entity_refs=("WI:1001",),
            ask_date=date(2026, 5, 1),
            owner_alias="demo",
        ),
        programs_root=programs_root,
    )
    monkeypatch.setattr(
        "src.commands.notify.load_bundle",
        lambda edition_name, reports_root="_", **_kw: SimpleNamespace(
            config=SimpleNamespace(author=SimpleNamespace(email="author@example.com")),
            program_context=SimpleNamespace(workstreams=(), leadership_readers=()),
        ),
    )
    monkeypatch.setattr(
        "src.commands.notify.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(PersonDirectory(alias="demo", email="demo@example.com", display_name="Demo Owner"),),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    result = runner.invoke(app, ["decisions", "nudge", "--program", "demo", "--id", "ask-1", "--dry-run"])

    assert result.exit_code == 0
    assert "NOTIFY PREVIEW" in result.stdout
    assert "To: demo@example.com" in result.stdout
    assert "Follow-up needed on decision ask ask-1" in result.stdout
    assert "Dry run: no nudge draft written." in result.stdout


def test_decisions_nudge_cli_writes_eml_with_leadership_fallback(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.notify.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.decisions.resolve_edition",
        lambda edition_id, programs_root: SimpleNamespace(
            workstreams=(
                SimpleNamespace(id="repair", area_paths=("Area\\Demo\\Repair",)),
            )
        ),
    )
    monkeypatch.setattr(
        "src.commands.decisions.load_confirmed_issue_snapshot",
        lambda edition_id, issue_number: SimpleNamespace(
            items=(
                SimpleNamespace(id=2002, area_path="Area\\Demo\\Repair"),
            )
        ),
    )
    class _FixedDateTime:
        @staticmethod
        def now(tz=None):
            value = date(2026, 5, 20)
            from datetime import datetime as _Datetime

            return _Datetime(2026, 5, 20, 9, 0, tzinfo=tz)

    monkeypatch.setattr("src.commands.decisions.datetime", _FixedDateTime)
    append_decision_ask(
        DecisionAsk(
            id="ask-2",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=8,
            text="Need rollout gate decision.",
            entity_refs=("WI:2002",),
            ask_date=date(2026, 5, 1),
            owner_alias="missing-owner",
        ),
        programs_root=programs_root,
    )
    monkeypatch.setattr(
        "src.commands.notify.load_bundle",
        lambda edition_name, reports_root="_", **_kw: SimpleNamespace(
            config=SimpleNamespace(author=SimpleNamespace(email="author@example.com")),
            program_context=SimpleNamespace(
                workstreams=(),
                leadership_readers=(SimpleNamespace(name="ltlead"),),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.commands.notify.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(PersonDirectory(alias="ltlead", email="ltlead@example.com", display_name="LT Lead"),),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    result = runner.invoke(app, ["decisions", "nudge", "--program", "demo", "--id", "ask-2"], input="y\n")
    eml_paths = sorted((programs_root / "demo_weekly" / "publications" / "demo_weekly" / "decision_ask_nudges").glob("*.eml"))

    assert result.exit_code == 0
    assert len(eml_paths) == 1
    assert "EML:" in result.stdout
    assert "Wrote 1 decision-ask nudge draft EML(s). Send manually via Outlook." in result.stdout
    eml_text = eml_paths[0].read_text(encoding="utf-8")
    assert "ltlead@example.com" in eml_text
    assert "Need rollout gate decision." in eml_text
    assert load_open_decision_asks("demo", programs_root=programs_root)[0].last_touched_at is not None
    audit_payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("demo", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_payloads[-1]["action_type"] == "decision_ask_nudge"
    assert audit_payloads[-1]["accepted"] is True
    assert audit_payloads[-1]["policy_rule"] == "decision_ask_nudge"
    assert audit_payloads[-1]["evidence_refs"] == ["WI:2002", "workstream:repair", "decision_ask:ask-2"]


def test_decisions_nudge_cli_records_declined_review_without_writing_eml(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("USERNAME", "demo-user")
    append_decision_ask(
        DecisionAsk(
            id="ask-3",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=9,
            text="Need leadership call on resilience scope.",
            entity_refs=("WI:3003",),
            ask_date=date(2026, 5, 1),
            owner_alias="demo",
        ),
        programs_root=programs_root,
    )
    monkeypatch.setattr(
        "src.commands.notify.load_bundle",
        lambda edition_name, reports_root="_", **_kw: SimpleNamespace(
            config=SimpleNamespace(author=SimpleNamespace(email="author@example.com")),
            program_context=SimpleNamespace(workstreams=(), leadership_readers=()),
        ),
    )
    monkeypatch.setattr(
        "src.commands.notify.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(PersonDirectory(alias="demo", email="demo@example.com", display_name="Demo Owner"),),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    result = runner.invoke(app, ["decisions", "nudge", "--program", "demo", "--id", "ask-3"], input="n\n")
    eml_paths = sorted((programs_root / "demo_weekly" / "publications" / "demo_weekly" / "decision_ask_nudges").glob("*.eml"))

    assert result.exit_code == 1
    assert eml_paths == []
    assert load_open_decision_asks("demo", programs_root=programs_root)[0].last_touched_at is None
    audit_payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("demo", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_payloads[-1]["action_type"] == "decision_ask_nudge"
    assert audit_payloads[-1]["accepted"] is False
    assert audit_payloads[-1]["author_alias"] == "demo-user"
    assert audit_payloads[-1]["rollback_mechanism"] == "No rollback needed; nudge draft was not written."
    assert audit_payloads[-1]["subject_alias"] == "demo"


def test_decisions_aging_apply_dry_run_previews_due_followups(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.decisions.datetime",
        SimpleNamespace(
            now=lambda tz=None: datetime(2026, 5, 20, 12, 0, tzinfo=tz or timezone.utc),
            combine=datetime.combine,
            min=datetime.min,
        ),
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-nudge",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=7,
            text="Need leadership call on rollout path.",
            entity_refs=("WI:1001",),
            ask_date=date(2026, 5, 1),
            owner_alias="demo",
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-escalate",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=8,
            text="Need escalation on dependency cutoff.",
            entity_refs=("WI:2002",),
            ask_date=date(2026, 4, 20),
            owner_alias="demo",
        ),
        programs_root=programs_root,
    )
    monkeypatch.setattr(
        "src.commands.notify.load_bundle",
        lambda edition_name, reports_root="_", **_kw: SimpleNamespace(
            config=SimpleNamespace(author=SimpleNamespace(email="author@example.com")),
            program_context=SimpleNamespace(workstreams=(), leadership_readers=()),
        ),
    )
    monkeypatch.setattr(
        "src.commands.notify.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(PersonDirectory(alias="demo", email="demo@example.com", display_name="Demo Owner"),),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )
    monkeypatch.setattr(
        "src.commands.decisions.plan_decision_ask_escalation",
        lambda edition_name, decision_ask_id: SimpleNamespace(
            edition_name=edition_name,
            decision_ask_id=decision_ask_id,
            artifacts=SimpleNamespace(previews=("preview",)),
        ),
    )
    monkeypatch.setattr(
        "src.commands.decisions.render_escalation_preview_plaintext",
        lambda artifacts: "ESCALATION PREVIEW",
    )

    result = runner.invoke(app, ["decisions", "aging", "--program", "demo", "--apply", "--dry-run"])

    assert result.exit_code == 0
    assert "DECISION DEBT - demo (2)" in result.stdout
    assert "NOTIFY PREVIEW" in result.stdout
    assert "ESCALATION PREVIEW" in result.stdout
    assert "Dry run: would write 2 decision-ask follow-up draft(s)." in result.stdout


def test_decisions_aging_apply_routes_nudges_and_escalations(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.decisions.datetime",
        SimpleNamespace(
            now=lambda tz=None: datetime(2026, 5, 20, 12, 0, tzinfo=tz or timezone.utc),
            combine=datetime.combine,
            min=datetime.min,
        ),
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-nudge",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=7,
            text="Need leadership call on rollout path.",
            entity_refs=("WI:1001",),
            ask_date=date(2026, 5, 1),
            owner_alias="demo",
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-escalate",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=8,
            text="Need escalation on dependency cutoff.",
            entity_refs=("WI:2002",),
            ask_date=date(2026, 4, 20),
            owner_alias="demo",
        ),
        programs_root=programs_root,
    )
    nudge_calls: list[str] = []
    escalation_calls: list[str] = []
    monkeypatch.setattr(
        "src.commands.decisions.plan_decision_ask_nudge",
        lambda program_id, decision_ask_id, programs_root, context_note=None: SimpleNamespace(
            ask=SimpleNamespace(id=decision_ask_id),
            preview=SimpleNamespace(to=("demo@example.com",)),
            context_note=context_note,
        ),
    )
    monkeypatch.setattr(
        "src.commands.decisions.apply_decision_ask_nudge",
        lambda plan, *, programs_root, generated_at=None, **_kw: nudge_calls.append(plan.ask.id)
        or (programs_root / "demo_weekly" / "publications" / "demo_weekly" / "decision_ask_nudges" / f"{plan.ask.id}.eml",),
    )
    monkeypatch.setattr(
        "src.commands.decisions.plan_decision_ask_escalation",
        lambda edition_name, decision_ask_id: SimpleNamespace(
            edition_name=edition_name,
            decision_ask_id=decision_ask_id,
        ),
    )
    monkeypatch.setattr(
        "src.commands.decisions.apply_decision_ask_escalation",
        lambda plan, *, generated_at=None, **_kw: escalation_calls.append(plan.decision_ask_id)
        or SimpleNamespace(eml_paths=(Path(f"/tmp/{plan.decision_ask_id}.eml"),)),
    )

    result = runner.invoke(app, ["decisions", "aging", "--program", "demo", "--apply"], input="y\n")

    assert result.exit_code == 0
    assert nudge_calls == ["ask-nudge"]
    assert escalation_calls == ["ask-escalate"]
    assert "EML:" in result.stdout
    assert "Wrote 2 decision-ask follow-up draft(s)." in result.stdout


# ---------------------------------------------------------------------------
# WI-3.11: vertex decisions link-outcome
# ---------------------------------------------------------------------------

def _seed_decision_and_get_natural_key(programs_root: Path) -> str:
    """Seed one decision, return its fact natural_key for use in link-outcome."""
    from src.core.decision_register import load_decisions
    from src.core.program_fact_store import ProgramFactStore, load_program_facts

    entry = DecisionEntry(
        id="decision-link-1",
        program_id="demo",
        title="Ship the feature",
        context="Must decide go/no-go.",
        decision="Proceed with the launch.",
        rationale=None,
        alternatives_considered=(),
        decided_by="demo",
        decision_date=date(2026, 5, 1),
        status=DecisionStatus.DECIDED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id=None,
        entity_refs=(),
    )
    save_decisions("demo", (entry,), programs_root=programs_root)

    # Load back to get the natural_key from the fact store
    db_root = programs_root.parent
    snapshot = load_program_facts("demo", programs_root=programs_root, db_root=db_root)
    for fact in snapshot.facts:
        if fact.fact_type == "decision.entry":
            return fact.natural_key
    raise AssertionError("Decision fact not found after save_decisions")


def test_link_outcome_adds_expected_outcome_refs(monkeypatch, tmp_path: Path) -> None:
    """link-outcome stores assumption natural key in expected_outcome_refs."""
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    natural_key = _seed_decision_and_get_natural_key(programs_root)

    result = runner.invoke(
        app,
        [
            "decisions",
            "link-outcome",
            "--program",
            "demo",
            "--decision-id",
            natural_key,
            "--assumption",
            "assumption:nk-feature-ready",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "assumption:nk-feature-ready" in result.stdout
    assert "expected_outcome_refs now:" in result.stdout


def test_link_outcome_appends_to_existing_refs(monkeypatch, tmp_path: Path) -> None:
    """Calling link-outcome twice appends a second assumption key."""
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    natural_key = _seed_decision_and_get_natural_key(programs_root)

    runner.invoke(
        app,
        [
            "decisions",
            "link-outcome",
            "--program",
            "demo",
            "--decision-id",
            natural_key,
            "--assumption",
            "assumption:nk-first",
            "--programs-root",
            str(programs_root),
        ],
    )
    result2 = runner.invoke(
        app,
        [
            "decisions",
            "link-outcome",
            "--program",
            "demo",
            "--decision-id",
            natural_key,
            "--assumption",
            "assumption:nk-second",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result2.exit_code == 0, result2.stdout
    assert "assumption:nk-first" in result2.stdout
    assert "assumption:nk-second" in result2.stdout


def test_link_outcome_missing_decision_fails(monkeypatch, tmp_path: Path) -> None:
    """link-outcome with an unknown decision id should fail with exit_code 1."""
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    # Don't seed any decisions → empty fact store
    (programs_root / "demo").mkdir(parents=True, exist_ok=True)

    result = runner.invoke(
        app,
        [
            "decisions",
            "link-outcome",
            "--program",
            "demo",
            "--decision-id",
            "DOES-NOT-EXIST",
            "--assumption",
            "assumption:nk-whatever",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code != 0


def test_link_outcome_persisted_to_fact_store(monkeypatch, tmp_path: Path) -> None:
    """expected_outcome_refs persists in the fact store payload after link-outcome."""
    from src.core.program_fact_store import load_program_facts

    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    natural_key = _seed_decision_and_get_natural_key(programs_root)

    result = runner.invoke(
        app,
        [
            "decisions",
            "link-outcome",
            "--program",
            "demo",
            "--decision-id",
            natural_key,
            "--assumption",
            "assumption:nk-roundtrip",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert result.exit_code == 0, result.stdout

    # Reload from fact store to verify persistence
    db_root = programs_root.parent
    snapshot = load_program_facts("demo", programs_root=programs_root, db_root=db_root)
    linked_facts = [
        f for f in snapshot.facts
        if f.fact_type == "decision.entry" and f.natural_key == natural_key
    ]
    assert linked_facts, "Decision fact must exist after link-outcome"
    refs = linked_facts[-1].payload.get("expected_outcome_refs") or []
    assert "assumption:nk-roundtrip" in refs

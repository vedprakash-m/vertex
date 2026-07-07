from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.commands import signals
from src.core.feedback.salience_modeler import read_salience_events
from src.core.ai_proposal_store import append_ai_proposal, build_ai_proposal_id
from src.core.journal import append_signal, read_review_log, read_signal_thread_log, read_signals
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import AIProposal, AIProposalStatus, Signal, SignalClass, SignalReviewDecision, WorkstreamSynthesis
from src.core.sqlite_stores import SQLiteSignalStore, get_program_sqlite_store_path


runner = CliRunner()


def _seed_program_dir(programs_root: Path, program_id: str, *, storage_backend: str | None = None) -> Path:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    if storage_backend is not None:
        (program_dir / "program.yaml").write_text(
            "\n".join(
                (
                    "schema_version: '2.0'",
                    f"id: {program_id}",
                    f"name: {program_id.upper()}",
                    f"storage_backend: {storage_backend}",
                    "",
                )
            ),
            encoding="utf-8",
        )
    return program_dir


def test_signals_add_appends_auto_approved_manual_signal(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(signals, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("USERNAME", "manual.author")
    _seed_program_dir(programs_root, "acme")

    result = runner.invoke(
        app,
        [
            "signals",
            "add",
            "--program",
            "acme",
            "--workstream",
            "storage",
            "--ref",
            "WI:1234",
            "Manual checkpoint from partner review.",
        ],
    )
    journal_signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)
    pending_result = runner.invoke(app, ["signals", "--program", "acme"])

    assert result.exit_code == 0
    assert "Added manual signal" in result.output
    assert len(journal_signals) == 1
    assert journal_signals[0].source == "manual"
    assert journal_signals[0].program_id == "acme"
    assert journal_signals[0].workstream_id == "storage"
    assert journal_signals[0].entity_refs == ("WI:1234",)
    assert journal_signals[0].text == "Manual checkpoint from partner review."
    assert journal_signals[0].confidence == Confidence.HIGH
    assert journal_signals[0].metadata is not None
    assert journal_signals[0].metadata["author"] == "manual.author"
    assert journal_signals[0].metadata["signal_class"] == SignalClass.STATUS.value
    assert len(reviews) == 1
    assert isinstance(reviews[0], SignalReviewDecision)
    assert reviews[0].decision == "approved"
    assert reviews[0].reviewed_by == "manual.author"
    assert pending_result.exit_code == 0
    assert "No pending signals for acme." in pending_result.output


def test_signals_add_dedupes_matching_manual_signal_within_week(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(signals, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("USERNAME", "manual.author")
    _seed_program_dir(programs_root, "acme")

    first = runner.invoke(app, ["signals", "add", "--program", "acme", "Duplicate manual signal."])
    second = runner.invoke(app, ["signals", "add", "--program", "acme", "Duplicate manual signal."])
    journal_signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "matching manual signal already exists this week" in second.output
    assert len(journal_signals) == 1
    assert len(reviews) == 1


def test_signals_add_uses_sqlite_backend_when_program_configured(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(signals, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("USERNAME", "manual.author")
    _seed_program_dir(programs_root, "acme", storage_backend="sqlite")

    result = runner.invoke(
        app,
        [
            "signals",
            "add",
            "--program",
            "acme",
            "--workstream",
            "storage",
            "SQLite-backed manual signal.",
        ],
    )

    signal_store = SQLiteSignalStore(programs_root=programs_root)

    assert result.exit_code == 0
    assert get_program_sqlite_store_path("acme", programs_root=programs_root).exists()
    assert len(signal_store.read("acme")) == 1
    assert len(signal_store.read_reviews("acme")) == 1
    assert read_signals("acme", programs_root=programs_root) == ()


def test_signals_review_updates_sidecar_without_mutating_journal(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(signals, "PROGRAMS_ROOT", programs_root)
    _seed_program_dir(programs_root, "acme")

    signal = Signal(
        id="sig-001",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="Checkpoint review notes indicate the ETA is still soft.",
        raw_ref="msg:AAMk123",
        confidence=Confidence.MEDIUM,
        metadata={"message_id": "AAMk123", "source_type": "email"},
    )
    partition_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    journal_path = append_signal(signal, programs_root=programs_root, partition_at=partition_at)
    before = journal_path.read_text(encoding="utf-8")

    list_result = runner.invoke(app, ["signals", "--program", "acme"])
    review_result = runner.invoke(app, ["signals", "review", "--program", "acme", "--reviewer", "maintainer"], input="a\n")
    after = journal_path.read_text(encoding="utf-8")
    reviews = read_review_log("acme", programs_root=programs_root)
    post_list = runner.invoke(app, ["signals", "--program", "acme"])

    assert list_result.exit_code == 0
    assert "PENDING SIGNALS — acme (1)" in list_result.output
    assert "sig-001" in list_result.output
    assert review_result.exit_code == 0
    assert "sig-001 | 2026-05-08T12:00:00+00:00 | workiq/email | acme | confidence medium" in review_result.output
    assert "Reviewed 1 signal(s) for acme." in review_result.output
    assert before == after
    assert len(reviews) == 1
    assert isinstance(reviews[0], SignalReviewDecision)
    assert reviews[0].decision == "approved"
    assert post_list.exit_code == 0
    assert "No pending signals for acme." in post_list.output


def test_signals_review_logs_salience_event_for_dismissed_catchup_signal(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(signals, "PROGRAMS_ROOT", programs_root)
    _seed_program_dir(programs_root, "acme")

    signal = Signal(
        id="sig-catchup-001",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="vertex/catchup",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=("WI:1234",),
        text="Catchup flagged likely schedule drift for the rollout workstream.",
        raw_ref="catchup:123",
        confidence=Confidence.MEDIUM,
        metadata={"catchup_origin": "workiq/email"},
    )
    append_signal(signal, programs_root=programs_root)

    review_result = runner.invoke(
        app,
        ["signals", "review", "--program", "acme", "--reviewer", "maintainer"],
        input="d\nnoise\n",
    )
    events = read_salience_events("acme", programs_root=programs_root)

    assert review_result.exit_code == 0
    assert len(events) == 1
    assert events[0].anomaly_id == "sig-catchup-001"
    assert events[0].action == "dismissed"
    assert events[0].workstream_id == "deployment"
    assert events[0].work_item_id == 1234
    assert events[0].weight_before is not None
    assert events[0].weight_after is not None


def test_signals_review_logs_salience_event_for_approved_catchup_signal(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(signals, "PROGRAMS_ROOT", programs_root)
    _seed_program_dir(programs_root, "acme")

    signal = Signal(
        id="sig-catchup-approve-001",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="vertex/catchup",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=("WI:5678",),
        text="Catchup found evidence that rollout risk needs follow-through.",
        raw_ref="catchup:5678",
        confidence=Confidence.MEDIUM,
        metadata={"catchup_origin": "workiq/email"},
    )
    append_signal(signal, programs_root=programs_root)

    review_result = runner.invoke(
        app,
        ["signals", "review", "--program", "acme", "--reviewer", "maintainer"],
        input="a\n",
    )
    events = read_salience_events("acme", programs_root=programs_root)

    assert review_result.exit_code == 0
    assert len(events) == 1
    assert events[0].anomaly_id == "sig-catchup-approve-001"
    assert events[0].action == "acted"
    assert events[0].workstream_id == "deployment"
    assert events[0].work_item_id == 5678
    assert events[0].weight_before is not None
    assert events[0].weight_after is not None


def test_signals_link_records_threading_without_mutating_journal(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(signals, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("USERNAME", "thread.author")
    _seed_program_dir(programs_root, "acme")

    first = Signal(
        id="sig-001",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="Partner thread says rollout risk is rising.",
        raw_ref="msg:AAMk123",
        confidence=Confidence.MEDIUM,
        metadata={"message_id": "AAMk123", "source_type": "email"},
    )
    second = Signal(
        id="sig-002",
        timestamp=datetime(2026, 5, 8, 13, 0, tzinfo=timezone.utc),
        source="manual",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="Manual follow-up confirmed the same concern.",
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata={"author": "thread.author"},
    )
    partition_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    journal_path = append_signal(first, programs_root=programs_root, partition_at=partition_at)
    append_signal(second, programs_root=programs_root, partition_at=partition_at)
    before = journal_path.read_text(encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "signals",
            "link",
            "--signal",
            "sig-001",
            "--signal",
            "sig-002",
            "--thread",
            "rollout-risk",
        ],
    )

    after = journal_path.read_text(encoding="utf-8")
    linked_signals = read_signals("acme", programs_root=programs_root)
    thread_links = read_signal_thread_log("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert "Linked 2 signal(s) in acme under thread 'rollout-risk'." in result.output
    assert before == after
    assert len(thread_links) == 2
    assert all(entry.thread_id == "rollout-risk" for entry in thread_links)
    assert all(signal.thread_id == "rollout-risk" for signal in linked_signals)


def test_signals_list_includes_pending_ai_proposals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(signals, "PROGRAMS_ROOT", programs_root)
    _seed_program_dir(programs_root, "acme")
    _append_pending_ai_proposal(programs_root, program_id="acme", workstream_id="storage")

    result = runner.invoke(app, ["signals", "--program", "acme"])

    assert result.exit_code == 0
    assert "PENDING AI PROPOSALS — acme (1)" in result.output
    assert "risk high | confidence medium" in result.output


def test_signals_review_mentions_pending_ai_proposals_when_no_signal_queue(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(signals, "PROGRAMS_ROOT", programs_root)
    _seed_program_dir(programs_root, "acme")
    _append_pending_ai_proposal(programs_root, program_id="acme", workstream_id="storage")

    result = runner.invoke(app, ["signals", "review", "--program", "acme", "--reviewer", "operator"])

    assert result.exit_code == 0
    assert "PENDING AI PROPOSALS — acme (1)" in result.output
    assert "vertex override --edition <edition>" in result.output
    assert "Reviewed 0 signal(s) for acme." in result.output


def test_signals_cli_supports_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(signals, "PROGRAMS_ROOT", programs_root)
    _seed_program_dir(programs_root, "acme")
    append_signal(
        Signal(
            id="sig-001",
            timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1234",),
            text="Checkpoint review notes indicate the ETA is still soft.",
            raw_ref="msg:AAMk123",
            confidence=Confidence.MEDIUM,
            metadata={"message_id": "AAMk123", "source_type": "email"},
        ),
        programs_root=programs_root,
        partition_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )
    _append_pending_ai_proposal(programs_root, program_id="acme", workstream_id="storage")

    human_result = runner.invoke(app, ["signals", "--program", "acme"])
    json_result = runner.invoke(app, ["signals", "--program", "acme", "--format", "json"])
    csv_result = runner.invoke(app, ["signals", "--program", "acme", "--format", "csv"])

    assert human_result.exit_code == 0
    assert "sig-001 | 2026-05-08T12:00:00+00:00 | workiq/email | acme | unreviewed | confidence medium" in human_result.stdout

    assert json_result.exit_code == 0
    assert '"program_id": "acme"' in json_result.stdout
    assert '"pending_signals": [' in json_result.stdout
    assert '"pending_ai_proposals": [' in json_result.stdout
    assert '"review_state": "unreviewed"' in json_result.stdout
    assert '"proposed_risk": "high"' in json_result.stdout

    assert csv_result.exit_code == 0
    assert "entry_type,id,program_id,timestamp,source,workstream_id,review_state,confidence,status,created_at,proposed_risk,text,overall_assessment,entity_refs,evidence_refs" in csv_result.stdout
    assert "signal,sig-001,acme,2026-05-08T12:00:00+00:00,workiq/email,acme,unreviewed,medium,,," in csv_result.stdout
    assert "Checkpoint review notes indicate the ETA is still soft." in csv_result.stdout
    assert "ai_proposal," in csv_result.stdout
    assert ",storage,,medium,pending,2026-05-10T09:00:00+00:00,high," in csv_result.stdout
    assert "Storage remains the gating lane until partner alignment is closed." in csv_result.stdout


def test_signals_list_orders_pending_signals_by_source_confidence_and_workiq_relevance(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(signals, "PROGRAMS_ROOT", programs_root)
    _seed_program_dir(programs_root, "acme")
    _write_people_directory(
        tmp_path / "knowledge",
        """
people:
  - alias: gm
    title: General Manager
  - alias: alex
    title: Senior Software Engineer
""".strip()
        + "\n",
    )

    pending_signals = (
        Signal(
            id="sig-workiq-low",
            timestamp=datetime(2026, 5, 10, 11, 30, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1234",),
            text="Alex asked for a status refresh.",
            raw_ref="msg:low",
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "alex", "thread_id": "thread-low"},
            thread_id="thread-low",
        ),
        Signal(
            id="sig-workiq-high-older",
            timestamp=datetime(2026, 5, 10, 10, 30, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1234",),
            text="GM asked for blocker confirmation.",
            raw_ref="msg:high-1",
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "gm", "thread_id": "thread-high"},
            thread_id="thread-high",
        ),
        Signal(
            id="sig-ado",
            timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1234",),
            text="ADO shows the target-date slip.",
            raw_ref="wi:1234",
            confidence=Confidence.HIGH,
        ),
        Signal(
            id="sig-workiq-high-newer",
            timestamp=datetime(2026, 5, 10, 10, 45, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1234",),
            text="GM reiterated the blocker in the same thread.",
            raw_ref="msg:high-2",
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "gm", "thread_id": "thread-high"},
            thread_id="thread-high",
        ),
    )
    for signal in pending_signals:
        append_signal(signal, programs_root=programs_root, partition_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc))

    result = runner.invoke(app, ["signals", "--program", "acme"])

    assert result.exit_code == 0
    assert "sig-ado | 2026-05-10T10:00:00+00:00 | ado/revision | acme | unreviewed | confidence high" in result.output
    assert "sig-workiq-high-newer | 2026-05-10T10:45:00+00:00 | workiq/email | acme | unreviewed | confidence medium" in result.output
    assert result.output.index("sig-ado") < result.output.index("sig-workiq-high-newer")
    assert result.output.index("sig-workiq-high-newer") < result.output.index("sig-workiq-high-older")
    assert result.output.index("sig-workiq-high-older") < result.output.index("sig-workiq-low")


def test_signals_list_honors_program_source_confidence_order(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(signals, "PROGRAMS_ROOT", programs_root)
    _seed_program_dir(programs_root, "acme")
    program_path = programs_root / "acme" / "program.yaml"
    program_path.write_text(
        """
schema_version: "2.0"
id: acme
name: Acme
source_confidence_order:
  - workiq
  - ado
""".strip()
        + "\n",
        encoding="utf-8",
    )
    _write_people_directory(
        tmp_path / "knowledge",
        """
people:
  - alias: gm
    title: General Manager
""".strip()
        + "\n",
    )

    for signal in (
        Signal(
            id="sig-workiq",
            timestamp=datetime(2026, 5, 10, 10, 30, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1234",),
            text="GM asked for blocker confirmation.",
            raw_ref="msg:high-1",
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "gm", "thread_id": "thread-high"},
            thread_id="thread-high",
        ),
        Signal(
            id="sig-ado",
            timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1234",),
            text="ADO shows the target-date slip.",
            raw_ref="wi:1234",
            confidence=Confidence.HIGH,
        ),
    ):
        append_signal(signal, programs_root=programs_root, partition_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc))

    result = runner.invoke(app, ["signals", "--program", "acme"])

    assert result.exit_code == 0
    assert result.output.index("sig-workiq") < result.output.index("sig-ado")


def _append_pending_ai_proposal(programs_root: Path, *, program_id: str, workstream_id: str) -> AIProposal:
    created_at = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc)
    proposal = AIProposal(
        id=build_ai_proposal_id(program_id, workstream_id=workstream_id, created_at=created_at),
        workstream_id=workstream_id,
        synthesis=WorkstreamSynthesis(
            workstream_id=workstream_id,
            overall_assessment="Storage remains the gating lane until partner alignment is closed.",
            proposed_risk=RiskLevel.HIGH,
            confidence=Confidence.MEDIUM,
            key_findings=("Partner dependency is still unresolved.",),
            evidence_refs=("sig-1",),
            open_questions=("Who owns the unblock path?",),
            recommended_actions=("Lock owner and next checkpoint.",),
        ),
        status=AIProposalStatus.PENDING,
        created_at=created_at,
        resolved_at=None,
        resolved_by=None,
    )
    append_ai_proposal(program_id, proposal, programs_root=programs_root)
    return proposal


def _write_people_directory(knowledge_root: Path, content: str) -> None:
    knowledge_root.mkdir(parents=True, exist_ok=True)
    (knowledge_root / "people_directory.yaml").write_text(content, encoding="utf-8")
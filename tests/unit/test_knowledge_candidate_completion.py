from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.ai.discovery.kb_claim_extractor import extract_claim_candidates_from_markdown
from src.commands.doctor_checks.kb_checks import knowledge_candidate_latency_check
from src.core.config_loader import load_review_config
from src.core.knowledge.predicate_registry import (
    KnowledgeClass,
    all_knowledge_classes,
    all_predicates,
    knowledge_class_for_predicate,
    validate_predicate_value,
)
from src.core.knowledge_candidate_store import (
    KnowledgeCandidateDecisionRecord,
    KnowledgeCandidateEntityResolution,
    append_candidate,
    append_triage_decision,
    build_candidate,
    candidate_knowledge_class,
    candidate_lifecycle_summary,
    candidate_payload_sha256,
    latest_candidate_review,
    load_pending_candidates,
    load_triage_decisions,
)
from src.core.knowledge_claim_store import append_claim_revision, summarize_knowledge_status
from src.core.knowledge_index import load_live_vault_hashes
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.ledger.event_log import ConfidenceTier
from src.core.ledger.source_refs import KnowledgeDocumentRef, OperatorAssertionRef
from src.core.operator_identity import OperatorIdentity

runner = CliRunner()
REVIEWER = OperatorIdentity(
    actor="reviewer",
    principal=r"SYNTHETIC\knowledge-reviewer",
    machine="synthetic-host",
    session="synthetic-session",
)


def _candidate(*, candidate_id: str = "cand-1", requires_authorized_review: bool = True):
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return build_candidate(
        candidate_id=candidate_id,
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=now,
        valid_until=None,
        proposed_confidence=ConfidenceTier.AI_EXTRACTED,
        source_ref=OperatorAssertionRef(asserted_by="extractor", asserted_at=now),
        pipeline="kb_extract",
        extraction_confidence=0.95,
        entity_resolution=(
            KnowledgeCandidateEntityResolution(
                raw_name="gen9",
                resolved_entity_id="sku_generation:gen9",
                match_kind="exact",
                score=1.0,
            ),
        ),
        corroborating_refs=(),
        batch_id="batch-1",
        created_at=now,
        requires_authorized_review=requires_authorized_review,
    )


def _write_program_review_policy(programs_root: Path) -> None:
    program = programs_root / "acme"
    program.mkdir(parents=True, exist_ok=True)
    (program / "program.yaml").write_text(
        "id: acme\nname: Synthetic\nknowledge_scopes:\n  - domain:storage-platform\n",
        encoding="utf-8",
    )
    (program / "review.yaml").write_text(
        "reviewers: []\n"
        "required: false\n"
        "knowledge_review_principals:\n"
        f"  - '{REVIEWER.principal}'\n",
        encoding="utf-8",
    )


def _invoke(arguments: list[str], *, programs_root: Path):
    return runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            *arguments,
            "--programs-root",
            str(programs_root),
        ],
    )


def test_predicate_registry_covers_all_required_knowledge_classes() -> None:
    classes = set(all_knowledge_classes())
    assert classes == set(KnowledgeClass)
    definitions = all_predicates()
    assert {definition.knowledge_class for definition in definitions} == classes
    assert len(definitions) >= len(classes)
    assert definitions[0].to_dict()["name"] == definitions[0].name
    with pytest.raises(ValueError, match="requires a boolean"):
        validate_predicate_value("is_deprecated", "true")
    with pytest.raises(ValueError, match="Unknown knowledge predicate"):
        knowledge_class_for_predicate("not_registered")


def test_review_config_preserves_explicit_knowledge_review_principals(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_review_policy(programs_root)

    settings = load_review_config(programs_root / "acme" / "review.yaml")

    assert settings.knowledge_review_principals == (REVIEWER.principal,)


def test_predicates_cli_renders_class_registry_in_text_and_json(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    text_result = runner.invoke(
        app,
        ["--no-catchup", "knowledge", "predicates"],
    )
    json_result = runner.invoke(
        app,
        ["--no-catchup", "knowledge", "predicates", "--format", "json"],
    )
    invalid_result = runner.invoke(
        app,
        ["--no-catchup", "knowledge", "predicates", "--format", "yaml"],
    )

    assert text_result.exit_code == 0
    assert "class=glossary" in text_result.output
    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout)[0]["knowledge_class"]
    assert invalid_result.exit_code == 2


def test_candidate_records_reject_mismatched_classes_and_review_flags(
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    candidate = _candidate()
    append_candidate(candidate, programs_root=programs_root)
    with pytest.raises(ValueError, match="class does not match"):
        candidate_knowledge_class(replace(candidate, knowledge_class="glossary"))
    assert candidate_lifecycle_summary(
        replace(candidate, requires_authorized_review=False),
        programs_root=programs_root,
    ).review_state.value == "not_required"
    assert latest_candidate_review("missing", programs_root=programs_root) is None

    pending_path = tmp_path / "knowledge" / "candidates" / "pending.jsonl"
    original = json.loads(pending_path.read_text(encoding="utf-8"))
    for field, invalid in (
        ("knowledge_class", "glossary"),
        ("requires_authorized_review", "yes"),
    ):
        payload = dict(original)
        payload[field] = invalid
        pending_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid"):
            load_pending_candidates(programs_root=programs_root)


def test_status_distinguishes_all_active_candidate_review_states(
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    _write_program_review_policy(programs_root)
    now = datetime.now(timezone.utc)
    for candidate_id, requires_review in (
        ("not-required", False),
        ("unreviewed", True),
        ("current", True),
        ("expired", True),
        ("invalid", True),
    ):
        append_candidate(
            _candidate(
                candidate_id=candidate_id,
                requires_authorized_review=requires_review,
            ),
            programs_root=programs_root,
        )
    for candidate_id, valid_until, machine in (
        ("current", now + timedelta(days=1), REVIEWER.machine),
        ("expired", now - timedelta(days=1), REVIEWER.machine),
        ("invalid", now + timedelta(days=1), None),
    ):
        append_triage_decision(
            KnowledgeCandidateDecisionRecord(
                candidate_id=candidate_id,
                kind="reviewed",
                decided_at=now - timedelta(hours=1),
                triage_actor=REVIEWER.actor,
                batch_id="batch-1",
                review_valid_until=valid_until,
                actor_principal=REVIEWER.principal,
                actor_machine=machine,
                actor_session=REVIEWER.session,
                candidate_sha256=candidate_payload_sha256(
                    _candidate(candidate_id=candidate_id)
                ),
                review_authority_program="acme",
                reviewed_subject="sku_generation:gen9",
            ),
            programs_root=programs_root,
        )

    summary = summarize_knowledge_status(
        knowledge_root=get_shared_knowledge_root(programs_root)
    )

    assert summary.review_required_candidate_count == 4
    assert summary.current_review_candidate_count == 1
    assert summary.unreviewed_candidate_count == 1
    assert summary.expired_review_candidate_count == 1
    assert summary.invalid_review_candidate_count == 1


def test_gated_candidate_approval_requires_current_authorized_review(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("src.commands.knowledge.capture_operator_identity", lambda *_args, **_kwargs: REVIEWER)
    programs_root = tmp_path / "programs"
    _write_program_review_policy(programs_root)
    append_candidate(_candidate(), programs_root=programs_root)

    result = _invoke(
        ["approve", "--candidate", "cand-1", "--actor", "reviewer", "--program", "acme"],
        programs_root=programs_root,
    )

    assert result.exit_code == 2
    assert "current authorized review" in result.output
    assert not (tmp_path / "knowledge" / "domains").exists()


def test_review_renewal_approval_resolution_and_history_are_retained(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("src.commands.knowledge.capture_operator_identity", lambda *_args, **_kwargs: REVIEWER)
    programs_root = tmp_path / "programs"
    _write_program_review_policy(programs_root)
    candidate = _candidate()
    append_candidate(candidate, programs_root=programs_root)
    valid_until = "2099-01-01T00:00:00+00:00"

    reviewed = _invoke(
        [
            "review",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--valid-until",
            valid_until,
            "--reason",
            "source and entity checked",
        ],
        programs_root=programs_root,
    )
    renewed = _invoke(
        [
            "renew",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--valid-until",
            "2099-02-01T00:00:00+00:00",
            "--reason",
            "review window extended",
        ],
        programs_root=programs_root,
    )
    active_list = _invoke(
        ["list", "--format", "json"],
        programs_root=programs_root,
    )
    approved = _invoke(
        ["approve", "--candidate", "cand-1", "--actor", "reviewer", "--program", "acme"],
        programs_root=programs_root,
    )
    resolved = _invoke(
        [
            "verify-resolution",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--reason",
            "claim exists and matches the approved candidate",
        ],
        programs_root=programs_root,
    )
    history = _invoke(
        ["history", "--candidate", "cand-1", "--format", "json"],
        programs_root=programs_root,
    )
    status = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert reviewed.exit_code == 0
    assert renewed.exit_code == 0
    assert active_list.exit_code == 0
    active_payload = json.loads(active_list.stdout)["candidates"][0]
    assert active_payload["knowledge_class"] == KnowledgeClass.MILESTONE_HORIZONS.value
    assert active_payload["review_state"] == "current"
    assert approved.exit_code == 0
    assert resolved.exit_code == 0
    assert history.exit_code == 0
    assert [row["kind"] for row in json.loads(history.stdout)["history"]] == [
        "reviewed",
        "review_renewed",
        "approved",
        "resolution_verified",
    ]
    assert all(
        row["candidate_sha256"] == candidate_payload_sha256(candidate)
        for row in json.loads(history.stdout)["history"]
    )
    status_payload = json.loads(status.stdout)
    assert status.exit_code == 0
    assert status_payload["registered_knowledge_class_count"] == len(KnowledgeClass)
    assert status_payload["verified_resolution_count"] == 1
    assert status_payload["triaged_candidate_count"] == 1
    assert status_payload["latest_triage_session_decision_count"] == 1
    assert status_payload["pending_candidates_by_knowledge_class"] == {}


def test_approval_and_resolution_use_the_subject_bound_at_review(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "src.commands.knowledge.capture_operator_identity",
        lambda *_args, **_kwargs: REVIEWER,
    )
    programs_root = tmp_path / "programs"
    _write_program_review_policy(programs_root)
    append_candidate(_candidate(), programs_root=programs_root)

    reviewed = _invoke(
        [
            "review",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--valid-until",
            "2099-01-01T00:00:00+00:00",
            "--reason",
            "entity checked",
        ],
        programs_root=programs_root,
    )
    monkeypatch.setattr(
        "src.commands.knowledge._effective_candidate_subject",
        lambda *_args, **_kwargs: "sku_generation:changed-after-review",
    )
    approved = _invoke(
        ["approve", "--candidate", "cand-1", "--actor", "reviewer", "--program", "acme"],
        programs_root=programs_root,
    )
    resolved = _invoke(
        [
            "verify-resolution",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--reason",
            "verify reviewed target",
        ],
        programs_root=programs_root,
    )
    claims_path = tmp_path / "knowledge" / "domains" / "storage-platform" / "claims"
    claim = json.loads(next(claims_path.glob("claims-*.jsonl")).read_text(encoding="utf-8"))

    assert reviewed.exit_code == 0
    assert approved.exit_code == 0
    assert resolved.exit_code == 0
    assert claim["subject"] == "sku_generation:gen9"


def test_revoked_review_is_invalid_in_read_consumers(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "src.commands.knowledge.capture_operator_identity",
        lambda *_args, **_kwargs: REVIEWER,
    )
    programs_root = tmp_path / "programs"
    _write_program_review_policy(programs_root)
    append_candidate(_candidate(), programs_root=programs_root)
    reviewed = _invoke(
        [
            "review",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--valid-until",
            "2099-01-01T00:00:00+00:00",
            "--reason",
            "checked",
        ],
        programs_root=programs_root,
    )
    (programs_root / "acme" / "review.yaml").write_text(
        "reviewers: []\nrequired: false\nknowledge_review_principals: []\n",
        encoding="utf-8",
    )

    list_result = _invoke(
        ["list", "--format", "json"],
        programs_root=programs_root,
    )
    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert reviewed.exit_code == 0
    assert json.loads(list_result.stdout)["candidates"][0]["review_state"] == "invalid"
    assert json.loads(status_result.stdout)["invalid_review_candidate_count"] == 1


def test_quarantine_batch_enforces_the_candidate_review_gate(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "src.commands.knowledge.capture_operator_identity",
        lambda *_args, **_kwargs: REVIEWER,
    )
    programs_root = tmp_path / "programs"
    _write_program_review_policy(programs_root)
    append_candidate(_candidate(), programs_root=programs_root)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "quarantine-batch",
            "--batch-id",
            "batch-1",
            "--actor",
            "reviewer",
            "--reason",
            "bad extraction",
            "--program",
            "acme",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 2
    assert "current authorized review" in result.output


def test_expired_review_refuses_approval(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("src.commands.knowledge.capture_operator_identity", lambda *_args, **_kwargs: REVIEWER)
    programs_root = tmp_path / "programs"
    _write_program_review_policy(programs_root)
    candidate = _candidate()
    append_candidate(candidate, programs_root=programs_root)
    now = datetime.now(timezone.utc)
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="reviewed",
            decided_at=now - timedelta(days=2),
            triage_actor=REVIEWER.actor,
            batch_id=candidate.batch_id,
            reason="initial review",
            review_valid_until=now - timedelta(days=1),
            actor_principal=REVIEWER.principal,
            actor_machine=REVIEWER.machine,
            actor_session=REVIEWER.session,
            candidate_sha256=candidate_payload_sha256(candidate),
            review_authority_program="acme",
            reviewed_subject="sku_generation:gen9",
        ),
        programs_root=programs_root,
    )

    result = _invoke(
        ["approve", "--candidate", "cand-1", "--actor", "reviewer", "--program", "acme"],
        programs_root=programs_root,
    )

    assert result.exit_code == 2
    assert "current authorized review" in result.output


def test_review_commands_refuse_invalid_windows_and_transitions(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "src.commands.knowledge.capture_operator_identity",
        lambda *_args, **_kwargs: REVIEWER,
    )
    programs_root = tmp_path / "programs"
    _write_program_review_policy(programs_root)
    append_candidate(_candidate(), programs_root=programs_root)

    invalid_date = _invoke(
        [
            "review",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--valid-until",
            "not-a-date",
            "--reason",
            "checked",
        ],
        programs_root=programs_root,
    )
    empty_reason = _invoke(
        [
            "review",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--valid-until",
            "2099-01-01T00:00:00+00:00",
            "--reason",
            " ",
        ],
        programs_root=programs_root,
    )
    renew_without_review = _invoke(
        [
            "renew",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--valid-until",
            "2099-01-01T00:00:00+00:00",
            "--reason",
            "renew",
        ],
        programs_root=programs_root,
    )
    initial = _invoke(
        [
            "review",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--valid-until",
            "2099-01-01T00:00:00+00:00",
            "--reason",
            "checked",
        ],
        programs_root=programs_root,
    )
    duplicate = _invoke(
        [
            "review",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--valid-until",
            "2099-02-01T00:00:00+00:00",
            "--reason",
            "duplicate",
        ],
        programs_root=programs_root,
    )

    assert invalid_date.exit_code == 2
    assert empty_reason.exit_code == 2
    assert renew_without_review.exit_code == 2
    assert initial.exit_code == 0
    assert duplicate.exit_code == 2


def test_review_authority_refuses_missing_wrong_scope_and_untrusted_identity(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate(), programs_root=programs_root)
    arguments = ["approve", "--candidate", "cand-1", "--actor", "reviewer"]

    missing_program = _invoke(arguments, programs_root=programs_root)
    escaping_program = _invoke(
        [*arguments, "--program", "../escape"],
        programs_root=programs_root,
    )

    program = programs_root / "acme"
    program.mkdir(parents=True)
    (program / "program.yaml").write_text(
        "id: acme\nname: Synthetic\nknowledge_scopes: []\n",
        encoding="utf-8",
    )
    (program / "review.yaml").write_text(
        "reviewers: []\n"
        "required: false\n"
        "knowledge_review_principals:\n"
        f"  - '{REVIEWER.principal}'\n",
        encoding="utf-8",
    )
    outside_scope = _invoke(
        [*arguments, "--program", "acme"],
        programs_root=programs_root,
    )

    (program / "program.yaml").write_text(
        "id: acme\nname: Synthetic\nknowledge_scopes:\n  - domain:storage-platform\n",
        encoding="utf-8",
    )
    (program / "review.yaml").unlink()
    missing_policy = _invoke(
        [*arguments, "--program", "acme"],
        programs_root=programs_root,
    )

    (program / "review.yaml").write_text(
        "reviewers: []\n"
        "required: false\n"
        "knowledge_review_principals:\n"
        f"  - '{REVIEWER.principal}'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.commands.knowledge.capture_operator_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unavailable")),
    )
    missing_identity = _invoke(
        [*arguments, "--program", "acme"],
        programs_root=programs_root,
    )
    monkeypatch.setattr(
        "src.commands.knowledge.capture_operator_identity",
        lambda *_args, **_kwargs: replace(REVIEWER, principal=r"SYNTHETIC\other"),
    )
    unauthorized = _invoke(
        [*arguments, "--program", "acme"],
        programs_root=programs_root,
    )

    assert [result.exit_code for result in (
        missing_program,
        escaping_program,
        outside_scope,
        missing_policy,
        missing_identity,
        unauthorized,
    )] == [2] * 6


def test_resolution_verification_refuses_a_missing_claim(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("src.commands.knowledge.capture_operator_identity", lambda *_args, **_kwargs: REVIEWER)
    programs_root = tmp_path / "programs"
    _write_program_review_policy(programs_root)
    candidate = _candidate()
    append_candidate(candidate, programs_root=programs_root)
    now = datetime.now(timezone.utc)
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="reviewed",
            decided_at=now,
            triage_actor=REVIEWER.actor,
            batch_id=candidate.batch_id,
            review_valid_until=now + timedelta(days=1),
            actor_principal=REVIEWER.principal,
            actor_machine=REVIEWER.machine,
            actor_session=REVIEWER.session,
            candidate_sha256=candidate_payload_sha256(candidate),
            review_authority_program="acme",
            reviewed_subject="sku_generation:gen9",
        ),
        programs_root=programs_root,
    )
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="approved",
            decided_at=now + timedelta(milliseconds=1),
            triage_actor=REVIEWER.actor,
            batch_id=candidate.batch_id,
            resulting_claim_id="01MISSINGCLAIM000000000000000",
            candidate_sha256=candidate_payload_sha256(candidate),
            review_authority_program="acme",
            reviewed_subject="sku_generation:gen9",
        ),
        programs_root=programs_root,
    )

    result = _invoke(
        [
            "verify-resolution",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--reason",
            "verify expected claim",
        ],
        programs_root=programs_root,
    )

    assert result.exit_code == 2
    assert "resulting claim" in result.output
    assert [record.kind for record in load_triage_decisions(programs_root=programs_root)] == [
        "reviewed",
        "approved",
    ]


def test_resolution_verification_refuses_unbound_or_disordered_history(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "src.commands.knowledge.capture_operator_identity",
        lambda *_args, **_kwargs: REVIEWER,
    )
    now = datetime.now(timezone.utc)

    cases = ("no-terminal", "skipped", "unbound", "review-after-terminal", "no-claim")
    for case in cases:
        programs_root = tmp_path / case / "programs"
        _write_program_review_policy(programs_root)
        candidate = _candidate()
        append_candidate(candidate, programs_root=programs_root)
        review_at = now - timedelta(minutes=1)
        if case == "review-after-terminal":
            review_at = now - timedelta(seconds=1)
        append_triage_decision(
            KnowledgeCandidateDecisionRecord(
                candidate_id=candidate.candidate_id,
                kind="reviewed",
                decided_at=review_at,
                triage_actor=REVIEWER.actor,
                batch_id=candidate.batch_id,
                review_valid_until=now + timedelta(days=1),
                actor_principal=REVIEWER.principal,
                actor_machine=REVIEWER.machine,
                actor_session=REVIEWER.session,
                candidate_sha256=candidate_payload_sha256(candidate),
                review_authority_program="acme",
                reviewed_subject="sku_generation:gen9",
            ),
            programs_root=programs_root,
        )
        if case != "no-terminal":
            append_triage_decision(
                KnowledgeCandidateDecisionRecord(
                    candidate_id=candidate.candidate_id,
                    kind="skipped" if case == "skipped" else "approved",
                    decided_at=now - timedelta(minutes=2)
                    if case == "review-after-terminal"
                    else now,
                    triage_actor=REVIEWER.actor,
                    batch_id=candidate.batch_id,
                    resulting_claim_id=None if case == "no-claim" else "01CLAIM",
                    candidate_sha256="sha256:wrong"
                    if case == "unbound"
                    else candidate_payload_sha256(candidate),
                    review_authority_program="acme",
                    reviewed_subject="sku_generation:gen9",
                ),
                programs_root=programs_root,
            )
        result = _invoke(
            [
                "verify-resolution",
                "--candidate",
                "cand-1",
                "--actor",
                "reviewer",
                "--program",
                "acme",
                "--reason",
                "verify",
            ],
            programs_root=programs_root,
        )
        assert result.exit_code == 2, case


def test_rejected_resolution_can_be_verified_from_retained_history(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "src.commands.knowledge.capture_operator_identity",
        lambda *_args, **_kwargs: REVIEWER,
    )
    programs_root = tmp_path / "programs"
    _write_program_review_policy(programs_root)
    append_candidate(_candidate(), programs_root=programs_root)

    reviewed = _invoke(
        [
            "review",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--valid-until",
            "2099-01-01T00:00:00+00:00",
            "--reason",
            "source checked",
        ],
        programs_root=programs_root,
    )
    rejected = _invoke(
        [
            "reject",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--reason",
            "duplicate",
        ],
        programs_root=programs_root,
    )
    verified = _invoke(
        [
            "verify-resolution",
            "--candidate",
            "cand-1",
            "--actor",
            "reviewer",
            "--program",
            "acme",
            "--reason",
            "rejection decision retained",
        ],
        programs_root=programs_root,
    )

    assert reviewed.exit_code == 0
    assert rejected.exit_code == 0
    assert verified.exit_code == 0
    assert [record.kind for record in load_triage_decisions(programs_root=programs_root)] == [
        "reviewed",
        "rejected",
        "resolution_verified",
    ]


def test_history_text_handles_empty_and_retained_history(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    candidate = _candidate(requires_authorized_review=False)
    append_candidate(candidate, programs_root=programs_root)

    empty = _invoke(
        ["history", "--candidate", "cand-1", "--format", "text"],
        programs_root=programs_root,
    )
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="rejected",
            decided_at=datetime.now(timezone.utc),
            triage_actor="reviewer",
            batch_id=candidate.batch_id,
            reason="duplicate",
        ),
        programs_root=programs_root,
    )
    retained = _invoke(
        ["history", "--candidate", "cand-1", "--format", "text"],
        programs_root=programs_root,
    )
    invalid_format = _invoke(
        ["history", "--candidate", "cand-1", "--format", "yaml"],
        programs_root=programs_root,
    )
    unknown = _invoke(
        ["history", "--candidate", "missing", "--format", "json"],
        programs_root=programs_root,
    )

    assert empty.exit_code == 0
    assert "No retained lifecycle events." in empty.output
    assert retained.exit_code == 0
    assert "kind=rejected" in retained.output
    assert invalid_format.exit_code == 2
    assert unknown.exit_code == 2


def test_kb_extractor_marks_new_candidates_for_authorized_review(tmp_path: Path) -> None:
    batch = extract_claim_candidates_from_markdown(
        markdown_text="Claim: subject=storage-platform;predicate=term_definition;value=Storage service vocabulary",
        scope="domain:storage-platform",
        vault_hash="sha256:synthetic",
        original_filename="synthetic.md",
        origin_path="synthetic.md",
        ingested_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    assert len(batch.candidates) == 1
    assert batch.candidates[0].knowledge_class == KnowledgeClass.GLOSSARY.value
    assert batch.candidates[0].requires_authorized_review is True


def test_kb_extractor_preserves_registered_boolean_knowledge_values() -> None:
    batch = extract_claim_candidates_from_markdown(
        markdown_text="\n".join(
            (
                "Claim: subject=program:acme;predicate=risk_accepted;value=true",
                "Claim: subject=program:acme;predicate=is_deprecated;value=false",
                "Claim: subject=program:acme;predicate=launch_blocker;value=null",
                "Claim: subject=program:acme;predicate=launch_blocker",
            )
        ),
        scope="program:acme",
        vault_hash="sha256:synthetic",
        original_filename="synthetic.md",
        origin_path="synthetic.md",
        ingested_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    assert [candidate.proposed_claim.value for candidate in batch.candidates] == [
        True,
        False,
        None,
        None,
    ]


def test_review_events_preserve_candidate_vault_refs_until_terminal_decision(
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candidate = build_candidate(
        candidate_id="cand-vault",
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=now,
        valid_until=None,
        proposed_confidence=ConfidenceTier.AI_EXTRACTED,
        source_ref=KnowledgeDocumentRef(
            vault_hash="sha256:synthetic-vault",
            original_filename="synthetic.md",
            origin_kind="knowledge_markdown",
            origin_path="synthetic.md",
            ingested_at=now,
            section="Claim",
        ),
        pipeline="kb_extract",
        extraction_confidence=0.95,
        entity_resolution=(),
        corroborating_refs=(),
        batch_id="batch-vault",
        requires_authorized_review=True,
    )
    append_candidate(candidate, programs_root=programs_root)
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="reviewed",
            decided_at=now,
            triage_actor=REVIEWER.actor,
            batch_id=candidate.batch_id,
            review_valid_until=now + timedelta(days=1),
            actor_principal=REVIEWER.principal,
            actor_machine=REVIEWER.machine,
            actor_session=REVIEWER.session,
            candidate_sha256=candidate_payload_sha256(candidate),
            review_authority_program="acme",
            reviewed_subject="sku_generation:gen9",
        ),
        programs_root=programs_root,
    )

    assert load_live_vault_hashes(knowledge_root=tmp_path / "knowledge") == (
        "sha256:synthetic-vault",
    )

    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="rejected",
            decided_at=now + timedelta(minutes=1),
            triage_actor=REVIEWER.actor,
            batch_id=candidate.batch_id,
            candidate_sha256=candidate_payload_sha256(candidate),
        ),
        programs_root=programs_root,
    )

    assert load_live_vault_hashes(knowledge_root=tmp_path / "knowledge") == ()


def test_doctor_surfaces_unreviewed_gated_candidates(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_candidate(_candidate(), programs_root=programs_root)

    check = knowledge_candidate_latency_check(programs_root=programs_root)

    assert check.status == "warn"
    assert check.metadata is not None
    assert check.metadata["unreviewed_candidate_count"] == 1
    assert "lack a current authorized review" in check.detail

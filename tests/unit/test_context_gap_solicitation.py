"""ADF-W3.7: unit tests for src/core/context_gap_solicitation.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.context_gap_solicitation import (
    ContextGapSolicitationError,
    generate_deterministic_solicitation,
    is_in_cooldown,
    record_solicitation_drafted,
    write_solicitation_draft,
)
from src.core.context_gap_store import RankedGap
from src.core.nudge_models import ResolvedRecipient

_NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("key,value", [
    ("packet_version", True), ("packet_version", 1.0), ("packet_version", "1"),
    ("target_key", None), ("target_key", []), ("target_key", ""),
    ("target_field", []), ("current_value", None), ("current_value", []),
    ("current_value_hash", None), ("current_value_hash", "0" * 64),
    ("eml_sha256", []),
])
def test_packet_validator_rejects_malformed_bound_shapes_without_mutation(
    tmp_path: Path, key: str, value,
) -> None:
    from hashlib import sha256
    import json
    from src.core.context_gap_solicitation import validate_solicitation_draft
    from tests.unit.test_context_gap_reply_import import _bound_intake_workspace

    programs, _, packet = _bound_intake_workspace(tmp_path)
    program = programs / "sample"
    log = program / "_feedback" / "context_gap_solicitations.jsonl"
    row = json.loads(log.read_text())
    row[key] = value
    binding = json.dumps((
        "sample", row["target_key"], row["target_field"], row["current_value"], row["current_value_hash"],
    ), ensure_ascii=False)
    draft = program / "nudge" / "drafts" / f"{packet.id}.eml"
    lines = draft.read_bytes().split(b"\r\n")
    lines = [
        b"X-Vertex-Packet-Binding: " + sha256(binding.encode("utf-8")).hexdigest().encode()
        if line.startswith(b"X-Vertex-Packet-Binding:") else line
        for line in lines
    ]
    draft.write_bytes(b"\r\n".join(lines))
    if key != "eml_sha256":
        row["eml_sha256"] = sha256(draft.read_bytes()).hexdigest()
    log.write_text(json.dumps(row) + "\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ContextGapSolicitationError):
        validate_solicitation_draft("sample", draft, programs_root=programs)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def _gap(*, program: str = "acme") -> RankedGap:
    return RankedGap(
        feature="workstream_registry",
        program=program,
        lane="deployment",
        field="deep_context",
        severity="quality_degraded",
        impact_estimate="high",
        count=3,
        first_seen=_NOW - timedelta(days=10),
        last_seen=_NOW,
        message="deep_context.why is missing",
        fix_hint="add deep_context.why / .what / .how to workstream_registry.yaml in workstream deployment",
    )


def _recipient() -> ResolvedRecipient:
    return ResolvedRecipient(alias="alex", email="alex@example.com", display_name="Alex")


def _staged_solicitation():
    return generate_deterministic_solicitation(_gap(), recipient=_recipient(), evidence_link="https://vertex/acme/gaps")


def test_generate_deterministic_solicitation_content() -> None:
    solicitation = _staged_solicitation()
    assert "deep_context.why is missing" in solicitation.current_gap
    assert "deployment" in solicitation.current_gap
    assert "high-impact" in solicitation.why_it_matters
    assert solicitation.requested_action == "add deep_context.why / .what / .how to workstream_registry.yaml in workstream deployment"
    assert solicitation.evidence_link == "https://vertex/acme/gaps"
    assert solicitation.generation_method == "deterministic"
    assert solicitation.current_value_hash is None
    assert solicitation.gap_fingerprint == "acme:workstream_registry:deployment:deep_context"


def test_no_second_in_memory_approval_lifecycle() -> None:
    from src.core import context_gap_solicitation as module

    assert not hasattr(module, "approve_solicitation")
    assert not hasattr(module, "reject_solicitation")
    assert not hasattr(_staged_solicitation(), "status")


def test_cooldown_false_when_never_drafted(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert is_in_cooldown(_gap(), programs_root=programs_root, now=_NOW) is False


def test_cooldown_true_within_window(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    solicitation = _staged_solicitation()
    record_solicitation_drafted(solicitation, programs_root=programs_root, now=_NOW)

    assert is_in_cooldown(_gap(), programs_root=programs_root, now=_NOW + timedelta(days=5)) is True


def test_cooldown_false_after_window_expires(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    solicitation = _staged_solicitation()
    record_solicitation_drafted(solicitation, programs_root=programs_root, now=_NOW)

    assert is_in_cooldown(_gap(), programs_root=programs_root, now=_NOW + timedelta(days=15)) is False


def test_cooldown_is_scoped_to_the_exact_gap_fingerprint(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    solicitation = _staged_solicitation()
    record_solicitation_drafted(solicitation, programs_root=programs_root, now=_NOW)

    different_gap = RankedGap(
        feature="workstream_registry", program="acme", lane="repair", field="deep_context",
        severity="quality_degraded", impact_estimate="high", count=1, first_seen=_NOW, last_seen=_NOW,
        message="deep_context.why is missing", fix_hint="add deep_context.why",
    )
    assert is_in_cooldown(different_gap, programs_root=programs_root, now=_NOW) is False


def test_write_draft_stays_pending_without_creating_approval(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    draft = write_solicitation_draft(
        _staged_solicitation(), from_email="vertex@example.com", programs_root=programs_root, now=_NOW,
    )
    assert draft.exists()
    assert not (draft.parent.parent / "draft_approvals.json").exists()


def test_write_draft_produces_real_eml_file(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    solicitation = _staged_solicitation()

    draft_path = write_solicitation_draft(
        solicitation, from_email="vertex@example.com", from_display_name="Vertex", programs_root=programs_root, now=_NOW,
    )

    assert draft_path.exists()
    assert draft_path.suffix == ".eml"
    raw = draft_path.read_bytes()
    assert b"X-Unsent: 1" in raw
    assert b"alex@example.com" in raw
    assert b"deep_context.why is missing" in raw


def test_write_draft_lands_in_the_existing_nudge_drafts_dir(tmp_path: Path) -> None:
    from src.core.edition_resolver import get_nudge_paths

    programs_root = tmp_path / "programs"
    solicitation = _staged_solicitation()

    draft_path = write_solicitation_draft(
        solicitation, from_email="vertex@example.com", programs_root=programs_root, now=_NOW
    )

    expected_dir = get_nudge_paths("acme", programs_root=programs_root).drafts_dir
    assert draft_path.parent == expected_dir


def test_legacy_record_remains_unknown_bound(tmp_path: Path) -> None:
    from src.core.context_gap_solicitation import validate_solicitation_draft

    root = tmp_path / "programs"
    solicitation = _staged_solicitation()
    draft = write_solicitation_draft(solicitation, from_email=None, programs_root=root)
    record_solicitation_drafted(solicitation, programs_root=root)
    log = root / "acme" / "_feedback" / "context_gap_solicitations.jsonl"
    before = log.read_bytes()
    assert validate_solicitation_draft("acme", draft, programs_root=root) is False
    assert log.read_bytes() == before
    assert b"current_value_hash" not in before and b"program_id" not in before


@pytest.mark.parametrize("timestamp", ["invalid", "2026-07-01T00:00:00"])
def test_invalid_cooldown_time_fails_without_repair(tmp_path: Path, timestamp: str) -> None:
    import json

    log = tmp_path / "acme" / "_feedback" / "context_gap_solicitations.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"gap_fingerprint": "acme:workstream_registry:deployment:deep_context", "solicitation_id": "legacy", "drafted_at": timestamp}))
    before = log.read_bytes()
    with pytest.raises(ContextGapSolicitationError):
        is_in_cooldown(_gap(), programs_root=tmp_path, now=_NOW)
    assert log.read_bytes() == before


def test_solicitation_never_overwrites_packet(tmp_path: Path) -> None:
    solicitation = _staged_solicitation()
    draft = write_solicitation_draft(solicitation, from_email=None, programs_root=tmp_path)
    before = draft.read_bytes()
    with pytest.raises(ContextGapSolicitationError, match="overwrite"):
        write_solicitation_draft(solicitation, from_email=None, programs_root=tmp_path)
    assert draft.read_bytes() == before


def test_unrelated_malformed_cooldown_record_is_not_silently_ignored(tmp_path: Path) -> None:
    log = tmp_path / "acme" / "_feedback" / "context_gap_solicitations.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"gap_fingerprint": "other"}\n')
    with pytest.raises(ContextGapSolicitationError):
        is_in_cooldown(_gap(), programs_root=tmp_path, now=_NOW)


@pytest.mark.parametrize("change", ["foreign", "unsupported", "relative"])
def test_producer_rejects_invalid_program_gap_and_root(tmp_path: Path, change: str) -> None:
    import json
    from src.core.context_gap_solicitation import produce_context_gap_solicitation
    from tests.unit.test_commands_context_solicitation import _workspace

    programs, fingerprint = _workspace(tmp_path)
    log = programs / "sample" / "_feedback" / "context_gaps.jsonl"
    record = json.loads(log.read_text())
    if change == "foreign":
        record["program"] = "other"
    elif change == "unsupported":
        record["field"] = "owner"
        fingerprint = fingerprint.replace("deep_context.why", "owner")
    else:
        programs = Path("relative")
    log.write_text(json.dumps(record) + "\n")
    with pytest.raises(ContextGapSolicitationError):
        produce_context_gap_solicitation(
            program_id="sample", gap_fingerprint=fingerprint, recipient_email="reviewer@example.com", programs_root=programs,
        )


@pytest.mark.parametrize("change", ["cooldown", "baseline"])
def test_producer_rechecks_after_acquiring_existing_nudge_lock(tmp_path: Path, monkeypatch, change: str) -> None:
    from src.core import context_gap_solicitation as module
    from tests.unit.test_commands_context_solicitation import _workspace

    programs, fingerprint = _workspace(tmp_path)
    if change == "cooldown":
        answers = iter((False, True))
        monkeypatch.setattr(module, "is_in_cooldown", lambda *args, **kwargs: next(answers))
    else:
        answers = iter((("old", "old-hash"), ("changed", "changed-hash")))
        monkeypatch.setattr(module, "read_registry_baseline", lambda *args, **kwargs: next(answers))
    with pytest.raises(ContextGapSolicitationError):
        module.produce_context_gap_solicitation(
            program_id="sample", gap_fingerprint=fingerprint, recipient_email="reviewer@example.com", programs_root=programs,
        )
    assert not list((programs / "sample" / "nudge").rglob("*.eml"))
    assert not (programs / "sample" / "_feedback" / "context_gap_solicitations.jsonl").exists()


def test_failed_cooldown_append_leaves_unapprovable_orphan_not_success(tmp_path: Path, monkeypatch) -> None:
    from src.core import context_gap_solicitation as module
    from tests.unit.test_commands_context_solicitation import _workspace

    programs, fingerprint = _workspace(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("Synthetic append failure")

    monkeypatch.setattr(module, "record_solicitation_drafted", fail)
    with pytest.raises(ContextGapSolicitationError, match="unavailable"):
        module.produce_context_gap_solicitation(
            program_id="sample", gap_fingerprint=fingerprint, recipient_email="reviewer@example.com", programs_root=programs,
        )
    orphan = next((programs / "sample" / "nudge" / "drafts").glob("*.eml"))
    with pytest.raises(ContextGapSolicitationError, match="binding"):
        module.validate_solicitation_draft("sample", orphan, programs_root=programs)


def test_binding_validation_does_not_read_foreign_draft(tmp_path: Path) -> None:
    from src.core.context_gap_solicitation import validate_solicitation_draft
    from tests.unit.test_commands_context_solicitation import _workspace

    programs, _ = _workspace(tmp_path)
    with pytest.raises(ContextGapSolicitationError, match="leaves"):
        validate_solicitation_draft("sample", tmp_path / "outside.eml", programs_root=programs)


def test_pending_writer_rejects_path_bearing_packet_identifier(tmp_path: Path) -> None:
    from dataclasses import replace

    packet = replace(_staged_solicitation(), id="..\\outside")
    with pytest.raises(ContextGapSolicitationError):
        write_solicitation_draft(packet, from_email=None, programs_root=tmp_path)
    assert not list(tmp_path.iterdir())

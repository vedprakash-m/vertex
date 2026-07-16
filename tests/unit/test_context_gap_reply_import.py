"""ADF-W3.7 remainder: unit tests for src/core/context_gap_reply_import.py."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
import json
from datetime import datetime, timezone
from dataclasses import replace

import pytest

from src.core.context_gap_reply_import import list_pending_replies, parse_reply_eml, replies_dir
from src.core.eml_writer import write_eml_atomic


def _intake_workspace(root: Path) -> tuple[Path, str]:
    program = root / "sample"
    feedback = program / "_feedback"
    feedback.mkdir(parents=True)
    fingerprint = "sample:doctor:lane-a:deep_context.why"
    (feedback / "context_gaps.jsonl").write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(), "feature": "doctor", "program": "sample",
        "lane": "lane-a", "field": "deep_context.why", "severity": "quality_degraded",
        "message": "Why missing", "impact_estimate": "high",
    }) + "\n", encoding="utf-8")
    (feedback / "context_gap_solicitations.jsonl").write_text(json.dumps({
        "gap_fingerprint": fingerprint, "solicitation_id": "sol-1",
        "drafted_at": "2026-10-24T00:00:00Z",
    }) + "\n", encoding="utf-8")
    (program / "workstream_registry.yaml").write_text(
        "workstreams:\n- id: lane-a\n  deep_context: {}\n", encoding="utf-8"
    )
    source = program / "reply.eml"
    _write_reply_eml(source, from_addr="Forged <owner@example.com>", subject="Re: gap",
                     body="The exact proposed answer.\n\n> Reference: sol-1\n")
    return source, fingerprint


def _import(root: Path, **kwargs):
    from src.core.context_gap_reply_import import import_context_gap_reply
    return import_context_gap_reply(
        program_id="sample", edition_id="sample_weekly", issue_number=1,
        source=root / "sample" / "reply.eml",
        gap_fingerprint="sample:doctor:lane-a:deep_context.why",
        solicitation_id="sol-1", programs_root=root, **kwargs,
    )


def _bound_intake_workspace(root: Path, registry_value: str | None = None):
    from src.core.context_gap_solicitation import produce_context_gap_solicitation
    from tests.unit.test_commands_context_solicitation import _workspace

    programs, fingerprint = _workspace(root)
    if registry_value is not None:
        (programs / "sample" / "workstream_registry.yaml").write_text(
            "workstreams:\n- id: deployment\n  deep_context:\n    why: " + registry_value + "\n"
        )
    packet = produce_context_gap_solicitation(
        program_id="sample", gap_fingerprint=fingerprint, recipient_email="owner@example.com",
        programs_root=programs,
    )
    source = programs / "sample" / "reply.eml"
    _write_reply_eml(source, from_addr="Forged <owner@example.com>", subject="Re: gap",
                     body=f"The exact proposed answer.\n\n> Reference: {packet.id}\n")
    return programs, dict(
        program_id="sample", edition_id="sample_weekly", issue_number=1, source=source,
        gap_fingerprint=fingerprint, solicitation_id=packet.id, programs_root=programs,
    ), packet


@pytest.mark.parametrize("change", [
    "foreign-program", "foreign-target", "foreign-field", "foreign-store", "foreign-reference",
    "bool-version", "future-version", "partial", "stripped", "bad-log", "nonmapping-log",
    "duplicate", "missing-log", "missing-record", "missing-draft", "edited-draft", "edited-hash",
    "missing-binding-header", "stale", "typed-stale",
])
def test_bound_intake_rejects_invalid_packet_without_writes(tmp_path: Path, change: str) -> None:
    from src.core.context_gap_reply_import import ContextGapReplyImportError, import_context_gap_reply

    programs, arguments, packet = _bound_intake_workspace(tmp_path)
    program = programs / "sample"
    log = program / "_feedback" / "context_gap_solicitations.jsonl"
    record = json.loads(log.read_text())
    draft = program / "nudge" / "drafts" / f"{packet.id}.eml"
    if change.startswith("foreign-") and change != "foreign-reference":
        key, value = {
            "foreign-program": ("program_id", "other"), "foreign-target": ("target_key", "other"),
            "foreign-field": ("target_field", "deep_context.what"), "foreign-store": ("target_store", "other"),
        }[change]
        record[key] = value
        log.write_text(json.dumps(record) + "\n")
    elif change == "foreign-reference":
        source = arguments["source"]
        source.write_bytes(source.read_bytes().replace(packet.id.encode(), b"other"))
    elif change in {"bool-version", "future-version", "partial", "stripped", "edited-hash"}:
        if change in {"bool-version", "future-version"}:
            record["packet_version"] = True if change == "bool-version" else 2
        elif change == "partial":
            del record["current_value_hash"]
        elif change == "stripped":
            record = {key: record[key] for key in ("gap_fingerprint", "solicitation_id", "drafted_at")}
        else:
            record["eml_sha256"] = "0" * 64
        log.write_text(json.dumps(record) + "\n")
    elif change in {"bad-log", "nonmapping-log", "duplicate", "missing-record"}:
        log.write_text({
            "bad-log": "{broken\n", "nonmapping-log": "[]\n",
            "duplicate": log.read_text() * 2, "missing-record": "",
        }[change])
    elif change == "missing-log":
        log.unlink()
    elif change == "missing-draft":
        draft.unlink()
    elif change == "edited-draft":
        draft.write_bytes(draft.read_bytes() + b"\r\nChanged")
    elif change == "missing-binding-header":
        raw = draft.read_bytes()
        draft.write_bytes(b"\r\n".join(
            line for line in raw.split(b"\r\n") if not line.startswith(b"X-Vertex-Packet-Binding:")
        ))
    else:
        (program / "workstream_registry.yaml").write_text(
            "workstreams:\n- id: deployment\n  deep_context:\n    why: "
            + ("null\n" if change == "typed-stale" else "Changed\n")
        )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ContextGapReplyImportError):
        import_context_gap_reply(**arguments)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("metadata", [
    "packet_version", "program_id", "target_store", "target_key", "target_field",
    "current_value", "current_value_hash", "eml_sha256",
])
def test_legacy_identifier_with_binding_metadata_cannot_downgrade(tmp_path: Path, metadata: str) -> None:
    from src.core.context_gap_reply_import import ContextGapReplyImportError

    _intake_workspace(tmp_path)
    log = tmp_path / "sample" / "_feedback" / "context_gap_solicitations.jsonl"
    record = json.loads(log.read_text())
    record[metadata] = None
    log.write_text(json.dumps(record) + "\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ContextGapReplyImportError):
        _import(tmp_path)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_metadata_only_binding_with_retained_legacy_draft_cannot_downgrade(tmp_path: Path) -> None:
    from src.core.context_gap_reply_import import ContextGapReplyImportError

    _intake_workspace(tmp_path)
    program = tmp_path / "sample"
    log = program / "_feedback" / "context_gap_solicitations.jsonl"
    record = json.loads(log.read_text())
    record["packet_version"] = 1
    log.write_text(json.dumps(record) + "\n")
    drafts = program / "nudge" / "drafts"
    drafts.mkdir(parents=True)
    _write_reply_eml(drafts / "sol-1.eml", from_addr="owner@example.com", subject="Legacy", body="Legacy")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ContextGapReplyImportError, match="binding"):
        _import(tmp_path)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("value,replacement", [
    ("null", '""'), ('""', "null"), ("1", '"1"'), ("true", '"True"'),
    ('"<missing>"', "null"), ('"<null>"', "null"),
])
def test_bound_intake_preserves_typed_packet_baseline(
    tmp_path: Path, value: str, replacement: str,
) -> None:
    from src.core.context_gap_reply_import import ContextGapReplyImportError, import_context_gap_reply

    programs, arguments, packet = _bound_intake_workspace(tmp_path, value)
    proposal = import_context_gap_reply(**arguments, dry_run=True)
    assert (proposal.current_value, proposal.current_value_hash) == (packet.current_value, packet.current_value_hash)
    (programs / "sample" / "workstream_registry.yaml").write_text(
        "workstreams:\n- id: deployment\n  deep_context:\n    why: " + replacement + "\n"
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ContextGapReplyImportError):
        import_context_gap_reply(**arguments)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("escape", ["identifier", "draft", "registry", "log"])
def test_bound_intake_rejects_packet_path_escape(tmp_path: Path, escape: str) -> None:
    from src.core.context_gap_reply_import import ContextGapReplyImportError, import_context_gap_reply

    programs, arguments, packet = _bound_intake_workspace(tmp_path)
    program = programs / "sample"
    log = program / "_feedback" / "context_gap_solicitations.jsonl"
    if escape == "identifier":
        identifier = "solicitation-v1-..\\outside"
        record = json.loads(log.read_text())
        record["solicitation_id"] = identifier
        log.write_text(json.dumps(record) + "\n")
        arguments["solicitation_id"] = identifier
    else:
        path = {
            "draft": program / "nudge" / "drafts" / f"{packet.id}.eml",
            "registry": program / "workstream_registry.yaml", "log": log,
        }[escape]
        outside = tmp_path / f"outside-{path.name}"
        path.rename(outside)
        try:
            path.symlink_to(outside)
        except OSError as error:
            pytest.skip(f"Local symbolic links unavailable: {error}")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ContextGapReplyImportError):
        import_context_gap_reply(**arguments)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("change", ["stale", "tampered", "missing"])
def test_bound_replay_validates_before_return_without_changing_decision(tmp_path: Path, change: str) -> None:
    from src.core.context_gap_reply_import import ContextGapReplyImportError, import_context_gap_reply
    from src.core.ncfl_proposal_store import update_proposal_status

    programs, arguments, packet = _bound_intake_workspace(tmp_path)
    proposal = import_context_gap_reply(**arguments)
    update_proposal_status("sample", proposal_id=proposal.proposal_id, new_status="dismissed",
                           actor="reviewer", programs_root=programs)
    draft = programs / "sample" / "nudge" / "drafts" / f"{packet.id}.eml"
    if change == "stale":
        (programs / "sample" / "workstream_registry.yaml").write_text(
            "workstreams:\n- id: deployment\n  deep_context: {why: Changed}\n"
        )
    elif change == "tampered":
        draft.write_bytes(draft.read_bytes() + b"\r\nChanged")
    else:
        draft.unlink()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ContextGapReplyImportError):
        import_context_gap_reply(**dict(arguments, issue_number=2))
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_bound_intake_uses_packet_baseline_and_preserves_conflicts_and_decisions(tmp_path: Path) -> None:
    from src.core.context_gap_reply_import import import_context_gap_reply
    from src.core.manual_registry_diff import ManualRegistryDiffError, render_workstream_registry_manual_diff
    from src.core.ncfl_proposal_store import load_proposals, update_proposal_status

    programs, arguments, packet = _bound_intake_workspace(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    preview = import_context_gap_reply(**arguments, dry_run=True)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    proposal = import_context_gap_reply(**arguments)
    assert proposal.current_value == packet.current_value
    assert proposal.current_value_hash == packet.current_value_hash
    assert "packet-time" in proposal.extraction_method_rationale
    assert "unverified" in proposal.extraction_method_rationale
    assert proposal.status == "pending" and not proposal.batch_eligible and not proposal.decision_history
    assert preview.proposal_id == proposal.proposal_id
    assert not (programs / "sample" / "nudge" / "draft_approvals.json").exists()
    assert render_workstream_registry_manual_diff(proposal, programs_root=programs).current_value == "<missing>"
    source = arguments["source"]
    _write_reply_eml(source, from_addr="other@example.com", subject="Re: gap",
                     body=f"Conflicting answer.\n\n> Reference: {packet.id}\n")
    conflicting = import_context_gap_reply(**arguments)
    assert conflicting.proposal_id != proposal.proposal_id
    assert len(load_proposals("sample", programs_root=programs)) == 2
    assert all(p.status == "pending" and not p.decision_history
               for p in load_proposals("sample", programs_root=programs))
    decided = update_proposal_status("sample", proposal_id=conflicting.proposal_id, new_status="dismissed",
                                     actor="reviewer", programs_root=programs)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert import_context_gap_reply(**dict(arguments, issue_number=2)) == decided
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    (programs / "sample" / "workstream_registry.yaml").write_text(
        "workstreams:\n- id: deployment\n  deep_context: {why: Changed}\n"
    )
    with pytest.raises(ManualRegistryDiffError, match="stale"):
        render_workstream_registry_manual_diff(proposal, programs_root=programs)


@pytest.mark.parametrize("body", [
    "Answer without a reference",
    "Answer\nReference: sol-1\nReference: another",
    "Answer\nReference: wrong",
    "Reference: sol-1",
])
def test_intake_rejects_ambiguous_or_empty_reply_without_writes(tmp_path: Path, body: str) -> None:
    source, _ = _intake_workspace(tmp_path)
    _write_reply_eml(source, from_addr="owner@example.com", subject="gap", body=body)
    from src.core.context_gap_reply_import import ContextGapReplyImportError
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ContextGapReplyImportError):
        _import(tmp_path)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("mutation", ["foreign-gap", "foreign-packet", "duplicate-packet", "duplicate-lane"])
def test_intake_rejects_cross_program_and_ambiguous_associations(tmp_path: Path, mutation: str) -> None:
    _intake_workspace(tmp_path)
    feedback = tmp_path / "sample" / "_feedback"
    if mutation == "foreign-gap":
        path = feedback / "context_gaps.jsonl"
        path.write_text(path.read_text().replace('"program": "sample"', '"program": "other"'))
    elif mutation in {"foreign-packet", "duplicate-packet"}:
        path = feedback / "context_gap_solicitations.jsonl"
        original = path.read_text()
        path.write_text(original.replace("sample:doctor", "other:doctor") if mutation == "foreign-packet"
                        else original + original)
    else:
        (tmp_path / "sample" / "workstream_registry.yaml").write_text(
            "workstreams:\n- id: lane-a\n- id: lane-a\n"
        )
    from src.core.context_gap_reply_import import ContextGapReplyImportError
    with pytest.raises(ContextGapReplyImportError):
        _import(tmp_path)
    assert not (tmp_path / "sample" / "context_proposals").exists()


def test_intake_spoofable_reply_is_pending_unverified_and_preserves_conflicts(tmp_path: Path) -> None:
    _intake_workspace(tmp_path)
    from src.core.ncfl_proposal_store import load_proposals, save_proposals
    preview = _import(tmp_path, dry_run=True)
    assert not (tmp_path / "sample" / "context_proposals").exists()
    existing = replace(preview, proposal_id="prior-proposal", source_value="Another answer")
    save_proposals("sample", 1, (existing,), programs_root=tmp_path)
    before_registry = (tmp_path / "sample" / "workstream_registry.yaml").read_bytes()
    staged = _import(tmp_path)
    assert staged.status == "pending" and not staged.batch_eligible
    assert staged.target_field == "deep_context.why"
    assert staged.source_value == "The exact proposed answer."
    assert "unverified" in staged.extraction_method_rationale
    assert "owner@example.com" in staged.extraction_method_rationale
    assert "sol-1" in staged.extraction_method_rationale
    assert "intake" in staged.extraction_method_rationale
    proposals = load_proposals("sample", programs_root=tmp_path)
    assert len(proposals) == 2
    assert all(p.status == "pending" and not p.decision_history for p in proposals)
    stored = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert _import(tmp_path).proposal_id == staged.proposal_id
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == stored
    assert (tmp_path / "sample" / "workstream_registry.yaml").read_bytes() == before_registry


@pytest.mark.parametrize("target", ["{}", "{why: null}", "{why: ''}"])
def test_intake_baseline_distinguishes_missing_null_empty_and_detects_edit(tmp_path: Path, target: str) -> None:
    _intake_workspace(tmp_path)
    registry = tmp_path / "sample" / "workstream_registry.yaml"
    registry.write_text(f"workstreams:\n- id: lane-a\n  deep_context: {target}\n")
    proposal = _import(tmp_path)
    from src.core.manual_registry_diff import ManualRegistryDiffError, render_workstream_registry_manual_diff
    diff = render_workstream_registry_manual_diff(proposal, programs_root=tmp_path)
    expected = "<missing>" if target == "{}" else "<null>" if "null" in target else ""
    assert diff.current_value == expected
    replacement = "{why: ''}" if target != "{why: ''}" else "{why: null}"
    registry.write_text(f"workstreams:\n- id: lane-a\n  deep_context: {replacement}\n")
    with pytest.raises(ManualRegistryDiffError, match="stale"):
        render_workstream_registry_manual_diff(proposal, programs_root=tmp_path)


@pytest.mark.parametrize("mutation", [
    "missing-gap", "unsupported-field", "missing-lane", "bad-log", "nonmapping-log",
    "missing-source", "bad-registry", "nonscalar-target", "null-container",
])
def test_intake_invalid_local_evidence_is_visible_and_never_stages(tmp_path: Path, mutation: str) -> None:
    _intake_workspace(tmp_path)
    program = tmp_path / "sample"
    gap_path = program / "_feedback" / "context_gaps.jsonl"
    fingerprint = "sample:doctor:lane-a:deep_context.why"
    if mutation == "missing-gap":
        gap_path.unlink()
    elif mutation in {"unsupported-field", "missing-lane"}:
        record = json.loads(gap_path.read_text())
        if mutation == "unsupported-field":
            record["field"] = "roles.email"
            fingerprint = "sample:doctor:lane-a:roles.email"
        else:
            record["lane"] = None
            fingerprint = "sample:doctor::deep_context.why"
        gap_path.write_text(json.dumps(record) + "\n")
    elif mutation in {"bad-log", "nonmapping-log"}:
        (program / "_feedback" / "context_gap_solicitations.jsonl").write_text(
            "{broken" if mutation == "bad-log" else "[]\n"
        )
    elif mutation == "missing-source":
        (program / "reply.eml").unlink()
    else:
        (program / "workstream_registry.yaml").write_text({
            "bad-registry": "workstreams: [",
            "nonscalar-target": "workstreams:\n- id: lane-a\n  deep_context: {why: [invalid]}\n",
            "null-container": "workstreams:\n- id: lane-a\n  deep_context: null\n",
        }[mutation])
    from src.core.context_gap_reply_import import ContextGapReplyImportError, import_context_gap_reply
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ContextGapReplyImportError):
        import_context_gap_reply(
            program_id="sample", edition_id="sample_weekly", issue_number=1, source=program / "reply.eml",
            gap_fingerprint=fingerprint, solicitation_id="sol-1", programs_root=tmp_path,
        )
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("field", ["why", "deep_context.what", "deep_context.how"])
def test_intake_normalizes_only_supported_gap_fields(tmp_path: Path, field: str) -> None:
    _intake_workspace(tmp_path)
    feedback = tmp_path / "sample" / "_feedback"
    path = feedback / "context_gaps.jsonl"
    path.write_text(path.read_text().replace("deep_context.why", field))
    path = feedback / "context_gap_solicitations.jsonl"
    path.write_text(path.read_text().replace("deep_context.why", field))
    from src.core.context_gap_reply_import import import_context_gap_reply
    proposal = import_context_gap_reply(
        program_id="sample", edition_id="sample_weekly", issue_number=1,
        source=tmp_path / "sample" / "reply.eml", gap_fingerprint=f"sample:doctor:lane-a:{field}",
        solicitation_id="sol-1", programs_root=tmp_path,
    )
    assert proposal.target_field == f"deep_context.{field.removeprefix('deep_context.')}"


@pytest.mark.parametrize("program_id", ["", ".", "..", "../sample", "other\\sample", "sample:stream"])
def test_intake_rejects_invalid_program_before_reads(tmp_path: Path, program_id: str) -> None:
    from src.core.context_gap_reply_import import ContextGapReplyImportError, import_context_gap_reply
    with pytest.raises(ContextGapReplyImportError, match="program"):
        import_context_gap_reply(
            program_id=program_id, edition_id="sample_weekly", issue_number=1,
            source=tmp_path / "reply.eml", gap_fingerprint="unused", solicitation_id="sol-1",
            programs_root=tmp_path,
        )


def test_intake_rejects_foreign_source_and_invalid_source_selection(tmp_path: Path) -> None:
    _intake_workspace(tmp_path)
    from src.core.context_gap_reply_import import ContextGapReplyImportError, import_context_gap_reply
    for source, issue in [(tmp_path / "other" / "reply.eml", 1), (tmp_path / "sample" / "reply.txt", 1),
                          (tmp_path / "sample" / "reply.eml", 0)]:
        with pytest.raises(ContextGapReplyImportError):
            import_context_gap_reply(
                program_id="sample", edition_id="sample_weekly", issue_number=issue,
                source=source, gap_fingerprint="sample:doctor:lane-a:deep_context.why",
                solicitation_id="sol-1", programs_root=tmp_path,
            )


def test_duplicate_across_issues_and_manual_edits_does_not_refresh_baseline(tmp_path: Path) -> None:
    _intake_workspace(tmp_path)
    original = _import(tmp_path)
    registry = tmp_path / "sample" / "workstream_registry.yaml"
    registry.write_text("workstreams:\n- id: lane-a\n  deep_context: {why: Manual repair}\n")
    from src.core.context_gap_reply_import import import_context_gap_reply
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    repeated = import_context_gap_reply(
        program_id="sample", edition_id="sample_weekly", issue_number=2,
        source=tmp_path / "sample" / "reply.eml", gap_fingerprint="sample:doctor:lane-a:deep_context.why",
        solicitation_id="sol-1", programs_root=tmp_path,
    )
    assert repeated == original
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_intake_rejects_foreign_proposal_queue(tmp_path: Path) -> None:
    _intake_workspace(tmp_path)
    preview = _import(tmp_path, dry_run=True)
    from src.core.ncfl_proposal_store import save_proposals
    save_proposals("sample", 1, (replace(preview, program_id="other"),), programs_root=tmp_path)
    from src.core.context_gap_reply_import import ContextGapReplyImportError
    with pytest.raises(ContextGapReplyImportError, match="foreign program"):
        _import(tmp_path)


def test_malformed_observations_are_not_silently_skipped_on_intake(tmp_path: Path) -> None:
    _intake_workspace(tmp_path)
    path = tmp_path / "sample" / "_feedback" / "context_gaps.jsonl"
    path.write_text(path.read_text() + "{broken\n")
    before = path.read_bytes()
    from src.core.context_gap_reply_import import ContextGapReplyImportError
    with pytest.raises(ContextGapReplyImportError):
        _import(tmp_path)
    assert path.read_bytes() == before
    assert not (tmp_path / "sample" / "context_proposals").exists()


def test_replay_preserves_existing_decision_without_reapproval(tmp_path: Path) -> None:
    _intake_workspace(tmp_path)
    original = _import(tmp_path)
    from src.core.ncfl_proposal_store import update_proposal_status
    decided = update_proposal_status(
        "sample", proposal_id=original.proposal_id, new_status="dismissed",
        actor="synthetic-reviewer", issue_number=1, rationale="Not verified", programs_root=tmp_path,
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert _import(tmp_path) == decided
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_decode_unknown_charset_and_empty_payload_remain_safe() -> None:
    from email.message import Message
    from src.core.context_gap_reply_import import _decode
    empty = Message()
    assert _decode(empty) == "None"
    encoded = Message()
    encoded["Content-Type"] = "text/plain; charset=not-a-charset"
    encoded.set_payload("answer")
    assert _decode(encoded) == "answer"


def test_reply_with_multiple_original_separators_uses_earliest(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    _write_reply_eml(
        path, from_addr="author@example.com", subject="gap",
        body="Answer\n________________________________\n-----Original Message-----\nReference: sol-1\n",
    )
    parsed = parse_reply_eml(path)
    assert parsed.body_text == "Answer"
    assert parsed.reference_marker == "sol-1"


@pytest.mark.parametrize("body_kind", ["html", "multipart-html", "multipart-empty", "no-from"])
def test_local_parser_preserves_existing_mime_behavior(tmp_path: Path, body_kind: str) -> None:
    message = EmailMessage()
    if body_kind != "no-from":
        message["From"] = "author@example.com"
    if body_kind in {"html", "multipart-html"}:
        message.set_content("<p>Answer</p><p>Reference: sol-1</p>", subtype="html")
        if body_kind == "multipart-html":
            message.make_mixed()
    elif body_kind == "multipart-empty":
        message.make_mixed()
        attachment = EmailMessage()
        attachment.set_content(b"data", maintype="application", subtype="octet-stream")
        message.attach(attachment)
    else:
        message.set_content("Answer")
    path = tmp_path / "reply.eml"
    path.write_bytes(message.as_bytes())
    parsed = parse_reply_eml(path)
    assert parsed.sender_email == (None if body_kind == "no-from" else "author@example.com")
    assert ("Answer" in parsed.body_text) == (body_kind != "multipart-empty")


@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("body", [
    "<p>Purpose &amp; scope.</p><p>Second line.</p><p>Reference: sol-1</p>",
    "<div>Purpose &amp; scope.</div><div>Second line.</div><div>Reference: sol-1</div>",
    "Purpose &amp; scope.<br>Second line.<br/>Reference: sol-1",
    '<DIV class="reply">Purpose &amp; <b>scope.</b></DIV><div>Second line.</div>'
    "<BR />Reference: sol-1",
])
def test_html_reply_preserves_plaintext_lines_and_reference(
    tmp_path: Path, multipart: bool, body: str,
) -> None:
    plain = tmp_path / "plain.eml"
    _write_reply_eml(
        plain, from_addr="author@example.com", subject="gap",
        body="Purpose & scope.\nSecond line.\nReference: sol-1",
    )
    message = EmailMessage()
    message["From"] = "author@example.com"
    message["Subject"] = "gap"
    message.set_content(body, subtype="html")
    if multipart:
        message.make_mixed()
    html = tmp_path / "html.eml"
    html.write_bytes(message.as_bytes())
    assert parse_reply_eml(html) == parse_reply_eml(plain)


def _write_reply_eml(path: Path, *, from_addr: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = "vertex@example.com"
    message["Subject"] = subject
    message.set_content(body)
    write_eml_atomic(path, eml_bytes=message.as_bytes())


def test_replies_dir_is_under_nudge(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert replies_dir("acme", programs_root=programs_root) == programs_root / "acme" / "nudge" / "replies"


def test_list_pending_replies_empty_when_directory_absent(tmp_path: Path) -> None:
    assert list_pending_replies("acme", programs_root=tmp_path / "programs") == ()


def test_list_pending_replies_finds_eml_files(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    directory = replies_dir("acme", programs_root=programs_root)
    directory.mkdir(parents=True)
    _write_reply_eml(directory / "reply1.eml", from_addr="alex@example.com", subject="Re: gap", body="answer")
    _write_reply_eml(directory / "reply2.eml", from_addr="priya@example.com", subject="Re: gap", body="answer 2")

    found = list_pending_replies("acme", programs_root=programs_root)
    assert len(found) == 2
    assert all(path.suffix == ".eml" for path in found)


def test_parse_reply_eml_extracts_sender_and_subject(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    _write_reply_eml(path, from_addr="Alex Example <alex@example.com>", subject="Re: [Vertex] Missing info", body="The answer is X.\n\nReference: solicitation-1\n")

    parsed = parse_reply_eml(path)
    assert parsed.sender_email == "alex@example.com"
    assert parsed.subject == "Re: [Vertex] Missing info"
    assert parsed.reference_marker == "solicitation-1"


def test_parse_reply_eml_isolates_new_content_above_original_message_marker(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    body = (
        "The answer is X.\n\n"
        "Reference: solicitation-1\n\n"
        "-----Original Message-----\n"
        "From: vertex@example.com\n"
        "Subject: [Vertex] Missing info needed\n\n"
        "Hi Alex,\n\nWhy does this workstream exist?\n"
    )
    _write_reply_eml(path, from_addr="alex@example.com", subject="Re: gap", body=body)

    parsed = parse_reply_eml(path)
    assert "The answer is X." in parsed.body_text
    assert "Why does this workstream exist" not in parsed.body_text


def test_parse_reply_eml_isolates_new_content_above_on_wrote_marker(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    body = (
        "The answer is Y.\n\n"
        "Reference: solicitation-2\n\n"
        "On Mon, Jul 1, 2026 at 9:00 AM Vertex <vertex@example.com> wrote:\n"
        "> Why does this workstream exist?\n"
    )
    _write_reply_eml(path, from_addr="priya@example.com", subject="Re: gap", body=body)

    parsed = parse_reply_eml(path)
    assert "The answer is Y." in parsed.body_text
    assert "Why does this workstream exist" not in parsed.body_text


def test_parse_reply_eml_reference_marker_survives_quote_prefix(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    body = "My answer.\n\n> Reference: solicitation-3\n"
    _write_reply_eml(path, from_addr="alex@example.com", subject="Re: gap", body=body)

    parsed = parse_reply_eml(path)
    assert parsed.reference_marker == "solicitation-3"


def test_parse_reply_eml_no_reference_marker_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    _write_reply_eml(path, from_addr="alex@example.com", subject="Re: gap", body="just an answer, no marker")

    parsed = parse_reply_eml(path)
    assert parsed.reference_marker is None


def test_parse_reply_eml_no_separator_keeps_full_body(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    _write_reply_eml(path, from_addr="alex@example.com", subject="Re: gap", body="Just a plain answer with no quoted content.")

    parsed = parse_reply_eml(path)
    assert parsed.body_text == "Just a plain answer with no quoted content."

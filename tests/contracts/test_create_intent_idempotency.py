"""Contract tests for ADF-W1.2: stable create intent + search-before-create.

INV-ADF-9: retrying or re-approving an intent cannot duplicate the remote
effect. Covers the canonical lost-response fixture: the create POST commits
server-side, but the client never sees the response (crash, timeout,
connection reset). A rerun of the same manifest must adopt the existing
work item via the ``vertex-intent-<id>`` System.Tags marker rather than
creating a second one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from src.core.actuation_outbox import create_task_idempotency_key, enqueue_create_task_intent, outbox_db_path
from src.core.ado_client import ADOClient
from src.core.ado_proposal import (
    ADOUpdateEntry,
    ADOUpdateProposal,
    open_locked_proposal_manifest,
    read_proposal_manifest_from_handle,
    write_proposal_manifest_to_handle,
)
from src.core.exceptions import AuthError, QueryError
from src.core.integration_types import PaginationOutcome
from src.core.ledger.event_log import read_events
from src.core.ledger.durable_outbox_store import load_entry, load_success_receipt
from src.m365.ado_writer import ADOWriter


class _FakeADOBackend:
    """A minimal in-memory ADO work-item store shared by the fake sessions."""

    def __init__(self) -> None:
        self.work_items: dict[int, dict] = {}
        self._next_id = 5001
        self.create_call_count = 0

    def create_work_item(self, patch: list[dict]) -> int:
        self.create_call_count += 1
        fields: dict[str, object] = {}
        for op in patch:
            field_name = op["path"].split("/fields/")[1]
            fields[field_name] = op["value"]
        work_item_id = self._next_id
        self._next_id += 1
        self.work_items[work_item_id] = {"id": work_item_id, "fields": fields}
        return work_item_id

    def query_wiql(self, wiql: str) -> list[int]:
        if "System.Tags" in wiql:
            tag = wiql.split("CONTAINS '")[1].split("'")[0]
            return [wid for wid, wi in self.work_items.items() if tag in str(wi["fields"].get("System.Tags", ""))]
        # Fallback path: area-path + created-date query -- return everything
        # (the test backend has no CreatedDate concept; title equality does
        # the real filtering in this fixture).
        return list(self.work_items.keys())

    def batch_get(self, ids: list[int], fields: tuple[str, ...]) -> list[dict]:
        rows = []
        for work_item_id in ids:
            stored = self.work_items.get(work_item_id)
            if stored is None:
                continue
            rows.append({"id": work_item_id, "fields": {f: stored["fields"].get(f) for f in fields}})
        return rows


class _FakeJsonResponse:
    def __init__(self, payload: dict, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = "{}"
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._payload


class _FakeReadSession:
    """Backs ADOClient._session: WIQL search + workitemsbatch reads."""

    def __init__(self, backend: _FakeADOBackend) -> None:
        self.backend = backend
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs) -> _FakeJsonResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if "wiql" in url:
            ids = self.backend.query_wiql(kwargs["json"]["query"])
            return _FakeJsonResponse({"workItems": [{"id": i} for i in ids]})
        if "workitemsbatch" in url:
            rows = self.backend.batch_get(kwargs["json"]["ids"], tuple(kwargs["json"]["fields"]))
            return _FakeJsonResponse({"value": rows})
        raise AssertionError(f"unexpected read request: {method} {url}")


class _LossyMutationSession:
    """Backs ADOClient._mutation_session: the create POST that loses its response once."""

    def __init__(self, backend: _FakeADOBackend, *, lose_first_response: bool) -> None:
        self.backend = backend
        self.calls: list[dict] = []
        self._lose_next = lose_first_response

    def request(self, method: str, url: str, **kwargs) -> _FakeJsonResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        assert "workitems/$Task" in url, f"only create-task POSTs expected, got {method} {url}"
        work_item_id = self.backend.create_work_item(kwargs["json"])
        if self._lose_next:
            self._lose_next = False
            # Server committed the write; the client never sees the response
            # (network partition / process crash before the read completes).
            raise ConnectionError("simulated lost response after server commit")
        return _FakeJsonResponse({"id": work_item_id})


def _fake_client(backend: _FakeADOBackend, *, lose_first_response: bool) -> ADOClient:
    client = object.__new__(ADOClient)
    client.organization = "contoso"
    client.project = "One"
    client.timeout = 30
    client.auth_method = "pat"
    client.show_progress = False
    client._rest_base_url = "https://dev.azure.com/contoso/One/_apis/wit/"
    client._session = _FakeReadSession(backend)
    client._mutation_session = _LossyMutationSession(backend, lose_first_response=lose_first_response)
    client._headers = lambda: {"Authorization": "Bearer fake", "Accept": "application/json"}
    return client


def _proposal(programs_root: Path) -> tuple[ADOUpdateProposal, Path]:
    task_data = '{"title": "Follow up on rollout risk", "area_path": "One\\\\Contoso\\\\Team", "assigned_to": "alice@example.com"}'
    entry = ADOUpdateEntry(
        work_item_id=0,
        action="create_task",
        field_or_tag="Task",
        current_value=None,
        proposed_value=task_data,
        reason="action-42",
        operation_intent_id="fixed-intent-abc123",
    )
    proposal = ADOUpdateProposal(
        id="prop-idem-1",
        program_id="fixture_prog",
        edition_id="fixture_prog_weekly",
        issue_number=1,
        update_type="action_item",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        expires_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        entries=(entry,),
    )
    manifest_path = programs_root / "fixture_prog" / "ado_proposals" / "prop-idem-1.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("", encoding="utf-8")
    return proposal, manifest_path


def test_lost_response_then_rerun_creates_zero_new_items(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)

    backend = _FakeADOBackend()
    first_client = _fake_client(backend, lose_first_response=True)
    first_writer = ADOWriter(first_client, programs_root=programs_root)

    # First apply: the POST commits server-side (backend has one work item)
    # but the response is lost. ADF-W1.3: this ambiguous outcome is now
    # captured as a durable outbox uncertain_remote_state row (Sec 8.11)
    # rather than propagating as an uncaught exception -- apply_manifest
    # completes normally with the entry marked "failed".
    first_artifacts = first_writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc))
    assert first_artifacts.failed_count == 1
    assert "uncertain" in (first_artifacts.proposal.entries[0].status_reason or "")

    assert backend.create_call_count == 1
    assert len(backend.work_items) == 1

    # The manifest on disk must already reflect the attempted dispatch (the
    # persist-before-dispatch write), even though the dispatch outcome was lost.
    with manifest_path.open("r", encoding="utf-8") as handle:
        reread_proposal, _ = read_proposal_manifest_from_handle(handle)
    reread_entry = reread_proposal.entries[0]
    assert reread_entry.entry_status == "failed"
    assert reread_entry.attempted_at is not None
    assert reread_entry.operation_intent_id == "fixed-intent-abc123"

    # Second apply (a fresh writer/client, as a real rerun would use): the
    # search-before-create step must find the already-created work item via
    # its vertex-intent tag and adopt it -- zero new create POSTs.
    second_client = _fake_client(backend, lose_first_response=False)
    second_writer = ADOWriter(second_client, programs_root=programs_root)
    artifacts = second_writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc))

    assert backend.create_call_count == 1  # unchanged -- no new item created
    assert len(backend.work_items) == 1
    assert artifacts.applied_count == 1
    applied_entry = artifacts.proposal.entries[0]
    assert applied_entry.entry_status == "applied"
    assert applied_entry.work_item_id == next(iter(backend.work_items))
    assert "search-before-create" in (applied_entry.status_reason or "")

    # No mutation POST was issued on the second run.
    assert len(second_client._mutation_session.calls) == 0

    events = read_events("fixture_prog", programs_root=programs_root)
    duplicate_events = [event for event in events if event.event_type == "actuation.duplicate_prevented.v1"]
    assert len(duplicate_events) == 1
    assert duplicate_events[0].payload["operation_intent_id"] == "fixed-intent-abc123"
    assert duplicate_events[0].payload["detection"] == "preflight_search"


def test_never_attempted_entry_skips_search_and_creates_normally(tmp_path: Path) -> None:
    """A first-ever attempt (attempted_at is None) must not pay for a WIQL search."""
    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)

    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=False)
    writer = ADOWriter(client, programs_root=programs_root)

    artifacts = writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc))

    assert artifacts.applied_count == 1
    assert backend.create_call_count == 1
    # No WIQL read call was made (the read session recorded zero calls).
    assert len(client._session.calls) == 0
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=client.organization, project=client.project,
        operation_intent_id=proposal.entries[0].operation_intent_id,
    )
    db = outbox_db_path(proposal.program_id, programs_root=programs_root)
    receipt = json.loads(load_success_receipt(db, key))
    assert load_entry(db, key).status == "completed"
    assert receipt["remote_id"] == artifacts.proposal.entries[0].work_item_id == next(iter(backend.work_items))
    assert receipt["proposal_id"] == proposal.id
    assert receipt["operation_intent_id"] == proposal.entries[0].operation_intent_id
    assert receipt["org"] == client.organization and receipt["project"] == client.project
    assert receipt["remote_response_hash"]


@pytest.mark.parametrize("storage_backend", ["file", "sqlite"])
@pytest.mark.parametrize("repeated_failure", [False, True])
@pytest.mark.parametrize("recovery_state", ["matching", "changed_remote", "existing_dismissed", "existing_deferred"])
def test_create_recovery_restores_missing_audit_review_without_new_effect(
    tmp_path, monkeypatch, storage_backend, repeated_failure, recovery_state,
) -> None:
    import src.m365.ado_writer as writer_module
    from src.core.store_factory import build_signal_store_for_program_id

    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    (programs_root / proposal.program_id / "program.yaml").write_text(json.dumps({
        "schema_version": "2.0", "id": proposal.program_id, "name": "Synthetic",
        "storage_backend": storage_backend,
    }), encoding="utf-8")
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    store = build_signal_store_for_program_id(proposal.program_id, programs_root=programs_root)
    monkeypatch.setattr(writer_module, "build_signal_store_for_program_id", lambda *args, **kwargs: store)
    append_review = store.append_review
    failures = [2 if repeated_failure else 1]

    def fail_review(*args, **kwargs):
        if failures[0]:
            failures[0] -= 1
            raise OSError("synthetic audit review write failure")
        return append_review(*args, **kwargs)

    monkeypatch.setattr(store, "append_review", fail_review)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=False)
    writer = ADOWriter(client, programs_root=programs_root)
    with pytest.raises(OSError, match="audit review write failure"):
        writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc))
    signals = store.read(proposal.program_id)
    assert len(signals) == 1 and not store.read_reviews(proposal.program_id)
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=client.organization, project=client.project,
        operation_intent_id=proposal.entries[0].operation_intent_id,
    )
    db = outbox_db_path(proposal.program_id, programs_root=programs_root)
    retained = load_entry(db, key)
    assert retained.status == "completed"
    recovered_at = datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc)
    if recovery_state == "changed_remote":
        backend.work_items[min(backend.work_items)]["fields"]["System.Title"] = "Changed after creation"
        result = writer.apply_manifest(manifest_path, applied_at=recovered_at)
        assert result.failed_count == 1 and result.applied_count == 0
        assert not store.read_reviews(proposal.program_id)
        assert store.read(proposal.program_id) == signals
        assert load_entry(db, key) == retained and backend.create_call_count == 1
        return
    if recovery_state.startswith("existing_"):
        from src.core.models_v2 import SignalReviewDecision

        original_review = SignalReviewDecision(
            signal_id=signals[0].id, decision=recovery_state.removeprefix("existing_"),
            reviewed_at=datetime(2026, 7, 2, 0, 3, tzinfo=timezone.utc),
            reviewed_by="synthetic_operator", note="Preserve operator decision",
        )
        append_review(proposal.program_id, original_review)
        result = writer.apply_manifest(manifest_path, applied_at=recovered_at)
        assert result.applied_count == 1
        assert store.read_reviews(proposal.program_id)[signals[0].id] == original_review
        assert store.read(proposal.program_id) == signals and backend.create_call_count == 1
        return
    if repeated_failure:
        with pytest.raises(OSError, match="audit review write failure"):
            writer.apply_manifest(manifest_path, applied_at=recovered_at)
        with manifest_path.open(encoding="utf-8") as handle:
            attempted, _ = read_proposal_manifest_from_handle(handle)
        assert attempted.entries[0].entry_status == "pending"
        assert not store.read_reviews(proposal.program_id)
    result = writer.apply_manifest(manifest_path, applied_at=recovered_at)
    assert result.applied_count == 1
    reviews = store.read_reviews(proposal.program_id)
    assert len(reviews) == 1
    assert reviews[signals[0].id].reviewed_at == recovered_at
    assert reviews[signals[0].id].decision == "approved"
    assert store.read(proposal.program_id) == signals
    assert backend.create_call_count == 1 and load_entry(db, key) == retained
    writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, 0, 10, tzinfo=timezone.utc))
    assert store.read(proposal.program_id) == signals and store.read_reviews(proposal.program_id) == reviews
    assert backend.create_call_count == 1


@pytest.mark.parametrize("drift", [
    "title", "assigned_to", "area_path", "description", "proposal_id",
    "intent", "missing_intent", "organization", "project", "missing_row",
    "domain", "correlation", "missing_attempt_time",
])
def test_bound_create_recovery_refuses_retained_intent_drift_before_provider_lookup(
    tmp_path, drift,
) -> None:
    from src.core.store_factory import build_signal_store_for_program_id
    import sqlite3

    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=True)
    first = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    assert first.failed_count == 1
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=client.organization, project=client.project,
        operation_intent_id=proposal.entries[0].operation_intent_id,
    )
    db = outbox_db_path(proposal.program_id, programs_root=programs_root)
    updated = first.proposal
    entry = updated.entries[0]
    if drift in {"title", "assigned_to", "area_path", "description"}:
        task = json.loads(entry.proposed_value)
        task[drift] = "changed value"
        entry = replace(entry, proposed_value=json.dumps(task))
        remote_field = {"title": "System.Title", "assigned_to": "System.AssignedTo",
                        "area_path": "System.AreaPath", "description": "System.Description"}[drift]
        backend.work_items[min(backend.work_items)]["fields"][remote_field] = "changed value"
    elif drift == "proposal_id":
        updated = replace(updated, id="changed-proposal")
    elif drift in {"intent", "missing_intent"}:
        entry = replace(entry, operation_intent_id="changed-intent" if drift == "intent" else None)
        if drift == "intent":
            backend.work_items[min(backend.work_items)]["fields"]["System.Tags"] = "vertex-intent-changed-intent"
    elif drift == "missing_attempt_time":
        task = json.loads(entry.proposed_value)
        task["description"] = "changed after attempted time removal"
        entry = replace(entry, attempted_at=None, proposed_value=json.dumps(task))
    elif drift in {"missing_row", "domain", "correlation"}:
        with sqlite3.connect(db) as connection:
            if drift == "missing_row":
                connection.execute("DELETE FROM outbox WHERE outbox_id = ?", (key,))
            else:
                column = "domain" if drift == "domain" else "correlation_id"
                connection.execute(f"UPDATE outbox SET {column} = ? WHERE outbox_id = ?", ("foreign", key))
    updated = replace(updated, entries=(entry,))
    write_proposal_manifest_to_handle_at_path(manifest_path, updated)
    retained = load_entry(db, key)
    events = tuple(read_events(proposal.program_id, programs_root=programs_root))
    recovery_client = _fake_client(backend, lose_first_response=False)
    if drift == "organization":
        recovery_client.organization = "foreign-org"
    elif drift == "project":
        recovery_client.project = "Foreign"
    result = ADOWriter(recovery_client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc),
    )
    assert result.failed_count == 1 and result.applied_count == 0
    assert "reconcil" in result.proposal.entries[0].status_reason.lower()
    assert not recovery_client._session.calls and not recovery_client._mutation_session.calls
    assert backend.create_call_count == 1
    assert load_entry(db, key) == retained
    assert tuple(read_events(proposal.program_id, programs_root=programs_root)) == events
    store = build_signal_store_for_program_id(proposal.program_id, programs_root=programs_root)
    assert not store.read(proposal.program_id) and not store.read_reviews(proposal.program_id)


@pytest.mark.parametrize("reference", [None, "retained-key"])
def test_create_outbox_reference_codec_and_legacy_default(tmp_path, reference) -> None:
    from src.core.ado_proposal import proposal_to_document, proposal_from_document

    proposal, _ = _proposal(tmp_path / "programs")
    proposal = replace(proposal, entries=(replace(proposal.entries[0], outbox_id=reference),))
    document = proposal_to_document(proposal)
    assert proposal_from_document(document) == proposal
    document["entries"][0].pop("outbox_id")
    assert proposal_from_document(document).entries[0].outbox_id is None


@pytest.mark.parametrize("invalid", [True, 1, [], {}, "", "   "])
def test_create_outbox_reference_codec_refuses_invalid_supplied_values(tmp_path, invalid) -> None:
    from src.core.ado_proposal import proposal_to_document, proposal_from_document

    proposal, _ = _proposal(tmp_path / "programs")
    document = proposal_to_document(proposal)
    document["entries"][0]["outbox_id"] = invalid
    with pytest.raises(ValueError, match="outbox reference"):
        proposal_from_document(document)


def test_create_attempt_persists_retained_reference_before_dispatch_and_replay(tmp_path, monkeypatch) -> None:
    import src.m365.ado_writer as writer_module

    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=False)
    original = client._mutation_session.request
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=client.organization, project=client.project,
        operation_intent_id=proposal.entries[0].operation_intent_id,
    )
    observed_entries = []
    persist = writer_module.write_proposal_manifest_to_handle

    def record_persisted(handle, proposal, **kwargs):
        persist(handle, proposal, **kwargs)
        persisted, _ = read_proposal_manifest_from_handle(handle)
        observed_entries.append(persisted.entries[0])

    monkeypatch.setattr(writer_module, "write_proposal_manifest_to_handle", record_persisted)

    def dispatch(*args, **kwargs):
        assert observed_entries[-1].outbox_id == key
        assert observed_entries[-1].attempted_at is not None
        return original(*args, **kwargs)

    client._mutation_session.request = dispatch
    writer = ADOWriter(client, programs_root=programs_root)
    result = writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc))
    assert result.applied_count == 1
    assert result.proposal.entries[0].outbox_id == key
    replay = writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc))
    assert replay.proposal.entries[0].outbox_id == key and backend.create_call_count == 1


@pytest.mark.parametrize("storage_backend", ["file", "sqlite"])
@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("legacy_signal", [False, True])
def test_created_task_audits_use_resolved_targets_and_preserve_distinct_effects(
    tmp_path, storage_backend, count, legacy_signal,
) -> None:
    from src.core.store_factory import build_signal_store_for_program_id

    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    (programs_root / proposal.program_id / "program.yaml").write_text(json.dumps({
        "schema_version": "2.0", "id": proposal.program_id, "name": "Synthetic",
        "storage_backend": storage_backend,
    }), encoding="utf-8")
    if count == 2:
        task = json.loads(proposal.entries[0].proposed_value)
        task["title"] = "Second distinct task"
        proposal = replace(proposal, entries=proposal.entries + (replace(
            proposal.entries[0], proposed_value=json.dumps(task),
            operation_intent_id="second-intent", reason="action-43",
        ),))
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=False)
    writer = ADOWriter(client, programs_root=programs_root)
    store = build_signal_store_for_program_id(proposal.program_id, programs_root=programs_root)
    if legacy_signal:
        writer._append_audit_signal(
            proposal, proposal.entries[0], applied_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            existing_signals=[], signal_store=store, raw_ref="ado:legacy",
        )
    before_signals = store.read(proposal.program_id)
    before_reviews = store.read_reviews(proposal.program_id)
    result = writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc))
    signals = store.read(proposal.program_id)
    reviews = store.read_reviews(proposal.program_id)
    new_signals = [signal for signal in signals if signal.id not in {old.id for old in before_signals}]
    assert result.applied_count == count and backend.create_call_count == count
    assert len(new_signals) == count
    assert len(reviews) == len(before_reviews) + count
    assert set(signal.metadata["work_item_id"] for signal in new_signals) == set(backend.work_items)
    for signal in new_signals:
        target = signal.metadata["work_item_id"]
        assert signal.id == f"{proposal.id}-{target}-create_task"
        assert signal.entity_refs == (f"WI:{target}",)
        assert signal.raw_ref == f"ado:{target}"
        assert f"WI:{target}" in signal.text and "WI:0" not in signal.text
        assert reviews[signal.id].decision == "approved"
        applied = next(entry for entry in result.proposal.entries if entry.work_item_id == target)
        key = create_task_idempotency_key(
            program_id=proposal.program_id, org=client.organization, project=client.project,
            operation_intent_id=applied.operation_intent_id,
        )
        receipt = json.loads(load_success_receipt(outbox_db_path(proposal.program_id, programs_root=programs_root), key))
        assert receipt["remote_id"] == target
    assert all(signal in signals for signal in before_signals)
    assert all(reviews[key] == value for key, value in before_reviews.items())
    with manifest_path.open(encoding="utf-8") as handle:
        persisted, _ = read_proposal_manifest_from_handle(handle)
    assert {entry.work_item_id for entry in persisted.entries} == set(backend.work_items)
    writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc))
    assert store.read(proposal.program_id) == signals
    assert store.read_reviews(proposal.program_id) == reviews
    assert backend.create_call_count == count


@pytest.mark.parametrize("storage_backend", ["file", "sqlite"])
@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("existing_audit", [False, True])
def test_verified_adoption_audits_resolved_targets_without_new_effects(
    tmp_path, storage_backend, count, existing_audit,
) -> None:
    from src.core.store_factory import build_signal_store_for_program_id

    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    (programs_root / proposal.program_id / "program.yaml").write_text(json.dumps({
        "schema_version": "2.0", "id": proposal.program_id, "name": "Synthetic",
        "storage_backend": storage_backend,
    }), encoding="utf-8")
    if count == 2:
        task = json.loads(proposal.entries[0].proposed_value)
        task["title"] = "Second recovered task"
        proposal = replace(proposal, entries=proposal.entries + (replace(
            proposal.entries[0], proposed_value=json.dumps(task),
            operation_intent_id="second-intent", reason="action-43",
        ),))
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=False)
    original = client._mutation_session.request

    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise ConnectionError("synthetic committed response lost")

    client._mutation_session.request = lost
    writer = ADOWriter(client, programs_root=programs_root)
    failed = writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc))
    assert failed.failed_count == count
    store = build_signal_store_for_program_id(proposal.program_id, programs_root=programs_root)
    prior_signals = []
    if existing_audit:
        writer._append_audit_signal(
            proposal, replace(failed.proposal.entries[0], work_item_id=min(backend.work_items)),
            applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
            existing_signals=[], signal_store=store, raw_ref=f"ado:{min(backend.work_items)}",
        )
        prior_signals = store.read(proposal.program_id)
    retained = {
        entry.operation_intent_id: load_entry(
            outbox_db_path(proposal.program_id, programs_root=programs_root),
            create_task_idempotency_key(
                program_id=proposal.program_id, org=client.organization, project=client.project,
                operation_intent_id=entry.operation_intent_id,
            ),
        ) for entry in failed.proposal.entries
    }
    recovery_client = _fake_client(backend, lose_first_response=False)
    recovery = ADOWriter(recovery_client, programs_root=programs_root)
    result = recovery.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc))
    signals = store.read(proposal.program_id)
    reviews = store.read_reviews(proposal.program_id)
    assert result.applied_count == count and backend.create_call_count == count
    assert not recovery_client._mutation_session.calls
    assert len(signals) == len(reviews) == count
    assert {signal.metadata["work_item_id"] for signal in signals} == set(backend.work_items)
    for signal in signals:
        target = signal.metadata["work_item_id"]
        assert signal.entity_refs == (f"WI:{target}",) and signal.raw_ref == f"ado:{target}"
        assert reviews[signal.id].decision == "approved"
        if signal not in prior_signals:
            assert signal.text == f"Adopted existing WI:{target} from proposal {proposal.id}."
            assert signal.metadata["adopted_existing"] is True
        applied = next(entry for entry in result.proposal.entries if entry.work_item_id == target)
        key = create_task_idempotency_key(
            program_id=proposal.program_id, org=client.organization, project=client.project,
            operation_intent_id=applied.operation_intent_id,
        )
        db = outbox_db_path(proposal.program_id, programs_root=programs_root)
        assert load_entry(db, key) == retained[applied.operation_intent_id]
        assert load_success_receipt(db, key) is None
    assert all(signal in signals for signal in prior_signals)
    recovery.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, 0, 10, tzinfo=timezone.utc))
    assert store.read(proposal.program_id) == signals and store.read_reviews(proposal.program_id) == reviews
    assert backend.create_call_count == count


@pytest.mark.parametrize("status", [101, 301, 302, 307, 308, 408, 500, 502, 503, 504])
def test_commit_before_ambiguous_http_response_is_uncertain_then_adopted(tmp_path, monkeypatch, status) -> None:
    from src.core.alerts import read_alerts
    from src.core.ledger.durable_outbox_store import lease_next
    from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN

    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=False)
    original = client._mutation_session.request

    def ambiguous(*args, **kwargs):
        response = original(*args, **kwargs)
        response.status_code = status
        response.text = "private-provider-response"
        return response

    monkeypatch.setattr(client._mutation_session, "request", ambiguous)
    result = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=client.organization, project=client.project,
        operation_intent_id=proposal.entries[0].operation_intent_id,
    )
    db = outbox_db_path(proposal.program_id, programs_root=programs_root)
    retained = load_entry(db, key)
    assert result.failed_count == 1 and result.applied_count == 0
    assert retained.status == "uncertain_remote_state"
    assert "private-provider-response" not in retained.failure_reason
    assert load_success_receipt(db, key) is None
    assert len(read_alerts(proposal.program_id, programs_root=programs_root)) == 1
    assert lease_next(db, ACTUATION_DISPATCH_DOMAIN, owner="retry") is None
    assert backend.create_call_count == 1
    assert client._mutation_session.calls[0]["allow_redirects"] is False
    recovery_client = _fake_client(backend, lose_first_response=False)
    adopted = ADOWriter(recovery_client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc),
    )
    assert adopted.applied_count == 1
    assert backend.create_call_count == 1 and recovery_client._mutation_session.calls == []
    assert load_entry(db, key) == retained


@pytest.mark.parametrize("storage_backend", ["file", "sqlite"])
@pytest.mark.parametrize("outcome", ["rejected", "lost_response", "invalid_id"])
def test_unsuccessful_creates_do_not_emit_success_target_audits(tmp_path, monkeypatch, storage_backend, outcome) -> None:
    from src.core.store_factory import build_signal_store_for_program_id

    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    (programs_root / proposal.program_id / "program.yaml").write_text(json.dumps({
        "schema_version": "2.0", "id": proposal.program_id, "name": "Synthetic",
        "storage_backend": storage_backend,
    }), encoding="utf-8")
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=outcome == "lost_response")
    if outcome == "rejected":
        monkeypatch.setattr(client._mutation_session, "request",
                            lambda *args, **kwargs: _FakeJsonResponse({}, status_code=400))
    elif outcome == "invalid_id":
        original = client._mutation_session.request
        def invalid(*args, **kwargs):
            original(*args, **kwargs)
            return _FakeJsonResponse({"id": None})
        monkeypatch.setattr(client._mutation_session, "request", invalid)
    result = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    store = build_signal_store_for_program_id(proposal.program_id, programs_root=programs_root)
    assert result.failed_count == 1 and result.applied_count == 0
    assert not store.read(proposal.program_id)
    assert not store.read_reviews(proposal.program_id)
    assert backend.create_call_count == (0 if outcome == "rejected" else 1)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 412, 422, 429])
def test_definite_http_create_rejection_remains_terminal_failure(tmp_path, monkeypatch, status) -> None:
    from src.core.alerts import read_alerts

    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=False)
    calls = []

    def reject(*args, **kwargs):
        calls.append(kwargs)
        return _FakeJsonResponse({}, status_code=status)

    monkeypatch.setattr(client._mutation_session, "request", reject)
    result = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=client.organization, project=client.project,
        operation_intent_id=proposal.entries[0].operation_intent_id,
    )
    db = outbox_db_path(proposal.program_id, programs_root=programs_root)
    assert result.failed_count == 1 and result.applied_count == 0
    assert load_entry(db, key).status == "failed_terminal"
    assert load_success_receipt(db, key) is None
    assert not read_alerts(proposal.program_id, programs_root=programs_root)
    assert backend.create_call_count == 0 and len(calls) == 1


@pytest.mark.parametrize("response_kind", ["empty", "invalid_json", "list", "null"])
def test_committed_create_with_unusable_success_body_is_uncertain(tmp_path, monkeypatch, response_kind) -> None:
    from src.core.alerts import read_alerts

    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=False)
    original = client._mutation_session.request

    def unusable(*args, **kwargs):
        response = original(*args, **kwargs)
        if response_kind == "empty":
            response.text = ""
        elif response_kind == "invalid_json":
            def malformed():
                raise ValueError("private-provider-response")
            response.json = malformed
        else:
            response._payload = [] if response_kind == "list" else None
        return response

    monkeypatch.setattr(client._mutation_session, "request", unusable)
    result = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=client.organization, project=client.project,
        operation_intent_id=proposal.entries[0].operation_intent_id,
    )
    db = outbox_db_path(proposal.program_id, programs_root=programs_root)
    entry = load_entry(db, key)
    assert result.failed_count == 1 and result.applied_count == 0
    assert entry.status == "uncertain_remote_state"
    assert "private-provider-response" not in entry.failure_reason
    assert backend.create_call_count == 1
    assert load_success_receipt(db, key) is None
    assert len(read_alerts(proposal.program_id, programs_root=programs_root)) == 1


@pytest.mark.parametrize("revision", ["absent", None, 1, 7, True, False, 0, -1, "7", 1.5])
def test_actual_create_revision_is_observed_not_fabricated(tmp_path, monkeypatch, revision) -> None:
    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=False)
    request = client._mutation_session.request

    def respond(*args, **kwargs):
        response = request(*args, **kwargs).json()
        if revision != "absent":
            response["rev"] = revision
        return _FakeJsonResponse(response)

    monkeypatch.setattr(client._mutation_session, "request", respond)
    artifacts = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=client.organization, project=client.project,
        operation_intent_id=proposal.entries[0].operation_intent_id,
    )
    db = outbox_db_path(proposal.program_id, programs_root=programs_root)
    valid = revision in ("absent", None) or (type(revision) is int and revision > 0)
    assert backend.create_call_count == 1
    with manifest_path.open(encoding="utf-8") as handle:
        retained, _ = read_proposal_manifest_from_handle(handle)
    if valid:
        expected = None if revision in ("absent", None) else revision
        assert artifacts.applied_count == 1
        assert retained.entries[0].remote_rev == expected
        assert artifacts.proposal.entries[0].remote_rev == expected
        receipt = json.loads(load_success_receipt(db, key))
        assert "remote_rev" in receipt and receipt["remote_rev"] == expected
    else:
        assert artifacts.failed_count == 1 and artifacts.applied_count == 0
        assert "reconcile" in retained.entries[0].status_reason
        assert load_entry(db, key).status == "uncertain_remote_state"
        assert load_success_receipt(db, key) is None
        assert retained.entries[0].remote_rev is None


@pytest.mark.parametrize("remote_id", [None, True, 0, -1, "5001", 1.5])
def test_actual_manifest_unknown_create_acknowledgement_cannot_complete(tmp_path: Path, monkeypatch, remote_id) -> None:
    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=False)
    request = client._mutation_session.request

    def invalid_response(*args, **kwargs):
        request(*args, **kwargs)
        return _FakeJsonResponse({"id": remote_id})

    monkeypatch.setattr(client._mutation_session, "request", invalid_response)
    artifacts = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    assert artifacts.failed_count == 1 and artifacts.applied_count == 0
    assert "reconcile" in artifacts.proposal.entries[0].status_reason
    assert backend.create_call_count == 1
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=client.organization, project=client.project,
        operation_intent_id=proposal.entries[0].operation_intent_id,
    )
    db = outbox_db_path(proposal.program_id, programs_root=programs_root)
    assert load_entry(db, key).status == "uncertain_remote_state"
    assert load_success_receipt(db, key) is None


@pytest.mark.parametrize("storage_backend", ["file", "sqlite"])
@pytest.mark.parametrize("retained_uncertainty", [False, True])
@pytest.mark.parametrize("failure", [
    "empty", "title_only", "multiple_markers", "marker_prefix", "wrong_title",
    "query_error", "auth_error", "capped", "missing_outcome", "wrong_count",
    "duplicate_outcome", "invalid_outcome", "invalid_count", "invalid_pages", "zero_pages", "invalid_truncation",
    "batch_error", "empty_batch", "foreign_batch", "duplicate_batch", "invalid_fields", "missing_fields",
    "invalid_tags", "invalid_title",
])
def test_unproven_create_recovery_cannot_adopt_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, retained_uncertainty: bool,
    storage_backend: str,
) -> None:
    from src.core.store_factory import build_signal_store_for_program_id

    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    (programs_root / proposal.program_id / "program.yaml").write_text(json.dumps({
        "schema_version": "2.0", "id": proposal.program_id, "name": "Synthetic",
        "storage_backend": storage_backend,
    }), encoding="utf-8")
    backend = _FakeADOBackend()
    if retained_uncertainty:
        write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
        first = ADOWriter(_fake_client(backend, lose_first_response=True), programs_root=programs_root)
        first_artifacts = first.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc))
        assert first_artifacts.failed_count == 1
        backend.work_items.clear()
    else:
        proposal = replace(proposal, entries=(
            replace(proposal.entries[0], attempted_at=datetime(2026, 7, 2, tzinfo=timezone.utc)),
        ))
        write_proposal_manifest_to_handle_at_path(manifest_path, proposal)

    marker = "vertex-intent-fixed-intent-abc123"
    if failure != "empty":
        fields = {"System.Title": "Wrong title" if failure == "wrong_title" else "Follow up on rollout risk"}
        if failure != "title_only":
            fields["System.Tags"] = marker + ("-different" if failure == "marker_prefix" else "")
        backend.work_items[7001] = {"id": 7001, "fields": fields}
        if failure == "multiple_markers":
            backend.work_items[7002] = {"id": 7002, "fields": dict(fields)}
    client = _fake_client(backend, lose_first_response=False)
    if failure in {"query_error", "auth_error"}:
        def fail_query(*args, **kwargs):
            raise (AuthError if failure == "auth_error" else QueryError)(
                "synthetic-private-provider-detail " + "x" * 1000,
            )
        monkeypatch.setattr(client, "execute_wiql", fail_query)
    elif failure in {
        "capped", "missing_outcome", "wrong_count", "duplicate_outcome", "invalid_outcome",
        "invalid_count", "invalid_pages", "zero_pages", "invalid_truncation",
    }:
        def incomplete_query(wiql, *, on_pagination=None):
            if on_pagination is not None and failure != "missing_outcome":
                outcome = PaginationOutcome(
                    total_fetched=True if failure == "invalid_count" else 2 if failure == "wrong_count" else 1,
                    page_count=True if failure == "invalid_pages" else 0 if failure == "zero_pages" else 1,
                    is_truncated=0 if failure == "invalid_truncation" else failure == "capped",
                )
                on_pagination(None if failure == "invalid_outcome" else outcome)
                if failure == "duplicate_outcome":
                    on_pagination(outcome)
            return [7001]
        monkeypatch.setattr(client, "execute_wiql", incomplete_query)
    if failure in {
        "batch_error", "empty_batch", "foreign_batch", "duplicate_batch", "invalid_fields",
        "missing_fields", "invalid_tags", "invalid_title",
    }:
        def invalid_batch(ids, *, fields):
            if failure == "batch_error":
                raise QueryError("synthetic readback unavailable")
            row = {"id": 7002 if failure == "foreign_batch" else 7001,
                   "fields": dict(backend.work_items[7001]["fields"])}
            if failure == "invalid_fields":
                row["fields"] = ["not a field mapping"]
            elif failure == "missing_fields":
                row.pop("fields")
            elif failure == "invalid_tags":
                row["fields"]["System.Tags"] = None
            elif failure == "invalid_title":
                row["fields"]["System.Title"] = None
            return [] if failure == "empty_batch" else [row, row] if failure == "duplicate_batch" else [row]
        monkeypatch.setattr(client, "query_work_items_batch", invalid_batch)
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=client.organization, project=client.project,
        operation_intent_id="fixed-intent-abc123",
    )
    db = outbox_db_path(proposal.program_id, programs_root=programs_root)
    before_outbox = load_entry(db, key) if db.exists() else None
    before_calls = backend.create_call_count
    before_events = tuple(read_events(proposal.program_id, programs_root=programs_root))

    artifacts = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc),
    )

    assert artifacts.applied_count == 0
    assert artifacts.failed_count == 1
    store = build_signal_store_for_program_id(proposal.program_id, programs_root=programs_root)
    assert not store.read(proposal.program_id)
    assert store.read_reviews(proposal.program_id) == {}
    reason = artifacts.proposal.entries[0].status_reason or ""
    assert "reconcil" in reason.lower()
    assert "synthetic-private-provider-detail" not in reason
    assert len(reason) < 200
    assert client._mutation_session.calls == []
    assert backend.create_call_count == before_calls
    assert (load_entry(db, key) if db.exists() else None) == before_outbox
    assert tuple(read_events(proposal.program_id, programs_root=programs_root)) == before_events
    with manifest_path.open(encoding="utf-8") as handle:
        persisted, _ = read_proposal_manifest_from_handle(handle)
    assert persisted.entries[0].entry_status == "failed"


@pytest.mark.parametrize("area_path", [None, "Project\\Rollout"])
def test_create_recovery_requires_exact_tag_but_preserves_normalized_title(
    tmp_path: Path, area_path: str | None,
) -> None:
    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    task = json.loads(proposal.entries[0].proposed_value)
    if area_path is None:
        task.pop("area_path", None)
    else:
        task["area_path"] = area_path
    proposal = replace(proposal, entries=(
        replace(proposal.entries[0], proposed_value=json.dumps(task), attempted_at=datetime(2026, 7, 2, tzinfo=timezone.utc)),
    ))
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    backend.work_items[7001] = {"id": 7001, "fields": {
        "System.Title": "  FOLLOW UP  on rollout risk  ",
        "System.Tags": "other; vertex-intent-fixed-intent-abc123; another",
        "System.AssignedTo": task["assigned_to"],
        **({"System.AreaPath": area_path} if area_path is not None else {}),
    }}
    client = _fake_client(backend, lose_first_response=False)

    artifacts = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc),
    )

    assert artifacts.applied_count == 1
    assert artifacts.proposal.entries[0].work_item_id == 7001
    assert backend.create_call_count == 0
    assert client._mutation_session.calls == []


@pytest.mark.parametrize("storage_backend", ["file", "sqlite"])
@pytest.mark.parametrize(("field", "invalid"), [
    ("System.Description", None), ("System.Description", "Changed description"),
    ("System.AreaPath", None), ("System.AreaPath", "One\\Contoso\\Team\\Child"),
    ("System.IterationPath", None), ("System.IterationPath", "One\\Other"),
    ("System.AssignedTo", None), ("System.AssignedTo", "bob@example.com"),
    ("System.AssignedTo", {"displayName": "alice@example.com"}),
    ("System.AssignedTo", {"uniqueName": 42}),
    ("Microsoft.VSTS.Common.Priority", None), ("Microsoft.VSTS.Common.Priority", 3),
    ("Microsoft.VSTS.Common.Priority", True), ("Microsoft.VSTS.Common.Priority", 2.0),
    ("Microsoft.VSTS.Common.Priority", "2"),
    ("Microsoft.VSTS.Scheduling.TargetDate", None),
    ("Microsoft.VSTS.Scheduling.TargetDate", "2026-07-05T00:00:00Z"),
])
def test_recovery_refuses_unproven_desired_fields(tmp_path, storage_backend, field, invalid) -> None:
    from src.core.store_factory import build_signal_store_for_program_id

    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    (programs_root / proposal.program_id / "program.yaml").write_text(json.dumps({
        "schema_version": "2.0", "id": proposal.program_id, "name": "Synthetic",
        "storage_backend": storage_backend,
    }), encoding="utf-8")
    task = json.loads(proposal.entries[0].proposed_value)
    task.update(description="<p>Retained task</p>", iteration_path="One\\Sprint",
                priority=2, target_date="2026-07-04T00:00:00Z")
    proposal = replace(proposal, entries=(replace(
        proposal.entries[0], proposed_value=json.dumps(task),
    ),))
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=True)
    writer = ADOWriter(client, programs_root=programs_root)
    assert writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc)).failed_count == 1
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=client.organization, project=client.project,
        operation_intent_id=proposal.entries[0].operation_intent_id,
    )
    db = outbox_db_path(proposal.program_id, programs_root=programs_root)
    retained = load_entry(db, key)
    events = tuple(read_events(proposal.program_id, programs_root=programs_root))
    remote_fields = backend.work_items[min(backend.work_items)]["fields"]
    if invalid is None:
        remote_fields.pop(field)
    else:
        remote_fields[field] = invalid
    recovered = writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc))
    assert recovered.failed_count == 1 and recovered.applied_count == 0
    assert "reconcil" in recovered.proposal.entries[0].status_reason.lower()
    store = build_signal_store_for_program_id(proposal.program_id, programs_root=programs_root)
    assert not store.read(proposal.program_id) and not store.read_reviews(proposal.program_id)
    assert tuple(read_events(proposal.program_id, programs_root=programs_root)) == events
    assert load_entry(db, key) == retained and load_success_receipt(db, key) is None
    assert backend.create_call_count == 1


@pytest.mark.parametrize("assignee", [42, "   "])
def test_recovery_refuses_invalid_requested_assignee_identity(tmp_path, assignee) -> None:
    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    task = json.loads(proposal.entries[0].proposed_value)
    task["assigned_to"] = assignee
    proposal = replace(proposal, entries=(replace(
        proposal.entries[0], proposed_value=json.dumps(task),
    ),))
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=True)
    writer = ADOWriter(client, programs_root=programs_root)
    assert writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc)).failed_count == 1
    before = tuple(read_events(proposal.program_id, programs_root=programs_root))
    result = writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc))
    assert result.failed_count == 1 and result.applied_count == 0
    assert backend.create_call_count == 1
    assert tuple(read_events(proposal.program_id, programs_root=programs_root)) == before


@pytest.mark.parametrize("assignee", [
    "  ALICE@EXAMPLE.COM ", {"uniqueName": "ALICE@example.com", "displayName": "Not identity"},
])
@pytest.mark.parametrize("optional_fields", [False, True])
def test_recovery_checks_sent_fields_and_explicit_assignee_identity(tmp_path, assignee, optional_fields) -> None:
    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    task = json.loads(proposal.entries[0].proposed_value)
    if optional_fields:
        task.update(description="<p>Retained task</p>", iteration_path="One\\Sprint",
                    priority=2, target_date="2026-07-04T00:00:00Z")
    proposal = replace(proposal, entries=(replace(
        proposal.entries[0], proposed_value=json.dumps(task),
    ),))
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=True)
    writer = ADOWriter(client, programs_root=programs_root)
    assert writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc)).failed_count == 1
    backend.work_items[min(backend.work_items)]["fields"]["System.AssignedTo"] = assignee
    result = writer.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc))
    assert result.applied_count == 1 and backend.create_call_count == 1
    batch = next(call for call in client._session.calls if "workitemsbatch" in call["url"])
    expected_fields = {"System.Title", "System.Tags", "System.AssignedTo", "System.AreaPath"}
    if optional_fields:
        expected_fields.update({"System.Description", "System.IterationPath",
                                "Microsoft.VSTS.Common.Priority", "Microsoft.VSTS.Scheduling.TargetDate"})
    assert set(batch["json"]["fields"]) == expected_fields
    assert len(client._session.calls) == 2


@pytest.mark.parametrize("revision", ["absent", None, 1, 9, True, False, 0, -1, "9", 1.5])
def test_recovery_adoption_retains_observed_revision(tmp_path, monkeypatch, revision) -> None:
    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    first = ADOWriter(_fake_client(backend, lose_first_response=True), programs_root=programs_root)
    assert first.apply_manifest(manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc)).failed_count == 1
    client = _fake_client(backend, lose_first_response=False)
    batch = client.query_work_items_batch

    def readback(*args, **kwargs):
        rows = batch(*args, **kwargs)
        if revision != "absent":
            rows[0]["rev"] = revision
        return rows

    monkeypatch.setattr(client, "query_work_items_batch", readback)
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=client.organization, project=client.project,
        operation_intent_id=proposal.entries[0].operation_intent_id,
    )
    db = outbox_db_path(proposal.program_id, programs_root=programs_root)
    before = load_entry(db, key)
    events = tuple(read_events(proposal.program_id, programs_root=programs_root))
    result = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc),
    )
    with manifest_path.open(encoding="utf-8") as handle:
        persisted, _ = read_proposal_manifest_from_handle(handle)
    if revision in ("absent", None) or (type(revision) is int and revision > 0):
        expected = None if revision in ("absent", None) else revision
        assert result.applied_count == 1
        assert result.proposal.entries[0].remote_rev == expected
        assert persisted.entries[0].remote_rev == expected
        assert persisted.entries[0].work_item_id == next(iter(backend.work_items))
    else:
        assert result.failed_count == 1 and result.applied_count == 0
        assert "reconcil" in persisted.entries[0].status_reason.lower()
        assert tuple(read_events(proposal.program_id, programs_root=programs_root)) == events
    assert backend.create_call_count == 1 and client._mutation_session.calls == []
    assert load_entry(db, key) == before
    assert load_success_receipt(db, key) is None


@pytest.mark.parametrize(("hit", "row"), [
    (None, {"id": None}), (True, {"id": 1}), (False, {"id": 0}), (0, {"id": 0}),
    (-1, {"id": -1}), ("7001", {"id": "7001"}), (1.5, {"id": 1.5}),
    (1, {"id": True}), (7001, {"id": "7001"}), (7001, {"id": 7001.0}),
    (7001, {}), (7001, {"id": None}), (7001, None), (7001, []), (7001, "row"),
])
def test_recovery_adoption_refuses_malformed_identity(tmp_path, monkeypatch, hit, row) -> None:
    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    proposal = replace(proposal, entries=(replace(
        proposal.entries[0], attempted_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    ),))
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=False)

    def hits(wiql, *, on_pagination):
        on_pagination(PaginationOutcome(total_fetched=1, page_count=1, is_truncated=False))
        return [hit]

    if isinstance(row, dict):
        row = {**row, "fields": {"System.Title": "Follow up on rollout risk",
                               "System.Tags": "vertex-intent-fixed-intent-abc123"}}
    monkeypatch.setattr(client, "execute_wiql", hits)
    monkeypatch.setattr(client, "query_work_items_batch", lambda *args, **kwargs: [row])
    result = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    assert result.failed_count == 1 and result.applied_count == 0
    assert "reconcil" in result.proposal.entries[0].status_reason.lower()
    assert backend.create_call_count == 0 and client._mutation_session.calls == []
    assert not read_events(proposal.program_id, programs_root=programs_root)
    assert not outbox_db_path(proposal.program_id, programs_root=programs_root).exists()


@pytest.mark.parametrize("invalid", ["foreign_org", "foreign_project", "missing_org", "missing_project", "nonobject"])
def test_create_backlog_cannot_use_another_client_destination(tmp_path: Path, invalid: str) -> None:
    programs_root = tmp_path / "programs"
    proposal, manifest_path = _proposal(programs_root)
    write_proposal_manifest_to_handle_at_path(manifest_path, proposal)
    backend = _FakeADOBackend()
    client = _fake_client(backend, lose_first_response=False)
    fields = {
        "org": "previous-org" if invalid == "foreign_org" else client.organization,
        "project": "Previous" if invalid == "foreign_project" else client.project,
        "operation_intent_id": "retained-intent",
        "title": "Retained destination only",
    }
    key = create_task_idempotency_key(
        program_id=proposal.program_id, org=fields["org"], project=fields["project"],
        operation_intent_id="retained-intent",
    )
    if invalid.startswith("missing_"):
        fields.pop(invalid.removeprefix("missing_"))
    enqueue_create_task_intent(
        program_id=proposal.program_id, idempotency_key=key,
        operation_intent_id="retained-intent", proposal_id="retained-proposal",
        payload_json="[]" if invalid == "nonobject" else json.dumps(fields), programs_root=programs_root,
    )

    artifacts = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )

    assert artifacts.applied_count == 1
    assert backend.create_call_count == 1
    assert [item["fields"]["System.Title"] for item in backend.work_items.values()] == ["Follow up on rollout risk"]
    assert len(client._mutation_session.calls) == 1
    assert client._mutation_session.calls[0]["url"] == client._rest_base_url + "workitems/$Task?api-version=7.1"
    retained = load_entry(outbox_db_path(proposal.program_id, programs_root=programs_root), key)
    assert retained is not None
    assert retained.status == "failed_terminal"
    assert "destination" in (retained.failure_reason or "")


def write_proposal_manifest_to_handle_at_path(path: Path, proposal: ADOUpdateProposal) -> None:
    with open_locked_proposal_manifest(path) as handle:
        write_proposal_manifest_to_handle(handle, proposal, proposal_status="pending")

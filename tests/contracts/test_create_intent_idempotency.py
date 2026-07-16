"""Contract tests for ADF-W1.2: stable create intent + search-before-create.

INV-ADF-9: retrying or re-approving an intent cannot duplicate the remote
effect. Covers the canonical lost-response fixture: the create POST commits
server-side, but the client never sees the response (crash, timeout,
connection reset). A rerun of the same manifest must adopt the existing
work item via the ``vertex-intent-<id>`` System.Tags marker rather than
creating a second one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.ado_client import ADOClient
from src.core.ado_proposal import (
    ADOUpdateEntry,
    ADOUpdateProposal,
    open_locked_proposal_manifest,
    read_proposal_manifest_from_handle,
    write_proposal_manifest_to_handle,
)
from src.core.exceptions import QueryError
from src.core.ledger.event_log import read_events
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


def write_proposal_manifest_to_handle_at_path(path: Path, proposal: ADOUpdateProposal) -> None:
    with open_locked_proposal_manifest(path) as handle:
        write_proposal_manifest_to_handle(handle, proposal, proposal_status="pending")

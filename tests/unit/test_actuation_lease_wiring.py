"""ADF-W1.10: smoke tests that ``ado apply``, ``ado rollback``, and the
autonomy-audit migration script are wired to their Appendix A.11 workspace
lease domains -- contention on the domain surfaces as a clear "busy" result
rather than crashing or silently racing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.core.ado_proposal import ADOUpdateEntry, ADOUpdateProposal, write_proposal_manifest
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN, LeaseHeldByAnotherOwner, acquire_lease
from src.m365.ado_writer import ADOWriter


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> "_FakeResponse":
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeResponse({"id": 42})


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = "{}"
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeADOClient:
    def __init__(self, *, rows_by_id: dict[int, dict[str, Any]]) -> None:
        self.rows_by_id = rows_by_id
        self.timeout = 30
        self.organization = "contoso"
        self.project = "One"
        self._rest_base_url = "https://dev.azure.com/contoso/One/_apis/wit/"
        self._session = _FakeSession()
        self._mutation_session = self._session

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer fake"}

    def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, Any]]:
        rows = []
        for work_item_id in work_item_ids:
            stored = self.rows_by_id.get(work_item_id)
            if stored is None:
                continue
            all_fields = dict(stored.get("fields", {}))
            rows.append({"id": work_item_id, "rev": stored.get("rev"), "fields": {f: all_fields[f] for f in fields if f in all_fields}})
        return rows

    def list_work_item_comments(self, work_item_id: int) -> list[dict[str, Any]]:
        return []


def _comment_proposal() -> ADOUpdateProposal:
    applied_at = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    return ADOUpdateProposal(
        id="prop-lease-smoke",
        program_id="fixture_prog",
        edition_id="fixture_prog_weekly",
        issue_number=1,
        update_type="comment",
        created_at=applied_at - timedelta(hours=1),
        expires_at=applied_at + timedelta(hours=70),
        entries=(
            ADOUpdateEntry(
                work_item_id=2001,
                action="add_comment",
                field_or_tag="comment",
                current_value=None,
                proposed_value="Vertex smoke comment.",
                reason="lease wiring smoke test",
                revision_id=3,
            ),
        ),
    )


def test_ado_apply_add_comment_busy_lease_surfaces_as_failed_entry(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposal = _comment_proposal()
    manifest_path = write_proposal_manifest(proposal, programs_root=programs_root)
    client = _FakeADOClient(rows_by_id={2001: {"rev": 3, "fields": {"System.Id": 2001, "System.Rev": 3}}})

    # Another actor holds the actuation_dispatch lease for this program.
    acquire_lease("fixture_prog", "other-worker", mutation_domain=ACTUATION_DISPATCH_DOMAIN, ttl_seconds=300, programs_root=programs_root)

    artifacts = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=datetime(2026, 7, 12, 12, 5, tzinfo=timezone.utc)
    )
    assert artifacts.failed_count == 1
    assert artifacts.applied_count == 0
    entry = artifacts.proposal.entries[0]
    assert "lease busy" in (entry.status_reason or "")
    # The mutation was never actually issued while contended.
    assert client._session.calls == []


def test_ado_rollback_add_comment_busy_lease_surfaces_as_failed_result(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposal = _comment_proposal()
    applied_entry = ADOUpdateEntry(
        work_item_id=2001,
        action="add_comment",
        field_or_tag="comment",
        current_value=None,
        proposed_value="Vertex smoke comment.",
        reason="lease wiring smoke test",
        revision_id=3,
        entry_status="applied",
        remote_rev=3,
    )
    from dataclasses import replace

    proposal = replace(proposal, entries=(applied_entry,))
    manifest_path = write_proposal_manifest(proposal, programs_root=programs_root)
    client = _FakeADOClient(rows_by_id={2001: {"rev": 3, "fields": {"System.Id": 2001, "System.Rev": 3}}})

    acquire_lease("fixture_prog", "other-worker", mutation_domain=ACTUATION_DISPATCH_DOMAIN, ttl_seconds=300, programs_root=programs_root)

    artifacts = ADOWriter(client, programs_root=programs_root).rollback_manifest(
        manifest_path, action_id="rollback-smoke", rolled_back_at=datetime(2026, 7, 12, 12, 10, tzinfo=timezone.utc)
    )
    assert artifacts.failed_count == 1
    result = artifacts.results[0]
    assert result.status == "failed"
    assert "lease busy" in (result.status_reason or "")
    assert client._session.calls == []


def test_migrate_autonomy_audit_busy_lease_raises(tmp_path: Path) -> None:
    from src.core.analytics_store import get_program_autonomy_audit_path
    from scripts.migrate_autonomy_audit import migrate_autonomy_audit

    programs_root = tmp_path / "programs"
    path = get_program_autonomy_audit_path("fixture_prog", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema_version": "1.0"}\n', encoding="utf-8")

    acquire_lease("fixture_prog", "other-worker", mutation_domain="state_migration", ttl_seconds=300, programs_root=programs_root)

    with pytest.raises(LeaseHeldByAnotherOwner):
        migrate_autonomy_audit("fixture_prog", programs_root=programs_root)


def test_migrate_autonomy_audit_main_reports_busy_lease_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from src.core.analytics_store import get_program_autonomy_audit_path
    from scripts.migrate_autonomy_audit import main

    programs_root = tmp_path / "programs"
    path = get_program_autonomy_audit_path("fixture_prog", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema_version": "1.0"}\n', encoding="utf-8")

    acquire_lease("fixture_prog", "other-worker", mutation_domain="state_migration", ttl_seconds=300, programs_root=programs_root)

    exit_code = main(["--program", "fixture_prog", "--programs-root", str(programs_root)])
    assert exit_code == 1
    assert "state_migration operation is in progress" in capsys.readouterr().out

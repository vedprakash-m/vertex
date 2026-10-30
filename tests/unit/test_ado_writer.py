from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import portalocker
import pytest
import yaml

from src.commands.ado import apply_ado_proposal
from src.core.ado_proposal import ADOUpdateEntry, ADOUpdateProposal, ProposalManifestLockedError, proposal_from_document, read_proposal_manifest, write_proposal_manifest
from src.core.exceptions import AuthError, CredentialExpired, QueryError, QueryTimeoutError
from src.core.integration_types import PaginationOutcome
from src.core.journal import load_latest_review_decisions, read_signals
from src.core.models_v2 import ADOConfig, Program
from src.core.sqlite_stores import SQLiteSignalStore
from src.m365.ado_writer import ADOApplyArtifacts, ADOWriter


def _apply_ado_proposal_with_fake_client(
    proposal_reference: str,
    *,
    applied_at: datetime,
    programs_root: Path,
    program: Program,
    client: _FakeADOClient,
) -> ADOApplyArtifacts:
    def program_loader(program_id: str, root: Path) -> tuple[Program, tuple[Any, ...]]:
        assert program_id == program.id
        assert root == programs_root
        return program, ()

    def client_factory(loaded_program: Program) -> _FakeADOClient:
        assert loaded_program is program
        return client

    return apply_ado_proposal(
        proposal_reference,
        applied_at=applied_at,
        programs_root=programs_root,
        program_loader=program_loader,
        client_factory=client_factory,
    )


def _comment_program() -> Program:
    return Program(
        schema_version="2.0",
        id="demo",
        name="Demo",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo",),
            work_item_types=("Task",),
            excluded_states=(),
            date_window_days=30,
        ),
    )


def _comment_proposal(proposal_id: str, at: datetime, *, entry_status: str = "pending") -> ADOUpdateProposal:
    return ADOUpdateProposal(
        id=proposal_id,
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=7,
        update_type="comment",
        created_at=at - timedelta(hours=2),
        expires_at=at + timedelta(hours=70),
        entries=(
            ADOUpdateEntry(
                work_item_id=1001,
                action="add_comment",
                field_or_tag="comment",
                current_value=None,
                proposed_value="Vertex demo_weekly issue #007\nRisk: high.",
                reason="Cited in confirmed issue #007.",
                revision_id=7,
                entry_status=entry_status,
            ),
        ),
    )


def test_apply_manifest_updates_entries_and_logs_audit_signals(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    applied_at = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)
    proposal = ADOUpdateProposal(
        id="prop-demo",
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=7,
        update_type="comment",
        created_at=applied_at - timedelta(hours=2),
        expires_at=applied_at + timedelta(hours=70),
        entries=(
            ADOUpdateEntry(
                work_item_id=1001,
                action="add_comment",
                field_or_tag="comment",
                current_value=None,
                proposed_value="Vertex demo_weekly issue #007\nRisk: high.",
                reason="Cited in confirmed issue #007.",
                revision_id=7,
            ),
            ADOUpdateEntry(
                work_item_id=1002,
                action="add_comment",
                field_or_tag="comment",
                current_value=None,
                proposed_value="Vertex demo_weekly issue #007\nRisk: medium.",
                reason="Cited in confirmed issue #007.",
                revision_id=1,
            ),
            ADOUpdateEntry(
                work_item_id=1003,
                action="add_comment",
                field_or_tag="comment",
                current_value=None,
                proposed_value="Vertex demo_weekly issue #007\nRisk: medium.",
                reason="Cited in confirmed issue #007.",
                revision_id=5,
            ),
            ADOUpdateEntry(
                work_item_id=1004,
                action="set_field",
                field_or_tag="Custom.RiskLevel",
                current_value="high",
                proposed_value="medium",
                reason="Sync Vertex override.",
                revision_id=8,
            ),
            ADOUpdateEntry(
                work_item_id=1005,
                action="add_tag",
                field_or_tag="System.Tags",
                current_value="alpha; beta",
                proposed_value="Needs-PM-Review",
                reason="Coverage gap persists.",
                revision_id=9,
            ),
        ),
    )
    manifest_path = write_proposal_manifest(proposal, programs_root=programs_root)
    client = _FakeADOClient(
        rows_by_id={
            1001: {"rev": 7, "fields": {"System.Id": 1001, "System.Rev": 7}},
            1002: {"rev": 2, "fields": {"System.Id": 1002, "System.Rev": 2}},
            1003: {"rev": 5, "fields": {"System.Id": 1003, "System.Rev": 5}},
            1004: {
                "rev": 8,
                "fields": {"System.Id": 1004, "System.Rev": 8, "Custom.RiskLevel": "high"},
            },
            1005: {
                "rev": 9,
                "fields": {"System.Id": 1005, "System.Rev": 9, "System.Tags": "alpha; beta"},
            },
        },
        comments_by_id={1003: [{"id": 900, "text": "Vertex demo_weekly issue #007\nAlready posted."}]},
    )

    artifacts = ADOWriter(client, programs_root=programs_root).apply_manifest(manifest_path, applied_at=applied_at)
    updated_proposal, proposal_status = read_proposal_manifest(manifest_path)
    signals = read_signals("demo", programs_root=programs_root)
    reviews = load_latest_review_decisions("demo", programs_root=programs_root)

    assert artifacts.proposal_status == "partially_applied"
    assert artifacts.applied_count == 3
    assert artifacts.skipped_count == 1
    assert artifacts.conflict_count == 1
    assert artifacts.failed_count == 0
    assert proposal_status == "partially_applied"
    assert [entry.entry_status for entry in updated_proposal.entries] == [
        "applied",
        "conflict",
        "skipped",
        "applied",
        "applied",
    ]
    assert len(signals) == 3
    assert all(signal.source == "vertex/ado_update" for signal in signals)
    assert set(reviews) == {signal.id for signal in signals}
    assert client.comment_lookup_count == 2
    assert client.session.calls[0]["method"] == "POST"
    assert client.session.calls[1]["method"] == "PATCH"
    assert client.session.calls[2]["method"] == "PATCH"

    rerun = ADOWriter(client, programs_root=programs_root).apply_manifest(manifest_path, applied_at=applied_at)

    assert rerun.applied_count == 3
    assert len(client.session.calls) == 3
    assert len(read_signals("demo", programs_root=programs_root)) == 3


def test_apply_manifest_writes_sqlite_backed_audit_signals(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "demo",
                "name": "Demo",
                "storage_backend": "sqlite",
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    applied_at = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)
    proposal = ADOUpdateProposal(
        id="prop-demo-sqlite",
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=7,
        update_type="comment",
        created_at=applied_at - timedelta(hours=2),
        expires_at=applied_at + timedelta(hours=70),
        entries=(
            ADOUpdateEntry(
                work_item_id=1001,
                action="add_comment",
                field_or_tag="comment",
                current_value=None,
                proposed_value="Vertex demo_weekly issue #007\nRisk: high.",
                reason="Cited in confirmed issue #007.",
                revision_id=7,
            ),
        ),
    )
    manifest_path = write_proposal_manifest(proposal, programs_root=programs_root)
    client = _FakeADOClient(
        rows_by_id={
            1001: {"rev": 7, "fields": {"System.Id": 1001, "System.Rev": 7}},
        },
    )

    artifacts = ADOWriter(client, programs_root=programs_root).apply_manifest(manifest_path, applied_at=applied_at)

    signal_store = SQLiteSignalStore(programs_root=programs_root)
    signals = signal_store.read("demo")
    reviews = signal_store.read_reviews("demo")

    assert artifacts.proposal_status == "applied"
    assert len(signals) == 1
    assert not read_signals("demo", programs_root=programs_root)
    assert signals[0].source == "vertex/ado_update"
    assert signals[0].metadata["proposal_id"] == "prop-demo-sqlite"
    assert reviews[signals[0].id].decision == "approved"


def test_rollback_manifest_reverts_applied_entries_idempotently(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    applied_at = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)
    rollback_at = datetime(2026, 5, 14, 9, 30, tzinfo=timezone.utc)
    proposal = ADOUpdateProposal(
        id="prop-rollback",
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=7,
        update_type="comment",
        created_at=applied_at - timedelta(hours=2),
        expires_at=applied_at + timedelta(hours=70),
        entries=(
            ADOUpdateEntry(
                work_item_id=1001,
                action="add_comment",
                field_or_tag="comment",
                current_value=None,
                proposed_value="Vertex demo_weekly issue #007\nRisk: high.",
                reason="Cited in confirmed issue #007.",
                revision_id=7,
                entry_status="applied",
            ),
            ADOUpdateEntry(
                work_item_id=1002,
                action="set_field",
                field_or_tag="Custom.RiskLevel",
                current_value="high",
                proposed_value="medium",
                reason="Sync Vertex override.",
                revision_id=8,
                entry_status="applied",
            ),
            ADOUpdateEntry(
                work_item_id=1003,
                action="set_field",
                field_or_tag="Custom.Owner",
                current_value="alex",
                proposed_value="sam",
                reason="Sync Vertex override.",
                revision_id=9,
                entry_status="pending",
            ),
        ),
    )
    manifest_path = write_proposal_manifest(proposal, programs_root=programs_root)
    client = _FakeADOClient(
        rows_by_id={
            1002: {
                "rev": 10,
                "fields": {"System.Id": 1002, "System.Rev": 10, "Custom.RiskLevel": "medium"},
            },
        },
        comments_by_id={1001: [{"id": 900, "text": "Vertex demo_weekly issue #007\nRisk: high."}]},
    )

    artifacts = ADOWriter(client).rollback_manifest(manifest_path, action_id="action-123", rolled_back_at=rollback_at)

    assert artifacts.rolled_back_count == 2
    assert artifacts.skipped_count == 1
    assert artifacts.conflict_count == 0
    assert artifacts.failed_count == 0
    assert [result.status for result in artifacts.results] == ["rolled_back", "rolled_back", "skipped"]
    assert client.rows_by_id[1002]["fields"]["Custom.RiskLevel"] == "high"
    assert client.comments_by_id[1001][-1]["text"].startswith("Vertex rollback action-123")
    assert client.session.calls[0]["method"] == "POST"
    assert client.session.calls[1]["method"] == "PATCH"

    rerun = ADOWriter(client).rollback_manifest(manifest_path, action_id="action-123", rolled_back_at=rollback_at)

    assert rerun.rolled_back_count == 0
    assert rerun.skipped_count == 3
    assert rerun.conflict_count == 0
    assert rerun.failed_count == 0
    assert len(client.session.calls) == 2


@pytest.mark.parametrize(
    ("pagination_mode", "lookup_error", "expected_status"),
    [
        *[(mode, None, "conflict") for mode in (
            "truncated",
            "missing",
            "invalid",
            "multiple",
            "invalid_total",
            "mismatched_total",
            "invalid_page_count",
            "zero_pages",
            "invalid_truncation",
        )],
        ("complete", AuthError("unauthorized"), "failed"),
        ("complete", CredentialExpired("credential expired"), "failed"),
        ("complete", QueryError("query failed"), "failed"),
        ("complete", QueryTimeoutError("query timed out"), "failed"),
    ],
)
def test_apply_ado_proposal_persists_comment_lookup_disposition(
    tmp_path: Path, pagination_mode: str, lookup_error: Exception | None, expected_status: str
) -> None:
    programs_root = tmp_path / "programs"
    applied_at = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)
    proposal = _comment_proposal(f"prop-apply-{pagination_mode}-{expected_status}", applied_at)
    manifest_path = write_proposal_manifest(proposal, programs_root=programs_root)
    client = _FakeADOClient(
        rows_by_id={1001: {"rev": 7, "fields": {"System.Id": 1001, "System.Rev": 7}}},
        comment_pagination_mode=pagination_mode,
        comment_lookup_error=lookup_error,
    )
    artifacts = _apply_ado_proposal_with_fake_client(
        str(manifest_path),
        applied_at=applied_at,
        programs_root=programs_root,
        program=_comment_program(),
        client=client,
    )

    persisted_proposal, persisted_status = read_proposal_manifest(manifest_path)
    assert artifacts.conflict_count == int(expected_status == "conflict")
    assert artifacts.failed_count == int(expected_status == "failed")
    assert persisted_status == "partially_applied"
    assert persisted_proposal.entries[0].entry_status == expected_status
    reason = persisted_proposal.entries[0].status_reason or ""
    assert ("comment history" if expected_status == "conflict" else "lookup failed") in reason
    assert client.session.calls == []
    assert read_signals("demo", programs_root=programs_root) == ()


@pytest.mark.parametrize(
    ("pagination_mode", "lookup_error", "expected_status"),
    [
        ("truncated", None, "conflict"),
        ("missing", None, "conflict"),
        ("invalid", None, "conflict"),
        ("complete", AuthError("unauthorized"), "failed"),
        ("complete", CredentialExpired("credential expired"), "failed"),
        ("complete", QueryError("query failed"), "failed"),
        ("complete", QueryTimeoutError("query timed out"), "failed"),
    ],
)
def test_rollback_comment_lookup_blocks_post_and_maps_disposition(
    tmp_path: Path,
    pagination_mode: str,
    lookup_error: Exception | None,
    expected_status: str,
) -> None:
    rolled_back_at = datetime(2026, 5, 14, 9, 30, tzinfo=timezone.utc)
    proposal = _comment_proposal("prop-rollback-comment-lookup", rolled_back_at, entry_status="applied")
    manifest_path = write_proposal_manifest(proposal, programs_root=tmp_path / "programs")
    client = _FakeADOClient(
        rows_by_id={},
        comment_pagination_mode=pagination_mode,
        comment_lookup_error=lookup_error,
    )

    artifacts = ADOWriter(client).rollback_manifest(
        manifest_path, action_id="action-123", rolled_back_at=rolled_back_at
    )

    assert artifacts.rolled_back_count == 0
    assert artifacts.conflict_count == int(expected_status == "conflict")
    assert artifacts.failed_count == int(expected_status == "failed")
    assert artifacts.results[0].status == expected_status
    assert client.session.calls == []


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_unexpected_comment_lookup_error_propagates_without_post(tmp_path: Path, operation: str) -> None:
    at = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)
    entry_status = "applied" if operation == "rollback" else "pending"
    proposal = _comment_proposal(f"prop-unexpected-{operation}", at, entry_status=entry_status)
    manifest_path = write_proposal_manifest(proposal, programs_root=tmp_path / "programs")
    rows_by_id = {1001: {"rev": 7, "fields": {"System.Id": 1001, "System.Rev": 7}}} if operation == "apply" else {}
    client = _FakeADOClient(rows_by_id=rows_by_id, comment_lookup_error=RuntimeError("unexpected failure"))
    writer = ADOWriter(client, programs_root=tmp_path / "programs")

    with pytest.raises(RuntimeError, match="unexpected failure"):
        if operation == "apply":
            writer.apply_manifest(manifest_path, applied_at=at)
        else:
            writer.rollback_manifest(manifest_path, action_id="action-123", rolled_back_at=at)

    assert client.session.calls == []


def test_empty_comment_signature_keeps_existing_no_lookup_behavior() -> None:
    client = _FakeADOClient(rows_by_id={})
    writer = ADOWriter(client)
    entry = ADOUpdateEntry(
        work_item_id=1001,
        action="add_comment",
        field_or_tag="comment",
        current_value=None,
        proposed_value=" \n\t",
        reason="test empty signature",
        revision_id=1,
    )

    assert writer._has_duplicate_comment(entry) is False
    assert client.comment_lookup_count == 0


def test_apply_manifest_rejects_expired_pending_entries(tmp_path: Path) -> None:
    manifest_path = write_proposal_manifest(
        ADOUpdateProposal(
            id="prop-expired",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=7,
            update_type="comment",
            created_at=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
            entries=(
                ADOUpdateEntry(
                    work_item_id=1001,
                    action="add_comment",
                    field_or_tag="comment",
                    current_value=None,
                    proposed_value="Vertex demo_weekly issue #007",
                    reason="Cited in confirmed issue #007.",
                    revision_id=7,
                ),
            ),
        ),
        programs_root=(tmp_path / "programs"),
    )

    with pytest.raises(ValueError, match="expired"):
        ADOWriter(_FakeADOClient(rows_by_id={1001: {"rev": 7, "fields": {"System.Id": 1001, "System.Rev": 7}}})).apply_manifest(
            manifest_path,
            applied_at=datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc),
        )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["proposal_status"] == "expired"


def test_apply_manifest_rejects_locked_manifest(tmp_path: Path) -> None:
    manifest_path = write_proposal_manifest(
        ADOUpdateProposal(
            id="prop-locked",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=7,
            update_type="comment",
            created_at=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
            entries=(
                ADOUpdateEntry(
                    work_item_id=1001,
                    action="add_comment",
                    field_or_tag="comment",
                    current_value=None,
                    proposed_value="Vertex demo_weekly issue #007",
                    reason="Cited in confirmed issue #007.",
                    revision_id=7,
                ),
            ),
        ),
        programs_root=(tmp_path / "programs"),
    )

    with manifest_path.open("a+", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        with pytest.raises(ProposalManifestLockedError, match="Another apply is in progress"):
            ADOWriter(_FakeADOClient(rows_by_id={1001: {"rev": 7, "fields": {"System.Id": 1001, "System.Rev": 7}}})).apply_manifest(
                manifest_path,
                applied_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
            )
        portalocker.unlock(handle)


class _FakeADOClient:
    def __init__(
        self,
        *,
        rows_by_id: dict[int, dict[str, Any]],
        comments_by_id: dict[int, list[dict[str, Any]]] | None = None,
        fail_patch_for: set[int] | None = None,
        comment_pagination_mode: str = "complete",
        comment_lookup_error: Exception | None = None,
    ) -> None:
        self.rows_by_id = rows_by_id
        self.comments_by_id = comments_by_id or {}
        self.comment_pagination_mode = comment_pagination_mode
        self.comment_lookup_error = comment_lookup_error
        self.comment_lookup_count = 0
        self.timeout = 30
        self._rest_base_url = "https://dev.azure.com/your-org/One/_apis/wit/"
        self._session = _FakeSession(self, fail_patch_for=fail_patch_for or set())
        # ADF-W1.1: ADOWriter._request_json now routes mutations through the
        # dedicated non-retrying session; alias it to the same fake so every
        # existing assertion against `_FakeSession.calls` keeps working.
        self._mutation_session = self._session

    @property
    def session(self) -> _FakeSession:
        return self._session

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer fake", "Accept": "application/json", "Content-Type": "application/json"}

    def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for work_item_id in work_item_ids:
            if work_item_id not in self.rows_by_id:
                continue
            stored = self.rows_by_id[work_item_id]
            all_fields = dict(stored.get("fields", {}))
            rows.append(
                {
                    "id": work_item_id,
                    "rev": stored.get("rev"),
                    "fields": {field: all_fields[field] for field in fields if field in all_fields},
                }
            )
        return rows

    def list_work_item_comments(
        self,
        work_item_id: int,
        *,
        on_pagination: Any | None = None,
    ) -> list[dict[str, Any]]:
        self.comment_lookup_count += 1
        if self.comment_lookup_error is not None:
            raise self.comment_lookup_error
        if on_pagination is not None and self.comment_pagination_mode != "missing":
            total = len(self.comments_by_id.get(work_item_id, ()))
            outcomes = {
                "invalid": (object(),),
                "multiple": (
                    PaginationOutcome(total_fetched=0, page_count=1, is_truncated=False),
                    PaginationOutcome(total_fetched=0, page_count=1, is_truncated=False),
                ),
                "invalid_total": (PaginationOutcome(total_fetched=True, page_count=1, is_truncated=False),),  # type: ignore[arg-type]
                "mismatched_total": (PaginationOutcome(total_fetched=total + 1, page_count=1, is_truncated=False),),
                "invalid_page_count": (PaginationOutcome(total_fetched=total, page_count=True, is_truncated=False),),  # type: ignore[arg-type]
                "zero_pages": (PaginationOutcome(total_fetched=total, page_count=0, is_truncated=False),),
                "invalid_truncation": (PaginationOutcome(total_fetched=total, page_count=1, is_truncated=1),),  # type: ignore[arg-type]
                "truncated": (PaginationOutcome(total_fetched=total, page_count=10, is_truncated=True),),
            }.get(
                self.comment_pagination_mode,
                (PaginationOutcome(total_fetched=total, page_count=1, is_truncated=False),),
            )
            for outcome in outcomes:
                on_pagination(outcome)  # type: ignore[arg-type]
        return list(self.comments_by_id.get(work_item_id, ()))


class _FakeSession:
    def __init__(self, client: _FakeADOClient, *, fail_patch_for: set[int] | None = None) -> None:
        self.client = client
        self.calls: list[dict[str, Any]] = []
        self._next_comment_id = 10000
        self.fail_patch_for: set[int] = fail_patch_for or set()

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        work_item_id = int(url.split("/workItems/")[1].split("/")[0].split("?")[0])
        if method == "POST" and url.endswith("comments?api-version=7.1-preview.4"):
            text = str(kwargs["json"]["text"])
            comment_id = self._next_comment_id
            self._next_comment_id += 1
            self.client.comments_by_id.setdefault(work_item_id, []).append({"id": comment_id, "text": text})
            return _FakeResponse({"id": comment_id, "text": text})
        if method == "PATCH" and url.endswith("?api-version=7.1"):
            if work_item_id in self.fail_patch_for:
                return _FakeResponse({"message": "Test op failed: System.Rev mismatch"}, status_code=400)
            for op in kwargs["json"]:
                if op["op"] == "test":  # read-only assertion — do not apply as a write
                    continue
                field_name = str(op["path"]).removeprefix("/fields/")
                self.client.rows_by_id[work_item_id].setdefault("fields", {})[field_name] = op["value"]
            return _FakeResponse({"id": work_item_id})
        raise AssertionError(f"Unexpected fake request: {method} {url}")


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


# ---------------------------------------------------------------------------
# Inline test-op: four-outcome cassette (S1A.2)
# ---------------------------------------------------------------------------


def test_inline_test_op_four_outcomes(tmp_path: Path) -> None:
    """Verify inline PATCH test-op covers applied, conflict, skipped, and failed."""
    programs_root = tmp_path / "programs"
    applied_at = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)

    proposal = ADOUpdateProposal(
        id="prop-test-op",
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=9,
        update_type="field",
        created_at=applied_at - timedelta(hours=1),
        expires_at=applied_at + timedelta(hours=48),
        entries=(
            # 1. applied — test op passes, field written
            ADOUpdateEntry(
                work_item_id=2001,
                action="set_field",
                field_or_tag="Custom.RiskLevel",
                current_value="medium",
                proposed_value="high",
                reason="Risk escalation.",
                revision_id=3,
            ),
            # 2. conflict — test op fails (race: revision changed between preflight and PATCH)
            ADOUpdateEntry(
                work_item_id=2002,
                action="add_tag",
                field_or_tag="System.Tags",
                current_value=None,
                proposed_value="Needs-Review",
                reason="Coverage gap.",
                revision_id=5,
            ),
            # 3. skipped — duplicate comment (POST path, no PATCH test op involved)
            ADOUpdateEntry(
                work_item_id=2003,
                action="add_comment",
                field_or_tag="comment",
                current_value=None,
                proposed_value="Vertex demo_weekly issue #009\nAlready posted.",
                reason="Cited in issue #009.",
                revision_id=7,
            ),
            # 4. failed — work item not found (live_row is None)
            ADOUpdateEntry(
                work_item_id=9999,
                action="set_field",
                field_or_tag="Custom.RiskLevel",
                current_value="low",
                proposed_value="medium",
                reason="Missing item.",
                revision_id=1,
            ),
        ),
    )

    manifest_path = write_proposal_manifest(proposal, programs_root=programs_root)
    client = _FakeADOClient(
        rows_by_id={
            2001: {"rev": 3, "fields": {"System.Id": 2001, "System.Rev": 3, "Custom.RiskLevel": "medium"}},
            2002: {"rev": 5, "fields": {"System.Id": 2002, "System.Rev": 5, "System.Tags": ""}},
            2003: {"rev": 7, "fields": {"System.Id": 2003, "System.Rev": 7}},
        },
        comments_by_id={
            2003: [{"id": 801, "text": "Vertex demo_weekly issue #009\nAlready posted."}]
        },
        fail_patch_for={2002},  # simulate race: WI 2002's revision changed after preflight
    )

    artifacts = ADOWriter(client, programs_root=programs_root).apply_manifest(
        manifest_path, applied_at=applied_at
    )
    updated_proposal, _ = read_proposal_manifest(manifest_path)

    assert [e.entry_status for e in updated_proposal.entries] == [
        "applied",
        "conflict",
        "skipped",
        "failed",
    ]
    assert artifacts.applied_count == 1
    assert artifacts.conflict_count == 1
    assert artifacts.skipped_count == 1
    assert artifacts.failed_count == 1

    # Verify the applied PATCH carried the test op as the first operation
    patch_call = next(c for c in client.session.calls if c["method"] == "PATCH")
    ops = patch_call["json"]
    assert ops[0] == {"op": "test", "path": "/fields/System.Rev", "value": 3}


# ---------------------------------------------------------------------------
# Lazy migration: old manifests without status_reason/remote_rev (S1A.5)
# ---------------------------------------------------------------------------


def test_lazy_migration_old_manifest_fields_are_none() -> None:
    """Old manifest documents without status_reason/remote_rev deserialize to None."""
    document = {
        "id": "prop-old",
        "program_id": "demo",
        "edition_id": "demo_weekly",
        "issue_number": 5,
        "update_type": "comment",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2026-01-08T00:00:00+00:00",
        "proposal_status": "applied",
        "entries": [
            {
                "work_item_id": 42,
                "action": "add_comment",
                "field_or_tag": "comment",
                "current_value": None,
                "proposed_value": "Hello",
                "reason": "Legacy entry.",
                "revision_id": 3,
                "entry_status": "applied",
                # status_reason and remote_rev intentionally absent
            }
        ],
    }

    proposal = proposal_from_document(document)

    assert len(proposal.entries) == 1
    entry = proposal.entries[0]
    assert entry.status_reason is None
    assert entry.remote_rev is None
    assert entry.entry_status == "applied"


def test_new_fields_round_trip_through_manifest(tmp_path: Path) -> None:
    """status_reason and remote_rev survive a write → read round-trip."""
    from src.core.ado_proposal import proposal_to_document

    applied_at = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    entry = ADOUpdateEntry(
        work_item_id=99,
        action="set_field",
        field_or_tag="Custom.RiskLevel",
        current_value="low",
        proposed_value="high",
        reason="Escalation.",
        revision_id=11,
        entry_status="applied",
        status_reason=None,
        remote_rev=11,
    )
    proposal = ADOUpdateProposal(
        id="prop-round-trip",
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=3,
        update_type="field",
        created_at=applied_at,
        expires_at=applied_at + timedelta(hours=48),
        entries=(entry,),
    )

    doc = proposal_to_document(proposal, proposal_status="applied")
    restored = proposal_from_document(doc)

    assert restored.entries[0].remote_rev == 11
    assert restored.entries[0].status_reason is None

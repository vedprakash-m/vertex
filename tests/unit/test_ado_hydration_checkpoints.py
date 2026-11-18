from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.core.ado_hydration import ADOHydrationConfig, ADOHydrationProvider
from src.core.ado_hydration_checkpoints import (
    CheckpointAdvance,
    HydrationSubresource,
    advance_checkpoint,
    ensure_checkpoint_table,
    read_checkpoints,
)
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.integration_types import HydrationMode
from tests.unit.test_ado_hydration import _FakeADOClient, _registration

_AT = datetime(2026, 5, 25, tzinfo=timezone.utc)
_SINCE = datetime(2026, 5, 1, tzinfo=timezone.utc)
_VERIFIED = datetime(2026, 5, 24, 23, tzinfo=timezone.utc)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_checkpoint_table(conn)
    conn.commit()
    return conn


def test_missing_row_reads_unknown() -> None:
    checkpoints = read_checkpoints(_conn(), (42,))
    checkpoint = checkpoints[(42, HydrationSubresource.REVISIONS)]
    assert checkpoint.state == "unknown"
    assert checkpoint.cursor is None
    assert len(checkpoints) == len(HydrationSubresource)


def test_advance_is_visible_only_after_commit_and_rolls_back_with_transaction() -> None:
    conn = _conn()
    conn.execute("BEGIN")
    advance_checkpoint(conn, CheckpointAdvance(42, HydrationSubresource.COMMENTS, "3"), at=_AT)
    conn.rollback()
    assert read_checkpoints(conn, (42,))[(42, HydrationSubresource.COMMENTS)].state == "unknown"

    conn.execute("BEGIN")
    advance_checkpoint(conn, CheckpointAdvance(42, HydrationSubresource.COMMENTS, "3"), at=_AT)
    conn.commit()
    checkpoint = read_checkpoints(conn, (42,))[(42, HydrationSubresource.COMMENTS)]
    assert (checkpoint.state, checkpoint.cursor, checkpoint.schema_version) == ("current", "3", 1)


def test_advance_outside_transaction_is_rejected() -> None:
    with pytest.raises(RuntimeError):
        advance_checkpoint(_conn(), CheckpointAdvance(42, HydrationSubresource.REVISIONS, "2"), at=_AT)


def test_unknown_subresource_is_rejected_by_schema() -> None:
    conn = _conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ado_hydration_checkpoints(work_item_id, subresource, schema_version, cursor, updated_at)"
            " VALUES (1, 'attachment_content', 1, 'x', 'now')"
        )


def test_older_schema_version_row_reads_unknown() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO ado_hydration_checkpoints(work_item_id, subresource, schema_version, cursor, updated_at)"
        " VALUES (42, 'revisions', 0, '9', 'now')"
    )
    assert read_checkpoints(conn, (42,))[(42, HydrationSubresource.REVISIONS)].state == "unknown"


def _seeded_store(tmp_path: Path) -> ChannelRegistryStore:
    store = ChannelRegistryStore(tmp_path / "registry.sqlite3", "demo")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO registrations(channel, program_id, provider_instance_id, ref_id, ref_kind, status,"
            " first_discovered_at, last_seen_at) VALUES ('ado', 'demo', 'default', '42', 'work_item', 'active',"
            " '2026-05-24T00:00:00+00:00', '2026-05-24T00:00:00+00:00')"
        )
    return store


def test_store_mark_verified_commits_checkpoints_atomically(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    store.mark_verified(
        "ado",
        (("42", "work_item"),),
        _AT,
        checkpoint_advances=(CheckpointAdvance(42, HydrationSubresource.REVISIONS, "2"),),
    )
    checkpoints = store.read_hydration_checkpoints((42,))
    assert checkpoints[(42, HydrationSubresource.REVISIONS)].cursor == "2"
    assert checkpoints[(42, HydrationSubresource.COMMENTS)].state == "unknown"


def test_store_failed_evidence_commit_leaves_checkpoint_unknown(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    bad = CheckpointAdvance(42, "attachment_content", "x")  # type: ignore[arg-type]
    with pytest.raises(sqlite3.IntegrityError):
        store.mark_verified(
            "ado",
            (("42", "work_item"),),
            _AT,
            checkpoint_advances=(CheckpointAdvance(42, HydrationSubresource.REVISIONS, "2"), bad),
        )
    assert store.read_hydration_checkpoints((42,))[(42, HydrationSubresource.REVISIONS)].state == "unknown"
    assert store.pullable_registrations("ado")[0].last_verified_at is None


class _RelationsClient(_FakeADOClient):
    def __init__(self) -> None:
        super().__init__()
        self.downloads: list[str] = []

    def get_work_item_relations(self, work_item_ids: list[int]) -> list[dict[str, Any]]:
        return [
            {
                "id": work_item_id,
                "relations": [
                    {"rel": "System.LinkTypes.Hierarchy-Reverse", "url": "https://x/_apis/wit/workItems/1"},
                    {
                        "rel": "AttachedFile",
                        "url": "https://x/_apis/wit/attachments/abc",
                        "attributes": {"name": "plan.docx", "resourceSize": 10},
                    },
                ],
            }
            for work_item_id in work_item_ids
        ]

    def download_attachment(self, url: str) -> bytes:
        self.downloads.append(url)
        return b""


def _provider(client: Any, checkpoints: dict[tuple[int, HydrationSubresource], Any] | None) -> ADOHydrationProvider:
    reader = None if checkpoints is None else (lambda ids: {key: value for key, value in checkpoints.items() if key[0] in ids})
    return ADOHydrationProvider(client, checkpoint_reader=reader)


def _current(wid: int, sub: HydrationSubresource, cursor: str) -> Any:
    from src.core.ado_hydration_checkpoints import HydrationCheckpoint

    return HydrationCheckpoint(wid, sub, "current", 1, cursor)


def test_unknown_checkpoint_forces_full_detail_rehydration_of_verified_item() -> None:
    client = _FakeADOClient()
    provider = _provider(client, {})
    reg = _registration("42", last_verified_at=_VERIFIED)
    result = provider.hydrate((reg,), _SINCE, "demo", ADOHydrationConfig(), mode=HydrationMode.FULL)
    assert client.revision_calls == [42]
    assert client.comment_calls == [42]
    advances = {(a.work_item_id, a.subresource): a.cursor for a in result.resources.checkpoint_advances}
    assert advances[(42, HydrationSubresource.REVISIONS)] == "2"
    assert advances[(42, HydrationSubresource.COMMENTS)] == "1"


def test_current_checkpoint_keeps_incremental_skip_for_unchanged_item() -> None:
    client = _FakeADOClient()
    provider = _provider(
        client,
        {
            (42, HydrationSubresource.REVISIONS): _current(42, HydrationSubresource.REVISIONS, "2"),
            (42, HydrationSubresource.COMMENTS): _current(42, HydrationSubresource.COMMENTS, "1"),
        },
    )
    reg = _registration("42", last_verified_at=_VERIFIED)
    result = provider.hydrate((reg,), _SINCE, "demo", ADOHydrationConfig(), mode=HydrationMode.FULL)
    assert client.revision_calls == []
    assert result.hydrated_ref_ids == (("42", "work_item"),)
    assert result.resources.checkpoint_advances == ()


def test_relations_and_attachment_metadata_checkpoints_without_download() -> None:
    client = _RelationsClient()
    provider = _provider(client, {})
    result = provider.hydrate((_registration("42"),), _SINCE, "demo", ADOHydrationConfig(), mode=HydrationMode.FULL)
    subs = {a.subresource for a in result.resources.checkpoint_advances if a.work_item_id == 42}
    assert subs == set(HydrationSubresource)
    assert client.downloads == []


def test_failed_detail_does_not_propose_history_checkpoints() -> None:
    class _Broken(_FakeADOClient):
        def list_work_item_comments(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
            return []  # no pagination outcome -> unverified

    result = _provider(_Broken(), {}).hydrate(
        (_registration("42"),), _SINCE, "demo", ADOHydrationConfig(), mode=HydrationMode.FULL
    )
    assert result.failed_ref_ids == (("42", "work_item"),)
    assert result.resources.checkpoint_advances == ()

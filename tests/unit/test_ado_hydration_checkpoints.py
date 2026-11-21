from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.core.ado_hydration import ADOHydrationConfig, ADOHydrationProvider
from src.core.ado_hydration_checkpoints import (
    CheckpointAdvance,
    HydrationCheckpoint,
    HydrationSubresource,
    advance_checkpoint,
    commit_checkpoint_advances,
    ensure_checkpoint_table,
    plan_checkpoint_reseeds,
    read_checkpoints,
    read_hydration_evidence,
)
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.exceptions import ConfigError
from src.core.integration_types import ChannelConfig, HydrationMode
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
    checkpoints = read_checkpoints(_conn(), "default", (42,))
    checkpoint = checkpoints[(42, HydrationSubresource.REVISIONS)]
    assert checkpoint.state == "unknown"
    assert checkpoint.cursor is None
    assert len(checkpoints) == len(HydrationSubresource)


def test_advance_is_visible_only_after_commit_and_rolls_back_with_transaction() -> None:
    conn = _conn()
    conn.execute("BEGIN")
    advance_checkpoint(
        conn,
        CheckpointAdvance(42, HydrationSubresource.COMMENTS, "3"),
        provider_instance_id="default",
        at=_AT,
    )
    conn.rollback()
    assert read_checkpoints(conn, "default", (42,))[
        (42, HydrationSubresource.COMMENTS)
    ].state == "unknown"

    conn.execute("BEGIN")
    advance_checkpoint(
        conn,
        CheckpointAdvance(42, HydrationSubresource.COMMENTS, "3"),
        provider_instance_id="default",
        at=_AT,
    )
    conn.commit()
    checkpoint = read_checkpoints(conn, "default", (42,))[
        (42, HydrationSubresource.COMMENTS)
    ]
    assert (checkpoint.state, checkpoint.cursor, checkpoint.schema_version) == ("current", "3", 2)


def test_advance_outside_transaction_is_rejected() -> None:
    with pytest.raises(RuntimeError):
        advance_checkpoint(
            _conn(),
            CheckpointAdvance(42, HydrationSubresource.REVISIONS, "2"),
            provider_instance_id="default",
            at=_AT,
        )


def test_unknown_subresource_is_rejected_by_schema() -> None:
    conn = _conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ado_hydration_checkpoints"
            "(provider_instance_id, work_item_id, subresource, schema_version, cursor, updated_at)"
            " VALUES ('default', 1, 'attachment_content', 2, 'x', 'now')"
        )


def test_older_schema_version_row_reads_unknown() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO ado_hydration_checkpoints"
        "(provider_instance_id, work_item_id, subresource, schema_version, cursor, updated_at)"
        " VALUES ('default', 42, 'revisions', 1, '9', 'now')"
    )
    assert read_checkpoints(conn, "default", (42,))[
        (42, HydrationSubresource.REVISIONS)
    ].state == "unknown"


def test_provider_instance_checkpoint_keys_are_isolated() -> None:
    conn = _conn()
    conn.execute("BEGIN")
    advance_checkpoint(
        conn,
        CheckpointAdvance(42, HydrationSubresource.REVISIONS, "2"),
        provider_instance_id="org-a",
        at=_AT,
    )
    conn.commit()

    org_a = read_checkpoints(conn, "org-a", (42,))
    org_b = read_checkpoints(conn, "org-b", (42,))

    assert org_a[(42, HydrationSubresource.REVISIONS)].state == "current"
    assert org_b[(42, HydrationSubresource.REVISIONS)].state == "unknown"


def test_legacy_checkpoint_table_is_preserved_but_never_reused() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ado_hydration_checkpoints (
            work_item_id INTEGER NOT NULL,
            subresource TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            cursor TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (work_item_id, subresource)
        );
        INSERT INTO ado_hydration_checkpoints
            (work_item_id, subresource, schema_version, cursor, updated_at)
            VALUES (42, 'revisions', 1, 'legacy', '2026-05-25T00:00:00+00:00');
        """
    )

    ensure_checkpoint_table(conn)

    assert read_checkpoints(conn, "default", (42,))[
        (42, HydrationSubresource.REVISIONS)
    ].state == "unknown"
    assert conn.execute(
        "SELECT cursor FROM ado_hydration_checkpoints_legacy_v1"
    ).fetchone()["cursor"] == "legacy"


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
    checkpoints = store.read_hydration_checkpoints("default", (42,))
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
    assert store.read_hydration_checkpoints("default", (42,))[
        (42, HydrationSubresource.REVISIONS)
    ].state == "unknown"
    assert store.pullable_registrations("ado")[0].last_verified_at is None


def test_store_rejects_checkpoint_provider_mismatch_before_mutation(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    advance = CheckpointAdvance(
        42, HydrationSubresource.REVISIONS, "2", provider_instance_id="org-a"
    )

    with pytest.raises(ValueError, match="one provider instance"):
        store.mark_verified(
            "ado",
            (("42", "work_item"),),
            _AT,
            provider_instance_id="default",
            checkpoint_advances=(advance,),
        )

    assert store.pullable_registrations("ado")[0].last_verified_at is None


@pytest.mark.parametrize(
    ("channel", "verified_refs"),
    [
        ("ado", ()),
        ("ado", (("42", "work_item"),)),
        ("kusto", (("99", "work_item"),)),
    ],
)
def test_store_rejects_checkpoint_advance_outside_verified_refs(
    tmp_path: Path,
    channel: str,
    verified_refs: tuple[tuple[str, str], ...],
) -> None:
    store = _seeded_store(tmp_path)

    with pytest.raises(ValueError, match="verified work-item evidence"):
        store.mark_verified(
            channel,
            verified_refs,
            _AT,
            checkpoint_advances=(
                CheckpointAdvance(
                    99,
                    HydrationSubresource.REVISIONS,
                    "2",
                ),
            ),
        )

    checkpoint = store.read_hydration_checkpoints("default", (99,))[
        (99, HydrationSubresource.REVISIONS)
    ]
    assert checkpoint.state == "unknown"
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
    reader = None if checkpoints is None else (
        lambda provider_instance_id, ids: {
            key: value for key, value in checkpoints.items() if key[0] in ids
        }
    )
    return ADOHydrationProvider(client, checkpoint_reader=reader)


def _current(wid: int, sub: HydrationSubresource, cursor: str) -> Any:
    from src.core.ado_hydration_checkpoints import HydrationCheckpoint

    return HydrationCheckpoint(wid, sub, "current", 2, cursor)


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


def test_hydration_reads_and_advances_the_registration_provider_scope() -> None:
    reads: list[tuple[str, tuple[int, ...]]] = []
    provider = ADOHydrationProvider(
        _FakeADOClient(),
        checkpoint_reader=lambda provider_id, ids: reads.append((provider_id, ids)) or {},
    )
    reg = replace(_registration("42", last_verified_at=_VERIFIED), provider_instance_id="org-a")

    result = provider.hydrate(
        (reg,), _SINCE, "demo", ADOHydrationConfig(), mode=HydrationMode.FULL
    )

    assert reads == [("org-a", (42,))]
    assert {advance.provider_instance_id for advance in result.resources.checkpoint_advances} == {
        "org-a"
    }


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
    assert {
        advance.subresource
        for advance in result.resources.checkpoint_advances
    } == {HydrationSubresource.ITEM}


def test_changed_item_reuses_verified_revision_prefix_and_records_page_evidence() -> None:
    old_revisions = [
        {
            "rev": 1,
            "fields": {"System.ChangedDate": "2026-05-20T10:00:00Z"},
        },
        {
            "rev": 2,
            "fields": {"System.ChangedDate": "2026-05-24T09:00:00Z"},
        },
    ]
    checkpoints = {
        (42, HydrationSubresource.REVISIONS): HydrationCheckpoint(
            42,
            HydrationSubresource.REVISIONS,
            "current",
            2,
            "2",
            provider_instance_id="default",
            updated_at=_VERIFIED,
            evidence_json=json.dumps(
                {
                    "schema_version": 2,
                    "subresource": "revisions",
                    "cursor": "2",
                    "records": old_revisions,
                    "pagination": {
                        "total_fetched": 2,
                        "page_count": 1,
                        "is_truncated": False,
                    },
                }
            ),
        )
    }
    revision_requests: list[dict[str, object]] = []

    class _Incremental(_FakeADOClient):
        def list_work_item_revisions(
            self, work_item_id: int, **kwargs: object
        ) -> list[dict[str, object]]:
            revision_requests.append(dict(kwargs))
            rows = [
                {
                    "rev": 3,
                    "fields": {"System.ChangedDate": "2026-05-24T10:00:00Z"},
                }
            ]
            callback = kwargs.get("on_pagination")
            if callable(callback):
                from src.core.integration_types import PaginationOutcome

                callback(PaginationOutcome(1, 1, False))
            return rows

    reg = replace(
        _registration("42", last_verified_at=datetime(2026, 5, 24, 9, 30, tzinfo=timezone.utc)),
    )
    result = _provider(_Incremental(), checkpoints).hydrate(
        (reg,),
        _SINCE,
        "demo",
        ADOHydrationConfig(anti_entropy_after_seconds=10**12),
        mode=HydrationMode.FULL,
    )

    assert [request.get("start_after_revision") for request in revision_requests] == [2]
    revision_advance = next(
        advance
        for advance in result.resources.checkpoint_advances
        if advance.subresource is HydrationSubresource.REVISIONS
    )
    evidence = json.loads(revision_advance.evidence_json or "{}")
    assert [row["rev"] for row in evidence["records"]] == [1, 2, 3]
    assert evidence["pagination"] == {
        "total_fetched": 1,
        "page_count": 1,
        "is_truncated": False,
    }


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
    assert {
        (advance.work_item_id, advance.subresource)
        for advance in result.resources.checkpoint_advances
    } == {
        (42, HydrationSubresource.ITEM),
        (42, HydrationSubresource.REVISIONS),
    }
    assert result.resources.checkpoint_evidence_ref_ids == (("42", "work_item"),)


def test_checkpoint_evidence_commit_preserves_page_records_through_replay(
    tmp_path: Path,
) -> None:
    store = _seeded_store(tmp_path)
    evidence = json.dumps(
        {
            "schema_version": 2,
            "subresource": "comments",
            "cursor": "1",
            "records": [{"id": 7}],
            "pagination": {
                "total_fetched": 1,
                "page_count": 1,
                "is_truncated": False,
            },
        },
        sort_keys=True,
    )
    store.mark_verified(
        "ado",
        (("42", "work_item"),),
        _AT,
        checkpoint_advances=(
            CheckpointAdvance(
                42,
                HydrationSubresource.COMMENTS,
                "1",
                evidence_json=evidence,
            ),
        ),
    )

    with sqlite3.connect(tmp_path / "registry.sqlite3") as conn:
        conn.row_factory = sqlite3.Row
        persisted = read_hydration_evidence(
            conn, "default", 42, HydrationSubresource.COMMENTS
        )
    assert persisted is not None
    assert json.loads(persisted)["pagination"]["page_count"] == 1
    checkpoint = store.read_hydration_checkpoints("default", (42,))[
        (42, HydrationSubresource.COMMENTS)
    ]
    assert checkpoint.state == "current"
    assert json.loads(checkpoint.evidence_json or "{}") == json.loads(evidence)


def test_anti_entropy_reseeds_all_history_after_expiry_without_losing_evidence() -> None:
    old = _AT.replace(day=1)
    checkpoints = {
        (42, subresource): HydrationCheckpoint(
            42,
            subresource,
            "current",
            2,
            "old",
            updated_at=old,
        )
        for subresource in (
            HydrationSubresource.REVISIONS,
            HydrationSubresource.COMMENTS,
        )
    }
    client = _FakeADOClient()
    result = _provider(client, checkpoints).hydrate(
        (_registration("42", last_verified_at=_VERIFIED),),
        _SINCE,
        "demo",
        ADOHydrationConfig(
            anti_entropy_after_seconds=1,
            anti_entropy_max_expired_items=1,
        ),
        mode=HydrationMode.FULL,
    )

    assert client.revision_calls == [42]
    assert client.comment_calls == [42]
    assert (42, "anti_entropy_expired") in result.resources.checkpoint_reseed_reasons

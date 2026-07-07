"""Unit tests for src.core.ledger.candidate_sqlite_store (W7-1 / PS-27).

Tests cover:
- DB initialization and schema creation
- insert_candidate idempotency (unique on candidate_id only; dedupe_key non-unique)
- load_candidates round-trip
- insert_decision (append-only history; latest-wins handled at application layer)
- load_decisions
- migrate_jsonl_to_sqlite (current JSONL + rotated files)
- Integration with candidate_store public API
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.ledger import candidate_sqlite_store as _sqls
from src.core.ledger.candidate_store import (
    CandidateEvent,
    CandidateDecisionRecord,
    append_candidate,
    append_triage_decision,
    load_pending_candidates,
    load_triage_decisions,
    active_candidates,
    get_candidate_dir,
    get_pending_path,
    get_triaged_path,
)
from src.core.ledger.source_refs import EmailRef, source_ref_to_dict

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


def _email_ref() -> EmailRef:
    return EmailRef(
        subject="Test",
        sent_at=NOW,
        sender="a@example.com",
        message_id="msg-1",
        vault_hash="sha256:vault1",
    )


def _make_candidate(
    candidate_id: str = "cand-1",
    program_id: str = "prog",
    dedupe_key: str | None = None,
    batch_id: str = "batch-1",
) -> CandidateEvent:
    dk = dedupe_key or f"sha256:{candidate_id}"
    return CandidateEvent(
        candidate_id=candidate_id,
        program_id=program_id,
        proposed_event_type="milestone.completed.v1",
        proposed_payload={"milestone_id": "m1", "completed_on": "2026-06-25"},
        proposed_occurred_at=NOW,
        proposed_temporal_confidence="exact",
        proposed_confidence="medium",
        source_ref=_email_ref(),
        pipeline="rev_mail",
        extraction_confidence=0.9,
        entity_resolution=(),
        dedupe_key=dk,
        dedupe_core_hash="sha256:core",
        source_document_key=f"email:sha256:vault1:msg-1",
        corroborating_refs=(),
        batch_id=batch_id,
        staged_at=NOW,
        schema_version="1",
        evidence_refs=(),
    )


def _make_decision(
    candidate_id: str = "cand-1",
    kind: str = "approved",
    decided_at: datetime | None = None,
) -> CandidateDecisionRecord:
    return CandidateDecisionRecord(
        candidate_id=candidate_id,
        kind=kind,
        decided_at=decided_at or NOW,
        triage_actor="operator",
        batch_id="batch-1",
    )


# ---------------------------------------------------------------------------
# DB initialization
# ---------------------------------------------------------------------------


class TestInitCandidateDb:
    def test_creates_db_file(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "prog" / "ledger" / "candidates"
        _sqls.init_candidate_db(db_dir)
        assert _sqls.candidate_db_path(db_dir).exists()

    def test_idempotent_double_init(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "prog" / "ledger" / "candidates"
        _sqls.init_candidate_db(db_dir)
        _sqls.init_candidate_db(db_dir)  # no error on second call
        assert _sqls.candidate_db_path(db_dir).exists()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "deep" / "nested" / "dir"
        _sqls.init_candidate_db(db_dir)
        assert db_dir.is_dir()

    def test_open_db_preserves_prior_wal_and_10s_busy_timeout(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "prog" / "ledger" / "candidates"
        _sqls.init_candidate_db(db_dir)

        with _sqls._open_db(_sqls.candidate_db_path(db_dir)) as conn:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]

        assert str(journal_mode).lower() == "wal"
        assert busy_timeout == 10_000
        assert synchronous == 1  # SQLite NORMAL


# ---------------------------------------------------------------------------
# sqlite_insert_candidate
# ---------------------------------------------------------------------------


class TestSqliteInsertCandidate:
    def _db_dir(self, tmp_path: Path) -> Path:
        db_dir = tmp_path / "ledger" / "candidates"
        _sqls.init_candidate_db(db_dir)
        return db_dir

    def _insert(self, db_dir: Path, *, candidate_id: str = "cand-1", dedupe_key: str = "sha256:dk1") -> bool:
        return _sqls.sqlite_insert_candidate(
            db_dir,
            candidate_id=candidate_id,
            program_id="prog",
            batch_id="b1",
            source_document_key="sdk-1",
            dedupe_key=dedupe_key,
            staged_at=NOW.isoformat(),
            payload_json=json.dumps({"candidate_id": candidate_id}),
        )

    def test_first_insert_returns_true(self, tmp_path: Path) -> None:
        db_dir = self._db_dir(tmp_path)
        assert self._insert(db_dir) is True

    def test_duplicate_candidate_id_returns_false(self, tmp_path: Path) -> None:
        db_dir = self._db_dir(tmp_path)
        self._insert(db_dir, candidate_id="cand-dup", dedupe_key="sha256:a")
        result = self._insert(db_dir, candidate_id="cand-dup", dedupe_key="sha256:b")
        assert result is False

    def test_same_dedupe_key_different_candidate_id_both_inserted(self, tmp_path: Path) -> None:
        # dedupe_key is non-unique at DB level; only candidate_id is the PK
        db_dir = self._db_dir(tmp_path)
        r1 = self._insert(db_dir, candidate_id="cand-1", dedupe_key="sha256:same")
        r2 = self._insert(db_dir, candidate_id="cand-2", dedupe_key="sha256:same")
        assert r1 is True
        assert r2 is True
        assert len(_sqls.sqlite_load_candidates(db_dir)) == 2

    def test_different_candidates_both_inserted(self, tmp_path: Path) -> None:
        db_dir = self._db_dir(tmp_path)
        r1 = self._insert(db_dir, candidate_id="c1", dedupe_key="sha256:k1")
        r2 = self._insert(db_dir, candidate_id="c2", dedupe_key="sha256:k2")
        assert r1 is True
        assert r2 is True
        rows = _sqls.sqlite_load_candidates(db_dir)
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# sqlite_load_candidates
# ---------------------------------------------------------------------------


class TestSqliteLoadCandidates:
    def test_empty_db_returns_empty_list(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "ledger" / "candidates"
        _sqls.init_candidate_db(db_dir)
        assert _sqls.sqlite_load_candidates(db_dir) == []

    def test_missing_db_returns_empty_list(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "ledger" / "candidates"
        assert _sqls.sqlite_load_candidates(db_dir) == []

    def test_round_trip_preserves_payload(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "ledger" / "candidates"
        _sqls.init_candidate_db(db_dir)
        payload = {"candidate_id": "cand-1", "x": 42}
        _sqls.sqlite_insert_candidate(
            db_dir,
            candidate_id="cand-1",
            program_id="prog",
            batch_id="b",
            source_document_key="sdk",
            dedupe_key="sha256:dk",
            staged_at=NOW.isoformat(),
            payload_json=json.dumps(payload),
        )
        rows = _sqls.sqlite_load_candidates(db_dir)
        assert len(rows) == 1
        assert rows[0]["candidate_id"] == "cand-1"
        assert rows[0]["x"] == 42


# ---------------------------------------------------------------------------
# sqlite_upsert_decision / sqlite_load_decisions
# ---------------------------------------------------------------------------


class TestSqliteDecisions:
    def _db_dir(self, tmp_path: Path) -> Path:
        db_dir = tmp_path / "ledger" / "candidates"
        _sqls.init_candidate_db(db_dir)
        return db_dir

    def _insert(
        self,
        db_dir: Path,
        *,
        candidate_id: str = "cand-1",
        kind: str = "approved",
        decided_at: str | None = None,
    ) -> None:
        dt = decided_at or NOW.isoformat()
        _sqls.sqlite_insert_decision(
            db_dir,
            candidate_id=candidate_id,
            kind=kind,
            decided_at=dt,
            triage_actor="operator",
            payload_json=json.dumps({"candidate_id": candidate_id, "kind": kind, "decided_at": dt}),
        )

    def test_empty_db_returns_empty_list(self, tmp_path: Path) -> None:
        db_dir = self._db_dir(tmp_path)
        assert _sqls.sqlite_load_decisions(db_dir) == []

    def test_insert_and_load_round_trip(self, tmp_path: Path) -> None:
        db_dir = self._db_dir(tmp_path)
        self._insert(db_dir, candidate_id="c1", kind="approved")
        rows = _sqls.sqlite_load_decisions(db_dir)
        assert len(rows) == 1
        assert rows[0]["kind"] == "approved"

    def test_multiple_decisions_for_same_candidate_all_kept(self, tmp_path: Path) -> None:
        # Append-only: both decisions persist; callers select latest by decided_at.
        db_dir = self._db_dir(tmp_path)
        t1 = "2026-06-25T10:00:00+00:00"
        t2 = "2026-06-25T11:00:00+00:00"
        self._insert(db_dir, kind="skipped", decided_at=t1)
        self._insert(db_dir, kind="approved", decided_at=t2)
        rows = _sqls.sqlite_load_decisions(db_dir)
        assert len(rows) == 2
        kinds = [r["kind"] for r in rows]
        assert kinds == ["skipped", "approved"]  # ordered by decided_at asc

    def test_all_rows_visible_regardless_of_timestamp_order(self, tmp_path: Path) -> None:
        # Append-only: no timestamp guard — all inserts are accepted.
        # Application layer (_latest_candidate_decisions) selects the newest.
        db_dir = self._db_dir(tmp_path)
        t_newer = "2026-06-25T11:00:00+00:00"
        t_older = "2026-06-25T10:00:00+00:00"
        self._insert(db_dir, kind="approved", decided_at=t_newer)
        self._insert(db_dir, kind="rejected", decided_at=t_older)
        rows = _sqls.sqlite_load_decisions(db_dir)
        assert len(rows) == 2
        # ordered by decided_at asc: older first
        assert rows[0]["kind"] == "rejected"
        assert rows[1]["kind"] == "approved"

    def test_multiple_candidates_each_get_one_row_per_insert(self, tmp_path: Path) -> None:
        db_dir = self._db_dir(tmp_path)
        self._insert(db_dir, candidate_id="c1", kind="approved")
        self._insert(db_dir, candidate_id="c2", kind="rejected")
        rows = _sqls.sqlite_load_decisions(db_dir)
        assert len(rows) == 2
        kinds = {r["kind"] for r in rows}
        assert kinds == {"approved", "rejected"}


# ---------------------------------------------------------------------------
# projection_outbox
# ---------------------------------------------------------------------------


class TestProjectionOutbox:
    def _db_dir(self, tmp_path: Path) -> Path:
        db_dir = tmp_path / "ledger" / "candidates"
        _sqls.init_candidate_db(db_dir)
        return db_dir

    def test_failed_pending_outbox_dead_letters_after_max_attempts(self, tmp_path: Path) -> None:
        db_dir = self._db_dir(tmp_path)
        _sqls.outbox_enqueue(
            db_dir,
            outbox_id="outbox-1",
            candidate_id="cand-1",
            program_id="prog",
            source_event_id="event-1",
            enqueued_at=NOW.isoformat(),
        )

        for attempt in range(_sqls.OUTBOX_MAX_ATTEMPTS - 1):
            status = _sqls.outbox_mark_failed(
                db_dir,
                outbox_id="outbox-1",
                attempted_at=(NOW.replace(minute=attempt)).isoformat(),
                failure_reason=f"projection failure {attempt}",
            )
            assert status == _sqls.OUTBOX_STATUS_PENDING

        status = _sqls.outbox_mark_failed(
            db_dir,
            outbox_id="outbox-1",
            attempted_at=NOW.isoformat(),
            failure_reason="poison projection event",
        )

        assert status == _sqls.OUTBOX_STATUS_DEAD_LETTER
        assert _sqls.outbox_list_pending(db_dir, program_id="prog") == []
        dead_letters = _sqls.outbox_list_dead_letters(db_dir, program_id="prog")
        assert len(dead_letters) == 1
        assert dead_letters[0]["outbox_id"] == "outbox-1"
        assert dead_letters[0]["attempt_count"] == _sqls.OUTBOX_MAX_ATTEMPTS
        assert dead_letters[0]["failure_reason"] == "poison projection event"


# ---------------------------------------------------------------------------
# migrate_jsonl_to_sqlite
# ---------------------------------------------------------------------------


class TestMigrateJsonlToSqlite:
    def _write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n",
            encoding="utf-8",
        )

    def _base_record(self, candidate_id: str, dedupe_key: str | None = None) -> dict:
        return {
            "candidate_id": candidate_id,
            "program_id": "prog",
            "proposed_event_type": "milestone.completed.v1",
            "proposed_payload": {"milestone_id": "m1", "completed_on": "2026-06-25"},
            "proposed_occurred_at": NOW.isoformat(),
            "proposed_temporal_confidence": "exact",
            "proposed_confidence": "medium",
            "source_ref": source_ref_to_dict(_email_ref()),
            "pipeline": "rev_mail",
            "extraction_confidence": 0.9,
            "entity_resolution": [],
            "dedupe_key": dedupe_key or f"sha256:{candidate_id}",
            "dedupe_core_hash": "sha256:core",
            "source_document_key": "sdk-1",
            "corroborating_refs": [],
            "batch_id": "b1",
            "staged_at": NOW.isoformat(),
        }

    def test_no_jsonl_files_returns_zeros(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "ledger" / "candidates"
        _sqls.init_candidate_db(db_dir)
        pending = db_dir / "pending.jsonl"
        triaged = db_dir / "triaged.jsonl"
        c, d = _sqls.migrate_jsonl_to_sqlite(db_dir, pending_path=pending, triaged_path=triaged)
        assert c == 0 and d == 0

    def test_imports_current_pending_file(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "ledger" / "candidates"
        pending = db_dir / "pending.jsonl"
        triaged = db_dir / "triaged.jsonl"
        _sqls.init_candidate_db(db_dir)
        self._write_jsonl(pending, [self._base_record("c1"), self._base_record("c2", "sha256:c2")])
        c, d = _sqls.migrate_jsonl_to_sqlite(db_dir, pending_path=pending, triaged_path=triaged)
        assert c == 2 and d == 0
        rows = _sqls.sqlite_load_candidates(db_dir)
        assert {r["candidate_id"] for r in rows} == {"c1", "c2"}

    def test_imports_rotated_files(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "ledger" / "candidates"
        pending = db_dir / "pending.jsonl"
        triaged = db_dir / "triaged.jsonl"
        rotated_dir = db_dir / "rotated"
        rotated_dir.mkdir(parents=True)
        self._write_jsonl(
            rotated_dir / "pending.20260625T120000Z.1.jsonl",
            [self._base_record("c-rotated", "sha256:rotated")],
        )
        self._write_jsonl(pending, [self._base_record("c-current", "sha256:current")])
        _sqls.init_candidate_db(db_dir)
        c, _ = _sqls.migrate_jsonl_to_sqlite(db_dir, pending_path=pending, triaged_path=triaged)
        assert c == 2
        rows = _sqls.sqlite_load_candidates(db_dir)
        ids = {r["candidate_id"] for r in rows}
        assert ids == {"c-rotated", "c-current"}

    def test_idempotent_when_db_already_has_rows(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "ledger" / "candidates"
        pending = db_dir / "pending.jsonl"
        triaged = db_dir / "triaged.jsonl"
        _sqls.init_candidate_db(db_dir)
        self._write_jsonl(pending, [self._base_record("c1")])
        _sqls.migrate_jsonl_to_sqlite(db_dir, pending_path=pending, triaged_path=triaged)
        # second call must be a no-op
        c2, d2 = _sqls.migrate_jsonl_to_sqlite(db_dir, pending_path=pending, triaged_path=triaged)
        assert c2 == 0 and d2 == 0
        assert len(_sqls.sqlite_load_candidates(db_dir)) == 1

    def test_imports_triaged_decisions(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "ledger" / "candidates"
        pending = db_dir / "pending.jsonl"
        triaged = db_dir / "triaged.jsonl"
        _sqls.init_candidate_db(db_dir)
        decision = {
            "candidate_id": "c1",
            "kind": "approved",
            "decided_at": NOW.isoformat(),
            "triage_actor": "operator",
        }
        self._write_jsonl(pending, [self._base_record("c1")])
        self._write_jsonl(triaged, [decision])
        _sqls.migrate_jsonl_to_sqlite(db_dir, pending_path=pending, triaged_path=triaged)
        rows = _sqls.sqlite_load_decisions(db_dir)
        assert len(rows) == 1
        assert rows[0]["kind"] == "approved"

    def test_legacy_record_without_evidence_refs_migrates(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "ledger" / "candidates"
        pending = db_dir / "pending.jsonl"
        triaged = db_dir / "triaged.jsonl"
        _sqls.init_candidate_db(db_dir)
        legacy = self._base_record("c-legacy")
        # Simulate legacy format: no evidence_refs / schema_version fields
        legacy.pop("evidence_refs", None)
        legacy.pop("schema_version", None)
        self._write_jsonl(pending, [legacy])
        c, _ = _sqls.migrate_jsonl_to_sqlite(db_dir, pending_path=pending, triaged_path=triaged)
        assert c == 1
        rows = _sqls.sqlite_load_candidates(db_dir)
        assert rows[0]["candidate_id"] == "c-legacy"
        assert "evidence_refs" not in rows[0]  # preserved in payload_json as-is


# ---------------------------------------------------------------------------
# Integration: candidate_store public API backed by SQLite
# ---------------------------------------------------------------------------


class TestCandidateStoreIntegration:
    """Verifies that the public API in candidate_store.py routes through SQLite."""

    def test_append_and_load_round_trip(self, tmp_path: Path) -> None:
        c = _make_candidate()
        assert append_candidate(c, programs_root=tmp_path) is True
        loaded = load_pending_candidates("prog", programs_root=tmp_path)
        assert len(loaded) == 1
        assert loaded[0].candidate_id == "cand-1"

    def test_idempotency_on_candidate_id(self, tmp_path: Path) -> None:
        c = _make_candidate()
        r1 = append_candidate(c, programs_root=tmp_path)
        r2 = append_candidate(c, programs_root=tmp_path)
        assert r1 is True
        assert r2 is False
        assert len(load_pending_candidates("prog", programs_root=tmp_path)) == 1

    def test_same_dedupe_key_different_candidate_id_both_stored(self, tmp_path: Path) -> None:
        # dedupe_key uniqueness is handled upstream (before staging); at the store
        # level, candidate_id is the only uniqueness key.
        c1 = _make_candidate(candidate_id="c1", dedupe_key="sha256:shared")
        c2 = _make_candidate(candidate_id="c2", dedupe_key="sha256:shared")
        r1 = append_candidate(c1, programs_root=tmp_path)
        r2 = append_candidate(c2, programs_root=tmp_path)
        assert r1 is True
        assert r2 is True
        assert len(load_pending_candidates("prog", programs_root=tmp_path)) == 2

    def test_different_candidates_both_stored(self, tmp_path: Path) -> None:
        c1 = _make_candidate(candidate_id="c1", dedupe_key="sha256:k1")
        c2 = _make_candidate(candidate_id="c2", dedupe_key="sha256:k2")
        append_candidate(c1, programs_root=tmp_path)
        append_candidate(c2, programs_root=tmp_path)
        loaded = load_pending_candidates("prog", programs_root=tmp_path)
        assert len(loaded) == 2

    def test_append_decision_and_load(self, tmp_path: Path) -> None:
        c = _make_candidate()
        append_candidate(c, programs_root=tmp_path)
        d = _make_decision()
        append_triage_decision(d, program_id="prog", programs_root=tmp_path)
        decisions = load_triage_decisions("prog", programs_root=tmp_path)
        assert len(decisions) == 1
        assert decisions[0].kind == "approved"

    def test_active_candidates_excludes_decided(self, tmp_path: Path) -> None:
        c = _make_candidate()
        append_candidate(c, programs_root=tmp_path)
        # Before decision: 1 active
        assert len(active_candidates("prog", programs_root=tmp_path)) == 1
        d = _make_decision(kind="approved")
        append_triage_decision(d, program_id="prog", programs_root=tmp_path)
        # After approval: 0 active
        assert len(active_candidates("prog", programs_root=tmp_path)) == 0

    def test_jsonl_audit_file_still_written(self, tmp_path: Path) -> None:
        c = _make_candidate()
        append_candidate(c, programs_root=tmp_path)
        pending_path = get_pending_path("prog", programs_root=tmp_path)
        assert pending_path.exists()
        lines = [l for l in pending_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["candidate_id"] == "cand-1"

    def test_sqlite_db_is_created_on_first_use(self, tmp_path: Path) -> None:
        c = _make_candidate()
        append_candidate(c, programs_root=tmp_path)
        db_dir = get_candidate_dir("prog", programs_root=tmp_path)
        assert (db_dir / "candidates.db").exists()

    def test_auto_migration_from_existing_jsonl(self, tmp_path: Path) -> None:
        """Programs with existing JSONL are automatically migrated on first API call."""
        program_id = "migprog"
        pending_path = get_pending_path(program_id, programs_root=tmp_path)
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        legacy = {
            "candidate_id": "cand-legacy",
            "program_id": program_id,
            "proposed_event_type": "milestone.completed.v1",
            "proposed_payload": {"milestone_id": "m1", "completed_on": "2026-06-25"},
            "proposed_occurred_at": NOW.isoformat(),
            "proposed_temporal_confidence": "exact",
            "proposed_confidence": "medium",
            "source_ref": source_ref_to_dict(_email_ref()),
            "pipeline": "rev_mail",
            "extraction_confidence": 0.9,
            "entity_resolution": [],
            "dedupe_key": "sha256:migration-test",
            "dedupe_core_hash": "sha256:core",
            "source_document_key": "sdk-mig",
            "corroborating_refs": [],
            "batch_id": "b1",
            "staged_at": NOW.isoformat(),
        }
        pending_path.write_text(json.dumps(legacy, sort_keys=True) + "\n", encoding="utf-8")

        # No SQLite DB yet — first call triggers migration
        loaded = load_pending_candidates(program_id, programs_root=tmp_path)
        assert len(loaded) == 1
        assert loaded[0].candidate_id == "cand-legacy"

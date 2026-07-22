from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Iterator

from src.core._db import open_program_db
from src.core.knowledge_candidate_store import KnowledgeCandidate, KnowledgeCandidateDecisionRecord, load_pending_candidates, load_triage_decisions
from src.core.knowledge_claim_store import KnowledgeClaimRevision, load_all_claim_revisions


SCHEMA_VERSION = "1"
SKIP_EXPIRY_DAYS = 90


def get_knowledge_index_path(*, knowledge_root: Path) -> Path:
    return knowledge_root / "knowledge-index.sqlite3"


@contextmanager
def connect_knowledge_index(*, knowledge_root: Path) -> Iterator[sqlite3.Connection]:
    path = get_knowledge_index_path(knowledge_root=knowledge_root)
    with open_program_db(path, durability="strict") as connection:
        _ensure_schema(connection)
        yield connection


def ensure_knowledge_index(*, knowledge_root: Path, programs_root: Path) -> bool:
    with connect_knowledge_index(knowledge_root=knowledge_root) as connection:
        row = connection.execute("SELECT schema_version FROM index_meta LIMIT 1").fetchone()
    if row is not None and row[0] == SCHEMA_VERSION:
        return False
    rebuild_knowledge_index(knowledge_root=knowledge_root, programs_root=programs_root)
    return True


def rebuild_knowledge_index(*, knowledge_root: Path, programs_root: Path) -> None:
    with connect_knowledge_index(knowledge_root=knowledge_root) as connection:
        connection.execute("DELETE FROM vault_refs")
        connection.execute("DELETE FROM index_meta")
        for revision in load_all_claim_revisions(knowledge_root=knowledge_root):
            _upsert_claim_refs(connection, revision)
        for candidate in load_pending_candidates(programs_root=programs_root):
            _upsert_candidate_refs(connection, candidate)
        for decision in load_triage_decisions(programs_root=programs_root):
            _apply_candidate_decision(connection, decision)
        connection.execute(
            "INSERT INTO index_meta (schema_version, rebuilt_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
        )


def upsert_claim_vault_refs(revision: KnowledgeClaimRevision, *, knowledge_root: Path) -> None:
    with connect_knowledge_index(knowledge_root=knowledge_root) as connection:
        _upsert_claim_refs(connection, revision)
        _touch_meta(connection)


def remove_claim_vault_refs(claim_id: str, *, knowledge_root: Path) -> None:
    with connect_knowledge_index(knowledge_root=knowledge_root) as connection:
        connection.execute("DELETE FROM vault_refs WHERE ref_owner_id = ? AND ref_owner_type = 'claim'", (claim_id,))
        _touch_meta(connection)


def upsert_candidate_vault_refs(candidate: KnowledgeCandidate, *, knowledge_root: Path) -> None:
    with connect_knowledge_index(knowledge_root=knowledge_root) as connection:
        _upsert_candidate_refs(connection, candidate)
        _touch_meta(connection)


def apply_candidate_decision_to_index(decision: KnowledgeCandidateDecisionRecord, *, knowledge_root: Path) -> None:
    with connect_knowledge_index(knowledge_root=knowledge_root) as connection:
        _apply_candidate_decision(connection, decision)
        _touch_meta(connection)


def load_live_vault_hashes(*, knowledge_root: Path, as_of: datetime | None = None) -> tuple[str, ...]:
    now = (as_of or datetime.now(timezone.utc)).isoformat()
    with connect_knowledge_index(knowledge_root=knowledge_root) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT vault_hash
            FROM vault_refs
            WHERE expires_at IS NULL OR expires_at > ?
            ORDER BY vault_hash
            """,
            (now,),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _upsert_claim_refs(connection: sqlite3.Connection, revision: KnowledgeClaimRevision) -> None:
    connection.execute("DELETE FROM vault_refs WHERE ref_owner_id = ? AND ref_owner_type = 'claim'", (revision.claim_id,))
    vault_hash = getattr(revision.source_ref, "vault_hash", None)
    if not isinstance(vault_hash, str) or not vault_hash:
        return
    connection.execute(
        "INSERT OR REPLACE INTO vault_refs (vault_hash, ref_owner_id, ref_owner_type, ref_role, expires_at) VALUES (?, ?, 'claim', 'source_ref', NULL)",
        (vault_hash, revision.claim_id),
    )


def _upsert_candidate_refs(connection: sqlite3.Connection, candidate: KnowledgeCandidate) -> None:
    connection.execute("DELETE FROM vault_refs WHERE ref_owner_id = ? AND ref_owner_type = 'candidate'", (candidate.candidate_id,))
    _insert_candidate_ref(connection, candidate.candidate_id, getattr(candidate.source_ref, "vault_hash", None), "source_ref")
    for ref in candidate.corroborating_refs:
        _insert_candidate_ref(connection, candidate.candidate_id, getattr(ref, "vault_hash", None), "corroborating_ref")


def _insert_candidate_ref(connection: sqlite3.Connection, candidate_id: str, vault_hash: object, role: str) -> None:
    if not isinstance(vault_hash, str) or not vault_hash:
        return
    connection.execute(
        "INSERT OR REPLACE INTO vault_refs (vault_hash, ref_owner_id, ref_owner_type, ref_role, expires_at) VALUES (?, ?, 'candidate', ?, NULL)",
        (vault_hash, candidate_id, role),
    )


def _apply_candidate_decision(connection: sqlite3.Connection, decision: KnowledgeCandidateDecisionRecord) -> None:
    if decision.kind == "skipped":
        expires_at = (decision.decided_at + timedelta(days=SKIP_EXPIRY_DAYS)).isoformat()
        connection.execute(
            "UPDATE vault_refs SET expires_at = ? WHERE ref_owner_id = ? AND ref_owner_type = 'candidate'",
            (expires_at, decision.candidate_id),
        )
        return
    connection.execute("DELETE FROM vault_refs WHERE ref_owner_id = ? AND ref_owner_type = 'candidate'", (decision.candidate_id,))


def _touch_meta(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM index_meta")
    connection.execute(
        "INSERT INTO index_meta (schema_version, rebuilt_at) VALUES (?, ?)",
        (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
    )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS vault_refs (
          vault_hash TEXT NOT NULL,
          ref_owner_id TEXT NOT NULL,
          ref_owner_type TEXT NOT NULL,
          ref_role TEXT NOT NULL,
          expires_at TEXT,
          PRIMARY KEY (vault_hash, ref_owner_id, ref_owner_type, ref_role)
        );
        CREATE INDEX IF NOT EXISTS ix_knowledge_vr_hash ON vault_refs(vault_hash);
        CREATE INDEX IF NOT EXISTS ix_knowledge_vr_expiry ON vault_refs(expires_at);

        CREATE TABLE IF NOT EXISTS index_meta (
          schema_version TEXT NOT NULL,
          rebuilt_at TEXT NOT NULL
        );
        """
    )

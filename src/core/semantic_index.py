from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
import re
import sqlite3
from typing import cast

from src.core._db import open_program_db
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.edition_resolver import resolve_edition_paths
from src.core.incident_journal_store import get_incident_journal_path, read_incident_entries
from src.core.models import RiskLevel
from src.core.snapshot_store import ARCHIVE_ROOT, get_archive_root, read_snapshot


@dataclass(frozen=True, slots=True)
class SemanticMatch:
    source_type: str
    issue_number: int | None
    generated_at: datetime
    excerpt: str
    score: float
    risk_level: RiskLevel | None = None
    source_ref: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticSimilarityMatch:
    source_type: str
    issue_number: int | None
    generated_at: datetime
    excerpt: str
    similarity: float
    risk_level: RiskLevel | None = None
    source_ref: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticIndexState:
    edition_name: str
    index_path: Path
    last_built_at: datetime | None
    latest_confirmed_issue: int | None
    latest_confirmed_at: datetime | None
    indexed_document_count: int
    semantic_index_dirty: bool
    dirty_reason: str | None
    last_optimized_at: datetime | None
    last_optimized_document_count: int


@dataclass(frozen=True, slots=True)
class _SemanticDocument:
    issue_number: int | None
    generated_at: datetime
    source_type: str
    excerpt: str
    risk_level: RiskLevel | None
    source_ref: str | None = None


_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_ISSUE_HEADING_PATTERN = re.compile(r"^issue\s+\d+$", re.IGNORECASE)
_RISK_RANKS = {
    RiskLevel.HIGH: 4,
    RiskLevel.MEDIUM: 3,
    RiskLevel.LOW: 2,
    RiskLevel.DONE: 1,
    RiskLevel.UNKNOWN: 0,
}


def search_archive_semantic_index(
    edition_name: str,
    query: str,
    *,
    archive_root: Path = ARCHIVE_ROOT,
    top_k: int = 5,
) -> tuple[SemanticMatch, ...]:
    return _search_semantic_index(
        edition_name,
        query,
        archive_root=archive_root,
        top_k=top_k,
        source_types=("narrative",),
    )


def search_history_semantic_index(
    edition_name: str,
    query: str,
    *,
    archive_root: Path = ARCHIVE_ROOT,
    top_k: int = 5,
) -> tuple[SemanticMatch, ...]:
    return _search_semantic_index(
        edition_name,
        query,
        archive_root=archive_root,
        top_k=top_k,
        source_types=("narrative", "incident"),
    )


def _search_semantic_index(
    edition_name: str,
    query: str,
    *,
    archive_root: Path,
    top_k: int,
    source_types: tuple[str, ...] | None,
) -> tuple[SemanticMatch, ...]:
    tokens = _normalize_tokens(query)
    if not tokens or top_k <= 0:
        return ()

    index_path = _ensure_archive_semantic_index(edition_name, archive_root=archive_root)
    with _connect_index(index_path) as connection:
        connection.row_factory = sqlite3.Row
        if _fts_enabled(connection):
            matches = _search_with_fts(connection, tokens, top_k, source_types=source_types)
            if matches:
                return matches
        return _search_without_fts(connection, tokens, top_k, source_types=source_types)


def rebuild_archive_semantic_index(
    edition_name: str,
    *,
    archive_root: Path = ARCHIVE_ROOT,
) -> Path:
    documents = _collect_semantic_documents(edition_name, archive_root=archive_root)
    index_path = get_semantic_index_path(edition_name, archive_root=archive_root)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    built_at = datetime.now(timezone.utc)

    with _connect_index(index_path) as connection:
        _ensure_schema(connection)
        connection.execute("DELETE FROM archive_documents")
        if _fts_enabled(connection):
            connection.execute("DELETE FROM archive_documents_fts")
        for document in documents:
            cursor = connection.execute(
                """
                INSERT INTO archive_documents (
                    issue_number,
                    generated_at,
                    source_type,
                    excerpt,
                    risk_level,
                    source_ref
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document.issue_number,
                    document.generated_at.isoformat(),
                    document.source_type,
                    document.excerpt,
                    None if document.risk_level is None else document.risk_level.value,
                    document.source_ref,
                ),
            )
            if _fts_enabled(connection):
                connection.execute(
                    "INSERT INTO archive_documents_fts(rowid, excerpt) VALUES (?, ?)",
                    (cursor.lastrowid, document.excerpt),
                )
        connection.commit()
    _write_semantic_index_state(
        edition_name,
        archive_root=archive_root,
        last_built_at=built_at,
        latest_confirmed_issue=_latest_confirmed_issue_number(edition_name, archive_root=archive_root),
        latest_confirmed_at=_latest_confirmed_generated_at(edition_name, archive_root=archive_root),
        indexed_document_count=len(documents),
        semantic_index_dirty=False,
        dirty_reason=None,
        last_optimized_at=built_at,
        last_optimized_document_count=len(documents),
    )
    return index_path


def update_archive_semantic_index_for_issue(
    edition_name: str,
    issue_number: int,
    *,
    archive_root: Path = ARCHIVE_ROOT,
) -> Path:
    index_path = get_semantic_index_path(edition_name, archive_root=archive_root)
    if not index_path.exists():
        return rebuild_archive_semantic_index(edition_name, archive_root=archive_root)

    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    entry = next(
        (
            candidate
            for candidate in archive_index.issues
            if candidate.kind == "confirmed" and candidate.issue_number == issue_number and candidate.md_path is not None
        ),
        None,
    )
    if entry is None:
        return rebuild_archive_semantic_index(edition_name, archive_root=archive_root)

    documents = _documents_for_archive_entry(entry)
    prior_state = load_semantic_index_state(edition_name, archive_root=archive_root)
    with _connect_index(index_path) as connection:
        _ensure_schema(connection)
        _delete_issue_documents(connection, issue_number)
        for document in documents:
            cursor = connection.execute(
                """
                INSERT INTO archive_documents (
                    issue_number,
                    generated_at,
                    source_type,
                    excerpt,
                    risk_level,
                    source_ref
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document.issue_number,
                    document.generated_at.isoformat(),
                    document.source_type,
                    document.excerpt,
                    None if document.risk_level is None else document.risk_level.value,
                    document.source_ref,
                ),
            )
            if _fts_enabled(connection):
                connection.execute(
                    "INSERT INTO archive_documents_fts(rowid, excerpt) VALUES (?, ?)",
                    (cursor.lastrowid, document.excerpt),
                )
        connection.commit()
        document_count = _count_documents(connection)

    latest_confirmed = find_latest_confirmed_entry(archive_index)
    _write_semantic_index_state(
        edition_name,
        archive_root=archive_root,
        last_built_at=datetime.now(timezone.utc),
        latest_confirmed_issue=(latest_confirmed.issue_number if latest_confirmed is not None else None),
        latest_confirmed_at=(latest_confirmed.generated_at if latest_confirmed is not None else None),
        indexed_document_count=document_count,
        semantic_index_dirty=False,
        dirty_reason=None,
        last_optimized_at=(prior_state.last_optimized_at if prior_state is not None else None),
        last_optimized_document_count=(prior_state.last_optimized_document_count if prior_state is not None else document_count),
    )
    return index_path


def optimize_archive_semantic_index(
    edition_name: str,
    *,
    archive_root: Path = ARCHIVE_ROOT,
    if_needed: bool = False,
) -> bool:
    index_path = _ensure_archive_semantic_index(edition_name, archive_root=archive_root)
    state = load_semantic_index_state(edition_name, archive_root=archive_root)
    with _connect_index(index_path) as connection:
        _ensure_schema(connection)
        document_count = _count_documents(connection)
        if if_needed and state is not None and (document_count - state.last_optimized_document_count) <= 1000:
            return False
        if _fts_enabled(connection):
            connection.execute("INSERT INTO archive_documents_fts(archive_documents_fts) VALUES ('optimize')")
        connection.commit()
        connection.execute("VACUUM")
        connection.execute("ANALYZE")

    prior_state = load_semantic_index_state(edition_name, archive_root=archive_root)
    _write_semantic_index_state(
        edition_name,
        archive_root=archive_root,
        last_built_at=(prior_state.last_built_at if prior_state is not None else datetime.now(timezone.utc)),
        latest_confirmed_issue=(prior_state.latest_confirmed_issue if prior_state is not None else _latest_confirmed_issue_number(edition_name, archive_root=archive_root)),
        latest_confirmed_at=(prior_state.latest_confirmed_at if prior_state is not None else _latest_confirmed_generated_at(edition_name, archive_root=archive_root)),
        indexed_document_count=document_count,
        semantic_index_dirty=False,
        dirty_reason=None,
        last_optimized_at=datetime.now(timezone.utc),
        last_optimized_document_count=document_count,
    )
    return True


def find_archive_similarity_match(
    edition_name: str,
    text: str,
    *,
    archive_root: Path = ARCHIVE_ROOT,
    min_similarity: float = 0.92,
    top_k: int = 5,
) -> SemanticSimilarityMatch | None:
    normalized_text = _normalize_excerpt(text)
    query_tokens = _normalize_tokens(normalized_text)
    if len(query_tokens) < 3:
        return None

    query_text = " ".join(query_tokens[: min(16, len(query_tokens))])
    candidates = search_archive_semantic_index(
        edition_name,
        query_text,
        archive_root=archive_root,
        top_k=top_k,
    )
    if not candidates:
        return None

    query_signature = " ".join(query_tokens)
    best_match: SemanticMatch | None = None
    best_similarity = 0.0
    for candidate in candidates:
        candidate_signature = " ".join(_normalize_tokens(candidate.excerpt))
        if not candidate_signature:
            continue
        similarity = SequenceMatcher(None, query_signature, candidate_signature).ratio()
        if similarity <= best_similarity:
            continue
        best_similarity = similarity
        best_match = candidate

    if best_match is None or best_similarity < min_similarity:
        return None

    return SemanticSimilarityMatch(
        source_type=best_match.source_type,
        issue_number=best_match.issue_number,
        generated_at=best_match.generated_at,
        excerpt=best_match.excerpt,
        similarity=best_similarity,
        risk_level=best_match.risk_level,
        source_ref=best_match.source_ref,
    )


def load_semantic_index_state(
    edition_name: str,
    *,
    archive_root: Path = ARCHIVE_ROOT,
) -> SemanticIndexState | None:
    state_path = get_semantic_index_state_path(edition_name, archive_root=archive_root)
    if not state_path.exists():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    editions = payload.get("editions")
    if not isinstance(editions, dict):
        return None
    raw_state = editions.get(edition_name)
    if not isinstance(raw_state, dict):
        return None
    return SemanticIndexState(
        edition_name=edition_name,
        index_path=get_semantic_index_path(edition_name, archive_root=archive_root),
        last_built_at=_parse_optional_datetime(raw_state.get("last_built_at")),
        latest_confirmed_issue=_parse_optional_int(raw_state.get("latest_confirmed_issue")),
        latest_confirmed_at=_parse_optional_datetime(raw_state.get("latest_confirmed_at")),
        indexed_document_count=_parse_optional_int(raw_state.get("indexed_document_count")) or 0,
        semantic_index_dirty=bool(raw_state.get("semantic_index_dirty", False)),
        dirty_reason=_parse_optional_string(raw_state.get("dirty_reason")),
        last_optimized_at=_parse_optional_datetime(raw_state.get("last_optimized_at")),
        last_optimized_document_count=_parse_optional_int(raw_state.get("last_optimized_document_count")) or 0,
    )


def mark_semantic_index_dirty(
    edition_name: str,
    reason: str,
    *,
    archive_root: Path = ARCHIVE_ROOT,
) -> None:
    prior_state = load_semantic_index_state(edition_name, archive_root=archive_root)
    _write_semantic_index_state(
        edition_name,
        archive_root=archive_root,
        last_built_at=(prior_state.last_built_at if prior_state is not None else None),
        latest_confirmed_issue=(prior_state.latest_confirmed_issue if prior_state is not None else _latest_confirmed_issue_number(edition_name, archive_root=archive_root)),
        latest_confirmed_at=(prior_state.latest_confirmed_at if prior_state is not None else _latest_confirmed_generated_at(edition_name, archive_root=archive_root)),
        indexed_document_count=(prior_state.indexed_document_count if prior_state is not None else 0),
        semantic_index_dirty=True,
        dirty_reason=reason.strip(),
        last_optimized_at=(prior_state.last_optimized_at if prior_state is not None else None),
        last_optimized_document_count=(prior_state.last_optimized_document_count if prior_state is not None else 0),
    )


def get_semantic_index_path(edition_name: str, *, archive_root: Path = ARCHIVE_ROOT) -> Path:
    return get_semantic_index_root(edition_name, archive_root=archive_root) / f"{edition_name}.sqlite3"


def get_semantic_index_root(edition_name: str, *, archive_root: Path = ARCHIVE_ROOT) -> Path:
    edition_root = get_archive_root(edition_name, archive_root)
    return edition_root.parent.parent / "semantic_index"


def get_semantic_index_state_path(edition_name: str, *, archive_root: Path = ARCHIVE_ROOT) -> Path:
    edition_root = get_archive_root(edition_name, archive_root)
    return edition_root.parent.parent / "semantic_index_state.json"


def _collect_archive_documents(edition_name: str, *, archive_root: Path) -> tuple[_SemanticDocument, ...]:
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    documents: list[_SemanticDocument] = []
    for entry in sorted(archive_index.issues, key=lambda issue: issue.issue_number):
        documents.extend(_documents_for_archive_entry(entry))
    return tuple(documents)


def _collect_semantic_documents(edition_name: str, *, archive_root: Path) -> tuple[_SemanticDocument, ...]:
    documents = list(_collect_archive_documents(edition_name, archive_root=archive_root))
    documents.extend(_collect_incident_documents(edition_name, archive_root=archive_root))
    return tuple(documents)


def _documents_for_archive_entry(entry: object) -> tuple[_SemanticDocument, ...]:
    md_path = getattr(entry, "md_path", None)
    if getattr(entry, "kind", None) != "confirmed" or md_path is None:
        return ()
    markdown_path = Path(md_path)
    if not markdown_path.exists():
        return ()
    issue_number = int(getattr(entry, "issue_number"))
    risk_level = _load_issue_risk_level(getattr(entry, "snapshot_path", None))
    return tuple(
        _SemanticDocument(
            issue_number=issue_number,
            generated_at=getattr(entry, "generated_at"),
            source_type="narrative",
            excerpt=excerpt,
            risk_level=risk_level,
            source_ref=f"Issue {issue_number:03d}",
        )
        for excerpt in _extract_markdown_excerpts(markdown_path.read_text(encoding="utf-8"))
    )


def _collect_incident_documents(edition_name: str, *, archive_root: Path) -> tuple[_SemanticDocument, ...]:
    resolved_paths = resolve_edition_paths(
        edition_name,
        programs_root=archive_root.parent / "programs",
    )
    if resolved_paths is None:
        return ()
    entries = read_incident_entries(
        resolved_paths.program_id,
        programs_root=archive_root.parent / "programs",
    )
    documents: list[_SemanticDocument] = []
    for entry in entries:
        excerpt = _normalize_excerpt(_build_incident_excerpt(entry))
        if not excerpt:
            continue
        documents.append(
            _SemanticDocument(
                issue_number=None,
                generated_at=entry.recorded_at,
                source_type="incident",
                excerpt=excerpt,
                risk_level=_incident_risk_level(entry.severity),
                source_ref=f"IcM {entry.incident_id}",
            )
        )
    return tuple(documents)


def _build_incident_excerpt(entry: object) -> str:
    linked_work_item_ids = tuple(getattr(entry, "linked_work_item_ids", ()) or ())
    ado_entity_refs = tuple(getattr(entry, "ado_entity_refs", ()) or ())
    parts = [f"IcM {getattr(entry, 'incident_id')}"]
    severity = getattr(entry, "severity", None)
    if severity is not None:
        parts.append(f"sev {severity}")
    owning_team = getattr(entry, "owning_team", None)
    if owning_team:
        parts.append(str(owning_team))
    workstream_id = getattr(entry, "workstream_id", None)
    if workstream_id:
        parts.append(f"workstream {workstream_id}")
    query_id = getattr(entry, "query_id", None)
    if query_id:
        parts.append(f"query {query_id}")
    if linked_work_item_ids:
        parts.append("work items " + " ".join(str(work_item_id) for work_item_id in linked_work_item_ids))
    if ado_entity_refs:
        parts.append("refs " + " ".join(str(ref) for ref in ado_entity_refs))
    parts.append(str(getattr(entry, "belief_change_summary")))
    return ". ".join(part for part in parts if part)


def _incident_risk_level(severity: int | None) -> RiskLevel | None:
    if severity is None:
        return None
    if severity <= 2:
        return RiskLevel.HIGH
    if severity == 3:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _extract_markdown_excerpts(markdown_body: str) -> tuple[str, ...]:
    blocks = [
        _normalize_excerpt(block)
        for block in re.split(r"\n\s*\n", markdown_body)
    ]
    blocks = [block for block in blocks if block]
    excerpts: list[str] = []
    index = 0
    while index < len(blocks):
        current = blocks[index]
        if _is_issue_heading(current):
            index += 1
            continue
        if _is_heading_only(current) and index + 1 < len(blocks):
            combined = _normalize_excerpt(f"{current} {blocks[index + 1]}")
            if combined:
                excerpts.append(combined)
            index += 2
            continue
        excerpts.append(current)
        index += 1
    return tuple(excerpts)


def _normalize_excerpt(text: str) -> str:
    replaced = _MARKDOWN_LINK_PATTERN.sub(r"\1", text)
    cleaned_lines = []
    for raw_line in replaced.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[#>*\-]+\s*", "", line)
        if _is_issue_heading(line):
            continue
        cleaned_lines.append(line)
    normalized = _WHITESPACE_PATTERN.sub(" ", " ".join(cleaned_lines)).strip()
    return normalized


def _is_heading_only(text: str) -> bool:
    return len(text) <= 80 and ":" not in text and text.count(".") <= 1


def _is_issue_heading(text: str) -> bool:
    return _ISSUE_HEADING_PATTERN.fullmatch(text.strip()) is not None


def _load_issue_risk_level(snapshot_path_value: str | None) -> RiskLevel | None:
    if not snapshot_path_value:
        return None
    snapshot_path = Path(snapshot_path_value)
    if not snapshot_path.exists():
        return None
    snapshot = read_snapshot(snapshot_path)
    risks = [dimension.risk for dimension in snapshot.scorecards]
    if not risks:
        risks = [item.risk_level for item in snapshot.items]
    if not risks:
        return None
    return max(risks, key=lambda risk: _RISK_RANKS.get(risk, -1))


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        connection.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS archive_documents (
            doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_number INTEGER,
            generated_at TEXT NOT NULL,
            source_type TEXT NOT NULL,
            excerpt TEXT NOT NULL,
            risk_level TEXT NULL,
            source_ref TEXT NULL
        )
        """
    )
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(archive_documents)").fetchall()
    }
    if "source_ref" not in columns:
        connection.execute("ALTER TABLE archive_documents ADD COLUMN source_ref TEXT NULL")
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS archive_documents_fts USING fts5(excerpt, tokenize='porter unicode61')"
        )
    except sqlite3.OperationalError:
        pass


def _fts_enabled(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name = 'archive_documents_fts'"
    ).fetchone()
    return row is not None


def _search_with_fts(
    connection: sqlite3.Connection,
    tokens: tuple[str, ...],
    top_k: int,
    *,
    source_types: tuple[str, ...] | None,
) -> tuple[SemanticMatch, ...]:
    match_expression = " OR ".join(f'"{token}"' for token in tokens)
    source_type_filter = ""
    parameters: list[object] = [match_expression]
    if source_types:
        placeholders = ", ".join("?" for _ in source_types)
        source_type_filter = f" AND documents.source_type IN ({placeholders})"
        parameters.extend(source_types)
    parameters.append(top_k)
    rows = connection.execute(
        f"""
        SELECT
            documents.issue_number,
            documents.generated_at,
            documents.source_type,
            documents.excerpt,
            documents.risk_level,
            documents.source_ref,
            bm25(archive_documents_fts) AS score
        FROM archive_documents_fts
        INNER JOIN archive_documents AS documents
            ON documents.doc_id = archive_documents_fts.rowid
        WHERE archive_documents_fts MATCH ?
        {source_type_filter}
        ORDER BY score ASC, documents.generated_at DESC, documents.doc_id DESC
        LIMIT ?
        """,
        tuple(parameters),
    ).fetchall()
    return tuple(_row_to_match(row) for row in rows)


def _search_without_fts(
    connection: sqlite3.Connection,
    tokens: tuple[str, ...],
    top_k: int,
    *,
    source_types: tuple[str, ...] | None,
) -> tuple[SemanticMatch, ...]:
    rows = connection.execute(
        "SELECT issue_number, generated_at, source_type, excerpt, risk_level, source_ref FROM archive_documents"
    ).fetchall()
    scored_rows: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        if source_types and str(row["source_type"]) not in source_types:
            continue
        excerpt_tokens = set(_normalize_tokens(str(row["excerpt"])))
        if not excerpt_tokens:
            continue
        overlap = len(excerpt_tokens.intersection(tokens))
        if overlap == 0:
            continue
        score = -float(overlap) / float(len(tokens))
        scored_rows.append((score, row))
    scored_rows.sort(
        key=lambda item: (
            item[0],
            -datetime.fromisoformat(str(item[1]["generated_at"])).timestamp(),
        )
    )
    return tuple(_row_to_match(row, score=score) for score, row in scored_rows[:top_k])


def _row_to_match(row: sqlite3.Row, *, score: float | None = None) -> SemanticMatch:
    raw_risk_level = row["risk_level"]
    risk_level = None if raw_risk_level in (None, "") else RiskLevel.from_string(str(raw_risk_level))
    resolved_score = float(row["score"]) if score is None and "score" in row.keys() else (0.0 if score is None else score)
    raw_issue_number = row["issue_number"]
    return SemanticMatch(
        source_type=str(row["source_type"]),
        issue_number=(None if raw_issue_number in (None, "") else int(raw_issue_number)),
        generated_at=datetime.fromisoformat(str(row["generated_at"])),
        excerpt=str(row["excerpt"]),
        score=resolved_score,
        risk_level=risk_level,
        source_ref=(None if row["source_ref"] in (None, "") else str(row["source_ref"])),
    )


def _normalize_tokens(text: str) -> tuple[str, ...]:
    seen: set[str] = set()
    tokens: list[str] = []
    for token in _TOKEN_PATTERN.findall(text.casefold()):
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tuple(tokens)


@contextmanager
def _connect_index(index_path: Path):
    with open_program_db(index_path) as connection:
        yield connection


def _ensure_archive_semantic_index(edition_name: str, *, archive_root: Path) -> Path:
    index_path = get_semantic_index_path(edition_name, archive_root=archive_root)
    if index_path.exists() and not _incident_journal_changed_since_last_build(edition_name, archive_root=archive_root):
        return index_path
    return rebuild_archive_semantic_index(edition_name, archive_root=archive_root)


def _incident_journal_changed_since_last_build(edition_name: str, *, archive_root: Path) -> bool:
    state = load_semantic_index_state(edition_name, archive_root=archive_root)
    journal_path = _get_incident_journal_path_for_edition(edition_name, archive_root=archive_root)
    if journal_path is None or not journal_path.exists():
        return False
    if state is None or state.last_built_at is None:
        return True
    journal_updated_at = datetime.fromtimestamp(journal_path.stat().st_mtime, tz=timezone.utc)
    return journal_updated_at > state.last_built_at


def _get_incident_journal_path_for_edition(edition_name: str, *, archive_root: Path) -> Path | None:
    resolved_paths = resolve_edition_paths(
        edition_name,
        programs_root=archive_root.parent / "programs",
    )
    if resolved_paths is None:
        return None
    return get_incident_journal_path(resolved_paths.program_id, programs_root=archive_root.parent / "programs")


def _delete_issue_documents(connection: sqlite3.Connection, issue_number: int) -> None:
    if _fts_enabled(connection):
        connection.execute(
            "DELETE FROM archive_documents_fts WHERE rowid IN (SELECT doc_id FROM archive_documents WHERE issue_number = ?)",
            (issue_number,),
        )
    connection.execute("DELETE FROM archive_documents WHERE issue_number = ?", (issue_number,))


def _count_documents(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) FROM archive_documents").fetchone()
    return 0 if row is None else int(row[0])


def _write_semantic_index_state(
    edition_name: str,
    *,
    archive_root: Path,
    last_built_at: datetime | None,
    latest_confirmed_issue: int | None,
    latest_confirmed_at: datetime | None,
    indexed_document_count: int,
    semantic_index_dirty: bool,
    dirty_reason: str | None,
    last_optimized_at: datetime | None,
    last_optimized_document_count: int,
) -> None:
    state_path = get_semantic_index_state_path(edition_name, archive_root=archive_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": "1.0", "editions": {}}
    if state_path.exists():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and isinstance(existing.get("editions"), dict):
            payload = existing
    payload.setdefault("schema_version", "1.0")
    editions = payload.get("editions")
    if not isinstance(editions, dict):
        editions = {}
        payload["editions"] = editions
    editions = cast(dict[str, object], editions)
    editions[edition_name] = {
        "last_built_at": None if last_built_at is None else last_built_at.isoformat(),
        "latest_confirmed_issue": latest_confirmed_issue,
        "latest_confirmed_at": None if latest_confirmed_at is None else latest_confirmed_at.isoformat(),
        "indexed_document_count": indexed_document_count,
        "semantic_index_dirty": semantic_index_dirty,
        "dirty_reason": dirty_reason,
        "last_optimized_at": None if last_optimized_at is None else last_optimized_at.isoformat(),
        "last_optimized_document_count": last_optimized_document_count,
    }
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _latest_confirmed_issue_number(edition_name: str, *, archive_root: Path) -> int | None:
    latest_confirmed = find_latest_confirmed_entry(read_archive_index(edition_name, archive_root=archive_root))
    return None if latest_confirmed is None else latest_confirmed.issue_number


def _latest_confirmed_generated_at(edition_name: str, *, archive_root: Path) -> datetime | None:
    latest_confirmed = find_latest_confirmed_entry(read_archive_index(edition_name, archive_root=archive_root))
    return None if latest_confirmed is None else latest_confirmed.generated_at


def _parse_optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None

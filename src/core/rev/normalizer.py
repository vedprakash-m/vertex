"""REV normalizer + semantic chunker (Zone A).

specs/program-context-intelligence.md §5.6. The canonical-text pipeline is a
**fixed-order** sequence so that offsets and hashes are reproducible across
runs (QG-DM-2 replay determinism):

    html/mime → text → strip quoted replies → normalize whitespace
              → PII/credential scrub (privacy.py)
              → semantic chunk with 500-char overlap
              → stable chunk_id + codepoint offsets into the canonical text

Chunk merge/dedupe (§5.6) is keyed by ``(event_type, dedupe_core_hash)``;
admitted excerpts' evidence refs are unioned; **contradictory** merges (same
core hash, conflicting event payloads) are routed to human triage rather than
silently collapsed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Sequence

from src.core.rev.entity_types import EntityType
from src.core.rev.ports import Chunk
from src.core.rev.privacy import (
    PseudonymTable,
    build_pseudonym_table_from_display_names,
    normalized_source_hash,
    pseudonymize_text,
    scrub_pii,
    scrub_pii_and_credentials,
)

NORMALIZATION_VERSION = "norm.v1"
CHUNKING_VERSION = "chunk.v1"
DEFAULT_CHUNK_SIZE = 2000
DEFAULT_CHUNK_OVERLAP = 500

# Semantic boundary preference: split on paragraph/line breaks first, then
# sentences, then hard size limit. Ordered strongest→weakest.
_BOUNDARY_RES = (
    re.compile(r"\n\s*\n"),       # paragraph break
    re.compile(r"\n"),            # line break
    re.compile(r"(?<=[.!?])\s+"),  # sentence end
)

# Quoted-reply markers (Outlook/Teams style). Stripped so canonical text holds
# only the *new* portion — the contributor signal — not accumulated history.
# A line that starts with one of these markers (allowing leading whitespace)
# signals the start of the quoted block; everything from that line onward is
# dropped.
_QUOTED_HEADER_RE = re.compile(r"^\s*(?:From|To|Cc|Bcc|Subject|Date|Sent)\s*:", re.IGNORECASE)
_QUOTED_ON_WROTE_RE = re.compile(r"^\s*On\s.+wrote:\s*$", re.IGNORECASE | re.DOTALL)
_QUOTED_ORIGINAL_RE = re.compile(r"^\s*-+\s*Original Message\s*-+", re.IGNORECASE)
_QUOTED_PREFIX_RE = re.compile(r"^\s*(?:&gt;|>)")


def _is_quoted_line(line: str) -> bool:
    return bool(
        _QUOTED_HEADER_RE.match(line)
        or _QUOTED_ON_WROTE_RE.match(line)
        or _QUOTED_ORIGINAL_RE.match(line)
        or _QUOTED_PREFIX_RE.match(line)
    )


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    canonical_text: str
    normalized_source_hash: str
    chunks: tuple[Chunk, ...]
    normalization_version: str = NORMALIZATION_VERSION
    chunking_version: str = CHUNKING_VERSION
    # W5-3: token→original mapping; None when no pseudonymization was applied.
    pseudonym_table: dict[str, str] | None = None


def normalize_html_to_text(html: str) -> str:
    """Convert HTML/MIME body to plain text (Zone A, dependency-free).

    Not a full HTML parser — REV mail bodies are simple. Strips tags, decodes
    the common entities, and collapses structural blocks to line breaks so the
    quoted-reply stripper can find Outlook/Teams markers.
    """
    if not html:
        return ""
    text = html
    # Block-level elements → newlines (preserve structure for quote detection).
    text = re.sub(r"(?i)<\s*(?:p|div|br|tr|li|h[1-6])[^>]*>", "\n", text)
    # Drop everything inside style/script/head.
    text = re.sub(r"(?is)<\s*(?:style|script|head)\b[^>]*>.*?<\s*/\s*(?:style|script|head)\s*>", " ", text)
    # Remove all remaining tags.
    text = re.sub(r"(?s)<[^>]+>", "", text)
    # Decode common entities.
    text = (text.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&#39;", "'")
                .replace("&apos;", "'"))
    return text


def strip_quoted_replies(text: str) -> str:
    """Remove trailing quoted-reply blocks (keep the new contribution)."""
    if not text:
        return ""
    lines = text.split("\n")
    kept: list[str] = []
    for line in lines:
        if _is_quoted_line(line):
            break  # once a quoted block starts, stop keeping lines
        kept.append(line)
    # If we stripped everything (whole body looked like one quote), keep original.
    if not any(line.strip() for line in kept):
        return text
    return "\n".join(kept)


def normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace, preserving single newlines as separators."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    # Protect newlines: collapse spaces only.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_canonical(
    canonical_text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> tuple[Chunk, ...]:
    """Semantic chunk with ``overlap``-char sliding overlap + stable chunk_id.

    Chunks carry codepoint offsets into ``canonical_text`` so spans can be
    mapped back to the canonical normalized source (and thence to vaulted
    excerpts). ``chunk_id`` is a stable hash of ``(source_hash, start, end)``.
    """
    if not canonical_text:
        return ()
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be in [0, chunk_size)")

    source_hash = normalized_source_hash(canonical_text)
    # Greedy boundary-respecting split with overlap by stepping a window.
    chunks: list[Chunk] = []
    n = len(canonical_text)
    cursor = 0
    idx = 0
    while cursor < n:
        end = min(cursor + chunk_size, n)
        # Only retreat to a boundary when the window was *capped* by chunk_size
        # (end < n). If end == n the remainder fits in one chunk — take it whole
        # rather than splitting a short tail into tiny pieces.
        best_end = end
        if end < n:
            # Only retreat to a semantic boundary when it is far enough from
            # the current cursor that the chunk carries at least
            # (chunk_size - overlap) new characters.  A boundary that lands
            # earlier would have forced the old min_stride guard to jump
            # cursor *past* the boundary, silently dropping all text between
            # the boundary and the jump point (PS-9 data-loss root cause).
            # Hard-splitting at `end` instead keeps every codepoint covered.
            min_chunk_len = max(1, chunk_size - overlap)
            for pattern in _BOUNDARY_RES:
                last_match = None
                for last_match in pattern.finditer(canonical_text, cursor, end):
                    pass
                if last_match is not None and last_match.start() >= cursor + min_chunk_len:
                    best_end = last_match.start()
                    break
        chunk_text = canonical_text[cursor:best_end].strip()
        if chunk_text:
            chunk_id = "chunk:" + hashlib.sha256(
                f"{source_hash}|{cursor}|{best_end}".encode("utf-8")
            ).hexdigest()[:16]
            overlap_with_previous = min(overlap, cursor) if idx > 0 else 0
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    text=chunk_text,
                    start_codepoint=cursor,
                    end_codepoint=best_end,
                    overlap_with_previous=overlap_with_previous,
                )
            )
            idx += 1
        if best_end >= n:
            break
        # Advance with overlap.  When no valid boundary was found in the
        # window (best_end == end == cursor + chunk_size), stride equals
        # chunk_size - overlap (the standard stride).  When a boundary was
        # used (best_end >= cursor + min_chunk_len), stride is at least
        # min_chunk_len - overlap = chunk_size - 2*overlap.  Always guarantee
        # at least one codepoint of forward progress.
        next_cursor = best_end - overlap
        if next_cursor <= cursor:
            next_cursor = cursor + 1
        cursor = next_cursor
    return tuple(chunks)


def normalize(
    raw_body: str,
    *,
    is_html: bool = True,
    source_type: EntityType = EntityType.MESSAGE,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    known_display_names: Sequence[str] | None = None,
) -> NormalizationResult:
    """Full §5.6 fixed-order pipeline → canonical text + chunks + hashes.

    PII is redacted into the canonical text **before** chunking/offsets are
    computed so spans are reproducible. Credential findings are *reported* by
    ``scrub_pii_and_credentials`` but are intentionally not surfaced on the
    result — callers must run the fail-closed local gate via
    ``privacy.run_local_checks`` *before* hydrating (§5.7 Stage 1 step 2), and
    quarantine on any credential hit. The canonical text returned here is the
    post-scrub form used for hashing and chunking.

    ``known_display_names`` (W5-3): list of person display names extracted from
    email headers (From/To/Cc). When provided, names are replaced with stable
    PERSON_N tokens in the canonical text *after* PII scrubbing. The
    token→original mapping is returned in ``NormalizationResult.pseudonym_table``
    so entity binding can resolve tokens back to canonical identities without
    the originals reaching the external model.
    """
    text = normalize_html_to_text(raw_body) if is_html else raw_body
    text = strip_quoted_replies(text)
    text = normalize_whitespace(text)
    # PII redaction into the canonical text; credentials are *reported*, not
    # silently redacted, so the caller can fail-closed. We still scrub PII even
    # when credentials are present (the caller quarantines regardless).
    canonical, _findings = scrub_pii_and_credentials(text)
    # W5-3: pseudonymize person display names → PERSON_N tokens after PII scrub.
    pseudonym_dict: dict[str, str] | None = None
    if known_display_names:
        table: PseudonymTable = build_pseudonym_table_from_display_names(
            list(known_display_names)
        )
        if not table.is_empty:
            canonical = pseudonymize_text(canonical, table)
            pseudonym_dict = table.to_dict()
    src_hash = normalized_source_hash(canonical)
    chunks = chunk_canonical(canonical, chunk_size=chunk_size, overlap=overlap)
    return NormalizationResult(
        canonical_text=canonical,
        normalized_source_hash=src_hash,
        chunks=chunks,
        normalization_version=NORMALIZATION_VERSION,
        chunking_version=CHUNKING_VERSION,
        pseudonym_table=pseudonym_dict,
    )


# --- Chunk merge / dedupe (§5.6) ---

@dataclass(frozen=True, slots=True)
class ChunkMergeKey:
    """``(event_type, dedupe_core_hash)`` — the §5.6 merge key."""

    event_type: str
    dedupe_core_hash: str


@dataclass(frozen=True, slots=True)
class ChunkMergeOutcome:
    merged: bool
    contradiction: bool              # same core hash, conflicting payload → human triage
    unioned_evidence_refs: tuple[str, ...]


def dedupe_core_hash_for(canonical_text: str, event_type: str) -> str:
    """Stable core hash for merge/dedupe (canonical text + event type)."""
    return "sha256:" + hashlib.sha256(
        f"{event_type}|{normalized_source_hash(canonical_text)}".encode("utf-8")
    ).hexdigest()


def merge_chunk_evidence(
    existing_refs: tuple[str, ...],
    incoming_refs: tuple[str, ...],
    *,
    existing_payload: dict | None,
    incoming_payload: dict | None,
) -> ChunkMergeOutcome:
    """Merge two same-key candidates' evidence refs (§5.6).

    Union the evidence refs. If both carry payloads that disagree on the same
    field, flag ``contradiction`` so the orchestrator routes the candidate to
    human triage instead of silently collapsing.
    """
    unioned = tuple(dict.fromkeys((*existing_refs, *incoming_refs)))
    contradiction = False
    if existing_payload is not None and incoming_payload is not None:
        for key, value in incoming_payload.items():
            if key in existing_payload and existing_payload[key] != value:
                contradiction = True
                break
    return ChunkMergeOutcome(
        merged=True,
        contradiction=contradiction,
        unioned_evidence_refs=unioned,
    )


__all__ = [
    "NormalizationResult",
    "normalize",
    "normalize_html_to_text",
    "strip_quoted_replies",
    "normalize_whitespace",
    "chunk_canonical",
    "dedupe_core_hash_for",
    "ChunkMergeKey",
    "ChunkMergeOutcome",
    "merge_chunk_evidence",
    "NORMALIZATION_VERSION",
    "CHUNKING_VERSION",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_CHUNK_OVERLAP",
]
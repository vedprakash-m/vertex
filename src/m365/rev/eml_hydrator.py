"""REV EmlHydrator (Zone C) — FR-PCI-5, Phase 1.

specs/gaps.md P1-2. Hydrates a locally-claimed ``.eml`` file into a
``HydratedContent`` object for the REV extraction pipeline.

**MIME walk order (priority):**
1. ``text/plain`` — preferred; lightweight; no HTML parser needed.
2. ``text/html`` — fallback; parsed with BeautifulSoup (lxml-html5lib-safe-html
   parser chain). HTML must be cleaned before extraction (scripts/styles removed).
3. Metadata-only — if no text body found (attachment-only or empty).

**Charset normalization:** Python's ``email`` library decodes each MIME part
using the declared charset with ``errors="replace"``; falls back to UTF-8 then
``latin-1``.

**Winmail.dat / application/* skip:** ``application/ms-tnef`` (Winmail.dat) and
all other ``application/*`` parts are skipped; a record is written to
``attachment_denied.jsonl`` so operators know why content was omitted. The
count of Winmail.dat parts skipped per cycle is exposed via
``winmail_skipped_count`` (RK-12 / OA-4 attachment denial).

**Quoted-reply stripping:** delegates to ``src.core.rev.normalizer.strip_quoted_replies``
to keep only the new contribution in the email thread.

**unique_body_ratio (RK-3):** ``ratio = len(unique_body) / len(full_body)``. If
the ratio is below ``_LOW_UNIQUE_BODY_RATIO`` (default 0.2, provisional —
calibrate after 10 cycles) the cycle is flagged via
``low_unique_body_count`` + ``route_metadata["low_unique_body"]`` so an operator
can review whether stripping was too aggressive. The unique body is **kept** as
canonical while it is non-trivial (preserving the quoted-reply strip
guarantee); the hydrator only falls back to the full body when stripping
yielded nothing recoverable. This avoids re-introducing quoted noise as new
context (a recall/precision hazard) while still surfacing the degrade.

**Empty body quarantine:** if after normalization the unique-body text is empty
(< 10 non-whitespace characters) and the full body is also empty, the content is
marked ``metadata_only``. The file's final disposition (processed vs
quarantined) is owned by ``EmlEnumerator`` (3-dir atomicity model), driven by
``run_rev_cycle``.

**10 MB size guard:** files larger than ``_MAX_EML_BYTES`` return
``Unsupported(size_exceeded)``. ``EmlEnumerator`` quarantines oversized files at
claim time; this hydrator check is a defensive backstop for recovery files.

**30s per-file timeout:** enforced via ``signal.alarm`` on POSIX; on Windows the
hydration is attempted without a hard timeout (safe because files are local).

**Depth limit:** ``message/rfc822`` nesting is recursed up to depth 3.

**Per-cycle counters** (read by ``run_rev_cycle`` via ``getattr`` to populate
``RevCycleReport``): ``winmail_skipped_count``, ``low_unique_body_count``.

Zone C: imports only ``src.core.*`` + stdlib + bs4.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.core.jsonl_utils import append_jsonl_line
from src.core.rev.entity_types import EntityType
from src.core.rev.identity import CanonicalItemIdentity
from src.core.rev.normalizer import (
    normalize,
    normalize_whitespace,
    strip_quoted_replies,
)
from src.core.rev.privacy import scrub_pii as _scrub_pii
from src.core.rev.ports import EnumeratedCandidate, HydratedContent
from src.core.rev.result import PortResult, Success, Unsupported

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30
_MIN_BODY_CHARS = 10          # below this → metadata_only / quarantine as body_empty
_MAX_MIME_DEPTH = 3
_MAX_EML_BYTES = 10 * 1024 * 1024   # 10 MB per .eml (size guard + quarantine trigger)
_LOW_UNIQUE_BODY_RATIO = 0.2        # provisional; calibrate after 10 cycles (RK-3)
_DENY_CONTENT_TYPES = {"application/ms-tnef", "application/octet-stream"}
_DENY_CONTENT_TYPE_PREFIX = "application/"
_WINMAIL_CONTENT_TYPE = "application/ms-tnef"
# Headers used to extract display names for W5-3 pseudonymization.
_PERSON_HEADERS = ("From", "To", "Cc", "Bcc")
_MIN_NAME_WORD_COUNT = 2  # ignore single-word names (aliases, role names, etc.)


def _extract_display_names(msg: email.message.Message) -> list[str]:
    """Extract person display names from From/To/Cc/Bcc headers (W5-3).

    Uses the stdlib ``email.utils.getaddresses`` to parse RFC 2822-format
    header values.  Returns only multi-word names (≥2 space-separated parts)
    to avoid false-positive substitutions on short aliases.
    """
    raw_values: list[str] = []
    for header in _PERSON_HEADERS:
        values = msg.get_all(header) or []
        raw_values.extend(values)
    display_names: list[str] = []
    seen: set[str] = set()
    for name, _addr in email.utils.getaddresses(raw_values):
        name = name.strip().strip('"').strip("'")
        if not name:
            continue
        if len(name.split()) < _MIN_NAME_WORD_COUNT:
            continue
        key = name.lower()
        if key not in seen:
            display_names.append(name)
            seen.add(key)
    return display_names


def _decode_part(part: email.message.Message) -> str:
    """Decode a MIME part payload to a string."""
    charset_raw = part.get_param("charset")
    charset: str = charset_raw if isinstance(charset_raw, str) else "utf-8"
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    for enc in (charset, "utf-8", "latin-1"):
        try:
            return payload.decode(enc, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("latin-1", errors="replace")


def _strip_html(html: str) -> str:
    """Strip HTML tags using BeautifulSoup, returning plain text."""
    import re
    # Pre-remove script/style tag content before BS4 parsing.
    # Python 3.11 html.parser treats <script> content as CDATA (raw text),
    # which can leak through BS4's decompose() on some platforms.
    cleaned = re.sub(r"<(script|style)[^>]*?>.*?</(script|style)>", "", html, flags=re.DOTALL | re.IGNORECASE)
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(cleaned, "html.parser")
        # Second-pass defensive decompose for any residual script/style nodes.
        for el in soup.find_all(["script", "style"]):
            el.decompose()
        return soup.get_text(separator=" ")
    except Exception as exc:
        log.debug("EmlHydrator: BeautifulSoup failed (%s) — stripping tags via fallback", exc)
        return re.sub(r"<[^>]+>", " ", cleaned)


def _walk_parts(
    msg: email.message.Message,
    *,
    depth: int = 0,
    text_parts: list[str],
    denied_types: list[str],
) -> None:
    """Recursively walk MIME parts, collecting text and denied-content logs."""
    if depth > _MAX_MIME_DEPTH:
        return
    content_type = msg.get_content_type()
    if msg.is_multipart():
        for part in msg.get_payload():  # type: ignore[union-attr]
            if isinstance(part, email.message.Message):
                _walk_parts(part, depth=depth + 1, text_parts=text_parts, denied_types=denied_types)
        return
    if content_type == "text/plain":
        text_parts.append(_decode_part(msg))
    elif content_type == "text/html":
        raw_html = _decode_part(msg)
        text_parts.append(_strip_html(raw_html))
    elif content_type == "message/rfc822":
        # Forwarded message — recurse into it.
        inner = msg.get_payload()
        if isinstance(inner, list) and inner and isinstance(inner[0], email.message.Message):
            _walk_parts(inner[0], depth=depth + 1, text_parts=text_parts, denied_types=denied_types)
    elif (
        content_type.startswith(_DENY_CONTENT_TYPE_PREFIX)
        or content_type in _DENY_CONTENT_TYPES
    ):
        denied_types.append(content_type)
    # Other types (image/*, audio/*, video/*) silently skipped.


class _TimeoutError(Exception):
    pass


def _timeout_handler(signum: int, frame: object) -> None:
    raise _TimeoutError("EmlHydrator: per-file timeout exceeded")


class EmlHydrator:
    """Hydrate a locally-claimed ``.eml`` file → ``HydratedContent`` (Phase 1).

    Satisfies the ``ContentHydrator`` Protocol (Zone A ``src/core/rev/ports.py``).
    The ``EnumeratedCandidate.partial_metadata["eml_path"]`` carries the path to
    the claimed file; if missing, hydration returns ``Unsupported``.

    Per-cycle counters (incremented during ``hydrate`` calls, read by
    ``run_rev_cycle`` after the cycle to populate ``RevCycleReport``):

    * ``winmail_skipped_count`` — Winmail.dat (``application/ms-tnef``) parts skipped
    * ``low_unique_body_count`` — items where quoted-reply stripping left a
      suspiciously small unique body (``unique_body_ratio < 0.2``)

    Usage::

        hydrator = EmlHydrator(
            mailbox_tenant_id="tenant-abc",
            principal_mailbox="tpm@example.com",
            attachment_denied_path=Path("programs/{program_id}/rev_inbox/attachment_denied.jsonl"),
        )
        result = hydrator.hydrate(candidate, correlation_id="rev-cycle-001")
    """

    def __init__(
        self,
        *,
        mailbox_tenant_id: str,
        principal_mailbox: str,
        container: str = "inbox",
        attachment_denied_path: Path | None = None,
        timeout_seconds: int = _TIMEOUT_SECONDS,
        low_unique_body_ratio: float = _LOW_UNIQUE_BODY_RATIO,
    ) -> None:
        self._tenant_id = mailbox_tenant_id
        self._principal_mailbox = principal_mailbox
        self._container = container
        self._attachment_denied_path = attachment_denied_path
        self._timeout_seconds = timeout_seconds
        self._low_unique_body_ratio = low_unique_body_ratio
        # Per-cycle counters (reset implicitly per CLI invocation / hydrator instance).
        self.winmail_skipped_count: int = 0
        self.low_unique_body_count: int = 0

    def hydrate(
        self,
        candidate: EnumeratedCandidate,
        *,
        correlation_id: str,
    ) -> PortResult[HydratedContent]:
        """Hydrate one .eml candidate. Returns Unsupported if eml_path is absent."""
        eml_path_str = candidate.partial_metadata.get("eml_path")
        if not eml_path_str:
            return Unsupported(
                entity_type=EntityType.MESSAGE.value,
                reason="eml_path_missing: candidate has no eml_path in partial_metadata",
            )
        eml_path = Path(str(eml_path_str))
        if not eml_path.exists():
            return Unsupported(
                entity_type=EntityType.MESSAGE.value,
                reason=f"eml_not_found: {eml_path}",
            )

        # Size guard (defensive backstop; EmlEnumerator quarantines at claim time).
        try:
            size = eml_path.stat().st_size
        except OSError:
            size = 0
        if size > _MAX_EML_BYTES:
            return Unsupported(
                entity_type=EntityType.MESSAGE.value,
                reason=f"size_exceeded: {size} bytes > {_MAX_EML_BYTES}",
            )

        message_id = str(candidate.partial_metadata.get("message_id", candidate.locator.resource_id))

        # Apply per-file timeout (POSIX only; SIGALRM not available on Windows).
        use_timeout = sys.platform != "win32" and hasattr(signal, "SIGALRM")
        if use_timeout:
            signal.signal(  # type: ignore[attr-defined]
                signal.SIGALRM, _timeout_handler  # type: ignore[attr-defined]
            )
            signal.alarm(self._timeout_seconds)  # type: ignore[attr-defined]

        try:
            hydrated = self._hydrate_path(
                eml_path,
                message_id=message_id,
                correlation_id=correlation_id,
            )
        except _TimeoutError:
            log.warning("EmlHydrator: timeout after %ds on %s", self._timeout_seconds, eml_path)
            return Unsupported(
                entity_type=EntityType.MESSAGE.value,
                reason=f"hydration_timeout: exceeded {self._timeout_seconds}s",
            )
        except Exception as exc:
            log.warning("EmlHydrator: failed to hydrate %s: %s", eml_path, exc)
            return Unsupported(
                entity_type=EntityType.MESSAGE.value,
                reason=f"hydration_error: {exc}",
            )
        finally:
            if use_timeout:
                signal.alarm(0)  # type: ignore[attr-defined]
                signal.signal(  # type: ignore[attr-defined]
                    signal.SIGALRM, signal.SIG_DFL  # type: ignore[attr-defined]
                )

        return Success(hydrated)

    def _hydrate_path(
        self,
        eml_path: Path,
        *,
        message_id: str,
        correlation_id: str,
    ) -> HydratedContent:
        """Parse + normalize one .eml file → HydratedContent."""
        with eml_path.open("rb") as fh:
            msg = email.message_from_binary_file(fh, policy=email.policy.compat32)

        text_parts: list[str] = []
        denied_types: list[str] = []
        _walk_parts(msg, text_parts=text_parts, denied_types=denied_types)

        # Winmail.dat (ms-tnef) parts skipped this message.
        winmail_here = denied_types.count(_WINMAIL_CONTENT_TYPE)
        if winmail_here:
            self.winmail_skipped_count += winmail_here

        # Log denied application/* parts (RK-12 / OA-4 attachment denial).
        if denied_types and self._attachment_denied_path is not None:
            record = json.dumps({
                "message_id": message_id,
                "eml_path": str(eml_path),
                "denied_content_types": denied_types,
                "logged_at": datetime.now(timezone.utc).isoformat(),
            }) + "\n"
            try:
                self._attachment_denied_path.parent.mkdir(parents=True, exist_ok=True)
                append_jsonl_line(self._attachment_denied_path, record, max_bytes=10 * 1024 * 1024)
            except OSError as exc:
                log.warning("EmlHydrator: could not write attachment_denied.jsonl: %s", exc)

        raw_body = " ".join(text_parts)
        full_canonical = normalize_whitespace(raw_body)
        stripped = strip_quoted_replies(raw_body)
        unique_canonical = normalize_whitespace(stripped)

        full_len = len(full_canonical.replace(" ", ""))
        unique_len = len(unique_canonical.replace(" ", ""))
        ratio = (unique_len / full_len) if full_len > 0 else 1.0
        low_unique = ratio < self._low_unique_body_ratio

        # RK-3: keep the unique (quote-stripped) body as canonical while it is
        # non-trivial — preserves the quoted-reply strip guarantee (no quoted
        # noise re-introduced as new context). Only fall back to the full body
        # when stripping yielded nothing recoverable.
        if unique_len < _MIN_BODY_CHARS and full_len >= _MIN_BODY_CHARS:
            chosen_text = full_canonical
            hydration_rung = "full_body"
            low_unique = True
        else:
            chosen_text = unique_canonical
            hydration_rung = "unique_body"
        if low_unique:
            self.low_unique_body_count += 1

        # W5-3: extract display names from From/To/Cc/Bcc before normalization.
        # Names are replaced with PERSON_N tokens in the canonical text so the
        # external model never sees raw display names.  The mapping is stored
        # in route_metadata["pseudonym_table"] for entity-binding resolution.
        display_names = _extract_display_names(msg)

        # Route through normalizer.normalize() for PII/credential scrubbing
        # (W1-4 / PS-24) + pseudonymization (W5-3).
        norm_result = normalize(chosen_text, is_html=False, known_display_names=display_names)
        canonical = norm_result.canonical_text
        source_hash = norm_result.normalized_source_hash
        chunks = norm_result.chunks

        # Scrub PII from the subject before storing it in route_metadata.
        # The subject is forwarded to the LLM extractor prompt and must not
        # carry direct identifiers (W1-4 / PS-24).
        raw_subject = str(msg.get("Subject", "")).strip()
        scrubbed_subject = _scrub_pii(raw_subject) if raw_subject else raw_subject

        # Extract route metadata from headers.
        route_metadata: dict[str, object] = {
            "subject": scrubbed_subject,
            "sender": str(msg.get("From", "")).strip(),
            "received_at": str(msg.get("Date", "")).strip(),
            "message_id": message_id,
            "conversation_id": str(msg.get("Thread-Index", message_id)).strip(),
            "low_unique_body": low_unique,
            "unique_body_ratio": round(ratio, 3),
        }
        # W5-3: include token→original mapping so entity binding can resolve
        # PERSON_N tokens back to the original display names.
        if norm_result.pseudonym_table:
            route_metadata["pseudonym_table"] = norm_result.pseudonym_table

        identity = CanonicalItemIdentity(
            source_type=EntityType.MESSAGE,
            tenant_id=self._tenant_id,
            principal_mailbox=self._principal_mailbox,
            container=self._container,
            resource_id=message_id,
        )

        is_metadata_only = len(canonical.replace(" ", "")) < _MIN_BODY_CHARS

        return HydratedContent(
            identity=identity,
            canonical_text=canonical,
            normalized_source_hash=source_hash,
            chunks=chunks,
            route_metadata=route_metadata,
            hydration_rung=hydration_rung,
            metadata_only=is_metadata_only,
            retrieved_at=datetime.now(timezone.utc),
            correlation_id=correlation_id,
        )


__all__ = ["EmlHydrator"]
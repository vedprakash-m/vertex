"""ADF-W3.7 remainder: local reply import.

Same credential-free, no-API pattern ADR-008/REV already established for
local `.eml` import (Section 8.10.8's reply-ingestion step): the
stakeholder replies to a solicitation draft (in their own mail client),
then the operator saves that reply as a local `.eml` file and drops it
into `programs/<id>/nudge/replies/`. This module enumerates and parses
those files -- it never touches a live mailbox.

Deliberately simpler than `src/m365/rev/eml_hydrator.py`: REV's hydrator is
built for high-volume mailbox import with its own candidate/correlation
pipeline; a handful of solicitation replies per week does not need that
machinery. This module is a small, standalone stdlib `email` parser.
"""

from __future__ import annotations

from email import message_from_bytes
from email.message import Message
from email.utils import parseaddr
from pathlib import Path

from src.core.context_gap_reply import ParsedReply
from src.core.edition_resolver import PROGRAMS_ROOT

_REFERENCE_MARKER_PREFIX = "Reference: "


def replies_dir(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "nudge" / "replies"


def list_pending_replies(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[Path, ...]:
    directory = replies_dir(program_id, programs_root=programs_root)
    if not directory.exists():
        return ()
    return tuple(sorted(directory.glob("*.eml")))


def parse_reply_eml(path: Path) -> ParsedReply:
    raw = path.read_bytes()
    message = message_from_bytes(raw)
    sender_email = _extract_sender_email(message)
    subject = str(message.get("Subject", "")).strip()
    body_text = _extract_body_text(message)
    reference_marker = _extract_reference_marker(body_text)
    return ParsedReply(
        sender_email=sender_email,
        subject=subject,
        body_text=_isolate_new_content(body_text),
        reference_marker=reference_marker,
    )


def _extract_sender_email(message: Message) -> str | None:
    raw_from = message.get("From")
    if not raw_from:
        return None
    _, email_address = parseaddr(str(raw_from))
    return email_address or None


def _extract_body_text(message: Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.is_multipart():
                return _decode(part)
        for part in message.walk():
            if part.get_content_type() == "text/html" and not part.is_multipart():
                return _strip_html_tags(_decode(part))
        return ""
    return _decode(message)


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return str(part.get_payload())
    if not isinstance(payload, bytes):
        return str(payload)
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _strip_html_tags(html: str) -> str:
    import re

    without_tags = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", without_tags).strip()


def _extract_reference_marker(body_text: str) -> str | None:
    for line in body_text.splitlines():
        stripped = line.strip().lstrip(">").strip()
        if stripped.startswith(_REFERENCE_MARKER_PREFIX):
            return stripped[len(_REFERENCE_MARKER_PREFIX) :].strip()
    return None


# Common top-posted-reply separators most mail clients insert before quoted
# original content. Best-effort: if none match, the full body is treated as
# the "new" content -- a human still reviews it before anything is applied,
# so a missed separator degrades to "extra quoted text visible," not a
# silent wrong answer.
_QUOTE_SEPARATORS = (
    "-----Original Message-----",
    "________________________________",
)


def _isolate_new_content(body_text: str) -> str:
    earliest_cut: int | None = None
    for separator in _QUOTE_SEPARATORS:
        index = body_text.find(separator)
        if index != -1 and (earliest_cut is None or index < earliest_cut):
            earliest_cut = index
    for line in body_text.splitlines():
        if line.strip().startswith("On ") and line.strip().endswith("wrote:"):
            index = body_text.find(line)
            if index != -1 and (earliest_cut is None or index < earliest_cut):
                earliest_cut = index
            break
    if earliest_cut is None:
        return body_text.strip()
    return body_text[:earliest_cut].strip()


__all__ = ["list_pending_replies", "parse_reply_eml", "replies_dir"]

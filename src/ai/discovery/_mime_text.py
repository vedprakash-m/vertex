from __future__ import annotations

from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path


class MIMETextError(ValueError):
    pass


def parse_eml_message(source_path: Path) -> ParsedEmailMessage:
    try:
        message = BytesParser(policy=policy.default).parsebytes(source_path.read_bytes())
    except Exception as error:
        raise MIMETextError(f"Unable to parse MIME email {source_path.name}: {error}") from error

    subject = str(message.get("Subject") or "").strip()
    sender = str(message.get("From") or "").strip()
    message_id = str(message.get("Message-ID") or "").strip() or None
    sent_at = _parse_sent_at(str(message.get("Date") or "").strip())
    body = _extract_body_text(message)
    return ParsedEmailMessage(
        subject=subject or source_path.name,
        sender=sender or "unknown@local",
        message_id=message_id,
        sent_at=sent_at,
        body_text=body,
    )


class ParsedEmailMessage:
    __slots__ = ("subject", "sender", "message_id", "sent_at", "body_text")

    def __init__(self, *, subject: str, sender: str, message_id: str | None, sent_at, body_text: str) -> None:
        self.subject = subject
        self.sender = sender
        self.message_id = message_id
        self.sent_at = sent_at
        self.body_text = body_text


def _parse_sent_at(value: str):
    if not value:
        raise MIMETextError("Email Date header is required.")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as error:
        raise MIMETextError("Email Date header is invalid.") from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=policy.default.header_factory('Date', value).datetime.tzinfo) if False else parsed
    return parsed


def _extract_body_text(message) -> str:
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            content = _decode_part(part)
            if content:
                parts.append(content)
    else:
        content = _decode_part(message)
        if content:
            parts.append(content)
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def _decode_part(part) -> str:
    try:
        payload = part.get_content()
    except Exception as error:
        raise MIMETextError(f"Unable to decode MIME body part: {error}") from error
    if not isinstance(payload, str):
        return ""
    return payload
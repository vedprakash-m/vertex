from __future__ import annotations

import os
import uuid
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, formataddr
from pathlib import Path

from src.core.exceptions import StateError


def build_eml_bytes(
    *,
    to: tuple[str, ...],
    cc: tuple[str, ...],
    bcc: tuple[str, ...] = (),
    subject: str,
    html_body: str,
    text_body: str,
    from_display_name: str | None,
    from_email: str | None,
    generated_at: datetime,
    mark_as_draft: bool = True,
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    if from_email:
        message["From"] = formataddr((from_display_name or "", from_email))
    if to:
        message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message["Date"] = format_datetime(generated_at)
    if mark_as_draft:
        message["X-Unsent"] = "1"

    plain_text = text_body.strip() or "See HTML body."
    message.set_content(plain_text)
    message.add_alternative(html_body, subtype="html")
    return message.as_bytes(policy=SMTP)


def write_eml(path: Path, *, eml_bytes: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(eml_bytes)
    return path


def write_eml_atomic(path: Path, *, eml_bytes: bytes) -> Path:
    """Atomic EML write: same-dir temp + fsync + replace. Wraps OSError as StateError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    unique = uuid.uuid4().hex[:8]
    temp_path = path.parent / f".{path.stem}_{pid}_{unique}.tmp.eml"
    try:
        with temp_path.open("wb") as handle:
            handle.write(eml_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        raise StateError(f"Failed to write EML to {path}: {exc}") from exc
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
    return path

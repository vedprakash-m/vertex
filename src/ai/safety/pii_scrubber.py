from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple


_EMAIL_PATTERN = re.compile(r"\b[a-zA-Z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+?\d[\d().\-\s]{8,}\d)(?!\w)")
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


class PIISignal(NamedTuple):
    signal_type: str
    excerpt: str
    position: int


@dataclass(frozen=True, slots=True)
class PIIScanResult:
    pii_detected: bool
    scrubbed_text: str
    signals: tuple[PIISignal, ...] = ()

    @property
    def signal_types(self) -> list[str]:
        return [signal.signal_type for signal in self.signals]


def scan_text(text: str, *, allowed_email_domains: tuple[str, ...] = ()) -> PIIScanResult:
    if not text:
        return PIIScanResult(pii_detected=False, scrubbed_text="")

    normalized_allowed_domains = {domain.lower() for domain in allowed_email_domains}
    signals: list[PIISignal] = []

    def _replace_email(match: re.Match[str]) -> str:
        domain = match.group(1).lower()
        if domain in normalized_allowed_domains:
            return match.group(0)
        signals.append(PIISignal("email", match.group(0), match.start()))
        return "[PII-FILTERED-EMAIL]"

    scrubbed_text = _EMAIL_PATTERN.sub(_replace_email, text)

    def _replace_phone(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) < 10:
            return match.group(0)
        signals.append(PIISignal("phone", match.group(0), match.start()))
        return "[PII-FILTERED-PHONE]"

    scrubbed_text = _PHONE_PATTERN.sub(_replace_phone, scrubbed_text)

    for match in _SSN_PATTERN.finditer(scrubbed_text):
        signals.append(PIISignal("ssn", match.group(0), match.start()))
    scrubbed_text = _SSN_PATTERN.sub("[PII-FILTERED-SSN]", scrubbed_text)

    return PIIScanResult(
        pii_detected=bool(signals),
        scrubbed_text=scrubbed_text,
        signals=tuple(signals),
    )


def filter_text(text: str, *, allowed_email_domains: tuple[str, ...] = ()) -> str:
    return scan_text(text, allowed_email_domains=allowed_email_domains).scrubbed_text
from __future__ import annotations

import base64
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import NamedTuple


_INJECTION_PHRASES = re.compile(
    r"\b("
    r"ignore (?:previous|above|all|prior) instructions?|"
    r"disregard (?:previous|above|all|prior) instructions?|"
    r"forget (?:previous|above|all|prior) instructions?|"
    r"override (?:previous|above)? instructions?|"
    r"you are now|act as (?:a )?(?:different|new|another)|pretend (?:to be|you are)|"
    r"new (?:system |role |persona )?prompt:|"
    r"switch (?:to )?(?:a )?new (?:role|persona|mode)|"
    r"send (?:all|the) (?:data|context|information|output) to|"
    r"exfiltrat|transmit (?:the )?(?:context|data|secrets)|"
    r"you have (?:root|admin|unrestricted) access|"
    r"bypass (?:the )?(?:filter|guard|restriction|safety)|"
    r"reveal (?:the )?(?:system prompt|instructions|context)|"
    r"print (?:the )?(?:system prompt|full context)"
    r")\b",
    re.IGNORECASE,
)

_DELIMITER_PATTERNS = re.compile(
    r"<(?:system|user|assistant|human|ai|instruction)(?:\s|>|/)"
    r"|\[SYSTEM\]|\[USER\]|\[ASSISTANT\]"
    r"|###\s*System(?:\s|:)"
    r"|###\s*(?:New\s+)?Instructions?(?:\s|:)",
    re.IGNORECASE,
)

_BASE64_PATTERN = re.compile(r"[A-Za-z0-9+/]{100,}={0,2}")
_DATA_URI_PATTERN = re.compile(r"data:[a-z]+/[a-z0-9.+-]+;base64,", re.IGNORECASE)
_WEBHOOK_PATTERN = re.compile(
    r"https?://(?:webhook|requestbin|pipedream|ngrok|burpcollab|interact\.sh)",
    re.IGNORECASE,
)


class InjectionSignal(NamedTuple):
    signal_type: str
    excerpt: str
    position: int


@dataclass(frozen=True, slots=True)
class ScanResult:
    injection_detected: bool
    signals: tuple[InjectionSignal, ...] = field(default_factory=tuple)

    @property
    def signal_types(self) -> list[str]:
        return [signal.signal_type for signal in self.signals]


class InjectionDetector:
    def scan(self, text: str) -> ScanResult:
        if not text or not text.strip():
            return ScanResult(injection_detected=False)

        signals: list[InjectionSignal] = []

        for match in _INJECTION_PHRASES.finditer(text):
            signals.append(
                InjectionSignal(
                    signal_type="phrase",
                    excerpt=text[max(0, match.start() - 10): match.end() + 10][:80],
                    position=match.start(),
                )
            )

        for match in _DELIMITER_PATTERNS.finditer(text):
            signals.append(
                InjectionSignal(
                    signal_type="delimiter",
                    excerpt=text[max(0, match.start() - 10): match.end() + 10][:80],
                    position=match.start(),
                )
            )

        for match in _BASE64_PATTERN.finditer(text):
            decoded = _try_decode_base64(match.group())
            if decoded and _INJECTION_PHRASES.search(decoded):
                signals.append(
                    InjectionSignal(
                        signal_type="base64",
                        excerpt=match.group()[:40] + "...",
                        position=match.start(),
                    )
                )

        url_decoded = _try_url_decode(text)
        if url_decoded != text:
            for match in _INJECTION_PHRASES.finditer(url_decoded):
                signals.append(
                    InjectionSignal(
                        signal_type="url_encoded",
                        excerpt=url_decoded[max(0, match.start() - 5): match.end() + 5][:80],
                        position=match.start(),
                    )
                )

        for match in _DATA_URI_PATTERN.finditer(text):
            signals.append(
                InjectionSignal(
                    signal_type="data_uri",
                    excerpt=text[match.start(): match.start() + 60],
                    position=match.start(),
                )
            )

        for match in _WEBHOOK_PATTERN.finditer(text):
            signals.append(
                InjectionSignal(
                    signal_type="webhook",
                    excerpt=match.group()[:80],
                    position=match.start(),
                )
            )

        return ScanResult(injection_detected=bool(signals), signals=tuple(signals))


def _try_decode_base64(value: str) -> str | None:
    try:
        padding = 4 - len(value) % 4
        padded = value + "=" * (padding % 4)
        return base64.b64decode(padded).decode("utf-8", errors="strict")
    except Exception:
        return None


def _try_url_decode(value: str) -> str:
    try:
        return urllib.parse.unquote(value)
    except Exception:
        return value
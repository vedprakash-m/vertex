"""specs/people.md §7.2: the shared `RegistryDiagnostic`/`DiagnosticSeverity`
typed diagnostic contract, used by every registry loader that needs to
surface a structured issue (legacy-field usage, missing `entity_id`,
migration gaps) without raising -- a load can succeed AND carry
diagnostics. First populated by PPL-W2A.2; intentionally its own small
module rather than living inside any one schema module, since every
future PPL-W2A/2B loader reuses this exact same shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DiagnosticSeverity(str, Enum):
    INFO = "info"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class RegistryDiagnostic:
    code: str
    severity: DiagnosticSeverity
    entity_id: str | None
    detail: str
    source_path: str | None = None

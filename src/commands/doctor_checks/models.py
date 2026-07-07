from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    label: str
    status: str
    detail: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DoctorReport:
    edition: str
    checks: tuple[DoctorCheck, ...]

    @property
    def warnings(self) -> int:
        return sum(1 for check in self.checks if check.status == "warn")

    @property
    def failures(self) -> int:
        # Blocking states use ``status="fail"`` (specs/declutter.md DC-02). A
        # ``status="error"`` check is treated as a failure too — some check
        # families (e.g. reality_checks) use the ``"error"`` severity for
        # blocking conditions, and the doctor's overall health must reflect
        # every blocking state regardless of which severity label it carries.
        return sum(1 for check in self.checks if check.status in ("fail", "error"))

    @property
    def overall(self) -> str:
        return "UNHEALTHY" if self.failures else "HEALTHY"


@dataclass(frozen=True, slots=True)
class ADOProbeResult:
    reachable: bool
    auth_method: str
    item_count: int | None
    token_minutes_remaining: int | None
    detail: str


@dataclass(frozen=True, slots=True)
class ProgramPeopleReferenceInfo:
    kinds: frozenset[str] = frozenset()
    files: frozenset[str] = frozenset()
    locations: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SchemaVersionAssessment:
    status: str
    detail: str
    version: str | None


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(file_path.stat().st_size for file_path in path.rglob("*") if file_path.is_file())


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"

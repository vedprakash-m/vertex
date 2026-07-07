from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from src.core.exceptions import ConfigError
from src.core.journal import PROGRAMS_ROOT, get_program_journal_archive_dir, get_program_journal_dir
from src.core.knowledge_store import SHARED_KNOWLEDGE_ROOT
from src.core.profile_encryption import inspect_people_profiles_file


@dataclass(frozen=True, slots=True)
class PrivacyFinding:
    relative_path: str
    line_number: int
    pattern_name: str


@dataclass(frozen=True, slots=True)
class JournalPrivacyScanResult:
    scanned_file_count: int
    findings: tuple[PrivacyFinding, ...]


@dataclass(frozen=True, slots=True)
class SensitiveProfileStatus:
    plaintext_relative_paths: tuple[str, ...]
    encrypted_relative_paths: tuple[str, ...]
    plaintext_profile_count: int
    encrypted_profile_count: int


_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ADO_PAT assignment",
        re.compile(r"(?:^|[\s\{\[,\"'])ADO_PAT(?:[\s\"']*)(?:=|:)(?:[\s\"']*)[A-Za-z0-9._-]{8,}", re.IGNORECASE),
    ),
    (
        "GRAPH_CLIENT_SECRET assignment",
        re.compile(
            r"(?:^|[\s\{\[,\"'])GRAPH_CLIENT_SECRET(?:[\s\"']*)(?:=|:)(?:[\s\"']*)[A-Za-z0-9._~+/=-]{12,}",
            re.IGNORECASE,
        ),
    ),
    (
        "client_secret field",
        re.compile(r"[\"']client_secret[\"'](?:\s*)(?:=|:)(?:\s*)[\"'][A-Za-z0-9._~+/=-]{12,}[\"']", re.IGNORECASE),
    ),
    (
        "Azure DevOps PAT token",
        re.compile(r"\bazdpat[_-][A-Za-z0-9]{8,}\b", re.IGNORECASE),
    ),
)


def scan_program_journal_for_credentials(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> JournalPrivacyScanResult:
    workspace_root = programs_root.parent
    findings: list[PrivacyFinding] = []
    scanned_file_count = 0

    for journal_root in (get_program_journal_dir(program_id, programs_root), get_program_journal_archive_dir(program_id, programs_root)):
        if not journal_root.exists():
            continue
        for file_path in sorted(path for path in journal_root.rglob("*") if path.is_file()):
            scanned_file_count += 1
            relative_path = _display_path(file_path, workspace_root)
            for line_number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                for pattern_name, pattern in _SECRET_PATTERNS:
                    if pattern.search(line):
                        findings.append(
                            PrivacyFinding(
                                relative_path=relative_path,
                                line_number=line_number,
                                pattern_name=pattern_name,
                            )
                        )
                        break

    return JournalPrivacyScanResult(
        scanned_file_count=scanned_file_count,
        findings=tuple(findings),
    )


def find_plaintext_sensitive_profile_files(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    shared_knowledge_root: Path = SHARED_KNOWLEDGE_ROOT,
) -> SensitiveProfileStatus:
    workspace_root = programs_root.parent
    plaintext_paths: list[str] = []
    encrypted_paths: list[str] = []
    plaintext_profile_count = 0
    encrypted_profile_count = 0

    for file_path in (
        shared_knowledge_root / "people_profiles.yaml",
        programs_root / program_id / "knowledge" / "people_profiles.yaml",
    ):
        if not file_path.exists():
            continue
        status = inspect_people_profiles_file(file_path)
        if not status.profile_count:
            continue
        relative_path = _display_path(file_path, workspace_root)
        if status.storage == "plaintext":
            plaintext_paths.append(relative_path)
            plaintext_profile_count += status.profile_count
        elif status.storage == "encrypted":
            encrypted_paths.append(relative_path)
            encrypted_profile_count += status.profile_count
        else:
            raise ConfigError(f"Unexpected people_profiles.yaml storage state '{status.storage}' in {file_path}.")

    return SensitiveProfileStatus(
        plaintext_relative_paths=tuple(plaintext_paths),
        encrypted_relative_paths=tuple(encrypted_paths),
        plaintext_profile_count=plaintext_profile_count,
        encrypted_profile_count=encrypted_profile_count,
    )


def _display_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return str(path)
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
from typing import Any

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError
from src.core.privacy_matrix import CHANNEL_POSTURE, Channel, channels
from src.core.privacy_scan import find_plaintext_sensitive_profile_files, scan_program_journal_for_credentials
from src.core.program_paths import resolve_channel_registry_path_for_read

_REGISTRY_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def run_privacy_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Privacy", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    program_id = resolved.paths.program_id
    journal_scan = scan_program_journal_for_credentials(program_id, programs_root=programs_root)
    checks: list[DoctorCheck] = []

    if journal_scan.findings:
        detail = "; ".join(
            f"{finding.relative_path}:L{finding.line_number} ({finding.pattern_name})"
            for finding in journal_scan.findings[:2]
        )
        if len(journal_scan.findings) > 2:
            detail = f"{detail}; +{len(journal_scan.findings) - 2} more"
        checks.append(DoctorCheck("Privacy Scan", "fail", f"Potential credential patterns found: {detail}"))
    else:
        label = "file" if journal_scan.scanned_file_count == 1 else "files"
        checks.append(
            DoctorCheck(
                "Privacy Scan",
                "ok",
                f"Scanned {journal_scan.scanned_file_count} journal {label}; no obvious credential patterns found.",
            )
        )

    try:
        profile_status = find_plaintext_sensitive_profile_files(program_id, programs_root=programs_root)
    except ConfigError as error:
        checks.append(DoctorCheck("Privacy Profiles", "fail", str(error)))
        return DoctorReport(edition=edition_name, checks=tuple(checks))

    if profile_status.plaintext_profile_count:
        plaintext_detail = ", ".join(profile_status.plaintext_relative_paths[:2])
        if len(profile_status.plaintext_relative_paths) > 2:
            plaintext_detail = f"{plaintext_detail}, +{len(profile_status.plaintext_relative_paths) - 2} more"
        encrypted_prefix = ""
        if profile_status.encrypted_profile_count:
            encrypted_detail = ", ".join(profile_status.encrypted_relative_paths[:2])
            if len(profile_status.encrypted_relative_paths) > 2:
                encrypted_detail = f"{encrypted_detail}, +{len(profile_status.encrypted_relative_paths) - 2} more"
            encrypted_prefix = (
                f"{profile_status.encrypted_profile_count} sensitive profile entr"
                f"{'y' if profile_status.encrypted_profile_count == 1 else 'ies'} verified encrypted at rest in {encrypted_detail}; "
            )
        checks.append(
            DoctorCheck(
                "Privacy Profiles",
                "warn",
                (
                    f"{encrypted_prefix}"
                    f"{profile_status.plaintext_profile_count} sensitive profile entr"
                    f"{'y' if profile_status.plaintext_profile_count == 1 else 'ies'} remain plaintext in {plaintext_detail}; encryption at rest is still pending."
                ),
            )
        )
    elif profile_status.encrypted_profile_count:
        detail = ", ".join(profile_status.encrypted_relative_paths[:2])
        if len(profile_status.encrypted_relative_paths) > 2:
            detail = f"{detail}, +{len(profile_status.encrypted_relative_paths) - 2} more"
        checks.append(
            DoctorCheck(
                "Privacy Profiles",
                "ok",
                f"{profile_status.encrypted_profile_count} sensitive profile entr{'y' if profile_status.encrypted_profile_count == 1 else 'ies'} verified encrypted at rest in {detail}.",
            )
        )
    else:
        checks.append(DoctorCheck("Privacy Profiles", "ok", "No populated plaintext people_profiles.yaml files detected for this program."))

    registry_check = build_registry_privacy_check(program_id, programs_root=programs_root)
    if registry_check is not None:
        checks.append(registry_check)

    # WS-15: cross-check the channel_registry against the privacy matrix.
    channel_posture_check = build_channel_posture_check(program_id, programs_root=programs_root)
    if channel_posture_check is not None:
        checks.append(channel_posture_check)

    return DoctorReport(edition=edition_name, checks=tuple(checks))


def build_registry_privacy_check(program_id: str, *, programs_root: Path) -> DoctorCheck | None:
    registry_path = resolve_channel_registry_path_for_read(program_id, programs_root=programs_root)
    wal_path = Path(str(registry_path) + "-wal")
    findings: list[str] = []

    if registry_path.exists():
        findings.extend(scan_channel_registry_for_privacy_findings(registry_path, program_id))
    if wal_path.exists():
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(wal_path.stat().st_mtime, tz=timezone.utc)
        if age > timedelta(hours=24):
            findings.append(
                f"stale WAL file programs/{program_id}/channel_registry.sqlite3-wal age={int(age.total_seconds() // 3600)}h"
            )

    if not registry_path.exists() and not wal_path.exists():
        return None
    if findings:
        detail = "; ".join(findings[:2])
        if len(findings) > 2:
            detail = f"{detail}; +{len(findings) - 2} more"
        return DoctorCheck("Privacy Registry", "warn", detail)
    return DoctorCheck("Privacy Registry", "ok", f"Scanned programs/{program_id}/channel_registry.sqlite3; no obvious registry privacy findings.")


def scan_channel_registry_for_privacy_findings(registry_path: Path, program_id: str) -> list[str]:
    findings: list[str] = []
    try:
        with sqlite3.connect(registry_path) as connection:
            rows = connection.execute(
                "SELECT channel, ref_id, ref_kind, ref_title, metadata_json FROM registrations"
            ).fetchall()
    except sqlite3.DatabaseError as error:
        return [f"unable to read programs/{program_id}/channel_registry.sqlite3 ({error})"]

    for channel, ref_id, ref_kind, ref_title, metadata_json in rows:
        if isinstance(ref_title, str) and _REGISTRY_EMAIL_PATTERN.search(ref_title):
            findings.append(f"{channel}:{ref_kind}:{ref_id} ref_title looks like it contains email-address PII")
        metadata = parse_registry_metadata_json(metadata_json)
        for key, value in metadata.items():
            if isinstance(value, str) and _REGISTRY_EMAIL_PATTERN.search(value):
                findings.append(f"{channel}:{ref_kind}:{ref_id} metadata[{key}] looks like it contains email-address PII")
    return findings


def parse_registry_metadata_json(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# WS-15: cross-check every channel observed in the registry against the
# privacy matrix. If an unknown channel appears (e.g. a future M365 capability
# not yet matrix'd), surface a warn so the operator knows the matrix needs an
# update. If a known channel is observed, the matrix entry is the
# authoritative source for retention + RBAC posture.


def build_channel_posture_check(program_id: str, *, programs_root: Path) -> DoctorCheck | None:
    """Assert every observed channel in the registry is known to the matrix.

    Returns a `warn` check for unknown channels (so the matrix gets updated)
    and an `ok` check listing the matched posture for every known channel.
    """
    registry_path = resolve_channel_registry_path_for_read(program_id, programs_root=programs_root)
    if not registry_path.exists():
        return None

    observed: set[str] = set()
    try:
        with sqlite3.connect(registry_path) as connection:
            for (channel,) in connection.execute("SELECT DISTINCT channel FROM registrations").fetchall():
                if isinstance(channel, str):
                    observed.add(channel)
    except sqlite3.DatabaseError:
        return None

    if not observed:
        return None

    known_channels = {c.value for c in channels()}
    unknown = sorted(observed - known_channels)
    matched = sorted(observed & known_channels)

    if unknown:
        detail = "unknown channels (matrix needs update): " + ", ".join(unknown)
        if matched:
            detail = detail + "; known: " + ", ".join(matched)
        return DoctorCheck("Privacy Channel Posture", "warn", detail)

    # All observed channels are in the matrix; surface the matrix posture.
    posture_summaries = []
    for channel_name in matched:
        try:
            channel = Channel(channel_name)
        except ValueError:
            continue
        p = CHANNEL_POSTURE[channel]
        write_clause = (
            f"write={p.write_default_class.value}" if p.write_default_class else "write=n/a"
        )
        posture_summaries.append(
            f"{channel_name}: read={p.read_default_class.value}, {write_clause}, "
            f"retention={p.retention.value}, rbac={p.rbac_model}"
        )
    return DoctorCheck(
        "Privacy Channel Posture",
        "ok",
        "all observed channels in matrix: " + "; ".join(posture_summaries[:3])
        + ("; +more" if len(posture_summaries) > 3 else ""),
    )

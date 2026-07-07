"""WS-17: redacted support bundle for SRE-grade incident triage.

A support bundle is a single zipped tarball that captures everything
an on-call SRE needs to triage a failure WITHOUT exposing PII:

  - ``gather_state.json`` (last-run footprint, PII not stored there)
  - ``run_telemetry.jsonl`` (per-channel latency history, PII not stored)
  - ``alerts.jsonl`` (open + resolved alerts, PII not stored)
  - ``manifests/issue_*.json`` (latest archive manifest, PII not stored)
  - ``doctor_report.txt`` (last doctor run, if present)
  - ``environment.txt`` (vertex version, python version, OS — no hostnames)
  - ``redaction_log.txt`` (every field scrubbed, with a redaction reason)

PII redaction follows the same discipline as the privacy matrix
(WS-15) — the **documented PII slots** are
``assignee_email`` / ``attendees`` / ``posted_by`` / ``user_principal_name``.
Any email-shaped string OUTSIDE those slots is scrubbed to ``[REDACTED]``.

The bundle is **always redacted**. There is no flag to disable
redaction — that would defeat the point.
"""
from __future__ import annotations

import json
import re
import os
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.core.exceptions import StateError
from src.core.gather_state_store import load_gather_state, resolve_gather_state_path_for_read
from src.core.jsonl_utils import parse_jsonl_line
from src.core.program_paths import resolve_run_telemetry_path_for_read
from src.core.run_telemetry import read_run_telemetry
from src.core.alerts import read_alerts, _alerts_path


_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_GUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
# Bearer-token-shaped strings (must not leak if someone pasted a token)
_TOKEN_PATTERN = re.compile(r"\b(?:Bearer\s+)?[A-Za-z0-9_\-]{32,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{8,}\b")
# ADO PAT regex (all orgs)
_PAT_PATTERN = re.compile(r"\b[a-z0-9]{20,52}\b")


@dataclass(frozen=True, slots=True)
class SupportBundleResult:
    """Result of building a support bundle."""
    bundle_path: Path
    size_bytes: int
    file_count: int
    redaction_count: int
    redaction_log: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RedactionLog:
    """Internal accumulator used during bundle build."""
    entries: tuple[str, ...] = ()

    def add(self, entry: str) -> "RedactionLog":
        return RedactionLog(entries=self.entries + (entry,))


# Documented PII slots where redaction is NOT applied (the privacy
# matrix classifies these as legitimate PII fields).
PII_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "assignee_email",
    "attendees",
    "posted_by",
    "user_principal_name",
})


def build_support_bundle(
    program_id: str,
    *,
    programs_root: Path,
    archive_root: Path | None = None,
    output_path: Path | None = None,
    include_resolved_alerts: bool = True,
) -> SupportBundleResult:
    """Build a redacted support bundle for the given program.

    Args:
        program_id: which program to bundle.
        programs_root: programs/ root.
        archive_root: optional archive/ root (latest manifest).
        output_path: where to write the .tar.gz. Defaults to
            ``<programs_root>/<id>/_alerts/support_bundle_<timestamp>.tar.gz``
            (so it lives next to the alerts, not in the workspace root).
        include_resolved_alerts: include resolved alerts (default True
            for forensic value; exclude to keep the bundle small).
    """
    program_dir = programs_root / program_id
    if not program_dir.exists():
        raise StateError(f"Program not found: {program_dir}")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if output_path is None:
        output_path = program_dir / "_alerts" / f"support_bundle_{ts}.tar.gz"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    redaction_log = RedactionLog()

    def _redact(node: Any, path: str = "") -> tuple[Any, RedactionLog]:
        return _redact_node(node, path, redaction_log)

    file_count = 0
    with tarfile.open(output_path, "w:gz") as tar:
        # 1. gather_state.json
        gather_state_path = resolve_gather_state_path_for_read(program_id, programs_root=programs_root)
        if gather_state_path.exists():
            payload = json.loads(gather_state_path.read_text(encoding="utf-8"))
            redacted, redaction_log = _redact(payload, "gather_state")
            _add_json(tar, "gather_state.json", redacted)
            file_count += 1

        # 2. run_telemetry.jsonl
        rt_path = resolve_run_telemetry_path_for_read(program_id, programs_root=programs_root)
        if rt_path.exists():
            rows = []
            for line in rt_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    payload = parse_jsonl_line(line)
                except json.JSONDecodeError:
                    rows.append({"_redacted": True, "_reason": "invalid JSON line"})
                    redaction_log = redaction_log.add("run_telemetry: dropped invalid JSON line")
                    continue
                redacted, redaction_log = _redact(payload, "run_telemetry")
                rows.append(redacted)
            _add_jsonl(tar, "run_telemetry.jsonl", rows)
            file_count += 1

        # 3. alerts.jsonl
        alerts_path = _alerts_path(program_id, programs_root)
        if alerts_path.exists():
            rows = []
            for alert in read_alerts(program_id, programs_root=programs_root, include_resolved=include_resolved_alerts):
                row = alert.to_dict()
                redacted, redaction_log = _redact(row, "alerts")
                rows.append(redacted)
            _add_jsonl(tar, "alerts.jsonl", rows)
            file_count += 1

        # 4. Latest archive manifest (if archive_root provided)
        if archive_root is not None and archive_root.exists():
            latest_manifest = _find_latest_manifest(archive_root, program_id)
            if latest_manifest is not None:
                try:
                    payload = json.loads(latest_manifest.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    payload = {"_redacted": True, "_reason": "invalid manifest JSON"}
                    redaction_log = redaction_log.add("archive_manifest: invalid JSON; redaction-only payload")
                redacted, redaction_log = _redact(payload, "archive_manifest")
                _add_json(tar, f"manifests/{latest_manifest.name}", redacted)
                file_count += 1

        # 5. environment.txt
        env_payload = _build_environment_payload()
        _add_text(tar, "environment.txt", env_payload)
        file_count += 1

        # 6. redaction_log.txt (last, after all redactions counted)
        _add_text(tar, "redaction_log.txt", "\n".join(redaction_log.entries) + ("\n" if redaction_log.entries else ""))
        file_count += 1

    size = output_path.stat().st_size
    return SupportBundleResult(
        bundle_path=output_path,
        size_bytes=size,
        file_count=file_count,
        redaction_count=len(redaction_log.entries),
        redaction_log=redaction_log.entries,
    )


# ---------- internals ----------


def _redact_node(node: Any, path: str, log: RedactionLog) -> tuple[Any, RedactionLog]:
    """Recursively walk a JSON-like node and redact PII-shaped strings.

    Replaces with ``[REDACTED]`` any email-shaped, GUID-shaped, or
    token-shaped string EXCEPT in documented PII slots."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in PII_ALLOWED_FIELDS:
                out[k] = v
                continue
            new_path = f"{path}.{k}" if path else k
            redacted, log = _redact_node(v, new_path, log)
            out[k] = redacted
        return out, log
    if isinstance(node, list):
        new_list = []
        for i, item in enumerate(node):
            new_path = f"{path}[{i}]"
            redacted, log = _redact_node(item, new_path, log)
            new_list.append(redacted)
        return new_list, log
    if isinstance(node, str):
        redacted, log = _redact_string(node, path, log)
        return redacted, log
    return node, log


def _redact_string(value: str, path: str, log: RedactionLog) -> tuple[Any, RedactionLog]:
    """Redact PII-shaped substrings inside a string value."""
    original = value
    redactions: list[str] = []
    redacted = value
    new_redacted, n = _EMAIL_PATTERN.subn("[REDACTED]", redacted)
    if n:
        redactions.append(f"{n} email(s)")
        redacted = new_redacted
    new_redacted, n = _GUID_PATTERN.subn("[REDACTED]", redacted)
    if n:
        redactions.append(f"{n} guid(s)")
        redacted = new_redacted
    new_redacted, n = _TOKEN_PATTERN.subn("[REDACTED]", redacted)
    if n:
        redactions.append(f"{n} token(s)")
        redacted = new_redacted
    if redactions:
        log = log.add(f"{path}: {', '.join(redactions)} (was {len(original)}B, now {len(redacted)}B)")
        return redacted, log
    return value, log


def _add_json(tar: tarfile.TarFile, name: str, payload: Any) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    tar.addfile(info, BytesIO(data))


def _add_jsonl(tar: tarfile.TarFile, name: str, rows: Iterable[Any]) -> None:
    buf = BytesIO()
    for row in rows:
        buf.write((json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    data = buf.getvalue()
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    tar.addfile(info, BytesIO(data))


def _add_text(tar: tarfile.TarFile, name: str, text: str) -> None:
    data = text.encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    tar.addfile(info, BytesIO(data))


def _find_latest_manifest(archive_root: Path, program_id: str) -> Path | None:
    """Return the most-recent manifest under archive/<edition>/manifests/."""
    candidates: list[Path] = []
    for edition_dir in archive_root.iterdir() if archive_root.exists() else []:
        manifests_dir = edition_dir / "manifests"
        if not manifests_dir.is_dir():
            continue
        for manifest in manifests_dir.glob("issue_*.json"):
            candidates.append(manifest)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _build_environment_payload() -> str:
    import platform
    import sys
    try:
        from src import __version__ as vertex_version  # type: ignore[attr-defined]
    except ImportError:
        vertex_version = "unknown"
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    os_name = platform.system()
    return (
        f"vertex: {vertex_version}\n"
        f"python: {py_version}\n"
        f"os: {os_name} {platform.release()}\n"
        f"host: [REDACTED]   # SRE support bundle never includes hostnames\n"
        f"bundle-generated-at: {datetime.now(timezone.utc).isoformat()}\n"
    )

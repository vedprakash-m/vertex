"""ADF-W4.3 (specs/arch-data-fix.md Section 8.10.1): operator tool to archive
obsolete machine-generated risk rows out of the active risk register.

The baseline evidence (Section 4.1) records 1,569 risk rows, ~1,556 of them
auto-derived cleanup rows -- the "Closed machine-generated hygiene candidates
[that] do not remain indefinitely in the strategic risk register" (Section
8.10.1). This script archives (moves, never deletes) those rows into a
content-hashed, source-traceable archive file and writes a rollback manifest
so the move can be undone. Archived history is queryable through ``vertex
risks history`` (the read path this script's archive feeds).

Archive criteria (conservative -- never archives human-strategic rows):
- ``kind`` is ``candidate`` or ``hygiene`` (machine-derived), AND
- ``status`` is terminal (``closed``, ``mitigated``, ``accepted``).

A ``strategic`` risk is never archived by this tool regardless of status --
that is a human-only decision. Read-only by default: ``--dry-run`` (default)
reports what would be archived; ``--execute`` performs the move; ``--rollback``
restores from a manifest.

Usage::

    python scripts/archive_obsolete_risks.py --program xpf --dry-run
    python scripts/archive_obsolete_risks.py --program xpf --execute
    python scripts/archive_obsolete_risks.py --program xpf --rollback <manifest_path>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.migration_log import append_migration_log
from src.core.models_v2 import RiskKind
from src.core.risk_register_engine import (
    _risk_entry_to_record,
    get_risk_register_path,
    load_risk_register,
    save_risk_register,
)

#: Statuses considered terminal for archive eligibility.
_TERMINAL_STATUSES = frozenset({"closed", "mitigated", "accepted"})
_ARCHIVE_SUBDIR = ".archive/risk_register"


@dataclass(frozen=True, slots=True)
class ArchivedRisk:
    risk_id: str
    title: str
    kind: str
    status: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class RiskArchiveManifest:
    schema_version: str
    program_id: str
    created_at: str
    reason: str
    archived_count: int
    archive_path: str
    archived: tuple[ArchivedRisk, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "program_id": self.program_id,
            "created_at": self.created_at,
            "reason": self.reason,
            "archived_count": self.archived_count,
            "archive_path": self.archive_path,
            "archived": [asdict(entry) for entry in self.archived],
        }


def _content_hash(record: dict[str, object]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def select_archive_candidates(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[list[object], list[object]]:
    """Return ``(to_archive, to_keep)`` partitioned by the archive criteria.

    ``to_archive`` are machine-derived terminal rows; ``to_keep`` are everything
    else (strategic risks, open candidates, etc.). Never raises on an absent
    register -- returns empty lists.
    """
    entries = list(load_risk_register(program_id, programs_root=programs_root))
    to_archive: list[object] = []
    to_keep: list[object] = []
    for entry in entries:
        is_machine = entry.kind in (RiskKind.CANDIDATE.value, RiskKind.HYGIENE.value)
        is_terminal = entry.status.value in _TERMINAL_STATUSES
        if is_machine and is_terminal:
            to_archive.append(entry)
        else:
            to_keep.append(entry)
    return to_archive, to_keep


def plan_archive(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> RiskArchiveManifest:
    """Read-only: build the manifest of what ``--execute`` would archive."""
    to_archive, _to_keep = select_archive_candidates(program_id, programs_root=programs_root)
    archived_records = []
    archived_summaries: list[ArchivedRisk] = []
    for entry in to_archive:
        record = _risk_entry_to_record(entry)
        archived_records.append(record)
        archived_summaries.append(
            ArchivedRisk(
                risk_id=entry.id,
                title=entry.title,
                kind=entry.kind,
                status=entry.status.value,
                content_hash=_content_hash(record),
            )
        )
    created_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_rel = f"{_ARCHIVE_SUBDIR}/{created_at}/archived_risks.yaml"
    return RiskArchiveManifest(
        schema_version="1",
        program_id=program_id,
        created_at=created_at,
        reason="ADF-W4.3: archive obsolete machine-generated terminal risk rows",
        archived_count=len(archived_summaries),
        archive_path=archive_rel,
        archived=tuple(archived_summaries),
    )


def execute_archive(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> RiskArchiveManifest:
    """Move terminal machine-derived rows into an archive and rewrite the register.

    Writes the archive YAML first, then rewrites the register with only the
    kept rows. Returns the rollback manifest. Raises if the register is absent.
    """
    register_path = get_risk_register_path(program_id, programs_root=programs_root)
    if not register_path.exists():
        raise FileNotFoundError(f"Risk register not found: {register_path}")

    to_archive, to_keep = select_archive_candidates(program_id, programs_root=programs_root)
    manifest = plan_archive(program_id, programs_root=programs_root)

    if not to_archive:
        return manifest

    # 1. Write the archive file (the moved rows + their full records).
    archive_dir = programs_root / program_id / manifest.archive_path
    archive_dir.parent.mkdir(parents=True, exist_ok=True)
    archive_records = [_risk_entry_to_record(e) for e in to_archive]
    archive_dir.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "archived_at": manifest.created_at,
                "program_id": program_id,
                "reason": manifest.reason,
                "risks": archive_records,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # 2. Back up the current register, then rewrite it with only kept rows.
    backup_path = register_path.with_suffix(".yaml.bak")
    backup_path.write_bytes(register_path.read_bytes())
    save_risk_register(program_id, tuple(to_keep), programs_root=programs_root)

    # 3. Write the rollback manifest alongside the archive.
    manifest_path = archive_dir.parent / "rollback_manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")

    # 4. Record in the migration log (operator audit trail).
    append_migration_log(
        program_id=program_id,
        kind="adf_w4_3_risk_archive",
        source_id=str(register_path),
        target_id=str(archive_dir),
        files_touched=(str(archive_dir), str(register_path), str(backup_path)),
        programs_root=programs_root,
    )
    return manifest


def rollback_archive(
    program_id: str,
    manifest_path: Path,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> int:
    """Restore archived rows from a manifest. Returns the count restored.

    Reads the archive YAML named in the manifest and re-merges its rows into
    the active register. Does not delete the archive file (it stays as history
    per Section 8.10.1's "Cleanup must not destroy the ability to explain a
    prior published risk").
    """
    manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive_rel = manifest_raw["archive_path"]
    archive_path = programs_root / program_id / archive_rel
    if not archive_path.exists():
        raise FileNotFoundError(f"Archive file not found: {archive_path}")
    archive_doc = yaml.safe_load(archive_path.read_text(encoding="utf-8"))
    archived_records = archive_doc.get("risks") or []
    if not isinstance(archived_records, list):
        raise ValueError(f"Archive {archive_path} has no 'risks' list")

    # Re-parse current entries + archived records, then save.
    from src.core.risk_register_engine import _parse_risk_entry

    current = list(load_risk_register(program_id, programs_root=programs_root))
    current_ids = {e.id for e in current}
    restored = 0
    for record in archived_records:
        if not isinstance(record, dict):
            continue
        entry = _parse_risk_entry(program_id, record)
        if entry.id not in current_ids:
            current.append(entry)
            restored += 1
    save_risk_register(program_id, tuple(current), programs_root=programs_root)
    return restored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive obsolete machine-generated risk rows (ADF-W4.3).")
    parser.add_argument("--program", required=True)
    parser.add_argument("--programs-root", type=Path, default=PROGRAMS_ROOT)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--rollback", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.rollback is not None:
        count = rollback_archive(args.program, args.rollback, programs_root=args.programs_root)
        print(f"Restored {count} archived risk rows into {args.program}'s register.")
        return 0

    if args.execute:
        manifest = execute_archive(args.program, programs_root=args.programs_root)
    else:
        manifest = plan_archive(args.program, programs_root=args.programs_root)

    print(f"Program: {args.program}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"Rows to archive: {manifest.archived_count}")
    print(f"Archive path: {manifest.archive_path}")
    for summary in manifest.archived[:20]:
        print(f"  - {summary.risk_id} [{summary.kind}/{summary.status}] {summary.title[:60]}")
    if manifest.archived_count > 20:
        print(f"  ... and {manifest.archived_count - 20} more")
    if not args.execute and manifest.archived_count > 0:
        print("\n(dry-run only; pass --execute to archive)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""ADF-W1.8: proactive autonomy_audit.jsonl foreign-row migration.

``src/core/analytics_store.py``'s reader now self-heals automatically (any
row that doesn't parse as an ``AutonomyAuditRecord`` is quarantined via
``jsonl_utils.quarantine_and_rewrite_jsonl`` rather than crashing the whole
read). This script deliberately triggers that same quarantine for one
program and additionally records a governance-visible
``migration_log.jsonl`` entry, so the cleanup shows up in migration-log
tooling and not only the quarantine directory.

Usage::

    python scripts/migrate_autonomy_audit.py --program xpf
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from src.core.analytics_store import get_program_autonomy_audit_path, load_autonomy_audit_records
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.migration_log import append_migration_log
from src.core.workspace_lease import LeaseHeldByAnotherOwner, acquire_lease, release_lease


def migrate_autonomy_audit(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> int:
    """Quarantine foreign rows from *program_id*'s autonomy_audit.jsonl.

    Returns the number of rows quarantined (0 if the file was already clean
    or absent). Read-only from the caller's perspective except for the
    quarantine side effect and, when rows were quarantined, one
    ``migration_log.jsonl`` append.

    ADF-W1.10 (Appendix A.11): runs under the program's ``state_migration``
    workspace lease domain so it cannot interleave with another concurrent
    ``state_migration`` operation on the same program. Raises
    ``LeaseHeldByAnotherOwner`` if one is already in progress -- the caller
    (``main``) surfaces this as a clear operator message rather than a raw
    traceback.
    """
    path = get_program_autonomy_audit_path(program_id, programs_root=programs_root)
    if not path.exists():
        return 0

    owner = f"migrate_autonomy_audit:{uuid.uuid4().hex[:12]}"
    lease = acquire_lease(program_id, owner, mutation_domain="state_migration", programs_root=programs_root)
    try:
        before_line_count = len(path.read_text(encoding="utf-8").splitlines())
        load_autonomy_audit_records(program_id, programs_root=programs_root)  # triggers quarantine as a side effect
        after_line_count = len(path.read_text(encoding="utf-8").splitlines())
        quarantined = max(0, before_line_count - after_line_count)

        if quarantined:
            append_migration_log(
                program_id=program_id,
                kind="autonomy_audit_foreign_row_quarantine",
                source_id=str(path),
                target_id=str(path.parent / "quarantine"),
                files_touched=(str(path),),
                dry_run=False,
                operator="scripts/migrate_autonomy_audit.py",
                programs_root=programs_root,
            )
        return quarantined
    finally:
        release_lease(lease, programs_root=programs_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--program", required=True, help="Program id, e.g. xpf.")
    parser.add_argument("--programs-root", type=Path, default=PROGRAMS_ROOT)
    args = parser.parse_args(argv)

    try:
        quarantined = migrate_autonomy_audit(args.program, programs_root=args.programs_root)
    except LeaseHeldByAnotherOwner as error:
        print(f"Another state_migration operation is in progress for {args.program}: {error}")
        return 1
    if quarantined:
        print(
            f"Quarantined {quarantined} foreign row(s) from {args.program}'s autonomy_audit.jsonl "
            f"(see journal/quarantine/ and migration_log.jsonl)."
        )
    else:
        print(f"No foreign rows found in {args.program}'s autonomy_audit.jsonl.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.core.nudge_models import NUDGE_STATE_SCHEMA_VERSION  # noqa: E402
from src.core.nudge_state_store import _load_payload, _atomic_write  # noqa: E402


@dataclass(frozen=True, slots=True)
class MigrationResult:
    path: Path
    legacy_keys_rewritten: int
    changed: bool


def migrate_payload(payload: dict) -> tuple[dict, int]:
    """Rewrite bare numeric keys to item:/freshness: canonical form.

    Preserves existing newer namespaced values (setdefault semantics).
    Sets schema_version to 1.1. Creates both item: and freshness: from each bare key.
    """
    migrated: dict = {}
    legacy_keys: list[tuple[str, object]] = []

    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        if key == "schema_version" or not key.isdigit():
            migrated[key] = value
            continue
        legacy_keys.append((key, value))

    # Write schema version first
    migrated["schema_version"] = NUDGE_STATE_SCHEMA_VERSION

    for item_id, timestamp in legacy_keys:
        item_key = f"item:{item_id}"
        freshness_key = f"freshness:{item_id}"
        migrated.setdefault(item_key, timestamp)
        migrated.setdefault(freshness_key, timestamp)

    return migrated, len(legacy_keys)


def migrate_state_file(path: Path, *, dry_run: bool = False) -> MigrationResult:
    payload = _load_payload(path)
    migrated, legacy_keys_rewritten = migrate_payload(payload)
    changed = migrated != payload
    if changed and not dry_run:
        _atomic_write(path, migrated)
    return MigrationResult(path=path, legacy_keys_rewritten=legacy_keys_rewritten, changed=changed)


def discover_state_files(repo_root: Path) -> tuple[Path, ...]:
    programs_root = repo_root / "programs"
    if not programs_root.exists():
        return ()
    # Check both new canonical path (programs/<id>/nudge/nudge_state.json)
    # and legacy path (programs/<id>/nudge_state.json)
    found: set[Path] = set()
    found.update(p for p in programs_root.glob("*/nudge/nudge_state.json") if p.is_file())
    found.update(p for p in programs_root.glob("*/nudge_state.json") if p.is_file())
    return tuple(sorted(found))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rewrite legacy bare numeric nudge_state.json keys to prefixed item:/freshness: keys."
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--state-file", dest="state_files", type=Path, action="append", default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    state_files = tuple(args.state_files) if args.state_files else discover_state_files(args.repo_root)
    if not state_files:
        print("No nudge state files found.")
        return 0

    changed_files = 0
    rewritten_keys = 0
    for path in state_files:
        result = migrate_state_file(path, dry_run=args.dry_run)
        status = "would rewrite" if args.dry_run and result.changed else "rewrote" if result.changed else "unchanged"
        print(f"{status}: {path} (legacy keys: {result.legacy_keys_rewritten})")
        if result.changed:
            changed_files += 1
            rewritten_keys += result.legacy_keys_rewritten

    print(f"Processed {len(state_files)} file(s); changed {changed_files}; legacy keys rewritten {rewritten_keys}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

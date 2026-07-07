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
class CompactResult:
    path: Path
    legacy_keys_removed: int
    changed: bool


def compact_payload(payload: dict) -> tuple[dict, int]:
    """Remove bare numeric keys; preserve and deduplicate into item:/freshness: form.

    Sets schema_version to 1.1. Never removes freshness: or unknown namespaces.
    """
    compacted: dict = {}
    legacy_keys_removed = 0

    # Set canonical schema version
    compacted["schema_version"] = NUDGE_STATE_SCHEMA_VERSION

    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        if key == "schema_version":
            continue  # already set above
        if key.isdigit():
            # Bare numeric: create both prefixed forms, remove the bare key
            legacy_keys_removed += 1
            item_key = f"item:{key}"
            freshness_key = f"freshness:{key}"
            compacted.setdefault(item_key, value)
            compacted.setdefault(freshness_key, value)
        else:
            compacted[key] = value

    return compacted, legacy_keys_removed


def compact_state_file(path: Path, *, dry_run: bool = False) -> CompactResult:
    payload = _load_payload(path)
    compacted, legacy_keys_removed = compact_payload(payload)
    changed = compacted != payload
    if changed and not dry_run:
        _atomic_write(path, compacted)
    return CompactResult(path=path, legacy_keys_removed=legacy_keys_removed, changed=changed)


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
        description="Remove remaining legacy bare numeric nudge_state.json keys after prefixed schema has been in use for one full cycle."
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
    removed_keys = 0
    for path in state_files:
        result = compact_state_file(path, dry_run=args.dry_run)
        status = "would compact" if args.dry_run and result.changed else "compacted" if result.changed else "unchanged"
        print(f"{status}: {path} (legacy keys removed: {result.legacy_keys_removed})")
        if result.changed:
            changed_files += 1
            removed_keys += result.legacy_keys_removed

    print(f"Processed {len(state_files)} file(s); changed {changed_files}; legacy keys removed {removed_keys}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Recoverably quarantine synthetic AI telemetry from program measurement trees.

RELO-W0.10a treats ``trace-model`` and ``unit-test`` records as test fixtures,
not operating evidence. This utility never deletes them: it copies unique raw
records to a sibling quarantine JSONL before atomically rewriting the live
sidecar without those records. Re-running it is safe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path


def is_synthetic_record(raw_line: str) -> bool:
    """Return whether one JSONL line bears a known test-only marker."""
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and (
        payload.get("deployment_id") == "trace-model" or payload.get("feature") == "unit-test"
    )


def quarantine_path(path: Path) -> Path:
    return path.with_name("ai_telemetry.quarantined.jsonl")


def quarantine_synthetic_records(path: Path, *, check_only: bool = False) -> tuple[int, int]:
    """Return ``(synthetic_count, newly_quarantined_count)`` for *path*.

    ``check_only`` does not alter files. A malformed line is retained in the
    live file rather than guessed at, because measurement repair must not
    silently discard unknown evidence.
    """
    if not path.exists():
        return (0, 0)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    synthetic = [line for line in lines if is_synthetic_record(line)]
    if check_only or not synthetic:
        return (len(synthetic), 0)

    quarantine = quarantine_path(path)
    existing = set(_line_digests(quarantine.read_text(encoding="utf-8").splitlines()) if quarantine.exists() else ())
    additions = [line for line in synthetic if _line_digest(line) not in existing]
    if additions:
        _append_and_fsync(quarantine, additions)
    retained = [line for line in lines if not is_synthetic_record(line)]
    _atomic_write(path, retained)
    return (len(synthetic), len(additions))


def _line_digest(line: str) -> str:
    return hashlib.sha256(line.rstrip("\r\n").encode("utf-8")).hexdigest()


def _line_digests(lines: Iterable[str]) -> Iterable[str]:
    return (_line_digest(line) for line in lines if line.strip())


def _append_and_fsync(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        for line in lines:
            handle.write(line if line.endswith(("\n", "\r")) else f"{line}\n")
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write(path: Path, lines: Iterable[str]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent, text=True)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report contamination without changing files.")
    parser.add_argument("paths", nargs="+", type=Path, help="Explicit telemetry sidecars to process.")
    args = parser.parse_args(argv)
    paths = tuple(args.paths)
    remaining = 0
    for path in paths:
        count, added = quarantine_synthetic_records(path, check_only=args.check)
        remaining += count if args.check else 0
        print(f"{path}: synthetic={count}; quarantined_new={added}; mode={'check' if args.check else 'apply'}")
    return 2 if args.check and remaining else 0


if __name__ == "__main__":
    raise SystemExit(main())

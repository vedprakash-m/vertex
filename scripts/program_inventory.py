#!/usr/bin/env python3
"""Program-directory inventory — the human-facing companion to ``vertex doctor``
DC-01 (specs/declutter.md §2.1, §6 Phase 0.5 task 1, §12).

Reports the authoritative root inventory of ``programs/<id>/``, classifying
every entry by tier (T-1 authored config … T-8 docs/research) using the
single Zone-A source of truth in ``src/core/program_paths.py``
(``ROOT_ENTRIES`` + ``RUNTIME_ARTIFACTS``). It supersedes the hand-counted
figures in §2/§12 and is the source the success metrics are computed from.

Usage::

    python scripts/program_inventory.py --program nova --root-only
    python scripts/program_inventory.py --program nova --root-only --format json
    python scripts/program_inventory.py --program nova        # incl. recursive subdir sizes

Classifications emitted per root entry:

* ``recognized``      — in ``ROOT_ENTRIES`` (tier shown: T-1..T-8)
* ``runtime-artifact``— a T-3 file in ``RUNTIME_ARTIFACTS`` (legacy root vs
  canonical ``runtime/`` location reported — the DC-02 split-brain signal)
* ``clutter``         — ``*.bak*`` / ``*.lock`` / ``*.cp1252bak`` remnants
* ``marker``          — a dotfile tool marker (recognized or unrecognized)
* ``unrecognized``    — a root entry not in the taxonomy (flag for review)

Exit code is non-zero if any clutter or unrecognized root entry is found, so
the script can gate operator approval.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.config_loader import PROGRAMS_ROOT  # noqa: E402
from src.core.program_paths import (  # noqa: E402
    ROOT_ENTRIES,
    ROOT_ENTRY_NAMES,
    RUNTIME_ARTIFACTS,
    RUNTIME_ARTIFACTS_BY_NAME,
    RUNTIME_FILENAMES,
    RUNTIME_SUBDIR,
)

# Root-entry tier lookup by name (files / markers / dirs).
_ROOT_TIER_BY_NAME = {entry.name: entry.tier for entry in ROOT_ENTRIES}
_ROOT_KIND_BY_NAME = {entry.name: entry.kind for entry in ROOT_ENTRIES}
# Runtime-artifact legacy filenames → artifact metadata.
_RUNTIME_BY_FILENAME = {a.filename: a for a in RUNTIME_ARTIFACTS}

_CLUTTER_SUFFIXES = (".bak", ".bak2", ".bak3", ".bak4", ".lock", ".cp1252bak")


def _is_clutter(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in _CLUTTER_SUFFIXES)


def _classify_root_entry(name: str, *, is_dir: bool) -> tuple[str, str | None, dict]:
    """Return ``(classification, tier_or_None, extra)`` for one root entry.

    ``extra`` carries per-classification detail (runtime-artifact legacy/canonical
    location + checkpointed flag; marker recognized flag).
    """
    if name in _RUNTIME_BY_FILENAME:
        artifact = _RUNTIME_BY_FILENAME[name]
        return "runtime-artifact", artifact.tier, {
            "artifact": artifact.name,
            "checkpointed": artifact.checkpointed,
            "delete_safe": artifact.delete_safe,
            "canonical_rel": str(artifact.canonical_rel),
        }
    if name in _ROOT_TIER_BY_NAME:
        return "recognized", _ROOT_TIER_BY_NAME[name], {}
    if _is_clutter(name):
        return "clutter", None, {}
    if name.startswith("."):
        return "marker", None, {"recognized": False}
    return "unrecognized", None, {}


def _dir_size(path: Path) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` under a directory (recursive)."""
    count = 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
            count += 1
    return count, total


def inventory_program(program_id: str, programs_root: Path, *, root_only: bool) -> dict:
    program_dir = programs_root / program_id
    result: dict = {
        "program": program_id,
        "programs_root": str(programs_root),
        "program_dir": str(program_dir),
        "program_dir_exists": program_dir.exists(),
        "root_entries": [],
        "subdir_sizes": [],
        "summary": {},
    }
    if not program_dir.exists():
        return result

    runtime_dir = program_dir / RUNTIME_SUBDIR

    # Root-level entries.
    clutter: list[dict] = []
    unrecognized: list[dict] = []
    by_tier: dict[str, int] = {}
    by_classification: dict[str, int] = {}
    for child in sorted(program_dir.iterdir(), key=lambda p: p.name):
        name = child.name
        is_dir = child.is_dir()
        classification, tier, extra = _classify_root_entry(name, is_dir=is_dir)
        entry: dict = {
            "name": name,
            "kind": "dir" if is_dir else ("file" if child.is_file() else "other"),
            "classification": classification,
            "tier": tier,
        }
        if not is_dir and child.is_file():
            try:
                entry["size_bytes"] = child.stat().st_size
            except OSError:
                entry["size_bytes"] = None
        # Runtime-artifact: report where the data actually lives today
        # (legacy root, canonical runtime/, or both — the DC-02 split-brain signal).
        if classification == "runtime-artifact":
            at_root = (program_dir / name).exists()
            at_runtime = (runtime_dir / name).exists()
            entry["at_root"] = at_root
            entry["at_runtime"] = at_runtime
            entry["split_brain"] = at_root and at_runtime
            entry.update(extra)
        elif classification == "marker":
            entry.update(extra)
        result["root_entries"].append(entry)
        by_classification[classification] = by_classification.get(classification, 0) + 1
        if tier is not None:
            by_tier[tier] = by_tier.get(tier, 0) + 1
        if classification == "clutter":
            clutter.append({"name": name, "kind": entry["kind"]})
        elif classification == "unrecognized":
            unrecognized.append({"name": name, "kind": entry["kind"]})

    root_file_count = sum(1 for e in result["root_entries"] if e["kind"] == "file")
    root_dir_count = sum(1 for e in result["root_entries"] if e["kind"] == "dir")

    summary: dict = {
        "root_file_count": root_file_count,
        "root_dir_count": root_dir_count,
        "root_entry_count": len(result["root_entries"]),
        "by_tier": dict(sorted(by_tier.items())),
        "by_classification": dict(sorted(by_classification.items())),
        "clutter_count": len(clutter),
        "clutter": clutter,
        "unrecognized_count": len(unrecognized),
        "unrecognized": unrecognized,
        "clean": not clutter and not unrecognized,
    }

    # Recursive subdirectory sizing (skipped under --root-only).
    if not root_only:
        for child in sorted(program_dir.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            count, total = _dir_size(child)
            summary.setdefault("subdir_sizes", [])
            result["subdir_sizes"].append(
                {"name": child.name, "file_count": count, "size_bytes": total}
            )

    result["summary"] = summary
    return result


def _render_human(report: dict) -> str:
    lines: list[str] = []
    s = report["summary"]
    lines.append(f"# Program inventory: {report['program']}")
    lines.append(f"  dir: {report['program_dir']} (exists={report['program_dir_exists']})")
    if not report["program_dir_exists"]:
        return "\n".join(lines)
    lines.append("")
    lines.append(
        f"  root entries: {s['root_entry_count']} "
        f"({s['root_file_count']} files, {s['root_dir_count']} dirs)"
    )
    lines.append("  by classification:")
    for cls, n in s["by_classification"].items():
        lines.append(f"    {cls}: {n}")
    if s["by_tier"]:
        lines.append("  by tier (recognized/runtime):")
        for tier, n in s["by_tier"].items():
            lines.append(f"    {tier}: {n}")
    lines.append("")
    lines.append("  root entries:")
    for e in report["root_entries"]:
        tier = f" [{e['tier']}]" if e["tier"] else ""
        size = ""
        if e.get("size_bytes") is not None:
            size = f" ({e['size_bytes']} bytes)"
        suffix = ""
        if e["classification"] == "runtime-artifact":
            loc = []
            if e.get("at_root"):
                loc.append("root")
            if e.get("at_runtime"):
                loc.append("runtime/")
            suffix = f" -> {','.join(loc) or 'absent'}"
            if e.get("split_brain"):
                suffix += " SPLIT-BRAIN"
            if e.get("checkpointed"):
                suffix += " checkpointed"
        lines.append(f"    {e['kind']:<5} {e['classification']:<18}{e['name']}{tier}{size}{suffix}")
    if report["subdir_sizes"]:
        lines.append("")
        lines.append("  subdirectory sizes (recursive):")
        for d in report["subdir_sizes"]:
            lines.append(f"    {d['name']:<20} {d['file_count']} files, {d['size_bytes']} bytes")
    lines.append("")
    if s["clutter"]:
        lines.append(f"  CLUTTER ({s['clutter_count']}): " + ", ".join(c["name"] for c in s["clutter"]))
    if s["unrecognized"]:
        lines.append(
            f"  UNRECOGNIZED ({s['unrecognized_count']}): "
            + ", ".join(u["name"] for u in s["unrecognized"])
        )
    lines.append(f"  clean: {s['clean']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Program-directory inventory (declutter.md).")
    parser.add_argument("--program", default="nova", help="Program id (default: nova).")
    parser.add_argument(
        "--programs-root",
        type=Path,
        default=PROGRAMS_ROOT,
        help=f"Programs root (default: {PROGRAMS_ROOT}).",
    )
    parser.add_argument(
        "--root-only",
        action="store_true",
        help="Report root-level entries only (skip recursive subdirectory sizing).",
    )
    parser.add_argument("--format", choices=["human", "json"], default="human")
    args = parser.parse_args(argv)

    report = inventory_program(args.program, args.programs_root, root_only=args.root_only)

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(_render_human(report))

    return 0 if report["summary"].get("clean", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
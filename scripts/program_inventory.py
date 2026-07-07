#!/usr/bin/env python3
"""Program-directory inventory — the human-facing companion to ``vertex doctor``
DC-01 (specs/declutter.md §2.1, §6 Phase 0.5 task 1, §12).

Reports the authoritative root inventory of ``programs/<id>/``, classifying
every entry by tier (T-1 authored config … T-8 docs/research) using the
single Zone-A source of truth in ``src/core/program_paths.py``
(``ROOT_ENTRIES`` + ``RUNTIME_ARTIFACTS``). It supersedes the hand-counted
figures in §2/§12 and is the source the success metrics are computed from.

Usage::

    python scripts/program_inventory.py --program example --root-only
    python scripts/program_inventory.py --program example --root-only --format json
    python scripts/program_inventory.py --program example        # incl. recursive subdir sizes

Classifications emitted per root entry:

* ``recognized``      — in ``ROOT_ENTRIES`` (tier shown: T-1..T-8)
* ``runtime-artifact``— a T-3 file in ``RUNTIME_ARTIFACTS`` (legacy root vs
  canonical ``runtime/`` location reported — the DC-02 split-brain signal)
* ``clutter``         — ``*.bak*`` / ``*.lock`` / ``*.cp1252bak`` remnants
* ``marker``          — a dotfile tool marker (recognized or unrecognized)
* ``unrecognized``    — a root entry not in the taxonomy (flag for review)

Exit code is non-zero if any clutter or unrecognized root entry is found, so
the script can gate operator approval. Registered runtime locations are checked
independently, including canonical-only stores; split-brain and wrong-kind
locations are also findings. Metadata/enumeration failures or links abort with
stderr/exit 2, never a successful partial report. Reports are metadata-only,
not atomic snapshots, cleanup approval or complete workspace recovery evidence.

Authored metadata is bounded to exact T-1/T-2 root registry entries and unknown
root entries (whose authorship is not inferred). Recursive mode also lists
descendants of T-1/T-2 directories as unclassified: the root registry supplies
no descendant rules. Other tiers and review currency are not assessed by this
authored view. ``--root-only`` never inspects authored descendants.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.workspace_paths import PROGRAMS_ROOT, is_bare_identifier  # noqa: E402
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
_AUTHORED_TIERS = frozenset({"T-1", "T-2"})
_AUTHORED_DIR_NAMES = frozenset(
    entry.name for entry in ROOT_ENTRIES if entry.tier in _AUTHORED_TIERS and entry.kind == "dir"
)
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
        if is_dir:
            return "unrecognized", None, {}
        artifact = _RUNTIME_BY_FILENAME[name]
        return "runtime-artifact", artifact.tier, {
            "artifact": artifact.name,
            "checkpointed": artifact.checkpointed,
            "delete_safe": artifact.delete_safe,
            "canonical_rel": str(artifact.canonical_rel),
        }
    if name in _ROOT_TIER_BY_NAME:
        if is_dir != (_ROOT_KIND_BY_NAME[name] == "dir"):
            return "unrecognized", None, {}
        return "recognized", _ROOT_TIER_BY_NAME[name], {}
    if _is_clutter(name):
        return "clutter", None, {}
    if name.startswith("."):
        return "marker", None, {"recognized": False}
    return "unrecognized", None, {}


def _checked_stat(path: Path) -> os.stat_result:
    metadata = path.lstat()
    reparse = getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(metadata.st_mode) or reparse:
        raise ValueError("Inventory cannot traverse links or reparse points.")
    return metadata


def _optional_stat(path: Path) -> os.stat_result | None:
    try:
        return _checked_stat(path)
    except FileNotFoundError:
        return None


def _scan(path: Path) -> Iterator[Path]:
    with os.scandir(path) as entries:
        for entry in entries:
            yield Path(entry.path)


def _kind(metadata: os.stat_result | None) -> str | None:
    if metadata is None:
        return None
    if stat.S_ISDIR(metadata.st_mode):
        return "dir"
    return "file" if stat.S_ISREG(metadata.st_mode) else "other"


def _dir_size(path: Path, *, authored_entries: list[dict] | None = None) -> tuple[int, int]:
    """Return metadata-only totals; failed traversal never yields partial success."""
    count = 0
    total = 0
    pending = [path]
    while pending:
        directory = pending.pop()
        if _kind(_checked_stat(directory)) != "dir":
            raise ValueError("Inventory recursive paths must remain directories.")
        for child in _scan(directory):
            metadata = _checked_stat(child)
            kind = _kind(metadata)
            if kind == "dir":
                pending.append(child)
            elif kind == "file":
                count += 1
                total += metadata.st_size
            else:
                raise ValueError("Inventory encountered an unsupported filesystem entry.")
            if authored_entries is not None:
                entry: dict = {
                    "path": str(child.relative_to(path.parent)),
                    "kind": kind,
                    "classification": "unclassified",
                    "tier": None,
                }
                if kind == "file":
                    entry["size_bytes"] = metadata.st_size
                authored_entries.append(entry)
    return count, total


def inventory_program(program_id: str, programs_root: Path, *, root_only: bool) -> dict:
    if not is_bare_identifier(program_id):
        raise ValueError("Inventory requires one bare program identifier.")
    programs_root = programs_root.absolute()
    program_dir = programs_root / program_id
    result: dict = {
        "program": program_id,
        "programs_root": str(programs_root),
        "program_dir": str(program_dir),
        "program_dir_exists": False,
        "scope": {
            "root_entries": True,
            "registered_runtime_locations": True,
            "recursive_sizes": not root_only,
            "authored_state": "root-only" if root_only else "recursive-metadata",
            "authored_registry_tiers": sorted(_AUTHORED_TIERS),
            "review_currency": False,
            "content_reads": False,
            "cleanup_authority": False,
            "snapshot_consistent": False,
        },
        "root_entries": [],
        "runtime_artifacts": [],
        "authored_entries": [],
        "subdir_sizes": [],
        "summary": {},
    }
    for ancestor in reversed(program_dir.parents):
        metadata = _optional_stat(ancestor)
        if metadata is None:
            return result
        if _kind(metadata) != "dir":
            raise ValueError("Inventory program ancestors must be directories.")
    program_metadata = _optional_stat(program_dir)
    if program_metadata is None:
        return result
    if _kind(program_metadata) != "dir":
        raise ValueError("Inventory program path must be a directory.")
    result["program_dir_exists"] = True

    runtime_dir = program_dir / RUNTIME_SUBDIR

    # Root-level entries.
    clutter: list[dict] = []
    unrecognized: list[dict] = []
    by_tier: dict[str, int] = {}
    by_classification: dict[str, int] = {}
    root_metadata = {}
    for child in sorted(_scan(program_dir), key=lambda p: p.name):
        metadata = _checked_stat(child)
        root_metadata[child.name] = metadata
        name = child.name
        kind = _kind(metadata)
        is_dir = kind == "dir"
        classification, tier, extra = _classify_root_entry(name, is_dir=is_dir)
        if kind == "other":
            classification, tier, extra = "unrecognized", None, {}
        entry: dict = {
            "name": name,
            "kind": kind,
            "classification": classification,
            "tier": tier,
        }
        if kind == "file":
            entry["size_bytes"] = metadata.st_size
        if classification == "runtime-artifact":
            entry.update(extra)
        elif classification == "marker":
            entry.update(extra)
        result["root_entries"].append(entry)
        by_classification[classification] = by_classification.get(classification, 0) + 1
        if tier is not None:
            by_tier[tier] = by_tier.get(tier, 0) + 1
        if classification == "clutter":
            clutter.append({"name": name, "kind": entry["kind"]})
        elif classification == "unrecognized" or (classification == "marker" and not extra.get("recognized")):
            unrecognized.append({"name": name, "kind": entry["kind"]})
        if tier in _AUTHORED_TIERS or classification == "unrecognized" or (
            classification == "marker" and not extra.get("recognized")
        ):
            authored: dict = {
                "path": name,
                "kind": kind,
                "classification": "recognized" if tier in _AUTHORED_TIERS else "unclassified",
                "tier": tier,
            }
            if kind == "file":
                authored["size_bytes"] = metadata.st_size
            result["authored_entries"].append(authored)

    runtime_metadata = root_metadata.get(RUNTIME_SUBDIR)
    if runtime_metadata is not None and _kind(runtime_metadata) != "dir":
        raise ValueError("Inventory runtime path must be a directory.")
    for artifact in RUNTIME_ARTIFACTS:
        legacy = root_metadata.get(artifact.filename)
        canonical = _optional_stat(runtime_dir / artifact.filename) if runtime_metadata is not None else None
        result["runtime_artifacts"].append({
            "name": artifact.filename,
            "artifact": artifact.name,
            "tier": artifact.tier,
            "legacy_path": str(program_dir / artifact.legacy_rel),
            "canonical_path": str(program_dir / artifact.canonical_rel),
            "at_root": legacy is not None,
            "at_runtime": canonical is not None,
            "root_kind": _kind(legacy),
            "runtime_kind": _kind(canonical),
            "split_brain": legacy is not None and canonical is not None,
            "checkpointed": artifact.checkpointed,
            "delete_safe": artifact.delete_safe,
            "retention": artifact.retention,
        })
    runtime_by_name = {entry["name"]: entry for entry in result["runtime_artifacts"]}
    for entry in result["root_entries"]:
        if entry["classification"] == "runtime-artifact":
            entry.update({key: runtime_by_name[entry["name"]][key] for key in ("at_root", "at_runtime", "split_brain")})
    split_brains = sum(entry["split_brain"] for entry in result["runtime_artifacts"])
    kind_mismatches = sum(
        entry["root_kind"] not in (None, "file") or entry["runtime_kind"] not in (None, "file")
        for entry in result["runtime_artifacts"]
    )
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
        "runtime_split_brain_count": split_brains,
        "runtime_kind_mismatch_count": kind_mismatches,
        "clean": not clutter and not unrecognized and not split_brains and not kind_mismatches,
    }

    # Recursive subdirectory sizing (skipped under --root-only).
    if not root_only:
        for name, metadata in sorted(root_metadata.items()):
            if _kind(metadata) != "dir":
                continue
            child = program_dir / name
            count, total = _dir_size(
                child,
                authored_entries=result["authored_entries"] if name in _AUTHORED_DIR_NAMES else None,
            )
            summary.setdefault("subdir_sizes", [])
            result["subdir_sizes"].append(
                {"name": child.name, "file_count": count, "size_bytes": total}
            )

    result["authored_entries"].sort(key=lambda entry: entry["path"])
    summary["authored_recognized_count"] = sum(
        entry["classification"] == "recognized" for entry in result["authored_entries"]
    )
    summary["authored_unclassified_count"] = sum(
        entry["classification"] == "unclassified" for entry in result["authored_entries"]
    )
    summary["clean"] = summary["clean"] and not summary["authored_unclassified_count"]
    result["summary"] = summary
    return result


def _render_human(report: dict) -> str:
    lines: list[str] = []
    s = report["summary"]
    lines.append(f"# Program inventory: {report['program']}")
    lines.append(f"  dir: {report['program_dir']} (exists={report['program_dir_exists']})")
    lines.append("  scope: metadata only; not an atomic snapshot or cleanup authorization")
    authored_scope = (
        "authored descendants not inspected (--root-only)"
        if report["scope"]["authored_state"] == "root-only"
        else "T-1/T-2 authored descendants inspected, not inherited-recognized"
    )
    lines.append(f"  scope: {authored_scope}; review currency not assessed")
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
    lines.append("")
    lines.append(
        f"  authored metadata (T-1/T-2 roots and unclassified candidates; other tiers not assessed): "
        f"{s['authored_recognized_count']} recognized, {s['authored_unclassified_count']} unclassified"
    )
    for entry in report["authored_entries"]:
        lines.append(f"    {entry['kind']:<5} {entry['classification']:<18}{entry['path']}")
    if report["subdir_sizes"]:
        lines.append("")
        lines.append("  subdirectory sizes (recursive):")
        for d in report["subdir_sizes"]:
            lines.append(f"    {d['name']:<20} {d['file_count']} files, {d['size_bytes']} bytes")
    lines.append("")
    lines.append("  registered runtime locations:")
    for entry in report["runtime_artifacts"]:
        if entry["at_root"] or entry["at_runtime"]:
            lines.append(
                f"    {entry['name']}: legacy={entry['root_kind'] or 'absent'}, "
                f"canonical={entry['runtime_kind'] or 'absent'}, split_brain={entry['split_brain']}"
            )
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
    parser.add_argument("--program", required=True, help="Explicit program id.")
    parser.add_argument(
        "--programs-root",
        type=Path,
        default=PROGRAMS_ROOT,
        help=f"Programs root (default: {PROGRAMS_ROOT}).",
    )
    parser.add_argument(
        "--root-only",
        action="store_true",
        help="Report root entries and registered runtime locations; skip recursive sizes and authored descendants.",
    )
    parser.add_argument("--format", choices=["human", "json"], default="human")
    args = parser.parse_args(argv)

    try:
        report = inventory_program(args.program, args.programs_root, root_only=args.root_only)
    except (OSError, ValueError) as error:
        print(
            f"Inventory failed for program {args.program!r} ({type(error).__name__}); no complete report was produced. "
            "Preserve existing paths; check the program selector, access, entry types and links before retrying.",
            file=sys.stderr,
        )
        return 2

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(_render_human(report))

    return 0 if report["summary"].get("clean", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
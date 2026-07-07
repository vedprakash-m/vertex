from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable, Mapping
import json
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil

log = logging.getLogger(__name__)

from src.core.archive_store import read_archive_index
from src.core.edition_resolver import resolve_edition_paths
from src.core.jinja_filters import build_anchor
from src.core.published_narrative_store import load_published_narratives
from src.core.snapshot_store import ARCHIVE_ROOT, get_archive_root


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = REPO_ROOT / "reports"
REMOVED_SECTION_MARKER = "<!-- REMOVED — section no longer in current draft -->"
SEEDING_MANIFEST_FILENAME = ".seeding_manifest.json"
NO_SEED_SENTINEL_FILENAME = ".no-seed"
_SCAFFOLD_COMMENT = re.compile(
    r"^\s*<!--\s*vertex:scaffold\b.*?-->\s*$",
    re.MULTILINE,
)
_SEEDED_COMMENT = re.compile(
    r"^\s*<!--\s*SEEDED from Issue\b.*?-->\s*$",
    re.MULTILINE,
)
_REMOVED_SECTION_COMMENT = re.compile(
    r"^\s*<!--\s*REMOVED\s*(?:—|-)\s*section no longer in current draft\s*-->\s*$",
    re.IGNORECASE,
)
_SCAFFOLD_PLACEHOLDER_COMMENT = re.compile(
    r"^\s*<!--\s*(?:SCAFFOLD|\{[A-Z0-9_]+\})\s*-->\s*$",
    re.MULTILINE,
)
_PLACEHOLDER_LINES = {
    "[Your narrative here]",
    "[WHAT MOVED paragraph]",
    "[WHERE WE ARE paragraph]",
}
_SCAFFOLD_PLACEHOLDERS = (
    "<!-- SCAFFOLD -->",
    "{PROGRAM_OBJECTIVE}",
    "{CURRENT_STATE_SIGNAL}",
    "{WHAT_CHANGED_SIGNAL_1}",
    "{WHAT_CHANGED_SIGNAL_2}",
    "{WHAT_CHANGED_SIGNAL_3}",
    "{DECISION_OR_ASK}",
    "[Your narrative here]",
    "[WHAT MOVED paragraph]",
    "[WHERE WE ARE paragraph]",
)


@dataclass(frozen=True, slots=True)
class NarrativeSeedingState:
    seeded: bool
    source_issue: int | None
    source_path: str | None
    files_seeded: tuple[str, ...] = ()
    source_hashes: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class PublishedBaselineSyncResult:
    source_issue: int
    applied_files: tuple[str, ...]
    skipped_files: tuple[str, ...]


def get_narratives_dir(
    edition: str,
    issue_number: int,
    reports_root: Path = REPORTS_ROOT,
) -> Path:
    resolved_paths = resolve_edition_paths(
        edition,
        programs_root=reports_root.parent / "programs",
    )
    if resolved_paths is not None:
        return resolved_paths.program_dir / "narratives" / f"issue_{issue_number:03d}"
    return reports_root / edition / "narratives" / f"issue_{issue_number:03d}"


def load_narratives(
    edition: str,
    issue_number: int,
    reports_root: Path = REPORTS_ROOT,
) -> dict[str, str]:
    directory = get_narratives_dir(edition, issue_number, reports_root)
    if not directory.exists():
        return {}
    narratives = {
        path.name: strip_scaffold_comments(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.md"))
    }
    return inject_narrative_placeholders(edition, issue_number, narratives, reports_root=reports_root)


def load_archived_narratives(
    edition: str,
    issue_number: int,
    archive_root: Path = ARCHIVE_ROOT,
) -> dict[str, str]:
    directory = get_archive_root(edition, archive_root) / "narratives" / f"issue_{issue_number:03d}"
    if not directory.exists():
        return {}
    narratives = {
        path.name: strip_scaffold_comments(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.md"))
    }
    return inject_narrative_placeholders(edition, issue_number, narratives, reports_root=REPORTS_ROOT)


def inject_narrative_placeholders(
    edition: str,
    issue_number: int,
    narratives: dict[str, str],
    reports_root: Path = REPORTS_ROOT,
) -> dict[str, str]:
    try:
        from src.core.edition_resolver import resolve_edition_paths
        from src.core.overrides_store import load_overrides
        from src.core.reality_store import RealityStore
        from src.core.metric_registry import load_metric_definition_map
    except ImportError:
        return narratives

    paths = resolve_edition_paths(
        edition,
        programs_root=reports_root.parent / "programs",
    )
    if not paths:
        return narratives

    program_id = paths.program_id

    overrides = load_overrides(edition, reports_root=reports_root, issue_number=issue_number)
    decisions_by_id = {}
    if overrides and overrides.decisions:
        for d in overrides.decisions:
            decisions_by_id[d.id] = d.statement

    # Hint injection source: accepted/modified HintProposals from the section proposal store.
    hints_by_id: dict[str, str] = {}
    try:
        from src.core.section_proposal_store import load_hint_proposals

        for hint in load_hint_proposals(program_id, issue_number, programs_root=reports_root.parent / "programs"):
            if hint.status in ("accepted", "modified"):
                hints_by_id[hint.hint_id] = hint.accepted_text or hint.suggested_sentence
    except Exception as exc:  # noqa: BLE001 — best-effort hint injection
        # WS-13 PB-29: the absence of a section-proposal store is normal
        # for older archives; log at debug to avoid noise but do not raise.
        log.debug("narrative_store: hint proposals not loaded: %s", exc)

    # ws-lead injection derives the lead sentence from the raw narratives dict already in scope.
    # Importing lazily avoids the circular import with exec_summary_diff_engine (which imports this module).
    try:
        from src.core.exec_summary_diff_engine import extract_lead_sentence
    except Exception:  # noqa: BLE001 — intentional cycle break
        # WS-13 PB-29: circular import fallback; extract_lead_sentence is
        # only used downstream when available. Silent degrade is the
        # documented contract.
        extract_lead_sentence = None  # type: ignore[assignment]

    metric_map = {}
    try:
        metric_map = load_metric_definition_map()
    except Exception as exc:  # noqa: BLE001 — best-effort metric map
        # WS-13 PB-29: metric map is advisory; absence is non-fatal.
        log.debug("narrative_store: metric map not loaded: %s", exc)

    # Keep render-time placeholder injection hermetic to the repo/runtime root
    # instead of silently reaching into the user's profile DB.
    store = RealityStore(program_id, db_root=reports_root.parent / "vertex-db")
    store.initialize()

    injected = {}
    for filename, text in narratives.items():
        # 1. Metric Injection
        def replace_metric(match):
            metric_id = match.group(1).strip()
            obs_list = store.list_metric_observations(metric_id)
            if not obs_list:
                return "[value pending]"
            obs = obs_list[-1]

            m_def = metric_map.get(metric_id)
            unit = m_def.unit.strip().lower() if (m_def and m_def.unit) else "count"

            if obs.value_num is not None:
                val = obs.value_num
                if isinstance(val, float):
                    formatted_val = f"{val:.1f}" if val % 1 != 0 else str(int(val))
                else:
                    formatted_val = str(val)
            elif obs.value_text is not None:
                formatted_val = obs.value_text
            else:
                return "[value pending]"

            if unit == "pct":
                return f"{formatted_val}%"
            elif unit == "mins":
                return f"{formatted_val} min"
            elif unit == "count":
                return formatted_val
            else:
                return f"{formatted_val} {unit}"

        text_with_metrics = re.sub(
            r"<!--\s*vertex:metric:\s*(\S+)\s*-->",
            replace_metric,
            text,
            flags=re.IGNORECASE,
        )

        # 2. Decision Injection
        def replace_decision(match):
            d_id = match.group(1).strip()
            statement = decisions_by_id.get(d_id)
            if statement is not None:
                return statement
            return "[decision pending]"

        text_with_decisions = re.sub(
            r"<!--\s*vertex:decision:\s*(\S+)\s*-->",
            replace_decision,
            text_with_metrics,
            flags=re.IGNORECASE,
        )

        # 3. Hint Injection (accepted/modified narrative-delta hints from the hint proposal store)
        def replace_hint(match):
            hint_id = match.group(1).strip()
            return hints_by_id.get(hint_id, "[hint pending]")

        text_with_hints = re.sub(
            r"<!--\s*vertex:hint:\s*(\S+)\s*-->",
            replace_hint,
            text_with_decisions,
            flags=re.IGNORECASE,
        )

        # 4. Workstream-lead Injection (auto-sync exec bullet to the workstream narrative lead).
        #    Render-only: QG-23 reads the raw exec_summary.md, so its ws-lead anchors are unaffected.
        def replace_ws_lead(match):
            ws_id = match.group(1).strip()
            if extract_lead_sentence is None:
                return "[lead pending]"
            for fname in (f"ws_{ws_id}.md", f"chapter_{ws_id}.md", f"{ws_id}.md"):
                if fname in narratives:
                    lead = extract_lead_sentence(narratives[fname])
                    if lead:
                        return lead
            return "[lead pending]"

        text_with_ws_lead = re.sub(
            r"<!--\s*vertex:ws-lead:\s*(\S+)\s*-->",
            replace_ws_lead,
            text_with_hints,
            flags=re.IGNORECASE,
        )

        injected[filename] = text_with_ws_lead

    return injected



def get_narrative_seeding_manifest_path(
    edition: str,
    issue_number: int,
    reports_root: Path = REPORTS_ROOT,
) -> Path:
    return get_narratives_dir(edition, issue_number, reports_root) / SEEDING_MANIFEST_FILENAME


def narrative_seeding_disabled(
    edition: str,
    issue_number: int,
    reports_root: Path = REPORTS_ROOT,
) -> bool:
    return (get_narratives_dir(edition, issue_number, reports_root) / NO_SEED_SENTINEL_FILENAME).exists()


def load_narrative_seeding_state(
    edition: str,
    issue_number: int,
    reports_root: Path = REPORTS_ROOT,
) -> NarrativeSeedingState | None:
    path = get_narrative_seeding_manifest_path(edition, issue_number, reports_root)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    files = payload.get("files", {})
    return NarrativeSeedingState(
        seeded=True,
        source_issue=int(payload["source_issue"]) if payload.get("source_issue") is not None else None,
        source_path=str(payload.get("source_path")) if payload.get("source_path") is not None else None,
        files_seeded=tuple(sorted(str(filename) for filename in files)),
        source_hashes={
            str(filename): str(details.get("source_hash", ""))
            for filename, details in files.items()
            if isinstance(details, dict) and details.get("source_hash")
        },
    )


def seed_narratives_from_prior(
    edition: str,
    *,
    target_issue_number: int,
    source_issue_number: int,
    valid_filenames: set[str],
    removed_section_ids: set[str],
    reports_root: Path = REPORTS_ROOT,
    archive_root: Path = ARCHIVE_ROOT,
) -> NarrativeSeedingState | None:
    target_dir = get_narratives_dir(edition, target_issue_number, reports_root)
    if not _directory_is_seedable(target_dir):
        return load_narrative_seeding_state(edition, target_issue_number, reports_root)

    source_narratives, source_path = _load_seed_source_narratives(
        edition,
        source_issue_number,
        reports_root=reports_root,
        archive_root=archive_root,
    )
    if not source_narratives:
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    seeded_files: dict[str, str] = {}
    for filename, content in source_narratives.items():
        section_id = _normalize_seed_section_id(filename)
        if filename not in valid_filenames and section_id is None:
            continue
        if section_id is not None and section_id in removed_section_ids:
            continue
        _write_markdown(
            target_dir / filename,
            f"<!-- SEEDED from Issue {source_issue_number:03d} — review and update with current evidence -->\n\n{content}".rstrip(),
        )
        seeded_files[filename] = f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"

    if not seeded_files:
        return None

    manifest_path = get_narrative_seeding_manifest_path(edition, target_issue_number, reports_root)
    if not manifest_path.exists():
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "source_issue": source_issue_number,
                    "source_path": source_path,
                    "files": {
                        filename: {"source_hash": source_hash}
                        for filename, source_hash in sorted(seeded_files.items())
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    return load_narrative_seeding_state(edition, target_issue_number, reports_root)


def sync_published_baseline_to_target(
    edition: str,
    *,
    target_issue_number: int,
    source_issue_number: int,
    reports_root: Path = REPORTS_ROOT,
    archive_root: Path = ARCHIVE_ROOT,
    write: bool = True,
    valid_filenames: set[str] | None = None,
    removed_section_ids: set[str] | None = None,
    published_narratives: Mapping[str, str] | None = None,
    published_source_hashes: Mapping[str, str] | None = None,
) -> PublishedBaselineSyncResult | None:
    published_source = dict(published_narratives or load_published_narratives(edition, source_issue_number, archive_root=archive_root))
    if not published_source:
        return None

    fallback_source, fallback_source_path = _load_fallback_source_narratives(
        edition,
        source_issue_number,
        reports_root=reports_root,
        archive_root=archive_root,
    )
    merged_source = dict(fallback_source)
    merged_source.update(published_source)
    if not merged_source:
        return None

    published_hash_lookup = {
        filename: str(source_hash)
        for filename, source_hash in (published_source_hashes or {}).items()
    }
    for filename, content in published_source.items():
        published_hash_lookup.setdefault(
            filename,
            f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}",
        )

    target_dir = get_narratives_dir(edition, target_issue_number, reports_root)
    if write:
        target_dir.mkdir(parents=True, exist_ok=True)

    applied_files: list[str] = []
    skipped_files: list[str] = []
    source_entries: dict[str, dict[str, str]] = {}
    removed_ids = removed_section_ids or set()
    for filename, content in sorted(merged_source.items()):
        section_id = _normalize_seed_section_id(filename)
        if valid_filenames is not None and filename not in valid_filenames and section_id is None:
            continue
        if section_id is not None and section_id in removed_ids:
            continue

        target_path = target_dir / filename
        fallback_content = fallback_source.get(filename, "")
        if not _target_file_is_safe_to_replace(target_path, fallback_content, source_issue_number):
            skipped_files.append(filename)
            continue

        source_path = "published_archive" if filename in published_source else (fallback_source_path or "archive")
        source_hash = published_hash_lookup.get(filename)
        if source_hash is None:
            source_hash = f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"

        applied_files.append(filename)
        source_entries[filename] = {
            "source_hash": source_hash,
            "source_path": source_path,
        }
        if write:
            _write_markdown(target_path, _build_published_seeded_content(source_issue_number, content).rstrip())

    if write and source_entries:
        _update_published_sync_seeding_manifest(
            edition,
            target_issue_number=target_issue_number,
            source_issue_number=source_issue_number,
            source_entries=source_entries,
            reports_root=reports_root,
        )

    return PublishedBaselineSyncResult(
        source_issue=source_issue_number,
        applied_files=tuple(applied_files),
        skipped_files=tuple(skipped_files),
    )


def strip_scaffold_comments(text: str) -> str:
    lines = text.splitlines(keepends=True)
    filtered_lines: list[str] = []
    removed_any = False
    for line in lines:
        stripped_line = line.strip()
        if _SCAFFOLD_COMMENT.match(line) or _SEEDED_COMMENT.match(line) or _SCAFFOLD_PLACEHOLDER_COMMENT.match(line):
            removed_any = True
            continue
        if stripped_line in _PLACEHOLDER_LINES:
            removed_any = True
            continue
        filtered_lines.append(line)
    if not removed_any:
        return text
    while filtered_lines and not filtered_lines[0].strip():
        filtered_lines.pop(0)
    while filtered_lines and not filtered_lines[-1].strip():
        filtered_lines.pop()
    normalized = "".join(filtered_lines)
    if not normalized.strip():
        return ""
    if normalized.strip() == "<!-- state -->":
        return ""
    return normalized


def find_unresolved_scaffold_placeholders(
    edition: str,
    issue_number: int,
    reports_root: Path = REPORTS_ROOT,
) -> dict[str, tuple[str, ...]]:
    directory = get_narratives_dir(edition, issue_number, reports_root)
    if not directory.exists():
        return {}
    unresolved: dict[str, tuple[str, ...]] = {}
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        placeholders = tuple(token for token in _SCAFFOLD_PLACEHOLDERS if token in text)
        if placeholders:
            unresolved[path.name] = placeholders
    return unresolved


def build_workstream_narrative_history(
    edition: str,
    issue_number: int,
    workstream_names: Iterable[str],
    current_workstream_blurbs: Mapping[str, str],
    archive_root: Path = ARCHIVE_ROOT,
) -> dict[str, tuple[str, ...]]:
    archive_index = read_archive_index(edition, archive_root=archive_root)
    previous_confirmed_issue_numbers = [
        entry.issue_number
        for entry in sorted(archive_index.issues, key=lambda entry: entry.issue_number, reverse=True)
        if entry.kind == "confirmed" and entry.issue_number < issue_number
    ][:2]
    if len(previous_confirmed_issue_numbers) < 2:
        return {}

    archived_narratives_by_issue = {
        previous_issue_number: load_archived_narratives(edition, previous_issue_number, archive_root=archive_root)
        for previous_issue_number in previous_confirmed_issue_numbers
    }
    history: dict[str, tuple[str, ...]] = {}
    for workstream_name in workstream_names:
        section_id = build_anchor(workstream_name)
        current_blurb = current_workstream_blurbs.get(section_id, "").strip()
        if not current_blurb:
            continue

        archived_blurbs: list[str] = []
        for previous_issue_number in previous_confirmed_issue_numbers:
            archived_blurb = archived_narratives_by_issue[previous_issue_number].get(f"ws_{section_id}.md", "").strip()
            if not archived_blurb:
                archived_blurbs = []
                break
            archived_blurbs.append(archived_blurb)

        if len(archived_blurbs) == len(previous_confirmed_issue_numbers):
            history[workstream_name] = (current_blurb, *archived_blurbs)
    return history


def merge_narratives(
    edition: str,
    issue_number: int,
    templates: dict[str, str],
    reports_root: Path = REPORTS_ROOT,
) -> Path:
    directory = get_narratives_dir(edition, issue_number, reports_root)
    directory.mkdir(parents=True, exist_ok=True)
    existing_paths = {path.name: path for path in directory.glob("*.md")}

    for filename, template in templates.items():
        path = directory / filename
        if not path.exists():
            _write_markdown(path, template)
            continue

        content = path.read_text(encoding="utf-8")
        uncommented = _strip_removed_marker(content)
        if uncommented != content:
            _write_markdown(path, uncommented)

    for filename, path in existing_paths.items():
        if filename in templates:
            continue
        content = path.read_text(encoding="utf-8")
        if _has_removed_marker(content):
            continue
        _write_markdown(path, f"{REMOVED_SECTION_MARKER}\n\n{content}")

    return directory


def archive_narratives(
    edition: str,
    issue_number: int,
    archive_root: Path = ARCHIVE_ROOT,
    reports_root: Path = REPORTS_ROOT,
) -> Path:
    source_dir = get_narratives_dir(edition, issue_number, reports_root)
    archive_dir = get_archive_root(edition, archive_root) / "narratives" / f"issue_{issue_number:03d}"
    if archive_dir.exists():
        shutil.rmtree(archive_dir)
    archive_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, archive_dir)
    return archive_dir


def write_narrative_section(
    edition: str,
    issue_number: int,
    section_id: str,
    text: str,
    reports_root: Path = REPORTS_ROOT,
) -> Path:
    directory = get_narratives_dir(edition, issue_number, reports_root)
    directory.mkdir(parents=True, exist_ok=True)
    target_path = directory / narrative_filename_for_section(section_id)
    normalized = text if text.endswith("\n") else f"{text}\n"
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(normalized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, target_path)
    return target_path


def narrative_filename_for_section(section_id: str) -> str:
    if section_id == "exec_summary":
        return "exec_summary.md"
    if section_id.startswith("chapter_"):
        return f"{section_id}.md"
    return f"ws_{section_id}.md"


def delete_narratives_dir(
    edition: str,
    issue_number: int,
    reports_root: Path = REPORTS_ROOT,
) -> None:
    directory = get_narratives_dir(edition, issue_number, reports_root)
    if directory.exists():
        shutil.rmtree(directory)


def reset_narratives_for_next_issue(
    edition: str,
    next_issue_number: int,
    templates: dict[str, str],
    reports_root: Path = REPORTS_ROOT,
) -> Path:
    return merge_narratives(edition, next_issue_number, templates, reports_root)


def _write_markdown(path: Path, content: str) -> None:
    normalized = content if content.endswith("\n") else f"{content}\n"
    path.write_text(normalized, encoding="utf-8")


def _strip_removed_marker(content: str) -> str:
    normalized = strip_scaffold_comments(content)
    lines = normalized.splitlines(keepends=True)
    index = 0
    removed_any = False
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if _REMOVED_SECTION_COMMENT.match(lines[index]):
            removed_any = True
            index += 1
            continue
        break
    if not removed_any:
        return content
    stripped = "".join(lines[index:])
    return stripped.lstrip("\r\n")


def _has_removed_marker(content: str) -> bool:
    normalized = strip_scaffold_comments(content)
    for line in normalized.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return _REMOVED_SECTION_COMMENT.match(line) is not None
    return False


def _load_seed_source_narratives(
    edition: str,
    issue_number: int,
    *,
    reports_root: Path,
    archive_root: Path,
) -> tuple[dict[str, str], str | None]:
    published = load_published_narratives(edition, issue_number, archive_root=archive_root)
    archived = load_archived_narratives(edition, issue_number, archive_root=archive_root)
    if published:
        merged = dict(archived)
        merged.update(published)
        return merged, "published_archive_preferred"
    if archived:
        return archived, "archive"
    local = load_narratives(edition, issue_number, reports_root=reports_root)
    if local:
        return local, "program_local"
    return {}, None


def _load_fallback_source_narratives(
    edition: str,
    issue_number: int,
    *,
    reports_root: Path,
    archive_root: Path,
) -> tuple[dict[str, str], str | None]:
    archived = load_archived_narratives(edition, issue_number, archive_root=archive_root)
    if archived:
        return archived, "archive"
    local = load_narratives(edition, issue_number, reports_root=reports_root)
    if local:
        return local, "program_local"
    return {}, None


def _target_file_is_safe_to_replace(target_path: Path, fallback_content: str, source_issue_number: int) -> bool:
    if not target_path.exists():
        return True
    current_content = target_path.read_text(encoding="utf-8")
    if current_content.startswith(_published_seed_comment(source_issue_number)):
        return True
    normalized_current = strip_scaffold_comments(current_content).strip()
    if not normalized_current:
        return True
    if not fallback_content.strip():
        return False
    return normalized_current == fallback_content.strip()


def _build_published_seeded_content(source_issue_number: int, content: str) -> str:
    normalized = content.rstrip()
    return (
        f"{_published_seed_comment(source_issue_number)}\n\n"
        f"{normalized}\n"
    )


def _published_seed_comment(source_issue_number: int) -> str:
    return (
        f"<!-- SEEDED from Issue {source_issue_number:03d} — published EML baseline, review and update with current evidence -->"
    )


def _update_published_sync_seeding_manifest(
    edition: str,
    *,
    target_issue_number: int,
    source_issue_number: int,
    source_entries: Mapping[str, Mapping[str, str]],
    reports_root: Path,
) -> None:
    manifest_path = get_narrative_seeding_manifest_path(
        edition,
        target_issue_number,
        reports_root=reports_root,
    )
    payload = {}
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    files_payload = payload.setdefault("files", {})
    for filename, entry in sorted(source_entries.items()):
        files_payload[filename] = {
            "source_hash": str(entry["source_hash"]),
            "source_path": str(entry["source_path"]),
        }
    payload["schema_version"] = "1.0"
    payload["source_issue"] = source_issue_number
    payload["source_path"] = "published_archive_preferred"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _directory_is_seedable(directory: Path) -> bool:
    if not directory.exists():
        return True
    markdown_paths = sorted(directory.glob("*.md"))
    if not markdown_paths:
        return True
    for path in markdown_paths:
        content = path.read_text(encoding="utf-8")
        if not _is_scaffold_only_content(content):
            return False
    return True


def _is_scaffold_only_content(content: str) -> bool:
    first_nonblank = next((line.strip() for line in content.splitlines() if line.strip()), "")
    if (
        first_nonblank.startswith("<!-- SCAFFOLD")
        or first_nonblank.startswith("<!-- vertex:scaffold")
    ):
        return True
    return strip_scaffold_comments(content) == ""


def _normalize_seed_section_id(filename: str) -> str | None:
    if filename == "exec_summary.md":
        return "exec_summary"
    if filename.startswith("ws_") and filename.endswith(".md"):
        return filename[3:-3]
    if filename.startswith("chapter_") and filename.endswith(".md"):
        return filename[8:-3]
    return None

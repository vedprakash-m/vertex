from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import re
import subprocess
from typing import Callable

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
_WEEK_SPEC_RE = re.compile(r"^(?P<year>\d{4})-W(?P<week>\d{2})$")


@dataclass(frozen=True, slots=True)
class PersonDirectorySnapshot:
    alias: str
    display_name: str | None
    title: str | None
    email: str | None
    team_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KBChangelogEntry:
    commit_sha: str
    committed_at: datetime
    alias: str
    change_type: str
    before: str | None
    after: str | None


@dataclass(frozen=True, slots=True)
class KBChangelogReport:
    program_id: str
    since_week: str
    since_date: date
    entries: tuple[KBChangelogEntry, ...]


GitRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


def build_kb_changelog_report(
    *,
    program_id: str,
    since_week: str,
    repo_root: Path = REPO_ROOT,
    git_runner: GitRunner | None = None,
) -> KBChangelogReport:
    since_date = parse_since_week(since_week)
    relative_path = _resolve_people_directory_relative_path(program_id=program_id, repo_root=repo_root)
    working_tree_path = repo_root / relative_path
    if not working_tree_path.exists():
        raise ValueError(f"No people_directory.yaml found for program '{program_id}'.")

    runner = git_runner or _run_git
    commits = _read_commit_history(relative_path=relative_path, since_date=since_date, repo_root=repo_root, git_runner=runner)
    entries: list[KBChangelogEntry] = []
    for commit_sha, committed_at in commits:
        current_people = _load_people_directory_from_revision(
            revision=commit_sha,
            relative_path=relative_path,
            repo_root=repo_root,
            git_runner=runner,
        )
        previous_people = _load_people_directory_from_revision(
            revision=f"{commit_sha}^",
            relative_path=relative_path,
            repo_root=repo_root,
            git_runner=runner,
        )
        entries.extend(
            _diff_people_directory(
                commit_sha=commit_sha,
                committed_at=committed_at,
                previous_people=previous_people,
                current_people=current_people,
            )
        )

    return KBChangelogReport(
        program_id=program_id,
        since_week=since_week,
        since_date=since_date,
        entries=tuple(entries),
    )


def _resolve_people_directory_relative_path(*, program_id: str, repo_root: Path) -> Path:
    shared_path = Path("knowledge") / "people_directory.yaml"
    if (repo_root / shared_path).exists():
        return shared_path
    return Path("programs") / program_id / "knowledge" / "people_directory.yaml"


def render_kb_changelog_report(report: KBChangelogReport) -> str:
    lines = [
        f"KB Changelog: {report.program_id}",
        f"Since: {report.since_week} ({report.since_date.isoformat()})",
        "",
    ]
    if not report.entries:
        lines.append("No people_directory.yaml changes found in this window.")
        return "\n".join(lines) + "\n"

    current_commit = ""
    for entry in report.entries:
        if entry.commit_sha != current_commit:
            if current_commit:
                lines.append("")
            lines.append(f"{entry.committed_at.date().isoformat()} {entry.commit_sha[:7]}")
            current_commit = entry.commit_sha
        lines.append(f"  {render_kb_changelog_entry(entry)}")
    return "\n".join(lines) + "\n"


def render_kb_changelog_entry(entry: KBChangelogEntry) -> str:
    if entry.change_type == "new_hire":
        return f"+ {entry.alias}: added ({entry.after or 'details unavailable'})"
    if entry.change_type == "departure":
        return f"- {entry.alias}: removed ({entry.before or 'details unavailable'})"
    if entry.change_type == "title_change":
        return f"~ {entry.alias}: title {entry.before or 'none'} -> {entry.after or 'none'}"
    if entry.change_type == "team_move":
        return f"~ {entry.alias}: teams {entry.before or 'none'} -> {entry.after or 'none'}"
    return f"~ {entry.alias}: {entry.before or 'none'} -> {entry.after or 'none'}"


def parse_since_week(value: str) -> date:
    normalized = value.strip()
    match = _WEEK_SPEC_RE.fullmatch(normalized)
    if match is None:
        raise ValueError("Expected --since in YYYY-Www format, for example 2026-W15.")
    year = int(match.group("year"))
    week = int(match.group("week"))
    return date.fromisocalendar(year, week, 1)


def _read_commit_history(
    *,
    relative_path: Path,
    since_date: date,
    repo_root: Path,
    git_runner: GitRunner,
) -> tuple[tuple[str, datetime], ...]:
    result = git_runner(
        [
            "git",
            "log",
            "--reverse",
            "--format=%H%x09%cI",
            f"--since={since_date.isoformat()}",
            "--",
            relative_path.as_posix(),
        ],
        repo_root,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "git log failed")
    commits: list[tuple[str, datetime]] = []
    for line in result.stdout.splitlines():
        sha, separator, timestamp = line.partition("\t")
        if not separator:
            continue
        committed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
        commits.append((sha.strip(), committed_at))
    return tuple(commits)


def _load_people_directory_from_revision(
    *,
    revision: str,
    relative_path: Path,
    repo_root: Path,
    git_runner: GitRunner,
) -> dict[str, PersonDirectorySnapshot]:
    result = git_runner(
        ["git", "show", f"{revision}:{relative_path.as_posix()}"],
        repo_root,
    )
    if result.returncode != 0:
        return {}
    payload = yaml.safe_load(result.stdout) or {}
    people = payload.get("people", []) if isinstance(payload, dict) else []
    snapshots: dict[str, PersonDirectorySnapshot] = {}
    for raw_person in people:
        if not isinstance(raw_person, dict):
            continue
        alias = str(raw_person.get("alias", "")).strip().lower()
        if not alias:
            continue
        snapshots[alias] = PersonDirectorySnapshot(
            alias=alias,
            display_name=_optional_text(raw_person.get("display_name") or raw_person.get("name")),
            title=_optional_text(raw_person.get("title")),
            email=_optional_text(raw_person.get("email")),
            team_ids=tuple(sorted(str(entry).strip() for entry in raw_person.get("team_ids", []) if str(entry).strip())),
        )
    return snapshots


def _diff_people_directory(
    *,
    commit_sha: str,
    committed_at: datetime,
    previous_people: dict[str, PersonDirectorySnapshot],
    current_people: dict[str, PersonDirectorySnapshot],
) -> tuple[KBChangelogEntry, ...]:
    entries: list[KBChangelogEntry] = []
    all_aliases = sorted(set(previous_people) | set(current_people))
    for alias in all_aliases:
        previous = previous_people.get(alias)
        current = current_people.get(alias)
        if previous is None and current is not None:
            entries.append(
                KBChangelogEntry(
                    commit_sha=commit_sha,
                    committed_at=committed_at,
                    alias=alias,
                    change_type="new_hire",
                    before=None,
                    after=_person_summary(current),
                )
            )
            continue
        if previous is not None and current is None:
            entries.append(
                KBChangelogEntry(
                    commit_sha=commit_sha,
                    committed_at=committed_at,
                    alias=alias,
                    change_type="departure",
                    before=_person_summary(previous),
                    after=None,
                )
            )
            continue
        if previous is None or current is None:
            continue
        if previous.title != current.title:
            entries.append(
                KBChangelogEntry(
                    commit_sha=commit_sha,
                    committed_at=committed_at,
                    alias=alias,
                    change_type="title_change",
                    before=previous.title,
                    after=current.title,
                )
            )
        if previous.team_ids != current.team_ids:
            entries.append(
                KBChangelogEntry(
                    commit_sha=commit_sha,
                    committed_at=committed_at,
                    alias=alias,
                    change_type="team_move",
                    before=", ".join(previous.team_ids) or None,
                    after=", ".join(current.team_ids) or None,
                )
            )
    return tuple(entries)


def _person_summary(person: PersonDirectorySnapshot) -> str:
    title = person.title or "title unknown"
    teams = ", ".join(person.team_ids) if person.team_ids else "no teams"
    return f"{title}; teams: {teams}"


def _optional_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())
    return text or None


def _run_git(command: list[str], repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )
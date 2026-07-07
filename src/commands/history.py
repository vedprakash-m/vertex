from __future__ import annotations

import csv
import difflib
import json
from dataclasses import asdict
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import typer

from src.core.archive_store import read_archive_index
from src.core.models import ArchiveEntry, RunManifest, Snapshot
from src.core.semantic_index import SemanticMatch, search_history_semantic_index
from src.core.snapshot_store import ARCHIVE_ROOT, read_snapshot


@dataclass(frozen=True, slots=True)
class HistoryListRow:
    issue_number: int
    generated_at: str
    kind: str
    edition_type: str
    freshness_summary: str
    qg_status: str
    note: str


@dataclass(frozen=True, slots=True)
class HistorySearchMatch:
    issue_number: int
    line_number: int
    line: str


@dataclass(frozen=True, slots=True)
class HistorySemanticMatch:
    issue_number: int | None
    reference: str
    generated_at: str
    source_type: str
    risk_level: str
    score: float
    excerpt: str


def history_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    last: int | None = typer.Option(None, "--last", help="Show only the most recent N archived issues."),
    issue: int | None = typer.Option(None, "--issue", help="Show the archived Markdown for a specific issue."),
    diff: tuple[int, int] | None = typer.Option(None, "--diff", help="Show a Markdown diff for two archived issues."),
    search: str | None = typer.Option(None, "--search", help="Search archived Markdown for a keyword."),
    semantic: str | None = typer.Option(None, "--semantic", help="Search archived confirmed narratives and incident learnings using the local semantic index."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    requested_modes = sum(value is not None for value in (issue, diff, search, semantic))
    if requested_modes > 1:
        raise typer.BadParameter("Use only one of --issue, --diff, --search, or --semantic at a time.")

    archive_index = read_archive_index(edition, archive_root=ARCHIVE_ROOT)
    if not archive_index.issues:
        typer.echo(f"No archived issues found for {edition}.")
        raise typer.Exit(code=1)

    if issue is not None:
        typer.echo(show_issue_history(edition_name=edition, issue_number=issue, archive_root=ARCHIVE_ROOT, format=format), nl=False)
        raise typer.Exit(code=0)

    if diff is not None:
        typer.echo(
            diff_issue_history(
                edition_name=edition,
                older_issue_number=diff[0],
                newer_issue_number=diff[1],
                archive_root=ARCHIVE_ROOT,
                format=format,
            ),
            nl=False,
        )
        raise typer.Exit(code=0)

    if search is not None:
        matches = search_issue_history(edition_name=edition, keyword=search, archive_root=ARCHIVE_ROOT)
        if not matches:
            typer.echo(f'No archived matches found for "{search}" in {edition}.')
            raise typer.Exit(code=1)
        typer.echo(render_history_search(matches, edition_name=edition, keyword=search, format=format), nl=False)
        raise typer.Exit(code=0)

    if semantic is not None:
        semantic_matches = search_issue_history_semantic(edition_name=edition, query=semantic, archive_root=ARCHIVE_ROOT)
        if not semantic_matches:
            typer.echo(f'No semantic history matches found for "{semantic}" in {edition}.')
            raise typer.Exit(code=1)
        typer.echo(render_history_semantic_search(semantic_matches, edition_name=edition, query=semantic, format=format), nl=False)
        raise typer.Exit(code=0)

    rows = list_issue_history(edition_name=edition, last=last, archive_root=ARCHIVE_ROOT)
    typer.echo(render_history_list(rows, format=format), nl=False)
    raise typer.Exit(code=0)


def list_issue_history(
    edition_name: str,
    last: int | None = None,
    archive_root: Path = ARCHIVE_ROOT,
) -> tuple[HistoryListRow, ...]:
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    ordered_entries = sorted(archive_index.issues, key=lambda entry: entry.issue_number, reverse=True)
    if last is not None:
        ordered_entries = ordered_entries[:last]

    rows: list[HistoryListRow] = []
    for entry in ordered_entries:
        manifest = _load_manifest(entry)
        snapshot = _load_snapshot(entry)
        rows.append(
            HistoryListRow(
                issue_number=entry.issue_number,
                generated_at=entry.generated_at.date().isoformat(),
                kind=entry.kind,
                edition_type=(snapshot.edition_type.value if snapshot is not None else "-"),
                freshness_summary=_format_freshness_summary(manifest),
                qg_status=_format_qg_status(manifest),
                note=(entry.reason or "-"),
            )
        )
    return tuple(rows)


def show_issue_history(
    edition_name: str,
    issue_number: int,
    archive_root: Path = ARCHIVE_ROOT,
    format: str = "human",
) -> str:
    entry = _require_archive_entry(edition_name, issue_number, archive_root=archive_root)
    if entry.kind != "confirmed" or not entry.md_path:
        reason = entry.reason or "No archived Markdown is available for this issue."
        raise typer.BadParameter(f"Issue {issue_number:03d} is not a confirmed archived issue. {reason}")

    snapshot = _load_snapshot(entry)
    markdown_path = Path(entry.md_path)
    if not markdown_path.exists():
        raise typer.BadParameter(f"Archived Markdown is missing for Issue {issue_number:03d}: {markdown_path}")

    payload = {
        "issue_number": issue_number,
        "generated_at": entry.generated_at.date().isoformat(),
        "edition_type": snapshot.edition_type.value if snapshot is not None else "-",
        "markdown_path": str(markdown_path),
        "markdown_body": markdown_path.read_text(encoding="utf-8"),
    }

    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        return _render_csv(
            rows=(payload,),
            columns=("issue_number", "generated_at", "edition_type", "markdown_path", "markdown_body"),
        )
    if format != "human":
        raise typer.BadParameter("Unsupported format. Use human, json, or csv.")

    header_lines = [
        f"Issue {issue_number:03d}",
        f"Date: {payload['generated_at']}",
        f"Edition type: {payload['edition_type']}",
        f"Markdown: {markdown_path}",
        "",
    ]
    return "\n".join(header_lines) + str(payload["markdown_body"])


def diff_issue_history(
    edition_name: str,
    older_issue_number: int,
    newer_issue_number: int,
    archive_root: Path = ARCHIVE_ROOT,
    format: str = "human",
) -> str:
    older_entry = _require_archive_entry(edition_name, older_issue_number, archive_root=archive_root)
    newer_entry = _require_archive_entry(edition_name, newer_issue_number, archive_root=archive_root)
    older_lines = _load_archive_markdown_lines(older_entry)
    newer_lines = _load_archive_markdown_lines(newer_entry)

    diff_lines = list(
        difflib.unified_diff(
            older_lines,
            newer_lines,
            fromfile=f"issue_{older_issue_number:03d}.md",
            tofile=f"issue_{newer_issue_number:03d}.md",
            lineterm="",
        )
    )
    payload = {
        "older_issue_number": older_issue_number,
        "newer_issue_number": newer_issue_number,
        "identical": not diff_lines,
        "diff": "\n".join(diff_lines) + ("\n" if diff_lines else ""),
    }

    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        return _render_csv(rows=(payload,), columns=("older_issue_number", "newer_issue_number", "identical", "diff"))
    if format != "human":
        raise typer.BadParameter("Unsupported format. Use human, json, or csv.")

    if not diff_lines:
        return f"Issue {older_issue_number:03d} and Issue {newer_issue_number:03d} have identical archived Markdown.\n"
    return str(payload["diff"])


def search_issue_history(
    edition_name: str,
    keyword: str,
    archive_root: Path = ARCHIVE_ROOT,
) -> tuple[HistorySearchMatch, ...]:
    needle = keyword.casefold()
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    matches: list[HistorySearchMatch] = []
    for entry in sorted(archive_index.issues, key=lambda issue: issue.issue_number):
        if entry.kind != "confirmed" or not entry.md_path:
            continue
        markdown_path = Path(entry.md_path)
        if not markdown_path.exists():
            continue
        for line_number, line in enumerate(markdown_path.read_text(encoding="utf-8").splitlines(), start=1):
            if needle in line.casefold():
                matches.append(HistorySearchMatch(issue_number=entry.issue_number, line_number=line_number, line=line))
    return tuple(matches)


def search_issue_history_semantic(
    edition_name: str,
    query: str,
    archive_root: Path = ARCHIVE_ROOT,
) -> tuple[HistorySemanticMatch, ...]:
    matches = search_history_semantic_index(edition_name, query, archive_root=archive_root)
    return tuple(
        HistorySemanticMatch(
            issue_number=match.issue_number,
            reference=_history_reference(match),
            generated_at=match.generated_at.date().isoformat(),
            source_type=match.source_type,
            risk_level="-" if match.risk_level is None else match.risk_level.value,
            score=round(match.score, 4),
            excerpt=match.excerpt,
        )
        for match in matches
    )


def render_history_list(rows: tuple[HistoryListRow, ...], *, format: str) -> str:
    if format == "json":
        return json.dumps([asdict(row) for row in rows], indent=2, sort_keys=True) + "\n"
    if format == "csv":
        return _render_csv(
            rows=tuple(asdict(row) for row in rows),
            columns=("issue_number", "generated_at", "kind", "edition_type", "freshness_summary", "qg_status", "note"),
        )
    if format != "human":
        raise typer.BadParameter("Unsupported format. Use human, json, or csv.")
    return "".join(
        f"{row.issue_number:03d}\t{row.generated_at}\t{row.kind}\t{row.edition_type}\t{row.freshness_summary}\t{row.qg_status}\t{row.note}\n"
        for row in rows
    )


def render_history_search(
    matches: tuple[HistorySearchMatch, ...],
    *,
    edition_name: str,
    keyword: str,
    format: str,
) -> str:
    if format == "json":
        return json.dumps(
            {
                "edition": edition_name,
                "keyword": keyword,
                "matches": [asdict(match) for match in matches],
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
    if format == "csv":
        return _render_csv(
            rows=tuple(asdict(match) for match in matches),
            columns=("issue_number", "line_number", "line"),
        )
    if format != "human":
        raise typer.BadParameter("Unsupported format. Use human, json, or csv.")
    return "".join(f"Issue {match.issue_number:03d}\tL{match.line_number}\t{match.line}\n" for match in matches)


def render_history_semantic_search(
    matches: tuple[HistorySemanticMatch, ...],
    *,
    edition_name: str,
    query: str,
    format: str,
) -> str:
    if format == "json":
        return json.dumps(
            {
                "edition": edition_name,
                "query": query,
                "matches": [asdict(match) for match in matches],
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
    if format == "csv":
        return _render_csv(
            rows=tuple(asdict(match) for match in matches),
            columns=("issue_number", "reference", "generated_at", "source_type", "risk_level", "score", "excerpt"),
        )
    if format != "human":
        raise typer.BadParameter("Unsupported format. Use human, json, or csv.")
    return "".join(
        f"{match.reference}\t{match.generated_at}\t{match.risk_level}\t{match.excerpt}\n"
        for match in matches
    )


def _history_reference(match: SemanticMatch) -> str:
    if match.source_ref:
        return match.source_ref
    if match.issue_number is not None:
        return f"Issue {match.issue_number:03d}"
    return match.source_type


def _render_csv(*, rows: tuple[dict[str, object], ...], columns: tuple[str, ...]) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row.get(column, "") for column in columns])
    return buffer.getvalue()


def _require_archive_entry(
    edition_name: str,
    issue_number: int,
    archive_root: Path,
) -> ArchiveEntry:
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    for entry in archive_index.issues:
        if entry.issue_number == issue_number:
            return entry
    raise typer.BadParameter(f"Issue {issue_number:03d} was not found in the archive index for {edition_name}.")


def _load_manifest(entry: ArchiveEntry) -> RunManifest | None:
    if not entry.manifest_path:
        return None
    manifest_path = Path(entry.manifest_path)
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and "schema_version" in payload:
        payload = {key: value for key, value in payload.items() if key != "schema_version"}
    if not isinstance(payload, dict):
        return None
    try:
        return RunManifest(
            manifest_id=str(payload["manifest_id"]),
            issue_number=int(payload["issue_number"]),
            edition=str(payload["edition"]),
            started_at=__import__("datetime", fromlist=["datetime"]).datetime.fromisoformat(str(payload["started_at"])),
            ended_at=__import__("datetime", fromlist=["datetime"]).datetime.fromisoformat(str(payload["ended_at"])),
            config_hash=str(payload["config_hash"]),
            snapshot_hash=str(payload["snapshot_hash"]),
            html_hash=str(payload["html_hash"]),
            md_hash=str(payload["md_hash"]),
            ado_calls=int(payload["ado_calls"]),
            ai_calls=int(payload["ai_calls"]),
            ai_cost_usd=float(payload["ai_cost_usd"]),
            freshness_summary={str(key): int(value) for key, value in dict(payload.get("freshness_summary", {})).items()},
            qg_results={str(key): bool(value) for key, value in dict(payload.get("qg_results", {})).items()},
            git_sha=(None if payload.get("git_sha") in (None, "") else str(payload.get("git_sha"))),
            ai_cost_by_model={str(key): float(value) for key, value in dict(payload.get("ai_cost_by_model", {})).items()},
        )
    except (KeyError, TypeError, ValueError):
        return None


def _load_snapshot(entry: ArchiveEntry) -> Snapshot | None:
    if not entry.snapshot_path:
        return None
    snapshot_path = Path(entry.snapshot_path)
    if not snapshot_path.exists():
        return None
    return read_snapshot(snapshot_path)


def _load_archive_markdown_lines(entry: ArchiveEntry) -> list[str]:
    if entry.kind != "confirmed" or not entry.md_path:
        raise typer.BadParameter(f"Issue {entry.issue_number:03d} does not have archived Markdown content.")
    markdown_path = Path(entry.md_path)
    if not markdown_path.exists():
        raise typer.BadParameter(f"Archived Markdown is missing for Issue {entry.issue_number:03d}: {markdown_path}")
    return markdown_path.read_text(encoding="utf-8").splitlines()


def _format_freshness_summary(manifest: RunManifest | None) -> str:
    if manifest is None:
        return "-"
    summary = manifest.freshness_summary
    return f"b{summary.get('blocks', 0)}/w{summary.get('warns', 0)}/i{summary.get('infos', 0)}"


def _format_qg_status(manifest: RunManifest | None) -> str:
    if manifest is None or not manifest.qg_results:
        return "-"
    failures = sorted(gate for gate, passed in manifest.qg_results.items() if not passed)
    if not failures:
        return "pass"
    return "fail:" + ",".join(failures)
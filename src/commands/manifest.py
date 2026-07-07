from __future__ import annotations

import csv
import json
from io import StringIO
from pathlib import Path

import typer

from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT
from src.core.manifest_writer import get_manifest_path
from src.core.models import ArchiveEntry, RunManifest, Snapshot
from src.core.snapshot_store import ARCHIVE_ROOT, read_snapshot


def manifest_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number. Defaults to the latest draft or confirmed issue."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    typer.echo(
        show_manifest(
            edition_name=edition,
            issue_number=issue,
            archive_root=ARCHIVE_ROOT,
            programs_root=PROGRAMS_ROOT,
            format=format,
        ),
        nl=False,
    )
    raise typer.Exit(code=0)


def show_manifest(
    *,
    edition_name: str,
    issue_number: int | None,
    archive_root: Path = ARCHIVE_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
    format: str = "human",
) -> str:
    resolved_issue = issue_number or _resolve_default_issue_number(edition_name, programs_root=programs_root, archive_root=archive_root)
    manifest, manifest_source, snapshot = _load_manifest_context(
        edition_name=edition_name,
        issue_number=resolved_issue,
        programs_root=programs_root,
        archive_root=archive_root,
    )
    payload = _build_manifest_payload(manifest=manifest, manifest_source=manifest_source, snapshot=snapshot)

    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        return render_manifest_csv(payload)
    if format != "human":
        raise typer.BadParameter("Unsupported format. Use human, json, or csv.")

    lines = [
        f"Manifest:       {manifest.manifest_id}",
        f"Issue:          #{manifest.issue_number} ({manifest.edition})",
        f"Data pulled:    {payload['data_pulled']}",
        f"Confirmed by:   {payload['confirmed_by']}",
        f"Confirmed at:   {payload['confirmed_at']}",
        f"Source:         {manifest_source}",
    ]
    ai_safety = payload["ai_safety"]
    if ai_safety == "malformed":
        lines.append("AI Safety:      malformed metadata")
    if isinstance(ai_safety, dict) and ai_safety:
        lines.append(
            "AI Budget:      "
            f"${float(ai_safety['spent_usd']):.6f} / ${float(ai_safety['budget_usd']):.6f} "
            f"({'within budget' if bool(ai_safety['within_budget']) else 'budget exceeded'})"
        )
        lines.append(f"AI Trace Run:   {ai_safety.get('trace_run_id') or '-'}")

    qg_failures = sorted(gate_id for gate_id, passed in manifest.qg_results.items() if not passed)
    lines.append(f"Quality Gates:  {'PASS' if not qg_failures else 'FAIL'}")
    if manifest.qg_results:
        for gate_id in sorted(manifest.qg_results):
            status = "PASS" if manifest.qg_results[gate_id] else "FAIL"
            lines.append(f"  {gate_id}: {status}")
    else:
        lines.append("  -")
    return "\n".join(lines) + "\n"


def _build_manifest_payload(
    *,
    manifest: RunManifest,
    manifest_source: str,
    snapshot: Snapshot | None,
) -> dict[str, object]:
    qg_failures = sorted(gate_id for gate_id, passed in manifest.qg_results.items() if not passed)
    return {
        "manifest_id": manifest.manifest_id,
        "issue_number": manifest.issue_number,
        "edition": manifest.edition,
        "data_pulled": snapshot.ado_data_as_of.isoformat() if snapshot is not None else "-",
        "confirmed_by": _optional_string(manifest.metadata.get("confirmed_by")) or "-",
        "confirmed_at": _optional_string(manifest.metadata.get("confirmed_at")) or "-",
        "source": manifest_source,
        "ai_safety": _build_ai_safety_payload(manifest),
        "quality_gates_overall": "PASS" if not qg_failures else "FAIL",
        "qg_results": dict(sorted(manifest.qg_results.items())),
    }


def render_manifest_csv(payload: dict[str, object]) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "manifest_id",
            "issue_number",
            "edition",
            "data_pulled",
            "confirmed_by",
            "confirmed_at",
            "source",
            "ai_safety",
            "quality_gates_overall",
            "qg_results",
        ]
    )
    writer.writerow(
        [
            payload["manifest_id"],
            payload["issue_number"],
            payload["edition"],
            payload["data_pulled"],
            payload["confirmed_by"],
            payload["confirmed_at"],
            payload["source"],
            json.dumps(payload["ai_safety"], sort_keys=True),
            payload["quality_gates_overall"],
            json.dumps(payload["qg_results"], sort_keys=True),
        ]
    )
    return buffer.getvalue()


def _build_ai_safety_payload(manifest: RunManifest) -> dict[str, object] | str:
    payload = manifest.metadata.get("ai_safety")
    if isinstance(payload, dict):
        return dict(payload)
    if payload is not None:
        return "malformed"
    if manifest.metadata.get("__malformed_metadata__"):
        return "malformed"
    return {}


def _resolve_default_issue_number(edition_name: str, *, programs_root: Path = PROGRAMS_ROOT, archive_root: Path) -> int:
    output_dir = get_program_output_dir(edition_name, programs_root=programs_root)
    manifest_paths = sorted(output_dir.glob("issue_*/issue_*.manifest.json"))
    if manifest_paths:
        return max(int(path.stem.split("_")[1].split(".")[0]) for path in manifest_paths)
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    latest = find_latest_confirmed_entry(archive_index)
    if latest is None:
        raise typer.BadParameter(f"No manifest found for {edition_name}. Run `vertex report --dry-run --edition {edition_name}` or confirm an issue first.")
    return latest.issue_number


def _load_manifest_context(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
    archive_root: Path,
) -> tuple[RunManifest, str, Snapshot | None]:
    archive_entry = _find_archive_entry(edition_name, issue_number, archive_root=archive_root)
    if archive_entry is not None and archive_entry.kind == "confirmed" and archive_entry.manifest_path:
        manifest_path = Path(archive_entry.manifest_path)
        if manifest_path.exists():
            snapshot = _load_output_snapshot(Path(archive_entry.snapshot_path)) if archive_entry.snapshot_path else None
            return _read_manifest(manifest_path), "confirmed archive", snapshot

    draft_manifest_path = get_manifest_path(edition_name, issue_number, programs_root=programs_root)
    if draft_manifest_path.exists():
        snapshot = _load_output_snapshot(get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.snapshot.json")
        return _read_manifest(draft_manifest_path), "draft output", snapshot
    if archive_entry is None or not archive_entry.manifest_path:
        raise typer.BadParameter(
            f"Manifest for Issue {issue_number:03d} was not found in output or archive for {edition_name}."
        )
    manifest_path = Path(archive_entry.manifest_path)
    if not manifest_path.exists():
        raise typer.BadParameter(f"Archived manifest is missing for Issue {issue_number:03d}: {manifest_path}")
    snapshot = _load_output_snapshot(Path(archive_entry.snapshot_path)) if archive_entry.snapshot_path else None
    return _read_manifest(manifest_path), "confirmed archive", snapshot


def _find_archive_entry(edition_name: str, issue_number: int, *, archive_root: Path) -> ArchiveEntry | None:
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    for entry in archive_index.issues:
        if entry.issue_number == issue_number:
            return entry
    return None


def _read_manifest(path: Path) -> RunManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "schema_version" in payload:
            payload = {key: value for key, value in payload.items() if key != "schema_version"}
        metadata = _manifest_mapping(payload.get("metadata"))
        if not isinstance(payload.get("metadata"), dict):
            metadata["__malformed_metadata__"] = True
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
            freshness_summary={key: int(value) for key, value in _manifest_mapping(payload.get("freshness_summary")).items()},  # type: ignore[call-overload]
            qg_results={key: bool(value) for key, value in _manifest_mapping(payload.get("qg_results")).items()},
            git_sha=(None if payload.get("git_sha") in (None, "") else str(payload.get("git_sha"))),
            ai_cost_by_model={key: float(value) for key, value in _manifest_mapping(payload.get("ai_cost_by_model")).items()},  # type: ignore[arg-type]
            notes=tuple(str(note) for note in payload.get("notes", [])),
            metadata=metadata,
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise typer.BadParameter(f"Manifest at {path} is invalid.") from error


def _manifest_mapping(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _load_output_snapshot(path: Path | None) -> Snapshot | None:
    if path is None or not path.exists():
        return None
    return read_snapshot(path)


def _optional_string(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sqlite3

import typer
import yaml

from src.core.analytics_store import AnalyticsRebuildArtifacts, rebuild_program_analytics
from src.core.edition_resolver import PROGRAMS_ROOT, load_program
from src.core.journal import (
    append_review_decision,
    append_signal,
    append_signal_thread_link,
    append_usage_marker,
    read_review_log,
    read_signal_thread_log,
    read_signals,
)
from src.core.models_v2 import SignalReviewDecision, SignalUsageMarker
from src.core.signal_classification import classify_signal as _classify_signal
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore, get_program_sqlite_store_path, _connect_program_db
from src.core.trajectory import backfill_trajectory_points, get_program_trajectory_dir, read_trajectory
from src.core.migration_log import _MIGRATION_LOG_FILENAME, append_migration_log
from src.core.charts.deployment_velocity import LEGACY_ALIAS_IDS, CANONICAL_RENDERER_ID as _CANONICAL_CHART_ID


@dataclass(frozen=True, slots=True)
class MigrationArtifacts:
    program_id: str
    target_backend: str
    signal_count: int
    review_count: int
    thread_link_count: int
    trajectory_point_count: int
    usage_marker_count: int
    database_path: Path
    config_updated: bool
    dry_run: bool


def migrate_command(
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    to: str | None = typer.Option(None, "--to", help="Target storage backend. Currently only sqlite is supported."),
    rebuild_analytics: bool = typer.Option(False, "--rebuild-analytics", help="Rebuild the per-program analytics projection database from archive and journal primaries."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview migrated counts without writing SQLite data or updating program.yaml."),
) -> None:
    if rebuild_analytics:
        if to is not None:
            raise typer.BadParameter("--to cannot be combined with --rebuild-analytics.")
        if program is None:
            raise typer.BadParameter("--program is required with --rebuild-analytics.")
        artifacts = run_rebuild_analytics(
            program_id=program,
            programs_root=PROGRAMS_ROOT,
        )
        typer.echo(
            f"Rebuilt analytics for {artifacts.program_id} | risks: {artifacts.confirmed_risks} | "
            f"claims: {artifacts.confirmed_claims} | decisions: {artifacts.confirmed_decisions} | "
            f"vitality: {artifacts.confirmed_vitality} | actions: {artifacts.confirmed_actions} | "
            f"autonomy: {artifacts.autonomy_audit}"
        )
        typer.echo(
            "Decision rebuild tiers: "
            f"program_facts={artifacts.program_fact_decisions} | "
            f"context_snapshot={artifacts.context_snapshot_decisions} | "
            f"raw_fallback={artifacts.raw_decision_fallbacks} | "
            f"low_fidelity={artifacts.low_fidelity_decisions}"
        )
        typer.echo(f"Analytics store: {artifacts.database_path}")
        raise typer.Exit(code=0)

    if program is None:
        raise typer.BadParameter("--program is required.")
    if to is None:
        raise typer.BadParameter("--to is required unless --rebuild-analytics is used.")

    migration_artifacts = run_storage_migration(
        program_id=program,
        target_backend=to,
        programs_root=PROGRAMS_ROOT,
        dry_run=dry_run,
    )
    typer.echo(
        f"Migrated {migration_artifacts.program_id} to {migration_artifacts.target_backend} | signals: {migration_artifacts.signal_count} | "
        f"reviews: {migration_artifacts.review_count} | threads: {migration_artifacts.thread_link_count} | "
        f"trajectory points: {migration_artifacts.trajectory_point_count} | usage markers: {migration_artifacts.usage_marker_count}"
    )
    if migration_artifacts.dry_run:
        typer.echo("Dry-run: sqlite database and program.yaml were not updated.")
    else:
        typer.echo(f"SQLite store: {migration_artifacts.database_path}")
        typer.echo("Program storage_backend updated to sqlite.")
    raise typer.Exit(code=0)


def run_rebuild_analytics(
    *,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> AnalyticsRebuildArtifacts:
    program = load_program(program_id, programs_root=programs_root)
    if program is None:
        raise typer.BadParameter(f"Program '{program_id}' was not found.")
    return rebuild_program_analytics(program_id=program_id, programs_root=programs_root)


def run_storage_migration(
    *,
    program_id: str,
    target_backend: str,
    programs_root: Path = PROGRAMS_ROOT,
    dry_run: bool = False,
) -> MigrationArtifacts:
    normalized_backend = target_backend.strip().lower()
    if normalized_backend == "file":
        return _run_to_file_migration(
            program_id=program_id,
            programs_root=programs_root,
            dry_run=dry_run,
        )
    if normalized_backend != "sqlite":
        raise typer.BadParameter(f"Unsupported migration target '{target_backend}'. Only 'sqlite' and 'file' are supported.")

    program = load_program(program_id, programs_root=programs_root)
    if program is None:
        raise typer.BadParameter(f"Program '{program_id}' was not found.")

    signals = read_signals(program_id, programs_root=programs_root)
    review_log = read_review_log(program_id, programs_root=programs_root)
    review_decisions = tuple(entry for entry in review_log if isinstance(entry, SignalReviewDecision))
    usage_markers = tuple(entry for entry in review_log if isinstance(entry, SignalUsageMarker))
    thread_links = read_signal_thread_log(program_id, programs_root=programs_root)
    trajectory_points_by_item = _load_file_trajectory_points(program_id, programs_root=programs_root)
    trajectory_point_count = sum(len(points) for points in trajectory_points_by_item.values())
    database_path = get_program_sqlite_store_path(program_id, programs_root=programs_root)

    if not dry_run:
        _write_sqlite_store(
            program_id=program_id,
            programs_root=programs_root,
            signals=signals,
            review_decisions=review_decisions,
            usage_markers=usage_markers,
            thread_links=thread_links,
            trajectory_points_by_item=trajectory_points_by_item,
        )
        _update_program_storage_backend(program_id, programs_root=programs_root, storage_backend="sqlite")

    # WS-11/WS-16: scan for legacy chart-renderer aliases (e.g.
    # `acme::deployment_velocity`) and rewrite to the canonical id. This
    # runs on every forward+rollback migration and is a no-op if no
    # alias is present. Each rewrite appends a row to migration_log.jsonl.
    files_touched, alias_rewrites = _rewrite_chart_aliases(
        program_id=program_id,
        programs_root=programs_root,
        dry_run=dry_run,
    )

    return MigrationArtifacts(
        program_id=program_id,
        target_backend=normalized_backend,
        signal_count=len(signals),
        review_count=len(review_decisions),
        thread_link_count=len(thread_links),
        trajectory_point_count=trajectory_point_count,
        usage_marker_count=len(usage_markers),
        database_path=database_path,
        config_updated=not dry_run,
        dry_run=dry_run,
    )


def _rewrite_chart_aliases(
    *,
    program_id: str,
    programs_root: Path,
    dry_run: bool,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """WS-11/WS-16: scan the program's tracked YAML files for legacy
    chart-renderer aliases (e.g. `acme::deployment_velocity`) and rewrite
    them to the canonical id (`core::deployment_velocity`). Returns:

        files_touched:  every file that contained at least one alias
        alias_rewrites: (legacy_id, new_id) pairs that were rewritten

    In dry-run mode, no files are written and no migration_log row is
    appended. Otherwise each rewrite appends a `chart_id_alias` row to
    `programs/<id>/migration_log.jsonl` (guarded by portalocker+fsync).
    """
    if not LEGACY_ALIAS_IDS:
        return ((), ())
    program_dir = programs_root / program_id
    if not program_dir.exists():
        return ((), ())
    yaml_files = sorted(
        path
        for path in program_dir.rglob("*.yaml")
        if not _is_runtime_yaml(path)
    )
    rewrites: list[tuple[str, str]] = []
    files_touched: list[str] = []
    for path in yaml_files:
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not any(alias in raw for alias in LEGACY_ALIAS_IDS):
            continue
        new_raw = raw
        for alias in LEGACY_ALIAS_IDS:
            if alias in new_raw:
                new_raw = new_raw.replace(alias, _CANONICAL_CHART_ID)
                rewrites.append((alias, _CANONICAL_CHART_ID))
        if new_raw != raw and not dry_run:
            path.write_text(new_raw, encoding="utf-8")
            files_touched.append(str(path.relative_to(program_dir)))
    if not dry_run and rewrites:
        # One migration_log row covering all rewrites in this program
        append_migration_log(
            program_id=program_id,
            kind="chart_id_alias",
            source_id=",".join(sorted({src for src, _ in rewrites})),
            target_id=",".join(sorted({dst for _, dst in rewrites})),
            files_touched=tuple(files_touched),
            dry_run=False,
            programs_root=programs_root,
        )
    return (tuple(files_touched), tuple(rewrites))


def _is_runtime_yaml(path: Path) -> bool:
    """Skip sidecars / migration log / fact-store files in the rewrite
    scan. We only rewrite the *tracked* config YAMLs (program.yaml,
    workstreams.yaml, edition.yaml, knowledge/*, etc.)."""
    name = path.name
    if name == _MIGRATION_LOG_FILENAME or name == "fact_sor_state.yaml" or name.endswith(".sig.json"):
        return True
    if path.parent.name in {"archive", "snapshots", "manifests", "vertex_store.sqlite3.d"}:
        return True
    return False


def _load_file_trajectory_points(program_id: str, *, programs_root: Path) -> dict[int, tuple]:
    trajectory_points_by_item: dict[int, tuple] = {}
    trajectory_dir = get_program_trajectory_dir(program_id, programs_root=programs_root)
    if not trajectory_dir.exists():
        return trajectory_points_by_item
    for path in sorted(trajectory_dir.glob("*.jsonl"), key=lambda entry: entry.name.lower()):
        try:
            work_item_id = int(path.stem)
        except ValueError:
            continue
        points = read_trajectory(program_id, work_item_id, programs_root=programs_root)
        if points:
            trajectory_points_by_item[work_item_id] = points
    return trajectory_points_by_item


def _write_sqlite_store(
    *,
    program_id: str,
    programs_root: Path,
    signals,
    review_decisions,
    usage_markers,
    thread_links,
    trajectory_points_by_item,
) -> None:
    target_path = get_program_sqlite_store_path(program_id, programs_root=programs_root)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix=f"vertex-sqlite-migrate-{program_id}-", dir=str(programs_root.parent)) as temp_dir:
        temp_programs_root = Path(temp_dir)
        (temp_programs_root / program_id).mkdir(parents=True, exist_ok=True)
        signal_store = SQLiteSignalStore(programs_root=temp_programs_root)
        trajectory_store = SQLiteTrajectoryStore(programs_root=temp_programs_root)

        for signal in signals:
            signal_store.append(_classify_signal(signal))
        for decision in review_decisions:
            signal_store.append_review(program_id, decision)
        for marker in usage_markers:
            signal_store.append_usage_marker(program_id, marker)
        for link in thread_links:
            signal_store.append_thread(program_id, link)
        for work_item_id, points in trajectory_points_by_item.items():
            for point in points:
                trajectory_store.append(program_id, work_item_id, point)

        temp_path = get_program_sqlite_store_path(program_id, programs_root=temp_programs_root)
        # Empty programs never call append*, so the db file may not have been created yet.
        # Force schema creation so os.replace can always succeed.
        if not temp_path.exists():
            with _connect_program_db(program_id, programs_root=temp_programs_root):
                pass
        if target_path.exists():
            backup_path = target_path.with_suffix(target_path.suffix + ".bak")
            if backup_path.exists():
                backup_path.unlink()
            os.replace(target_path, backup_path)
        os.replace(temp_path, target_path)


def _update_program_storage_backend(program_id: str, *, programs_root: Path, storage_backend: str) -> None:
    program_path = programs_root / program_id / "program.yaml"
    document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise typer.BadParameter(f"Program '{program_id}' has an invalid program.yaml document.")
    document["storage_backend"] = storage_backend
    program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Reverse migration: SQLite → file journal (S1A.6)
# ---------------------------------------------------------------------------


def _validate_sqlite_integrity(db_path: Path) -> None:
    """Raise typer.BadParameter if the DB is corrupt or unreadable."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        raise typer.BadParameter(f"Cannot open SQLite DB: {exc}")
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        result = str(rows[0][0]).lower() if rows else ""
        if result != "ok":
            messages = "; ".join(str(row[0]) for row in rows)
            raise typer.BadParameter(f"SQLite integrity_check failed: {messages}")
    finally:
        conn.close()


def _run_to_file_migration(
    *,
    program_id: str,
    programs_root: Path,
    dry_run: bool,
) -> MigrationArtifacts:
    program = load_program(program_id, programs_root=programs_root)
    if program is None:
        raise typer.BadParameter(f"Program '{program_id}' was not found.")

    db_path = get_program_sqlite_store_path(program_id, programs_root=programs_root)
    if not db_path.exists():
        raise typer.BadParameter(f"SQLite DB not found: {db_path}. Nothing to migrate.")

    _validate_sqlite_integrity(db_path)

    signal_store = SQLiteSignalStore(programs_root=programs_root)
    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)

    signals = signal_store.read(program_id)
    reviews = signal_store.read_reviews(program_id)  # dict[str, SignalReviewDecision]
    usage_markers = signal_store.read_usage_markers(program_id)
    threads = signal_store.read_threads(program_id)  # dict[str, SignalThreadLink]
    work_item_ids = trajectory_store.list_work_item_ids(program_id)
    trajectory_points_by_item: dict[int, tuple] = {
        wi_id: trajectory_store.read(program_id, wi_id) for wi_id in work_item_ids
    }
    trajectory_point_count = sum(len(pts) for pts in trajectory_points_by_item.values())

    if not dry_run:
        for signal in signals:
            append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
        for decision in reviews.values():
            append_review_decision(program_id, decision, programs_root)
        for marker in usage_markers:
            append_usage_marker(program_id, marker, programs_root)
        for link in threads.values():
            append_signal_thread_link(program_id, link, programs_root)
        for wi_id, points in trajectory_points_by_item.items():
            backfill_trajectory_points(program_id, wi_id, points, programs_root=programs_root)

        # Rename the SQLite DB (tombstone)
        rollback_suffix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tombstone_path = db_path.with_name(f"{db_path.name}.pre-rollback-{rollback_suffix}")
        os.replace(db_path, tombstone_path)

        _update_program_storage_backend(program_id, programs_root=programs_root, storage_backend="file")

    return MigrationArtifacts(
        program_id=program_id,
        target_backend="file",
        signal_count=len(signals),
        review_count=len(reviews),
        thread_link_count=len(threads),
        trajectory_point_count=trajectory_point_count,
        usage_marker_count=len(usage_markers),
        database_path=db_path,
        config_updated=not dry_run,
        dry_run=dry_run,
    )
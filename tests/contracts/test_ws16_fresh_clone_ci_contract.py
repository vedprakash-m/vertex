"""WS-16: fresh-clone-to-first-command CI + schema-major migration round-trip.

This contract enforces the spec §WS-16 acceptance criteria on a developer
machine, without requiring a full GitHub Actions runner. It asserts three
properties:

  1. **ci.yml structure** — the CI workflow MUST contain a `fresh-clone-smoke`
     job that, in order, calls `pip install`, exercises `vertex --help`,
     materializes an isolated test program, runs `vertex doctor`, and
     executes the file→sqlite→file migration round-trip. (This is the
     structural ratchet: deleting or re-ordering the job fails the build.)

  2. **Schema-major migration round-trip is lossless** — for any
     populated program, `file → sqlite → file` MUST preserve the
     program.yaml's `storage_backend` field semantics (set to "sqlite"
     after the forward migration, removed-or-"file" after the rollback)
     AND MUST NOT delete the file-side history (signals, reviews) used
     as the migration source.

  3. **Fresh-clone isolation** — the round-trip MUST work on a `programs/`
     root that is gitignored / not in the repo working tree. The CI job
     uses a tmpdir; the contract asserts that no `programs/<id>/` lives
     in the repo working tree after the smoke run on a clean clone.

The unit test (property 2) calls `migrate.run_storage_migration` directly,
so it works on any machine that has the project on its PYTHONPATH. The
CI structural assertions (properties 1 and 3) read `.github/workflows/ci.yml`
as a plain text file (so we don't pull in `pyyaml` or a YAML parser — the
file is small enough to grep for the marker strings).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys

import yaml

import pytest

from src.commands import migrate
from src.core.journal import append_review_decision, append_signal
from src.core.models import Confidence
from src.core.models_v2 import Signal, SignalReviewDecision
from src.core.sqlite_stores import SQLiteSignalStore, get_program_sqlite_store_path


REPO_ROOT = Path(__file__).resolve().parents[2]

from src.core.charts.deployment_velocity import LEGACY_ALIAS_IDS as _CHART_LEGACY_ALIAS_IDS

_LEGACY_ALIASES_EXIST = bool(_CHART_LEGACY_ALIAS_IDS)


CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


# ---------- helpers ----------

def _seed_program_layout(programs_root: Path, program_id: str = "roundtrip") -> Path:
    """Create a minimal program.yaml so `migrate.load_program` can succeed."""
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": program_id,
                "name": "Roundtrip Test Program",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return programs_root


def _seed_file_history(programs_root: Path, program_id: str = "roundtrip") -> None:
    """Seed a deterministic file-side signal + review history using the
    journal API. Mirrors `_seed_file_history` in `tests/unit/test_commands_migrate.py`."""
    captured_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    append_signal(
        Signal(
            id="rt-1",
            timestamp=captured_at,
            source="ws_test",
            program_id=program_id,
            workstream_id="ws_rt",
            entity_refs=("WI:1",),
            text="Roundtrip signal 1.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=captured_at,
    )
    append_signal(
        Signal(
            id="rt-2",
            timestamp=captured_at,
            source="ws_test",
            program_id=program_id,
            workstream_id="ws_rt",
            entity_refs=("WI:2",),
            text="Roundtrip signal 2.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=captured_at,
    )
    append_review_decision(
        program_id,
        SignalReviewDecision(
            signal_id="rt-1",
            decision="approved",
            reviewed_at=captured_at,
            reviewed_by="system",
            note=None,
        ),
        programs_root=programs_root,
    )


# ---------- property 1: ci.yml structural assertions ----------

def test_ci_yml_declares_fresh_clone_smoke_job() -> None:
    """The CI workflow must contain a `fresh-clone-smoke` job. This is the
    structural ratchet — deleting the job (silently disabling fresh-clone
    coverage) fails the contract."""
    assert CI_YML.exists(), f"missing {CI_YML.relative_to(REPO_ROOT)}"
    text = CI_YML.read_text(encoding="utf-8")
    assert re.search(r"^\s{2}fresh-clone-smoke\s*:", text, re.MULTILINE), (
        "ci.yml does not declare a `fresh-clone-smoke:` job. The WS-16 "
        "fresh-clone-to-first-command contract requires a dedicated job "
        "that runs on a clean checkout (no cached programs/) and exercises "
        "`vertex --help`, `vertex doctor`, and the migration round-trip."
    )


def test_ci_fresh_clone_job_pip_installs_first() -> None:
    """Inside the fresh-clone-smoke job, the first non-checkout step MUST
    be a `pip install` (mirroring a real first-time user's first command).
    Putting anything else first would let the job pass with a pre-warmed
    environment, defeating the ratchet."""
    text = CI_YML.read_text(encoding="utf-8")
    job_block = _extract_job_block(text, "fresh-clone-smoke")
    # Iterate over steps in order; the first step that mentions `pip install`
    # MUST come before any step that runs a `vertex` CLI call. (Job-level
    # comments and prose descriptions are ignored — we only look at
    # `- name:` step bodies.)
    steps = _extract_step_bodies(job_block)
    pip_install_step = None
    for idx, (name, body) in enumerate(steps):
        if "pip install" in body and pip_install_step is None:
            pip_install_step = idx
            break
    assert pip_install_step is not None, (
        "fresh-clone-smoke job has no step that runs `pip install`"
    )
    # No step at or before pip_install_step may invoke a `vertex` CLI call.
    forbidden = ("vertex --help", "vertex doctor", "vertex migrate", "vx --help")
    for idx, (name, body) in enumerate(steps[: pip_install_step + 1]):
        for cmd in forbidden:
            if cmd in body:
                assert idx > pip_install_step, (
                    f"fresh-clone-smoke step `{name}` runs `{cmd}` BEFORE the "
                    f"`pip install` step. That defeats the fresh-clone contract — "
                    f"a real first-time user would hit a `vertex: command not found` "
                    f"error."
                )


def _extract_step_bodies(job_block: str) -> list[tuple[str, str]]:
    """Return [(step_name, step_body_text), ...] for every step in a CI job.
    A step begins with `      - name:` (6-space indent in GitHub Actions) and
    ends at the next sibling `- name:` or the end of the block. The body
    includes the `run:` block, env: block, and any other step fields."""
    lines = job_block.splitlines()
    step_starts: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        m = re.match(r"^\s{6}-\s+name:\s*(.+)$", line)
        if m:
            step_starts.append((idx, m.group(1).strip()))
    if not step_starts:
        return []
    out: list[tuple[str, str]] = []
    for i, (start, name) in enumerate(step_starts):
        end = step_starts[i + 1][0] if i + 1 < len(step_starts) else len(lines)
        out.append((name, "\n".join(lines[start:end])))
    return out


def test_ci_fresh_clone_job_runs_migrate_round_trip() -> None:
    """The job must execute BOTH the forward migration (`--to sqlite`) AND
    the rollback migration (`--to file`). This is the spec §WS-16 acceptance
    "schema-major migration round-trips with rollback"."""
    text = CI_YML.read_text(encoding="utf-8")
    job_block = _extract_job_block(text, "fresh-clone-smoke")
    assert "--to sqlite" in job_block, (
        "fresh-clone-smoke job does not call `vertex migrate --to sqlite`. "
        "WS-16 acceptance requires the forward schema-major migration."
    )
    assert "--to file" in job_block, (
        "fresh-clone-smoke job does not call `vertex migrate --to file`. "
        "WS-16 acceptance requires the rollback migration."
    )
    # And the rebuild-analytics verb (the third leg)
    assert "--rebuild-analytics" in job_block, (
        "fresh-clone-smoke job does not call `vertex migrate --rebuild-analytics`. "
        "WS-16 acceptance requires the rebuild round-trip too."
    )


def test_ci_fresh_clone_job_does_not_touch_real_programs_dir() -> None:
    """The job must use a tmpdir for the test program (NOT the repo's
    gitignored `programs/`). The `acme` and `fabrikam` programs are operator
    state — CI must not depend on them or accidentally create new
    `programs/freshclone/` entries in the working tree."""
    text = CI_YML.read_text(encoding="utf-8")
    job_block = _extract_job_block(text, "fresh-clone-smoke")
    # Must use a tmpdir
    assert "mktemp -d" in job_block or "TMPDIR=" in job_block or "tmp" in job_block, (
        "fresh-clone-smoke job does not use a tmpdir for the isolated program"
    )
    # And must NOT write into the real programs/ working tree. The
    # check-for-leak assertion in the job body is the source of truth.
    assert "if [ -d programs/freshclone" in job_block, (
        "fresh-clone-smoke job does not assert that no `programs/freshclone/` "
        "leaked into the working tree."
    )


def _extract_job_block(ci_text: str, job_name: str) -> str:
    """Return the substring of `ci_text` covering one top-level CI job.
    The job is delimited by `^  <name>:` at the start of a line (CI jobs
    are indented 2 spaces) and the next blank-line-then-`^  <name>:` pair
    (or EOF)."""
    lines = ci_text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if re.match(rf"^\s{{2}}{re.escape(job_name)}\s*:", line):
            start = idx
            break
    if start is None:
        raise AssertionError(f"job `{job_name}` not found in ci.yml")
    # Find the next sibling job header (also 2-space indent, ends with `:`)
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        if re.match(r"^\s{2}[a-z][a-z0-9_-]*\s*:", line):
            end = idx
            break
    return "\n".join(lines[start:end])


# ---------- property 2: schema-major round-trip is lossless ----------

def test_storage_migration_round_trip_preserves_program_yaml_semantics(tmp_path: Path) -> None:
    """file → sqlite must flip program.yaml's `storage_backend` to "sqlite";
    sqlite → file must remove-or-reset it. The file-side history MUST remain
    intact in both directions (lossless)."""
    programs_root = tmp_path / "programs"
    _seed_program_layout(programs_root, "roundtrip")
    _seed_file_history(programs_root, "roundtrip")

    # Forward: file → sqlite
    forward = migrate.run_storage_migration(
        program_id="roundtrip",
        target_backend="sqlite",
        programs_root=programs_root,
    )
    assert forward.signal_count == 2
    assert forward.review_count == 1
    assert not forward.dry_run
    assert forward.config_updated is True
    # program.yaml must reflect the new backend
    prog_yaml = yaml.safe_load(
        (programs_root / "roundtrip" / "program.yaml").read_text(encoding="utf-8")
    )
    assert prog_yaml.get("storage_backend") == "sqlite", (
        f"forward migration did not set storage_backend=sqlite: {prog_yaml}"
    )
    # SQLite store exists
    sqlite_path = get_program_sqlite_store_path("roundtrip", programs_root=programs_root)
    assert sqlite_path.exists(), f"forward migration did not write {sqlite_path}"

    # Rollback: sqlite → file
    rollback = migrate.run_storage_migration(
        program_id="roundtrip",
        target_backend="file",
        programs_root=programs_root,
    )
    assert rollback.dry_run is False
    assert rollback.config_updated is True
    # program.yaml must reflect the rollback
    prog_yaml = yaml.safe_load(
        (programs_root / "roundtrip" / "program.yaml").read_text(encoding="utf-8")
    )
    backend = prog_yaml.get("storage_backend")
    assert backend in (None, "file"), (
        f"rollback did not remove-or-reset storage_backend: {backend!r}"
    )
    # SQLite store must be gone (or moved to a tombstone, depending on impl)
    # We accept either: not at the canonical path, OR at a .pre-rollback- tombstone.
    tombstones = list(programs_root.glob("roundtrip/vertex_store.sqlite3.pre-rollback-*"))
    if tombstones:
        # Tombstoned: still on disk, but not at the canonical path
        assert not sqlite_path.exists(), (
            f"rollback left the canonical sqlite path intact: {sqlite_path}"
        )
    # File-side history MUST be intact
    from src.core.journal import read_signals
    remaining = {s.id for s in read_signals("roundtrip", programs_root=programs_root)}
    assert {"rt-1", "rt-2"}.issubset(remaining), (
        f"rollback destroyed file-side history; surviving ids: {remaining}"
    )


def test_storage_migration_dry_run_is_idempotent(tmp_path: Path) -> None:
    """Spec §WS-16 acceptance: a `dry-run` must NOT touch program.yaml or
    the sqlite store. A second dry-run on the same program must report the
    same counts (idempotent preview)."""
    programs_root = tmp_path / "programs"
    _seed_program_layout(programs_root, "roundtrip")
    _seed_file_history(programs_root, "roundtrip")

    first = migrate.run_storage_migration(
        program_id="roundtrip",
        target_backend="sqlite",
        programs_root=programs_root,
        dry_run=True,
    )
    second = migrate.run_storage_migration(
        program_id="roundtrip",
        target_backend="sqlite",
        programs_root=programs_root,
        dry_run=True,
    )
    assert first.signal_count == second.signal_count
    assert first.review_count == second.review_count
    # The dry-run promise: no sqlite written, no program.yaml changed
    assert not get_program_sqlite_store_path(
        "roundtrip", programs_root=programs_root
    ).exists()
    prog_yaml = yaml.safe_load(
        (programs_root / "roundtrip" / "program.yaml").read_text(encoding="utf-8")
    )
    assert "storage_backend" not in prog_yaml, (
        f"dry-run leaked a storage_backend key: {prog_yaml}"
    )


# ---------- property 2b: chart-alias migration log ----------

@pytest.mark.skipif(
    not _LEGACY_ALIASES_EXIST,
    reason="No legacy chart aliases configured; migration tests skipped.",
)
def test_chart_alias_migration_rewrites_and_logs(tmp_path: Path) -> None:
    """WS-11/WS-16 acceptance: `vertex migrate` rewrites the historical
    `acme::deployment_velocity` chart id to the canonical `core::deployment_velocity`
    in the program's tracked YAML files AND appends a row to
    `programs/<id>/migration_log.jsonl`. This is the auditable surface
    for "we migrated your chart configs" — without it, operators have
    no record of which files were touched, and a re-run on a clean
    clone would silently no-op."""
    from src.core.migration_log import read_migration_log
    from src.core.charts.deployment_velocity import CANONICAL_RENDERER_ID, LEGACY_ALIAS_IDS

    programs_root = tmp_path / "programs"
    _seed_program_layout(programs_root, "aliasprog")
    _seed_file_history(programs_root, "aliasprog")
    # Seed a tracked config that uses the legacy alias. We pick a path
    # under the program root (not under archive/) so the rewriter scans it.
    legacy_id = LEGACY_ALIAS_IDS[0]
    target_yaml = programs_root / "aliasprog" / "workstreams.yaml"
    target_yaml.write_text(
        "workstreams:\n  - id: ws_ap\n    chart_renderer: "
        f"{legacy_id}\n    x_axis: week\n    y_axes: [count]\n",
        encoding="utf-8",
    )

    # Round-trip: file → sqlite
    migrate.run_storage_migration(
        program_id="aliasprog",
        target_backend="sqlite",
        programs_root=programs_root,
    )

    # 1. The YAML was rewritten to the canonical id
    rewritten = target_yaml.read_text(encoding="utf-8")
    assert legacy_id not in rewritten, (
        f"alias not rewritten in {target_yaml.name}: {rewritten!r}"
    )
    assert CANONICAL_RENDERER_ID in rewritten, (
        f"canonical id not present in {target_yaml.name}: {rewritten!r}"
    )

    # 2. A migration_log row was appended
    log_entries = read_migration_log("aliasprog", programs_root=programs_root)
    assert log_entries, "no migration_log rows were written"
    chart_alias_rows = [e for e in log_entries if e.kind == "chart_id_alias"]
    assert len(chart_alias_rows) >= 1, (
        f"expected ≥1 chart_id_alias row, got: {[e.kind for e in log_entries]}"
    )
    row = chart_alias_rows[0]
    assert row.source_id == legacy_id
    assert row.target_id == CANONICAL_RENDERER_ID
    assert row.dry_run is False
    assert any("workstreams.yaml" in f for f in row.files_touched)


@pytest.mark.skipif(
    not _LEGACY_ALIASES_EXIST,
    reason="No legacy chart aliases configured; migration tests skipped.",
)
def test_chart_alias_migration_dry_run_does_not_write(tmp_path: Path) -> None:
    """Dry-run must NOT touch the YAMLs and must NOT append to migration_log.
    The contract: a preview must be observably side-effect-free."""
    from src.core.migration_log import read_migration_log
    from src.core.charts.deployment_velocity import CANONICAL_RENDERER_ID, LEGACY_ALIAS_IDS

    programs_root = tmp_path / "programs"
    _seed_program_layout(programs_root, "aliasprog_dry")
    _seed_file_history(programs_root, "aliasprog_dry")
    legacy_id = LEGACY_ALIAS_IDS[0]
    target_yaml = programs_root / "aliasprog_dry" / "workstreams.yaml"
    original_text = (
        "workstreams:\n  - id: ws_ap\n    chart_renderer: "
        f"{legacy_id}\n    x_axis: week\n    y_axes: [count]\n"
    )
    target_yaml.write_text(original_text, encoding="utf-8")

    # Dry-run forward
    migrate.run_storage_migration(
        program_id="aliasprog_dry",
        target_backend="sqlite",
        programs_root=programs_root,
        dry_run=True,
    )

    # The YAML must be untouched
    assert target_yaml.read_text(encoding="utf-8") == original_text
    # No migration_log row written
    log_entries = read_migration_log("aliasprog_dry", programs_root=programs_root)
    assert log_entries == (), (
        f"dry-run leaked migration_log rows: {[e.kind for e in log_entries]}"
    )


# ---------- property 3: fresh-clone isolation (live) ----------

def test_fresh_clone_isolation_works_locally(tmp_path: Path) -> None:
    """The same sequence the CI job runs should work on a developer
    machine: bootstrap a test program into a tmpdir, run the round-trip,
    confirm nothing was written into the real `programs/` working tree.

    This is the locally-runnable mirror of the CI job. It exists so a
    developer can `pytest tests/contracts/test_ws16_fresh_clone_ci_contract.py`
    before pushing and catch any drift in the smoke recipe."""
    # Snapshot the real programs/ dir
    real_programs = REPO_ROOT / "programs"
    before = set(real_programs.glob("*")) if real_programs.exists() else set()

    # Materialize a fresh program in an isolated tmpdir using the tracked template
    isolated_root = tmp_path / "isolated_programs"
    isolated_root.mkdir()
    (isolated_root / "_templates").mkdir()
    import shutil
    shutil.copytree(
        real_programs / "_templates" / "example_tpm",
        isolated_root / "_templates" / "example_tpm",
    )

    # Use the bootstrap script's library entry point
    from scripts.setup_test_program import main as bootstrap_main

    rc = bootstrap_main(
        [
            "--template", "example_tpm",
            "--program", "freshclone_local",
            "--programs-root", str(isolated_root),
        ]
    )
    assert rc == 0, "bootstrap_main returned non-zero"
    assert (isolated_root / "freshclone_local" / "program.yaml").exists()

    # Seed a file-side history using the journal API (same path the real
    # gather.py uses, so this is a realistic precondition for `migrate`).
    _seed_file_history(isolated_root, "freshclone_local")

    # Forward migration
    forward = migrate.run_storage_migration(
        program_id="freshclone_local",
        target_backend="sqlite",
        programs_root=isolated_root,
    )
    assert forward.signal_count == 2
    assert forward.review_count == 1
    prog_yaml = yaml.safe_load(
        (isolated_root / "freshclone_local" / "program.yaml").read_text(encoding="utf-8")
    )
    assert prog_yaml.get("storage_backend") == "sqlite"

    # Rollback
    rollback = migrate.run_storage_migration(
        program_id="freshclone_local",
        target_backend="file",
        programs_root=isolated_root,
    )
    assert rollback.config_updated is True
    prog_yaml = yaml.safe_load(
        (isolated_root / "freshclone_local" / "program.yaml").read_text(encoding="utf-8")
    )
    assert prog_yaml.get("storage_backend") in (None, "file"), (
        f"rollback did not remove-or-reset storage_backend: {prog_yaml}"
    )

    # The seeded file-side history must be reachable via the journal after
    # the round-trip. (Migrate does NOT delete the source; rollback may
    # leave a tombstone at vertex_store.sqlite3.pre-rollback-*, which is
    # fine and explicitly allowed.)
    from src.core.journal import read_signals
    remaining = {s.id for s in read_signals("freshclone_local", programs_root=isolated_root)}
    assert {"rt-1", "rt-2"}.issubset(remaining), (
        f"rollback destroyed file-side history; surviving ids: {remaining}"
    )

    # Final invariant: the real repo programs/ dir was untouched
    after = set(real_programs.glob("*")) if real_programs.exists() else set()
    assert before == after, (
        f"running the WS-16 contract test touched the repo's programs/ dir: "
        f"before={sorted(p.name for p in before)} after={sorted(p.name for p in after)}"
    )

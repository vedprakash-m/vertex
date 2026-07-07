from __future__ import annotations

from pathlib import Path
import shutil
import threading

import yaml

from src.core.config_loader import discover_report_editions, load_bundle_with_mode
from src.core.overrides_store import DimensionOverride, OverridesDocument, ScorecardOverrides, save_overrides


DEFAULT_V2_EDITIONS = ("acme_weekly",)
DEFAULT_V2_PROGRAMS = ("acme",)

# Directories and file patterns that represent runtime state, never needed by unit tests.
# ``checkpoints`` is excluded so that pre-existing live checkpoints (e.g. ``issue_078_*``)
# from the local C: cache never leak into the test workspace, which breaks
# doctor / rollback / admin-flip tests that assert the no-checkpoint path.
_RUNTIME_DIRS = (
    "archive",
    "journal",
    "narratives",
    "overrides",
    "output",
    "rev_inbox",
    "summaries",
    "trajectories",
    "checkpoints",
)
_RUNTIME_FILE_PATTERNS = (
    "trusted_baseline.yaml",
    "risk_register.yaml",
    "m365_registry.yaml",
    "*.sqlite3",
    "fact_sor_state.yaml",
    "fact_store_family_cycles.yaml",
    "fact_store_sor.yaml",
    "platform_proof_log.yaml",
)
_SLIM_IGNORE = shutil.ignore_patterns(*_RUNTIME_DIRS, *_RUNTIME_FILE_PATTERNS)

# Session-level local cache: set once by prime_local_source_cache(), used by all tests.
# Avoids repeated copytree calls from the Q: network drive.
_LOCAL_SOURCE_CACHE: Path | None = None
_CACHE_LOCK = threading.Lock()

# Pre-computed seed overrides document (computed once per session from the cached config).
# Used by reset_overrides_to_seed_state to skip 7-YAML bundle load per test call.
_SEED_OVERRIDES: dict[str, OverridesDocument] | None = None
_REQUIRED_PROGRAM_CONFIG_FILES = (
    "program.yaml",
    "workstreams.yaml",
)
_IDENTITY_KEYS = frozenset({"owner", "author", "reviewed_by", "alias", "email"})


def get_source_root(repo_root: Path) -> Path:
    """Return the local C: cache root if primed, else fall back to repo_root (Q: drive).

    Use this in custom _seed_*_layout helpers to avoid direct Q: drive copytree calls.
    """
    return _LOCAL_SOURCE_CACHE if _LOCAL_SOURCE_CACHE is not None else repo_root


def prime_local_source_cache(repo_root: Path, cache_root: Path) -> None:
    """Copy slim source data from repo_root (Q: network drive) to a local cache_root once.

    Called by the session-scoped conftest fixture before any test runs.  Subsequent
    stage_v2_report_workspace() calls read from the local C: cache instead of Q:.
    """
    global _LOCAL_SOURCE_CACHE
    with _CACHE_LOCK:
        if _LOCAL_SOURCE_CACHE is not None:
            return  # Already primed (e.g. by a previous xdist worker)

        schemas_src = repo_root / "reports" / "schemas"
        if schemas_src.exists():
            (cache_root / "reports").mkdir(parents=True, exist_ok=True)
            shutil.copytree(schemas_src, cache_root / "reports" / "schemas")

        # Editions now live under programs/<id>/editions/ (see specs/move-editions.md),
        # so they are staged as part of the programs tree below — no separate flat
        # editions/ directory is copied.

        programs_src = repo_root / "programs"
        if programs_src.exists():
            # Skip 71 MB of runtime state — no test uses include_runtime_state=True.
            _copy_programs_tree(programs_src, cache_root / "programs")
            # Defensive: even with _SLIM_IGNORE, a previously-primed cache from a
            # session that did NOT exclude ``checkpoints``/``fact_sor_state.yaml``
            # could still carry those files.  Sweep them out so a stale cache
            # never leaks live state (e.g. a real issue_078_* checkpoint) into
            # tests that assert the no-checkpoint path.
            for program_root in (cache_root / "programs").iterdir():
                if not program_root.is_dir():
                    continue
                for runtime_dir in _RUNTIME_DIRS:
                    target = program_root / runtime_dir
                    if target.exists():
                        shutil.rmtree(target, ignore_errors=True)
                for fname in (
                    "trusted_baseline.yaml",
                    "risk_register.yaml",
                    "m365_registry.yaml",
                    "fact_sor_state.yaml",
                    "fact_store_family_cycles.yaml",
                    "fact_store_sor.yaml",
                    "platform_proof_log.yaml",
                ):
                    p = program_root / fname
                    if p.exists():
                        p.unlink()
                for sqlite_path in program_root.glob("*.sqlite3"):
                    sqlite_path.unlink()
            _validate_cached_program_tree(cache_root / "programs")

        knowledge_src = repo_root / "knowledge"
        if knowledge_src.exists():
            # Exclude dashboards/ (136 files, 1.3 MB) — no unit test reads chart dashboard data.
            shutil.copytree(
                knowledge_src,
                cache_root / "knowledge",
                ignore=shutil.ignore_patterns("dashboards"),
            )
            _normalize_shared_knowledge(cache_root / "knowledge")

        _LOCAL_SOURCE_CACHE = cache_root

        # Pre-compute the seed overrides document so reset_overrides_to_seed_state()
        # doesn't need to re-parse 7 YAML files on every test call.
        global _SEED_OVERRIDES
        _SEED_OVERRIDES = _build_seed_overrides(cache_root)


def _copy_programs_tree(programs_src: Path, programs_dst: Path) -> None:
    """Copy the programs tree and fail fast if required config files are absent."""
    if programs_dst.exists():
        shutil.rmtree(programs_dst, ignore_errors=True)
    shutil.copytree(programs_src, programs_dst, ignore=_SLIM_IGNORE)


def _validate_cached_program_tree(programs_root: Path) -> None:
    """Ensure the slimmed cache still contains the config files test bundles require."""
    if not programs_root.exists():
        raise FileNotFoundError(f"Cached programs root missing: {programs_root}")

    missing: list[str] = []
    for program_root in programs_root.iterdir():
        if not program_root.is_dir() or not (program_root / "program.yaml").exists():
            continue
        for filename in _REQUIRED_PROGRAM_CONFIG_FILES:
            if not (program_root / filename).exists():
                missing.append(f"{program_root.name}/{filename}")

    if missing:
        raise FileNotFoundError(
            "Cached programs tree is missing required config files: "
            + ", ".join(sorted(missing))
        )


def stage_v2_report_workspace(
    repo_root: Path,
    workspace_root: Path,
    *,
    edition_names: tuple[str, ...] = DEFAULT_V2_EDITIONS,
    program_names: tuple[str, ...] = DEFAULT_V2_PROGRAMS,
    include_runtime_state: bool = False,
) -> Path:
    # Use the local C: cache when available; fall back to repo_root (Q: drive) otherwise.
    src = _LOCAL_SOURCE_CACHE if _LOCAL_SOURCE_CACHE is not None else repo_root

    reports_root = workspace_root / "reports"
    shutil.copytree(src / "reports" / "schemas", reports_root / "schemas")

    # Editions live under programs/<id>/editions/ and are staged as part of the
    # programs-tree copy below. discover_report_editions() and the edition
    # resolver both read from the programs tree, so no flat editions/ directory
    # is required in the workspace.
    programs_root = workspace_root / "programs"
    for program_name in program_names:
        source_program = src / "programs" / program_name
        if not source_program.exists():
            import pytest
            pytest.skip(f"Requires local program data for {program_name}")
        target_program_root = programs_root / program_name

        if _LOCAL_SOURCE_CACHE is not None:
            # Cache is already slim (runtime dirs excluded by prime_local_source_cache).
            # C: → C: copy is fast; no cleanup step needed.
            shutil.copytree(source_program, target_program_root)
        elif not include_runtime_state:
            # Fallback: copying from Q: drive — use ignore_patterns to skip 71 MB of
            # runtime state instead of copying it all and then deleting.
            shutil.copytree(source_program, target_program_root, ignore=_SLIM_IGNORE)
        else:
            shutil.copytree(source_program, target_program_root)

        if _LOCAL_SOURCE_CACHE is not None:
            # Defensive sweep: a cache primed by an earlier session may still
            # contain pre-existing checkpoints / fact_sor_state files that
            # would otherwise leak into the test workspace.  Mirror the
            # include_runtime_state=False cleanup path so the test workspace
            # is always free of live state.
            for runtime_dir in _RUNTIME_DIRS:
                runtime_target = target_program_root / runtime_dir
                if runtime_target.exists():
                    shutil.rmtree(runtime_target, ignore_errors=True)
            for fname in (
                "trusted_baseline.yaml",
                "risk_register.yaml",
                "m365_registry.yaml",
                "fact_sor_state.yaml",
                "fact_store_sor.yaml",
                "platform_proof_log.yaml",
            ):
                p = target_program_root / fname
                if p.exists():
                    p.unlink()
            for sqlite_path in target_program_root.glob("*.sqlite3"):
                sqlite_path.unlink()

        if include_runtime_state is False and _LOCAL_SOURCE_CACHE is None:
            # Q: fallback path: ignore_patterns skips dirs and named files, but glob
            # patterns like "*.sqlite3" only work at the top level in ignore_patterns.
            # Do an explicit sweep for any leftover runtime files.
            for runtime_dir in _RUNTIME_DIRS:
                shutil.rmtree(target_program_root / runtime_dir, ignore_errors=True)
            for fname in (
                "trusted_baseline.yaml",
                "risk_register.yaml",
                "m365_registry.yaml",
                "fact_sor_state.yaml",
                "fact_store_sor.yaml",
                "platform_proof_log.yaml",
            ):
                p = target_program_root / fname
                if p.exists():
                    p.unlink()
            for sqlite_path in target_program_root.glob("*.sqlite3"):
                sqlite_path.unlink()

        # Belt-and-suspenders: if source_program existed as a directory but lacked
        # program.yaml (e.g. programs/acme/ present with only runtime dirs on the CI
        # runner after a previous step), the copy succeeds but leaves an incomplete
        # workspace.  Catch that here and skip rather than proceeding to a later
        # failure in disable_kusto_in_report_copy or config_loader.
        if not (target_program_root / "program.yaml").exists():
            import pytest
            pytest.skip(f"Requires full program data for {program_name} (program.yaml absent after copy)")

        _normalize_program_org(target_program_root)

    knowledge_root = src / "knowledge"
    if knowledge_root.exists():
        shutil.copytree(knowledge_root, workspace_root / "knowledge")
        _normalize_shared_knowledge(workspace_root / "knowledge")

    return reports_root


def _build_seed_overrides(cache_root: Path) -> dict[str, OverridesDocument]:
    """Compute seed OverridesDocument for each edition from the cached config (called once)."""
    reports_root = cache_root / "reports"
    programs_root = cache_root / "programs"
    result: dict[str, OverridesDocument] = {}
    for edition_name in discover_report_editions(reports_root=reports_root, programs_root=programs_root):
        bundle = load_bundle_with_mode(
            edition_name,
            reports_root=reports_root,
            programs_root=programs_root,
        ).bundle
        scorecards = tuple(
            ScorecardOverrides(
                name=scorecard.name,
                dimensions=tuple(
                    DimensionOverride(name=dimension.name, risk=None)
                    for dimension in scorecard.dimensions
                ),
            )
            for scorecard in bundle.config.scorecards
        )
        result[edition_name] = OverridesDocument(issue_number=None, top_3_now=(), scorecards=scorecards)
    return result


def reset_overrides_to_seed_state(reports_root: Path) -> None:
    # Fast path: use pre-computed seed overrides (avoids re-parsing 7 YAML files per call).
    if _SEED_OVERRIDES is not None:
        for edition_name, seed_document in _SEED_OVERRIDES.items():
            save_overrides(edition_name, seed_document, reports_root=reports_root)
        return

    programs_root = reports_root.parent / "programs"
    v2_editions = discover_report_editions(reports_root=reports_root, programs_root=programs_root)
    if v2_editions:
        for edition_name in v2_editions:
            bundle = load_bundle_with_mode(
                edition_name,
                reports_root=reports_root,
                programs_root=programs_root,
            ).bundle
            scorecards = tuple(
                ScorecardOverrides(
                    name=scorecard.name,
                    dimensions=tuple(
                        DimensionOverride(name=dimension.name, risk=None)
                        for dimension in scorecard.dimensions
                    ),
                )
                for scorecard in bundle.config.scorecards
            )
            seed_document = OverridesDocument(
                issue_number=None,
                top_3_now=(),
                scorecards=scorecards,
            )
            save_overrides(edition_name, seed_document, reports_root=reports_root)
        return

    for edition_path in reports_root.iterdir():
        if not edition_path.is_dir() or edition_path.name == "schemas":
            continue

        config_path = edition_path / "config.yaml"
        overrides_path = edition_path / "overrides.yaml"
        if not config_path.exists() or not overrides_path.exists():
            continue

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        scorecards = tuple(
            ScorecardOverrides(
                name=str(scorecard.get("name", "")),
                dimensions=tuple(
                    DimensionOverride(name=str(dimension.get("name", "")), risk=None)
                    for dimension in scorecard.get("dimensions", [])
                ),
            )
            for scorecard in payload.get("scorecards", [])
        )
        seed_document = OverridesDocument(
            issue_number=None,
            top_3_now=(),
            scorecards=scorecards,
        )
        save_overrides(edition_path.name, seed_document, reports_root=reports_root)


def disable_kusto_in_report_copy(reports_root: Path) -> None:
    reset_overrides_to_seed_state(reports_root)
    program_path = reports_root.parent / "programs" / "acme" / "program.yaml"
    if not program_path.exists():
        import pytest
        pytest.skip("Requires local programs/acme data (program.yaml missing)")
    payload = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    payload.setdefault("kusto", {})["enabled"] = False
    payload.setdefault("m365", {})["enabled"] = False
    # Disable AI so unit tests are deterministic regardless of whether
    # VERTEX_AI_DEPLOYMENT is set in the local environment.  Tests that
    # specifically exercise AI behaviour call _enable_v2_program_ai() to
    # re-enable it after this helper runs.
    payload.setdefault("ai", {})["enabled"] = False
    program_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _normalize_program_org(program_root: Path) -> None:
    """Replace internal ADO org names with the public placeholder in a test workspace copy.

    Also resets fact_sor_state.yaml to 'legacy' mode so that tests which copy programs
    into a temp workspace (without a SQLite database) always use the YAML-backed shim
    path rather than getting empty workstream/fact results from an absent SQLite DB.
    """
    program_path = program_root / "program.yaml"
    if not program_path.exists():
        return
    payload = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    ado_block = payload.get("ado")
    if isinstance(ado_block, dict) and ado_block.get("organization") == "msazure":
        ado_block["organization"] = "your-org"
    program_path.write_text(
        yaml.safe_dump(_sanitize_yaml_values(payload), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    workstreams_path = program_root / "workstreams.yaml"
    if workstreams_path.exists():
        workstreams_payload = yaml.safe_load(workstreams_path.read_text(encoding="utf-8")) or {}
        workstreams_path.write_text(
            yaml.safe_dump(_sanitize_yaml_values(workstreams_payload), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    # Reset fact_store_sor.yaml to 'legacy' mode in test workspaces.
    # Tests copy programs/ without the SQLite DB; if mode is 'primary', load_program_facts
    # returns an empty snapshot (no DB = no facts) which causes spurious test failures.
    sor_state_path = program_root / "fact_store_sor.yaml"
    if sor_state_path.exists():
        sor_state = yaml.safe_load(sor_state_path.read_text(encoding="utf-8")) or {}
        if isinstance(sor_state, dict) and sor_state.get("mode") != "legacy":
            sor_state["mode"] = "legacy"
            sor_state_path.write_text(yaml.safe_dump(sor_state, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _normalize_shared_knowledge(knowledge_root: Path) -> None:
    for yaml_path in knowledge_root.rglob("*.yaml"):
        payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        yaml_path.write_text(
            yaml.safe_dump(_sanitize_yaml_values(payload), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


def _sanitize_yaml_values(value, *, key: str | None = None):
    if isinstance(value, dict):
        return {item_key: _sanitize_yaml_values(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_yaml_values(item, key=key) for item in value]
    if isinstance(value, str):
        if (key or "").lower() in _IDENTITY_KEYS:
            return "maintainer@example.com" if "@" in value else "maintainer"
        if "@microsoft.com" in value.lower():
            return "maintainer@example.com"
    return value

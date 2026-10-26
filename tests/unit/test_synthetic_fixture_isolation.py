from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dotenv
import pytest
import yaml

from src.core import config_loader_v2
from src.core.overrides_store import load_overrides
from tests.support import report_test_setup
from tests.unit.test_config_loader import _write_demo_v2_layout
from tests.unit.test_report_test_setup import _write_program_tree


def test_actual_pytest_startup_does_not_copy_repository_context(tmp_path: Path) -> None:
    test_path = tmp_path / "test_startup.py"
    test_path.write_text("def test_startup():\n    assert True\n", encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    script = """
import os
from pathlib import Path
import sys
import dotenv
import pytest

root = Path(sys.argv[1]).resolve()
private = (root / "programs", root / "knowledge")

def guard(event, args):
    if event not in {"open", "os.scandir", "shutil.copytree"}:
        return
    if not args or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    path = Path(os.fsdecode(args[0])).resolve()
    if path.name == ".env" or any(path.is_relative_to(base) for base in private):
        raise AssertionError("Private source access forbidden during pytest startup")

sys.addaudithook(guard)
dotenv.load_dotenv = lambda *args, **kwargs: False
raise SystemExit(pytest.main([
    "-p", "tests.conftest", sys.argv[2], "-q", "-o", "addopts=",
]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), str(test_path)],
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("operation", ["source", "prime", "stage"])
@pytest.mark.parametrize("relative", ["", "programs", "tests"])
def test_repository_checkout_is_not_a_report_fixture_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, relative: str,
) -> None:
    root = Path(__file__).resolve().parents[2] / relative
    monkeypatch.setattr(report_test_setup, "_LOCAL_SOURCE_CACHE", None)
    monkeypatch.setattr(report_test_setup, "_LOCAL_SOURCE_ROOT", None)

    def forbidden_copy(*args, **kwargs):
        pytest.fail("Repository fixture source must be rejected before copying")

    monkeypatch.setattr(report_test_setup.shutil, "copytree", forbidden_copy)
    with pytest.raises(ValueError, match="synthetic"):
        if operation == "source":
            report_test_setup.get_source_root(root)
        elif operation == "prime":
            report_test_setup.prime_local_source_cache(root, tmp_path / "cache")
        else:
            report_test_setup.stage_v2_report_workspace(root, tmp_path / "workspace")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("operation", ["prime", "stage"])
@pytest.mark.parametrize("relative", ["", "programs", "tests"])
def test_repository_checkout_is_not_a_fixture_destination(
    tmp_path: Path, synthetic_sources: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch, operation: str, relative: str,
) -> None:
    _, source = synthetic_sources
    destination = Path(__file__).resolve().parents[2] / relative

    def forbidden_write(*args, **kwargs):
        pytest.fail("Checkout destination must be rejected before writes")

    monkeypatch.setattr(Path, "mkdir", forbidden_write)
    monkeypatch.setattr(report_test_setup.shutil, "copytree", forbidden_write)
    with pytest.raises(ValueError, match="synthetic"):
        if operation == "prime":
            report_test_setup.prime_local_source_cache(source, destination)
        else:
            report_test_setup.stage_v2_report_workspace(source, destination)


def _add_stale_runtime(program: Path) -> None:
    (program / "journal").mkdir(exist_ok=True)
    (program / "journal" / "stale.yaml").write_text("synthetic\n", encoding="utf-8")
    (program / "trusted_baseline.yaml").write_text("synthetic\n", encoding="utf-8")
    (program / "stale.sqlite3").write_bytes(b"synthetic, not a database")


def test_explicit_priming_removes_stale_cache_runtime(
    tmp_path: Path, synthetic_sources: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source = synthetic_sources
    (source / "programs" / "marker.txt").write_text("synthetic\n", encoding="utf-8")
    original = report_test_setup._copy_programs_tree

    def copy_programs(src: Path, dst: Path) -> None:
        original(src, dst)
        _add_stale_runtime(dst / "acme")

    monkeypatch.setattr(report_test_setup, "_copy_programs_tree", copy_programs)
    cache = tmp_path / "cache"
    report_test_setup.prime_local_source_cache(source, cache)
    cached_program = cache / "programs" / "acme"
    assert not (cached_program / "journal").exists()
    assert not (cached_program / "trusted_baseline.yaml").exists()
    assert not (cached_program / "stale.sqlite3").exists()
    assert (cache / "programs" / "marker.txt").is_file()


def test_explicit_empty_source_can_be_primed_without_invented_context(
    tmp_path: Path, synthetic_sources: tuple[Path, Path],
) -> None:
    source = tmp_path / "empty"
    source.mkdir()
    cache = tmp_path / "cache"
    report_test_setup.prime_local_source_cache(source, cache)
    assert report_test_setup.get_source_root(source) == cache
    assert report_test_setup._SEED_OVERRIDES == {}
    assert not cache.exists()


@pytest.mark.parametrize("cached", [False, True])
def test_staging_removes_stale_runtime_from_explicit_sources(
    tmp_path: Path, synthetic_sources: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch, cached: bool,
) -> None:
    _, source = synthetic_sources
    if cached:
        monkeypatch.setattr(report_test_setup, "_LOCAL_SOURCE_CACHE", source)
        monkeypatch.setattr(report_test_setup, "_LOCAL_SOURCE_ROOT", source.resolve())
    original = report_test_setup.shutil.copytree

    def copytree(src, dst, *args, **kwargs):
        result = original(src, dst, *args, **kwargs)
        if Path(dst).name == "acme":
            _add_stale_runtime(Path(dst))
        return result

    monkeypatch.setattr(report_test_setup.shutil, "copytree", copytree)
    reports = report_test_setup.stage_v2_report_workspace(source, tmp_path / "workspace")
    program = reports.parent / "programs" / "acme"
    assert not (program / "journal").exists()
    assert not (program / "trusted_baseline.yaml").exists()
    assert not (program / "stale.sqlite3").exists()


@pytest.mark.parametrize("missing", ["program", "config"])
def test_staging_preserves_explicit_missing_source_diagnostics(
    tmp_path: Path, synthetic_sources: tuple[Path, Path], missing: str,
) -> None:
    source = tmp_path / "incomplete"
    (source / "reports" / "schemas").mkdir(parents=True)
    if missing == "config":
        (source / "programs" / "acme").mkdir(parents=True)
    with pytest.raises(pytest.skip.Exception, match="Requires"):
        report_test_setup.stage_v2_report_workspace(source, tmp_path / "workspace")


def test_explicit_program_without_shared_knowledge_does_not_invent_it(
    tmp_path: Path, synthetic_sources: tuple[Path, Path],
) -> None:
    source = tmp_path / "minimal"
    (source / "reports" / "schemas").mkdir(parents=True)
    _write_program_tree(source / "programs")
    reports = report_test_setup.stage_v2_report_workspace(source, tmp_path / "workspace")
    assert reports.is_dir()
    assert not (reports.parent / "knowledge").exists()


def test_legacy_seed_reset_ignores_non_editions(
    tmp_path: Path, synthetic_sources: tuple[Path, Path],
) -> None:
    reports = tmp_path / "workspace" / "reports"
    (reports / "schemas").mkdir(parents=True)
    (reports / "not-an-edition").mkdir()
    (reports / "marker.txt").write_text("synthetic\n", encoding="utf-8")
    report_test_setup.reset_overrides_to_seed_state(reports)
    assert not list(reports.rglob("overrides.yaml"))


def _root_functions(*names: str) -> dict[str, Any]:
    """Compile selected functions without importing root conftest or its startup."""
    path = Path(__file__).resolve().parents[1] / "conftest.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {node.name for node in functions} == set(names)
    namespace: dict[str, Any] = {"pytest": pytest, "Path": Path}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


@pytest.fixture
def synthetic_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    roots = (tmp_path / "ambient", tmp_path / "sealed")
    for root in roots:
        (root / "reports" / "schemas").mkdir(parents=True)
        program = _write_program_tree(root / "programs")
        (program / "program.yaml").write_text(f"id: acme\nname: {root.name}\n", encoding="utf-8")
        (root / "knowledge").mkdir()
        (root / "knowledge" / "fixture.yaml").write_text(f"source: {root.name}\n", encoding="utf-8")
    monkeypatch.setattr(report_test_setup, "_LOCAL_SOURCE_CACHE", None)
    monkeypatch.setattr(report_test_setup, "_LOCAL_SOURCE_ROOT", None, raising=False)
    # The old seed cache must not leak beyond this test, including during the red run.
    monkeypatch.setattr(report_test_setup, "_SEED_OVERRIDES", None, raising=False)
    copytree = report_test_setup.shutil.copytree

    def sealed_copy(source: Path, destination: Path, *args, **kwargs):
        assert Path(source).resolve().is_relative_to(tmp_path.resolve())
        assert Path(destination).resolve().is_relative_to(tmp_path.resolve())
        return copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(report_test_setup.shutil, "copytree", sealed_copy)
    yield roots


def test_primed_cache_cannot_replace_an_explicit_synthetic_source(
    tmp_path: Path, synthetic_sources: tuple[Path, Path],
) -> None:
    ambient, sealed = synthetic_sources
    cache = tmp_path / "cache"
    report_test_setup.prime_local_source_cache(ambient, cache)

    assert report_test_setup.get_source_root(sealed) == sealed
    reports = report_test_setup.stage_v2_report_workspace(sealed, tmp_path / "workspace")

    assert "name: sealed" in (reports.parent / "programs" / "acme" / "program.yaml").read_text(encoding="utf-8")
    assert (reports.parent / "knowledge" / "fixture.yaml").read_text(encoding="utf-8") == "source: sealed\n"


def test_synthetic_staging_has_parity_before_and_after_unrelated_priming(
    tmp_path: Path, synthetic_sources: tuple[Path, Path],
) -> None:
    ambient, sealed = synthetic_sources
    program = sealed / "programs" / "acme"
    (program / "journal").mkdir()
    (program / "journal" / "runtime.yaml").write_text("synthetic runtime\n", encoding="utf-8")
    (program / "fixture.sqlite3").write_bytes(b"synthetic, not a database")
    reports_before = report_test_setup.stage_v2_report_workspace(sealed, tmp_path / "before")

    report_test_setup.prime_local_source_cache(ambient, tmp_path / "cache")
    reports_after = report_test_setup.stage_v2_report_workspace(sealed, tmp_path / "after")

    def snapshot(root: Path) -> dict[Path, bytes]:
        return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    assert snapshot(reports_before.parent) == snapshot(reports_after.parent)
    assert not (reports_after.parent / "programs" / "acme" / "journal").exists()
    assert not (reports_after.parent / "programs" / "acme" / "fixture.sqlite3").exists()


def test_matching_source_keeps_synthetic_staging_parity_with_the_cache(
    tmp_path: Path, synthetic_sources: tuple[Path, Path],
) -> None:
    _, sealed = synthetic_sources
    reports_before = report_test_setup.stage_v2_report_workspace(sealed, tmp_path / "before")
    cache = tmp_path / "cache"
    report_test_setup.prime_local_source_cache(sealed, cache)

    assert report_test_setup.get_source_root(sealed) == cache
    reports_after = report_test_setup.stage_v2_report_workspace(sealed, tmp_path / "after")

    for relative in (
        Path("programs") / "acme" / "program.yaml",
        Path("programs") / "acme" / "workstreams.yaml",
        Path("knowledge") / "fixture.yaml",
    ):
        assert (reports_before.parent / relative).read_bytes() == (reports_after.parent / relative).read_bytes()


def _write_sealed_v2_bundle(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    editions, programs = _write_demo_v2_layout(root)
    editions.rename(programs / "demo" / "editions")
    program_path = programs / "demo" / "program.yaml"
    program = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program["kusto"]["enabled"] = False
    program["key_dependencies"] = [
        {"from_item": "Synthetic upstream", "to_item": "Synthetic downstream", "impact": "Synthetic"}
    ]
    program_path.write_text(yaml.safe_dump(program, sort_keys=False), encoding="utf-8")

    def forbidden_facts(*args, **kwargs):
        pytest.fail("Synthetic config loading must not consult fact databases")

    monkeypatch.setattr(config_loader_v2, "load_program_facts", forbidden_facts)


def test_matching_cache_root_saves_precomputed_seed_without_reloading_bundle(
    tmp_path: Path, synthetic_sources: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sealed = synthetic_sources
    _write_sealed_v2_bundle(sealed, monkeypatch)
    cache = tmp_path / "cache"
    report_test_setup.prime_local_source_cache(sealed, cache)
    seed = report_test_setup._SEED_OVERRIDES["demo_weekly"]

    def forbidden_reload(*args, **kwargs):
        pytest.fail("Matching cache root must use its already computed seed")

    monkeypatch.setattr(report_test_setup, "load_bundle_with_mode", forbidden_reload)
    monkeypatch.setattr(report_test_setup, "discover_report_editions", forbidden_reload)
    report_test_setup.reset_overrides_to_seed_state(cache / "reports")

    assert report_test_setup.get_source_root(sealed) == cache
    assert load_overrides("demo_weekly", reports_root=cache / "reports") == seed
    assert seed.scorecards[0].name == "Demo Scorecard"
    assert seed.scorecards[0].dimensions[0].name == "Demo Dimension"
    assert seed.scorecards[0].dimensions[0].risk is None
    assert (cache / "programs" / "demo" / "overrides" / "issue_000.yaml").is_file()
    assert not (sealed / "programs" / "demo" / "overrides").exists()


def test_staged_v2_seed_rebuilds_from_actual_workspace_bundle(
    tmp_path: Path, synthetic_sources: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sealed = synthetic_sources
    _write_sealed_v2_bundle(sealed, monkeypatch)
    cache = tmp_path / "cache"
    report_test_setup.prime_local_source_cache(sealed, cache)
    cached_seed = report_test_setup._SEED_OVERRIDES["demo_weekly"]
    reports = report_test_setup.stage_v2_report_workspace(
        sealed, tmp_path / "workspace", program_names=("demo",), edition_names=("demo_weekly",),
    )
    scorecards_path = reports.parent / "programs" / "demo" / "scorecards.yaml"
    scorecards = yaml.safe_load(scorecards_path.read_text(encoding="utf-8"))
    scorecards["scorecards"][0]["name"] = "Workspace Scorecard"
    scorecards["scorecards"][0]["dimensions"][0]["name"] = "Workspace Dimension"
    scorecards_path.write_text(yaml.safe_dump(scorecards, sort_keys=False), encoding="utf-8")

    bundle = report_test_setup.load_bundle_with_mode(
        "demo_weekly", reports_root=reports, programs_root=reports.parent / "programs",
    )
    assert bundle.mode == "v2"
    assert bundle.bundle.config.scorecards[0].name == "Workspace Scorecard"
    report_test_setup.reset_overrides_to_seed_state(reports)

    saved = load_overrides("demo_weekly", reports_root=reports)
    assert saved is not None
    assert saved.issue_number is None
    assert saved.top_3_now == ()
    assert saved.scorecards[0].name == "Workspace Scorecard"
    assert saved.scorecards[0].dimensions[0].name == "Workspace Dimension"
    assert saved.scorecards[0].dimensions[0].risk is None
    assert cached_seed.scorecards[0].name == "Demo Scorecard"
    assert report_test_setup._SEED_OVERRIDES["demo_weekly"] is cached_seed
    assert not (cache / "programs" / "demo" / "overrides").exists()
    assert not (sealed / "programs" / "demo" / "overrides").exists()


def test_already_primed_cache_preserves_original_owner_and_seed(
    tmp_path: Path, synthetic_sources: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient, sealed = synthetic_sources
    cache = tmp_path / "cache"
    report_test_setup.prime_local_source_cache(ambient, cache)
    seeds = report_test_setup._SEED_OVERRIDES
    before = {path.relative_to(cache): path.read_bytes() for path in cache.rglob("*") if path.is_file()}

    def forbidden_reprime(*args, **kwargs):
        pytest.fail("An already primed cache must not copy or rebuild another source")

    monkeypatch.setattr(report_test_setup.shutil, "copytree", forbidden_reprime)
    monkeypatch.setattr(report_test_setup, "_build_seed_overrides", forbidden_reprime)
    unused_cache = tmp_path / "second-cache"
    report_test_setup.prime_local_source_cache(sealed, unused_cache)

    assert report_test_setup._LOCAL_SOURCE_CACHE == cache
    assert report_test_setup._LOCAL_SOURCE_ROOT == ambient.resolve()
    assert report_test_setup._SEED_OVERRIDES is seeds
    assert report_test_setup.get_source_root(ambient) == cache
    assert report_test_setup.get_source_root(sealed) == sealed
    assert not unused_cache.exists()
    assert {path.relative_to(cache): path.read_bytes() for path in cache.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("include_runtime_state", [False, True])
def test_foreign_cache_does_not_change_explicit_runtime_selection(
    tmp_path: Path, synthetic_sources: tuple[Path, Path], include_runtime_state: bool,
) -> None:
    ambient, sealed = synthetic_sources
    program = sealed / "programs" / "acme"
    (program / "journal").mkdir()
    (program / "journal" / "runtime.yaml").write_text("synthetic runtime\n", encoding="utf-8")
    report_test_setup.prime_local_source_cache(ambient, tmp_path / "cache")

    reports = report_test_setup.stage_v2_report_workspace(
        sealed, tmp_path / "workspace", include_runtime_state=include_runtime_state,
    )

    runtime = reports.parent / "programs" / "acme" / "journal" / "runtime.yaml"
    assert runtime.exists() is include_runtime_state


def test_collection_hooks_block_dotenv_before_a_consumer_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _root_functions("pytest_configure")
    patch = pytest.MonkeyPatch()
    namespace["_DOTENV_PATCH"] = patch
    marker_calls: list[tuple[str, str]] = []
    config = SimpleNamespace(addinivalue_line=lambda *args: marker_calls.append(args))
    attempted: list[Path] = []

    def forbidden_load(path: Path, **_kwargs) -> bool:
        attempted.append(path)
        raise AssertionError("Collection must not load dotenv files")

    monkeypatch.setattr(dotenv, "load_dotenv", forbidden_load)
    try:
        namespace["pytest_configure"](config)
        # Execute the same import-and-call pattern used by cli.py, not its startup.
        consumer: dict[str, Any] = {"__file__": str(tmp_path / "consumer.py"), "Path": Path}
        exec(
            "from dotenv import load_dotenv\n"
            "loaded = load_dotenv(Path(__file__).parent / '.env', override=False)\n",
            consumer,
        )
        assert consumer["loaded"] is False
        assert not attempted
        assert marker_calls == [
            ("markers", "integration: mark test as requiring live Azure DevOps connectivity")
        ]
        teardown = _root_functions("pytest_unconfigure")
        teardown["_DOTENV_PATCH"] = patch
        teardown["pytest_unconfigure"](config)
    finally:
        patch.undo()
    assert dotenv.load_dotenv is forbidden_load

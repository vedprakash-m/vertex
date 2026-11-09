# Adapted from Shiproom tests/conftest.py

from __future__ import annotations

import os

import typer.core

# Typer renders CLI errors through Rich panels (boxed, line-wrapped, ANSI-colored).
# That rendering is environment-sensitive — it depends on terminal width and on the
# FORCE_COLOR variable that CI runners (GitHub Actions) export — so plain-substring
# assertions on CLI error messages pass on a local dev shell but break under CI:
# ANSI codes get interspersed in the message and long messages wrap across panel
# lines. Disable Rich rendering so typer falls back to plain click error output,
# which prints the message unwrapped and uncolored — deterministic across platforms.
# Must run at conftest import, before the Typer app is invoked.
typer.core.HAS_RICH = False
os.environ.pop("FORCE_COLOR", None)
os.environ["NO_COLOR"] = "1"

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
_DOTENV_PATCH = pytest.MonkeyPatch()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Update golden files with current output.",
    )
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run live integration tests that require Azure DevOps connectivity.",
    )


def pytest_configure(config: pytest.Config) -> None:
    import dotenv

    # CLI modules load dotenv during collection, before autouse fixtures can run.
    _DOTENV_PATCH.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    config.addinivalue_line(
        "markers",
        "integration: mark test as requiring live Azure DevOps connectivity",
    )


def pytest_unconfigure(config: pytest.Config) -> None:
    _DOTENV_PATCH.undo()


@pytest.fixture(autouse=True)
def reset_output_subdir_cache() -> None:
    """Reset the edition_resolver output-subdir cache after each test.

    Prevents VERTEX_OUTPUT_SUBDIR set in one test from leaking into the next via
    the module-level _output_subdir_cached variable in edition_resolver.
    """
    yield
    from src.core.edition_resolver import _reset_output_subdir_cache
    _reset_output_subdir_cache()


@pytest.fixture
def isolated_cli_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "_maybe_register_tier_decision_sink",
        "_maybe_run_scheduled_db_maintenance",
        "_maybe_run_session_catchup",
    ):
        monkeypatch.setattr(f"cli.{name}", lambda *_args, **_kwargs: None)


# Data-dependent tests that bypass the guarded `stage_v2_report_workspace` helper
# and assume private programs/acme or editions/acme_weekly config directly. These are
# gitignored, so on the fresh-clone CI these tests must skip rather than fail. (Tests
# that DO go through stage_v2_report_workspace already skip themselves; tests that own
# a skipif predicate are fixed at the predicate. This list covers the remainder.)
_REQUIRES_LIVE_PROGRAM_DATA: frozenset[str] = frozenset(
    {
        # tests/unit/test_commands_list.py
        "test_root_skip_issue_records_next_issue",
        "test_root_skip_issue_honors_vertex_default_edition",
        "test_root_skip_issue_falls_back_to_legacy_default_edition",
        # tests/unit/test_debt_baseline.py
        "test_build_debt_baseline_collects_expected_schema",
        # tests/golden/test_remediation_artifacts.py
        "test_quality_and_remediation_json_snapshots",
        # tests/contracts/test_phase4_step4_gold_corpus_scaffold.py (corpus is gitignored)
        "test_gold_corpus_directories_exist",
        "test_gold_corpus_has_minimum_case_counts",
        # tests/contracts/test_schema_versions.py
        "test_schema_versions_match_contract",
    }
)
_REQUIRES_GOLD_CORPUS: frozenset[str] = frozenset(
    {
        "test_gold_corpus_directories_exist",
        "test_gold_corpus_has_minimum_case_counts",
    }
)
_REQUIRES_FABRIKAM_EDITION: frozenset[str] = frozenset(
    {
        "test_root_skip_issue_honors_vertex_default_edition",
        "test_root_skip_issue_falls_back_to_legacy_default_edition",
    }
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Skip live-data-dependent tests when private programs/acme data is absent
    # (fresh-clone CI). Runs regardless of the --run-integration flag.
    from tests.support.data_guards import live_program_data_available

    if not live_program_data_available():
        skip_no_data = pytest.mark.skip(
            reason="Requires local programs/acme data (absent on fresh-clone CI)"
        )
        for item in items:
            if item.name.split("[")[0] in _REQUIRES_LIVE_PROGRAM_DATA:
                item.add_marker(skip_no_data)

    gold_corpus = REPO_ROOT / "programs" / "acme" / "gold_corpus"
    fabrikam_edition = REPO_ROOT / "programs" / "fabrikam" / "editions" / "fabrikam_weekly.yaml"
    for item in items:
        test_name = item.name.split("[")[0]
        if test_name in _REQUIRES_GOLD_CORPUS and not gold_corpus.is_dir():
            item.add_marker(pytest.mark.skip(reason="Requires local programs/acme/gold_corpus data"))
        if test_name in _REQUIRES_FABRIKAM_EDITION and not fabrikam_edition.is_file():
            item.add_marker(pytest.mark.skip(reason="Requires local programs/fabrikam edition data"))

    if config.getoption("--run-integration"):
        return
    skip_integration = pytest.mark.skip(reason="need --run-integration to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def fixtures_dir(repo_root: Path) -> Path:
    fixtures = repo_root / "tests" / "fixtures"
    if not fixtures.exists():
        pytest.skip("Requires local fixture data")
    return fixtures

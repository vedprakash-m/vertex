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


@pytest.fixture(scope="session", autouse=True)
def _prime_local_repo_cache(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Copy Q: drive source data to a local C: temp directory once per session.

    Prevents every test from doing a 72 MB copytree from the network drive.
    After this runs, stage_v2_report_workspace() reads from the local cache.
    """
    from tests.support.report_test_setup import prime_local_source_cache

    cache_root = tmp_path_factory.mktemp("repo_source_cache", numbered=False)
    prime_local_source_cache(REPO_ROOT, cache_root)


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
    config.addinivalue_line(
        "markers",
        "integration: mark test as requiring live Azure DevOps connectivity",
    )


@pytest.fixture(autouse=True)
def reset_output_subdir_cache() -> None:
    """Reset the edition_resolver output-subdir cache after each test.

    Prevents VERTEX_OUTPUT_SUBDIR set in one test from leaking into the next via
    the module-level _output_subdir_cached variable in edition_resolver.
    """
    yield
    from src.core.edition_resolver import _reset_output_subdir_cache
    _reset_output_subdir_cache()


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
        # tests/unit/test_commands_metric.py
        "test_admin_metric_status_defaults_to_repo_vertex_db",
        "test_admin_metric_validate_command_marks_wiql_binding_validated",
        # tests/unit/test_commands_report.py
        "test_generate_report_draft_readiness_uses_visible_newsletter_items",
        "test_report_cli_normalizes_sections_and_passes_them_to_generator",
        "test_report_cli_returns_exit_code_4_on_ado_timeout",
        "test_generate_report_draft_computes_readiness_from_v2_signal_context",
        # tests/unit/test_commands_quickstart.py
        "test_hypothesis_quickstart_requires_binding_inputs_for_missing_binding",
        # tests/unit/test_debt_baseline.py
        "test_build_debt_baseline_collects_expected_schema",
        # tests/golden/test_remediation_artifacts.py
        "test_quality_and_remediation_json_snapshots",
        # tests/contracts/test_phase4_step4_gold_corpus_scaffold.py (corpus is gitignored)
        "test_gold_corpus_directories_exist",
        "test_gold_corpus_has_minimum_case_counts",
        # tests/contracts/test_ai_disabled_write_paths.py — belt-and-suspenders alongside
        # the stage_v2_report_workspace guard; programs/acme/ can appear as an incomplete
        # directory on CI runners (runtime dirs only), causing the copy to succeed but
        # program.yaml to be absent.
        "test_report_disabled_mode_writes_no_ai_artifacts",
        "test_propose_disabled_mode_writes_no_ai_artifacts",
        "test_review_full_disabled_mode_writes_no_ai_artifacts",
        "test_decision_brief_disabled_mode_writes_no_ai_artifacts",
        "test_nudge_disabled_mode_writes_no_ai_artifacts",
        "test_prep_disabled_mode_writes_no_ai_artifacts",
        # tests/contracts/test_schema_versions.py
        "test_schema_versions_match_contract",
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

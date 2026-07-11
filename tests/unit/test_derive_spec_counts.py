from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "derive_spec_counts.py"
    spec = importlib.util.spec_from_file_location("derive_spec_counts", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def module():
    return _load_module()


class TestCollectedTests:
    """arch-fix.md Phase 0: the collection-count regex must survive a pytest
    wording change. pytest < 8 emits "collected N items"; pytest 9.x emits
    "N tests collected in X.XXs". Both must resolve, not silently -> -1."""

    def test_old_pytest_wording(self, module, monkeypatch, tmp_path):
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="collected 42 items\n", returncode=0),
        )
        assert module._count_collected_tests(tmp_path) == 42

    def test_new_pytest_wording(self, module, monkeypatch, tmp_path):
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="7812 tests collected in 6.91s\n", returncode=0),
        )
        assert module._count_collected_tests(tmp_path) == 7812

    def test_singular_test_wording(self, module, monkeypatch, tmp_path):
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="1 test collected in 0.01s\n", returncode=0),
        )
        assert module._count_collected_tests(tmp_path) == 1

    def test_unrecognized_output_returns_negative_one(self, module, monkeypatch, tmp_path):
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="no tests ran\n", returncode=0),
        )
        assert module._count_collected_tests(tmp_path) == -1

    def test_timeout_returns_negative_one(self, module, monkeypatch, tmp_path):
        import subprocess as real_subprocess

        def _raise(*a, **k):
            raise real_subprocess.TimeoutExpired(cmd="pytest", timeout=120)

        monkeypatch.setattr(module.subprocess, "run", _raise)
        assert module._count_collected_tests(tmp_path) == -1


class TestCliCommandCounting:
    """arch-fix.md Phase 0: the probe must point at the real entrypoint
    (root cli.py) and count both functional-form leaf commands and add_typer
    groups registered on the top-level `app`."""

    def test_counts_leaf_commands_and_groups(self, module, monkeypatch, tmp_path):
        fake_cli = textwrap.dedent(
            """
            app = typer.Typer()
            sub_app = typer.Typer()
            other_app = typer.Typer()

            app.add_typer(sub_app, name="ado")
            app.add_typer(other_app, name="admin")
            app.command("ask")(ask_command)
            app.command("gather")(gather_command)
            other_app.command("nested")(nested_command)
            """
        ).lstrip()
        cli_path = tmp_path / "cli.py"
        cli_path.write_text(fake_cli, encoding="utf-8")
        monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

        leaf_commands, command_groups = module._count_commands_in_cli()

        assert leaf_commands == 2  # ask, gather (not other_app's "nested")
        assert command_groups == 2  # ado, admin

    def test_missing_cli_returns_negative_one(self, module, monkeypatch, tmp_path):
        monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
        assert module._count_commands_in_cli() == (-1, -1)


def test_main_resolves_all_metrics_on_live_tree(module):
    """Regression guard: on the real repo, every metric should resolve (no -1),
    matching arch-fix.md's Phase-0 requirement that the probe not be broken."""
    exit_code = module.main(["--format", "json"])
    assert exit_code == 0

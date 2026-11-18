from __future__ import annotations

import importlib.util
import json
import re
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

    def test_collection_uses_active_interpreter(self, module, monkeypatch, tmp_path):
        def run(command, **kwargs):
            assert command == [sys.executable, "-m", "pytest", str(tmp_path), "-q", "--tb=no", "--co"]
            return SimpleNamespace(stdout="1 test collected in 0.01s\n", returncode=0)

        monkeypatch.setattr(module.subprocess, "run", run)
        assert module._count_collected_tests(tmp_path) == 1

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

    @pytest.mark.parametrize(
        "node_id",
        [
            "tests/test_counts.py::test_example[collected 42 items]",
            "tests/test_counts.py::test_example[42 tests collected in 1.00s]",
        ],
    )
    def test_collection_summary_is_not_confused_with_test_ids(
        self, module, monkeypatch, tmp_path, node_id
    ):
        output = f"{node_id}\n85/100 tests collected (15 deselected) in 1.00s\n"
        monkeypatch.setattr(
            module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=output, returncode=0)
        )
        assert module._count_collected_tests(tmp_path) == 100

    def test_unrecognized_output_returns_negative_one(self, module, monkeypatch, tmp_path):
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="no tests ran\n", returncode=0),
        )
        assert module._count_collected_tests(tmp_path) == -1

    @pytest.mark.parametrize(
        "stdout",
        ["collected 42 items\n", "42 tests collected in 1.00s\n"],
    )
    def test_collection_error_does_not_report_partial_count(
        self, module, monkeypatch, tmp_path, capsys, stdout
    ):
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout=stdout, returncode=2),
        )
        assert module._count_collected_tests(tmp_path) == -1
        assert module.main(["--format", "json"]) == 1
        captured = capsys.readouterr()
        assert '"collected_tests": -1' in captured.out
        assert "collected_tests" in captured.err

    def test_real_collection_error_is_not_reported_as_success(
        self, module, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
        (tmp_path / "test_good.py").write_text("def test_pass(): pass\n", encoding="utf-8")
        (tmp_path / "test_broken.py").write_text(
            "raise RuntimeError('synthetic collection failure')\n", encoding="utf-8"
        )
        run = module.subprocess.run
        results = []

        def record_run(*args, **kwargs):
            result = run(*args, **kwargs)
            results.append(result)
            return result

        monkeypatch.setattr(module.subprocess, "run", record_run)
        assert module._count_collected_tests(tmp_path) == -1
        assert results[0].returncode != 0
        assert "test_good.py::test_pass" in results[0].stdout
        assert re.search(
            r"collected\s+\d+\s+items|\d+\s+tests?\s+collected", results[0].stdout
        )

    def test_timeout_returns_negative_one(self, module, monkeypatch, tmp_path):
        import subprocess as real_subprocess

        def _raise(*a, **k):
            raise real_subprocess.TimeoutExpired(cmd="pytest", timeout=600)

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


def test_main_resolves_all_metrics_on_live_tree(module, monkeypatch, capsys):
    """Regression guard: on the real repo, every metric should resolve (no -1),
    matching arch-fix.md's Phase-0 requirement that the probe not be broken."""
    monkeypatch.setattr(module, "_count_collected_tests", lambda tests_root: 1)
    exit_code = module.main(["--format", "json"])
    assert exit_code == 0
    counts = json.loads(capsys.readouterr().out)
    assert counts["collected_tests"] == 1
    assert counts["zone_a_modules"] > 0
    assert counts["test_files"] > 0

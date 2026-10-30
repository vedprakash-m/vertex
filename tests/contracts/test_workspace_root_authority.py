from __future__ import annotations

import ast
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.core.exceptions import ConfigError


_REPO_ROOT = Path(__file__).resolve().parents[2]
_AUTHORITY = "src.core.workspace_paths"


@pytest.mark.parametrize("kind", ["relative", "checkout", "nested-checkout", "file"])
def test_new_program_root_refuses_unsafe_context(tmp_path: Path, kind: str) -> None:
    from src.core.workspace_paths import validate_new_programs_root

    root = tmp_path / "programs"
    if kind == "relative":
        root = Path("programs")
    elif kind == "checkout":
        (tmp_path / ".git").mkdir()
    elif kind == "nested-checkout":
        root.mkdir()
        (root / ".git").write_text("gitdir: synthetic", encoding="utf-8")
    else:
        root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ConfigError):
        validate_new_programs_root(root)


def test_new_program_root_resolves_private_links_and_rejects_checkout_links(tmp_path: Path) -> None:
    from src.core.workspace_paths import resolve_report_workspace_roots, validate_new_programs_root

    private = tmp_path / "private"
    private.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(private, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(private)], check=True, capture_output=True)
    assert validate_new_programs_root(link / "programs") == private / "programs"
    assert resolve_report_workspace_roots(link / "programs") == (
        private / "programs", private / "reports", private / "archive",
    )
    with pytest.raises(ConfigError):
        validate_new_programs_root(checkout / "programs")
    checkout_link = tmp_path / "checkout-link"
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(checkout_link), str(checkout)], check=True, capture_output=True)
    else:
        checkout_link.symlink_to(checkout, target_is_directory=True)
    with pytest.raises(ConfigError):
        validate_new_programs_root(checkout_link / "programs")
    outward_link = checkout / "private-link"
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(outward_link), str(private)], check=True, capture_output=True)
    else:
        outward_link.symlink_to(private, target_is_directory=True)
    with pytest.raises(ConfigError):
        validate_new_programs_root(outward_link / "programs")


def test_new_program_root_reports_resolution_and_storage_failure(monkeypatch, tmp_path: Path) -> None:
    from src.core.workspace_paths import validate_new_programs_root

    monkeypatch.setattr(os, "access", lambda *_args: False)
    with pytest.raises(ConfigError, match="accessible"):
        validate_new_programs_root(tmp_path / "programs")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unavailable")))
        with pytest.raises(ConfigError, match="resolve"):
            validate_new_programs_root(tmp_path / "programs")


def _root_owner_modules() -> list[str]:
    modules: list[str] = []
    for path in sorted((_REPO_ROOT / "src").rglob("*.py")):
        module = ".".join(path.relative_to(_REPO_ROOT).with_suffix("").parts)
        if module == _AUTHORITY:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "PROGRAMS_ROOT" for target in node.targets)
            or isinstance(node, ast.ImportFrom)
            and node.module == _AUTHORITY
            and any(alias.name == "PROGRAMS_ROOT" for alias in node.names)
            for node in tree.body
        ):
            modules.append(module)
    return modules


_IMPORT_PROBE = """
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile

tempfile.tempdir = os.getcwd()
def guard(event, arguments):
    if event in {"socket.connect", "socket.getaddrinfo", "sqlite3.connect",
                 "os.mkdir", "os.rename", "os.remove", "os.rmdir"}:
        raise AssertionError("Root import must not perform network, DB or filesystem mutation: " + event)
    if event == "open" and arguments[0] != os.devnull and arguments[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
        raise AssertionError("Root import must not open files for writing")

sys.addaudithook(guard)
with open(os.devnull, "w") as sink:
    sink.write("probe")
try:
    open("forbidden-write", "w")
except AssertionError:
    pass
else:
    raise AssertionError("Root import guard must reject filesystem writes")
modules, first, expected_text, changed_environment = json.loads(sys.argv[1])
expected = Path(expected_text)
initial = importlib.import_module(first)
assert initial.PROGRAMS_ROOT == expected, (first, initial.PROGRAMS_ROOT, expected)
captured = initial.PROGRAMS_ROOT
os.environ["VERTEX_PROGRAMS_ROOT"] = changed_environment
for name in modules:
    module = importlib.import_module(name)
    assert module.PROGRAMS_ROOT is captured, (name, module.PROGRAMS_ROOT, captured)

from src.core import config_loader, config_loader_v2, edition_resolver, journal, program_paths
from src.core import knowledge_store, milestone_engine, program_fact_store, reality_store
from src.core.ledger import event_log

for function in (config_loader.load_report_config, config_loader_v2.load_edition_bundle,
                 edition_resolver.resolve_edition_paths, journal.get_program_journal_dir,
                 program_paths.get_runtime_dir, knowledge_store.load_program_knowledge, event_log.read_events):
    assert inspect.signature(function).parameters["programs_root"].default is captured, function.__name__
assert program_paths.get_runtime_dir("fixture") == expected / "fixture" / "runtime"
assert journal.get_program_journal_dir("fixture").is_relative_to(expected)
assert program_fact_store._resolve_fact_db_root(captured) is None
assert milestone_engine._resolve_fact_db_root(captured) is None
assert reality_store.get_program_reality_db_path("fixture") == expected.parent / "vertex-db" / "fixture" / "vertex.sqlite3"
print("Shared root, bound defaults and pure path projections verified for", len(modules), "owners.")
"""


@pytest.mark.parametrize("first", [
    "src.core.config_loader", "src.core.edition_resolver", "src.core.ledger.event_log",
])
@pytest.mark.parametrize("configured", [False, True])
def test_root_owners_share_one_captured_authority_without_io(
    tmp_path: Path, first: str, configured: bool,
) -> None:
    selected = tmp_path / "private workspace" / "programs"
    expected = selected if configured else _REPO_ROOT / "programs"
    environment = os.environ.copy()
    environment.pop("VERTEX_DB_PATH", None)
    if configured:
        environment["VERTEX_PROGRAMS_ROOT"] = str(selected)
    else:
        environment.pop("VERTEX_PROGRAMS_ROOT", None)
    environment["PYTHONPATH"] = str(_REPO_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    before = tuple(tmp_path.rglob("*"))

    result = subprocess.run(
        [
            sys.executable, "-B", "-c", _IMPORT_PROBE,
            json.dumps([_root_owner_modules(), first, str(expected), str(tmp_path / "other" / "programs")]),
        ],
        cwd=tmp_path, env=environment, capture_output=True, text=True, check=False, timeout=45,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Shared root, bound defaults and pure path projections verified" in result.stdout
    assert tuple(tmp_path.rglob("*")) == before


def test_programs_root_declarations_cannot_diverge_again() -> None:
    violations: list[str] = []
    for path in sorted((_REPO_ROOT / "src").rglob("*.py")):
        if path == _REPO_ROOT / "src" / "core" / "workspace_paths.py":
            continue
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            if any(isinstance(target, ast.Name) and target.id == "PROGRAMS_ROOT" for target in targets):
                violations.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")
    assert not violations, f"Import PROGRAMS_ROOT from {_AUTHORITY}, do not redeclare it: {violations}"


@pytest.mark.parametrize("value", ["", "  ", "relative", r"C:relative", r"\root-relative", "bad\nroot"])
def test_invalid_configured_root_is_explicit_not_cwd(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    authority = importlib.import_module(_AUTHORITY)
    monkeypatch.setenv("VERTEX_PROGRAMS_ROOT", value)
    with pytest.raises(ConfigError, match="VERTEX_PROGRAMS_ROOT"):
        authority._resolve_programs_root()


def test_unset_and_explicit_absolute_root_resolution_do_not_create_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = importlib.import_module(_AUTHORITY)
    monkeypatch.delenv("VERTEX_PROGRAMS_ROOT", raising=False)
    assert authority._resolve_programs_root() == _REPO_ROOT / "programs"
    selected = tmp_path / "not-created" / "programs"
    monkeypatch.setenv("VERTEX_PROGRAMS_ROOT", str(selected))
    assert authority._resolve_programs_root() == selected
    assert not selected.exists()


@pytest.mark.parametrize("configured", [False, True])
def test_report_and_confirm_preflights_preserve_runtime_roots_in_installed_process(
    tmp_path: Path, configured: bool,
) -> None:
    workspace = tmp_path / "external"
    programs_root = workspace / "programs"
    programs_root.mkdir(parents=True)
    decoy = tmp_path / "decoy"
    for name in ("reports", "archive"):
        root = decoy / name
        root.mkdir(parents=True)
        (root / "marker.txt").write_text("unchanged", encoding="utf-8")
    before = {path: path.read_bytes() for path in decoy.rglob("*") if path.is_file()}
    script = """
import dotenv
import json
from typer.testing import CliRunner
import cli
from src.commands import confirm, report

dotenv.load_dotenv = lambda *_args, **_kwargs: False
for name in ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance",
             "_maybe_run_session_catchup"):
    setattr(cli, name, lambda *_args, **_kwargs: None)
seen = {}

def stop_report(edition, *, reports_root, programs_root):
    seen["report"] = [str(programs_root), str(reports_root)]
    raise RuntimeError("report reached generation")

def stop_confirm(edition, *, archive_root):
    seen["confirm"] = [str(archive_root)]
    raise RuntimeError("confirm reached archive read")

report.load_bundle_with_mode = stop_report
confirm._resolve_edition_paths = lambda *_args, **_kwargs: None
confirm.read_archive_index = stop_confirm
runner = CliRunner()
report_result = runner.invoke(cli.app, [
    "report", "--edition", "fixture", "--issue", "80", "--dry-run", "--offline",
])
confirm_result = runner.invoke(cli.app, [
    "confirm", "--edition", "fixture", "--issue", "7", "--dry-run",
])
assert str(report_result.exception) == "report reached generation", report_result.output
assert str(confirm_result.exception) == "confirm reached archive read", confirm_result.output
from src.core.config_loader import REPORTS_ROOT, SCHEMAS_ROOT
from src.core.snapshot_store import ARCHIVE_ROOT
from src.core.workspace_paths import REPO_ROOT as repo_root
assert REPORTS_ROOT == repo_root / "reports"
assert SCHEMAS_ROOT == repo_root / "reports" / "schemas"
assert ARCHIVE_ROOT == repo_root / "archive"
print(json.dumps(seen))
"""
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if configured:
        environment["VERTEX_PROGRAMS_ROOT"] = str(programs_root)
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        if name in os.environ:
            environment[name] = os.environ[name]

    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=decoy,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    expected_programs = programs_root if configured else _REPO_ROOT / "programs"
    expected_reports = workspace / "reports" if configured else _REPO_ROOT / "reports"
    expected_archive = workspace / "archive" if configured else _REPO_ROOT / "archive"
    assert json.loads(result.stdout) == {
        "report": [str(expected_programs), str(expected_reports)],
        "confirm": [str(expected_archive)],
    }
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("kind", ["checkout", "installation", "linked-checkout"])
def test_external_report_preflight_rejects_unsafe_program_roots_before_store_access(
    tmp_path: Path, kind: str,
) -> None:
    from src.core.workspace_paths import resolve_report_workspace_roots

    root = tmp_path / "programs"
    if kind == "checkout":
        (tmp_path / ".git").mkdir()
    elif kind == "installation":
        root = _REPO_ROOT / "programs"
    else:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir()
        root = tmp_path / "linked" / "programs"
        try:
            (tmp_path / "linked").symlink_to(checkout, target_is_directory=True)
        except OSError:
            if sys.platform != "win32":
                raise
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(tmp_path / "linked"), str(checkout)],
                check=True,
                capture_output=True,
            )

    with pytest.raises(ConfigError):
        resolve_report_workspace_roots(programs_root=root)

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

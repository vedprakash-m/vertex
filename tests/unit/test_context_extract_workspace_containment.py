from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


_CHILD = r"""
import json
import os
from pathlib import Path
import sys

reads, effects, staged = [], [], []
try:
    import cli
    import src.commands.context as command
    from typer.testing import CliRunner
except Exception as error:
    print(json.dumps({"import_error": str(error), "reads": [], "effects": [], "staged": []}))
    raise SystemExit(0)

def startup(*args, **kwargs):
    effects.append("startup")
    raise AssertionError("Startup must not precede extraction admission")

for name in ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance",
             "_maybe_run_session_catchup"):
    setattr(cli, name, startup)

def stage(program, issue, proposals, *, programs_root):
    if os.environ["CASE"] != "stage":
        effects.append("staging")
        raise AssertionError("Preview/invalid input must not stage")
    staged.append({
        "program": program, "issue": issue, "root": str(programs_root),
        "values": sorted(proposal.source_value for proposal in proposals),
    })
    return proposals

command.stage_extracted_proposals = stage
if os.environ["CASE"] == "captured":
    os.environ["VERTEX_PROGRAMS_ROOT"] = os.environ["DECOY_ROOT"]

active = True
def audit(event, args):
    if not active:
        return
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).absolute()
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            effects.append("write:" + str(path))
            raise AssertionError("Extraction must not write")
        if path.suffix in {".yaml", ".json", ".jsonl"}:
            reads.append(str(path))
    elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "sqlite3.connect"}:
        effects.append(event)
        raise AssertionError("Extraction must not mutate or open a database")

sys.addaudithook(audit)
args = json.loads(os.environ["CLI_ARGS"])
sys.argv = ["vertex", *args]
result = CliRunner().invoke(cli.app, args)
active = False
print(json.dumps({
    "code": result.exit_code, "output": result.output,
    "exception": repr(result.exception), "reads": reads, "effects": effects, "staged": staged,
}))
"""


def _seed(programs: Path, *, empty: bool = False, duplicate: bool = False) -> None:
    program = programs / ("other" if duplicate else "synthetic")
    (program / "editions").mkdir(parents=True, exist_ok=True)
    (program / "editions" / "synthetic_weekly.yaml").write_text(
        f"program_id: {program.name}\n", encoding="utf-8",
    )
    if duplicate:
        return
    (program / "overrides").mkdir(exist_ok=True)
    marker = "AMBIENT" if programs.parent.name == "installation" else "SELECTED"
    payload = "issue_number: 1\n"
    if not empty:
        payload += (
            f"top_3_now:\n  - type: decision\n    text: {marker}_DECISION\n"
            f"scorecards:\n  Delivery:\n    {marker}_DIMENSION:\n      risk: high\n"
        )
    (program / "overrides" / "issue_001.yaml").write_text(payload, encoding="utf-8")


@pytest.fixture(scope="module")
def installation(tmp_path_factory: pytest.TempPathFactory) -> Path:
    source = Path(__file__).resolve().parents[2]
    target = tmp_path_factory.mktemp("context-extract-install") / "installation"
    shutil.copytree(source / "src", target / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(source / "vertex" / "policies", target / "vertex" / "policies")
    shutil.copyfile(source / "cli.py", target / "cli.py")
    _seed(target / "programs")
    return target


def _run(installation: Path, root: Path | str | None, case: str, args: list[str]) -> dict:
    env = dict(os.environ)
    for name in tuple(env):
        if name.startswith("VERTEX_"):
            env.pop(name)
    if root is not None:
        env["VERTEX_PROGRAMS_ROOT"] = str(root)
    env.update(
        CASE=case, DECOY_ROOT=str(installation / "programs"), CLI_ARGS=json.dumps(args),
        PYTHONDONTWRITEBYTECODE="1",
    )
    result = subprocess.run(
        [sys.executable, "-c", _CHILD], cwd=installation, env=env,
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _bytes(*homes: Path) -> dict[str, str]:
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for home in homes for path in home.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("case", ("selected", "explicit", "captured", "empty", "legacy", "stage"))
def test_actual_extract_selected_decisions_risks_and_empty(
    tmp_path: Path, installation: Path, case: str,
) -> None:
    root = tmp_path / "selected" / "programs"
    _seed(root, empty=case == "empty")
    args = ["context", "extract", "--edition", "synthetic_weekly", "--issue", "1"]
    if case != "stage":
        args.append("--dry-run")
    if case == "explicit":
        args += ["--programs-root", str(root), "--reports-root", str(root.parent / "reports")]
    before = _bytes(root, installation / "programs")
    result = _run(installation, None if case == "legacy" else root, case, args)
    assert result["code"] == 0, result
    assert not result["effects"], result
    assert _bytes(root, installation / "programs") == before
    if case == "empty":
        assert "extracted 0 proposal(s)" in result["output"]
    elif case == "stage":
        assert result["staged"] == [{
            "program": "synthetic", "issue": 1, "root": str(root),
            "values": ["SELECTED_DECISION", "high"],
        }]
        assert "Staged 2 extracted proposal(s)" in result["output"]
    else:
        marker = "AMBIENT" if case == "legacy" else "SELECTED"
        assert f"{marker}_DECISION" in result["output"]
        assert f"{marker}_DIMENSION.dimension_risk_level | high | high" in result["output"]
        assert ("SELECTED" if marker == "AMBIENT" else "AMBIENT") not in result["output"]
    if case != "legacy":
        assert not any(Path(path).is_relative_to(installation / "programs") for path in result["reads"])


@pytest.mark.parametrize(
    "case, diagnostic",
    (
        ("installation", "outside installation assets"),
        ("git", "outside every Git checkout"),
        ("linked-git", "outside every Git checkout"),
        ("relative", "absolute path"),
        ("drive-relative", "absolute path"),
        ("blank", "nonempty"),
        ("conflict", "Conflicting report workspace roots"),
        ("explicit-installation", "outside installation assets"),
        ("missing", "Unknown edition"),
        ("ambiguous", "Ambiguous edition"),
    ),
)
def test_actual_extract_invalid_selection_before_reads_startup_and_effects(
    tmp_path: Path, installation: Path, case: str, diagnostic: str,
) -> None:
    root: Path | str = tmp_path / "selected" / "programs"
    _seed(Path(root))
    args = ["context", "extract", "--edition", "synthetic_weekly", "--issue", "1"]
    if case == "installation":
        root = installation / "programs"
    elif case in {"git", "linked-git"}:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").write_text("gitdir: synthetic", encoding="utf-8")
        if case == "linked-git":
            link = tmp_path / "linked"
            if sys.platform == "win32":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(checkout)],
                    check=True, capture_output=True,
                )
            else:
                link.symlink_to(checkout, target_is_directory=True)
            root = link / "programs"
        else:
            root = checkout / "programs"
    elif case in {"relative", "drive-relative", "blank"}:
        root = {"relative": "programs", "drive-relative": "C:programs", "blank": ""}[case]
    elif case == "conflict":
        args += ["--reports-root", str(installation / "reports")]
    elif case == "explicit-installation":
        args += ["--programs-root", str(installation / "programs")]
    elif case == "missing":
        (Path(root) / "synthetic" / "editions" / "synthetic_weekly.yaml").unlink()
    elif case == "ambiguous":
        _seed(Path(root), duplicate=True)
    before = _bytes(tmp_path / "selected", installation / "programs")
    result = _run(installation, root, case, args)
    assert diagnostic in result.get("import_error", result.get("output", "")), result
    if "code" in result:
        assert result["code"] == 2, result
    assert not result["reads"], result
    assert not result["effects"] and not result["staged"], result
    assert _bytes(tmp_path / "selected", installation / "programs") == before


def test_other_context_route_keeps_startup(installation: Path, tmp_path: Path) -> None:
    root = tmp_path / "programs"
    _seed(root)
    result = _run(
        installation, root, "proposals",
        ["context", "proposals", "--edition", "synthetic_weekly"],
    )
    assert result["effects"] == ["startup"], result
    assert result["code"] == 1, result


@pytest.mark.parametrize("case", ("selected", "conflict", "ambiguous"))
def test_in_process_explicit_pair_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    import cli
    import src.commands.context as command
    from typer.testing import CliRunner

    root = tmp_path / "selected" / "programs"
    _seed(root)
    if case == "ambiguous":
        _seed(root, duplicate=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("Extraction preview must not reach startup or staging")

    for name in (
        "_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance",
        "_maybe_run_session_catchup",
    ):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(command, "stage_extracted_proposals", forbidden)
    args = [
        "context", "extract", "--edition", "synthetic_weekly", "--issue", "1",
        "--dry-run", "--programs-root", str(root), "--reports-root",
        str(tmp_path / ("decoy" if case == "conflict" else "selected") / "reports"),
    ]
    before = _bytes(root)
    result = CliRunner().invoke(cli.app, args)
    assert _bytes(root) == before
    assert result.exit_code == (0 if case == "selected" else 2), result.output + repr(result.exception)
    assert ("SELECTED_DECISION" if case == "selected" else
            "Conflicting report workspace roots" if case == "conflict" else "Ambiguous edition") in result.output

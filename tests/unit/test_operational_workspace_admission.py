from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("command", ("freshness", "prep"))
@pytest.mark.parametrize("selection", ("relative", "blank", "installation", "checkout", "linked-checkout"))
def test_freshness_prep_installed_unsafe_roots_fail_before_effects(
    tmp_path: Path, command: str, selection: str,
) -> None:
    root = tmp_path / "programs"
    if selection == "relative":
        root = Path("programs")
    elif selection == "blank":
        root = ""
    elif selection == "installation":
        root = Path(__file__).resolve().parents[2] / "programs"
    else:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").write_text("gitdir: synthetic", encoding="utf-8")
        root = checkout / "programs"
        if selection == "linked-checkout":
            link = tmp_path / "linked"
            if sys.platform == "win32":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(checkout)],
                    check=True, capture_output=True,
                )
            else:
                link.symlink_to(checkout, target_is_directory=True)
            root = link / "programs"
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    result = subprocess.run(
        [sys.executable, "-c", """
import cli
def forbidden(*args, **kwargs):
    raise AssertionError("Unsafe workspace reached startup or generation")
for name in ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"):
    setattr(cli, name, forbidden)
from src.commands import freshness, prep
freshness.generate_freshness_report = forbidden
prep.generate_prep_brief = forbidden
cli.app()
""", "--no-catchup", command, "--edition", "fixture"],
        cwd=tmp_path, env=dict(os.environ, VERTEX_PROGRAMS_ROOT=str(root)),
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode in (1, 2)
    assert "workspace" in result.stderr or "VERTEX_PROGRAMS_ROOT" in result.stderr
    assert "Unsafe workspace reached" not in result.stderr
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before

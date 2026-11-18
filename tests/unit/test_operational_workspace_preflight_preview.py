from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.test_operational_workspace_preflight_cli import (
    freshness_prep_external_homes,
)


@pytest.mark.parametrize("command", ("freshness", "prep"))
@pytest.mark.parametrize("entrypoint", ("preflight", "direct", "cli", "legacy", "captured"))
def test_freshness_prep_actual_external_and_nested_legacy(
    freshness_prep_external_homes, command: str, entrypoint: str,
) -> None:
    import hashlib
    import json
    import os
    import subprocess
    import sys

    selected, cwd, installation = freshness_prep_external_homes
    home = installation if entrypoint == "legacy" else selected

    def protected():
        roots = [cwd, selected / "programs" / "acme" / "archive",
                 installation / "programs" / "acme" / "archive"]
        if home != installation:
            roots.append(installation / "programs")
        return {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for root in roots for path in root.rglob("*") if path.is_file()
        }

    before = protected()
    env = dict(os.environ)
    env.pop("VERTEX_PROGRAMS_ROOT", None)
    env["PYTHONPATH"] = os.pathsep.join((
        str(installation), str(Path(__file__).resolve().parents[2]), env.get("PYTHONPATH", ""),
    ))
    if entrypoint != "legacy":
        env["VERTEX_PROGRAMS_ROOT"] = str(selected / "programs")
    code = f"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from src.commands import {command} as command
from src.ai.ai_mode import AIMode, set_ai_mode
set_ai_mode(AIMode.DISABLED)
from tests.unit.test_commands_freshness import _sample_items
if {entrypoint!r} == "captured":
    os.environ["VERTEX_PROGRAMS_ROOT"] = {str(cwd / "programs")!r}
if {command!r} == "freshness":
    command._load_live_freshness_items = lambda bundle, timestamp, since: (_sample_items(timestamp), 0)
    def forbidden_notification(*args, **kwargs):
        raise AssertionError("Dry-run must not record a notification")
    command.append_confirmed_notify_run = forbidden_notification
else:
    import src.core.engms_content
    src.core.engms_content.fetch_engms_page_summary = lambda *args, **kwargs: None
if {entrypoint!r} == "preflight":
    command._preflight_{command}_workspace()
elif {entrypoint!r} == "cli":
    from cli import app
    app()
else:
    if {command!r} == "freshness":
        result = command.generate_freshness_report(
            edition_name="acme_weekly", notify=True, dry_run=True,
            as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        )
        assert result.notify_previews
        path = result.md_path
    else:
        result = command.generate_prep_brief(
            edition_name="example_lt_deck",
            as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        )
        path = result.markdown_path
    assert result.issue_number == 1
    assert path.is_relative_to(Path({str(home / "programs")!r})), path
    assert path.exists()
    print(json.dumps({{"issue_number": result.issue_number, "path": str(path)}}))
"""
    arguments = []
    if entrypoint == "cli":
        arguments = [
            "--no-catchup", command, "--edition",
            "acme_weekly" if command == "freshness" else "example_lt_deck",
            "--format", "json",
        ]
        if command == "freshness":
            arguments.extend(["--notify", "--dry-run"])
    result = subprocess.run(
        [sys.executable, "-c", code, *arguments], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert protected() == before
    assert result.returncode in ((0, 2, 3) if command == "freshness" and entrypoint == "cli" else (0,)), result.stderr
    if entrypoint != "preflight":
        payload = json.loads(result.stdout)
        assert payload["issue_number"] == 1
    assert not list(home.rglob("notify_runs.jsonl"))

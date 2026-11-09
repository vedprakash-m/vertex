from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows legacy console encoding")
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (("--help",), b"capture-lt-deck"),
        (("capture-lt-deck", "--help"), b"--edition"),
        (("ingest-update", "--help"), b"--edition"),
        (("calibration", "--help"), b"edit-distance-trend"),
        (("calibration", "edit-distance-trend", "--help"), b"--window"),
        (("privacy", "show", "--help"), b"--section"),
        (("rev", "--help"), b"rotate-processed"),
        (("rev", "rotate-processed", "--help"), b"--max-age-days"),
    ],
)
def test_cli_help_with_legacy_windows_output_encoding(
    arguments: tuple[str, ...], expected: bytes, tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith(
            ("VERTEX_", "ADO_", "AZURE_", "WORKIQ_", "GH_", "GITHUB_",
             "OPENAI_", "ANTHROPIC_", "AWS_", "KUSTO_")
        ):
            del environment[name]
    environment.update(
        {
            "APPDATA": str(tmp_path),
            "HOME": str(tmp_path),
            "LOCALAPPDATA": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "cp1252",
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "VERTEX_DB_PATH": str(tmp_path / "vertex.db"),
            "VERTEX_PROGRAMS_ROOT": str(tmp_path / "programs"),
            "XDG_CACHE_HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path),
            "XDG_DATA_HOME": str(tmp_path),
        }
    )
    # Do not load the real checkout's .env in the child process.
    code = (
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "import dotenv; dotenv.load_dotenv = lambda *args, **kwargs: False; "
        "import cli; cli.app()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(Path(__file__).resolve().parents[2]), *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("cp1252", errors="replace")
    assert expected in result.stdout

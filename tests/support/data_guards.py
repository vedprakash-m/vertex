"""Single source of truth for "is private live program/edition data present?".

The fresh-clone CI checks out only tracked files. Private program/edition config
(`programs/acme/`, `editions/acme_weekly.yaml`, …) is gitignored, so data-dependent
tests must skip there. Historically tests detected this with
``programs/.exists() and any(programs.iterdir())`` — but rev. 326 committed
``programs/_templates/example_tpm/``, so ``programs/`` is now ALWAYS non-empty and
that predicate silently became always-True, making data-dependent tests *run* (and
fail) on the fresh clone. This module fixes that by detecting *real* live data, not
merely a non-empty ``programs/`` directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"
EDITIONS_ROOT = REPO_ROOT / "editions"

# The canonical live program these tests exercise. Its program.yaml is gitignored,
# so its presence is a reliable signal that real (non-template) data is checked out.
_LIVE_PROGRAM_MARKER = PROGRAMS_ROOT / "acme" / "program.yaml"


def live_program_data_available() -> bool:
    """True only when real (non-template) program data is present on disk."""
    return _LIVE_PROGRAM_MARKER.exists()


def live_edition_data_available(edition: str = "acme_weekly") -> bool:
    """True only when the named private edition config is present on disk."""
    return (EDITIONS_ROOT / f"{edition}.yaml").exists()


def require_live_program_data() -> None:
    """Skip the calling test unless real live program data is present."""
    if not live_program_data_available():
        pytest.skip("Requires local programs/acme data (absent on fresh-clone CI)")

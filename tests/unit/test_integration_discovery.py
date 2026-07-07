"""Direct smoke coverage for the extracted integration discovery helpers (D-13).

The config/binding/discovery helpers are disk- and provider-coupled (they read
program.yaml and run providers) and are exercised end-to-end by the integration
command suite; this module smoke-tests the deterministic seam.
"""

from __future__ import annotations

from pathlib import Path

from src.commands.integration_discovery import _candidate_store
from src.core.source_candidate_store import SourceCandidateStore


def test_candidate_store_constructs_for_program(tmp_path: Path) -> None:
    store = _candidate_store("demo", tmp_path)
    assert isinstance(store, SourceCandidateStore)

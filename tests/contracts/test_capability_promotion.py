"""G6 — capability promotion contract.

Asserts that when all three Phase-1 capabilities are promoted to `complete`,
`load_program_capability_status` reads them cleanly and returns the expected
statuses. The test uses a temp-patched file so it is green immediately and
can serve as a regression guard once B15/E5/A10 flip the live YAML.

When the live `programs/acme/capability_status.yaml` is updated to have all
three capabilities `complete`, replace the tmp_path fixture below with a
direct assertion against `PROGRAMS_ROOT / "acme"`.
"""
from __future__ import annotations
from pathlib import Path
import yaml

import pytest

from src.core.capability_status import load_program_capability_status, find_program_capability_status


_REQUIRED_CAPABILITIES = ("ado_activation", "kusto_activation", "m365_activation")

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_PROGRAMS_ROOT = REPO_ROOT / "programs"
# Detect REAL live data, not merely a non-empty programs/ dir: programs/_templates/
# is tracked (rev. 326), so "any(iterdir())" is always True even on the fresh-clone CI.
from tests.support.data_guards import live_program_data_available

_LIVE_PROGRAMS_EXIST = live_program_data_available()


def _write_all_complete_yaml(program_dir: Path) -> None:
    program_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": "1.0",
        "capabilities": [
            {
                "id": cap_id,
                "status": "complete",
                "summary": f"{cap_id} promoted to complete.",
                "degradation": "",
                "last_reviewed_on": "2026-05-18",
            }
            for cap_id in _REQUIRED_CAPABILITIES
        ],
    }
    (program_dir / "capability_status.yaml").write_text(
        yaml.dump(doc, sort_keys=False), encoding="utf-8"
    )


_STUB_PROGRAM_DOC = {"id": "acme", "name": "Acme", "schema_version": "2.0"}


def test_all_phase1_capabilities_load_as_complete_when_yaml_is_complete(tmp_path: Path) -> None:
    """Promotion contract: loader accepts all-complete yaml without errors."""
    program_dir = tmp_path / "acme"
    _write_all_complete_yaml(program_dir)

    statuses = load_program_capability_status(
        "acme", programs_root=tmp_path, program_document=_STUB_PROGRAM_DOC
    )
    by_id = {s.capability_id: s for s in statuses}

    for cap_id in _REQUIRED_CAPABILITIES:
        assert cap_id in by_id, f"Missing capability: {cap_id}"
        assert by_id[cap_id].status == "complete", f"{cap_id} not complete: {by_id[cap_id].status}"


def test_find_program_capability_status_returns_none_for_unknown_id(tmp_path: Path) -> None:
    program_dir = tmp_path / "acme"
    _write_all_complete_yaml(program_dir)

    statuses = load_program_capability_status(
        "acme", programs_root=tmp_path, program_document=_STUB_PROGRAM_DOC
    )
    assert find_program_capability_status(statuses, "nonexistent_cap") is None


@pytest.mark.skipif(not _LIVE_PROGRAMS_EXIST, reason="Requires programs/ data")
def test_live_capability_status_yaml_has_all_required_ids() -> None:
    """Live file must declare all three Phase-1 capability IDs (any status)."""
    statuses = load_program_capability_status("acme", programs_root=LIVE_PROGRAMS_ROOT)
    by_id = {s.capability_id: s for s in statuses}

    for cap_id in _REQUIRED_CAPABILITIES:
        assert cap_id in by_id, f"Live capability_status.yaml missing: {cap_id}"
        assert by_id[cap_id].status in {"complete", "in_progress", "deferred", "unavailable", "unknown"}, (
            f"Invalid status for {cap_id}: {by_id[cap_id].status}"
        )


@pytest.mark.skipif(not _LIVE_PROGRAMS_EXIST, reason="Requires programs/ data")
def test_live_ado_activation_is_complete() -> None:
    """ado_activation must be complete (A8 done)."""
    statuses = load_program_capability_status("acme", programs_root=LIVE_PROGRAMS_ROOT)
    status = find_program_capability_status(statuses, "ado_activation")
    assert status is not None
    assert status.status == "complete", f"ado_activation is {status.status!r} — A8 not yet done"


@pytest.mark.skipif(not _LIVE_PROGRAMS_EXIST, reason="Requires programs/ data")
def test_live_kusto_activation_is_complete() -> None:
    """kusto_activation must be complete (B15 closed 2026-05-19)."""
    statuses = load_program_capability_status("acme", programs_root=LIVE_PROGRAMS_ROOT)
    status = find_program_capability_status(statuses, "kusto_activation")
    assert status is not None
    assert status.status == "complete", f"kusto_activation is {status.status!r} — B15 not yet closed"


@pytest.mark.skipif(not _LIVE_PROGRAMS_EXIST, reason="Requires programs/ data")
def test_live_m365_activation_is_complete() -> None:
    """m365_activation must be complete (E5 closed 2026-05-19)."""
    statuses = load_program_capability_status("acme", programs_root=LIVE_PROGRAMS_ROOT)
    status = find_program_capability_status(statuses, "m365_activation")
    assert status is not None
    assert status.status == "complete", f"m365_activation is {status.status!r} — E5 not yet closed"

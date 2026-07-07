"""Direct coverage for the extracted FR-SG-37 optimization-proposal writer.

Guards the D-25 / Phase 3 extraction from ``src/commands/confirm.py`` into
``src/commands/confirm_stages/optimization_proposals.py``. The writer is an
isolated, idempotent feedback write to ``_feedback/derivation_adjustments.yaml``
— it must not duplicate pending proposals on re-run and must no-op when there
are no qualifying streaks.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from src.commands.confirm_stages import optimization_proposals
from src.commands.confirm_stages.optimization_proposals import write_optimization_proposals


def _streak(dimension: str, override_value: str, streak_count: int = 3):
    return SimpleNamespace(dimension=dimension, override_value=override_value, streak_count=streak_count)


def _proposals_path(programs_root: Path, program_id: str = "acme") -> Path:
    return programs_root / program_id / "_feedback" / "derivation_adjustments.yaml"


def test_no_streaks_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(optimization_proposals, "get_override_streaks", lambda *a, **k: ())
    write_optimization_proposals("acme", edition_id="acme_weekly", issue_number=1, programs_root=tmp_path)
    assert not _proposals_path(tmp_path).exists()


def test_writes_proposal_for_streak(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        optimization_proposals,
        "get_override_streaks",
        lambda *a, **k: (_streak("Velocity", "High", 4),),
    )
    write_optimization_proposals("acme", edition_id="acme_weekly", issue_number=2, programs_root=tmp_path)
    path = _proposals_path(tmp_path)
    assert path.exists()
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    proposals = payload["proposals"]
    assert len(proposals) == 1
    p = proposals[0]
    assert p["dimension"] == "Velocity"
    assert p["override_value"] == "High"
    assert p["streak_count"] == 4
    assert p["edition_id"] == "acme_weekly"
    assert p["issue_number"] == 2
    assert p["status"] == "pending"
    assert "make this the default" in p["message"]


def test_idempotent_does_not_duplicate_pending(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        optimization_proposals,
        "get_override_streaks",
        lambda *a, **k: (_streak("Velocity", "High"),),
    )
    write_optimization_proposals("acme", edition_id="acme_weekly", issue_number=2, programs_root=tmp_path)
    write_optimization_proposals("acme", edition_id="acme_weekly", issue_number=3, programs_root=tmp_path)
    payload = yaml.safe_load(_proposals_path(tmp_path).read_text(encoding="utf-8"))
    # Same (dimension, override_value) pending -> not duplicated.
    assert len(payload["proposals"]) == 1


def test_appends_new_distinct_proposal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        optimization_proposals,
        "get_override_streaks",
        lambda *a, **k: (_streak("Velocity", "High"),),
    )
    write_optimization_proposals("acme", edition_id="acme_weekly", issue_number=2, programs_root=tmp_path)
    monkeypatch.setattr(
        optimization_proposals,
        "get_override_streaks",
        lambda *a, **k: (_streak("Risk", "Blocked"),),
    )
    write_optimization_proposals("acme", edition_id="acme_weekly", issue_number=3, programs_root=tmp_path)
    payload = yaml.safe_load(_proposals_path(tmp_path).read_text(encoding="utf-8"))
    dims = {p["dimension"] for p in payload["proposals"]}
    assert dims == {"Velocity", "Risk"}


def test_non_pending_existing_does_not_block_new(tmp_path: Path, monkeypatch) -> None:
    path = _proposals_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump({"proposals": [{"dimension": "Velocity", "override_value": "High", "status": "accepted"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        optimization_proposals,
        "get_override_streaks",
        lambda *a, **k: (_streak("Velocity", "High"),),
    )
    write_optimization_proposals("acme", edition_id="acme_weekly", issue_number=4, programs_root=tmp_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Accepted one stays; a new pending one is added (dedup only blocks pending).
    statuses = sorted(p["status"] for p in payload["proposals"])
    assert statuses == ["accepted", "pending"]

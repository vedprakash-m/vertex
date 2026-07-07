from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.brief_intervention_store import (
    BriefInterventionStatus,
    append_brief_intervention_resolution,
    load_brief_intervention_resolutions,
)


def test_load_brief_intervention_resolutions_round_trips_latest_resolution(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_brief_intervention_resolution(
        "acme",
        proposal_id="decision-ask-d-1-nudge",
        title="Nudge LT on decision ask",
        command="vertex brief --approve decision-ask-d-1-nudge",
        source_hash="hash-1",
        status=BriefInterventionStatus.APPROVED,
        resolved_at=datetime(2026, 5, 21, 9, 1, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    resolutions = load_brief_intervention_resolutions("acme", programs_root=programs_root)

    assert resolutions["decision-ask-d-1-nudge"].status is BriefInterventionStatus.APPROVED


def test_load_brief_intervention_resolutions_rejects_non_string_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    interventions_path = programs_root / "acme" / "_feedback" / "brief_interventions.jsonl"
    interventions_path.parent.mkdir(parents=True, exist_ok=True)
    interventions_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "proposal_id": "decision-ask-d-1-nudge",
                "status": 123,
                "title": "Nudge LT on decision ask",
                "command": "vertex brief --approve decision-ask-d-1-nudge",
                "source_hash": "hash-1",
                "resolved_at": "2026-05-21T09:01:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="status must be a string"):
        load_brief_intervention_resolutions("acme", programs_root=programs_root)


def test_load_brief_intervention_resolutions_rejects_non_string_proposal_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    interventions_path = programs_root / "acme" / "_feedback" / "brief_interventions.jsonl"
    interventions_path.parent.mkdir(parents=True, exist_ok=True)
    interventions_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "proposal_id": 123,
                "status": "approved",
                "title": "Nudge LT on decision ask",
                "command": "vertex brief --approve decision-ask-d-1-nudge",
                "source_hash": "hash-1",
                "resolved_at": "2026-05-21T09:01:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="proposal_id must be a string"):
        load_brief_intervention_resolutions("acme", programs_root=programs_root)


def test_load_brief_intervention_resolutions_rejects_naive_resolved_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    interventions_path = programs_root / "acme" / "_feedback" / "brief_interventions.jsonl"
    interventions_path.parent.mkdir(parents=True, exist_ok=True)
    interventions_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "proposal_id": "decision-ask-d-1-nudge",
                "status": "approved",
                "title": "Nudge LT on decision ask",
                "command": "vertex brief --approve decision-ask-d-1-nudge",
                "source_hash": "hash-1",
                "resolved_at": "2026-05-21T09:01:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="resolved_at must include timezone information"):
        load_brief_intervention_resolutions("acme", programs_root=programs_root)
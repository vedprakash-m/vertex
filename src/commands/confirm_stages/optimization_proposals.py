"""FR-SG-37 derivation-adjustment proposal writer for confirm.

Extracted from ``src/commands/confirm.py`` (D-25 / Phase 3). When a scorecard
dimension has been overridden the same way for a sustained streak, confirm
records a "make this the default?" proposal under the program's local
``_feedback/derivation_adjustments.yaml``. This is an isolated, idempotent
feedback write (it de-dupes pending proposals by ``(dimension, override_value)``)
that is entirely separate from the archive-snapshot / baseline transaction, so
it is safe to lift out of the confirm transaction module. ``confirm.py`` imports
``write_optimization_proposals`` under its historical private alias.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.core.analytics_store import get_override_streaks


def write_optimization_proposals(
    program_id: str,
    *,
    edition_id: str,
    issue_number: int,
    programs_root: Path,
) -> None:
    """FR-SG-37: Write derivation adjustment proposals for dimensions with streak ≥ 3."""
    streaks = get_override_streaks(program_id, min_streak=3, programs_root=programs_root)
    if not streaks:
        return
    proposals_path = programs_root / program_id / "_feedback" / "derivation_adjustments.yaml"
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if proposals_path.exists():
        existing = yaml.safe_load(proposals_path.read_text(encoding="utf-8")) or {}
    current_proposals: list[dict[str, Any]] = list(existing.get("proposals") or [])
    existing_keys = {
        (p["dimension"], p["override_value"])
        for p in current_proposals
        if p.get("status") == "pending"
    }
    now_iso = datetime.now(timezone.utc).isoformat()
    for streak in streaks:
        key = (streak.dimension, streak.override_value)
        if key in existing_keys:
            continue
        current_proposals.append(
            {
                "dimension": streak.dimension,
                "override_value": streak.override_value,
                "streak_count": streak.streak_count,
                "edition_id": edition_id,
                "issue_number": issue_number,
                "proposed_at": now_iso,
                "message": (
                    f"Vertex noticed you consistently override {streak.dimension} to "
                    f"{streak.override_value} ({streak.streak_count}× in a row) — "
                    f"make this the default derivation?"
                ),
                "status": "pending",
            }
        )
    proposals_path.write_text(
        yaml.safe_dump({"proposals": current_proposals}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

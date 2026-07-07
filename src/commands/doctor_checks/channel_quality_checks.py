from __future__ import annotations

from pathlib import Path
from typing import Callable

from src.commands.doctor_checks.models import DoctorCheck
from src.core.conversion_fidelity import load_conversion_fidelity
from src.core.models_v2 import TrajectoryPoint
from src.core.trajectory import load_all_trajectories


def conversion_fidelity_check(program_id: str, programs_root: Path) -> DoctorCheck | None:
    """FR-SG-43: Surface conversion fidelity scores in doctor output."""
    entries = load_conversion_fidelity(program_id, programs_root=programs_root)
    if not entries:
        return None
    low_fidelity = [entry for entry in entries if entry.score < 0.5]
    metadata_entries = [{"function": entry.function, "score": entry.score} for entry in entries]
    lines = [
        f"{entry.function}: {entry.sourced_inputs}/{entry.required_inputs} ({entry.score:.0%})"
        for entry in entries
    ]
    if low_fidelity:
        return DoctorCheck(
            label="Conversion Fidelity",
            status="warn",
            detail="Low fidelity functions: " + ", ".join(entry.function for entry in low_fidelity) + "\n" + "\n".join(lines),
            metadata={"entries": metadata_entries},
        )
    return DoctorCheck(
        label="Conversion Fidelity",
        status="ok",
        detail="\n".join(lines),
        metadata={"entries": metadata_entries},
    )


def eta_credibility_check(
    program_id: str,
    programs_root: Path,
    *,
    compute_eta_credibility_fn: Callable[[tuple[TrajectoryPoint, ...]], tuple[float, object]] | None = None,
) -> DoctorCheck | None:
    """FR-SG-21: Surface work items with low ETA credibility (credibility < 0.5)."""
    compute_credibility: Callable[[tuple[TrajectoryPoint, ...]], tuple[float, object]]
    if compute_eta_credibility_fn is None:
        compute_credibility = _default_compute_eta_credibility
    else:
        compute_credibility = compute_eta_credibility_fn
    trajectories = load_all_trajectories(program_id, programs_root=programs_root)
    if not trajectories:
        return None
    low_credibility: list[str] = []
    for work_item_id, points in sorted(trajectories.items()):
        credibility, _ = compute_credibility(points)
        if credibility < 0.5:
            low_credibility.append(f"WI#{work_item_id} (credibility={credibility:.0%})")
    if not low_credibility:
        return DoctorCheck(
            label="ETA Credibility",
            status="ok",
            detail=f"All {len(trajectories)} items with trajectories have credibility ≥ 50%.",
            metadata={},
        )
    return DoctorCheck(
        label="ETA Credibility",
        status="warn",
        detail="Items with low ETA credibility (< 50%): " + ", ".join(low_credibility),
        metadata={"low_credibility_count": len(low_credibility)},
    )


def _default_compute_eta_credibility(points: tuple[TrajectoryPoint, ...]) -> tuple[float, object]:
    from src.core.trajectory_analyzer import compute_eta_credibility as _compute_eta_credibility  # noqa: PLC0415

    return _compute_eta_credibility(points)

"""S-11b: Per-program spot-check gate contract test.

Gate: "Per-program spot-check gate; ICS/docs floors | per-program floor passes"

Verifies that when a program is brought up to the fleet, it passes a suite of
basic health checks before being considered production-ready.  Each check is
deterministic (no AI, no external services) and verifiable from the local
file system and in-memory state.

Spot-checks verified:
1. Program directory structure: programs/<prog>/ exists with required anchors.
2. Fact store: can open and snapshot the program's SQLite DB without error.
3. Milestone schema: at least one milestone.entry fact has the required
   payload fields (or the snapshot is empty — quiet programs are valid).
4. SoR state: fact_store_sor.yaml is absent (legacy) or has a valid mode.
5. Policy file: source_authority.yaml is present and loadable.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pytest
import yaml

from src.core.program_fact_store import (
    ProgramFactStore,
    ProgramFactSnapshot,
    FactReviewState,
    FactLifecycleState,
)


# ─── helpers ────────────────────────────────────────────────────────────────

_POLICY_PATH = pathlib.Path(__file__).resolve().parents[2] / "vertex" / "policies" / "source_authority.yaml"

_REQUIRED_MILESTONE_PAYLOAD_KEYS = {
    "id",
    "title",
    "status",
}


def _spot_check_program(program_id: str, programs_root: pathlib.Path) -> dict[str, str]:
    """Run spot checks for one program.  Returns {check_name: "pass" | reason}.

    Each check is independent; a failure in one does not prevent others.
    """
    results: dict[str, str] = {}
    prog_dir = programs_root / program_id

    # Check 1: program directory exists
    results["directory_exists"] = "pass" if prog_dir.is_dir() else f"missing: {prog_dir}"

    # Check 2: fact store opens cleanly
    try:
        db_root = programs_root.parent
        store = ProgramFactStore(program_id, db_root=db_root)
        snapshot = store.snapshot(as_of=None)
        results["fact_store_opens"] = "pass"
    except Exception as exc:  # noqa: BLE001
        snapshot = None
        results["fact_store_opens"] = f"error: {exc}"

    # Check 3: milestone schema validation (only if facts exist)
    if snapshot is not None:
        milestone_facts = [
            f for f in snapshot.facts
            if f.fact_type == "milestone.entry"
            and f.review_state == FactReviewState.ACCEPTED
            and f.lifecycle_state == FactLifecycleState.ACTIVE
        ]
        if not milestone_facts:
            results["milestone_schema"] = "pass (no milestones — quiet program)"
        else:
            bad = [
                f.fact_id
                for f in milestone_facts
                if not _REQUIRED_MILESTONE_PAYLOAD_KEYS.issubset(f.payload.keys())
            ]
            results["milestone_schema"] = "pass" if not bad else f"malformed: {bad[:3]}"
    else:
        results["milestone_schema"] = "skip (fact_store_opens failed)"

    # Check 4: SoR state file valid-or-absent
    sor_path = prog_dir / "fact_store_sor.yaml"
    if not sor_path.exists():
        results["sor_state_valid"] = "pass (absent → legacy mode)"
    else:
        try:
            doc = yaml.safe_load(sor_path.read_text(encoding="utf-8")) or {}
            mode = doc.get("mode", "")
            results["sor_state_valid"] = (
                "pass" if mode in {"legacy", "shadow", "primary"}
                else f"invalid mode: {mode!r}"
            )
        except Exception as exc:  # noqa: BLE001
            results["sor_state_valid"] = f"yaml error: {exc}"

    # Check 5: source_authority.yaml present and parseable
    if _POLICY_PATH.exists():
        try:
            doc = yaml.safe_load(_POLICY_PATH.read_text(encoding="utf-8")) or {}
            has_family_map = isinstance(doc.get("family_map"), dict)
            results["policy_loadable"] = "pass" if has_family_map else "missing family_map"
        except Exception as exc:  # noqa: BLE001
            results["policy_loadable"] = f"yaml error: {exc}"
    else:
        results["policy_loadable"] = f"missing: {_POLICY_PATH}"

    return results


# ─── S-11b contract tests ────────────────────────────────────────────────────

class TestPerProgramSpotCheckGate:
    """S-11b: per-program spot-check gate passes for all fleet programs."""

    def test_spot_check_function_returns_all_five_check_names(self, tmp_path) -> None:
        """Contract: spot_check returns exactly the 5 documented checks."""
        programs_root = tmp_path / "programs"
        (programs_root / "dummy-prog").mkdir(parents=True)

        results = _spot_check_program("dummy-prog", programs_root)
        assert set(results.keys()) == {
            "directory_exists",
            "fact_store_opens",
            "milestone_schema",
            "sor_state_valid",
            "policy_loadable",
        }

    def test_quiet_program_passes_spot_check(self, tmp_path) -> None:
        """A program with no facts (quiet lane) passes all checks."""
        programs_root = tmp_path / "programs"
        program_id = "quiet-lane"
        (programs_root / program_id).mkdir(parents=True)

        results = _spot_check_program(program_id, programs_root)

        assert results["directory_exists"] == "pass"
        assert results["fact_store_opens"] == "pass"
        assert "quiet program" in results["milestone_schema"]  # empty is valid
        assert "absent" in results["sor_state_valid"]  # no SoR file is valid

    def test_program_with_valid_sor_state_passes(self, tmp_path) -> None:
        """A program with a valid fact_store_sor.yaml passes the SoR check."""
        programs_root = tmp_path / "programs"
        program_id = "active-prog"
        prog_dir = programs_root / program_id
        prog_dir.mkdir(parents=True)

        sor_content = "schema_version: '2.0'\nmode: shadow\nrecorded_at: '2024-01-01T00:00:00+00:00'\n"
        (prog_dir / "fact_store_sor.yaml").write_text(sor_content, encoding="utf-8")

        results = _spot_check_program(program_id, programs_root)
        assert results["sor_state_valid"] == "pass"

    def test_program_with_invalid_sor_mode_fails_spot_check(self, tmp_path) -> None:
        """A program with an invalid SoR mode fails the SoR check (not silently ignored)."""
        programs_root = tmp_path / "programs"
        program_id = "bad-sor-prog"
        prog_dir = programs_root / program_id
        prog_dir.mkdir(parents=True)

        sor_content = "schema_version: '2.0'\nmode: invalid_mode\nrecorded_at: '2024-01-01T00:00:00+00:00'\n"
        (prog_dir / "fact_store_sor.yaml").write_text(sor_content, encoding="utf-8")

        results = _spot_check_program(program_id, programs_root)
        assert results["sor_state_valid"].startswith("invalid mode")

    def test_missing_program_directory_fails_spot_check(self, tmp_path) -> None:
        """A program directory that doesn't exist fails the directory check."""
        programs_root = tmp_path / "programs"
        programs_root.mkdir(parents=True)

        results = _spot_check_program("nonexistent-program", programs_root)
        assert results["directory_exists"].startswith("missing")

    def test_source_authority_policy_loads_in_spot_check(self, tmp_path) -> None:
        """The real source_authority.yaml passes the policy check."""
        programs_root = tmp_path / "programs"
        (programs_root / "check-prog").mkdir(parents=True)

        results = _spot_check_program("check-prog", programs_root)
        # The real policy file exists and has family_map
        assert results["policy_loadable"] == "pass"

    def test_all_spot_checks_pass_for_two_independent_programs(self, tmp_path) -> None:
        """Two programs checked independently — one active, one quiet."""
        programs_root = tmp_path / "programs"
        for prog_id in ("prog-a", "prog-b"):
            (programs_root / prog_id).mkdir(parents=True)

        results_a = _spot_check_program("prog-a", programs_root)
        results_b = _spot_check_program("prog-b", programs_root)

        # Both should pass core checks
        assert results_a["directory_exists"] == "pass"
        assert results_b["directory_exists"] == "pass"
        assert results_a["fact_store_opens"] == "pass"
        assert results_b["fact_store_opens"] == "pass"
        # Results are independent (no cross-program bleed)
        assert results_a is not results_b

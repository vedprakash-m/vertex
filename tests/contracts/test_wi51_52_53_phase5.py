"""Phase 5 contract tests: WI-5.1 (full reality status), WI-5.2 (family_modes),
WI-5.3 (fact-store-flip --family + clean-cycle gate).

Tests:
 1. WI-5.2: family_modes_save_and_load_roundtrip
 2. WI-5.2: all_families_shadow_simultaneously
 3. WI-5.2: v1_schema_readable_with_empty_family_modes
 4. WI-5.2: v2_schema_version_written
 5. WI-5.2: resolve_family_sor_mode_falls_back_to_program_mode
 6. WI-5.3: flip_family_refused_without_cycles
 7. WI-5.3: flip_family_succeeds_with_sufficient_cycles
 8. WI-5.3: family_cycles_per_family_isolation
 9. WI-5.3: record_and_reset_family_cycles
10. WI-5.1: reality_checks_entity_count_ok
11. WI-5.1: reality_checks_entity_count_warn
12. WI-5.1: reality_checks_override_recert_ok
13. WI-5.1: reality_checks_override_recert_warn
14. WI-5.1: reality_status_payload_contains_wi51_fields
15. WI-5.1: ask_miss_count_7d_counts_recent_entries
16. WI-5.1: reality_status_command_registered_with_all_programs_flag
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_v1_sor_yaml(path: Path, mode: str = "legacy") -> None:
    """Write a v1 (no family_modes) SoR YAML file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "mode": mode,
                "recorded_at": "2026-06-01T00:00:00",
                "recorded_by": "test",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# WI-5.2: SoR v2 family_modes
# ---------------------------------------------------------------------------

def test_family_modes_save_and_load_roundtrip(tmp_path: Path) -> None:
    """save_fact_sor_state with family_modes → load_fact_sor_state returns them."""
    from src.core.fact_sor_state import save_fact_sor_state, load_fact_sor_state, AUTHORITY_FAMILIES

    now = datetime.now(timezone.utc)
    fm = {AUTHORITY_FAMILIES[0]: "shadow", AUTHORITY_FAMILIES[1]: "primary"}
    state = save_fact_sor_state(
        "myprog",
        mode="shadow",
        recorded_at=now,
        recorded_by="test",
        family_modes=fm,
        programs_root=tmp_path,
    )
    assert state.family_modes == fm

    loaded = load_fact_sor_state("myprog", programs_root=tmp_path)
    assert loaded is not None
    assert loaded.mode == "shadow"
    assert loaded.family_modes[AUTHORITY_FAMILIES[0]] == "shadow"
    assert loaded.family_modes[AUTHORITY_FAMILIES[1]] == "primary"


def test_all_families_shadow_simultaneously(tmp_path: Path) -> None:
    """All AUTHORITY_FAMILIES can be set to shadow simultaneously (§6.7)."""
    from src.core.fact_sor_state import save_fact_sor_state, load_fact_sor_state, AUTHORITY_FAMILIES

    now = datetime.now(timezone.utc)
    fm = {f: "shadow" for f in AUTHORITY_FAMILIES}
    save_fact_sor_state(
        "myprog",
        mode="shadow",
        recorded_at=now,
        family_modes=fm,
        programs_root=tmp_path,
    )
    loaded = load_fact_sor_state("myprog", programs_root=tmp_path)
    assert loaded is not None
    assert loaded.mode == "shadow"
    for fam in AUTHORITY_FAMILIES:
        assert loaded.family_modes[fam] == "shadow", f"family {fam!r} not shadow"


def test_v1_schema_readable_with_empty_family_modes(tmp_path: Path) -> None:
    """v1 YAML (no family_modes key) loads with an empty family_modes dict."""
    from src.core.fact_sor_state import load_fact_sor_state, get_fact_sor_state_path

    path = get_fact_sor_state_path("myprog", programs_root=tmp_path)
    _write_v1_sor_yaml(path, mode="legacy")

    loaded = load_fact_sor_state("myprog", programs_root=tmp_path)
    assert loaded is not None
    assert loaded.mode == "legacy"
    assert loaded.family_modes == {}


def test_v2_schema_version_written(tmp_path: Path) -> None:
    """save_fact_sor_state writes schema_version: '2.0'."""
    from src.core.fact_sor_state import save_fact_sor_state, get_fact_sor_state_path

    now = datetime.now(timezone.utc)
    save_fact_sor_state(
        "myprog",
        mode="legacy",
        recorded_at=now,
        programs_root=tmp_path,
    )
    path = get_fact_sor_state_path("myprog", programs_root=tmp_path)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["schema_version"] == "2.0"


def test_resolve_family_sor_mode_falls_back_to_program_mode(tmp_path: Path) -> None:
    """resolve_family_sor_mode returns program-level mode when family not in family_modes."""
    from src.core.fact_sor_state import save_fact_sor_state, resolve_family_sor_mode, AUTHORITY_FAMILIES

    now = datetime.now(timezone.utc)
    save_fact_sor_state(
        "myprog",
        mode="shadow",
        recorded_at=now,
        programs_root=tmp_path,
    )
    # No family_modes set — should fall back to "shadow"
    mode = resolve_family_sor_mode("myprog", AUTHORITY_FAMILIES[0], programs_root=tmp_path)
    assert mode == "shadow"


# ---------------------------------------------------------------------------
# WI-5.3: fact-store-flip --family + clean-cycle gate
# ---------------------------------------------------------------------------

def test_flip_family_refused_without_cycles(tmp_path: Path) -> None:
    """flip_family_to_primary raises StateError when cycle count < 5."""
    from src.commands.admin_fact_store_flip import flip_family_to_primary, _FAMILY_FLIP_CLEAN_CYCLES_REQUIRED
    from src.core.exceptions import StateError
    from src.core.fact_sor_state import save_fact_sor_state, AUTHORITY_FAMILIES

    now = datetime.now(timezone.utc)
    save_fact_sor_state(
        "myprog",
        mode="shadow",
        recorded_at=now,
        programs_root=tmp_path,
    )
    family = AUTHORITY_FAMILIES[0]
    with pytest.raises(StateError, match="clean cycles"):
        flip_family_to_primary(
            program_id="myprog",
            family=family,
            programs_root=tmp_path,
        )


def test_flip_family_succeeds_with_sufficient_cycles(tmp_path: Path) -> None:
    """flip_family_to_primary succeeds when cycle count >= 5."""
    from src.commands.admin_fact_store_flip import (
        flip_family_to_primary,
        _FAMILY_FLIP_CLEAN_CYCLES_REQUIRED,
        FamilyFlipResult,
    )
    from src.core.fact_sor_state import (
        save_fact_sor_state,
        load_fact_sor_state,
        record_family_clean_cycle,
        AUTHORITY_FAMILIES,
    )

    now = datetime.now(timezone.utc)
    save_fact_sor_state(
        "myprog",
        mode="shadow",
        recorded_at=now,
        programs_root=tmp_path,
    )
    family = AUTHORITY_FAMILIES[0]
    # Record exactly the required number of clean cycles
    for _ in range(_FAMILY_FLIP_CLEAN_CYCLES_REQUIRED):
        record_family_clean_cycle("myprog", family, programs_root=tmp_path)

    result = flip_family_to_primary(
        program_id="myprog",
        family=family,
        programs_root=tmp_path,
    )
    assert isinstance(result, FamilyFlipResult)
    assert result.next_mode == "primary"
    assert result.family == family

    # Verify persisted
    loaded = load_fact_sor_state("myprog", programs_root=tmp_path)
    assert loaded is not None
    assert loaded.family_modes.get(family) == "primary"


def test_family_cycles_per_family_isolation(tmp_path: Path) -> None:
    """Clean-cycle counts are independent per authority family."""
    from src.core.fact_sor_state import (
        record_family_clean_cycle,
        load_family_clean_cycles,
        AUTHORITY_FAMILIES,
    )

    family_a = AUTHORITY_FAMILIES[0]
    family_b = AUTHORITY_FAMILIES[1]
    for _ in range(3):
        record_family_clean_cycle("myprog", family_a, programs_root=tmp_path)
    for _ in range(1):
        record_family_clean_cycle("myprog", family_b, programs_root=tmp_path)

    cycles = load_family_clean_cycles("myprog", programs_root=tmp_path)
    assert cycles[family_a] == 3
    assert cycles[family_b] == 1


def test_record_and_reset_family_cycles(tmp_path: Path) -> None:
    """record_family_clean_cycle increments; reset_family_clean_cycles zeros it."""
    from src.core.fact_sor_state import (
        record_family_clean_cycle,
        reset_family_clean_cycles,
        load_family_clean_cycles,
        AUTHORITY_FAMILIES,
    )

    family = AUTHORITY_FAMILIES[0]
    assert record_family_clean_cycle("myprog", family, programs_root=tmp_path) == 1
    assert record_family_clean_cycle("myprog", family, programs_root=tmp_path) == 2
    reset_family_clean_cycles("myprog", family, programs_root=tmp_path)
    cycles = load_family_clean_cycles("myprog", programs_root=tmp_path)
    assert cycles[family] == 0


# ---------------------------------------------------------------------------
# WI-5.1: reality_checks.py
# ---------------------------------------------------------------------------

def test_reality_checks_entity_count_ok(tmp_path: Path) -> None:
    """entity_count check returns ok when count ≤ 150."""
    from src.commands.reality_checks import check_entity_count

    entities_path = tmp_path / "myprog" / "knowledge" / "entities.yaml"
    entities_path.parent.mkdir(parents=True, exist_ok=True)
    entities_path.write_text(
        yaml.safe_dump([{"id": f"e{i}"} for i in range(50)]),
        encoding="utf-8",
    )
    result = check_entity_count("myprog", programs_root=tmp_path)
    assert result.check_id == "entity_count_threshold"
    assert result.status == "ok"
    assert result.details["count"] == 50


def test_reality_checks_entity_count_warn(tmp_path: Path) -> None:
    """entity_count check returns warn when count > 150."""
    from src.commands.reality_checks import check_entity_count

    entities_path = tmp_path / "myprog" / "knowledge" / "entities.yaml"
    entities_path.parent.mkdir(parents=True, exist_ok=True)
    entities_path.write_text(
        yaml.safe_dump([{"id": f"e{i}"} for i in range(200)]),
        encoding="utf-8",
    )
    result = check_entity_count("myprog", programs_root=tmp_path, threshold=150)
    assert result.status == "warn"
    assert result.details["count"] == 200


def test_reality_checks_override_recert_ok(tmp_path: Path) -> None:
    """override_recertification check returns ok when no override file exists."""
    from src.commands.reality_checks import check_override_recertification

    result = check_override_recertification("myprog", programs_root=tmp_path)
    assert result.check_id == "override_recertification_due"
    assert result.status == "ok"


def test_reality_checks_override_recert_warn(tmp_path: Path) -> None:
    """override_recertification check returns warn when acknowledged_at is expired."""
    from src.commands.reality_checks import check_override_recertification

    override_path = tmp_path / "myprog" / "source_authority.yaml"
    override_path.parent.mkdir(parents=True, exist_ok=True)
    expired_date = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    override_path.write_text(
        yaml.safe_dump({
            "override_ttl_days": 90,
            "authority": {
                "metric": {
                    "primary": "ado",
                    "i_understand_this_bypasses_human_judgment": True,
                    "acknowledged_at": expired_date,
                },
            },
        }),
        encoding="utf-8",
    )
    result = check_override_recertification(
        "myprog",
        programs_root=tmp_path,
        as_of=datetime.now(timezone.utc),
    )
    assert result.status == "warn"
    assert "metric" in result.message


def test_reality_status_payload_contains_wi51_fields(tmp_path: Path) -> None:
    """_build_program_status_payload returns all WI-5.1 fields."""
    from unittest.mock import patch, MagicMock
    from src.commands.reality import _build_program_status_payload

    mock_snapshot = MagicMock()
    mock_snapshot.facts = ()

    mock_qg = MagicMock()
    mock_qg.passed = True
    mock_qg.exit_code = 0
    mock_qg.message = "ok"
    mock_qg.forceable = False

    with (
        patch("src.core.program_fact_store.load_program_facts", return_value=mock_snapshot),
        patch("src.core.truth_model.build_trust_context_from_snapshot", return_value=MagicMock()),
        patch("src.core.quality_gates.qg27.evaluate_qg27", return_value=mock_qg),
        patch("src.commands.reality_checks.run_reality_checks", return_value=()),
        patch("src.core.fact_sor_state.load_fact_sor_state", return_value=None),
    ):
        payload = _build_program_status_payload(
            "myprog",
            programs_root=tmp_path,
            db_root=None,
        )

    # WI-5.1 required fields
    assert "sor_mode" in payload
    assert "family_modes" in payload
    assert "o16_contradiction_summary" in payload
    assert "executed_sync_count" in payload
    assert "ask_miss_count_7d" in payload
    assert "reality_checks" in payload
    assert "qg27" in payload
    assert "truth_level_counts" in payload


def test_ask_miss_count_7d_counts_recent_entries(tmp_path: Path) -> None:
    """_count_ask_misses_7d counts only entries within the last 7 days."""
    from src.commands.reality import _count_ask_misses_7d
    (tmp_path / "programs" / "acme" / "publications").mkdir(parents=True)
    miss_log = (tmp_path / "programs") / "acme" / "publications" / "ask_misses.jsonl"

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=10)).isoformat()

    miss_log.write_text(
        json.dumps({"logged_at": recent, "question": "q1"}) + "\n"
        + json.dumps({"logged_at": old, "question": "q2"}) + "\n"
        + json.dumps({"logged_at": recent, "question": "q3"}) + "\n",
        encoding="utf-8",
    )
    count = _count_ask_misses_7d(program_id="acme", programs_root=tmp_path / "programs")
    assert count == 2


def test_reality_status_command_registered_with_all_programs_flag() -> None:
    """vertex reality status --help lists --program and --all-programs (WI-5.1 fleet default)."""
    from typer.testing import CliRunner
    from cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["reality", "status", "--help"])
    assert result.exit_code == 0
    assert "--program" in result.stdout
    assert "--all-programs" in result.stdout

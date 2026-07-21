"""specs/people.md Phase 2a, PPL-W2A.6: tests for stakeholder dual-read
and STK-04 (src/core/program_context.py::_parse_stakeholders).

specs/people.md §9.1's own verification bar: "STK-01/02/03 and STK-04
pass on fixtures with top-level-only, charter-only, equivalent-duplicate,
and conflicting-duplicate inputs."
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from src.core.program_context import InvariantSeverity, load_program_context


def _write_yaml(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.dump(data, handle, default_flow_style=False)


def _write_program(
    tmp_path: Path,
    *,
    program_id: str = "test_prog",
    top_level_stakeholders: list[dict] | None = None,
    charter_stakeholders: list[dict] | None = None,
    raci_alias: str | None = None,
) -> Path:
    """Mirrors tests/unit/test_program_context.py's `_minimal_program`
    convention, extended to control BOTH stakeholder sources
    independently for the dual-read fixture matrix."""
    prog_dir = tmp_path / "programs" / program_id
    prog_dir.mkdir(parents=True, exist_ok=True)

    program_document: dict = {
        "schema_version": "3.0",
        "id": program_id,
        "name": "Test Program",
        "sub_programs": [{"id": "sub1", "name": "Sub One"}],
    }
    if top_level_stakeholders is not None:
        program_document["stakeholder_register"] = top_level_stakeholders
    if charter_stakeholders is not None:
        program_document["charter"] = {"stakeholder_register": charter_stakeholders}
    _write_yaml(prog_dir / "program.yaml", program_document)

    workstream: dict = {"id": "ws1", "name": "Workstream 1"}
    if raci_alias is not None:
        workstream["raci"] = {"accountable": raci_alias}
    _write_yaml(prog_dir / "workstreams.yaml", {"schema_version": "1.0", "workstreams": [workstream]})
    _write_yaml(prog_dir / "workstream_registry.yaml", {
        "schema_version": "1.0",
        "workstreams": [{
            "id": "ws1",
            "sub_program_id": "sub1",
            "lifecycle_state": "active",
            "deep_context": {"why": "why text", "what": "what text"},
            "last_reviewed_date": str(date.today()),
            "roles": [],
            "stakeholders": [],
        }],
    })
    return tmp_path / "programs"


def test_top_level_only_stakeholder_is_recognized_and_produces_no_stk04(tmp_path: Path) -> None:
    programs_root = _write_program(
        tmp_path,
        top_level_stakeholders=[{"alias": "alice", "role": "owner"}],
        raci_alias="alice",
    )

    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)

    assert "alice" in {s.alias for s in ctx.stakeholder_register}
    assert not [v for v in ctx.invariant_violations if v.code in ("STK-01", "STK-04")]


def test_charter_only_stakeholder_is_recognized_and_produces_no_stk04(tmp_path: Path) -> None:
    programs_root = _write_program(
        tmp_path,
        charter_stakeholders=[{"alias": "bob", "role": "owner"}],
        raci_alias="bob",
    )

    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)

    assert "bob" in {s.alias for s in ctx.stakeholder_register}
    assert not [v for v in ctx.invariant_violations if v.code in ("STK-01", "STK-04")]


def test_equivalent_duplicate_across_both_sources_produces_no_stk04(tmp_path: Path) -> None:
    programs_root = _write_program(
        tmp_path,
        top_level_stakeholders=[{"alias": "carol", "role": "owner", "email": "carol@example.com"}],
        charter_stakeholders=[{"alias": "carol", "role": "owner", "email": "carol@example.com"}],
        raci_alias="carol",
    )

    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)

    assert not [v for v in ctx.invariant_violations if v.code == "STK-04"]
    carol = next(s for s in ctx.stakeholder_register if s.alias == "carol")
    assert carol.role == "owner"


def test_conflicting_duplicate_across_both_sources_raises_stk04_error(tmp_path: Path) -> None:
    programs_root = _write_program(
        tmp_path,
        top_level_stakeholders=[{"alias": "dave", "role": "reviewer", "email": "dave@example.com"}],
        charter_stakeholders=[{"alias": "dave", "role": "owner", "email": "dave@example.com"}],
        raci_alias="dave",
    )

    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)

    stk04 = [v for v in ctx.invariant_violations if v.code == "STK-04"]
    assert len(stk04) == 1
    assert stk04[0].severity == InvariantSeverity.ERROR
    assert stk04[0].entity_id == "dave"
    assert "reviewer" in stk04[0].detail
    assert "owner" in stk04[0].detail


def test_conflicting_duplicate_still_counts_as_known_for_stk01_charter_wins(tmp_path: Path) -> None:
    # A conflicting duplicate is an ERROR (STK-04), but the alias must still
    # be recognized (charter's value wins) -- it must not ALSO produce a
    # false "unknown alias" STK-01 violation on top of the real STK-04 one.
    programs_root = _write_program(
        tmp_path,
        top_level_stakeholders=[{"alias": "dave", "role": "reviewer"}],
        charter_stakeholders=[{"alias": "dave", "role": "owner"}],
        raci_alias="dave",
    )

    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)

    assert not [v for v in ctx.invariant_violations if v.code == "STK-01"]
    dave = next(s for s in ctx.stakeholder_register if s.alias == "dave")
    assert dave.role == "owner"  # Charter is canonical.


def test_conflicting_email_only_also_raises_stk04(tmp_path: Path) -> None:
    programs_root = _write_program(
        tmp_path,
        top_level_stakeholders=[{"alias": "erin", "role": "owner", "email": "erin.old@example.com"}],
        charter_stakeholders=[{"alias": "erin", "role": "owner", "email": "erin.new@example.com"}],
    )

    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)

    assert len([v for v in ctx.invariant_violations if v.code == "STK-04"]) == 1


def test_stk01_still_fires_for_a_genuinely_unknown_alias(tmp_path: Path) -> None:
    # Zero-regression proof: an alias in neither source is still flagged.
    programs_root = _write_program(
        tmp_path,
        top_level_stakeholders=[{"alias": "alice"}],
        raci_alias="totally_unknown_alias",
    )

    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)

    assert [v for v in ctx.invariant_violations if v.code == "STK-01"]


def test_no_stakeholders_in_either_source_is_not_an_stk04_error(tmp_path: Path) -> None:
    programs_root = _write_program(tmp_path)

    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)

    assert not [v for v in ctx.invariant_violations if v.code == "STK-04"]
    assert ctx.stakeholder_register == ()

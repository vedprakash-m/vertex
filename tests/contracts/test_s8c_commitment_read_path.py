"""S-8c: commitment read-path overlay (γ-Read → ProgramReality).

Extends the S-8a demo read-path slice to the ``commitment`` family — one of
the four v1-authoritative families (``commitment.date_set``). When the
``commitment`` family's SoR mode is non-legacy (``shadow``/``primary``),
``commitment_store.load_commitment_entries()`` must project from
``ProgramReality.commitments()`` instead of the legacy Plane 1 shim, with a
graceful fallback to the legacy path if ProgramReality is unavailable.

This mirrors ``MilestoneStage._load_milestones_via_reality`` exactly: an
overlay that is *only* active when the family is non-legacy, never breaks the
read path on a ProgramReality error, and never changes behaviour in ``legacy``
mode. The public ``load_commitment_entries()`` return signature is preserved
(backward compatible).

Zone A contract test (INV-3 applies).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from src.core import commitment_store
from src.core.commitment_store import CommitmentEntry, SlipRecord


def _legacy_entry(commitment_id: str = "cm-legacy-1") -> CommitmentEntry:
    return CommitmentEntry(
        commitment_id=commitment_id,
        title="Legacy commitment",
        dri="dri@x",
        due_date="2026-07-01",
        direction="outbound",
        status="active",
        description="from YAML shim",
        entity_ref=None,
        slip_history=(),
        program_id="xpf",
    )


def _reality_record(commitment_id: str = "cm-reality-1") -> SimpleNamespace:
    """A record-like object that ProgramReality.commitments() exposes via ``.record``."""
    return SimpleNamespace(
        commitment_id=commitment_id,
        title="Reality commitment",
        dri="dri@y",
        due_date="2026-08-01",
        direction="inbound",
        status="active",
        description="from ProgramReality",
        entity_ref="ws-1",
        slip_history=(
            {
                "slipped_at": "2026-06-01T00:00:00+00:00",
                "old_due_date": "2026-07-01",
                "new_due_date": "2026-08-01",
                "reason": "dependency slip",
            },
        ),
        program_id="xpf",
    )


def _seed_sor_state(tmp_path, family_mode: str) -> None:
    """Write a fact_store_sor.yaml with a per-family commitment mode."""
    state = (
        "schema_version: '2'\n"
        "mode: legacy\n"
        f"family_modes:\n  commitment: {family_mode}\n"
        "recorded_at: 2026-06-28T00:00:00+00:00\n"
        "recorded_by: test\n"
    )
    (tmp_path / "xpf").mkdir(parents=True, exist_ok=True)
    (tmp_path / "xpf" / "fact_store_sor.yaml").write_text(state, encoding="utf-8")


def test_legacy_mode_uses_legacy_path(monkeypatch, tmp_path) -> None:
    """In legacy mode, the reality overlay is never consulted."""
    _seed_sor_state(tmp_path, family_mode="legacy")

    reality_calls: list[str] = []

    def _fail_via_reality(*_a, **_kw):  # pragma: no cover - must not run
        reality_calls.append("called")
        raise AssertionError("reality overlay must not run in legacy mode")

    monkeypatch.setattr(commitment_store, "_load_commitment_entries_via_reality", _fail_via_reality)
    monkeypatch.setattr(
        commitment_store,
        "_load_commitment_entries_legacy",
        lambda *_a, **_kw: (_legacy_entry(),),
    )

    entries = commitment_store.load_commitment_entries("xpf", programs_root=tmp_path)
    assert reality_calls == []
    assert len(entries) == 1
    assert entries[0].commitment_id == "cm-legacy-1"


def test_shadow_mode_projects_from_reality(monkeypatch, tmp_path) -> None:
    """Non-legacy mode projects commitments from ProgramReality.commitments()."""
    _seed_sor_state(tmp_path, family_mode="shadow")

    def _overlay(*_a, **_kw):
        return tuple(
            commitment_store._commitment_entry_from_record(r)
            for r in (_reality_record(),)
        )

    monkeypatch.setattr(commitment_store, "_load_commitment_entries_via_reality", _overlay)

    entries = commitment_store.load_commitment_entries("xpf", programs_root=tmp_path)
    assert len(entries) == 1
    assert entries[0].commitment_id == "cm-reality-1"
    assert entries[0].direction == "inbound"
    assert entries[0].entity_ref == "ws-1"
    assert entries[0].slip_count == 1
    assert isinstance(entries[0].slip_history[0], SlipRecord)


def test_reality_unavailable_falls_back_to_legacy(monkeypatch, tmp_path, caplog) -> None:
    """A ProgramReality failure must not break the read path — graceful fallback + warn."""
    _seed_sor_state(tmp_path, family_mode="primary")

    def _boom(*_a, **_kw):
        raise RuntimeError("ProgramReality unavailable")

    monkeypatch.setattr(commitment_store, "_load_commitment_entries_via_reality", _boom)
    monkeypatch.setattr(
        commitment_store,
        "_load_commitment_entries_legacy",
        lambda *_a, **_kw: (_legacy_entry("cm-fallback"),),
    )

    with caplog.at_level(logging.WARNING, logger="src.core.commitment_store"):
        entries = commitment_store.load_commitment_entries("xpf", programs_root=tmp_path)

    # Fallback used
    assert len(entries) == 1
    assert entries[0].commitment_id == "cm-fallback"
    # A warning must be surfaced (never silent degrade)
    assert any(
        "commitment" in rec.message.lower() or "reality" in rec.message.lower()
        for rec in caplog.records
    ), f"expected a fallback warning, got: {[r.message for r in caplog.records]}"


def test_commitment_entry_from_record_maps_all_fields() -> None:
    """The record → CommitmentEntry mapping preserves every payload field."""
    record = _reality_record()
    entry = commitment_store._commitment_entry_from_record(record)
    assert entry.commitment_id == "cm-reality-1"
    assert entry.title == "Reality commitment"
    assert entry.dri == "dri@y"
    assert entry.due_date == "2026-08-01"
    assert entry.direction == "inbound"
    assert entry.status == "active"
    assert entry.description == "from ProgramReality"
    assert entry.entity_ref == "ws-1"
    assert entry.program_id == "xpf"
    assert entry.is_slipped
    assert entry.slip_count == 1
    assert entry.slip_history[0].reason == "dependency slip"


def test_commitment_entry_from_record_handles_missing_slip_history() -> None:
    record = SimpleNamespace(
        commitment_id="cm-x",
        title="t",
        dri="d",
        due_date="2026-01-01",
        direction="outbound",
        status="active",
        description="",
        entity_ref=None,
        slip_history=None,
        program_id="xpf",
    )
    entry = commitment_store._commitment_entry_from_record(record)
    assert entry.slip_history == ()
    assert not entry.is_slipped


def test_direction_filter_applies_after_overlay(monkeypatch, tmp_path) -> None:
    """Direction/status filters apply to the reality-overlaid result set too."""
    _seed_sor_state(tmp_path, family_mode="shadow")

    def _overlay(*_a, **_kw):
        recs = [_reality_record("in-1"), _reality_record("out-1")]
        # second one is outbound to exercise the filter
        recs[1] = SimpleNamespace(**{**recs[1].__dict__, "direction": "outbound", "commitment_id": "out-1"})
        return tuple(commitment_store._commitment_entry_from_record(r) for r in recs)

    monkeypatch.setattr(commitment_store, "_load_commitment_entries_via_reality", _overlay)

    inbound = commitment_store.load_commitment_entries(
        "xpf", programs_root=tmp_path, direction="inbound"
    )
    assert {e.commitment_id for e in inbound} == {"in-1"}

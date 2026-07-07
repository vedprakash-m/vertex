"""S-8d: workstream/ownership read-path overlay (γ-Read → ProgramReality).

Extends the S-8a demo read-path slice to the ``workstream.entry`` fact type —
the ``workitem.state`` authority family that carries ``ownership.changed``
(one of the four v1-authoritative families). When the ``workitem.state``
family's SoR mode is non-legacy (``shadow``/``primary``),
``load_current_workstreams()`` must project from
``ProgramReality.workstreams()`` instead of the legacy Plane 1 shim, with a
graceful fallback to the legacy path if ProgramReality is unavailable.

This mirrors ``MilestoneStage._load_milestones_via_reality`` and the S-8c
commitment overlay: the overlay is *only* active when the family is
non-legacy, never breaks the read path on a ProgramReality error, and never
changes behaviour in ``legacy`` mode. The public
``load_current_workstreams()`` return signature is preserved (backward
compatible).

Zone A contract test (INV-3 applies).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from src.core import program_fact_store
from src.core.models_v2 import Workstream


def _legacy_workstream(ws_id: str = "ws-legacy-1") -> Workstream:
    return Workstream(
        id=ws_id,
        name="Legacy workstream",
        aliases=(),
        area_paths=(),
        ado_team=None,
        ado_pipeline_ids=(),
        ado_repository_ids=(),
        pm_owner="pm@x",
        eng_owner="eng@x",
        accountable_owner=None,
        accountable_email=None,
        responsible_owners=(),
        consulted_owners=(),
        informed_owners=(),
        dri_email=None,
        alternate_owner=None,
        always_notify=(),
        description=None,
        why_it_matters=None,
        history_summary=None,
        leadership_sensitivity=None,
        current_blocker=None,
        ado_saved_query_ids=(),
        last_reviewed_date=None,
        signal_sources=None,
        owner_person_id=None,
        status="active",
    )


def _reality_workstream(ws_id: str = "ws-reality-1") -> Workstream:
    ws = _legacy_workstream(ws_id)
    # ownership.changed → a different owner than the legacy record
    return Workstream(
        id=ws_id,
        name="Reality workstream",
        aliases=ws.aliases,
        area_paths=ws.area_paths,
        ado_team=ws.ado_team,
        ado_pipeline_ids=ws.ado_pipeline_ids,
        ado_repository_ids=ws.ado_repository_ids,
        pm_owner="new-pm@y",  # ownership change surfaced via ProgramReality
        eng_owner=ws.eng_owner,
        accountable_owner=ws.accountable_owner,
        accountable_email=ws.accountable_email,
        responsible_owners=ws.responsible_owners,
        consulted_owners=ws.consulted_owners,
        informed_owners=ws.informed_owners,
        dri_email=ws.dri_email,
        alternate_owner=ws.alternate_owner,
        always_notify=ws.always_notify,
        description=ws.description,
        why_it_matters=ws.why_it_matters,
        history_summary=ws.history_summary,
        leadership_sensitivity=ws.leadership_sensitivity,
        current_blocker=ws.current_blocker,
        ado_saved_query_ids=ws.ado_saved_query_ids,
        last_reviewed_date=ws.last_reviewed_date,
        signal_sources=ws.signal_sources,
        owner_person_id=ws.owner_person_id,
        status=ws.status,
    )


def _seed_sor_state(tmp_path, family_mode: str) -> None:
    """Write a fact_store_sor.yaml with a per-family workitem.state mode."""
    state = (
        "schema_version: '2'\n"
        "mode: legacy\n"
        f"family_modes:\n  workitem.state: {family_mode}\n"
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

    monkeypatch.setattr(program_fact_store, "_load_workstreams_via_reality", _fail_via_reality)
    monkeypatch.setattr(
        program_fact_store,
        "_load_current_workstreams_legacy",
        lambda *_a, **_kw: (_legacy_workstream(),),
    )

    workstreams = program_fact_store.load_current_workstreams("xpf", programs_root=tmp_path)
    assert reality_calls == []
    assert len(workstreams) == 1
    assert workstreams[0].id == "ws-legacy-1"


def test_shadow_mode_projects_from_reality(monkeypatch, tmp_path) -> None:
    """Non-legacy mode projects workstreams from ProgramReality.workstreams()."""
    _seed_sor_state(tmp_path, family_mode="shadow")

    monkeypatch.setattr(
        program_fact_store,
        "_load_workstreams_via_reality",
        lambda *_a, **_kw: (_reality_workstream(),),
    )

    workstreams = program_fact_store.load_current_workstreams("xpf", programs_root=tmp_path)
    assert len(workstreams) == 1
    assert workstreams[0].id == "ws-reality-1"
    # ownership.changed value surfaces through the read facade
    assert workstreams[0].pm_owner == "new-pm@y"


def test_reality_unavailable_falls_back_to_legacy(monkeypatch, tmp_path, caplog) -> None:
    """A ProgramReality failure must not break the read path — graceful fallback + warn."""
    _seed_sor_state(tmp_path, family_mode="primary")

    def _boom(*_a, **_kw):
        raise RuntimeError("ProgramReality unavailable")

    monkeypatch.setattr(program_fact_store, "_load_workstreams_via_reality", _boom)
    monkeypatch.setattr(
        program_fact_store,
        "_load_current_workstreams_legacy",
        lambda *_a, **_kw: (_legacy_workstream("ws-fallback"),),
    )

    with caplog.at_level(logging.WARNING, logger="src.core.program_fact_store"):
        workstreams = program_fact_store.load_current_workstreams("xpf", programs_root=tmp_path)

    assert len(workstreams) == 1
    assert workstreams[0].id == "ws-fallback"
    assert any(
        "workstream" in rec.message.lower() or "reality" in rec.message.lower()
        for rec in caplog.records
    ), f"expected a fallback warning, got: {[r.message for r in caplog.records]}"


def test_reality_overlay_returns_workstream_records(monkeypatch, tmp_path) -> None:
    """Overlay returns Workstream records (not FactAssessments) — signature preserved."""
    _seed_sor_state(tmp_path, family_mode="primary")

    monkeypatch.setattr(
        program_fact_store,
        "_load_workstreams_via_reality",
        lambda *_a, **_kw: (_reality_workstream("a"), _reality_workstream("b")),
    )

    workstreams = program_fact_store.load_current_workstreams("xpf", programs_root=tmp_path)
    assert {ws.id for ws in workstreams} == {"a", "b"}
    assert all(isinstance(ws, Workstream) for ws in workstreams)

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
import pytest
import yaml

from src.commands import report_lookback as report_lookback_module
from src.core.exceptions import ConfigError
from src.core.fact_sor_state import save_fact_sor_state
from src.core.html_renderer import HTMLRenderer, RenderContext
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.fact_bridge import append_bridged_assumption_event
from src.core.ledger.source_refs import OperatorAssertionRef
from src.core.models import DeltaSet, EditionType, FreshnessReport, ProgramContext, ReportData, ReviewSection, ReviewState, ReviewStatus, RiskLevel
from src.core.program_fact_store import FactLineage, ProgramFactInput, ProgramFactSnapshot, ProgramFactStore
from src.core.program_reality import FactAssessment, ProgramReality
from src.core.truth_levels import TruthLevel
from src.core.view_models import EditionMeta, HealthSummary
from src.core.models_v2 import Assumption, AssumptionStatus

NOW = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)


def _snapshot(program_id: str, when: datetime) -> ProgramFactSnapshot:
    return ProgramFactSnapshot(program_id=program_id, as_of=when, facts=())


def _seed_program(programs_root: Path, program_id: str) -> None:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": program_id,
                "name": program_id.upper(),
                "storage_backend": "sqlite",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _assumption(assumption_id: str, *, text: str = "Synthetic assumption") -> Assumption:
    return Assumption(
        id=assumption_id,
        program_id="nova",
        text=text,
        validation_method=None,
        validation_due=date(2026, 7, 9),
        status=AssumptionStatus.UNVALIDATED,
        linked_risk_id=None,
        linked_milestone_id=None,
        owner_alias="owner@example.com",
        identified_date=NOW.date(),
        entity_refs=(f"ASSUMPTION:{assumption_id}",),
    )


def _assessment(
    record: Assumption,
    *,
    truth_level: TruthLevel,
    disputed: bool = False,
    stale: bool = False,
) -> FactAssessment:
    return FactAssessment(
        record=record,
        fact_id=f"fact:{record.id}",
        truth_level=truth_level,
        disputed=disputed,
        stale=stale,
        provisional_inputs=False,
        evidence=("synthetic fixture",),
        lineage=FactLineage(
            source_document_key="email:sha256:fixture",
            approval_event_id="evt-approval",
            domain_event_id="evt-source",
        ),
    )


def _reality(*, assumptions: tuple[FactAssessment, ...]) -> ProgramReality:
    return ProgramReality(
        program_id="nova",
        snapshot=_snapshot("nova", NOW),
        sor_mode="shadow",
        as_of=NOW,
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=assumptions,
        _workstreams=(),
        _claims=(),
        _commitments=(),
        _family_sor_modes={"judgment": "shadow"},
    )


def _snapshots() -> tuple[object, ...]:
    return (
        type("SnapshotStub", (), {"ado_data_as_of": datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)})(),
        type("SnapshotStub", (), {"ado_data_as_of": datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)})(),
    )


def _render_context(*, assumption_lifecycle) -> RenderContext:
    report = ReportData(
        issue_number=42,
        edition=EditionType.LOOKBACK,
        generated_at=NOW,
        ado_data_as_of=NOW,
        program=ProgramContext(
            program_name="NOVA",
            mission="Synthetic lookback",
            pillars=(),
            workstreams=(),
            glossary={},
            people=(),
        ),
        items=(),
        deltas=DeltaSet(
            issue_number=42,
            previous_issue_number=41,
            new_items=(),
            closed_items=(),
            risk_changes=(),
            eta_changes=(),
            unchanged_count=0,
        ),
        scorecard=(),
        scorecard_deltas=(),
        exec_summary_text="Synthetic lookback render context.",
        workstream_blurbs={},
        freshness=FreshnessReport(issue_number=42, items=(), blocks=0, warns=0, infos=0),
        hygiene_warnings=(),
        review_status=ReviewStatus(
            issue_number=42,
            sections=(
                ReviewSection(
                    section_id="assumption_lifecycle",
                    state=ReviewState.APPROVED,
                    reviewer="reviewer@example.com",
                    note=None,
                    updated_at=NOW,
                ),
            ),
        ),
        manifest_id="12345678-1234-5678-1234-567812345678",
    )
    return RenderContext(
        title="Synthetic lookback",
        subtitle="",
        preheader="Synthetic preheader",
        report=report,
        edition_meta=EditionMeta(
            edition="nova_weekly",
            issue_number=42,
            generated_at=NOW,
            ado_data_as_of=NOW,
            manifest_id=report.manifest_id,
            qg_status="PASS",
        ),
        health=HealthSummary(
            overall_risk=RiskLevel.LOW,
            high_count=0,
            medium_count=0,
            low_count=1,
            done_count=0,
            total_count=1,
            delta_direction="unchanged",
            prior_counts=None,
        ),
        assumption_lifecycle=assumption_lifecycle,
    )


def test_lookback_assumption_loader_legacy_mode_preserves_legacy_behavior(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"judgment": "legacy"},
        recorded_at=NOW,
        recorded_by="test",
        programs_root=programs_root,
    )
    legacy_assumption = _assumption("assumption-legacy", text="Legacy assumption")
    monkeypatch.setattr(report_lookback_module, "load_current_assumptions", lambda *_a, **_kw: (legacy_assumption,))

    assumptions, assessments, warnings, _ = report_lookback_module._load_lookback_assumptions(
        program_id="nova",
        as_of=NOW,
        programs_root=programs_root,
        load_program_reality=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("reality path must not run")),
    )

    assert assumptions == (legacy_assumption,)
    assert assessments is None
    assert warnings == ()


def test_lookback_assumption_non_legacy_surfaces_fact_assessment_metadata_into_render(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"judgment": "shadow"},
        recorded_at=NOW,
        recorded_by="test",
        programs_root=programs_root,
    )
    assumption = _assumption("assumption-reality", text="Reality assumption")
    summary, warnings = report_lookback_module._build_lookback_assumption_lifecycle(
        program_id="nova",
        snapshots=_snapshots(),
        as_of=NOW,
        programs_root=programs_root,
        load_program_reality=lambda *_a, **_kw: _reality(
            assumptions=(
                _assessment(
                    assumption,
                    truth_level=TruthLevel.RAW_OBSERVED,
                    disputed=True,
                ),
            )
        ),
    )

    assert warnings == ()
    rendered = HTMLRenderer("nova_weekly").render(_render_context(assumption_lifecycle=summary))

    assert "○ UNCONFIRMED" in rendered
    assert "[DISPUTED ⚠]" in rendered
    assert "⚠ includes unconfirmed sources" in rendered


def test_lookback_assumption_empty_set_cross_check_warns_when_legacy_has_data(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"judgment": "shadow"},
        recorded_at=NOW,
        recorded_by="test",
        programs_root=programs_root,
    )
    monkeypatch.setattr(
        report_lookback_module,
        "_load_legacy_assumptions",
        lambda *_a, **_kw: (_assumption("assumption-legacy"),),
    )

    assumptions, assessments, warnings, _ = report_lookback_module._load_lookback_assumptions(
        program_id="nova",
        as_of=NOW,
        programs_root=programs_root,
        load_program_reality=lambda *_a, **_kw: _reality(assumptions=()),
    )

    assert assumptions == ()
    assert assessments == ()
    assert len(warnings) == 1
    assert "ProgramReality returned 0 assumptions" in warnings[0]


@pytest.mark.parametrize("program_mode", ("legacy", "primary"))
@pytest.mark.parametrize("operation", ("cross_check", "rollback"))
def test_nonlegacy_assumption_checks_and_rollback_use_only_authored_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, program_mode: str, operation: str,
) -> None:
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.delenv("VERTEX_REPORT_ALLOW_LEGACY_ASSUMPTION_ROLLBACK", raising=False)
    if operation == "rollback":
        monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_ASSUMPTION_ROLLBACK", "1")
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova", mode=program_mode, family_modes={"judgment": "primary"},
        recorded_at=datetime(2020, 1, 1, tzinfo=timezone.utc), programs_root=programs_root,
    )
    path = programs_root / "nova" / "assumptions.yaml"
    path.write_text(
        "schema_version: '1.0'\nassumptions:\n"
        "  - id: shared-assumption\n    text: Authored assumption\n"
        "    status: unvalidated\n    identified_date: '2020-01-01'\n",
        encoding="utf-8",
    )
    original = path.read_bytes()
    store = ProgramFactStore("nova", db_root=tmp_path / "database")
    for assumption_id in ("shared-assumption", "stored-only"):
        store.append_fact(
            ProgramFactInput(
                "assumption.entry", (f"ASSUMPTION:{assumption_id}",),
                {
                    "id": assumption_id, "program_id": "nova", "text": "Stored assumption",
                    "status": "unvalidated", "identified_date": "2020-01-01",
                },
            ),
            recorded_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )

    def load_reality(*_args, **_kwargs):
        if operation == "rollback":
            raise RuntimeError("Synthetic reality failure")
        return _reality(assumptions=())

    assumptions, assessments, warnings, _ = report_lookback_module._load_lookback_assumptions(
        program_id="nova", as_of=NOW, programs_root=programs_root,
        archive_root=tmp_path / "archive", load_program_reality=load_reality,
    )

    if operation == "cross_check":
        assert assumptions == assessments == ()
        assert "legacy source has 1" in warnings[0]
    else:
        assert tuple(item.text for item in assumptions) == ("Authored assumption",)
        assert "audited rollback flag" in warnings[0]
    assert path.read_bytes() == original


def test_assumption_comparison_reports_invalid_authored_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova", mode="primary", recorded_at=NOW, programs_root=programs_root,
    )
    (programs_root / "nova" / "assumptions.yaml").write_text(
        "schema_version: '2.0'\nassumptions: []\n", encoding="utf-8",
    )

    assumptions, assessments, warnings, _ = report_lookback_module._load_lookback_assumptions(
        program_id="nova", as_of=NOW, programs_root=programs_root,
        load_program_reality=lambda *_a, **_kw: _reality(assumptions=()),
    )

    assert assumptions == assessments == ()
    assert "assumption legacy cross-check unavailable" in warnings[0]
    assert "Unsupported assumptions schema_version" in warnings[0]


@pytest.mark.parametrize("record_state", ("empty", "in_window", "outside_window"))
def test_lookback_lifecycle_retains_loader_warning_with_or_without_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record_state: str,
) -> None:
    record = _assumption("authored-assumption")
    if record_state == "outside_window":
        record = replace(record, identified_date=date(2026, 1, 1))
    monkeypatch.setattr(
        report_lookback_module,
        "_load_lookback_assumptions",
        lambda **_kwargs: (
            () if record_state == "empty" else (record,), (),
            ("Synthetic assumption source warning",), {},
        ),
    )

    summary, warnings = report_lookback_module._build_lookback_assumption_lifecycle(
        program_id="nova", snapshots=_snapshots(), as_of=NOW, programs_root=tmp_path / "programs",
    )

    assert (summary is not None) is (record_state == "in_window")
    assert warnings == ("Synthetic assumption source warning",)


def test_lookback_assumption_requires_audited_rollback_flag_for_reality_failure(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"judgment": "primary"},
        recorded_at=NOW,
        recorded_by="test",
        programs_root=programs_root,
    )

    with pytest.raises(ConfigError, match="audited legacy rollback"):
        report_lookback_module._load_lookback_assumptions(
            program_id="nova",
            as_of=NOW,
            programs_root=programs_root,
            load_program_reality=lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("facade unavailable")),
        )


def test_lookback_assumption_audited_rollback_warns_and_uses_legacy(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"judgment": "primary"},
        recorded_at=NOW,
        recorded_by="test",
        programs_root=programs_root,
    )
    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_ASSUMPTION_ROLLBACK", "1")
    legacy_assumption = _assumption("assumption-rollback", text="Rollback assumption")
    monkeypatch.setattr(report_lookback_module, "_load_legacy_assumptions", lambda *_a, **_kw: (legacy_assumption,))

    assumptions, assessments, warnings, _ = report_lookback_module._load_lookback_assumptions(
        program_id="nova",
        as_of=NOW,
        programs_root=programs_root,
        load_program_reality=lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("facade unavailable")),
    )

    assert assumptions == (legacy_assumption,)
    assert assessments == ()
    assert len(warnings) == 1
    assert "degraded to legacy assumption source via audited rollback flag" in warnings[0]


def test_lookback_assumption_render_fallback_injects_visible_warning_banner(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"judgment": "shadow"},
        recorded_at=NOW,
        recorded_by="test",
        programs_root=programs_root,
    )
    legacy_assumption = _assumption("assumption-legacy", text="Legacy fallback assumption")
    monkeypatch.setattr(report_lookback_module, "load_current_assumptions", lambda *_a, **_kw: (legacy_assumption,))
    monkeypatch.setattr(
        report_lookback_module,
        "_build_lookback_assumption_row_from_assessment",
        lambda *_a, **_kw: (_ for _ in ()).throw(AttributeError("record missing text")),
    )

    summary, warnings = report_lookback_module._build_lookback_assumption_lifecycle(
        program_id="nova",
        snapshots=_snapshots(),
        as_of=NOW,
        programs_root=programs_root,
        load_program_reality=lambda *_a, **_kw: _reality(
            assumptions=(
                _assessment(
                    legacy_assumption,
                    truth_level=TruthLevel.SOURCE_VALIDATED,
                ),
            )
        ),
    )
    assert warnings == ()
    rendered = HTMLRenderer("nova_weekly").render(_render_context(assumption_lifecycle=summary))

    assert "older assumption source because live assumption metadata rendering failed" in rendered
    assert "Legacy fallback assumption" in rendered


@pytest.mark.parametrize("shaping_failure", [None, "sort", "rows"])
@pytest.mark.parametrize(
    ("visible_levels", "outside_levels", "expected_footer"),
    [
        ((TruthLevel.RAW_OBSERVED,), (), True),
        ((TruthLevel.SOURCE_VALIDATED,), (), False),
        ((TruthLevel.RAW_OBSERVED, TruthLevel.HUMAN_CONFIRMED), (), True),
        ((TruthLevel.SOURCE_VALIDATED,), (TruthLevel.RAW_OBSERVED,), False),
        ((TruthLevel.RAW_OBSERVED,), (TruthLevel.SOURCE_VALIDATED,), True),
    ],
)
def test_lookback_assumption_disclosure_tracks_visible_assessments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    shaping_failure: str | None,
    visible_levels: tuple[TruthLevel, ...],
    outside_levels: tuple[TruthLevel, ...],
    expected_footer: bool,
) -> None:
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.delenv("VERTEX_REPORT_ALLOW_LEGACY_ASSUMPTION_ROLLBACK", raising=False)
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova", mode="legacy", family_modes={"judgment": "shadow"},
        recorded_at=NOW, programs_root=programs_root,
    )
    assessments = tuple(
        _assessment(
            _assumption(f"visible-{index}", text=f"Visible assumption {index}"),
            truth_level=level,
        )
        for index, level in enumerate(visible_levels)
    ) + tuple(
        _assessment(
            replace(
                _assumption(f"outside-{index}", text=f"Outside assumption {index}"),
                identified_date=date(2026, 6, 1),
            ),
            truth_level=level,
        )
        for index, level in enumerate(outside_levels)
    )
    if shaping_failure:
        def fail_shaping(*_args, **_kwargs):
            raise RuntimeError("Synthetic assessment shaping failure")

        monkeypatch.setattr(
            report_lookback_module,
            "_sort_lookback_assessment_rows"
            if shaping_failure == "sort" else "_build_lookback_assumption_rows_from_assessments",
            fail_shaping,
        )

    summary, warnings = report_lookback_module._build_lookback_assumption_lifecycle(
        program_id="nova", snapshots=_snapshots(), as_of=NOW, programs_root=programs_root,
        load_program_reality=lambda *_a, **_kw: _reality(assumptions=assessments),
    )

    assert warnings == ()
    assert summary is not None
    assert tuple(row.title for row in summary.rows) == tuple(
        f"Visible assumption {index}" for index in range(len(visible_levels))
    )
    assert summary.includes_unconfirmed_sources is expected_footer
    assert (summary.render_warning is not None) is (shaping_failure is not None)
    if shaping_failure:
        assert all(row.evidence_truth_level is None for row in summary.rows)
    rendered = HTMLRenderer("nova_weekly").render(_render_context(assumption_lifecycle=summary))
    assert ("⚠ includes unconfirmed sources" in rendered) is expected_footer
    assert ("○ UNCONFIRMED" in rendered) is (expected_footer and shaping_failure is None)
    assert "Outside assumption" not in rendered


@pytest.mark.parametrize("source", ["legacy", "rollback"])
def test_lookback_assumption_without_assessments_does_not_infer_disclosure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str,
) -> None:
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.delenv("VERTEX_REPORT_ALLOW_LEGACY_ASSUMPTION_ROLLBACK", raising=False)
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova", mode="legacy", family_modes={"judgment": "primary" if source == "rollback" else "legacy"},
        recorded_at=NOW, programs_root=programs_root,
    )
    entry = _assumption("authored", text="Authored assumption")
    monkeypatch.setattr(report_lookback_module, "load_current_assumptions", lambda *_a, **_kw: (entry,))
    monkeypatch.setattr(report_lookback_module, "_load_legacy_assumptions", lambda *_a, **_kw: (entry,))
    if source == "rollback":
        monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_ASSUMPTION_ROLLBACK", "1")

    def fail_reality(*_args, **_kwargs):
        raise RuntimeError("Synthetic reality failure")

    summary, warnings = report_lookback_module._build_lookback_assumption_lifecycle(
        program_id="nova", snapshots=_snapshots(), as_of=NOW, programs_root=programs_root,
        load_program_reality=fail_reality,
    )

    assert summary is not None
    assert summary.includes_unconfirmed_sources is False
    assert summary.render_warning is None
    if source == "rollback":
        assert "audited rollback flag" in warnings[0]
    else:
        assert warnings == ()
    rendered = HTMLRenderer("nova_weekly").render(_render_context(assumption_lifecycle=summary))
    assert "Authored assumption" in rendered
    assert "⚠ includes unconfirmed sources" not in rendered


def test_lookback_assumption_outside_window_does_not_render_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova", mode="legacy", family_modes={"judgment": "shadow"},
        recorded_at=NOW, programs_root=programs_root,
    )
    outside = replace(_assumption("outside"), identified_date=date(2026, 6, 1))
    summary, warnings = report_lookback_module._build_lookback_assumption_lifecycle(
        program_id="nova", snapshots=_snapshots(), as_of=NOW, programs_root=programs_root,
        load_program_reality=lambda *_a, **_kw: _reality(assumptions=(
            _assessment(outside, truth_level=TruthLevel.RAW_OBSERVED),
        )),
    )
    assert summary is None
    assert warnings == ()


def test_lookback_assumption_synthetic_bridged_fact_renders_trust_badge(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    edition_path = programs_root / "nova" / "editions" / "nova_weekly.yaml"
    edition_path.parent.mkdir()
    edition_path.write_text("program_id: nova\n", encoding="utf-8")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"judgment": "shadow"},
        recorded_at=NOW,
        recorded_by="test",
        programs_root=programs_root,
    )
    event = build_event_envelope(
        program_id="nova",
        event_type="assumption.stated.v1",
        occurred_at=NOW,
        recorded_at=NOW,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="synthetic-fixture",
        payload={
            "assumption_id": "assumption:synthetic-bridge-1",
            "statement": "Synthetic bridged assumption",
        },
        source_ref=OperatorAssertionRef(asserted_by="synthetic-fixture", asserted_at=NOW),
    )
    append_bridged_assumption_event(event, db_root=programs_root.parent)

    summary, warnings = report_lookback_module._build_lookback_assumption_lifecycle(
        program_id="nova",
        snapshots=_snapshots(),
        as_of=NOW,
        programs_root=programs_root,
        edition_name="nova_weekly",
    )
    assert warnings == ()
    rendered = HTMLRenderer("nova_weekly").render(_render_context(assumption_lifecycle=summary))

    assert "Synthetic bridged assumption" in rendered
    assert "✔ CONFIRMED" in rendered

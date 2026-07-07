"""Unit tests for W3-1/2/3: RealityCompletenessVector.

Tests verify:
  - Dataclass construction + serialization roundtrip (to_dict)
  - compute_reality_completeness_vector with various program configs
  - Correct unknown_metrics population
  - Source visibility determination (EML/ICS/ADO/Kusto/IcM/Teams)
  - Lineage coverage from fact store (zero facts, some facts, all with lineage)
  - SoR mode per family (default legacy when no state file)
  - Model calibration area (no corpus, corpus present)
  - Integration with RevHealthReport.completeness_vector
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


FIXED_DT = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_programs_root(tmp_path: Path, program_id: str = "test-prog") -> tuple[Path, Path]:
    """Return (programs_root, prog_dir) with an empty program directory."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / program_id
    prog_dir.mkdir(parents=True)
    return programs_root, prog_dir


def _write_minimal_program_yaml(prog_dir: Path, *, m365_rev: bool = False, ado: bool = False, kusto: bool = False) -> None:
    """Write a minimal valid program.yaml."""
    lines = [
        "schema_version: '3.0'",
        "id: test-prog",
        "name: Test Program",
        "chapter_namespace: test",
    ]
    if ado:
        lines += [
            "ado:",
            "  organization: contoso",
            "  project: Foo",
            "  area_paths:",
            "    - Foo\\Bar",
            "  work_item_types:",
            "    - Bug",
            "  date_window_days: 14",
        ]
    if kusto:
        lines += [
            "kusto:",
            "  cluster: https://example.kusto.windows.net",
            "  database: mydb",
            "  enabled: true",
        ]
    if m365_rev:
        lines += [
            "m365:",
            "  enabled: true",
            "  rev:",
            "    profile: search_hydrate",
            "    fact_bridge_enabled: true",
        ]
    (prog_dir / "program.yaml").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# SourceVisibility dataclass
# ---------------------------------------------------------------------------

class TestSourceVisibility:
    def test_to_dict_complete(self) -> None:
        from src.core.reality_completeness import SourceVisibility
        sv = SourceVisibility(surface="eml", status="complete")
        d = sv.to_dict()
        assert d["surface"] == "eml"
        assert d["status"] == "complete"
        assert d["reason"] is None

    def test_to_dict_unavailable_with_reason(self) -> None:
        from src.core.reality_completeness import SourceVisibility
        sv = SourceVisibility(surface="teams", status="unavailable", reason="ZIP schema unconfirmed")
        d = sv.to_dict()
        assert d["status"] == "unavailable"
        assert "ZIP" in d["reason"]


# ---------------------------------------------------------------------------
# ContextCoverageArea
# ---------------------------------------------------------------------------

class TestContextCoverageArea:
    def test_to_dict_round_trip(self) -> None:
        from src.core.reality_completeness import ContextCoverageArea, SourceVisibility
        area = ContextCoverageArea(
            source_visibility=(
                SourceVisibility("eml", "complete"),
                SourceVisibility("ics", "unavailable"),
            ),
            observed_required_surfaces=1,
            expected_required_surfaces=2,
            enumeration_completeness=0.5,
            oldest_unprocessed_source_age_seconds=3600.0,
        )
        d = area.to_dict()
        assert d["observed_required_surfaces"] == 1
        assert d["expected_required_surfaces"] == 2
        assert d["enumeration_completeness"] == 0.5
        assert d["oldest_unprocessed_source_age_seconds"] == 3600.0
        assert len(d["source_visibility"]) == 2


# ---------------------------------------------------------------------------
# RealityIntegrityArea
# ---------------------------------------------------------------------------

class TestRealityIntegrityArea:
    def test_to_dict_with_lineage(self) -> None:
        from src.core.reality_completeness import RealityIntegrityArea
        area = RealityIntegrityArea(
            lineage_numerator=3,
            lineage_denominator=5,
            lineage_coverage=0.6,
            sor_mode_per_family={"judgment": "shadow", "commitment": "legacy"},
            unresolved_entities=None,
        )
        d = area.to_dict()
        assert d["lineage_coverage"] == 0.6
        assert d["sor_mode_per_family"]["judgment"] == "shadow"
        assert d["unresolved_entities"] is None

    def test_to_dict_no_facts(self) -> None:
        from src.core.reality_completeness import RealityIntegrityArea
        area = RealityIntegrityArea(
            lineage_numerator=0,
            lineage_denominator=0,
            lineage_coverage=None,
            sor_mode_per_family={},
            unresolved_entities=None,
        )
        d = area.to_dict()
        assert d["lineage_coverage"] is None


# ---------------------------------------------------------------------------
# ModelCalibrationArea
# ---------------------------------------------------------------------------

class TestModelCalibrationArea:
    def test_no_corpus(self) -> None:
        from src.core.reality_completeness import ModelCalibrationArea
        area = ModelCalibrationArea(
            corpus_present=False,
            macro_precision=None,
            macro_recall=None,
            macro_f1=None,
            abstention_rate=None,
            auto_binding_precision=None,
            auto_binding_coverage=None,
        )
        d = area.to_dict()
        assert d["corpus_present"] is False
        assert d["macro_f1"] is None

    def test_with_metrics(self) -> None:
        from src.core.reality_completeness import ModelCalibrationArea
        area = ModelCalibrationArea(
            corpus_present=True,
            macro_precision=0.82,
            macro_recall=0.77,
            macro_f1=0.795,
            abstention_rate=0.05,
            auto_binding_precision=0.91,
            auto_binding_coverage=0.85,
        )
        d = area.to_dict()
        assert d["macro_f1"] == 0.795


# ---------------------------------------------------------------------------
# RealityCompletenessVector — top-level
# ---------------------------------------------------------------------------

class TestRealityCompletenessVector:
    def test_to_dict_includes_all_areas(self, tmp_path: Path) -> None:
        from src.core.reality_completeness import (
            RealityCompletenessVector, ContextCoverageArea, RealityIntegrityArea,
            ModelCalibrationArea, SourceVisibility, REALITY_COMPLETENESS_SCHEMA_VERSION,
        )
        vec = RealityCompletenessVector(
            program_id="acme",
            computed_at=FIXED_DT,
            schema_version=REALITY_COMPLETENESS_SCHEMA_VERSION,
            context_coverage=ContextCoverageArea(
                source_visibility=(SourceVisibility("eml", "unknown"),),
                observed_required_surfaces=0,
                expected_required_surfaces=1,
                enumeration_completeness=None,
                oldest_unprocessed_source_age_seconds=None,
            ),
            reality_integrity=RealityIntegrityArea(
                lineage_numerator=0,
                lineage_denominator=0,
                lineage_coverage=None,
                sor_mode_per_family={},
                unresolved_entities=None,
            ),
            model_calibration=ModelCalibrationArea(
                corpus_present=False,
                macro_precision=None, macro_recall=None, macro_f1=None,
                abstention_rate=None, auto_binding_precision=None, auto_binding_coverage=None,
            ),
            unknown_metrics=("lineage_coverage", "unresolved_entities"),
        )
        d = vec.to_dict()
        assert d["program_id"] == "acme"
        assert d["schema_version"] == REALITY_COMPLETENESS_SCHEMA_VERSION
        assert "context_coverage" in d
        assert "reality_integrity" in d
        assert "model_calibration" in d
        assert "lineage_coverage" in d["unknown_metrics"]


# ---------------------------------------------------------------------------
# compute_reality_completeness_vector — integration
# ---------------------------------------------------------------------------

class TestComputeRealityCompletenessVector:
    def test_no_program_yaml(self, tmp_path: Path) -> None:
        """When program.yaml is missing, should not crash; EML shows unavailable."""
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, _ = _make_programs_root(tmp_path, "ghost")
        vec = compute_reality_completeness_vector(
            "ghost",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        assert vec.program_id == "ghost"
        assert vec.computed_at == FIXED_DT
        # EML surface: no program.yaml → load_program returns None → "unavailable"
        eml_vis = next(sv for sv in vec.context_coverage.source_visibility if sv.surface == "eml")
        assert eml_vis.status == "unavailable"

    def test_teams_always_unavailable(self, tmp_path: Path) -> None:
        """Teams is always unavailable regardless of program config."""
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, m365_rev=True, ado=True)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        teams_vis = next(sv for sv in vec.context_coverage.source_visibility if sv.surface == "teams")
        assert teams_vis.status == "unavailable"
        assert "ZIP" in (teams_vis.reason or "")

    def test_eml_unknown_when_no_cycle(self, tmp_path: Path) -> None:
        """EML is 'unknown' when REV is configured but no cycle has run."""
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, m365_rev=True)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            last_cycle_stop=None,
            reference_dt=FIXED_DT,
        )
        eml_vis = next(sv for sv in vec.context_coverage.source_visibility if sv.surface == "eml")
        assert eml_vis.status == "unknown"
        assert "no REV cycle" in (eml_vis.reason or "")

    def test_eml_complete_when_cycle_complete_and_not_stale(self, tmp_path: Path) -> None:
        """EML is 'complete' when last cycle completed and inbox not stale."""
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, m365_rev=True)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            last_cycle_stop="complete",
            inbox_newest_age_days=1.0,
            inbox_stale=False,
            reference_dt=FIXED_DT,
        )
        eml_vis = next(sv for sv in vec.context_coverage.source_visibility if sv.surface == "eml")
        assert eml_vis.status == "complete"

    def test_eml_partial_when_inbox_stale(self, tmp_path: Path) -> None:
        """EML is 'partial' when inbox is stale."""
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, m365_rev=True)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            last_cycle_stop="complete",
            inbox_newest_age_days=20.0,
            inbox_stale=True,
            reference_dt=FIXED_DT,
        )
        eml_vis = next(sv for sv in vec.context_coverage.source_visibility if sv.surface == "eml")
        assert eml_vis.status == "partial"
        assert "stale" in (eml_vis.reason or "")

    def test_ado_unknown_when_only_configured(self, tmp_path: Path) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, ado=True)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        ado_vis = next(sv for sv in vec.context_coverage.source_visibility if sv.surface == "ado")
        assert ado_vis.status == "unknown"
        assert vec.context_coverage.enumeration_completeness is None
        assert "enumeration_completeness" in vec.unknown_metrics

    def test_ado_unavailable_when_not_configured(self, tmp_path: Path) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir)  # no ADO
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        ado_vis = next(sv for sv in vec.context_coverage.source_visibility if sv.surface == "ado")
        assert ado_vis.status == "unavailable"

    def test_ics_unavailable_when_no_inbox_dir(self, tmp_path: Path) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, m365_rev=True)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        ics_vis = next(sv for sv in vec.context_coverage.source_visibility if sv.surface == "ics")
        assert ics_vis.status == "unavailable"

    def test_ics_partial_when_inbox_dir_exists(self, tmp_path: Path) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, m365_rev=True)
        (prog_dir / "rev_ics_inbox").mkdir()
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            last_cycle_stop="complete",
            reference_dt=FIXED_DT,
        )
        ics_vis = next(sv for sv in vec.context_coverage.source_visibility if sv.surface == "ics")
        assert ics_vis.status == "partial"

    def test_enumeration_completeness_ratio(self, tmp_path: Path) -> None:
        """A completed REV cycle cannot fill the missing ADO evidence."""
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, m365_rev=True, ado=True)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            last_cycle_stop="complete",
            inbox_newest_age_days=1.0,
            inbox_stale=False,
            reference_dt=FIXED_DT,
        )
        cc = vec.context_coverage
        assert cc.expected_required_surfaces >= 1, (
            f"At least EML should be in expected_required_surfaces; got {cc.expected_required_surfaces}"
        )
        assert cc.observed_required_surfaces == 1
        assert cc.expected_required_surfaces == 2
        assert cc.enumeration_completeness is None

    def test_lineage_coverage_zero_when_no_facts(self, tmp_path: Path) -> None:
        """When fact store has no accepted facts, lineage_coverage is None."""
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        ri = vec.reality_integrity
        assert ri.lineage_denominator == 0
        assert ri.lineage_coverage is None
        assert "lineage_coverage" in vec.unknown_metrics

    def test_sor_mode_default_legacy(self, tmp_path: Path) -> None:
        """When no fact_store_sor.yaml exists, all families default to legacy."""
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        for family, mode in vec.reality_integrity.sor_mode_per_family.items():
            assert mode == "legacy", f"{family} expected legacy, got {mode}"

    def test_unresolved_entities_always_in_unknown_metrics(self, tmp_path: Path) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        assert "unresolved_entities" in vec.unknown_metrics
        assert vec.reality_integrity.unresolved_entities is None

    def test_model_calibration_no_corpus(self, tmp_path: Path) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        mc = vec.model_calibration
        assert mc.corpus_present is False
        assert mc.macro_f1 is None

    def test_model_calibration_with_corpus(self, tmp_path: Path) -> None:
        """When corpus file exists, corpus_present=True."""
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir)
        quality_dir = prog_dir / "_quality"
        quality_dir.mkdir()
        (quality_dir / "rev_labeled_corpus.jsonl").write_text("{}\n", encoding="utf-8")
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        assert vec.model_calibration.corpus_present is True

    def test_model_calibration_reads_metrics_file(self, tmp_path: Path) -> None:
        """When rev_quality_metrics.json exists, metrics are loaded."""
        import json
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir)
        quality_dir = prog_dir / "_quality"
        quality_dir.mkdir()
        (quality_dir / "rev_labeled_corpus.jsonl").write_text("{}\n", encoding="utf-8")
        (quality_dir / "rev_quality_metrics.json").write_text(
            json.dumps({
                "macro_precision": 0.82,
                "macro_recall": 0.78,
                "macro_f1": 0.80,
                "abstention_rate": 0.04,
                "auto_binding_precision": 0.91,
                "auto_binding_coverage": 0.87,
            }),
            encoding="utf-8",
        )
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        mc = vec.model_calibration
        assert mc.macro_f1 == pytest.approx(0.80)
        assert mc.auto_binding_precision == pytest.approx(0.91)

    def test_to_dict_is_json_serializable(self, tmp_path: Path) -> None:
        """to_dict() output must be JSON-serializable."""
        import json
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, m365_rev=True, ado=True)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        serialized = json.dumps(vec.to_dict())
        roundtripped = json.loads(serialized)
        assert roundtripped["program_id"] == "test-prog"
        assert roundtripped["schema_version"] == "reality_completeness.v1"


# ---------------------------------------------------------------------------
# render_completeness_vector_human
# ---------------------------------------------------------------------------

class TestRenderCompletenessVectorHuman:
    def test_render_includes_all_areas(self, tmp_path: Path) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector, render_completeness_vector_human
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, m365_rev=True, ado=True)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        rendered = render_completeness_vector_human(vec)
        assert "Area 1" in rendered
        assert "Area 2" in rendered
        assert "Area 3" in rendered

    def test_render_shows_source_surfaces(self, tmp_path: Path) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector, render_completeness_vector_human
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, m365_rev=True)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        rendered = render_completeness_vector_human(vec)
        assert "eml" in rendered
        assert "teams" in rendered

    def test_render_shows_unknown_metrics(self, tmp_path: Path) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector, render_completeness_vector_human
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir)
        vec = compute_reality_completeness_vector(
            "test-prog",
            programs_root=programs_root,
            reference_dt=FIXED_DT,
        )
        rendered = render_completeness_vector_human(vec)
        assert "unknown_metrics" in rendered


# ---------------------------------------------------------------------------
# Integration: RevHealthReport.completeness_vector
# ---------------------------------------------------------------------------

def _write_gather_evidence(
    programs_root: Path,
    *,
    channel: str | None = "ado",
    degraded: bool = False,
    program_id: str = "test-prog",
    status: str = "committed",
) -> Path:
    from dataclasses import replace
    from src.core.gather_run_manifest import (
        ChannelOutcomeEntry, GatherRunManifest, GatherRunStatus,
        QueryResultEntry, RequiredScopeStatus, compute_manifest_hash, get_committed_run_dir,
        write_manifest,
    )
    manifest = GatherRunManifest(
        run_id="gather-test",
        status=GatherRunStatus(status),
        program_id=program_id,
        actor_identity_type="interactive",
        lease_owner="test",
        lease_fencing_token=1,
        started_at=FIXED_DT,
        scope_as_of=FIXED_DT,
        required_scope_status=RequiredScopeStatus.FULL,
        finished_at=FIXED_DT,
        query_results=(
            QueryResultEntry("query", "scope", "wiql", FIXED_DT, 0, (), "empty", False, "FULL"),
        ),
        channel_outcomes=(
            ChannelOutcomeEntry(channel, degraded, "source failure" if degraded else None, 0.0),
        ) if channel is not None else (),
    )
    manifest = replace(manifest, manifest_hash=compute_manifest_hash(manifest))
    run_dir = get_committed_run_dir("test-prog", manifest.run_id, programs_root=programs_root)
    run_dir.mkdir(parents=True)
    return write_manifest(run_dir, manifest)


class TestStructuredSourceEvidence:
    @pytest.mark.parametrize("surface", ["ado", "kusto"])
    @pytest.mark.parametrize("evidence", [
        "missing", "corrupt", "invalid", "foreign", "running", "wrong-channel",
        "query-only", "duplicate-channel", "invalid-channel", "schema", "hash",
    ])
    def test_unknown_without_applicable_execution(
        self, tmp_path: Path, surface: str, evidence: str,
    ) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, ado=surface == "ado", kusto=surface == "kusto")
        if evidence != "missing":
            path = _write_gather_evidence(
                programs_root,
                channel=None if evidence == "query-only" else "icm" if evidence == "wrong-channel" else surface,
                program_id="foreign" if evidence == "foreign" else "test-prog",
                status="running" if evidence == "running" else "committed",
            )
            if evidence == "corrupt":
                path.write_text("{", encoding="utf-8")
            elif evidence == "invalid":
                path.write_text("[]", encoding="utf-8")
            elif evidence in ("duplicate-channel", "invalid-channel", "schema", "hash"):
                import json
                from src.core.gather_run_manifest import (
                    compute_manifest_hash, read_manifest, write_manifest,
                )
                from dataclasses import replace
                manifest = read_manifest(path.parent)
                if evidence == "duplicate-channel":
                    manifest = replace(manifest, channel_outcomes=manifest.channel_outcomes * 2)
                elif evidence == "invalid-channel":
                    outcome = replace(manifest.channel_outcomes[0], degraded="false")
                    manifest = replace(manifest, channel_outcomes=(outcome,))
                elif evidence == "schema":
                    manifest = replace(manifest, schema_version="unsupported")
                manifest = replace(manifest, manifest_hash=compute_manifest_hash(manifest))
                write_manifest(path.parent, manifest)
                if evidence == "hash":
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["manifest_hash"] = "mismatched"
                    path.write_text(json.dumps(payload), encoding="utf-8")
        vector = compute_reality_completeness_vector("test-prog", programs_root=programs_root)
        visibility = next(v for v in vector.context_coverage.source_visibility if v.surface == surface)
        assert visibility.status == "unknown"
        assert surface in visibility.reason
        assert vector.context_coverage.enumeration_completeness is None
        assert "enumeration_completeness" in vector.unknown_metrics

    @pytest.mark.parametrize("surface", ["ado", "kusto"])
    @pytest.mark.parametrize("degraded", [False, True])
    def test_execution_without_scope_proof_is_partial(
        self, tmp_path: Path, surface: str, degraded: bool,
    ) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, ado=surface == "ado", kusto=surface == "kusto")
        _write_gather_evidence(programs_root, channel=surface, degraded=degraded)
        vector = compute_reality_completeness_vector("test-prog", programs_root=programs_root)
        visibility = next(v for v in vector.context_coverage.source_visibility if v.surface == surface)
        assert visibility.status == "partial"
        assert visibility.reason
        assert vector.context_coverage.observed_required_surfaces == 1
        assert vector.context_coverage.enumeration_completeness is None

    @pytest.mark.parametrize("error", [PermissionError("private-location"), ValueError("private-value"), KeyError("private-key"), TypeError("private-shape")])
    def test_reader_failure_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
    ) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, ado=True)
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr("src.core.gather_run_manifest.resolve_latest_committed_manifest", fail)
        vector = compute_reality_completeness_vector("test-prog", programs_root=programs_root)
        visibility = next(v for v in vector.context_coverage.source_visibility if v.surface == "ado")
        assert visibility.status == "unknown"
        assert type(error).__name__ in visibility.reason
        assert str(error) not in visibility.reason

    @pytest.mark.parametrize("error", [RuntimeError("unexpected"), AttributeError("unexpected")])
    def test_unexpected_reader_bug_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
    ) -> None:
        from src.core.reality_completeness import compute_reality_completeness_vector
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, ado=True)
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr("src.core.gather_run_manifest.resolve_latest_committed_manifest", fail)
        with pytest.raises(type(error), match="unexpected"):
            compute_reality_completeness_vector("test-prog", programs_root=programs_root)

    @pytest.mark.parametrize("execution", [False, True])
    def test_health_json_and_human_preserve_unknown_coverage(self, tmp_path: Path, execution: bool) -> None:
        from src.core.rev.health import build_rev_health_report, render_rev_health_human
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, ado=True, kusto=True)
        if execution:
            _write_gather_evidence(programs_root)
        report = build_rev_health_report("test-prog", programs_root=programs_root)
        coverage = report.to_dict()["completeness_vector"]["context_coverage"]
        statuses = {v["surface"]: v["status"] for v in coverage["source_visibility"]}
        assert statuses["ado"] == ("partial" if execution else "unknown")
        assert statuses["kusto"] == "unknown"
        assert coverage["enumeration_completeness"] is None
        rendered = render_rev_health_human(report)
        assert "enumeration_completeness=unknown" in rendered
        assert "100%" not in rendered


class TestRevHealthReportCompletenessVector:
    def test_completeness_vector_populated_in_health_report(self, tmp_path: Path) -> None:
        """build_rev_health_report populates completeness_vector field (W3-3)."""
        from src.core.rev.health import build_rev_health_report
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, m365_rev=True)
        report = build_rev_health_report("test-prog", programs_root=programs_root)
        assert report.completeness_vector is not None
        assert report.completeness_vector.program_id == "test-prog"

    def test_completeness_vector_in_to_dict(self, tmp_path: Path) -> None:
        """RevHealthReport.to_dict() includes completeness_vector key."""
        from src.core.rev.health import build_rev_health_report
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir)
        report = build_rev_health_report("test-prog", programs_root=programs_root)
        d = report.to_dict()
        assert "completeness_vector" in d

    def test_render_includes_completeness_section(self, tmp_path: Path) -> None:
        """render_rev_health_human includes the completeness vector section."""
        from src.core.rev.health import build_rev_health_report, render_rev_health_human
        programs_root, prog_dir = _make_programs_root(tmp_path)
        _write_minimal_program_yaml(prog_dir, m365_rev=True)
        report = build_rev_health_report("test-prog", programs_root=programs_root)
        rendered = render_rev_health_human(report)
        assert "completeness vector" in rendered.lower() or "Area 1" in rendered

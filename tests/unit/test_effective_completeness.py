"""RELO-W2.4/W2.5: effective completeness projection (downgrade-only) and
dual-surface consistency between `vertex doctor --rev-health` and cockpit."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.gather_run_manifest import (
    ChannelOutcomeEntry,
    FailedRefEntry,
    GatherRunManifest,
    GatherRunStatus,
    QueryResultEntry,
    RequiredScopeStatus,
    ORACLE_RESULT_OPERATOR_EXPORT_MATCH,
    compute_manifest_hash,
    get_committed_run_dir,
    write_manifest,
)
from src.core.reality_completeness import (
    ContextCoverageArea,
    ModelCalibrationArea,
    RealityCompletenessVector,
    RealityIntegrityArea,
    SourceVisibility,
)

T0 = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)
PROGRAM = "test-prog"


def _vector(completeness: float | None = 1.0, visibility: tuple[SourceVisibility, ...] | None = None) -> RealityCompletenessVector:
    visibility = visibility if visibility is not None else (
        SourceVisibility("eml", "complete"),
        SourceVisibility("ado", "unavailable", "ADO not configured in program.yaml"),
    )
    return RealityCompletenessVector(
        program_id=PROGRAM,
        computed_at=T0,
        schema_version="test",
        context_coverage=ContextCoverageArea(
            source_visibility=visibility,
            observed_required_surfaces=1,
            expected_required_surfaces=1,
            enumeration_completeness=completeness,
            oldest_unprocessed_source_age_seconds=None,
        ),
        reality_integrity=RealityIntegrityArea(0, 0, None, {}, None),
        model_calibration=ModelCalibrationArea(False, None, None, None, None, None, None),
        unknown_metrics=(),
    )


def _manifest(**overrides) -> GatherRunManifest:
    base = GatherRunManifest(
        run_id="gather-test",
        status=GatherRunStatus.COMMITTED,
        program_id=PROGRAM,
        actor_identity_type="interactive",
        lease_owner="test",
        lease_fencing_token=1,
        started_at=T0,
        scope_as_of=T0,
        required_scope_status=RequiredScopeStatus.FULL,
        finished_at=T0,
        query_results=(
            QueryResultEntry(
                "q",
                "scope-a",
                "wiql",
                T0,
                0,
                (),
                "empty",
                False,
                "FULL",
                ORACLE_RESULT_OPERATOR_EXPORT_MATCH,
            ),
        ),
        channel_outcomes=(ChannelOutcomeEntry("icm", False, None, 0.0),),
    )
    return replace(base, **overrides)


_TRUNCATED = (QueryResultEntry("q", "scope-a", "wiql", T0, 200, (), "h", True, "FULL"),)
_ERRORED = (QueryResultEntry("q", "scope-a", "wiql", T0, 0, (), "h", False, "ERROR", None, "timeout"),)
_DEGRADED = (ChannelOutcomeEntry("icm", True, "source failure", 0.0),)

_DOWNGRADE_CASES = [
    ({"query_results": _TRUNCATED}, "query_truncated:scope-a"),
    ({"query_results": _ERRORED}, "query_incomplete:scope-a:ERROR"),
    ({"query_results": _ERRORED}, "query_failed:scope-a"),
    ({"channel_outcomes": _DEGRADED}, "channel_degraded:icm"),
    ({"status": GatherRunStatus.FAILED}, "manifest_status:failed"),
    ({"status": GatherRunStatus.QUARANTINED}, "manifest_status:quarantined"),
    ({"required_scope_status": RequiredScopeStatus.PARTIAL}, "required_scope_partial"),
    ({"failed_refs": (FailedRefEntry("work_item", "1", "boom"),)}, "failed_refs:1"),
    (
        {"query_results": (QueryResultEntry("q", "scope-a", "wiql", T0, 0, (), "h", False, "FULL"),)},
        "query_oracle_weak:scope-a",
    ),
    (
        {
            "query_results": (
                QueryResultEntry(
                    "q",
                    "scope-a",
                    "wiql",
                    T0,
                    0,
                    (),
                    "h",
                    False,
                    "FULL",
                    "operator_source_export:mismatch:reported=1:observed=0",
                ),
            )
        },
        "query_oracle_mismatch:scope-a",
    ),
]


class TestProjection:
    def test_fully_complete_run_is_unchanged(self) -> None:
        from src.core.effective_completeness import project_effective_completeness

        result = project_effective_completeness(_vector(), _manifest())
        assert (result.value, result.reasons, result.downgraded) == (1.0, (), False)

    def test_no_manifest_downgrades_full_vector_to_unknown(self) -> None:
        from src.core.effective_completeness import project_effective_completeness

        result = project_effective_completeness(_vector(), None)
        assert result.base == 1.0
        assert result.value is None
        assert result.reasons == ("manifest_missing",)
        assert result.downgraded is True

    @pytest.mark.parametrize(("overrides", "reason"), _DOWNGRADE_CASES)
    def test_partial_sources_downgrade_full(self, overrides: dict, reason: str) -> None:
        from src.core.effective_completeness import project_effective_completeness

        result = project_effective_completeness(_vector(), _manifest(**overrides))
        assert result.base == 1.0
        assert result.value is None
        assert result.downgraded is True
        assert reason in result.reasons

    def test_partial_visibility_is_a_reason(self) -> None:
        from src.core.effective_completeness import project_effective_completeness

        vector = _vector(None, (SourceVisibility("ado", "partial", "x"), SourceVisibility("kusto", "unknown", "y")))
        result = project_effective_completeness(vector, _manifest())
        assert result.reasons == ("source_partial:ado", "source_unknown:kusto")
        assert (result.value, result.downgraded) == (None, False)

    @pytest.mark.parametrize("base", [0.5, None])
    def test_never_upgrades_non_full_value(self, base: float | None) -> None:
        from src.core.effective_completeness import project_effective_completeness

        result = project_effective_completeness(_vector(base), _manifest(query_results=_TRUNCATED))
        assert result.value == base
        assert result.downgraded is False
        assert "query_truncated:scope-a" in result.reasons

    def test_no_vector_has_no_value(self) -> None:
        from src.core.effective_completeness import project_effective_completeness

        result = project_effective_completeness(None, _manifest(query_results=_TRUNCATED))
        assert (result.base, result.value, result.downgraded) == (None, None, False)
        assert result.reasons == (
            "query_oracle_weak:scope-a",
            "query_truncated:scope-a",
            "vector_unavailable",
        )


def _write_committed(programs_root: Path, manifest: GatherRunManifest) -> None:
    manifest = replace(manifest, manifest_hash=compute_manifest_hash(manifest))
    run_dir = get_committed_run_dir(PROGRAM, manifest.run_id, programs_root=programs_root)
    run_dir.mkdir(parents=True)
    write_manifest(run_dir, manifest)


def _doctor_surface(rendered: str) -> tuple[str, tuple[str, ...]] | None:
    for line in rendered.splitlines():
        if line.strip().startswith("effective_completeness="):
            value = line.strip().split("=", 1)[1].split(" ", 1)[0]
            reasons = tuple(line.split("reasons: ", 1)[1].rstrip(")").split(", "))
            return value, reasons
    return None


def _cockpit_surface(snapshot) -> tuple[str, tuple[str, ...]] | None:
    for finding in snapshot.findings:
        if finding.finding_id == "source.completeness.downgraded":
            return "unknown", finding.evidence_refs
    return None


class TestDualSurfaceConsistency:
    def test_doctor_json_preserves_shared_projection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import typer

        from src.commands.doctor_checks.context_checks import run_rev_health
        from src.core.effective_completeness import project_effective_completeness

        programs_root = tmp_path / "programs"
        (programs_root / PROGRAM).mkdir(parents=True)
        manifest = _manifest(query_results=_TRUNCATED)
        _write_committed(programs_root, manifest)
        monkeypatch.setattr(
            "src.core.rev.health.compute_reality_completeness_vector",
            lambda *args, **kwargs: _vector(),
        )

        with pytest.raises(typer.Exit):
            run_rev_health(
                edition=None,
                rev_program=PROGRAM,
                programs_root=programs_root,
                editions_root=tmp_path / "editions",
                format="json",
                resolve_edition_paths_fn=lambda *args, **kwargs: None,
            )

        expected = project_effective_completeness(_vector(), manifest)
        output = json.loads(capsys.readouterr().out)
        assert output["effective_completeness"] == {
            "base": expected.base,
            "value": expected.value,
            "downgraded": expected.downgraded,
            "reasons": list(expected.reasons),
        }

    def test_partial_numeric_projection_keeps_value_and_reasons_in_both_surfaces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.core.cockpit_builder import build_cockpit_snapshot
        from src.core.rev.health import build_rev_health_report, render_rev_health_human

        programs_root = tmp_path / "programs"
        (programs_root / PROGRAM).mkdir(parents=True)
        manifest = _manifest(query_results=_TRUNCATED)
        _write_committed(programs_root, manifest)
        monkeypatch.setattr(
            "src.core.rev.health.compute_reality_completeness_vector",
            lambda *args, **kwargs: _vector(0.5),
        )

        report = build_rev_health_report(PROGRAM, programs_root=programs_root)
        snapshot = build_cockpit_snapshot(PROGRAM, programs_root=programs_root, now=T0)
        finding = next(
            item
            for item in snapshot.findings
            if item.finding_id == "source.completeness.downgraded"
        )

        assert report.effective_completeness is not None
        assert report.effective_completeness.value == 0.5
        assert _doctor_surface(render_rev_health_human(report)) == (
            "50%",
            ("query_oracle_weak:scope-a", "query_truncated:scope-a"),
        )
        assert "50%" in finding.detail
        assert finding.evidence_refs == (
            "query_oracle_weak:scope-a",
            "query_truncated:scope-a",
        )

    def test_missing_manifest_surfaces_unknown_on_doctor_and_cockpit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.core.cockpit_builder import build_cockpit_snapshot
        from src.core.rev.health import build_rev_health_report, render_rev_health_human

        programs_root = tmp_path / "programs"
        (programs_root / PROGRAM).mkdir(parents=True)
        monkeypatch.setattr(
            "src.core.rev.health.compute_reality_completeness_vector",
            lambda *args, **kwargs: _vector(),
        )

        report = build_rev_health_report(PROGRAM, programs_root=programs_root)
        snapshot = build_cockpit_snapshot(PROGRAM, programs_root=programs_root, now=T0)

        assert report.effective_completeness is not None
        assert report.effective_completeness.value is None
        assert report.effective_completeness.reasons == ("manifest_missing",)
        assert _doctor_surface(render_rev_health_human(report)) == ("unknown", ("manifest_missing",))
        assert _cockpit_surface(snapshot) == ("unknown", ("manifest_missing",))

    def test_cockpit_reports_projection_failure_as_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.core.cockpit_builder import build_cockpit_snapshot

        programs_root = tmp_path / "programs"
        (programs_root / PROGRAM).mkdir(parents=True)
        monkeypatch.setattr(
            "src.core.rev.health.build_program_effective_completeness",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("private detail")),
        )

        snapshot = build_cockpit_snapshot(PROGRAM, programs_root=programs_root, now=T0)
        finding = next(
            finding
            for finding in snapshot.findings
            if finding.finding_id == "source.completeness.unavailable"
        )

        assert finding.status == "warn"
        assert "private detail" not in finding.detail
        assert finding.evidence_refs == ("completeness_projection_unavailable",)

    @pytest.mark.parametrize(
        "overrides",
        [{}] + [case[0] for case in _DOWNGRADE_CASES if "status" not in case[0]],
    )
    def test_doctor_and_cockpit_agree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict) -> None:
        from src.core.cockpit_builder import build_cockpit_snapshot
        from src.core.effective_completeness import project_effective_completeness
        from src.core.rev.health import build_rev_health_report, render_rev_health_human

        programs_root = tmp_path / "programs"
        (programs_root / PROGRAM).mkdir(parents=True)
        manifest = _manifest(**overrides)
        _write_committed(programs_root, manifest)
        monkeypatch.setattr("src.core.rev.health.compute_reality_completeness_vector", lambda *a, **k: _vector())

        report = build_rev_health_report(PROGRAM, programs_root=programs_root)
        rendered = render_rev_health_human(report)
        snapshot = build_cockpit_snapshot(PROGRAM, programs_root=programs_root, now=T0)

        expected = project_effective_completeness(_vector(), manifest)
        assert report.effective_completeness == expected
        doctor = _doctor_surface(rendered)
        cockpit = _cockpit_surface(snapshot)
        assert doctor == cockpit
        if overrides:
            assert doctor == ("unknown", expected.reasons)
        else:
            assert doctor is None
            assert rendered == render_rev_health_human(replace(report, effective_completeness=None))
            assert "enumeration_completeness=100%" in rendered

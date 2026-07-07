"""Direct coverage for the extracted confirm claim-extractor resolution cluster.

Guards the D-25 / Phase 3 extraction from ``src/commands/confirm.py`` into
``src/commands/confirm_stages/claim_resolution.py``. These helpers decide
whether AI claim extraction runs and translate extractor failures into operator
warnings; they never persist claims (the durable write stays in confirm.py).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ai.claim_extractor import (
    ClaimExtractorBudgetError,
    ClaimExtractorError,
    ClaimExtractorSafetyError,
)
from src.core.claim_tracker import ClaimExtractionResult
from src.commands.confirm_stages import claim_resolution
from src.commands.confirm_stages.claim_resolution import (
    claim_extractor_mode,
    evaluate_claim_extraction_calibration_gate,
    prepare_confirm_claim_extraction_for_v2,
    record_confirmed_claims_for_v2,
    render_claim_extractor_fallback_warning,
    resolve_confirm_claim_extraction,
)


@pytest.mark.parametrize(
    "raw_program, expected",
    [
        (None, None),
        ("not-a-dict", None),
        ({}, None),
        ({"ai": {"enabled": False}}, None),
        ({"ai": {"enabled": True}}, "calibration"),
        ({"ai": {"enabled": True, "claim_extractor": {}}}, "calibration"),
        ({"ai": {"enabled": True, "claim_extractor": {"mode": "production"}}}, "production"),
        ({"ai": {"enabled": True, "claim_extractor": {"mode": "PRODUCTION"}}}, "production"),
        ({"ai": {"enabled": True, "claim_extractor": {"mode": "bogus"}}}, "calibration"),
        ({"ai": {"enabled": True, "claim_extractor": {"mode": None}}}, "calibration"),
    ],
)
def test_claim_extractor_mode(raw_program, expected) -> None:
    assert claim_extractor_mode(raw_program) == expected


@pytest.mark.parametrize(
    "message, fragment",
    [
        ("HTTP 429 too many requests", "rate limit"),
        ("rate limit exceeded", "rate limit"),
        ("operation timed out", "timed out"),
        ("request timeout", "timed out"),
        ("invalid json payload", "invalid structured output"),
        ("non-object payload received", "invalid structured output"),
        ("some other failure", "fell back to regex extractor: some other failure"),
    ],
)
def test_render_claim_extractor_fallback_warning(message, fragment) -> None:
    assert fragment in render_claim_extractor_fallback_warning(ClaimExtractorError(message))


def _resolved(raw_program: dict):
    return SimpleNamespace(raw_program=raw_program, program=SimpleNamespace(id="acme"))


def _call(resolved, *, legacy=False):
    return resolve_confirm_claim_extraction(
        resolved=resolved,
        edition_name="acme_weekly",
        issue_number=5,
        confirmed_at=datetime(2026, 6, 5, 12, 0, 0),
        narratives={},
        items=(),
        valid_workstream_ids=(),
        legacy_regex_extractor=legacy,
    )


def test_resolve_legacy_regex_short_circuits() -> None:
    assert _call(_resolved({"ai": {"enabled": True}}), legacy=True) == (None, "regex", ())


def test_resolve_ai_disabled_falls_back_to_regex() -> None:
    assert _call(_resolved({"ai": {"enabled": False}})) == (None, "regex", ())


def test_resolve_success_path(monkeypatch) -> None:
    sentinel = object()

    class _FakeExtractor:
        @staticmethod
        def from_program(program):
            return SimpleNamespace(extract_claims=lambda **_kwargs: sentinel)

    monkeypatch.setattr(claim_resolution, "ClaimExtractor", _FakeExtractor)
    result, mode, warnings = _call(_resolved({"ai": {"enabled": True, "claim_extractor": {"mode": "production"}}}))
    assert result is sentinel and mode == "production" and warnings == ()


def test_resolve_extractor_error_falls_back_with_warning(monkeypatch) -> None:
    def _raise(**_kwargs):
        raise ClaimExtractorError("HTTP 429 rate limit")

    class _FakeExtractor:
        @staticmethod
        def from_program(program):
            return SimpleNamespace(extract_claims=_raise)

    monkeypatch.setattr(claim_resolution, "ClaimExtractor", _FakeExtractor)
    result, mode, warnings = _call(_resolved({"ai": {"enabled": True}}))
    assert result is None and mode == "regex"
    assert len(warnings) == 1 and "rate limit" in warnings[0]


@pytest.mark.parametrize(
    "error, fragment",
    [
        (ClaimExtractorBudgetError("budget exceeded"), "budget exceeded"),
        (ClaimExtractorSafetyError("pii detected"), "safety pipeline"),
    ],
)
def test_resolve_budget_and_safety_errors_raise_runtime(monkeypatch, error, fragment) -> None:
    def _raise(**_kwargs):
        raise error

    class _FakeExtractor:
        @staticmethod
        def from_program(program):
            return SimpleNamespace(extract_claims=_raise)

    monkeypatch.setattr(claim_resolution, "ClaimExtractor", _FakeExtractor)
    with pytest.raises(RuntimeError) as excinfo:
        _call(_resolved({"ai": {"enabled": True}}))
    assert fragment in str(excinfo.value)


def _calibration_record(*, mode="calibration", ai_only=0, regex_only=0):
    return SimpleNamespace(mode=mode, ai_only_count=ai_only, regex_only_count=regex_only)


def test_evaluate_calibration_gate_none_and_non_calibration() -> None:
    assert evaluate_claim_extraction_calibration_gate(None).results == ()
    assert evaluate_claim_extraction_calibration_gate(_calibration_record(mode="production")).results == ()


def test_evaluate_calibration_gate_within_tolerance_passes() -> None:
    report = evaluate_claim_extraction_calibration_gate(_calibration_record(ai_only=1, regex_only=1))
    assert len(report.results) == 1
    gate = report.results[0]
    assert gate.gate_id == "QG-CE1" and gate.passed is True and gate.forceable is True


def test_evaluate_calibration_gate_ai_heavy_divergence_fails() -> None:
    report = evaluate_claim_extraction_calibration_gate(_calibration_record(ai_only=3, regex_only=1))
    gate = report.results[0]
    assert gate.passed is False
    assert "AI extraction found more" in gate.message


def test_evaluate_calibration_gate_regex_heavy_divergence_fails() -> None:
    report = evaluate_claim_extraction_calibration_gate(_calibration_record(ai_only=1, regex_only=4))
    gate = report.results[0]
    assert gate.passed is False
    assert "Regex extraction found more" in gate.message


def test_prepare_claim_extraction_unresolved_edition_returns_empty(tmp_path: Path) -> None:
    reports_root = tmp_path / "repo" / "reports"
    reports_root.mkdir(parents=True)
    result = prepare_confirm_claim_extraction_for_v2(
        edition_name="nonexistent_edition",
        issue_number=1,
        confirmed_at=datetime(2026, 6, 5, 12, 0, 0),
        reports_root=reports_root,
        items=(),
    )
    assert result == (None, "", (), None)


def test_prepare_claim_extraction_builds_calibration_record(monkeypatch, tmp_path: Path) -> None:
    reports_root = tmp_path / "repo" / "reports"
    reports_root.mkdir(parents=True)
    resolved = SimpleNamespace(program=SimpleNamespace(id="acme"), workstreams=())
    monkeypatch.setattr(
        claim_resolution,
        "resolve_edition",
        lambda edition_name, editions_root, programs_root: resolved,
    )
    monkeypatch.setattr(claim_resolution, "load_narratives", lambda *a, **k: {})
    extraction_sentinel = ClaimExtractionResult(claims=(), decision_asks=(), warnings=())
    monkeypatch.setattr(
        claim_resolution,
        "resolve_confirm_claim_extraction",
        lambda **_kwargs: (extraction_sentinel, "calibration", ("warn",)),
    )
    built = {}

    def _build(**kwargs):
        built.update(kwargs)
        return "calibration-record"

    monkeypatch.setattr(claim_resolution, "build_claim_extraction_calibration_record", _build)
    result, mode, warnings, record = prepare_confirm_claim_extraction_for_v2(
        edition_name="acme_weekly",
        issue_number=2,
        confirmed_at=datetime(2026, 6, 5, 12, 0, 0),
        reports_root=reports_root,
        items=(),
    )
    assert result is extraction_sentinel and mode == "calibration" and warnings == ("warn",)
    assert record == "calibration-record"
    assert built["ai_extracted"] is extraction_sentinel and built["program_id"] == "acme"


def test_prepare_claim_extraction_regex_mode_skips_calibration_record(monkeypatch, tmp_path: Path) -> None:
    reports_root = tmp_path / "repo" / "reports"
    reports_root.mkdir(parents=True)
    resolved = SimpleNamespace(program=SimpleNamespace(id="acme"), workstreams=())
    monkeypatch.setattr(
        claim_resolution,
        "resolve_edition",
        lambda edition_name, editions_root, programs_root: resolved,
    )
    monkeypatch.setattr(claim_resolution, "load_narratives", lambda *a, **k: {})
    monkeypatch.setattr(
        claim_resolution,
        "resolve_confirm_claim_extraction",
        lambda **_kwargs: (None, "regex", ()),
    )
    result, mode, warnings, record = prepare_confirm_claim_extraction_for_v2(
        edition_name="acme_weekly",
        issue_number=2,
        confirmed_at=datetime(2026, 6, 5, 12, 0, 0),
        reports_root=reports_root,
        items=(),
    )
    assert result is None and mode == "regex" and record is None


def test_record_confirmed_claims_for_v2_returns_empty_when_edition_is_missing(tmp_path: Path) -> None:
    reports_root = tmp_path / "repo" / "reports"
    reports_root.mkdir(parents=True)
    assert (
        record_confirmed_claims_for_v2(
            edition_name="missing_edition",
            issue_number=1,
            confirmed_at=datetime(2026, 6, 5, 12, 0, 0),
            reports_root=reports_root,
            items=(),
        )
        == ()
    )


def test_record_confirmed_claims_for_v2_skips_resolution_when_result_is_precomputed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reports_root = tmp_path / "repo" / "reports"
    reports_root.mkdir(parents=True)
    resolved = SimpleNamespace(
        program=SimpleNamespace(id="acme"),
        workstreams=(SimpleNamespace(id="ws-a", area_paths=("Area\\A",)),),
    )
    extraction_sentinel = ClaimExtractionResult(claims=(), decision_asks=(), warnings=())
    persist_calls: list[dict[str, object]] = []
    resolution_called = False

    monkeypatch.setattr(
        claim_resolution,
        "resolve_edition",
        lambda edition_name, editions_root, programs_root: resolved,
    )
    monkeypatch.setattr(claim_resolution, "load_narratives", lambda *args, **kwargs: {"exec_summary": "ok"})

    def _unexpected_resolution(**_kwargs):
        nonlocal resolution_called
        resolution_called = True
        raise AssertionError("claim extraction should not be re-resolved")

    monkeypatch.setattr(claim_resolution, "resolve_confirm_claim_extraction", _unexpected_resolution)

    def _record(**kwargs):
        persist_calls.append(kwargs)
        return SimpleNamespace(
            warnings=("persisted warning",),
            written_claims=("claim-1",),
            written_decision_asks=(),
        )

    monkeypatch.setattr(claim_resolution, "record_confirmed_claims", _record)

    warnings = record_confirmed_claims_for_v2(
        edition_name="acme_weekly",
        issue_number=2,
        confirmed_at=datetime(2026, 6, 5, 12, 0, 0),
        reports_root=reports_root,
        items=(),
        extraction_result=extraction_sentinel,
        extraction_mode="production",
        resolve_extraction_if_missing=False,
    )

    assert resolution_called is False
    assert persist_calls and persist_calls[0]["extraction_result"] is extraction_sentinel
    assert persist_calls[0]["extraction_mode"] == "production"
    assert warnings == (
        "persisted warning",
        "Claim tracker recorded 1 claim(s) and 0 decision ask(s).",
    )


def test_record_confirmed_claims_for_v2_resolves_missing_extraction_and_merges_warnings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reports_root = tmp_path / "repo" / "reports"
    reports_root.mkdir(parents=True)
    resolved = SimpleNamespace(
        program=SimpleNamespace(id="acme"),
        workstreams=(SimpleNamespace(id="ws-a", area_paths=("Area\\A",)),),
    )
    extraction_sentinel = object()

    monkeypatch.setattr(
        claim_resolution,
        "resolve_edition",
        lambda edition_name, editions_root, programs_root: resolved,
    )
    monkeypatch.setattr(claim_resolution, "load_narratives", lambda *args, **kwargs: {"exec_summary": "ok"})
    monkeypatch.setattr(
        claim_resolution,
        "resolve_confirm_claim_extraction",
        lambda **kwargs: (extraction_sentinel, "calibration", ("extractor warning",)),
    )
    monkeypatch.setattr(
        claim_resolution,
        "record_confirmed_claims",
        lambda **kwargs: SimpleNamespace(
            warnings=("persisted warning",),
            written_claims=(),
            written_decision_asks=("ask-1",),
        ),
    )

    warnings = record_confirmed_claims_for_v2(
        edition_name="acme_weekly",
        issue_number=2,
        confirmed_at=datetime(2026, 6, 5, 12, 0, 0),
        reports_root=reports_root,
        items=(),
    )

    assert warnings == (
        "extractor warning",
        "persisted warning",
        "Claim tracker recorded 0 claim(s) and 1 decision ask(s).",
    )

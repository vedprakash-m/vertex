"""WP-8/GAP-4: Contract tests verifying continuity gates CG-01/05/06 are
parameterized via scorecard_band_block_ids and don't fail for programs that
have a single scorecard or no scorecards.
"""
from __future__ import annotations

from src.core.quality_gates.continuity import evaluate_continuity_gates


_BASE_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Issue 001</title></head>
<body>
<div data-vertex-block="cadence-note">Weekly</div>
<div data-vertex-block="{primary}">Primary Scorecard</div>
<div data-vertex-block="{secondary}">Secondary Scorecard</div>
<div data-vertex-block="exec-summary">Executive Summary</div>
<div data-vertex-block="jump-to-section">Jump to Section</div>
<div data-vertex-block="chapter-workstreams">Workstreams</div>
Issue 001
</body>
</html>
"""

_SINGLE_BAND_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Issue 001</title></head>
<body>
<div data-vertex-block="cadence-note">Weekly</div>
<div data-vertex-block="program-scorecard">The Scorecard</div>
<div data-vertex-block="exec-summary">Executive Summary</div>
<div data-vertex-block="jump-to-section">Jump to Section</div>
<div data-vertex-block="chapter-workstreams">Workstreams</div>
Issue 001
</body>
</html>
"""

_NO_BAND_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Issue 001</title></head>
<body>
<div data-vertex-block="exec-summary">Executive Summary</div>
<div data-vertex-block="jump-to-section">Jump to Section</div>
<div data-vertex-block="chapter-workstreams">Workstreams</div>
Issue 001
</body>
</html>
"""


def test_cg05_passes_for_nova_two_band_layout() -> None:
    """CG-05 passes when primary/secondary bands are in correct order."""
    html = _BASE_HTML.format(primary="scorecard-band-primary", secondary="scorecard-band-secondary")
    report = evaluate_continuity_gates(html_content=html, issue_number=1)
    results = {r.gate_id: r for r in report.results}
    assert results["CG-05"].passed, results["CG-05"].message


def test_cg05_passes_vacuously_for_single_band_program() -> None:
    """CG-05 must pass (vacuous) when only one band is configured."""
    report = evaluate_continuity_gates(
        html_content=_SINGLE_BAND_HTML,
        issue_number=1,
        scorecard_band_block_ids=("program-scorecard",),
    )
    results = {r.gate_id: r for r in report.results}
    assert results["CG-05"].passed, results["CG-05"].message


def test_cg06_passes_vacuously_for_single_band_program() -> None:
    """CG-06 must pass (vacuous) when only one band is configured."""
    report = evaluate_continuity_gates(
        html_content=_SINGLE_BAND_HTML,
        issue_number=1,
        scorecard_band_block_ids=("program-scorecard",),
    )
    results = {r.gate_id: r for r in report.results}
    assert results["CG-06"].passed, results["CG-06"].message


def test_cg01_passes_vacuously_for_no_band_program() -> None:
    """CG-01 must pass (vacuous) when no scorecard bands are configured."""
    report = evaluate_continuity_gates(
        html_content=_NO_BAND_HTML,
        issue_number=1,
        scorecard_band_block_ids=(),
    )
    results = {r.gate_id: r for r in report.results}
    assert results["CG-01"].passed, results["CG-01"].message


def test_cg05_passes_vacuously_for_no_band_program() -> None:
    """CG-05/06 must pass vacuously when no scorecard bands are configured."""
    report = evaluate_continuity_gates(
        html_content=_NO_BAND_HTML,
        issue_number=1,
        scorecard_band_block_ids=(),
    )
    results = {r.gate_id: r for r in report.results}
    assert results["CG-05"].passed
    assert results["CG-06"].passed


def test_custom_band_ids_work_end_to_end() -> None:
    """A program can use fully custom band IDs and CG gates respect them."""
    custom_html = _BASE_HTML.format(primary="my-program-scorecard", secondary="my-program-scorecard-2")
    report = evaluate_continuity_gates(
        html_content=custom_html,
        issue_number=1,
        scorecard_band_block_ids=("my-program-scorecard", "my-program-scorecard-2"),
    )
    results = {r.gate_id: r for r in report.results}
    assert results["CG-05"].passed, results["CG-05"].message
    assert results["CG-06"].passed, results["CG-06"].message


def test_nova_default_bands_still_work_with_no_kwarg() -> None:
    """Callers that don't pass scorecard_band_block_ids get the Acme default behavior."""
    html = _BASE_HTML.format(primary="scorecard-band-primary", secondary="scorecard-band-secondary")
    report_with_kwarg = evaluate_continuity_gates(
        html_content=html,
        issue_number=1,
        scorecard_band_block_ids=("scorecard-band-primary", "scorecard-band-secondary"),
    )
    report_without_kwarg = evaluate_continuity_gates(html_content=html, issue_number=1)
    ids_with = {r.gate_id: r.passed for r in report_with_kwarg.results}
    ids_without = {r.gate_id: r.passed for r in report_without_kwarg.results}
    assert ids_with == ids_without

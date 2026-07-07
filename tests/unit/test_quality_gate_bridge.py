"""Direct coverage for the extracted bridge gates (QG-B1 / QG-B2 / QG-B3).

Guards the D-09 / Phase 3 peel of the bridge cluster from the
``src/core/quality_gates`` package into ``src/core/quality_gates/bridge.py``
(re-exported from ``__init__``).
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from src.core.models import ReviewSection, ReviewState, ReviewStatus
from src.core.quality_gates import evaluate_bridge_gates


def _contract(
    *,
    added=(),
    removed=(),
    removed_by_override=(),
    additions=(),
    removals=(),
    seeded=False,
    files_seeded=(),
    source_hashes=None,
):
    return SimpleNamespace(
        section_roster=SimpleNamespace(added_sections=added, removed_sections=removed),
        scorecard_composition=SimpleNamespace(
            removed_by_override=removed_by_override,
            proposed_additions=additions,
            proposed_removals=removals,
        ),
        narrative_seeding=SimpleNamespace(
            seeded=seeded, files_seeded=files_seeded, source_hashes=source_hashes
        ),
    )


def _review(sections=()):
    return ReviewStatus(issue_number=1, sections=tuple(sections))


def test_no_contract_is_noop() -> None:
    assert evaluate_bridge_gates(
        continuation_contract=None, narratives={}, review_status=_review()
    ).results == ()


def test_clean_contract_all_pass() -> None:
    report = evaluate_bridge_gates(
        continuation_contract=_contract(), narratives={}, review_status=_review()
    )
    by_id = {r.gate_id: r for r in report.results}
    assert by_id["QG-B1"].passed and by_id["QG-B2"].passed and by_id["QG-B3"].passed


def test_section_roster_drift_blocks_b1() -> None:
    report = evaluate_bridge_gates(
        continuation_contract=_contract(added=("ws:new",), removed=("ws:gone",)),
        narratives={},
        review_status=_review(),
    )
    b1 = {r.gate_id: r for r in report.results}["QG-B1"]
    assert b1.passed is False and b1.exit_code == 2 and b1.forceable is True
    assert "added sections: ws:new" in b1.message
    assert "missing prior sections: ws:gone" in b1.message


def test_scorecard_composition_drift_blocks_b2() -> None:
    report = evaluate_bridge_gates(
        continuation_contract=_contract(additions=(("Velocity", "Throughput"),)),
        narratives={},
        review_status=_review(),
    )
    b2 = {r.gate_id: r for r in report.results}["QG-B2"]
    assert b2.passed is False
    assert "proposed additions: Velocity / Throughput" in b2.message


def test_graduated_drift_is_advisory_not_blocking() -> None:
    report = evaluate_bridge_gates(
        continuation_contract=_contract(added=("ws:new",)),
        narratives={},
        review_status=_review(),
        bridge_graduated=True,
    )
    b1 = {r.gate_id: r for r in report.results}["QG-B1"]
    assert b1.passed is False and b1.exit_code == 1  # advisory
    # When graduated, the seeded-narrative gate (QG-B3) is skipped entirely.
    assert "QG-B3" not in {r.gate_id for r in report.results}


def test_seeded_unchanged_without_approval_blocks_b3() -> None:
    content = "Seeded exec summary body.\n"
    digest = f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"
    contract = _contract(seeded=True, files_seeded=("exec_summary.md",), source_hashes={"exec_summary.md": digest})
    report = evaluate_bridge_gates(
        continuation_contract=contract,
        narratives={"exec_summary.md": content},
        review_status=_review(),  # not approved
    )
    b3 = {r.gate_id: r for r in report.results}["QG-B3"]
    assert b3.passed is False
    assert "exec_summary" in b3.message


def test_seeded_unchanged_but_approved_passes_b3() -> None:
    content = "Seeded exec summary body.\n"
    digest = f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"
    contract = _contract(seeded=True, files_seeded=("exec_summary.md",), source_hashes={"exec_summary.md": digest})
    approved = _review(
        sections=(ReviewSection(section_id="exec_summary", state=ReviewState.APPROVED, reviewer="a", note=None, updated_at=None),)
    )
    report = evaluate_bridge_gates(
        continuation_contract=contract, narratives={"exec_summary.md": content}, review_status=approved
    )
    b3 = {r.gate_id: r for r in report.results}["QG-B3"]
    assert b3.passed is True


def test_seeded_changed_content_passes_b3() -> None:
    contract = _contract(
        seeded=True,
        files_seeded=("exec_summary.md",),
        source_hashes={"exec_summary.md": "sha256:deadbeef"},
    )
    report = evaluate_bridge_gates(
        continuation_contract=contract,
        narratives={"exec_summary.md": "Author rewrote this entirely.\n"},
        review_status=_review(),
    )
    b3 = {r.gate_id: r for r in report.results}["QG-B3"]
    assert b3.passed is True

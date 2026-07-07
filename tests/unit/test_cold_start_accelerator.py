"""Unit tests for FR-SG-44: cold_start_accelerator.py"""

from __future__ import annotations

import textwrap
from pathlib import Path

from src.core.cold_start_accelerator import (
    ColdStartSeedResult,
    bootstrap_from_newsletter,
    compute_signal_density_thresholds,
    infer_workstreams_from_program_yaml,
)


# ---------------------------------------------------------------------------
# bootstrap_from_newsletter
# ---------------------------------------------------------------------------

_SAMPLE_NEWSLETTER = textwrap.dedent("""\
    # Acme Weekly — Issue 42

    ## Acme Readiness

    The Acme deployment is at risk due to firmware approval delays.
    The team has decided to move the milestone from May 15 to May 22.

    ## DD on PF

    All gates are on track. No critical blockers identified this week.
    The escalation from last week has been resolved.

    ## Platform Health

    Deployment velocity is within SLA. No concerns.
""")


def test_bootstrap_from_newsletter_returns_seed_result() -> None:
    result = bootstrap_from_newsletter("acme", _SAMPLE_NEWSLETTER)
    assert isinstance(result, ColdStartSeedResult)
    assert result.program_id == "acme"


def test_bootstrap_from_newsletter_infers_workstreams_from_h2() -> None:
    result = bootstrap_from_newsletter("acme", _SAMPLE_NEWSLETTER)
    ws_ids = {ws.workstream_id for ws in result.inferred_workstreams}
    # H2 headers should yield workstream candidates
    assert len(ws_ids) >= 2


def test_bootstrap_from_newsletter_extracts_risk_candidates() -> None:
    result = bootstrap_from_newsletter("acme", _SAMPLE_NEWSLETTER)
    assert len(result.risk_candidates) >= 1
    # The word "risk" appears in the Acme section
    risk_texts = [r.description for r in result.risk_candidates]
    assert any("risk" in t.lower() for t in risk_texts)


def test_bootstrap_from_newsletter_extracts_decision_candidates() -> None:
    result = bootstrap_from_newsletter("acme", _SAMPLE_NEWSLETTER)
    assert len(result.decision_candidates) >= 1
    decision_texts = [d.text for d in result.decision_candidates]
    assert any("decided" in t.lower() for t in decision_texts)


def test_bootstrap_from_newsletter_all_candidates_are_cold_start() -> None:
    result = bootstrap_from_newsletter("acme", _SAMPLE_NEWSLETTER)
    for rc in result.risk_candidates:
        assert rc.source == "cold-start"
    for dc in result.decision_candidates:
        assert dc.source == "cold-start"


def test_bootstrap_from_newsletter_includes_notes() -> None:
    result = bootstrap_from_newsletter("acme", _SAMPLE_NEWSLETTER)
    assert len(result.notes) >= 1
    combined = " ".join(result.notes)
    assert "cold-start" in combined.lower() or "candidate" in combined.lower()


def test_bootstrap_from_newsletter_seeded_at_is_utc() -> None:
    import datetime
    result = bootstrap_from_newsletter("acme", _SAMPLE_NEWSLETTER)
    assert result.seeded_at.tzinfo == datetime.timezone.utc


def test_bootstrap_from_newsletter_respects_max_risk_candidates() -> None:
    result = bootstrap_from_newsletter("acme", _SAMPLE_NEWSLETTER, max_risk_candidates=1)
    assert len(result.risk_candidates) <= 1


def test_bootstrap_from_newsletter_respects_max_decision_candidates() -> None:
    result = bootstrap_from_newsletter("acme", _SAMPLE_NEWSLETTER, max_decision_candidates=1)
    assert len(result.decision_candidates) <= 1


def test_bootstrap_from_newsletter_empty_text_returns_empty_result() -> None:
    result = bootstrap_from_newsletter("acme", "")
    assert result.risk_candidates == ()
    assert result.decision_candidates == ()


def test_bootstrap_from_newsletter_strips_html_tags() -> None:
    html = "<h2>Acme</h2><p>This is at <b>risk</b> due to delays.</p>"
    result = bootstrap_from_newsletter("acme", html)
    # Should not crash; may or may not extract risk candidates depending on text
    assert isinstance(result, ColdStartSeedResult)


def test_bootstrap_from_newsletter_workstream_hint_matches_section() -> None:
    result = bootstrap_from_newsletter("acme", _SAMPLE_NEWSLETTER)
    # Risk candidates from Acme section should carry a workstream hint
    nova_risks = [r for r in result.risk_candidates if r.workstream_hint and "acme" in r.workstream_hint.lower()]
    assert len(nova_risks) >= 1


# ---------------------------------------------------------------------------
# infer_workstreams_from_program_yaml
# ---------------------------------------------------------------------------

def test_infer_workstreams_returns_empty_when_file_absent(tmp_path: Path) -> None:
    result = infer_workstreams_from_program_yaml("acme", programs_root=tmp_path / "programs")
    assert result == ()


def test_infer_workstreams_reads_yaml_workstream_list(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_file = programs_root / "acme" / "program.yaml"
    program_file.parent.mkdir(parents=True, exist_ok=True)
    program_file.write_text(
        textwrap.dedent("""\
            id: acme
            workstreams:
              - id: nova_readiness
                name: Acme Readiness
              - id: dd_on_pf
                name: DD on PF
        """),
        encoding="utf-8",
    )
    result = infer_workstreams_from_program_yaml("acme", programs_root=programs_root)
    assert len(result) == 2
    ids = {ws.workstream_id for ws in result}
    assert "nova_readiness" in ids
    assert "dd_on_pf" in ids


def test_infer_workstreams_inferred_from_is_program_yaml(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_file = programs_root / "acme" / "program.yaml"
    program_file.parent.mkdir(parents=True, exist_ok=True)
    program_file.write_text(
        textwrap.dedent("""\
            workstreams:
              - id: ws1
                name: WS One
        """),
        encoding="utf-8",
    )
    result = infer_workstreams_from_program_yaml("acme", programs_root=programs_root)
    assert result[0].inferred_from == "program_yaml"


def test_infer_workstreams_skips_entries_without_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_file = programs_root / "acme" / "program.yaml"
    program_file.parent.mkdir(parents=True, exist_ok=True)
    program_file.write_text(
        textwrap.dedent("""\
            workstreams:
              - name: No ID here
        """),
        encoding="utf-8",
    )
    result = infer_workstreams_from_program_yaml("acme", programs_root=programs_root)
    assert result == ()


# ---------------------------------------------------------------------------
# compute_signal_density_thresholds
# ---------------------------------------------------------------------------

def test_compute_signal_density_thresholds_returns_empty_for_no_input() -> None:
    assert compute_signal_density_thresholds({}) == {}


def test_compute_signal_density_thresholds_floors_at_global_min() -> None:
    result = compute_signal_density_thresholds({"ws1": 0}, global_min=0.5)
    assert result["ws1"] >= 0.5


def test_compute_signal_density_thresholds_all_keys_present() -> None:
    counts = {"ws1": 10, "ws2": 20, "ws3": 5}
    result = compute_signal_density_thresholds(counts)
    assert set(result.keys()) == set(counts.keys())


def test_compute_signal_density_thresholds_values_are_positive() -> None:
    counts = {"ws1": 10, "ws2": 20}
    result = compute_signal_density_thresholds(counts)
    for v in result.values():
        assert v > 0

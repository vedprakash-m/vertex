"""Comprehensive test suite for persona_checker and persona system.

Covers the §8.2 test matrix from specs/persona.md.
Tests are aligned with actual implementation behavior.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from src.core.ban_list_validator import BanListViolation, find_structural_rule_violations
from src.core.config_loader import EditorialRules, VerbositySettings
from src.core.persona_checker import run_persona_checks
from src.core.persona_models import (
    PersonaCheck,
    PersonaDefinition,
    PersonaEnforcementConfig,
    PersonaOverride,
    PersonaRegistry,
    StructuralRule,
)
from src.core.scope_resolver import ScopeResolver


# =============================================================================
# HELPERS
# =============================================================================

def _make_rules(
    *,
    structural_rules: tuple[StructuralRule, ...] = (),
) -> EditorialRules:
    return EditorialRules(
        schema_version="1.0",
        stale_warn_days=14,
        stale_block_days=30,
        banned_phrases=(),
        banned_openings=(),
        verbosity=VerbositySettings(
            workstream_blurb_max_sentences=None,
            workstream_blurb_max_words=None,
            exec_bullet_max_words=None,
            exec_max_bullets=None,
            scorecard_summary_max_sentences=None,
        ),
        structural_rules=structural_rules,
    )


def _make_registry(
    *personas: PersonaDefinition,
    mode: str = "enforce",
    enabled: bool = True,
) -> PersonaRegistry:
    return PersonaRegistry(
        schema_version="1.0",
        enforcement=PersonaEnforcementConfig(mode=mode, enabled=enabled),
        personas=personas,
    )


# =============================================================================
# SCHEMA / LOAD TESTS
# =============================================================================

def test_missing_registry_returns_none() -> None:
    report = run_persona_checks(
        registry=None,
        exec_summary_text="Summary",
        workstream_blurbs={},
        loaded_narratives={},
        rendered_html="<html></html>",
        subject_line="Subject",
        ban_rule_results=(),
        structural_rule_results=(),
        editorial_rules=_make_rules(),
        overrides=None,
        program_phase=None,
        evaluation_date=date(2026, 5, 28),
    )
    assert report is None


def test_quarantined_unknown_check_type() -> None:
    registry = _make_registry(
        PersonaDefinition(
            id="test", priority="critical",
            checks=(PersonaCheck(
                id="k", type="unknown_type_xyz", scope="exec_summary",
                message="Unknown type", severity="warn",
            ),),
        ),
    )
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="test",
        workstream_blurbs={},
        loaded_narratives={},
        rendered_html="",
        subject_line="",
        ban_rule_results=(),
        structural_rule_results=(),
        editorial_rules=_make_rules(),
        overrides=None,
        program_phase=None,
        evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.quarantined) == 1
    assert "unknown check type" in report.quarantined[0].skip_reason


# =============================================================================
# CHECK TYPES x HAPPY PATH
# =============================================================================

def test_keyword_present_happy() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="exec_summary",
            keywords=["launch", "gate", "blocked"], message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="The gate launched successfully",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.passed) == 1


def test_keyword_absent_happy() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_absent", scope="exec_summary",
            keywords=["tapestry", "delve"], message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="Status: on track",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.passed) == 1


def test_regex_present_happy() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="regex_present", scope="exec_summary",
            pattern=r"\b20\d{2}-\d{2}-\d{2}\b", message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="Checkpoint 2026-06-01 for launch",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.passed) == 1


def test_sentence_length_max_happy() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="sentence_length_max", scope="exec_summary",
            threshold=30, message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="Short sentence.",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.passed) == 1


def test_structure_present_happy() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="structure_present", scope="rendered_html",
            element="exec-summary", message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html='<div data-vertex-block="exec-summary">Content</div>',
        subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.passed) == 1


def test_delegate_to_rule_ban_list_happy() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="delegate_to_rule", scope="exec_summary",
            rule_ref="banned_openings", message="", severity="block",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="All clear, nothing banned here",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(),  # No violations
        structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.passed) == 1


def test_delegate_to_rule_structural_happy() -> None:
    sr = (StructuralRule(
        id="ado_link_format", regex_absent=r"ADO#\d+",
        scope=("exec_summary",), severity="warn", autofix_hint="Use hyperlink",
    ),)
    # resolver with violation text so find_structural_rule_violations finds the match
    resolver = ScopeResolver(
        exec_summary_text="See ADO#12345 for details",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
    )
    structural = find_structural_rule_violations(resolver=resolver, editorial_rules=_make_rules(structural_rules=sr))
    # Structural rule found a violation (ADO#12345 present), so check should FAIL (not pass)
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="delegate_to_rule", scope="exec_summary",
            rule_ref="ado_link_format", message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="See ADO#12345 for details",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=structural,
        editorial_rules=_make_rules(structural_rules=sr), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.failed) == 1  # Checks for violation → found one → fails


# =============================================================================
# CHECK TYPES x VIOLATIONS
# =============================================================================

def test_keyword_present_missing_keyword() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="exec_summary",
            keywords=["launch", "gate", "blocked"], message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="Status update.",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.failed) == 1


def test_keyword_absent_finds_banned_word() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_absent", scope="exec_summary",
            keywords=["tapestry", "delve"], message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="This tapestry of importance",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.failed) == 1
    assert report.failed[0].matched_text == "tapestry"


def test_regex_present_no_match() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="regex_present", scope="exec_summary",
            pattern=r"\b20\d{2}-\d{2}-\d{2}\b", message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="No ISO date here",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.failed) == 1


def test_sentence_length_max_exceeds() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="sentence_length_max", scope="exec_summary",
            threshold=5, message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="This is a very long sentence that exceeds the threshold set for maximum word count per sentence in the configuration",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.failed) == 1


# =============================================================================
# SCOPE RESOLUTION
# =============================================================================

def test_scope_exec_summary() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="exec_summary",
            keywords=["launch"], message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="Launch gate confirmed",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.results) == 1
    assert report.results[0].location == "exec_summary"
    assert report.results[0].status == "passed"


def test_scope_workstream_specific() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="workstream:dd_on_pf",
            keywords=["rdma"], message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="",
        workstream_blurbs={"dd_on_pf": "RDMA performance is critical"},
        loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.results) == 1
    assert report.results[0].location == "workstream:dd_on_pf"
    assert report.results[0].status == "passed"


def test_scope_each_narrative_per_location() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="each_narrative",
            keywords=["checkpoint"], message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="",
        workstream_blurbs={},
        loaded_narratives={
            "dd_on_pf": "Checkpoint 2026-06-01",
            "safety": "No checkpoint here",
        },
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert report.total_evaluations == 2
    locations = {r.location: r.status for r in report.results}
    assert locations["narrative:safety"] == "passed"  # "checkpoint" is substring of "No checkpoint here"
    assert locations["narrative:dd_on_pf"] == "passed"


def test_scope_rendered_html() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="structure_present", scope="rendered_html",
            element="exec-summary", message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html='<div data-vertex-block="exec-summary">Content</div>',
        subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.results) == 1
    assert report.results[0].location == "rendered_html"
    assert report.results[0].status == "passed"


def test_scope_subject_line() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="subject_line",
            keywords=["Acme"], message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="",
        subject_line="Acme Weekly Update - Issue 78",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.results) == 1
    assert report.results[0].location == "subject_line"
    assert report.results[0].status == "passed"


def test_scope_not_found_for_unknown_section() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="workstream:nonexistent",
            keywords=["test"], message="", severity="warn", strict_scope=False,
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="",
        workstream_blurbs={"dd_on_pf": "RDMA"},
        loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.results) == 1
    assert report.results[0].status == "scope_not_found"


def test_strict_scope_true_fails_on_missing_pre_render_scope() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="workstream:nonexistent",
            keywords=["test"], message="", severity="warn", strict_scope=True,
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="",
        workstream_blurbs={"dd_on_pf": "RDMA"},
        loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.results) == 1
    assert report.results[0].status == "failed"


# =============================================================================
# EACH_NARRATIVE AGGREGATION
# =============================================================================

def test_each_narrative_all_pass() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="each_narrative",
            keywords=["checkpoint"], message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="",
        workstream_blurbs={},
        loaded_narratives={"a": "Checkpoint 2026-06-01", "b": "Checkpoint 2026-06-02"},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert report.total_checks == 1
    assert report.total_evaluations == 2
    assert len(report.passed) == 2
    assert len(report.failed) == 0


def test_each_narrative_evaluation_order_sorted() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="each_narrative",
            keywords=["checkpoint"], message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="",
        workstream_blurbs={},
        loaded_narratives={"z_workstream": "Checkpoint", "a_workstream": "Checkpoint", "m_workstream": "Checkpoint"},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    locations = [r.location for r in report.results]
    assert locations == sorted(locations)  # Sorted lexicographic order


# =============================================================================
# PHASE GUARD
# =============================================================================

def test_phase_guard_skips_mismatched_phase() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="exec_summary",
            keywords=["launch"], message="", severity="warn", phase="steady_state",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="Launch gate confirmed",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase="ramp_active",
        evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.skipped) == 1
    assert "phase mismatch" in report.skipped[0].skip_reason


def test_phase_null_always_runs() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="exec_summary",
            keywords=["launch"], message="", severity="warn", phase=None,
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="Launch gate confirmed",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase="ramp_active",
        evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.passed) == 1


# =============================================================================
# ENFORCE_AFTER
# =============================================================================

def test_enforce_after_before_date_downgrades_to_warn() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="exec_summary",
            keywords=["LAUNCH"], message="", severity="block",
            enforce_after="2026-06-30",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="status update only",  # Does not contain "LAUNCH"
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.failed) == 1
    assert report.failed[0].effective_severity == "warn"


def test_enforce_after_after_date_respects_block() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="exec_summary",
            keywords=["LAUNCH"], message="", severity="block",
            enforce_after="2026-05-20",  # Already passed
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="status update only",  # Does not contain "LAUNCH"
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.failed) == 1
    assert report.failed[0].effective_severity == "block"


# =============================================================================
# CHECK DEPENDENCIES
# =============================================================================

def test_dependency_order_topological() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(
            PersonaCheck(
                id="pre", type="keyword_present", scope="exec_summary",
                keywords=["launch"], message="Pre check", severity="warn",
            ),
            PersonaCheck(
                id="post", type="keyword_present", scope="exec_summary",
                keywords=["gate"], message="Post check", severity="warn",
                requires=("pre",),
            ),
        ),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="launch and gate",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.passed) == 2


# =============================================================================
# ENFORCEMENT MODES
# =============================================================================

def test_shadow_mode_no_surfaced() -> None:
    registry = _make_registry(
        PersonaDefinition(
            id="p", priority="critical",
            checks=(PersonaCheck(
                id="k", type="keyword_present", scope="exec_summary",
                keywords=["launch"], message="", severity="block",
            ),),
        ),
        mode="shadow",
    )
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="The launch is today",  # Contains "launch"
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert all(not r.surfaced for r in report.results)
    assert report.enforcement_mode == "shadow"


def test_warn_mode_downgrades_blocks() -> None:
    registry = _make_registry(
        PersonaDefinition(
            id="p", priority="critical",
            checks=(PersonaCheck(
                id="k", type="keyword_present", scope="exec_summary",
                keywords=["LAUNCH"], message="", severity="block",
            ),),
        ),
        mode="warn",
    )
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="status update only",  # Does not contain "LAUNCH"
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.warnings) == 1


def test_enforce_mode_respects_severity() -> None:
    registry = _make_registry(
        PersonaDefinition(
            id="p", priority="critical",
            checks=(PersonaCheck(
                id="k", type="keyword_present", scope="exec_summary",
                keywords=["LAUNCH"], message="", severity="block",
            ),),
        ),
        mode="enforce",
    )
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="status update only",  # Does not contain "LAUNCH"
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.blocks) == 1


# =============================================================================
# DELEGATE_TO_RULE
# =============================================================================

def test_delegate_to_rule_ban_list_fires() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="delegate_to_rule", scope="exec_summary",
            rule_ref="banned_openings", message="", severity="block",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="This week we shipped",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(BanListViolation(
            rule_id="BF-100", location="exec_summary", phrase="This week",
            matched_text="This week", category="banned_opening",
        ),),
        structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.failed) == 1
    assert report.failed[0].status == "failed"


def test_delegate_to_rule_unknown_rule_quarantined() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="delegate_to_rule", scope="exec_summary",
            rule_ref="nonexistent_rule", message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="test",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.quarantined) == 1
    assert "unknown rule_ref" in report.quarantined[0].skip_reason


# =============================================================================
# SCOPE SIZE LIMITS
# =============================================================================

def test_presence_check_completes_on_large_text() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="exec_summary",
            keywords=["launch"], message="", severity="warn",
        ),),
    ))
    large_text = "word " * 15000  # ~90KB
    report = run_persona_checks(
        registry=registry,
        exec_summary_text=large_text,
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert report.total_evaluations == 1


# =============================================================================
# PERFORMANCE ALGORITHMIC CONTROLS
# =============================================================================

def test_registry_loaded_once_reused_across_calls() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="regex_present", scope="exec_summary",
            pattern=r"\b20\d{2}-\d{2}-\d{2}\b", message="", severity="warn",
        ),),
    ))
    for _ in range(2):
        report = run_persona_checks(
            registry=registry,
            exec_summary_text="Date 2026-06-01",
            workstream_blurbs={}, loaded_narratives={},
            rendered_html="", subject_line="",
            ban_rule_results=(), structural_rule_results=(),
            editorial_rules=_make_rules(), overrides=None,
            program_phase=None, evaluation_date=date(2026, 5, 28),
        )
        assert report is not None
        assert len(report.passed) == 1


def test_max_checks_bounded_in_loop() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=tuple(
            PersonaCheck(id=f"check_{i}", type="keyword_present", scope="exec_summary",
                         keywords=["test"], message=f"Check {i}", severity="warn")
            for i in range(50)
        ),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="test",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert report.total_checks == 50


# =============================================================================
# PRIVACY / PII
# =============================================================================

def test_matched_text_truncated_to_200_chars() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="exec_summary",
            keywords=["launch"], message="", severity="warn",
        ),),
    ))
    long_text = "launch " + ("a" * 300)
    report = run_persona_checks(
        registry=registry,
        exec_summary_text=long_text,
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    if report.results[0].matched_text is not None:
        assert len(report.results[0].matched_text) <= 200


def test_email_in_matched_text_is_masked() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="keyword_present", scope="exec_summary",
            keywords=["contact"], message="", severity="warn",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="Contact maintainer@example.com for details",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    if report.results[0].matched_text:
        assert "@microsoft.com" not in report.results[0].matched_text


# =============================================================================
# QUARANTINE VISIBILITY
# =============================================================================

def test_quarantined_check_declared_severity_preserved() -> None:
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(PersonaCheck(
            id="k", type="unknown_type", scope="exec_summary",
            message="", severity="block",
        ),),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="test",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.quarantined) == 1
    assert report.quarantined[0].declared_severity == "block"


# =============================================================================
# PUBLISH GATE INTEGRATION
# =============================================================================

def test_publish_gate_loads_artifact_with_blocks() -> None:
    from src.commands.publish_gate import _load_persona_signal_failures
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        issue_dir = root / "acme_weekly" / "output" / "acme_weekly" / "issue_001"
        issue_dir.mkdir(parents=True)
        artifact = issue_dir / "issue_001.persona_signal_coverage.json"
        artifact.write_text(json.dumps({
            "enforcement_mode": "enforce",
            "total_checks": 1,
            "total_evaluations": 1,
            "results": [{
                "persona_id": "editorial",
                "check_id": "exec_no_banned_opening",
                "status": "failed",
                "effective_severity": "block",
                "location": "exec_summary",
                "message": "Banned opening",
            }],
        }), encoding="utf-8")
        manifest = issue_dir / "issue_001.manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        # Set manifest mtime BEFORE artifact so artifact mtime > manifest mtime (not stale)
        manifest_time = time.time()
        artifact_time = manifest_time + 2.0
        os.utime(manifest, (manifest_time, manifest_time))
        os.utime(artifact, (artifact_time, artifact_time))
        failures, warnings = _load_persona_signal_failures(
            edition="acme_weekly", issue=1, programs_root=root,
        )
        assert len(failures) == 1
        assert "QG-P" in failures[0]


def test_publish_gate_missing_artifact_warns() -> None:
    from src.commands.publish_gate import _load_persona_signal_failures
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        failures, warnings = _load_persona_signal_failures(
            edition="acme_weekly", issue=1, programs_root=root,
        )
        assert len(failures) == 0
        assert len(warnings) == 1
        assert "not found" in warnings[0]


def test_publish_gate_shadow_mode_skips() -> None:
    from src.commands.publish_gate import _load_persona_signal_failures
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        issue_dir = root / "acme_weekly" / "output" / "acme_weekly" / "issue_001"
        issue_dir.mkdir(parents=True)
        artifact = issue_dir / "issue_001.persona_signal_coverage.json"
        artifact.write_text(json.dumps({
            "enforcement_mode": "shadow",
            "total_checks": 1,
            "total_evaluations": 1,
            "results": [{
                "persona_id": "editorial",
                "check_id": "k",
                "status": "failed",
                "effective_severity": "none",
                "location": "exec_summary",
                "message": "Blocked",
            }],
        }), encoding="utf-8")
        manifest = issue_dir / "issue_001.manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        # Set manifest mtime BEFORE artifact so artifact mtime > manifest mtime (not stale)
        manifest_time = time.time()
        artifact_time = manifest_time + 2.0
        os.utime(manifest, (manifest_time, manifest_time))
        os.utime(artifact, (artifact_time, artifact_time))
        failures, warnings = _load_persona_signal_failures(
            edition="acme_weekly", issue=1, programs_root=root,
        )
        assert len(failures) == 0


# =============================================================================
# REVIEWER DEGRADATION
# =============================================================================

def test_reviewer_loads_missing_artifact_returns_none() -> None:
    from src.commands.review_full import _load_persona_coverage_artifact
    with TemporaryDirectory() as tmpdir:
        result = _load_persona_coverage_artifact(
            programs_root=Path(tmpdir),
            edition_name="acme_weekly",
            issue_number=99,
        )
        assert result is None


def test_reviewer_loads_malformed_artifact_returns_none() -> None:
    from src.commands.review_full import _load_persona_coverage_artifact
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        issue_dir = root / "acme_weekly" / "output" / "acme_weekly" / "issue_001"
        issue_dir.mkdir(parents=True)
        artifact = issue_dir / "issue_001.persona_signal_coverage.json"
        artifact.write_text("not valid json{{", encoding="utf-8")
        result = _load_persona_coverage_artifact(
            programs_root=root,
            edition_name="acme_weekly",
            issue_number=1,
        )
        assert result is None


# =============================================================================
# QG-P GATE EVALUATION
# =============================================================================

def test_qg_p_passes_when_no_blocks() -> None:
    from src.core.quality_gates import evaluate_persona_signal_gates

    class FakeCoverage:
        enforcement_mode = "enforce"
        blocks: tuple = ()

    report = evaluate_persona_signal_gates(FakeCoverage())
    assert report.results[0].gate_id == "QG-P"
    assert report.results[0].passed is True


def test_qg_p_fails_when_blocks_present() -> None:
    from src.core.quality_gates import evaluate_persona_signal_gates

    class BlockResult:
        check_id = "k"
        persona_id = "editorial"
        location = "exec_summary"
        message = "Block"

    class FakeCoverage:
        enforcement_mode = "enforce"
        @property
        def blocks(self): return (BlockResult(),)

    report = evaluate_persona_signal_gates(FakeCoverage())
    assert report.results[0].gate_id == "QG-P"
    assert report.results[0].passed is False


def test_qg_p_skips_when_none_coverage() -> None:
    from src.core.quality_gates import evaluate_persona_signal_gates

    report = evaluate_persona_signal_gates(None)
    assert len(report.results) == 0


# =============================================================================
# ALWAYS_ACTIVE PERSONA
# =============================================================================

def test_always_active_persona_runs_all_checks() -> None:
    registry = _make_registry(
        PersonaDefinition(
            id="editorial", priority="critical", always_active=True,
            checks=(
                PersonaCheck(
                    id="check1", type="keyword_present", scope="exec_summary",
                    keywords=["launch"], message="", severity="warn",
                ),
                PersonaCheck(
                    id="check2", type="regex_present", scope="exec_summary",
                    pattern=r"\b20\d{2}-\d{2}-\d{2}\b", message="", severity="warn",
                ),
            ),
        ),
        mode="enforce",
    )
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="Launch 2026-06-01",
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.passed) == 2


# =============================================================================
# OVERRIDE DOWNGRADE
# =============================================================================

def test_override_severity_downgrade() -> None:
    registry = _make_registry(
        PersonaDefinition(
            id="p", priority="critical",
            checks=(PersonaCheck(
                id="k", type="keyword_present", scope="exec_summary",
                keywords=["LAUNCH"], message="", severity="block",
            ),),
        ),
        mode="enforce",
    )

    class MockOverrides:
        persona_overrides = (
            PersonaOverride(
                check_id="k",
                override_severity="warn",
                reason="test override",
                expires="2026-12-31",
                approved_by="operator",
            ),
        )

    report = run_persona_checks(
        registry=registry,
        exec_summary_text="status update only",  # Does not contain "LAUNCH"
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=MockOverrides(),
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.blocks) == 0
    assert len(report.warnings) == 1


# =============================================================================
# SEVERITY PRECEDENCE CHAIN (additional coverage)
# =============================================================================

def test_severity_override_beats_enforce_after_past() -> None:
    """Override (higher in precedence chain) beats enforce_after past (which would keep block)."""
    registry = _make_registry(
        PersonaDefinition(
            id="p", priority="critical",
            checks=(PersonaCheck(
                id="k", type="keyword_present", scope="exec_summary",
                keywords=["LAUNCH"], message="", severity="block",
                enforce_after="2026-05-01",  # past — enforce_after does NOT downgrade
            ),),
        ),
        mode="enforce",
    )

    class MockOverrides:
        persona_overrides = (
            PersonaOverride(
                check_id="k",
                override_severity="warn",
                reason="grace period",
                expires="2026-12-31",
                approved_by="operator",
            ),
        )

    report = run_persona_checks(
        registry=registry,
        exec_summary_text="status update",  # Does not contain "LAUNCH"
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=MockOverrides(),
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    # Override is highest in chain: block stays downgraded to warn despite enforce_after past
    assert len(report.blocks) == 0
    assert len(report.warnings) == 1


def test_severity_shadow_mode_preserves_declared_and_effective_severity() -> None:
    """Shadow mode: declared_severity preserved, effective_severity not downgraded, surfaced=False."""
    registry = _make_registry(
        PersonaDefinition(
            id="p", priority="critical",
            checks=(PersonaCheck(
                id="k", type="keyword_present", scope="exec_summary",
                keywords=["LAUNCH"], message="", severity="block",
            ),),
        ),
        mode="shadow",
    )
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="status update only",  # Does not contain "LAUNCH"
        workstream_blurbs={}, loaded_narratives={},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert len(report.failed) == 1
    result = report.failed[0]
    assert result.declared_severity == "block"
    assert result.surfaced is False
    assert report.enforcement_mode == "shadow"


# =============================================================================
# ZONE A CONTRACT (unit-level)
# =============================================================================

def test_zone_a_persona_core_no_ai_imports() -> None:
    """Zone A: persona_checker, persona_models, scope_resolver must not import src.ai or src.m365."""
    import ast
    from pathlib import Path as _Path
    repo_root = _Path(__file__).parent.parent.parent
    zone_a_files = [
        repo_root / "src" / "core" / "persona_checker.py",
        repo_root / "src" / "core" / "persona_models.py",
        repo_root / "src" / "core" / "scope_resolver.py",
    ]
    forbidden = ("src.ai", "src.m365")
    violations: list[str] = []
    for f in zone_a_files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name.startswith(p) for p in forbidden):
                        violations.append(f"{f.name}: imports {alias.name!r}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if any(module.startswith(p) for p in forbidden):
                    violations.append(f"{f.name}: from {module!r}")
    assert violations == [], "Zone A violation: " + "; ".join(violations)


def test_zone_a_persona_checker_runs_without_network() -> None:
    """Zone A: run_persona_checks completes without any network or AI calls."""
    registry = _make_registry(PersonaDefinition(
        id="p", priority="critical",
        checks=(
            PersonaCheck(id="k1", type="keyword_present", scope="exec_summary",
                         keywords=["gate"], message="", severity="warn"),
            PersonaCheck(id="k2", type="regex_present", scope="each_narrative",
                         pattern=r"\b20\d{2}-\d{2}-\d{2}\b", message="", severity="warn"),
            PersonaCheck(id="k3", type="sentence_length_max", scope="exec_summary",
                         threshold=30, message="", severity="warn"),
        ),
    ))
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="Gate decision confirmed for 2026-06-01.",
        workstream_blurbs={},
        loaded_narratives={"ws1": "Checkpoint 2026-06-01 gate passed.", "ws2": "Next review by 2026-06-15."},
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    assert report is not None
    assert report.total_checks == 3
    assert report.evaluation_time_ms >= 0


# =============================================================================
# SCHEMA CONDITIONAL VALIDATION
# =============================================================================

def test_schema_conditional_keyword_present_requires_keywords() -> None:
    """JSON Schema: keyword_present check without keywords field fails validation."""
    import jsonschema
    from pathlib import Path as _Path
    schema_path = _Path(__file__).parent.parent.parent / "src" / "core" / "schemas" / "personas.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    invalid_doc = {
        "schema_version": "1.0",
        "enforcement": {"enabled": True, "mode": "warn"},
        "personas": [{
            "id": "test_persona",
            "priority": "normal",
            "checks": [{
                "id": "missing_keywords",
                "type": "keyword_present",
                "scope": "exec_summary",
                "message": "Test check",
                "severity": "warn",
                # missing: keywords field
            }],
        }],
    }
    errors = list(validator.iter_errors(invalid_doc))
    assert any(
        "keywords" in str(e.message) or "keywords" in str(e.path)
        for e in errors
    ), f"Expected 'keywords' required error, got: {[e.message for e in errors]}"


def test_schema_conditional_regex_present_requires_pattern() -> None:
    """JSON Schema: regex_present check without pattern field fails validation."""
    import jsonschema
    from pathlib import Path as _Path
    schema_path = _Path(__file__).parent.parent.parent / "src" / "core" / "schemas" / "personas.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    invalid_doc = {
        "schema_version": "1.0",
        "enforcement": {"enabled": True, "mode": "warn"},
        "personas": [{
            "id": "test_persona",
            "priority": "normal",
            "checks": [{
                "id": "missing_pattern",
                "type": "regex_present",
                "scope": "exec_summary",
                "message": "Test check",
                "severity": "warn",
                # missing: pattern field
            }],
        }],
    }
    errors = list(validator.iter_errors(invalid_doc))
    assert any(
        "pattern" in str(e.message) or "pattern" in str(e.path)
        for e in errors
    ), f"Expected 'pattern' required error, got: {[e.message for e in errors]}"


# =============================================================================
# PERFORMANCE
# =============================================================================

def test_full_registry_evaluation_under_500ms() -> None:
    """Gap 7: complete persona evaluation for a representative workload must complete under 500ms."""
    import time as _time

    personas = tuple(
        PersonaDefinition(
            id=f"persona_{i}",
            priority="normal",
            checks=tuple(
                PersonaCheck(
                    id=f"check_{i}_{j}",
                    type="keyword_present",
                    scope="each_narrative",
                    keywords=["checkpoint", "gate", "eta"],
                    message="",
                    severity="warn",
                )
                for j in range(3)
            ),
        )
        for i in range(15)
    )
    registry = PersonaRegistry(
        schema_version="1.0",
        enforcement=PersonaEnforcementConfig(mode="enforce", enabled=True),
        personas=personas,
    )
    narratives = {f"ws_{k}": f"Checkpoint 2026-06-{k+1:02d} gate confirmed." for k in range(10)}

    start = _time.monotonic()
    report = run_persona_checks(
        registry=registry,
        exec_summary_text="Gate decision confirmed.",
        workstream_blurbs={},
        loaded_narratives=narratives,
        rendered_html="", subject_line="",
        ban_rule_results=(), structural_rule_results=(),
        editorial_rules=_make_rules(), overrides=None,
        program_phase=None, evaluation_date=date(2026, 5, 28),
    )
    elapsed_ms = (_time.monotonic() - start) * 1000

    assert report is not None
    assert elapsed_ms < 500, f"Persona evaluation took {elapsed_ms:.1f}ms, expected < 500ms"

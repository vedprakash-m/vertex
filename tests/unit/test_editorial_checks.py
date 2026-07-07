from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.core.config_loader import EditorialRules, VerbositySettings
from src.core.persona_checker import run_persona_checks
from src.core.persona_models import (
    PersonaCheck,
    PersonaDefinition,
    PersonaEnforcementConfig,
    PersonaRegistry,
)
from src.core.scope_resolver import ScopeResolver


def _rules() -> EditorialRules:
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
        structural_rules=(),
    )


def _registry(*checks: PersonaCheck) -> PersonaRegistry:
    return PersonaRegistry(
        schema_version="1.0",
        enforcement=PersonaEnforcementConfig(mode="enforce", enabled=True),
        personas=(
            PersonaDefinition(
                id="editorial_guard",
                priority="critical",
                always_active=True,
                checks=checks,
            ),
        ),
    )


@dataclass(frozen=True)
class _Dimension:
    name: str
    risk: str | None = None
    eta: str | None = None
    hide_from_scorecard: bool = False


@dataclass(frozen=True)
class _Scorecard:
    name: str
    dimensions: tuple[_Dimension, ...]


@dataclass(frozen=True)
class _Overrides:
    scorecards: tuple[_Scorecard, ...]
    removed_dimensions: tuple = ()
    removed_sections: tuple[str, ...] = ()
    top_3_now: tuple = ()
    governance: object | None = None
    persona_overrides: tuple = ()


def test_scope_resolver_exec_summary_bullets_and_overrides_scorecard() -> None:
    resolver = ScopeResolver(
        exec_summary_text="Lead\n- First bullet\n- Second bullet\nParagraph",
        workstream_blurbs={},
        loaded_narratives={},
        rendered_html="",
        subject_line="",
        overrides_scorecard_text="ws\x1fPlatform\x1frisk=high\nschema_version=1",
        published_baseline={"exec_summary": "- __Platform__ (High) Launch risk"},
    )

    bullets = resolver.resolve("exec_summary_bullets")[0]
    overrides = resolver.resolve("overrides_scorecard")[0]
    baseline = resolver.resolve("published_baseline:exec_summary")[0]

    assert bullets.found is True
    assert bullets.text == "- First bullet\n- Second bullet"
    assert overrides.text.startswith("ws\x1fPlatform\x1frisk=high")
    assert baseline.text == "- __Platform__ (High) Launch risk"


def test_format_matches_and_published_baseline_match_pass() -> None:
    registry = _registry(
        PersonaCheck(
            id="exec_has_bold_label_bullets",
            type="format_matches",
            scope="exec_summary",
            pattern=r"^- __[^_\n]+__",
            regex_flags="MULTILINE",
            min=2,
            message="Exec summary must use bold label bullets.",
            severity="block",
        ),
        PersonaCheck(
            id="exec_matches_baseline_shape",
            type="published_baseline_match",
            scope="exec_summary",
            baseline_scope="exec_summary",
            check_attributes=("bullet_lines_count", "bold_label_count"),
            tolerance=(("bullet_lines_count", "-1"),),
            message="Exec summary regressed from the published baseline format.",
            severity="warn",
        ),
    )

    report = run_persona_checks(
        registry=registry,
        exec_summary_text="- __Platform__ (High) Launch risk\n- __Security__ (Medium) Follow-up ongoing",
        workstream_blurbs={},
        loaded_narratives={},
        rendered_html="",
        subject_line="",
        ban_rule_results=(),
        structural_rule_results=(),
        editorial_rules=_rules(),
        overrides=None,
        program_phase=None,
        evaluation_date=date(2026, 6, 27),
        published_baseline={
            "exec_summary": "- __Platform__ (High) Launch risk\n- __Security__ (Medium) Follow-up ongoing"
        },
    )

    assert report is not None
    assert len(report.failed) == 0
    assert len(report.passed) == 2


def test_cross_scope_consistency_and_count_range_fail_on_mismatch() -> None:
    overrides = _Overrides(
        scorecards=(
            _Scorecard(
                name="Weekly",
                dimensions=(
                    _Dimension(name="Platform", risk="high"),
                    _Dimension(name="Security", risk="high"),
                ),
            ),
        ),
    )
    registry = _registry(
        PersonaCheck(
            id="high_risk_in_exec_summary",
            type="cross_scope_consistency",
            scope="exec_summary",
            source_scope="overrides_scorecard",
            source_extract="risk_levels_high",
            target_scope="exec_summary",
            require_all_in_target=True,
            message="All High-risk workstreams must be named in exec summary.",
            severity="block",
        ),
        PersonaCheck(
            id="p0_counts_match",
            type="count_range",
            scope="exec_summary",
            pattern=r"P0:\s*(\d+)/(\d+)",
            extract_numerator_from="p0_open_count",
            extract_denominator_from="p0_total_count",
            message="P0 counts must match source data.",
            severity="warn",
        ),
    )

    report = run_persona_checks(
        registry=registry,
        exec_summary_text="- __Platform__ (High) Launch risk\nP0: 2/3",
        workstream_blurbs={},
        loaded_narratives={},
        rendered_html="",
        subject_line="",
        ban_rule_results=(),
        structural_rule_results=(),
        editorial_rules=_rules(),
        overrides=_Overrides(
            scorecards=overrides.scorecards,
            top_3_now=(type("Top", (), {"type": "p0", "by_date": None})(),),
        ),
        program_phase=None,
        evaluation_date=date(2026, 6, 27),
    )

    assert report is not None
    assert {result.check_id for result in report.failed} == {
        "high_risk_in_exec_summary",
        "p0_counts_match",
    }


def test_scorecard_alignment_and_terminology_consistency_fail() -> None:
    overrides = _Overrides(
        scorecards=(
            _Scorecard(
                name="Weekly",
                dimensions=(_Dimension(name="ws_platform", risk="high", eta="2026-07-15"),),
            ),
        ),
    )
    registry = _registry(
        PersonaCheck(
            id="narrative_risk_matches_override",
            type="scorecard_alignment",
            scope="each_narrative",
            risk_keyword_map=(
                ("high", ("high risk", "at risk")),
                ("low", ("on track", "low risk")),
            ),
            message="Narrative risk must match override risk.",
            severity="warn",
        ),
        PersonaCheck(
            id="no_dfd_when_eta_is_set",
            type="terminology_consistency",
            scope="each_narrative",
            source_field="eta",
            when_source_present=type(
                "WhenSourcePresentStub",
                (),
                {
                    "forbidden_patterns": (r"\bDFD\b",),
                    "required_pattern": None,
                },
            )(),
            message="DFD forbidden when ETA exists.",
            severity="warn",
        ),
    )

    report = run_persona_checks(
        registry=registry,
        exec_summary_text="",
        workstream_blurbs={},
        loaded_narratives={
            "ws_platform": "__Platform__ (Low) On track, but DFD remains for final rollout."
        },
        rendered_html="",
        subject_line="",
        ban_rule_results=(),
        structural_rule_results=(),
        editorial_rules=_rules(),
        overrides=overrides,
        program_phase=None,
        evaluation_date=date(2026, 6, 27),
    )

    assert report is not None
    assert {result.check_id for result in report.failed} == {
        "narrative_risk_matches_override",
        "no_dfd_when_eta_is_set",
    }


def test_section_structure_enforces_missing_subrule() -> None:
    registry = _registry(
        PersonaCheck(
            id="exec_summary_structure",
            type="section_structure",
            scope="exec_summary",
            rules=(
                type(
                    "SectionSubRuleStub",
                    (),
                    {
                        "id": "needs_wins_heading",
                        "pattern": r"Recent Wins",
                        "message": "Recent Wins heading required.",
                        "require": True,
                        "min_matching_lines": 0,
                    },
                )(),
            ),
            message="Exec summary structure invalid.",
            severity="block",
        ),
    )

    report = run_persona_checks(
        registry=registry,
        exec_summary_text="- __Platform__ (High) Launch risk",
        workstream_blurbs={},
        loaded_narratives={},
        rendered_html="",
        subject_line="",
        ban_rule_results=(),
        structural_rule_results=(),
        editorial_rules=_rules(),
        overrides=None,
        program_phase=None,
        evaluation_date=date(2026, 6, 27),
    )

    assert report is not None
    assert len(report.blocks) == 1

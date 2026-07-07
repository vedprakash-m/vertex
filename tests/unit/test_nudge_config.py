"""Unit tests for src.core.nudge_config."""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.core.exceptions import ConfigError
from src.core.nudge_config import load_nudge_config, parse_stale_overrides, validate_nudge_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_edition_yaml(
    program_id: str,
    sections_block: str,
    full_hygiene_extras: str = "",
) -> str:
    """Build a valid nudge edition YAML string with proper indentation."""
    # sections_block must be pre-indented at 2-space level relative to full_hygiene:
    fh_extras_indented = ""
    if full_hygiene_extras.strip():
        fh_extras_indented = "\n".join(
            f"  {line}" if line.strip() else ""
            for line in full_hygiene_extras.strip().splitlines()
        ) + "\n"
    return (
        f"schema_version: '2.0'\n"
        f"id: {program_id}_nudge\n"
        f"program_id: {program_id}\n"
        f"type: nudge\n"
        f"hygiene:\n"
        f"  cooldown_days: 7\n"
        f"  comment_window_days: 7\n"
        f"full_hygiene:\n"
        f"  recipient: tpm\n"
        f"  brand_label: \"Test Brand\"\n"
        f"  status_keywords:\n"
        f"    - blocked\n"
        f"    - on track\n"
        f"  risk_on_track_values:\n"
        f"    - \"On Track\"\n"
        + fh_extras_indented
        + sections_block
    )


def _make_edition(
    tmp_path: Path,
    program_id: str,
    custom_sections: str = "",
    full_hygiene_extras: str = "",
) -> Path:
    edition_dir = tmp_path / "programs" / program_id / "editions"
    edition_dir.mkdir(parents=True, exist_ok=True)
    default_sections = (
        "  sections:\n"
        "    - id: priority\n"
        "      title: \"Priority Items\"\n"
        "      criteria:\n"
        "        source: registry\n"
        "      stale_business_days: 2\n"
    )
    sections_block = custom_sections if custom_sections else default_sections
    content = _make_edition_yaml(program_id, sections_block, full_hygiene_extras)
    path = edition_dir / f"{program_id}_nudge.yaml"
    path.write_text(content, encoding="utf-8")
    return tmp_path / "programs"


def _make_mock_program(has_ado: bool = False) -> Any:
    program = MagicMock()
    program.ado = None
    if has_ado:
        ado = MagicMock()
        ado.organization = "msazure"
        ado.project = "One"
        ado.area_paths = ["One\\Xstore"]
        ado.work_item_types = []
        ado.excluded_states = []
        ado.api_timeout_seconds = 30
        program.ado = ado
    return program


def _make_templates_root(tmp_path: Path) -> Path:
    tpl_dir = tmp_path / "templates" / "partials"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    (tpl_dir / "nudge_full_hygiene.j2").write_text("{# stub #}", encoding="utf-8")
    (tpl_dir / "nudge_full_hygiene_alt.j2").write_text("{# stub #}", encoding="utf-8")
    return tmp_path / "templates"


# ---------------------------------------------------------------------------
# parse_stale_overrides
# ---------------------------------------------------------------------------


def test_parse_stale_overrides_basic() -> None:
    result = parse_stale_overrides(["priority=3", "remaining_ramp=5"])
    assert result == {"priority": 3, "remaining_ramp": 5}


def test_parse_stale_overrides_empty() -> None:
    assert parse_stale_overrides([]) == {}


def test_parse_stale_overrides_rejects_zero() -> None:
    with pytest.raises(ValueError, match="days must be positive"):
        parse_stale_overrides(["priority=0"])


def test_parse_stale_overrides_rejects_missing_equals() -> None:
    with pytest.raises(ValueError, match="expected id=days"):
        parse_stale_overrides(["priority3"])


def test_parse_stale_overrides_rejects_duplicate() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        parse_stale_overrides(["priority=2", "priority=4"])


def test_parse_stale_overrides_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="section id is empty"):
        parse_stale_overrides(["=3"])


def test_parse_stale_overrides_rejects_non_int_days() -> None:
    with pytest.raises(ValueError, match="days must be an integer"):
        parse_stale_overrides(["priority=two"])


# ---------------------------------------------------------------------------
# load_nudge_config — new format
# ---------------------------------------------------------------------------


def test_load_nudge_config_new_format_registry(tmp_path: Path) -> None:
    programs_root = _make_edition(tmp_path, "nova")
    tpl_root = _make_templates_root(tmp_path)
    program = _make_mock_program(has_ado=False)

    config = load_nudge_config(
        program_id="nova",
        program=program,
        programs_root=programs_root,
        templates_root=tpl_root,
    )

    assert len(config.sections) == 1
    assert config.sections[0].id == "priority"
    assert config.sections[0].stale_business_days == 2
    assert config.sections[0].letter == "A"
    assert config.sections[0].criteria.source == "registry"
    assert config.delivery.recipient == "tpm"
    assert config.evaluation.cooldown_days == 7
    assert config.evaluation.comment_window_days == 7
    assert "blocked" in config.evaluation.status_keywords
    assert "On Track" in config.evaluation.risk_on_track_values


def test_load_nudge_config_auto_assigns_letters(tmp_path: Path) -> None:
    programs_root = _make_edition(tmp_path, "multi", custom_sections=(
        "  sections:\n"
        "    - id: alpha\n"
        "      title: Alpha\n"
        "      criteria:\n"
        "        source: registry\n"
        "      stale_business_days: 2\n"
        "    - id: beta\n"
        "      title: Beta\n"
        "      criteria:\n"
        "        source: registry\n"
        "      stale_business_days: 3\n"
        "    - id: gamma\n"
        "      title: Gamma\n"
        "      criteria:\n"
        "        source: registry\n"
        "      stale_business_days: 4\n"
    ))
    tpl_root = _make_templates_root(tmp_path)
    program = _make_mock_program(has_ado=False)

    config = load_nudge_config(
        program_id="multi",
        program=program,
        programs_root=programs_root,
        templates_root=tpl_root,
    )

    assert config.sections[0].letter == "A"
    assert config.sections[1].letter == "B"
    assert config.sections[2].letter == "C"


def test_load_nudge_config_tag_section_requires_ado(tmp_path: Path) -> None:
    programs_root = _make_edition(tmp_path, "nova", custom_sections=(
        "  sections:\n"
        "    - id: ramp\n"
        "      title: Ramp\n"
        "      criteria:\n"
        "        source: tag\n"
        "        tags:\n"
        "          - RAMPP1\n"
        "      stale_business_days: 4\n"
    ))
    tpl_root = _make_templates_root(tmp_path)
    program = _make_mock_program(has_ado=False)  # no ADO!

    with pytest.raises(ConfigError, match="program.ado is required"):
        load_nudge_config(
            program_id="nova",
            program=program,
            programs_root=programs_root,
            templates_root=tpl_root,
        )


def test_load_nudge_config_tag_section_with_ado(tmp_path: Path) -> None:
    programs_root = _make_edition(tmp_path, "nova", custom_sections=(
        "  sections:\n"
        "    - id: ramp\n"
        "      title: Ramp Items\n"
        "      criteria:\n"
        "        source: tag\n"
        "        tags:\n"
        "          - RAMPP1\n"
        "          - RAMP P1\n"
        "      stale_business_days: 4\n"
    ))
    tpl_root = _make_templates_root(tmp_path)
    program = _make_mock_program(has_ado=True)

    config = load_nudge_config(
        program_id="nova",
        program=program,
        programs_root=programs_root,
        templates_root=tpl_root,
    )

    assert len(config.sections) == 1
    assert config.sections[0].id == "ramp"
    assert config.sections[0].criteria.source == "tag"
    assert "RAMPP1" in config.sections[0].criteria.tags
    assert "RAMP P1" in config.sections[0].criteria.tags


def test_load_nudge_config_section_cooldown_overrides_global(tmp_path: Path) -> None:
    programs_root = _make_edition(tmp_path, "nova", custom_sections=(
        "  sections:\n"
        "    - id: priority\n"
        "      title: Priority\n"
        "      criteria:\n"
        "        source: registry\n"
        "      stale_business_days: 2\n"
        "      cooldown_days: 3\n"
    ))
    tpl_root = _make_templates_root(tmp_path)
    program = _make_mock_program()

    config = load_nudge_config(
        program_id="nova",
        program=program,
        programs_root=programs_root,
        templates_root=tpl_root,
    )

    # Per-section cooldown_days=3 should be stored
    assert config.sections[0].cooldown_days == 3
    # Global cooldown is still 7
    assert config.evaluation.cooldown_days == 7


def test_load_nudge_config_rejects_duplicate_section_ids(tmp_path: Path) -> None:
    programs_root = _make_edition(tmp_path, "nova", custom_sections=(
        "  sections:\n"
        "    - id: priority\n"
        "      title: Priority A\n"
        "      criteria:\n"
        "        source: registry\n"
        "      stale_business_days: 2\n"
        "    - id: priority\n"
        "      title: Priority B\n"
        "      criteria:\n"
        "        source: registry\n"
        "      stale_business_days: 3\n"
    ))
    tpl_root = _make_templates_root(tmp_path)
    program = _make_mock_program()

    with pytest.raises(ConfigError, match="Duplicate section id"):
        load_nudge_config(
            program_id="nova",
            program=program,
            programs_root=programs_root,
            templates_root=tpl_root,
        )


def test_load_nudge_config_missing_edition_raises_config_error(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    tpl_root = _make_templates_root(tmp_path)
    program = _make_mock_program()

    with pytest.raises(ConfigError):
        load_nudge_config(
            program_id="nova",
            program=program,
            programs_root=programs_root,
            templates_root=tpl_root,
        )


# ---------------------------------------------------------------------------
# load_nudge_config — legacy shim
# ---------------------------------------------------------------------------


def test_load_nudge_config_legacy_shim_emits_deprecation_warning(tmp_path: Path) -> None:
    edition_dir = tmp_path / "programs" / "nova" / "editions"
    edition_dir.mkdir(parents=True, exist_ok=True)
    content = textwrap.dedent("""\
        schema_version: '2.0'
        id: nova_nudge
        program_id: nova
        type: nudge
        hygiene:
          cooldown_days: 7
          comment_window_days: 7
        full_hygiene:
          recipient: tpm
          brand_label: "NOVA"
          ramp_p1_tag:
            - "RAMPP1"
          post_ramp_tag: "POST RAMP"
          area_paths:
            - "One\\\\Xstore"
          status_keywords:
            - blocked
          risk_on_track_values:
            - "On Track"
    """)
    (edition_dir / "nova_nudge.yaml").write_text(content, encoding="utf-8")
    programs_root = tmp_path / "programs"
    tpl_root = _make_templates_root(tmp_path)
    program = _make_mock_program(has_ado=True)

    with pytest.warns(DeprecationWarning, match="legacy full_hygiene flat keys"):
        config = load_nudge_config(
            program_id="nova",
            program=program,
            programs_root=programs_root,
            templates_root=tpl_root,
        )

    # Legacy shim produces sections: A (registry), B (ramp_p1_tag), C (post_ramp_tag)
    assert len(config.sections) >= 1
    assert config.sections[0].id == "priority"  # Section A always registry in legacy shim


# ---------------------------------------------------------------------------
# validate_nudge_config
# ---------------------------------------------------------------------------


def test_validate_nudge_config_empty_recipient_is_error() -> None:
    from src.core.nudge_models import (  # noqa: PLC0415
        NudgeConfig, NudgeDeliveryConfig, NudgeEvaluationConfig,
        NudgePresentationConfig, NudgeSectionCriteria, NudgeSectionSpec,
    )
    config = NudgeConfig(
        sections=(NudgeSectionSpec(
            id="s1", title="S1",
            criteria=NudgeSectionCriteria(source="registry"),
            stale_business_days=2, letter="A",
        ),),
        delivery=NudgeDeliveryConfig(recipient="", delivery_mode="broadcast", cadence_days=7),
        evaluation=NudgeEvaluationConfig(
            comment_window_days=7, status_keywords=(), risk_on_track_values=(),
            cooldown_days=7, nudge_exempt_item_ids=frozenset(),
        ),
        presentation=NudgePresentationConfig(
            brand_label="Test", email_subject_label="Test",
            template="partials/nudge_full_hygiene.j2", preheader="",
            compress_titles_with_ai=False,
        ),
    )
    errors = validate_nudge_config(config, MagicMock(ado=None))
    assert any("recipient" in e.lower() for e in errors)


def test_validate_nudge_config_zero_cooldown_is_error() -> None:
    from src.core.nudge_models import (  # noqa: PLC0415
        NudgeConfig, NudgeDeliveryConfig, NudgeEvaluationConfig,
        NudgePresentationConfig, NudgeSectionCriteria, NudgeSectionSpec,
    )
    config = NudgeConfig(
        sections=(NudgeSectionSpec(
            id="s1", title="S1",
            criteria=NudgeSectionCriteria(source="registry"),
            stale_business_days=2, letter="A",
        ),),
        delivery=NudgeDeliveryConfig(recipient="alice", delivery_mode="broadcast", cadence_days=7),
        evaluation=NudgeEvaluationConfig(
            comment_window_days=7, status_keywords=(), risk_on_track_values=(),
            cooldown_days=0, nudge_exempt_item_ids=frozenset(),
        ),
        presentation=NudgePresentationConfig(
            brand_label="Test", email_subject_label="Test",
            template="partials/nudge_full_hygiene.j2", preheader="",
            compress_titles_with_ai=False,
        ),
    )
    errors = validate_nudge_config(config, MagicMock(ado=None))
    assert any("cooldown" in e.lower() for e in errors)

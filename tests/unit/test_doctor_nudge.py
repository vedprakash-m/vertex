"""Unit tests for nudge doctor checks NQ-1 through NQ-9."""
from __future__ import annotations

import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.commands.doctor_checks.nudge_checks import run_nudge_doctor
from src.core.nudge_models import NUDGE_AUDIT_MAX_BYTES, NUDGE_STATE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_edition(tmp_path: Path, program_id: str, content: str | None = None) -> tuple[Path, Path]:
    programs_root = tmp_path / "programs"
    edition_dir = programs_root / program_id / "editions"
    edition_dir.mkdir(parents=True, exist_ok=True)
    tpl_root = tmp_path / "templates"
    (tpl_root / "partials").mkdir(parents=True, exist_ok=True)
    (tpl_root / "partials" / "nudge_full_hygiene.j2").write_text("{# stub #}", encoding="utf-8")
    (tpl_root / "partials" / "nudge_full_hygiene_alt.j2").write_text("{# stub #}", encoding="utf-8")

    default_content = textwrap.dedent(f"""\
        schema_version: '2.0'
        id: {program_id}_nudge
        program_id: {program_id}
        type: nudge
        hygiene:
          cooldown_days: 7
          comment_window_days: 7
        full_hygiene:
          recipient: tpm
          brand_label: "Test"
          status_keywords:
            - blocked
          risk_on_track_values:
            - "On Track"
          sections:
            - id: priority
              title: Priority Items
              criteria:
                source: registry
              stale_business_days: 2
    """)
    (edition_dir / f"{program_id}_nudge.yaml").write_text(
        content or default_content, encoding="utf-8"
    )
    return programs_root, tpl_root


# ---------------------------------------------------------------------------
# NQ-1: Edition file present
# ---------------------------------------------------------------------------


def test_nq1_missing_edition_file_fails(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    tpl_root = tmp_path / "templates"
    tpl_root.mkdir()

    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq1 = next(c for c in checks if "NQ-1" in c.label)
    assert nq1.status == "fail"
    assert "Missing" in nq1.detail


def test_nq1_present_edition_passes(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq1 = next(c for c in checks if "NQ-1" in c.label)
    assert nq1.status == "ok"


# ---------------------------------------------------------------------------
# NQ-2: sections format
# ---------------------------------------------------------------------------


def test_nq2_new_sections_format_passes(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq2 = next(c for c in checks if "NQ-2" in c.label)
    assert nq2.status == "ok"


def test_nq2_legacy_format_warns(tmp_path: Path) -> None:
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
          ramp_p1_tag:
            - "RAMPP1"
          area_paths:
            - "One\\\\Xstore"
          status_keywords:
            - blocked
          risk_on_track_values:
            - "On Track"
    """)
    programs_root, tpl_root = _make_edition(tmp_path, "nova", content=content)
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq2 = next(c for c in checks if "NQ-2" in c.label)
    assert nq2.status == "warn"
    assert "Legacy" in nq2.detail or "legacy" in nq2.detail


# ---------------------------------------------------------------------------
# NQ-4: Template exists
# ---------------------------------------------------------------------------


def test_nq4_missing_template_fails(tmp_path: Path) -> None:
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
          template: "partials/nonexistent_template.j2"
          status_keywords:
            - blocked
          risk_on_track_values:
            - "On Track"
          sections:
            - id: priority
              title: Priority
              criteria:
                source: registry
              stale_business_days: 2
    """)
    programs_root, tpl_root = _make_edition(tmp_path, "nova", content=content)
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq4 = next(c for c in checks if "NQ-4" in c.label)
    assert nq4.status == "fail"


def test_nq4_existing_template_passes(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq4 = next(c for c in checks if "NQ-4" in c.label)
    assert nq4.status == "ok"


# ---------------------------------------------------------------------------
# NQ-5: State file valid
# ---------------------------------------------------------------------------


def test_nq5_no_state_file_ok(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq5 = next(c for c in checks if "NQ-5" in c.label)
    assert nq5.status == "ok"
    assert "first run" in nq5.detail


def test_nq5_valid_state_file_passes(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    now_ts = datetime(2026, 6, 21, tzinfo=timezone.utc).isoformat()
    state_path = programs_root / "nova" / "nudge_state.json"
    state_path.write_text(
        json.dumps({"schema_version": "1.1", "item:1001": now_ts}),
        encoding="utf-8",
    )
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq5 = next(c for c in checks if "NQ-5" in c.label)
    assert nq5.status == "ok"


def test_nq5_valid_schema_1_2_dict_state_passes(tmp_path: Path) -> None:
    """Schema 1.2 stores dict values ({triggered_at, origin, run_id}) per D-5;
    NQ-5 must not flag these as invalid timestamps."""
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    now_ts = datetime(2026, 6, 21, tzinfo=timezone.utc).isoformat()
    state_path = programs_root / "nova" / "nudge_state.json"
    state_path.write_text(
        json.dumps({
            "schema_version": "1.2",
            "item:1001": {"origin": "mark_sent", "run_id": "run_1", "triggered_at": now_ts},
        }),
        encoding="utf-8",
    )
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq5 = next(c for c in checks if "NQ-5" in c.label)
    assert nq5.status == "ok"
    assert "1 cooldown record" in nq5.detail


def test_nq5_schema_1_2_bad_triggered_at_flagged(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    state_path = programs_root / "nova" / "nudge_state.json"
    state_path.write_text(
        json.dumps({
            "schema_version": "1.2",
            "item:1001": {"origin": "mark_sent", "run_id": "run_1", "triggered_at": "not-a-date"},
        }),
        encoding="utf-8",
    )
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq5 = next(c for c in checks if "NQ-5" in c.label)
    assert nq5.status == "warn"
    assert "1 invalid timestamp" in nq5.detail


def test_nq5_corrupt_state_file_fails(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    state_path = programs_root / "nova" / "nudge_state.json"
    state_path.write_text("not valid json!!", encoding="utf-8")
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq5 = next(c for c in checks if "NQ-5" in c.label)
    assert nq5.status == "fail"


# ---------------------------------------------------------------------------
# NQ-6: State schema version current
# ---------------------------------------------------------------------------


def test_nq6_current_schema_version_passes(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    state_path = programs_root / "nova" / "nudge_state.json"
    now_ts = datetime(2026, 6, 21, tzinfo=timezone.utc).isoformat()
    state_path.write_text(
        json.dumps({"schema_version": NUDGE_STATE_SCHEMA_VERSION, "item:1001": now_ts}),
        encoding="utf-8",
    )
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq6 = next(c for c in checks if "NQ-6" in c.label)
    assert nq6.status == "ok"


def test_nq6_old_schema_version_warns(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    state_path = programs_root / "nova" / "nudge_state.json"
    now_ts = datetime(2026, 6, 21, tzinfo=timezone.utc).isoformat()
    state_path.write_text(
        json.dumps({"schema_version": "1.0", "item:1001": now_ts}),
        encoding="utf-8",
    )
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq6 = next(c for c in checks if "NQ-6" in c.label)
    assert nq6.status == "warn"


# ---------------------------------------------------------------------------
# NQ-7: Section IDs unique and non-empty
# ---------------------------------------------------------------------------


def test_nq7_unique_section_ids_pass(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq7 = next(c for c in checks if "NQ-7" in c.label)
    assert nq7.status == "ok"


def test_nq7_duplicate_section_ids_fail(tmp_path: Path) -> None:
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
          status_keywords:
            - blocked
          risk_on_track_values:
            - "On Track"
          sections:
            - id: priority
              title: Priority A
              criteria:
                source: registry
              stale_business_days: 2
            - id: priority
              title: Priority B
              criteria:
                source: registry
              stale_business_days: 3
    """)
    programs_root, tpl_root = _make_edition(tmp_path, "nova", content=content)
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq7 = next(c for c in checks if "NQ-7" in c.label)
    assert nq7.status == "fail"


# ---------------------------------------------------------------------------
# NQ-8: No @example.com in config
# ---------------------------------------------------------------------------


def test_nq8_no_example_com_passes(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq8 = next(c for c in checks if "NQ-8" in c.label)
    assert nq8.status == "ok"


def test_nq8_example_com_in_recipient_fails(tmp_path: Path) -> None:
    content = textwrap.dedent("""\
        schema_version: '2.0'
        id: nova_nudge
        program_id: nova
        type: nudge
        hygiene:
          cooldown_days: 7
          comment_window_days: 7
        full_hygiene:
          recipient: user@example.com
          status_keywords:
            - blocked
          risk_on_track_values:
            - "On Track"
          sections:
            - id: priority
              title: Priority
              criteria:
                source: registry
              stale_business_days: 2
    """)
    programs_root, tpl_root = _make_edition(tmp_path, "nova", content=content)
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq8 = next(c for c in checks if "NQ-8" in c.label)
    assert nq8.status == "fail"


# ---------------------------------------------------------------------------
# NQ-9: Audit JSONL size
# ---------------------------------------------------------------------------


def test_nq9_no_audit_log_passes(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq9 = next(c for c in checks if "NQ-9" in c.label)
    assert nq9.status == "ok"
    assert "first run" in nq9.detail


def test_nq9_small_audit_log_passes(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    from src.core.edition_resolver import get_nudge_paths  # noqa: PLC0415
    np = get_nudge_paths("nova", programs_root=programs_root)
    np.audit_path.parent.mkdir(parents=True, exist_ok=True)
    np.audit_path.write_text('{"event_type":"dry_run"}\n', encoding="utf-8")

    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq9 = next(c for c in checks if "NQ-9" in c.label)
    assert nq9.status == "ok"


def test_nq9_oversized_audit_log_fails(tmp_path: Path) -> None:
    programs_root, tpl_root = _make_edition(tmp_path, "nova")
    from src.core.edition_resolver import get_nudge_paths  # noqa: PLC0415
    np = get_nudge_paths("nova", programs_root=programs_root)
    np.audit_path.parent.mkdir(parents=True, exist_ok=True)
    # Write exactly the cap size
    np.audit_path.write_bytes(b"x" * NUDGE_AUDIT_MAX_BYTES)

    checks = run_nudge_doctor("nova", programs_root=programs_root, templates_root=tpl_root)
    nq9 = next(c for c in checks if "NQ-9" in c.label)
    assert nq9.status == "fail"

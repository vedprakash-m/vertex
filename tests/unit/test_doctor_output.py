"""Direct coverage for the extracted doctor presentation surface.

Guards the Phase 3 doctor-decomposition extraction of the pure presentation
helpers (`doctor_tip`, `build_doctor_payload`, `render_doctor_output`) into
`src/commands/doctor_checks/output.py`. These must stay side-effect free and
behaviourally identical to the historical in-`doctor.py` implementations.
"""

from __future__ import annotations

import csv
import json

import pytest
import typer

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.commands.doctor_checks.output import (
    build_doctor_payload,
    doctor_tip,
    render_doctor_output,
)


def _all_tip_flags_false() -> dict[str, bool]:
    return {
        "check_auth": False,
        "operator_gates": False,
        "platform_readiness": False,
        "kb": False,
        "ids": False,
        "cadence": False,
        "channels": False,
        "privacy": False,
        "kusto": False,
        "milestones": False,
        "dependencies": False,
        "actions": False,
        "risks": False,
        "escalations": False,
        "decisions": False,
        "assumptions": False,
        "readiness": False,
        "semantic_index": False,
        "metric_bindings": False,
        "consistency": False,
        "checkpoints": False,
        "storage": False,
        "charts": False,
        "watch_sources": False,
        "catchup_log": False,
        "circuit_breakers": False,
        "context": False,
    }


def test_doctor_tip_default_is_fix_hint() -> None:
    assert doctor_tip(**_all_tip_flags_false()) == "Tip: Run `vertex doctor --fix` to auto-repair common issues."


def test_doctor_tip_context_branch() -> None:
    flags = _all_tip_flags_false()
    flags["context"] = True
    assert "--context" in doctor_tip(**flags)


@pytest.mark.parametrize(
    "flag, fragment",
    [
        ("check_auth", "--check-auth"),
        ("operator_gates", "--operator-gates"),
        ("storage", "--storage"),
        ("charts", "--charts"),
        ("circuit_breakers", "--circuit-breakers"),
        ("schedule_health", "--schedule-health"),
    ],
)
def test_doctor_tip_selected_branches(flag: str, fragment: str) -> None:
    flags = _all_tip_flags_false()
    flags[flag] = True
    assert fragment in doctor_tip(**flags)


def test_doctor_tip_first_true_flag_wins() -> None:
    # check_auth precedes operator_gates in the evaluation order.
    flags = _all_tip_flags_false()
    flags["check_auth"] = True
    flags["operator_gates"] = True
    assert "--check-auth" in doctor_tip(**flags)


def _sample_report() -> DoctorReport:
    return DoctorReport(
        edition="acme_weekly",
        checks=(
            DoctorCheck(label="Auth", status="ok", detail="all good"),
            DoctorCheck(label="IDs", status="warn", detail="missing id", metadata={"missing": 1}),
        ),
    )


def test_build_doctor_payload_shape() -> None:
    payload = build_doctor_payload(report=_sample_report(), tip="do thing")
    assert payload["edition"] == "acme_weekly"
    assert payload["warnings"] == 1
    assert payload["failures"] == 0
    assert payload["tip"] == "do thing"
    checks = payload["checks"]
    assert isinstance(checks, list) and len(checks) == 2
    # metadata only present when truthy
    assert "metadata" not in checks[0]
    assert checks[1]["metadata"] == {"missing": 1}


def test_render_doctor_output_json_roundtrip() -> None:
    payload = build_doctor_payload(report=_sample_report(), tip=None)
    rendered = render_doctor_output(payload, format="json")
    assert json.loads(rendered) == payload


def test_render_doctor_output_csv_rows() -> None:
    payload = build_doctor_payload(report=_sample_report(), tip="t")
    rows = list(csv.DictReader(render_doctor_output(payload, format="csv").splitlines()))
    assert len(rows) == 2
    assert rows[0]["label"] == "Auth"
    assert rows[1]["metadata_json"] == json.dumps({"missing": 1}, sort_keys=True)


def test_render_doctor_output_csv_empty_checks() -> None:
    payload = build_doctor_payload(report=DoctorReport(edition="e", checks=()), tip="t")
    rows = list(csv.reader(render_doctor_output(payload, format="csv").splitlines()))
    # header + one placeholder row
    assert len(rows) == 2
    assert rows[0][0] == "edition"


def test_render_doctor_output_human_and_bad_format_raise() -> None:
    payload = build_doctor_payload(report=_sample_report(), tip=None)
    with pytest.raises(typer.BadParameter):
        render_doctor_output(payload, format="human")
    with pytest.raises(typer.BadParameter):
        render_doctor_output(payload, format="xml")

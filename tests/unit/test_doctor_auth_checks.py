from __future__ import annotations

from src.commands.doctor_checks.auth_checks import missing_workiq_tools


def test_missing_workiq_tools_accepts_ask_work_iq_capability() -> None:
    assert missing_workiq_tools(("ask_work_iq",)) == ()

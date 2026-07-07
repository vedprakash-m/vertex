from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.commands.doctor_checks.channel_source_health_checks import slice_source_health_check


def test_slice_source_health_check_warns_with_waived_role(monkeypatch) -> None:
    waived_until = datetime(2026, 6, 30, tzinfo=timezone.utc)
    unhealthy_role = SimpleNamespace(
        contract_id="acme.deployment_velocity",
        role="telemetry",
        state="stale",
        waiver=SimpleNamespace(expires=waived_until, owner="owner@example.com"),
        blocks_confirm=False,
        last_yield="2026-06-01",
        last_fresh=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    summary = SimpleNamespace(
        unhealthy_roles=(unhealthy_role,),
        healthy_contract_count=2,
        contract_count=3,
        function="newsletter",
        waived_contract_count=1,
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.channel_source_health_checks.build_slice_source_health_summary",
        lambda slice_contracts, gather_state, *, waivers, function_name: summary,
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.channel_source_health_checks.build_transcript_source_health",
        lambda gather_state, waivers: None,
    )

    check = slice_source_health_check(["slice"], object(), (), function_name="newsletter")

    assert check is not None
    assert check.status == "warn"
    assert "[waived until 2026-06-30T00:00:00+00:00 by owner@example.com]" in check.detail
    assert check.metadata is not None
    assert check.metadata["unhealthy_roles"][0]["waiver_owner"] == "owner@example.com"


def test_slice_source_health_check_fails_when_blocking_transcript_is_only_extra_role(monkeypatch) -> None:
    unhealthy_roles = tuple(
        SimpleNamespace(
            contract_id=f"contract-{index}",
            role="telemetry",
            state="stale",
            waiver=None,
            blocks_confirm=False,
            last_yield=f"yield-{index}",
            last_fresh=None,
        )
        for index in range(4)
    )
    summary = SimpleNamespace(
        unhealthy_roles=unhealthy_roles,
        healthy_contract_count=0,
        contract_count=4,
        function="deck",
        waived_contract_count=0,
    )
    transcript_role = SimpleNamespace(
        contract_id="vertex/transcript",
        role="transcript",
        state="auth_failed",
        waiver=None,
        blocks_confirm=True,
        last_yield=None,
        last_fresh=None,
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.channel_source_health_checks.build_slice_source_health_summary",
        lambda slice_contracts, gather_state, *, waivers, function_name: summary,
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.channel_source_health_checks.build_transcript_source_health",
        lambda gather_state, waivers: transcript_role,
    )

    check = slice_source_health_check(["slice"], object(), (), function_name="deck")

    assert check is not None
    assert check.status == "fail"
    assert "vertex/transcript:transcript=auth_failed" not in check.detail
    assert check.metadata is not None
    assert len(check.metadata["unhealthy_roles"]) == 5
    assert check.metadata["unhealthy_roles"][-1]["contract_id"] == "vertex/transcript"

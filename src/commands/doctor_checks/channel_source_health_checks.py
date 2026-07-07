from __future__ import annotations

from typing import Any

from src.commands.doctor_checks.models import DoctorCheck
from src.core.source_health import build_slice_source_health_summary, build_transcript_source_health


def slice_source_health_check(
    slice_contracts: Any,
    gather_state: Any,
    waivers: Any = (),
    *,
    function_name: str = "newsletter",
) -> DoctorCheck | None:
    summary = build_slice_source_health_summary(
        slice_contracts,
        gather_state,
        waivers=tuple(waivers),
        function_name=function_name,
    )
    if summary is None:
        return None
    unhealthy_roles = list(summary.unhealthy_roles)
    transcript_health = build_transcript_source_health(gather_state, tuple(waivers))
    if transcript_health is not None and transcript_health.state != "healthy":
        unhealthy_roles.append(transcript_health)
    detail = f"{summary.healthy_contract_count}/{summary.contract_count} {summary.function} slice source contracts fully healthy."
    if summary.waived_contract_count:
        detail = f"{detail} {summary.waived_contract_count} contract(s) currently waived."
    if unhealthy_roles:
        detail = (
            f"{detail} Issues: "
            + ", ".join(
                f"{role.contract_id}:{role.role}={role.state}"
                + (
                    f" [waived until {role.waiver.expires.isoformat()} by {role.waiver.owner}]"
                    if role.waiver is not None
                    else ""
                )
                for role in unhealthy_roles[:4]
            )
            + "."
        )
    has_blocking_roles = any(role.blocks_confirm for role in unhealthy_roles)
    return DoctorCheck(
        "Source Health",
        "fail" if has_blocking_roles else ("warn" if unhealthy_roles else "ok"),
        detail,
        metadata={
            "function": summary.function,
            "contract_count": summary.contract_count,
            "healthy_contract_count": summary.healthy_contract_count,
            "unhealthy_roles": [
                {
                    "contract_id": role.contract_id,
                    "role": role.role,
                    "state": role.state,
                    "last_yield": role.last_yield,
                    "last_fresh": None if role.last_fresh is None else role.last_fresh.isoformat(),
                    "blocks_confirm": role.blocks_confirm,
                    "waiver_owner": None if role.waiver is None else role.waiver.owner,
                    "waiver_expires": None if role.waiver is None else role.waiver.expires.isoformat(),
                }
                for role in unhealthy_roles
            ],
        },
    )

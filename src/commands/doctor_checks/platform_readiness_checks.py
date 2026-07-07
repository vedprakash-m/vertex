from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.commands.fleet import build_fleet_report
from src.core.gather_state_store import load_gather_state
from src.core.platform_proof_catalog import (
    PlatformProofDefinition,
    iter_platform_archetype_proof_definitions,
    iter_platform_core_proof_definitions,
)
from src.core.platform_proof_log_store import PlatformProofRecord, load_platform_proof_records
from src.core.platform_s7_store import load_platform_s7_state
from src.core.store_factory import build_signal_store, build_signal_store_for_program_id


def run_platform_readiness_doctor(
    *,
    programs_root: Path,
    reports_root: Path,
    editions_root: Path,
    run_channel_doctor_fn: Callable[..., DoctorReport],
) -> DoctorReport:
    fleet_report = build_fleet_report(programs_root=programs_root)
    proof_records_by_program = load_platform_proof_records_by_program(programs_root=programs_root)

    checks = [
        platform_fleet_active_programs_check(fleet_report),
        platform_adapter_coverage_check(fleet_report, programs_root=programs_root),
        platform_confirmed_program_channel_health_check(
            fleet_report=fleet_report,
            reports_root=reports_root,
            editions_root=editions_root,
            programs_root=programs_root,
            run_channel_doctor_fn=run_channel_doctor_fn,
        ),
        *[
            platform_required_proof_check(
                definition,
                proof_records_by_program=proof_records_by_program,
            )
            for definition in iter_platform_core_proof_definitions()
        ],
        platform_archetype_proof_check(proof_records_by_program=proof_records_by_program),
        platform_s7_position_check(programs_root=programs_root),
    ]
    fail_count = sum(1 for check in checks if check.status == "fail")
    warn_count = sum(1 for check in checks if check.status == "warn")
    ok_count = sum(1 for check in checks if check.status == "ok")
    summary_status = "fail" if fail_count else "warn" if warn_count else "ok"
    summary = DoctorCheck(
        "Platform Readiness",
        summary_status,
        f"{ok_count}/{len(checks)} platform-readiness criteria currently pass from repo state; {fail_count} blocking and {warn_count} advisory criteria remain.",
        metadata={
            "program_count": len(fleet_report.programs),
            "ok_count": ok_count,
            "fail_count": fail_count,
            "warn_count": warn_count,
        },
    )
    return DoctorReport(edition="fleet", checks=(summary, *checks))


def load_platform_proof_records_by_program(*, programs_root: Path) -> dict[str, tuple[PlatformProofRecord, ...]]:
    records_by_program: dict[str, tuple[PlatformProofRecord, ...]] = {}
    for program_dir in sorted(programs_root.iterdir(), key=lambda entry: entry.name.lower()):
        if not program_dir.is_dir() or not (program_dir / "program.yaml").exists():
            continue
        records_by_program[program_dir.name] = load_platform_proof_records(program_dir.name, programs_root=programs_root)
    return records_by_program


def platform_fleet_active_programs_check(fleet_report: Any) -> DoctorCheck:
    active_confirmed = [
        program.program_id
        for program in fleet_report.programs
        if program.lifecycle_state == "active" and program.latest_issue_number is not None
    ]
    onboarding = [program.program_id for program in fleet_report.programs if program.lifecycle_state == "onboarding"]
    if len(active_confirmed) >= 2:
        return DoctorCheck(
            "PR:Fleet Active Programs",
            "ok",
            f"{len(active_confirmed)} active program(s) have at least one confirmed issue: {', '.join(active_confirmed)}.",
            metadata={
                "active_confirmed_program_ids": active_confirmed,
                "onboarding_program_ids": onboarding,
            },
        )
    return DoctorCheck(
        "PR:Fleet Active Programs",
        "fail",
        f"Only {len(active_confirmed)} active program(s) currently have confirmed issues ({', '.join(active_confirmed) or 'none'}). Onboarding/unproven programs: {', '.join(onboarding) or 'none'}.",
        metadata={
            "active_confirmed_program_ids": active_confirmed,
            "onboarding_program_ids": onboarding,
        },
    )


def platform_adapter_coverage_check(fleet_report: Any, *, programs_root: Path) -> DoctorCheck:
    adapters: dict[str, set[str]] = {
        "ADO": set(),
        "Kusto": set(),
        "IcM": set(),
        "M365/WorkIQ": set(),
        "Teams": set(),
    }
    for program in fleet_report.programs:
        gather_state = load_gather_state(program.program_id, programs_root=programs_root)
        channels = {} if gather_state is None else gather_state.channels
        if int((channels.get("ado") or {}).get("signal_count") or 0) > 0:
            adapters["ADO"].add(program.program_id)
        if int((channels.get("kusto") or {}).get("signal_count") or 0) > 0:
            adapters["Kusto"].add(program.program_id)
        if int((channels.get("icm") or {}).get("signal_count") or 0) > 0:
            adapters["IcM"].add(program.program_id)
        if int((channels.get("workiq") or {}).get("signal_count") or 0) > 0 or int((channels.get("transcript") or {}).get("signal_count") or 0) > 0:
            adapters["M365/WorkIQ"].add(program.program_id)
    # PB-40: build the signal store ONCE outside the per-program loop so
    # the Q: drive hang vector is closed. `signal_store.read(program_id)`
    # is a per-program call but it does NOT re-read the program; only
    # the store *construction* hits Q:.
    signal_store = build_signal_store(programs_root=programs_root)
    for program in fleet_report.programs:
        if any(signal.source == "teams" for signal in signal_store.read(program.program_id)):
            adapters["Teams"].add(program.program_id)

    missing = [adapter for adapter, program_ids in adapters.items() if not program_ids]
    detail = "; ".join(
        f"{adapter}: {', '.join(sorted(program_ids)) if program_ids else 'none'}"
        for adapter, program_ids in adapters.items()
    )
    return DoctorCheck(
        "PR:Adapter Coverage",
        "ok" if not missing else "fail",
        (
            f"Adapter certification evidence by program — {detail}."
            if not missing
            else f"Adapter certification is still incomplete ({', '.join(missing)} missing real-signal evidence). Current evidence — {detail}."
        ),
        metadata={
            "program_ids_by_adapter": {adapter: sorted(program_ids) for adapter, program_ids in adapters.items()},
            "missing_adapters": missing,
        },
    )


def platform_confirmed_program_channel_health_check(
    *,
    fleet_report: Any,
    reports_root: Path,
    editions_root: Path,
    programs_root: Path,
    run_channel_doctor_fn: Callable[..., DoctorReport],
) -> DoctorCheck:
    failing_programs: list[str] = []
    details: dict[str, str] = {}
    for program in fleet_report.programs:
        if program.lifecycle_state != "active" or program.latest_issue_number is None:
            continue
        channel_report = run_channel_doctor_fn(
            edition_name=program.primary_edition,
            reports_root=reports_root,
            editions_root=editions_root,
            programs_root=programs_root,
        )
        summary_check = next((check for check in channel_report.checks if check.label == "Channels"), None)
        if summary_check is not None and summary_check.status != "ok":
            failing_programs.append(program.program_id)
            details[program.program_id] = summary_check.detail
    if not failing_programs:
        return DoctorCheck(
            "PR:Confirmed Program Channel Health",
            "ok",
            "All active confirmed programs currently have passing `doctor --channels` summaries.",
            metadata={"failing_program_ids": []},
        )
    return DoctorCheck(
        "PR:Confirmed Program Channel Health",
        "fail",
        "Required-source/channel health is still open for: " + "; ".join(f"{program_id}: {details[program_id]}" for program_id in failing_programs),
        metadata={"failing_program_ids": failing_programs, "details": details},
    )


def platform_required_proof_check(
    definition: PlatformProofDefinition,
    *,
    proof_records_by_program: dict[str, tuple[PlatformProofRecord, ...]],
) -> DoctorCheck:
    passed_programs = sorted(
        program_id
        for program_id, records in proof_records_by_program.items()
        if any(record.proof_id == definition.proof_id and record.status == "passed" for record in records)
    )
    if passed_programs:
        return DoctorCheck(
            definition.label,
            "ok",
            f"Passed proof '{definition.proof_id}' is recorded for: {', '.join(passed_programs)}.",
            metadata={"proof_id": definition.proof_id, "program_ids": passed_programs, "phase": definition.phase},
        )
    return DoctorCheck(
        definition.label,
        "fail",
        f"UNPROVEN — no passed {definition.description.lower()} is recorded in any program's platform_proof_log.yaml.",
        metadata={"proof_id": definition.proof_id, "program_ids": [], "phase": definition.phase},
    )


def platform_archetype_proof_check(
    *,
    proof_records_by_program: dict[str, tuple[PlatformProofRecord, ...]],
) -> DoctorCheck:
    proof_programs: dict[str, list[str]] = {}
    missing_archetypes: list[str] = []
    for definition in iter_platform_archetype_proof_definitions():
        assert definition.archetype is not None
        program_ids = sorted(
            program_id
            for program_id, records in proof_records_by_program.items()
            if any(record.proof_id == definition.proof_id and record.status == "passed" for record in records)
        )
        proof_programs[definition.archetype] = program_ids
        if not program_ids:
            missing_archetypes.append(definition.archetype)
    if not missing_archetypes:
        detail = "; ".join(f"{archetype}: {', '.join(program_ids)}" for archetype, program_ids in proof_programs.items())
        return DoctorCheck(
            "PR:P6b Archetype Proofs",
            "ok",
            f"All declared archetypes have recorded passed proof runs — {detail}.",
            metadata={"program_ids_by_archetype": proof_programs, "missing_archetypes": [], "proof_ids": [definition.proof_id for definition in iter_platform_archetype_proof_definitions()]},
        )
    return DoctorCheck(
        "PR:P6b Archetype Proofs",
        "fail",
        "UNPROVEN — no passed proof record exists yet for: " + ", ".join(missing_archetypes) + ". Record those runs in platform_proof_log.yaml before claiming P6b readiness.",
        metadata={"program_ids_by_archetype": proof_programs, "missing_archetypes": missing_archetypes, "proof_ids": [definition.proof_id for definition in iter_platform_archetype_proof_definitions()]},
    )


def platform_s7_position_check(*, programs_root: Path) -> DoctorCheck:
    state = load_platform_s7_state(programs_root=programs_root)
    if state is None:
        return DoctorCheck(
            "PR:S7 Position",
            "warn",
            "S7 completion/deferral is still a manual sign-off item: the repo has rollback/read-inventory tooling, but no machine-readable flag yet records whether S7 is complete or explicitly deferred out of the V-11 bar.",
            metadata={
                "machine_readable": False,
                "manual_review_required": True,
            },
        )
    detail = (
        "S7 is recorded as complete in the machine-readable platform state."
        if state.position == "complete"
        else "S7 is explicitly deferred out of the V-11 bar with recorded justification in the machine-readable platform state."
    )
    return DoctorCheck(
        "PR:S7 Position",
        "ok",
        detail,
        metadata={
            "machine_readable": True,
            "manual_review_required": False,
            "position": state.position,
            "recorded_at": state.recorded_at.isoformat(),
            "recorded_by": state.recorded_by,
            "justification": state.justification,
        },
    )

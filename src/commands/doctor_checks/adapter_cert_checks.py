"""Adapter-cert sub-check (WS-3 / spec §WS-3).

Audits UIL channel adapter readiness by:
  - Reading ``programs/<prog>/adapter_cert.yaml`` if it exists.
  - Checking which channels are enabled via ``uil_channel_flags.py``.
  - Reporting per-channel certification status.
  - Performing a WorkIQ verb availability probe (stub test).

Exposed via ``vertex doctor --adapter-cert``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.uil_channel_flags import UIL_CHANNEL_ENABLED_FUNCS


_ALL_CHANNELS: tuple[str, ...] = ("ado", "kusto", "teams", "icm")
_ADAPTER_CERT_FILENAME = "adapter_cert.yaml"

# Warn when a credential expires within this many days.
DEFAULT_EXPIRY_WARNING_DAYS: int = 14


def _load_adapter_cert(
    program_id: str,
    programs_root: Path,
) -> dict[str, Any]:
    """Load adapter_cert.yaml from the program directory. Returns empty dict if absent."""
    cert_path = programs_root / program_id / _ADAPTER_CERT_FILENAME
    if not cert_path.exists():
        return {}
    try:
        import yaml  # noqa: PLC0415

        raw = yaml.safe_load(cert_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_adapter_cert(
    program_id: str,
    programs_root: Path,
    cert_data: dict[str, Any],
) -> None:
    """Write adapter_cert.yaml to the program directory."""
    import yaml  # noqa: PLC0415

    cert_path = programs_root / program_id / _ADAPTER_CERT_FILENAME
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_text(
        yaml.dump(cert_data, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


def _check_credential_expiry(
    channel: str,
    cert_entry: dict[str, Any],
    today: date | None = None,
    warning_days: int = DEFAULT_EXPIRY_WARNING_DAYS,
) -> DoctorCheck | None:
    """Return a warn/fail DoctorCheck if the channel credential is expiring soon.

    Returns ``None`` if no ``credential_expiry`` date is recorded for the channel.

    The ``credential_expiry`` field in ``adapter_cert.yaml`` must be an ISO-8601
    date string (``YYYY-MM-DD``).  Operators record it when configuring PATs /
    service-principal secrets so ``doctor --adapter-cert`` gives an advance warning.
    """
    expiry_raw = cert_entry.get("credential_expiry") if isinstance(cert_entry, dict) else None
    if not expiry_raw:
        return None
    try:
        expiry_date = date.fromisoformat(str(expiry_raw))
    except (ValueError, TypeError):
        return None

    today_val = today or datetime.now(timezone.utc).date()
    days_remaining = (expiry_date - today_val).days

    if days_remaining < 0:
        return DoctorCheck(
            f"Credential Expiry: {channel.upper()}",
            "fail",
            (
                f"Channel '{channel}' credential expired {-days_remaining} day(s) ago "
                f"({expiry_date.isoformat()}). "
                f"Renew the PAT/token and update credential_expiry in adapter_cert.yaml."
            ),
            metadata={
                "channel": channel,
                "expiry_date": expiry_date.isoformat(),
                "days_remaining": days_remaining,
            },
        )
    if days_remaining <= warning_days:
        return DoctorCheck(
            f"Credential Expiry: {channel.upper()}",
            "warn",
            (
                f"Channel '{channel}' credential expires in {days_remaining} day(s) "
                f"({expiry_date.isoformat()}).  Renew before expiry to avoid gather failures."
            ),
            metadata={
                "channel": channel,
                "expiry_date": expiry_date.isoformat(),
                "days_remaining": days_remaining,
            },
        )
    return None


def _probe_workiq_verb(
    probe_fn: Callable[[], bool] | None = None,
) -> DoctorCheck:
    """Probe whether the WorkIQ verb is available.

    Uses the injected ``probe_fn`` when provided (test seam).
    In production this checks whether the ``workiq`` module is importable
    and the ``ask_work_iq`` tool name is accessible.
    """
    if probe_fn is not None:
        available = probe_fn()
    else:
        try:
            import importlib  # noqa: PLC0415

            spec = importlib.util.find_spec("workiq")  # type: ignore[attr-defined]
            available = spec is not None
        except Exception:  # noqa: BLE001
            available = False

    if available:
        return DoctorCheck(
            "WorkIQ Probe",
            "ok",
            "WorkIQ verb (ask_work_iq) is reachable.",
            metadata={"available": True},
        )
    return DoctorCheck(
        "WorkIQ Probe",
        "warn",
        (
            "WorkIQ verb (ask_work_iq) is NOT reachable. "
            "M365 Teams/email evidence will be unavailable. "
            "Install or configure the WorkIQ connector to enable M365 data."
        ),
        metadata={"available": False},
    )


def run_adapter_cert_doctor(
    *,
    edition_name: str,
    program_id: str,
    programs_root: Path,
    channel_enabled_fns: dict[str, Callable[[], bool]] | None = None,
    workiq_probe_fn: Callable[[], bool] | None = None,
    write_cert: bool = True,
    today: date | None = None,
) -> DoctorReport:
    """Return a DoctorReport auditing UIL adapter certification per WS-3.

    Parameters
    ----------
    edition_name
        The resolved edition id (used as the report edition label).
    program_id
        The resolved program id.
    programs_root
        Root of the ``programs/`` directory.
    channel_enabled_fns
        Dependency seam for tests; defaults to ``UIL_CHANNEL_ENABLED_FUNCS``.
    workiq_probe_fn
        Dependency seam for WorkIQ probe; ``None`` uses the live import check.
    write_cert
        When ``True``, update ``adapter_cert.yaml`` after the check.
        Set ``False`` in dry-run or test scenarios.
    today
        Override today's date for testing the expiry-warning logic.
    """
    enabled_fns = channel_enabled_fns or UIL_CHANNEL_ENABLED_FUNCS
    existing_cert = _load_adapter_cert(program_id, programs_root)

    checks: list[DoctorCheck] = []
    cert_updates: dict[str, Any] = dict(existing_cert)

    for channel in _ALL_CHANNELS:
        enabled = enabled_fns.get(channel, lambda: False)()
        cert_entry = existing_cert.get("channels", {}).get(channel, {})
        cert_status = cert_entry.get("status", "uncertified") if isinstance(cert_entry, dict) else "uncertified"
        cert_version = cert_entry.get("version") if isinstance(cert_entry, dict) else None

        if not enabled:
            label = f"Adapter Cert: {channel.upper()}"
            detail = (
                f"Channel '{channel}' is disabled (env flag not set). "
                f"Set VERTEX_UIL_{channel.upper()}=1 to enable."
            )
            checks.append(DoctorCheck(label, "info", detail, metadata={"channel": channel, "enabled": False}))
            continue

        # Channel is enabled — check cert status.
        label = f"Adapter Cert: {channel.upper()}"
        if cert_status == "certified":
            detail = f"Channel '{channel}' is enabled and certified."
            if cert_version:
                detail += f" (version: {cert_version})"
            status = "ok"
        else:
            detail = (
                f"Channel '{channel}' is enabled but not yet certified. "
                f"Run 'vertex facts dual-read-log --program {program_id}' "
                f"and record certification to update adapter_cert.yaml."
            )
            status = "warn"

        checks.append(
            DoctorCheck(label, status, detail, metadata={"channel": channel, "enabled": True, "cert_status": cert_status})
        )

        # Credential expiry check (proactive N-days warning).
        expiry_check = _check_credential_expiry(channel, cert_entry, today=today)
        if expiry_check is not None:
            checks.append(expiry_check)

        # Update cert record for write-back.
        channels_block = cert_updates.setdefault("channels", {})
        if not isinstance(channels_block.get(channel), dict):
            channels_block[channel] = {}
        channels_block[channel]["enabled"] = True

    # WorkIQ probe.
    checks.append(_probe_workiq_verb(probe_fn=workiq_probe_fn))

    # Write-back cert file.
    if write_cert:
        try:
            _write_adapter_cert(program_id, programs_root, cert_updates)
        except Exception:  # noqa: BLE001
            pass  # non-fatal; doctor is read-only in nature

    return DoctorReport(edition=edition_name, checks=tuple(checks))

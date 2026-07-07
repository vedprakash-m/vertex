from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import yaml

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.exceptions import ConfigError


_DEFAULT_LABELS = {
    "ado_activation": "ADO activation",
    "kusto_activation": "Kusto activation",
    "m365_activation": "M365 activation",
    "graph_app_only_auth": "Graph app-only auth",
}
_ALLOWED_STATUSES = {"complete", "deferred", "in_progress", "unavailable", "unknown"}


@dataclass(frozen=True, slots=True)
class ProgramCapabilityStatus:
    capability_id: str
    label: str
    status: str
    summary: str
    degradation: str | None = None
    last_reviewed_on: date | None = None

    @property
    def detail(self) -> str:
        if self.degradation:
            return f"{self.summary} {self.degradation}"
        return self.summary

    def to_payload(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "label": self.label,
            "status": self.status,
            "summary": self.summary,
            "degradation": self.degradation,
            "last_reviewed_on": self.last_reviewed_on.isoformat() if self.last_reviewed_on is not None else None,
        }


def load_program_capability_status(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    program_document: Mapping[str, Any] | None = None,
) -> tuple[ProgramCapabilityStatus, ...]:
    program_dir = programs_root / program_id
    raw_program = dict(program_document) if program_document is not None else _load_program_document(program_dir)
    statuses: dict[str, ProgramCapabilityStatus] = {
        "kusto_activation": _default_kusto_activation_status(raw_program),
        "m365_activation": _default_m365_activation_status(raw_program),
        "graph_app_only_auth": ProgramCapabilityStatus(
            capability_id="graph_app_only_auth",
            label=_DEFAULT_LABELS["graph_app_only_auth"],
            status="unavailable",
            summary="No persisted Graph app-only auth completion signal is available yet.",
            degradation="Graph-backed status sources and L2 governance graduation remain unavailable.",
        ),
    }

    status_path = program_dir / "capability_status.yaml"
    if status_path.exists():
        document = yaml.safe_load(status_path.read_text(encoding="utf-8")) or {}
        raw_capabilities = document.get("capabilities")
        if raw_capabilities is None:
            raw_capabilities = ()
        if not isinstance(raw_capabilities, list):
            raise ConfigError(f"Invalid capability status file for '{program_id}': capabilities must be a list.")
        for raw_entry in raw_capabilities:
            entry = _capability_status_from_document(raw_entry, program_id=program_id)
            statuses[entry.capability_id] = entry

    ordered_ids = tuple(_DEFAULT_LABELS) + tuple(sorted(capability_id for capability_id in statuses if capability_id not in _DEFAULT_LABELS))
    return tuple(statuses[capability_id] for capability_id in ordered_ids if capability_id in statuses)


def find_program_capability_status(
    statuses: tuple[ProgramCapabilityStatus, ...],
    capability_id: str,
) -> ProgramCapabilityStatus | None:
    return next((status for status in statuses if status.capability_id == capability_id), None)


def summarize_program_capabilities(statuses: tuple[ProgramCapabilityStatus, ...]) -> str | None:
    visible = tuple(f"{status.label} {status.status.replace('_', ' ')}" for status in statuses)
    return "; ".join(visible) if visible else None


def latest_program_capability_reviewed_on(statuses: tuple[ProgramCapabilityStatus, ...]) -> date | None:
    reviewed_dates = tuple(
        status.last_reviewed_on
        for status in statuses
        if status.last_reviewed_on is not None
    )
    return max(reviewed_dates) if reviewed_dates else None


def summarize_program_capability_reviews(statuses: tuple[ProgramCapabilityStatus, ...]) -> str | None:
    if not statuses:
        return None
    latest_reviewed_on = latest_program_capability_reviewed_on(statuses)
    missing_review_labels = tuple(
        status.label
        for status in statuses
        if status.last_reviewed_on is None and status.status != "unavailable"
    )
    if latest_reviewed_on is None:
        if not missing_review_labels:
            return None
        return f"missing review dates: {', '.join(missing_review_labels)}"
    if not missing_review_labels:
        return f"latest {latest_reviewed_on.isoformat()}"
    return f"latest {latest_reviewed_on.isoformat()}; missing review dates: {', '.join(missing_review_labels)}"


def summarize_program_capability_verification(statuses: tuple[ProgramCapabilityStatus, ...]) -> str | None:
    pending_labels = tuple(
        status.label
        for status in statuses
        if status.status in {"deferred", "in_progress", "unknown"}
    )
    if not pending_labels:
        return None
    return f"live verification pending: {', '.join(pending_labels)}"


def _default_m365_activation_status(program_document: Mapping[str, Any]) -> ProgramCapabilityStatus:
    raw_m365 = program_document.get("m365")
    enabled = False
    if isinstance(raw_m365, Mapping):
        enabled = bool(raw_m365.get("enabled"))
    if enabled:
        return ProgramCapabilityStatus(
            capability_id="m365_activation",
            label=_DEFAULT_LABELS["m365_activation"],
            status="complete",
            summary="M365 is enabled in program config.",
        )
    return ProgramCapabilityStatus(
        capability_id="m365_activation",
        label=_DEFAULT_LABELS["m365_activation"],
        status="unavailable",
        summary="M365 is disabled in program config.",
        degradation="WorkIQ enrichment, leakage detection, and Graph-backed delivery loops remain inactive.",
    )


def _default_kusto_activation_status(program_document: Mapping[str, Any]) -> ProgramCapabilityStatus:
    raw_kusto = program_document.get("kusto")
    enabled = False
    if isinstance(raw_kusto, Mapping):
        enabled = bool(raw_kusto.get("enabled"))
    if enabled:
        return ProgramCapabilityStatus(
            capability_id="kusto_activation",
            label=_DEFAULT_LABELS["kusto_activation"],
            status="complete",
            summary="Kusto is enabled in program config.",
        )
    return ProgramCapabilityStatus(
        capability_id="kusto_activation",
        label=_DEFAULT_LABELS["kusto_activation"],
        status="unavailable",
        summary="Kusto is disabled in program config.",
        degradation="Telemetry validation and IcM-via-Kusto queries remain inactive.",
    )


def _load_program_document(program_dir: Path) -> dict[str, Any]:
    program_path = program_dir / "program.yaml"
    if not program_path.exists():
        raise ConfigError(f"Program config was not found at '{program_path}'.")
    document = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise ConfigError(f"Program config at '{program_path}' must be a mapping.")
    return document


def _capability_status_from_document(raw_entry: object, *, program_id: str) -> ProgramCapabilityStatus:
    if not isinstance(raw_entry, Mapping):
        raise ConfigError(f"Invalid capability status entry for '{program_id}': each entry must be a mapping.")

    capability_id = _required_string(raw_entry.get("id"), field_name="id").strip()
    if not capability_id:
        raise ConfigError(f"Invalid capability status entry for '{program_id}': missing id.")

    status = _required_string(raw_entry.get("status"), field_name="status").strip().lower()
    if status not in _ALLOWED_STATUSES:
        raise ConfigError(
            f"Invalid capability status '{status}' for '{program_id}' capability '{capability_id}'. "
            f"Expected one of: {', '.join(sorted(_ALLOWED_STATUSES))}."
        )

    summary = _required_string(raw_entry.get("summary"), field_name="summary").strip()
    if not summary:
        raise ConfigError(f"Invalid capability status entry for '{program_id}' capability '{capability_id}': missing summary.")

    raw_label = raw_entry.get("label")
    if raw_label is None:
        label = (_DEFAULT_LABELS.get(capability_id) or capability_id.replace("_", " ").title()).strip()
    else:
        label = _required_string(raw_label, field_name="label").strip()
    raw_degradation = raw_entry.get("degradation")
    degradation = _optional_string(raw_degradation, field_name="degradation")
    if degradation is not None:
        degradation = degradation.strip() or None
    last_reviewed_on = _parse_optional_date(raw_entry.get("last_reviewed_on"), program_id=program_id, capability_id=capability_id)
    return ProgramCapabilityStatus(
        capability_id=capability_id,
        label=label,
        status=status,
        summary=summary,
        degradation=degradation,
        last_reviewed_on=last_reviewed_on,
    )


def _parse_optional_date(value: object, *, program_id: str, capability_id: str) -> date | None:
    if value in {None, ""}:
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ConfigError(
            f"Invalid last_reviewed_on for '{program_id}' capability '{capability_id}': {value!r}. Expected YYYY-MM-DD."
        )
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ConfigError(
            f"Invalid last_reviewed_on for '{program_id}' capability '{capability_id}': {value!r}. Expected YYYY-MM-DD."
        ) from error


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"Invalid capability status entry: {field_name} must be a string.")
    return value


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"Invalid capability status entry: {field_name} must be a string.")
    return value
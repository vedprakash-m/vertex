from __future__ import annotations

import hashlib
from typing import Any

from src.core.models_v2 import IntegrationError, Signal


WORKIQ_INVALID_RECORD_OPERATOR_ACTION = (
    "Check record format, remove terminal controls and verify permitted mail URLs before retrying."
)
WORKIQ_PLAN_BUDGET_MESSAGE = "WorkIQ query-plan budget exhausted; at least one plan was not attempted."
WORKIQ_PLAN_BUDGET_OPERATOR_ACTION = "Review the WorkIQ query-plan time budget and retry."


def is_workiq_catchup_diagnostic(signal: Signal) -> bool:
    metadata = signal.metadata or {}
    return (
        (signal.source == "system" or (
            signal.source == "vertex/catchup" and metadata.get("catchup_origin") == "system"
        ))
        and metadata.get("integration_source") == "workiq"
        and (
            metadata.get("quarantined") is True
            or metadata.get("workiq_structured_response_unusable") is True
            or metadata.get("workiq_plan_budget_exhausted") is True
        )
    )


def workiq_catchup_diagnostic_identity(signal: Signal) -> str:
    metadata = signal.metadata or {}
    error = metadata.get("error")
    value = error.strip() if isinstance(error, str) and error.strip() else signal.id
    category = (
        "quarantine" if metadata.get("quarantined") is True
        else "budget" if metadata.get("workiq_plan_budget_exhausted") is True
        else "discovery"
    )
    return hashlib.sha256(f"{category}\x1f{value}".encode("utf-8", errors="surrogatepass")).hexdigest()


def workiq_catchup_diagnostic_summary(signal: Signal) -> str:
    metadata = signal.metadata or {}
    if metadata.get("quarantined") is not True:
        if metadata.get("workiq_plan_budget_exhausted") is True:
            return "WorkIQ: query-plan time budget exhausted; at least one plan was not attempted."
        return "WorkIQ: structured discovery response unavailable or invalid."

    count = metadata.get("quarantined_record_count")
    description = (
        f"{count} inspected record occurrence(s)"
        if isinstance(count, int) and not isinstance(count, bool) and count > 0 else "records"
    )
    error = metadata.get("error")
    reason = ""
    if metadata.get("workiq_structured_record_invalid") is True:
        reason = " for malformed or unsafe content"
    elif isinstance(error, str):
        if error.endswith("for missing or invalid source time."):
            reason = " for missing or invalid source time"
        elif error.endswith("for missing or invalid source identity."):
            reason = " for missing or invalid source identity"
    return f"WorkIQ: {description} quarantined{reason}."


def integration_operator_action(source: str) -> str | None:
    normalized_source = source.strip().lower()
    if normalized_source == "ado":
        return "Verify Azure DevOps access and saved-query configuration before retrying gather."
    if normalized_source == "kusto":
        return "Run 'vertex admin auth setup' and verify Kusto cluster access before retrying gather."
    if normalized_source == "workiq":
        return "Verify Agency/WorkIQ credentials and CLI availability before retrying gather."
    if normalized_source == "icm":
        return "Verify IcM or backing Kusto access before retrying gather."
    return None


def build_integration_error(
    *, source: str, stage: str, error: str, retryable: bool = True,
    operator_action: str | None = None,
) -> IntegrationError:
    normalized_error = error.strip() or "Unknown integration error"
    return IntegrationError(
        source=source,
        stage=stage,
        retryable=retryable,
        message=normalized_error,
        operator_action=operator_action if operator_action is not None else integration_operator_action(source),
    )


def append_integration_error_once(
    sink: list[IntegrationError] | None,
    *,
    source: str,
    stage: str,
    error: str,
    retryable: bool = True,
    operator_action: str | None = None,
) -> bool:
    if sink is None:
        return False
    detail = build_integration_error(
        source=source, stage=stage, error=error, retryable=retryable,
        operator_action=operator_action,
    )
    identity = (detail.source.strip().lower(), detail.stage.strip().lower(), detail.message)
    for existing in sink:
        if (existing.source.strip().lower(), existing.stage.strip().lower(), existing.message) == identity:
            return False
    sink.append(detail)
    return True


def config_provider_instance_id(config: Any) -> str | None:
    extra = getattr(config, "extra", None)
    if not isinstance(extra, dict):
        return None
    raw_value = extra.get("instance_id")
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    return value or None


def binding_provider_instance_id(binding: Any) -> str | None:
    return config_provider_instance_id(getattr(binding, "config", binding))

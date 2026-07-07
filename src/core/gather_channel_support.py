from __future__ import annotations

from typing import Any

from src.core.models_v2 import IntegrationError


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


def build_integration_error(*, source: str, stage: str, error: str) -> IntegrationError:
    normalized_error = error.strip() or "Unknown integration error"
    return IntegrationError(
        source=source,
        stage=stage,
        retryable=True,
        message=normalized_error,
        operator_action=integration_operator_action(source),
    )


def append_integration_error_once(
    sink: list[IntegrationError] | None,
    *,
    source: str,
    stage: str,
    error: str,
) -> bool:
    if sink is None:
        return False
    detail = build_integration_error(source=source, stage=stage, error=error)
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

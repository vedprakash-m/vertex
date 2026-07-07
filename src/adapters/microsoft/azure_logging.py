from __future__ import annotations

import logging


_AZURE_LOGGER_NAMES = (
    "azure.identity",
    "azure.core.pipeline.policies.http_logging_policy",
)


def quiet_azure_sdk_logging() -> None:
    """Suppress noisy Azure SDK auth tracebacks in expected auth-blocked flows."""
    for logger_name in _AZURE_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(logging.CRITICAL)

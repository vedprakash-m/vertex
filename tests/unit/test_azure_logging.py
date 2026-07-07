from __future__ import annotations

import logging

from src.adapters.microsoft.azure_logging import quiet_azure_sdk_logging


def test_quiet_azure_sdk_logging_raises_azure_logger_thresholds() -> None:
    identity_logger = logging.getLogger("azure.identity")
    http_logger = logging.getLogger("azure.core.pipeline.policies.http_logging_policy")
    identity_logger.setLevel(logging.NOTSET)
    http_logger.setLevel(logging.INFO)

    quiet_azure_sdk_logging()

    assert identity_logger.level == logging.CRITICAL
    assert http_logger.level == logging.CRITICAL

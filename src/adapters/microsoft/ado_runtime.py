from __future__ import annotations

import functools
from typing import Any

from src.adapters.microsoft.azure_logging import quiet_azure_sdk_logging

_CLI_PROCESS_TIMEOUT = 120  # az account get-access-token is slow (60s+) for ADO resource on this machine


def load_ado_credential_types() -> tuple[bool, tuple[Any, ...]]:
    try:
        quiet_azure_sdk_logging()
        from azure.identity import AzureCliCredential, DefaultAzureCredential
    except ImportError:
        return False, ()
    return True, (
        functools.partial(AzureCliCredential, process_timeout=_CLI_PROCESS_TIMEOUT),
        DefaultAzureCredential,
    )

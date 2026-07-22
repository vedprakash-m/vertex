from __future__ import annotations

from typing import Any

from src.core.exceptions import QueryError
from src.adapters.microsoft.azure_logging import quiet_azure_sdk_logging


def load_kusto_sdk_types() -> tuple[Any, Any, Any, Any]:
    try:
        quiet_azure_sdk_logging()
        from azure.identity import DefaultAzureCredential
        from azure.kusto.data import ClientRequestProperties
        from azure.kusto.data import KustoClient as AzureKustoClient
        from azure.kusto.data import KustoConnectionStringBuilder
    except ImportError as error:  # pragma: no cover - depends on optional packages
        raise QueryError(
            'Kusto support requires optional dependencies. Run: pip install -e ".[kusto]"'
        ) from error
    return AzureKustoClient, KustoConnectionStringBuilder, ClientRequestProperties, DefaultAzureCredential

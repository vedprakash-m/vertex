"""FR-SG-48: ExternalConnectorConfig — standalone to avoid circular imports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ConnectorType = Literal["github_issues", "sharepoint_lists"]


@dataclass(frozen=True, slots=True)
class ExternalConnectorConfig:
    """Configuration for one external connector entry in slice_contracts.yaml."""

    dep_id: str
    connector_type: ConnectorType
    source_url: str
    team: str
    gates: tuple[str, ...] = ()
    auth_token: str | None = None

"""Provider facade for the gather coordinator (Phase 7 / D-04).

The gather orchestrator must not import concrete provider/transport classes
(``ADOClient``, ``GraphMailClient``, ``GraphCalendarClient``, …) directly from
``src.core.*`` / ``src.m365.*``. It reaches them only through this facade so
that:

* there is a single seam where transports can be swapped (e.g. monkeypatched in
  tests) without touching the ~5k-LOC coordinator, and
* the Source-of-Record-flip work can substitute providers/factories without the
  coordinator importing transport classes by name.

This module re-exports the transport types (for annotations and factory
injection) and exposes thin construction helpers. The architecture-fitness
contract ``test_no_provider_leakage_in_gather_coordinator`` enforces that
``gather.py`` imports providers only through this facade.
"""

from __future__ import annotations

from typing import Any

from src.core.ado_client import ADOClient as ADOClient
from src.m365.graph_calendar_client import GraphCalendarClient as GraphCalendarClient
from src.m365.graph_mail_client import GraphMailClient as GraphMailClient

__all__ = [
    "ADOClient",
    "GraphCalendarClient",
    "GraphMailClient",
    "create_graph_calendar_client",
    "create_graph_mail_client",
]


def create_graph_calendar_client(bridge_client: Any) -> GraphCalendarClient:
    """Construct a Graph calendar client at the single coordinator seam."""
    return GraphCalendarClient(bridge_client)


def create_graph_mail_client(bridge_client: Any) -> GraphMailClient:
    """Construct a Graph mail client at the single coordinator seam."""
    return GraphMailClient(bridge_client)

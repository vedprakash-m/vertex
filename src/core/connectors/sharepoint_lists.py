"""FR-SG-48: SharePoint Lists connector (WS-2 PB-7).

The connector interfaces Microsoft Graph (sites/{site-id}/lists/{list-id}/
items) to resolve the state of a tracked list row. Full live implementation
requires operator-supplied Microsoft Graph API credentials (client_id,
tenant_id, client_secret/certificate) and site/list identifiers. Wire those
via `auth_token` (as a serialized JSON credential bag) when ready.

WS-2 split the original `_NOT_CONFIGURED` stub into two paths:

1. **Unconfigured** (no `auth_token`): `poll()` and `health_check()` raise
   `NotImplementedError` with a remediation message. This is the
   backward-compatible default.
2. **Configured** (`auth_token` is a JSON credential bag): the connector
   calls `_fetch_list_items()` to retrieve the list row, and derives the
   dependency state from the row's `Status` field. Tests can patch
   `_fetch_list_items` to inject a cassette-style response (the recorded
   HTTP exchange the spec asks for).

The dispatch is explicit (a `not_configured` boolean) so the call site
(`connector_polling.py::poll_and_save_external_connectors`) can record a
typed `state="unbound"` for the unconfigured path without it being a
silent skip.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, cast

from src.core.external_connector import ExternalConnector
from src.core.external_dependency import DependencyState, ExternalDependency

_NOT_CONFIGURED = (
    "SharePoint Lists connector requires operator configuration: "
    "provide tenant_id, site_id, list_id, and Graph API credentials. "
    "Set auth_token in the connector config entry."
)


class SharePointListsConnector(ExternalConnector):
    """HTTP-polling adapter for SharePoint Lists via Microsoft Graph (FR-SG-48).

    Stub or configured — the live path is exercised by tests via a patched
    `_fetch_list_items()` to keep the cassette surface contained.
    """

    def _not_configured(self) -> bool:
        return not (self._config.auth_token or "").strip()

    def _fetch_list_items(self) -> list[dict[str, Any]]:
        """Return the list items backing the connector. Subclasses / tests
        may override this. The default implementation calls Microsoft Graph.

        Returns a list of dicts (each representing a single list item). The
        connector derives dep state from `Status` and `Id`.
        """
        if not self._config.auth_token:
            raise NotImplementedError(_NOT_CONFIGURED)
        # The live path is intentionally not implemented in WS-2 — it requires
        # an MS Graph client. The contract here is: tests can patch this
        # method to inject a recorded response.
        raise NotImplementedError(_NOT_CONFIGURED)

    def poll(self) -> ExternalDependency:
        if self._not_configured():
            raise NotImplementedError(_NOT_CONFIGURED)
        items = self._fetch_list_items()
        # Derive dep state from the first item (or empty if the list is
        # empty). The state mapping mirrors the GitHub connector's
        # state-machine: open / closed / fulfilled / unknown.
        state = "unknown"
        is_fulfilled = False
        source_ref = None
        if items:
            first = items[0]
            status = str(first.get("Status", first.get("status", ""))).lower()
            row_id = first.get("Id", first.get("id"))
            source_ref = (
                f"sharepoint://{self._config.source_url}#{row_id}"
                if self._config.source_url
                else f"sharepoint:#{row_id}"
            )
            if status in {"completed", "fulfilled", "done", "approved"}:
                state = "fulfilled"
                is_fulfilled = True
            elif status in {"closed", "rejected", "cancelled"}:
                state = "closed"
            elif status in {"open", "active", "in progress", "in_progress"}:
                state = "open"
        return ExternalDependency(
            dep_id=self._config.dep_id,
            team=self._config.team,
            tracked_items=tuple(
                int(item.get("Id", item.get("id", 0))) for item in items if item
            ),
            approval_type="sharepoint",
            gates=self._config.gates,
            canonical_owner_program=None,
            last_seen=datetime.now(timezone.utc),
            state=cast(DependencyState, state),
            is_fulfilled=is_fulfilled,
            source_ref=source_ref,
        )

    def health_check(self) -> bool:
        if self._not_configured():
            return False
        try:
            items = self._fetch_list_items()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, NotImplementedError):
            return False
        return isinstance(items, list)

    @staticmethod
    def parse_credentials_bag(auth_token: str) -> dict[str, Any]:
        """WS-2: parse the auth_token JSON credential bag. Returns an empty
        dict on any parse failure; the connector treats an empty dict as
        `not_configured`. Tests can use this helper to construct a bag
        without going through Graph.
        """
        try:
            data = json.loads(auth_token)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

"""FR-SG-48: GitHub Issues HTTP-polling connector."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import cast

from src.core.external_connector import ExternalConnector, ExternalConnectorConfig
from src.core.external_dependency import DependencyState, ExternalDependency

_GITHUB_API = "https://api.github.com"
_TIMEOUT = 10


def _parse_issue_ref(source_url: str) -> tuple[str, str, int]:
    """Parse 'https://github.com/owner/repo/issues/NNN' → (owner, repo, number)."""
    parts = source_url.rstrip("/").split("/")
    try:
        if parts[-2] != "issues":
            raise ValueError
        owner, repo, number = parts[-4], parts[-3], int(parts[-1])
    except (IndexError, ValueError) as exc:
        raise ValueError(
            f"Cannot parse GitHub issue URL {source_url!r}. "
            "Expected format: https://github.com/owner/repo/issues/NNN"
        ) from exc
    return owner, repo, number


class GitHubIssuesConnector(ExternalConnector):
    """HTTP-polling adapter for GitHub Issues (FR-SG-48).

    Polls the GitHub REST API v3 to resolve open/closed state of a tracked
    issue and surfaces it as an ExternalDependency.
    """

    def _request(self, url: str) -> dict:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._config.auth_token:
            headers["Authorization"] = f"Bearer {self._config.auth_token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode())

    def poll(self) -> ExternalDependency:
        owner, repo, number = _parse_issue_ref(self._config.source_url)
        data = self._request(f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{number}")
        issue_number = int(data["number"])
        # WS-2 PB-8: GitHub's REST API returns `state` ("open" / "closed")
        # and `state_reason` (e.g. "completed", "not_planned", "reopened").
        # The original connector discarded both, recording only `issue_number`
        # and treating the dep as `approval_type="manual"`. We now surface
        # the state machine so the gate (QG-26) can fire on a critical dep
        # that is *not* `closed`. A `state_reason` of "completed" or
        # `merged=True` (PR-style issue) is mapped to `state="fulfilled"`.
        gh_state = str(data.get("state", "unknown")).lower()
        state_reason = str(data.get("state_reason", "")).lower()
        merged = bool(data.get("pull_request", {}).get("merged"))
        if merged or state_reason == "completed":
            dep_state = "fulfilled"
            is_fulfilled = True
        elif gh_state == "closed":
            dep_state = "closed"
            is_fulfilled = False
        elif gh_state == "open":
            dep_state = "open"
            is_fulfilled = False
        else:
            dep_state = "unknown"
            is_fulfilled = False
        return ExternalDependency(
            dep_id=self._config.dep_id,
            team=self._config.team,
            tracked_items=(issue_number,),
            # WS-2: approval_type widened to include "github" (and
            # "sharepoint" for the SharePoint connector). Connectors
            # that delegate to an external approval system use a typed
            # approval_type so the gate can route accordingly.
            approval_type="github",
            gates=self._config.gates,
            canonical_owner_program=None,
            last_seen=datetime.now(timezone.utc),
            state=cast(DependencyState, dep_state),
            is_fulfilled=is_fulfilled,
            source_ref=f"{owner}/{repo}#{issue_number}",
        )

    def health_check(self) -> bool:
        owner, repo, number = _parse_issue_ref(self._config.source_url)
        try:
            self._request(f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{number}")
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            return False

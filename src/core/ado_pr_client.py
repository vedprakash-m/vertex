from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from src.core.ado_client import ADOClient


log = logging.getLogger(__name__)


def _parse_ado_datetime(value: Any) -> datetime | None:
    """Parse an ADO ISO-8601 timestamp, returning None when absent or malformed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class PullRequestSummary:
    pr_id: int
    title: str
    status: str                    # "active" | "completed" | "abandoned"
    created_by: str
    target_ref: str                # e.g. "refs/heads/main"
    source_ref: str                # e.g. "refs/heads/feature/aggregate-dir"
    url: str
    created_at: datetime
    merged_at: datetime | None
    repository_id: str
    workstream_ids: tuple[str, ...] = ()


class ADOPRClient:
    def __init__(self, client: ADOClient) -> None:
        self._client = client

    def list_pull_requests(
        self,
        repository_id: str,
        status: str | None = None,
        top: int = 100,
    ) -> tuple[PullRequestSummary, ...]:
        url = f"https://dev.azure.com/{self._client.organization}/{self._client.project}/_apis/git/repositories/{repository_id}/pullrequests?api-version=7.1"
        params: dict[str, str] = {}
        if status is not None:
            params["searchCriteria.status"] = status
        params["$top"] = str(top)

        response = self._client._request_json("GET", url, params=params)
        value = response.get("value", [])

        summaries = []
        for item in value:
            if not isinstance(item, dict):
                log.warning("Skipping malformed PR entry (not an object) in repo %s", repository_id)
                continue

            # Required fields. A PR missing any of these is unusable downstream (it would
            # crash signal extraction or mis-key the work item), so skip it rather than
            # raising a KeyError that aborts the entire batch.
            pr_id_raw = item.get("pullRequestId")
            title = item.get("title")
            status_value = item.get("status")
            target_ref = item.get("targetRefName")
            source_ref = item.get("sourceRefName")
            if pr_id_raw is None or title is None or status_value is None or target_ref is None or source_ref is None:
                log.warning(
                    "Skipping PR with missing required fields in repo %s (id=%r)",
                    repository_id,
                    pr_id_raw,
                )
                continue
            try:
                pr_id = int(pr_id_raw)
            except (TypeError, ValueError):
                log.warning("Skipping PR with non-numeric id %r in repo %s", pr_id_raw, repository_id)
                continue

            # creationDate drives the PR signal timestamp. Do not fabricate a value with
            # datetime.now() when it is absent/unparseable — that would silently mis-date the
            # PR as "just created". Skip the entry so callers never see a fabricated timestamp.
            created_at = _parse_ado_datetime(item.get("creationDate"))
            if created_at is None:
                log.warning(
                    "Skipping PR %s in repo %s: missing or unparseable creationDate %r",
                    pr_id,
                    repository_id,
                    item.get("creationDate"),
                )
                continue

            created_by_info = item.get("createdBy") or {}
            if not isinstance(created_by_info, dict):
                created_by_info = {}
            created_by = created_by_info.get("displayName") or created_by_info.get("uniqueName") or "Unknown"

            merged_at = None
            if status_value == "completed":
                merged_at = _parse_ado_datetime(item.get("closedDate"))

            links = item.get("_links") or {}
            if not isinstance(links, dict):
                links = {}
            web_link = links.get("web") or {}
            if not isinstance(web_link, dict):
                web_link = {}
            web_url = web_link.get("href") or item.get("url") or ""

            summaries.append(
                PullRequestSummary(
                    pr_id=pr_id,
                    title=str(title),
                    status=str(status_value),
                    created_by=created_by,
                    target_ref=str(target_ref),
                    source_ref=str(source_ref),
                    url=web_url,
                    created_at=created_at,
                    merged_at=merged_at,
                    repository_id=repository_id,
                )
            )
        return tuple(summaries)

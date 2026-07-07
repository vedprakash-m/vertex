from __future__ import annotations

from typing import Any, Mapping

from src.core.exceptions import AuthError, QueryError


class TeamsWebhookClient:
    def __init__(self, *, webhook_url: str) -> None:
        normalized = webhook_url.strip()
        if not normalized:
            raise AuthError("Missing Teams incoming webhook URL.")
        if not normalized.lower().startswith("https://"):
            raise AuthError("Teams incoming webhook URL must use https.")
        self._webhook_url = normalized

    def post_card(self, payload: Mapping[str, Any]) -> None:
        requests_module = _get_requests_module()
        response = requests_module.post(
            self._webhook_url,
            headers={"Content-Type": "application/json"},
            json=dict(payload),
            timeout=30,
        )

        if response.status_code in {200, 202}:
            return
        if response.status_code in {401, 403}:
            raise AuthError(
                "Teams webhook delivery was rejected. Verify the incoming webhook URL is still valid for the target channel."
            )
        if response.status_code in {404, 410}:
            raise AuthError("Teams incoming webhook URL was not found. Recreate or update the configured webhook URL.")
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "?")
            raise QueryError(f"429 Too Many Requests from Teams incoming webhook delivery (Retry-After: {retry_after}s).")
        if response.status_code >= 500:
            raise QueryError(f"{response.status_code} Server Error from Teams incoming webhook delivery: {response.text[:200]}")
        raise QueryError(f"{response.status_code} Error from Teams incoming webhook delivery: {response.text[:200]}")


def _get_requests_module() -> Any:
    try:
        import requests
    except ImportError as error:
        raise QueryError("Teams incoming webhook delivery requires requests, which is missing from the current environment.") from error
    return requests
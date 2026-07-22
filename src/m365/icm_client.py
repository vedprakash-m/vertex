from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

from src.core.exceptions import AuthError, QueryError


_DEFAULT_TIMEOUT_SECONDS = 30


class IcmClient:
    """IcM incident client with lazy credential validation.

    Credentials are stored on construction but only validated when a live API
    call is made, mirroring the KustoClient pattern. This allows the client to
    be constructed safely in environments where IcM credentials are absent
    (doctor auth-probes, test fixtures, etc.) without raising on import.
    """

    def __init__(
        self,
        *,
        incidents_url: str | None = None,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        scope: str | None = None,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._incidents_url = (incidents_url or os.environ.get("ICM_INCIDENTS_URL", "")).strip()
        self._tenant_id = (tenant_id or os.environ.get("ICM_TENANT_ID", "")).strip()
        self._client_id = (client_id or os.environ.get("ICM_CLIENT_ID", "")).strip()
        self._client_secret = (client_secret or os.environ.get("ICM_CLIENT_SECRET", "")).strip()
        self._scope = (scope or os.environ.get("ICM_SCOPE") or _default_scope(self._incidents_url)).strip()
        self._timeout_seconds = timeout_seconds

    def _require_credentials(self) -> None:
        """Raise AuthError if any required credential is missing.

        Called at the start of each live API method so construction is always
        safe regardless of credential availability in the environment.
        """
        if not self._incidents_url:
            raise AuthError("Missing ICM_INCIDENTS_URL for direct IcM incident access.")
        if not self._tenant_id:
            raise AuthError("Missing ICM_TENANT_ID for direct IcM incident access.")
        if not self._client_id:
            raise AuthError("Missing ICM_CLIENT_ID for direct IcM incident access.")
        if not self._client_secret:
            raise AuthError("Missing ICM_CLIENT_SECRET for direct IcM incident access.")
        if not self._scope:
            raise AuthError("Missing ICM_SCOPE for direct IcM incident access.")

    def list_incidents(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_credentials()
        credential_class = _get_client_secret_credential_class()
        requests_module = _get_requests_module()

        credential = credential_class(
            tenant_id=self._tenant_id,
            client_id=self._client_id,
            client_secret=self._client_secret,
        )
        token = credential.get_token(self._scope).token
        response = requests_module.get(
            self._incidents_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            params=params,
            timeout=self._timeout_seconds,
        )

        if response.status_code == 401:
            raise AuthError("401 Unauthorized from direct IcM incident access. Verify tenant, app registration, and token scope.")
        if response.status_code == 403:
            raise AuthError("403 Forbidden from direct IcM incident access. Verify the service principal has IcM API access.")
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "?")
            raise QueryError(f"429 Too Many Requests from direct IcM incident access (Retry-After: {retry_after}s).")
        if response.status_code >= 500:
            raise QueryError(f"{response.status_code} Server Error from direct IcM incident access: {response.text[:200]}")
        if response.status_code >= 400:
            raise QueryError(f"{response.status_code} Error from direct IcM incident access: {response.text[:200]}")

        try:
            payload = response.json()
        except ValueError as error:
            raise QueryError("Direct IcM incident access returned a non-JSON payload.") from error

        if isinstance(payload, list):
            return {"items": payload}
        if isinstance(payload, dict):
            return payload
        raise QueryError("Direct IcM incident access returned an unsupported payload shape.")


def _default_scope(incidents_url: str) -> str:
    if not incidents_url:
        return ""
    parsed = urlsplit(incidents_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/.default"


def _get_client_secret_credential_class() -> Any:
    try:
        from azure.identity import ClientSecretCredential
    except ImportError as error:
        raise AuthError(
            'Direct IcM incident access requires azure-identity. Run: pip install -e "."'
        ) from error
    return ClientSecretCredential


def _get_requests_module() -> Any:
    try:
        import requests
    except ImportError as error:
        raise QueryError("Direct IcM incident access requires requests, which is missing from the current environment.") from error
    return requests

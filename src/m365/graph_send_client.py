from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src.core.exceptions import AuthError, QueryError


_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"


@dataclass(frozen=True, slots=True)
class GraphMailMessage:
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    html_body: str


class GraphSendClient:
    def __init__(self, *, tenant_id: str | None = None, client_id: str | None = None) -> None:
        self._tenant_id = (tenant_id or os.environ.get("GRAPH_TENANT_ID", "")).strip()
        self._client_id = (client_id or os.environ.get("GRAPH_CLIENT_ID", "")).strip()
        if not self._tenant_id:
            raise AuthError("Missing GRAPH_TENANT_ID for Graph mail send.")
        if not self._client_id:
            raise AuthError("Missing GRAPH_CLIENT_ID for Graph mail send.")

    def send_mail(self, message: GraphMailMessage, *, save_to_sent_items: bool = True) -> None:
        credential_class = _get_device_code_credential_class()
        requests_module = _get_requests_module()

        credential = credential_class(tenant_id=self._tenant_id, client_id=self._client_id)
        token = credential.get_token(_GRAPH_SCOPE).token
        response = requests_module.post(
            _SEND_MAIL_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "message": {
                    "subject": message.subject,
                    "body": {
                        "contentType": "HTML",
                        "content": message.html_body,
                    },
                    "toRecipients": [
                        {"emailAddress": {"address": address}}
                        for address in message.to
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": address}}
                        for address in message.cc
                    ],
                },
                "saveToSentItems": save_to_sent_items,
            },
            timeout=30,
        )

        if response.status_code in {200, 202}:
            return
        if response.status_code == 401:
            raise AuthError("401 Unauthorized from Graph mail send. Re-authenticate the device-code client and verify Mail.Send consent.")
        if response.status_code == 403:
            raise AuthError("403 Forbidden from Graph mail send. Verify delegated Mail.Send permission has been approved for the Graph app.")
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "?")
            raise QueryError(f"429 Too Many Requests from Graph mail send (Retry-After: {retry_after}s).")
        if response.status_code >= 500:
            raise QueryError(f"{response.status_code} Server Error from Graph mail send: {response.text[:200]}")
        raise QueryError(f"{response.status_code} Error from Graph mail send: {response.text[:200]}")


def _get_device_code_credential_class() -> Any:
    try:
        from azure.identity import DeviceCodeCredential
    except ImportError as error:
        raise AuthError(
            "Graph mail send requires azure-identity. Run: pip install -r requirements.txt"
        ) from error
    return DeviceCodeCredential


def _get_requests_module() -> Any:
    try:
        import requests
    except ImportError as error:
        raise QueryError("Graph mail send requires requests, which is missing from the current environment.") from error
    return requests
"""Armada D-13: read-only scheduled ADO credential boundary.

The secret stays in the OS keyring (Windows Credential Manager on the
operator host).  Callers receive it only long enough to construct a child
process environment; neither this module nor the scheduler launcher logs it.
"""
from __future__ import annotations

from typing import Protocol


ADO_SCHEDULED_KEYRING_SERVICE = "vertex.ado"
ARMADA_SCHEDULED_KEYRING_ACCOUNT = "armada"


class _KeyringLike(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...


def _keyring() -> _KeyringLike:
    try:
        import keyring  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("keyring>=25.3.0 is required for scheduled ADO credentials") from exc
    return keyring


def get_scheduled_ado_pat(*, account: str = ARMADA_SCHEDULED_KEYRING_ACCOUNT, keyring_module: _KeyringLike | None = None) -> str:
    """Return a configured PAT or a precise, secret-free operator error."""
    secret = (keyring_module or _keyring()).get_password(ADO_SCHEDULED_KEYRING_SERVICE, account)
    if secret is None or not secret.strip():
        raise RuntimeError(
            f"No scheduled ADO PAT is configured for keyring service '{ADO_SCHEDULED_KEYRING_SERVICE}' "
            f"account '{account}'. Configure the read-only credential before scheduling gather."
        )
    return secret.strip()


def set_scheduled_ado_pat(secret: str, *, account: str = ARMADA_SCHEDULED_KEYRING_ACCOUNT, keyring_module: _KeyringLike | None = None) -> None:
    if not secret or not secret.strip():
        raise ValueError("Scheduled ADO PAT must be non-empty.")
    (keyring_module or _keyring()).set_password(ADO_SCHEDULED_KEYRING_SERVICE, account, secret.strip())

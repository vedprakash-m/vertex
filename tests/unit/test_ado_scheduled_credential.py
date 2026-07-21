from __future__ import annotations

import pytest

from src.core.ado_scheduled_credential import (
    ADO_SCHEDULED_KEYRING_SERVICE,
    get_scheduled_ado_pat,
    set_scheduled_ado_pat,
)


class _Keyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password


def test_scheduled_pat_round_trip_is_keyring_backed_and_trimmed() -> None:
    keyring = _Keyring()
    set_scheduled_ado_pat("  secret  ", keyring_module=keyring)

    assert keyring.values[(ADO_SCHEDULED_KEYRING_SERVICE, "armada")] == "secret"
    assert get_scheduled_ado_pat(keyring_module=keyring) == "secret"


def test_scheduled_pat_missing_is_secret_free_operator_error() -> None:
    with pytest.raises(RuntimeError, match="No scheduled ADO PAT is configured"):
        get_scheduled_ado_pat(keyring_module=_Keyring())

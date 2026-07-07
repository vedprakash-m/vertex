"""WS-7: archive signing / verification tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.archive_signing import (
    SignatureRecord,
    get_archive_signing_key,
    load_signature_record,
    set_archive_signing_key,
    sign_manifest,
    verify_signature,
    write_signature_record,
)


class _StubKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password


def test_sign_and_verify_round_trip() -> None:
    payload = {"issue_number": 78, "edition": "acme_weekly", "snapshot_hash": "sha256:abc"}
    key = b"secret-123"
    record = sign_manifest(
        edition="acme_weekly",
        issue_number=78,
        manifest_payload=payload,
        key=key,
    )
    assert record.algorithm == "hmac-sha256"
    assert record.content_hash.startswith("sha256:")
    assert verify_signature(record, manifest_payload=payload, key=key) is True


def test_verify_fails_on_tampered_manifest() -> None:
    payload = {"issue_number": 78, "edition": "acme_weekly"}
    record = sign_manifest(
        edition="acme_weekly",
        issue_number=78,
        manifest_payload=payload,
        key=b"k",
    )
    tampered = dict(payload)
    tampered["edition"] = "fabrikam_weekly"
    assert verify_signature(record, manifest_payload=tampered, key=b"k") is False


def test_verify_fails_on_wrong_key() -> None:
    payload = {"issue_number": 78}
    record = sign_manifest(
        edition="acme_weekly", issue_number=78, manifest_payload=payload, key=b"k1"
    )
    assert verify_signature(record, manifest_payload=payload, key=b"k2") is False


def test_sidecar_round_trip(tmp_path: Path) -> None:
    payload = {"issue_number": 78, "edition": "acme_weekly"}
    record = sign_manifest(
        edition="acme_weekly",
        issue_number=78,
        manifest_payload=payload,
        key=b"k",
    )
    sidecar = tmp_path / "issue_078.sig.json"
    write_signature_record(sidecar, record)
    loaded = load_signature_record(sidecar)
    assert loaded is not None
    assert loaded.edition == "acme_weekly"
    assert loaded.issue_number == 78
    assert loaded.algorithm == "hmac-sha256"
    # Sidecar round-trips: verify the loaded record against the payload.
    assert verify_signature(loaded, manifest_payload=payload, key=b"k") is True


def test_sidecar_missing_returns_none(tmp_path: Path) -> None:
    assert load_signature_record(tmp_path / "missing.sig.json") is None


def test_sidecar_malformed_raises(tmp_path: Path) -> None:
    """WS-7: a sidecar that is not parseable as a JSON object raises.
    Either JSON decode failure or shape mismatch counts as malformed —
    we must NOT silently treat a tampered sidecar as a no-op."""
    bad = tmp_path / "bad.sig.json"
    bad.write_text("not-a-json-object", encoding="utf-8")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        load_signature_record(bad)


def test_keyring_helpers_use_stub() -> None:
    kr = _StubKeyring()
    set_archive_signing_key("secret-xyz", keyring_module=kr)
    assert get_archive_signing_key(keyring_module=kr) == b"secret-xyz"


def test_keyring_missing_key_returns_none() -> None:
    kr = _StubKeyring()
    assert get_archive_signing_key(keyring_module=kr) is None

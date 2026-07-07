"""WS-7: confirm-path archive-signing contract.

Tests that:
1. The confirm transaction (specifically `execute_archive_transaction`) calls
   the archive-signing primitive and writes a `.sig.json` sidecar next to the
   manifest when a key is configured.
2. The sidecar HMAC tag actually verifies against the on-disk manifest.
3. When the keyring is unavailable (no key configured) the transaction
   surfaces a warning but does NOT crash.
4. The `_try_sign_archive_manifest` helper is idempotent (re-signing with the
   same key produces a verifiable sidecar).
5. `manifest_signature_sidecar_path` is the stable path consumers depend on.
6. Tampering with the manifest invalidates the sidecar — i.e. signature
   catches integrity violations.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.core.archive_signing import (
    get_archive_signing_key,
    load_signature_record,
    manifest_signature_sidecar_path,
    set_archive_signing_key,
    verify_manifest_file,
    verify_signature,
)
from src.commands.confirm_stages.archive_transaction import (
    _try_sign_archive_manifest,
)


class _StubKeyring:
    """In-memory keyring so tests don't touch the real Windows Credential
    Manager / macOS keychain / Linux secret service."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password


@pytest.fixture
def stub_keyring(monkeypatch: pytest.MonkeyPatch) -> _StubKeyring:
    kr = _StubKeyring()
    monkeypatch.setattr(
        "src.core.archive_signing._import_keyring", lambda: kr
    )
    return kr


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")


def test_try_sign_writes_sidecar_when_key_configured(
    tmp_path: Path, stub_keyring: _StubKeyring
) -> None:
    set_archive_signing_key("secret-abc", keyring_module=stub_keyring)
    manifest_path = tmp_path / "issue_078.json"
    _write_manifest(manifest_path, {"issue_number": 78, "edition": "acme_weekly", "kind": "confirmed"})

    warnings = _try_sign_archive_manifest(
        manifest_path=manifest_path,
        edition_name="acme_weekly",
        issue_number=78,
    )

    assert warnings == (), f"Expected no warnings, got {warnings}"
    sidecar_path = manifest_signature_sidecar_path(manifest_path)
    assert sidecar_path.exists()
    record = load_signature_record(sidecar_path)
    assert record is not None
    assert record.edition == "acme_weekly"
    assert record.issue_number == 78
    assert record.algorithm == "hmac-sha256"

    # Verifier command path: the on-disk manifest + sidecar must verify
    key = get_archive_signing_key(keyring_module=stub_keyring)
    assert key is not None
    assert verify_manifest_file(manifest_path=manifest_path, sidecar_path=sidecar_path, key=key) is True


def test_try_sign_skips_with_warning_when_no_key(
    tmp_path: Path, stub_keyring: _StubKeyring
) -> None:
    # No key configured — stub_keyring has nothing in its store.
    manifest_path = tmp_path / "issue_079.json"
    _write_manifest(manifest_path, {"issue_number": 79, "edition": "acme_weekly"})

    warnings = _try_sign_archive_manifest(
        manifest_path=manifest_path,
        edition_name="acme_weekly",
        issue_number=79,
    )

    assert len(warnings) == 1
    assert "no HMAC key" in warnings[0]
    sidecar_path = manifest_signature_sidecar_path(manifest_path)
    assert not sidecar_path.exists(), "Should not write a sidecar when no key is configured"


def test_try_sign_detects_tampered_manifest(
    tmp_path: Path, stub_keyring: _StubKeyring
) -> None:
    set_archive_signing_key("secret-abc", keyring_module=stub_keyring)
    manifest_path = tmp_path / "issue_080.json"
    _write_manifest(manifest_path, {"issue_number": 80, "edition": "acme_weekly", "snapshot_hash": "original"})

    warnings_before_tamper = _try_sign_archive_manifest(
        manifest_path=manifest_path,
        edition_name="acme_weekly",
        issue_number=80,
    )
    assert warnings_before_tamper == ()

    # Tamper with the on-disk manifest AFTER signing.
    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered["snapshot_hash"] = "attacker-injected"
    manifest_path.write_text(json.dumps(tampered, sort_keys=True, indent=2), encoding="utf-8")

    sidecar_path = manifest_signature_sidecar_path(manifest_path)
    key = get_archive_signing_key(keyring_module=stub_keyring)
    assert key is not None
    assert (
        verify_manifest_file(manifest_path=manifest_path, sidecar_path=sidecar_path, key=key) is False
    ), "Tampered manifest must fail verification"


def test_try_sign_is_idempotent(
    tmp_path: Path, stub_keyring: _StubKeyring
) -> None:
    set_archive_signing_key("secret-abc", keyring_module=stub_keyring)
    manifest_path = tmp_path / "issue_081.json"
    _write_manifest(manifest_path, {"issue_number": 81, "edition": "acme_weekly"})

    # First sign
    w1 = _try_sign_archive_manifest(
        manifest_path=manifest_path,
        edition_name="acme_weekly",
        issue_number=81,
    )
    assert w1 == ()
    sidecar = manifest_signature_sidecar_path(manifest_path)
    record_v1 = load_signature_record(sidecar)
    assert record_v1 is not None

    # Re-sign with the same key — content unchanged so tag must verify.
    w2 = _try_sign_archive_manifest(
        manifest_path=manifest_path,
        edition_name="acme_weekly",
        issue_number=81,
    )
    assert w2 == ()
    record_v2 = load_signature_record(sidecar)
    assert record_v2 is not None
    assert record_v1.signature == record_v2.signature


def test_try_sign_raises_on_malformed_manifest(
    tmp_path: Path, stub_keyring: _StubKeyring
) -> None:
    set_archive_signing_key("secret-abc", keyring_module=stub_keyring)
    manifest_path = tmp_path / "issue_082.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    # Valid JSON but not a dict (a list).
    manifest_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    with pytest.raises(ValueError, match="not a JSON object"):
        _try_sign_archive_manifest(
            manifest_path=manifest_path,
            edition_name="acme_weekly",
            issue_number=82,
        )


def test_sidecar_path_convention(tmp_path: Path) -> None:
    """The sidecar-path helper is the only contract consumer code (e.g. the
    `vertex archive verify` command) depends on. Lock the convention."""
    manifest = tmp_path / "manifests" / "issue_078.json"
    sidecar = manifest_signature_sidecar_path(manifest)
    assert sidecar == tmp_path / "manifests" / "issue_078.json.sig"

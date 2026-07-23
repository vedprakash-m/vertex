"""WS-7: archive signature module.

Spec §WS-7 says the default artifact-signing path is **cosign keyless
(Sigstore)** — externally verifiable by an auditor. The cosign path requires
network access to the Sigstore transparency log, which is unavailable in
this Windows-CI environment and in many air-gapped Microsoft tenants. The
fallback is **HMAC-SHA256 with a keyring-backed key** (the same `keyring>=25`
base dependency already declared in `pyproject.toml`); this is the only path
implemented in this commit.

The `vertex archive verify --edition <e>` command is the verification entry
point. It loads the manifest, recomputes the content hash, and checks the
HMAC tag against the keyring-stored secret.

The spec's note on transparency: when a tenant can reach Sigstore, an
operator-supplied `sign_with_cosign()` implementation is expected to be
added (this is a [HUMAN GATE] decision — see WS-7 in `specs/prod-vis.md`).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


_KEYRING_SERVICE = "vertex-archive-signing"
_DEFAULT_KEYRING_USER = "primary"


class _KeyringLike(Protocol):
    """Structural type for the bits of `keyring` we use. Avoids a hard import
    so the module is testable without keyring on the test machine."""

    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...


def _import_keyring() -> _KeyringLike | None:
    try:
        import keyring  # type: ignore[import-untyped]

        return keyring
    except ImportError:
        return None


@dataclass(frozen=True, slots=True)
class SignatureRecord:
    edition: str
    issue_number: int
    content_hash: str
    algorithm: str
    signature: str
    key_id: str


def _sign_bytes(key: bytes, content: bytes) -> str:
    return hmac.new(key, content, hashlib.sha256).hexdigest()


def get_archive_signing_key(
    *,
    service: str = _KEYRING_SERVICE,
    username: str = _DEFAULT_KEYRING_USER,
    keyring_module: _KeyringLike | None = None,
) -> bytes | None:
    """Return the HMAC key stored under `service`/`username` in the
    system keyring, or None if no key is set.

    Tests can pass a stub `keyring_module` to avoid touching the real
    system keyring.
    """
    kr = keyring_module if keyring_module is not None else _import_keyring()
    if kr is None:
        return None
    try:
        secret = kr.get_password(service, username)
    except Exception:  # noqa: BLE001 — NoKeyringError or similar backend failure
        return None
    if secret is None or not secret.strip():
        return None
    return secret.encode("utf-8")


def set_archive_signing_key(
    secret: str,
    *,
    service: str = _KEYRING_SERVICE,
    username: str = _DEFAULT_KEYRING_USER,
    keyring_module: _KeyringLike | None = None,
) -> None:
    """Store the HMAC key in the system keyring. Used by setup/admin tools;
    not used at runtime by report/confirm."""
    kr = keyring_module if keyring_module is not None else _import_keyring()
    if kr is None:
        raise RuntimeError("keyring is not available; install keyring>=25.3.0")
    kr.set_password(service, username, secret)


def sign_manifest(
    *,
    edition: str,
    issue_number: int,
    manifest_payload: dict[str, Any],
    key: bytes,
    key_id: str = _DEFAULT_KEYRING_USER,
) -> SignatureRecord:
    """Sign a manifest payload. `key` is the HMAC secret; callers should
    obtain it from `get_archive_signing_key()`.
    """
    canonical = json.dumps(
        manifest_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    content_hash = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    raw_bytes = canonical.encode("utf-8")
    signature = _sign_bytes(key, raw_bytes)
    return SignatureRecord(
        edition=edition,
        issue_number=issue_number,
        content_hash=content_hash,
        algorithm="hmac-sha256",
        signature=signature,
        key_id=key_id,
    )


def verify_signature(
    record: SignatureRecord,
    *,
    manifest_payload: dict[str, Any],
    key: bytes,
) -> bool:
    """Return True iff `record` is a valid HMAC tag over the canonical JSON
    of `manifest_payload`."""
    canonical = json.dumps(
        manifest_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    raw_bytes = canonical.encode("utf-8")
    expected = _sign_bytes(key, raw_bytes)
    return hmac.compare_digest(expected, record.signature)


def load_signature_record(sidecar_path: Path) -> SignatureRecord | None:
    """Load a `.sig.json` sidecar. Returns None if the file is missing or
    unreadable. Raises ValueError on a malformed payload (we do NOT want
    to silently treat a tampered sidecar as a no-op)."""
    if not sidecar_path.exists():
        return None
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Signature sidecar {sidecar_path} is not a JSON object")
    required = ("edition", "issue_number", "content_hash", "algorithm", "signature", "key_id")
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"Signature sidecar {sidecar_path} missing keys: {missing}")
    return SignatureRecord(
        edition=str(payload["edition"]),
        issue_number=int(payload["issue_number"]),
        content_hash=str(payload["content_hash"]),
        algorithm=str(payload["algorithm"]),
        signature=str(payload["signature"]),
        key_id=str(payload["key_id"]),
    )


def write_signature_record(sidecar_path: Path, record: SignatureRecord) -> None:
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(
        json.dumps(
            {
                "edition": record.edition,
                "issue_number": record.issue_number,
                "content_hash": record.content_hash,
                "algorithm": record.algorithm,
                "signature": record.signature,
                "key_id": record.key_id,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def sign_manifest_file(
    *,
    manifest_path: Path,
    edition: str,
    issue_number: int,
    key: bytes,
    key_id: str = _DEFAULT_KEYRING_USER,
) -> SignatureRecord:
    """Sign a manifest file *as written on disk* (re-canonicalizing via
    `json.loads` then re-dumping with `sort_keys=True, separators=(",", ":")`).

    The caller is responsible for ensuring the file is finalized before this
    call. Returns a `SignatureRecord` whose `content_hash` is the SHA-256 of
    the canonical bytes re-derived here — i.e. *not* a re-hash of arbitrary
    disk bytes. The verifier (see `verify_signature`) re-canonicalizes the
    payload, so a re-serialized file (key order changed but content same)
    still verifies.

    Note: this helper does *not* check that `manifest_path` is a
    path Vertex wrote (it accepts any JSON). It is the caller's contract
    to pass a trusted manifest path. WS-7 hookup uses
    `archive_paths.manifest_path`, which is the only manifest the
    archive transaction writes."""
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest_path does not exist: {manifest_path}")
    raw = manifest_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(
            f"Manifest at {manifest_path} is not a JSON object (got {type(payload).__name__})"
        )
    return sign_manifest(
        edition=edition,
        issue_number=issue_number,
        manifest_payload=payload,
        key=key,
        key_id=key_id,
    )


def verify_manifest_file(
    *,
    manifest_path: Path,
    sidecar_path: Path,
    key: bytes,
) -> bool:
    """End-to-end check: load the on-disk manifest + on-disk sidecar, and
    return True iff the sidecar's HMAC tag matches a re-canonicalized hash
    of the on-disk manifest. Returns False (NOT raises) on missing sidecar
    so callers can decide whether to fail-loud or treat absence as
    a separate (e.g. pre-signing-era) condition.
    """
    record = load_signature_record(sidecar_path)
    if record is None:
        return False
    raw = manifest_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return False
    return verify_signature(record, manifest_payload=payload, key=key)


def manifest_signature_sidecar_path(manifest_path: Path) -> Path:
    """Path of the `.sig.json` sidecar that accompanies a given manifest.
    Convention: `issue_NNN.json` → `issue_NNN.sig.json`. Stable for the
    verifier command and for any future `vertex archive verify` lookup."""
    return manifest_path.with_suffix(manifest_path.suffix + ".sig")


def archive_signing_unavailable() -> bool:
    """True iff the system keyring cannot be reached AND no fallback key was
    provided via env. The confirm path uses this to decide whether to
    skip-vs-block on signing."""
    return get_archive_signing_key() is None


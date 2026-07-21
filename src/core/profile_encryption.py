from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import uuid
from typing import Any, Protocol

import yaml

from src.core.exceptions import ConfigError


PEOPLE_PROFILES_KEYRING_SERVICE = "vertex.people_profiles"
_PEOPLE_PROFILES_STORAGE = "encrypted"
_PEOPLE_PROFILES_ENCRYPTION_FORMAT = "fernet"


class ProfileKeyring(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SensitiveProfileFileStatus:
    path: Path
    storage: str
    profile_count: int
    key_id: str | None = None


def load_people_profiles_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    document = _read_yaml_mapping(path)
    if _is_encrypted_people_profiles_document(document):
        return _decrypt_people_profiles_document(path, document)
    return document


def inspect_people_profiles_file(path: Path) -> SensitiveProfileFileStatus:
    if not path.exists():
        return SensitiveProfileFileStatus(path=path, storage="missing", profile_count=0)

    document = _read_yaml_mapping(path)
    if _is_encrypted_people_profiles_document(document):
        decrypted = _decrypt_people_profiles_document(path, document)
        return SensitiveProfileFileStatus(
            path=path,
            storage=_PEOPLE_PROFILES_STORAGE,
            profile_count=_count_profiles(decrypted, path),
            key_id=_extract_key_id(document, path),
        )

    return SensitiveProfileFileStatus(path=path, storage="plaintext", profile_count=_count_profiles(document, path))


def encrypt_people_profiles_file(path: Path) -> SensitiveProfileFileStatus:
    current_status = inspect_people_profiles_file(path)
    if current_status.storage == _PEOPLE_PROFILES_STORAGE:
        return current_status
    if current_status.storage == "missing":
        raise ConfigError(f"No people_profiles.yaml file exists at {path}.")

    document = load_people_profiles_document(path)
    key_id = uuid.uuid4().hex
    key = _generate_fernet_key()
    _get_keyring_backend().set_password(PEOPLE_PROFILES_KEYRING_SERVICE, key_id, key)
    envelope = _build_encrypted_people_profiles_document(document, path=path, key_id=key_id)
    _write_yaml_mapping(path, envelope)
    return inspect_people_profiles_file(path)


def decrypt_people_profiles_file(path: Path) -> SensitiveProfileFileStatus:
    current_status = inspect_people_profiles_file(path)
    if current_status.storage == "plaintext":
        return current_status
    if current_status.storage == "missing":
        raise ConfigError(f"No people_profiles.yaml file exists at {path}.")

    document = load_people_profiles_document(path)
    _write_yaml_mapping(path, document)
    return inspect_people_profiles_file(path)


def dump_people_profiles_document(document: dict[str, Any], *, existing_path: Path | None = None) -> str:
    if existing_path is not None and existing_path.exists():
        existing_document = _read_yaml_mapping(existing_path)
        if _is_encrypted_people_profiles_document(existing_document):
            key_id = _extract_key_id(existing_document, existing_path)
            encrypted_document = _build_encrypted_people_profiles_document(document, path=existing_path, key_id=key_id)
            return _render_yaml(encrypted_document)
    return _render_yaml(document)


def shred_people_profiles_key(key_id: str) -> None:
    """Irreversibly remove an encrypted-profile key after its sole profile
    payload has been replaced with a redacted document."""
    normalized_key_id = key_id.strip()
    if not normalized_key_id:
        raise ConfigError("Cannot cryptographically shred an empty people-profiles key ID.")
    backend = _get_keyring_backend()
    try:
        backend.delete_password(PEOPLE_PROFILES_KEYRING_SERVICE, normalized_key_id)
    except AttributeError as error:
        raise ConfigError(
            "The configured people-profiles keyring does not support deletion; "
            "refusing to claim cryptographic shredding."
        ) from error
    except Exception as error:
        raise ConfigError(f"Unable to cryptographically shred people-profiles key {normalized_key_id!r}: {error}") from error


def _build_encrypted_people_profiles_document(document: dict[str, Any], *, path: Path, key_id: str) -> dict[str, Any]:
    key = _load_fernet_key(key_id, path)
    fernet_type = _build_fernet_type()
    payload = _render_yaml(document).encode("utf-8")
    ciphertext = fernet_type(key.encode("ascii")).encrypt(payload).decode("ascii")
    return {
        "schema_version": "1.0",
        "storage": _PEOPLE_PROFILES_STORAGE,
        "encryption": {
            "format": _PEOPLE_PROFILES_ENCRYPTION_FORMAT,
            "keyring_service": PEOPLE_PROFILES_KEYRING_SERVICE,
            "key_id": key_id,
        },
        "ciphertext": ciphertext,
    }


def _decrypt_people_profiles_document(path: Path, envelope: dict[str, Any]) -> dict[str, Any]:
    key_id = _extract_key_id(envelope, path)
    key = _load_fernet_key(key_id, path)
    ciphertext = envelope.get("ciphertext")
    if not isinstance(ciphertext, str) or not ciphertext.strip():
        raise ConfigError(f"Encrypted people_profiles envelope in {path} is missing ciphertext.")

    fernet_type = _build_fernet_type()
    try:
        plaintext = fernet_type(key.encode("ascii")).decrypt(ciphertext.encode("ascii"))
    except Exception as error:
        raise ConfigError(f"Unable to decrypt encrypted people_profiles.yaml at {path}: {error}") from error

    try:
        document = yaml.safe_load(plaintext.decode("utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Decrypted people_profiles.yaml payload in {path} is not valid YAML: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in decrypted {path}.")
    return document


def _extract_key_id(envelope: dict[str, Any], path: Path) -> str:
    encryption = envelope.get("encryption")
    if not isinstance(encryption, dict):
        raise ConfigError(f"Encrypted people_profiles envelope in {path} is missing encryption metadata.")
    if encryption.get("format") != _PEOPLE_PROFILES_ENCRYPTION_FORMAT:
        raise ConfigError(f"Unsupported people_profiles encryption format '{encryption.get('format')}' in {path}.")
    key_id = encryption.get("key_id")
    if not isinstance(key_id, str) or not key_id.strip():
        raise ConfigError(f"Encrypted people_profiles envelope in {path} is missing key_id.")
    return key_id.strip()


def _count_profiles(document: dict[str, Any], path: Path) -> int:
    raw_profiles = document.get("profiles") or []
    if not isinstance(raw_profiles, list):
        raise ConfigError(f"Expected 'profiles' list in {path}.")
    return sum(1 for entry in raw_profiles if isinstance(entry, dict))


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}.")
    return document


def _write_yaml_mapping(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(f"{path.suffix}.bak"))
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(_render_yaml(document), encoding="utf-8")
    os.replace(temp_path, path)


def _render_yaml(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=False)


def _generate_fernet_key() -> str:
    fernet_type = _build_fernet_type()
    return fernet_type.generate_key().decode("ascii")


def _load_fernet_key(key_id: str, path: Path) -> str:
    try:
        key = _get_keyring_backend().get_password(PEOPLE_PROFILES_KEYRING_SERVICE, key_id)
    except Exception as error:
        raise ConfigError(f"Unable to read people_profiles encryption key for {path}: {error}") from error
    if not key:
        raise ConfigError(f"Missing people_profiles encryption key '{key_id}' for {path} in keyring.")
    return key


def _build_fernet_type():
    try:
        from cryptography.fernet import Fernet
    except ImportError as error:
        raise ConfigError("cryptography is required for encrypted people_profiles.yaml support.") from error
    return Fernet


def _get_keyring_backend() -> ProfileKeyring:
    try:
        import keyring
    except ImportError as error:
        raise ConfigError("keyring is required for encrypted people_profiles.yaml support.") from error
    return keyring


def _is_encrypted_people_profiles_document(document: dict[str, Any]) -> bool:
    return document.get("storage") == _PEOPLE_PROFILES_STORAGE and isinstance(document.get("encryption"), dict)
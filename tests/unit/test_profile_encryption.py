from __future__ import annotations

from pathlib import Path

from src.core.profile_encryption import decrypt_people_profiles_file, encrypt_people_profiles_file, load_people_profiles_document


class _FakeKeyring:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self._values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._values[(service_name, username)] = password


def test_people_profiles_encrypt_decrypt_round_trip(tmp_path: Path, monkeypatch) -> None:
    profiles_path = tmp_path / "people_profiles.yaml"
    profiles_path.write_text(
        (
            'schema_version: "1.0"\n'
            'profiles:\n'
            '  - alias: demo\n'
            '    comm_style: concise\n'
            '    cares_about: [clarity]\n'
        ),
        encoding="utf-8",
    )
    fake_keyring = _FakeKeyring()
    monkeypatch.setattr("src.core.profile_encryption._get_keyring_backend", lambda: fake_keyring)

    encrypted = encrypt_people_profiles_file(profiles_path)

    assert encrypted.storage == "encrypted"
    assert encrypted.profile_count == 1
    assert "comm_style: concise" not in profiles_path.read_text(encoding="utf-8")

    decrypted_document = load_people_profiles_document(profiles_path)

    assert decrypted_document["profiles"][0]["alias"] == "demo"
    assert decrypted_document["profiles"][0]["comm_style"] == "concise"

    decrypted = decrypt_people_profiles_file(profiles_path)

    assert decrypted.storage == "plaintext"
    assert "comm_style: concise" in profiles_path.read_text(encoding="utf-8")
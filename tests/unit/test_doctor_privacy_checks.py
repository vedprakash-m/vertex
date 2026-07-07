from __future__ import annotations

from src.commands.doctor_checks.privacy_checks import parse_registry_metadata_json


def test_parse_registry_metadata_json_ignores_non_object_payloads() -> None:
    assert parse_registry_metadata_json('["demo@example.com"]') == {}

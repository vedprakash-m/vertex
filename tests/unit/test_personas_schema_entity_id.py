"""specs/people.md Phase 2a, PPL-W2A.5: tests for the optional
`entity_id` field added to `src/core/schemas/personas.schema.json`,
binding a person-specific persona to the canonical registry entity_id
(§7.9: "any person-specific persona must bind to canonical entity_id").

Synthetic fixtures only (no real customer/program persona names) --
`programs/*/knowledge/personas.yaml` is gitignored real customer data and
must never be read directly by a test (it wouldn't exist in a fresh CI
clone anyway). This file instead constructs documents matching real
production personas.yaml's STRUCTURE to prove backward compatibility.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "src" / "core" / "schemas" / "personas.schema.json"


def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _base_document(personas: list[dict]) -> dict:
    return {
        "schema_version": "1.0",
        "enforcement": {"enabled": True, "mode": "warn"},
        "personas": personas,
    }


def test_existing_persona_records_without_entity_id_still_validate() -> None:
    # Zero-regression proof: every persona record shape used in production
    # today (no entity_id field at all) must still validate unchanged.
    document = _base_document(
        [
            {"id": "generic_persona_a", "priority": "critical", "role": "Senior TPM", "always_active": True},
            {"id": "generic_persona_b", "priority": "high"},
        ]
    )

    jsonschema.validate(document, _schema())  # Must not raise.


def test_person_specific_persona_can_bind_to_a_canonical_entity_id() -> None:
    document = _base_document([{"id": "generic_persona_a", "priority": "critical", "entity_id": "person:01HQ8Y1A2B3C4D5E6F7G8H9J0K"}])

    jsonschema.validate(document, _schema())  # Must not raise.


def test_team_entity_id_also_valid() -> None:
    document = _base_document([{"id": "generic_persona_a", "priority": "critical", "entity_id": "team:01HQ8Y2K3L4M5N6P7Q8R9S0T1U"}])

    jsonschema.validate(document, _schema())  # Must not raise.


def test_entity_id_must_match_the_canonical_prefix_pattern() -> None:
    document = _base_document([{"id": "generic_persona_a", "priority": "critical", "entity_id": "not-a-canonical-id"}])

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, _schema())


def test_entity_id_may_be_explicitly_null() -> None:
    document = _base_document([{"id": "generic_persona_a", "priority": "critical", "entity_id": None}])

    jsonschema.validate(document, _schema())  # Must not raise -- null is a valid "not yet bound" state.

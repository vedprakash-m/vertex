"""Synthetic authority contracts, not validation or ratification of a deployment."""
from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from scripts.verify_relo import assess_terminal_state

_VALID_PRINCIPAL_MODES = {"delegated", "application", "none"}


@pytest.fixture
def matrix() -> dict:
    # Deliberately authored examples; no provider set or operator approval is implied.
    return {
        "schema_version": "1",
        "sources": [
            {
                "source": "fixture_required",
                "mandatory": True,
                "interactive": {"principal_mode": "delegated"},
                "scheduled": {"principal_mode": "none"},
            },
            {
                "source": "fixture_optional",
                "mandatory": False,
                "interactive": {"principal_mode": "application"},
                "scheduled": {
                    "principal_mode": "application",
                    "operation_contract_ref": "fixture-contract-not-an-approval",
                },
            },
        ],
    }


def _assert_generic_authority_invariants(document: dict) -> None:
    assert document["schema_version"] == "1"
    sources = document["sources"]
    assert len({row["source"] for row in sources}) == len(sources)
    for row in sources:
        assert isinstance(row["mandatory"], bool)
        assert row["interactive"]["principal_mode"] in _VALID_PRINCIPAL_MODES
        scheduled = row["scheduled"]
        assert scheduled["principal_mode"] in {"application", "none"}
        if scheduled["principal_mode"] == "application":
            assert scheduled.get("operation_contract_ref")


def test_synthetic_matrix_exercises_generic_authority_invariants(matrix) -> None:
    _assert_generic_authority_invariants(matrix)


@pytest.mark.parametrize(
    ("context", "field", "value"),
    [
        ("interactive", "principal_mode", "unknown"),
        ("scheduled", "principal_mode", "delegated"),
        ("scheduled", "principal_mode", "unknown"),
        ("scheduled", "operation_contract_ref", ""),
    ],
)
def test_invalid_authority_examples_are_rejected(matrix, context, field, value) -> None:
    invalid = deepcopy(matrix)
    invalid["sources"][1][context][field] = value
    with pytest.raises(AssertionError):
        _assert_generic_authority_invariants(invalid)


def test_projection_reads_only_the_selected_synthetic_root(tmp_path, matrix) -> None:
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "relo-authority-matrix.yaml").write_text(yaml.safe_dump(matrix), encoding="utf-8")
    before = {path: path.read_bytes() for path in governance.iterdir()}

    report = assess_terminal_state(tmp_path, spec_path=tmp_path / "absent.md")

    assert report["source_authority"] == {
        "mandatory_sources": ["fixture_required"],
        "scheduled_principal_modes": {"fixture_required": "none", "fixture_optional": "application"},
    }
    assert report["terminal_state"] is None
    assert report["assessment"] == "not_assessed"
    assert {path: path.read_bytes() for path in governance.iterdir()} == before

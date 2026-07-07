"""WS-24 contract tests: model_registry behavior.

The registry is the runtime guard against silent model-version drift
(governance/threat-model.md T-7). The tests below pin:

- default_pin produces a 90-day deprecation-review window;
- a no-op match (deployment unchanged) returns ``matched=True`` and
  does not raise;
- a bumped deployment raises ``ModelBumpDetectedError`` when
  ``policy_block_on_bump=True`` and records a bump row;
- a bumped deployment is allowed (returns ``matched=False,
  blocked=False``) when ``policy_block_on_bump=False``;
- ``read_model_pin`` returns the most-recent pin (append-only history);
- the sidecar lives at the registered D-18 path and is
  portalocker-routed.
"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.model_registry import (
    DEFAULT_POLICY_BLOCK_ON_BUMP,
    ModelBumpDetectedError,
    ModelPin,
    default_pin,
    model_registry_path,
    read_model_pin,
    read_model_registry,
    record_model_deployment_used,
    register_model_pin,
)
from src.core.state_reader_registry import STATE_READER_REGISTRY


REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PY = REPO_ROOT / "src" / "core" / "model_registry.py"


# ---------------------------------------------------------------------------
# Library tests
# ---------------------------------------------------------------------------


def test_default_pin_has_90d_deprecation_window() -> None:
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    pin = default_pin("claim_extractor", now=now)
    assert pin.feature_name == "claim_extractor"
    assert pin.pinned_at == now
    assert pin.deprecation_review_at == now + timedelta(days=90)
    assert pin.recert_at is None


def test_default_pin_default_blocks_on_bump() -> None:
    pin = default_pin("blurb_generator")
    assert pin.policy_block_on_bump is True
    # Module constant is also True (consistency check).
    assert DEFAULT_POLICY_BLOCK_ON_BUMP is True


def test_match_path_returns_matched_true_no_raise(tmp_path: Path) -> None:
    register_model_pin(default_pin("blurb_generator", deployment_id="gpt-4o", model_id="gpt-4o"), programs_root=tmp_path)
    result = record_model_deployment_used(
        "blurb_generator",
        deployment_id="gpt-4o",
        model_id="gpt-4o",
        programs_root=tmp_path,
    )
    assert result.matched is True
    assert result.blocked is False
    assert result.previous_deployment_id == "gpt-4o"
    assert result.current_deployment_id == "gpt-4o"


def test_bumped_deployment_raises_when_blocked(tmp_path: Path) -> None:
    register_model_pin(default_pin("blurb_generator", deployment_id="gpt-4o", model_id="gpt-4o"), programs_root=tmp_path)
    with pytest.raises(ModelBumpDetectedError):
        record_model_deployment_used(
            "blurb_generator",
            deployment_id="gpt-4o-2024-08-06",  # bump
            model_id="gpt-4o-2024-08-06",
            programs_root=tmp_path,
        )
    # The bump row is still recorded even though the raise happened —
    # audit trail is the priority.
    bumps = _read_bumps(tmp_path)
    assert len(bumps) == 1
    assert bumps[0]["matched"] is False
    assert bumps[0]["blocked"] is True


def test_bumped_deployment_allowed_when_policy_disabled(tmp_path: Path) -> None:
    register_model_pin(
        default_pin("blurb_generator", deployment_id="gpt-4o", model_id="gpt-4o"),
        programs_root=tmp_path,
    )
    result = record_model_deployment_used(
        "blurb_generator",
        deployment_id="gpt-4o-2024-08-06",
        model_id="gpt-4o-2024-08-06",
        programs_root=tmp_path,
        policy_block_on_bump=False,
    )
    assert result.matched is False
    assert result.blocked is False
    assert result.previous_deployment_id == "gpt-4o"
    assert result.current_deployment_id == "gpt-4o-2024-08-06"


def test_read_model_pin_returns_most_recent(tmp_path: Path) -> None:
    # Append two pins for the same feature; read returns the second.
    register_model_pin(
        default_pin("blurb_generator", deployment_id="gpt-4o", model_id="gpt-4o"),
        programs_root=tmp_path,
    )
    register_model_pin(
        default_pin("blurb_generator", deployment_id="gpt-4o-2024-08-06", model_id="gpt-4o-2024-08-06"),
        programs_root=tmp_path,
    )
    pin = read_model_pin("blurb_generator", programs_root=tmp_path)
    assert pin is not None
    assert pin.deployment_id == "gpt-4o-2024-08-06"


def test_read_model_pin_returns_none_when_absent(tmp_path: Path) -> None:
    assert read_model_pin("missing", programs_root=tmp_path) is None


def test_no_pin_means_first_call_writes_a_bump_row(tmp_path: Path) -> None:
    """A feature with no pin is treated as a bump (the operator
    must register a pin before serving traffic)."""
    with pytest.raises(ModelBumpDetectedError):
        record_model_deployment_used(
            "no_pin_feature",
            deployment_id="gpt-4o",
            model_id="gpt-4o",
            programs_root=tmp_path,
        )
    bumps = _read_bumps(tmp_path)
    assert len(bumps) == 1
    assert bumps[0]["previous_deployment_id"] == ""  # no pin yet


def test_register_model_pin_is_portalocker_routed(tmp_path: Path) -> None:
    """PB-37: the sidecar must be append-only via ``append_jsonl_line``
    (portalocker + fsync). Direct .open("a", ...) writes are forbidden."""
    text = REGISTRY_PY.read_text(encoding="utf-8")
    assert "append_jsonl_line" in text
    tree = ast.parse(text, filename=str(REGISTRY_PY))
    direct_appends: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "open":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "a" in arg.value:
                    direct_appends.append((node.lineno, node.col_offset))
    assert not direct_appends, (
        f"model_registry.py has direct .open('a',...) calls at {direct_appends} — "
        "must route through append_jsonl_line (PB-37)"
    )


def test_sidecar_lives_at_d18_path(tmp_path: Path) -> None:
    assert model_registry_path(tmp_path) == tmp_path / "_state" / "model_registry.jsonl"


def test_model_registry_state_registered() -> None:
    reg = STATE_READER_REGISTRY["model_registry"]
    assert reg.owner_module == "src.core.model_registry"
    for sym in ("read_model_registry", "read_model_pin", "register_model_pin", "record_model_deployment_used"):
        assert sym in reg.reader_symbols


def test_pin_and_bump_round_trip(tmp_path: Path) -> None:
    """Register a pin, then bump it; the sidecar should have one pin
    row + one bump row that are independently parseable."""
    register_model_pin(
        default_pin("claim_extractor", deployment_id="gpt-4o", model_id="gpt-4o"),
        programs_root=tmp_path,
    )
    with pytest.raises(ModelBumpDetectedError):
        record_model_deployment_used(
            "claim_extractor",
            deployment_id="gpt-4o-mini",
            model_id="gpt-4o-mini",
            programs_root=tmp_path,
        )
    raw = (tmp_path / "_state" / "model_registry.jsonl").read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(line)["kind"] for line in raw if line.strip()]
    assert kinds == ["pin", "bump"]


def test_recert_at_is_optional(tmp_path: Path) -> None:
    now = datetime(2026, 6, 9, tzinfo=timezone.utc)
    pin = ModelPin(
        feature_name="x",
        model_id="m",
        deployment_id="d",
        pinned_at=now,
        deprecation_review_at=now + timedelta(days=30),
        recert_at=None,
    )
    register_model_pin(pin, programs_root=tmp_path)
    loaded = read_model_pin("x", programs_root=tmp_path)
    assert loaded is not None
    assert loaded.recert_at is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_bumps(tmp_path: Path) -> list[dict[str, object]]:
    path = model_registry_path(tmp_path)
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("kind") == "bump":
            rows.append(payload)
    return rows

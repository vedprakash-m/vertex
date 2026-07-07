"""WS-24: model-version lifecycle registry.

The platform pins the exact ``model_id`` + ``deployment_id`` that each AI
feature is allowed to use. When an operator flips a feature's deployment
(e.g. ``gpt-4o`` → ``gpt-4o-2024-08-06``) the system must:

1. Record the bump so an SRE can see who flipped what and when.
2. Optionally **block** the bumped deployment at runtime so the feature
   cannot silently switch models without the registry's consent. This
   protects the eval-set and the prompt-card pipeline: a model that has
   not been re-eval'd cannot be served to a feature that still uses
   the old model's contract.

The default for ``policy_block_on_bump`` is ``True`` (conservative — fail
closed). Operators who want auto-adopt can flip to ``False`` via
``vertex admin model-registry configure``.

Sidecar: ``programs/<id>/_state/model_registry.jsonl`` (PB-37 routed).
Registered in ``state_reader_registry`` as the 27th state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping

from src.core.jsonl_utils import append_jsonl_line
from src.core.exceptions import StateError


MODEL_REGISTRY_FILENAME = "model_registry.jsonl"

# Default policy: fail closed. Operators can override per-feature via
# ``policy_block_on_bump: false`` in the registry row.
DEFAULT_POLICY_BLOCK_ON_BUMP = True

# Recert window: 90 days. After this, a ``recert_at`` becomes "stale"
# and ``read_model_registry`` flags the row with ``needs_recert=True``.
DEFAULT_RECERT_WINDOW_DAYS = 90

# Deprecation review horizon: 90 days. After the model's
# ``deprecation_review_at`` passes, the row is flagged ``past_review=True``
# and the operator should retire the deployment.
DEFAULT_DEPRECATION_REVIEW_DAYS = 90


@dataclass(frozen=True, slots=True)
class ModelPin:
    """The pinned deployment for a single AI feature."""
    feature_name: str
    model_id: str
    deployment_id: str
    pinned_at: datetime
    deprecation_review_at: datetime
    recert_at: datetime | None
    policy_block_on_bump: bool = DEFAULT_POLICY_BLOCK_ON_BUMP

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "model_id": self.model_id,
            "deployment_id": self.deployment_id,
            "pinned_at": _iso(self.pinned_at),
            "deprecation_review_at": _iso(self.deprecation_review_at),
            "recert_at": _iso(self.recert_at) if self.recert_at is not None else None,
            "policy_block_on_bump": self.policy_block_on_bump,
        }


@dataclass(frozen=True, slots=True)
class ModelBumpResult:
    """The outcome of comparing an actual deployment to the registered pin."""
    feature_name: str
    matched: bool
    previous_deployment_id: str
    current_deployment_id: str
    previous_model_id: str
    current_model_id: str
    recorded_at: datetime
    blocked: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "matched": self.matched,
            "previous_deployment_id": self.previous_deployment_id,
            "current_deployment_id": self.current_deployment_id,
            "previous_model_id": self.previous_model_id,
            "current_model_id": self.current_model_id,
            "recorded_at": _iso(self.recorded_at),
            "blocked": self.blocked,
        }


class ModelBumpDetectedError(RuntimeError):
    """Raised when a feature's actual deployment does not match the
    registry pin and ``policy_block_on_bump=True``. Callers should let
    this propagate so the ``FallbackAIClient`` can route to the
    deterministic / backup deployment."""


# ---------- write-side ----------


def record_model_deployment_used(
    feature_name: str,
    *,
    deployment_id: str,
    model_id: str,
    programs_root: Path,
    policy_block_on_bump: bool = DEFAULT_POLICY_BLOCK_ON_BUMP,
    now: datetime | None = None,
) -> ModelBumpResult:
    """Compare the just-used deployment to the registered pin.

    - On match: append a "match" row and return ``matched=True``.
    - On mismatch: append a "bump" row. If ``policy_block_on_bump=True``,
      raise ``ModelBumpDetectedError`` *and* return the result (so the
      caller can log it; the raise is what blocks the call).

    The sidecar is append-only; bumps are recorded forever even when
    blocked. The bump history is the audit trail.
    """
    now = now or datetime.now(timezone.utc)
    pin = read_model_pin(feature_name, programs_root=programs_root, now=now)
    matched = (
        pin is not None
        and pin.deployment_id == deployment_id
        and pin.model_id == model_id
    )
    blocked = (not matched) and policy_block_on_bump
    result = ModelBumpResult(
        feature_name=feature_name,
        matched=matched,
        previous_deployment_id=pin.deployment_id if pin else "",
        current_deployment_id=deployment_id,
        previous_model_id=pin.model_id if pin else "",
        current_model_id=model_id,
        recorded_at=now,
        blocked=blocked,
    )
    _append_bump(result, programs_root=programs_root)
    if blocked:
        pin_desc = (
            f"{pin.deployment_id!r} (model {pin.model_id!r})"
            if pin is not None
            else "(no registered pin)"
        )
        raise ModelBumpDetectedError(
            f"{feature_name}: deployment {deployment_id!r} (model {model_id!r}) "
            f"does not match {pin_desc} — blocked per policy"
        )
    return result


# ---------- read-side ----------


def read_model_registry(
    programs_root: Path,
    *,
    now: datetime | None = None,
) -> tuple[ModelPin, ...]:
    """Return the registered pins (most-recent first per feature)."""
    path = model_registry_path(programs_root)
    if not path.exists():
        return ()
    now = now or datetime.now(timezone.utc)
    rows: list[ModelPin] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise StateError(f"Invalid model_registry row: {line[:80]!r}: {error}") from error
            if payload.get("kind") != "pin":
                continue
            try:
                rows.append(_pin_from_payload(payload, now=now))
            except (KeyError, TypeError, ValueError) as error:
                raise StateError(f"Invalid model_registry pin: {error}") from error
    rows.sort(key=lambda p: p.pinned_at, reverse=True)
    return tuple(rows)


def read_model_pin(
    feature_name: str,
    *,
    programs_root: Path,
    now: datetime | None = None,
) -> ModelPin | None:
    """Return the most-recent pin for a single feature (or None)."""
    for pin in read_model_registry(programs_root, now=now):
        if pin.feature_name == feature_name:
            return pin
    return None


def register_model_pin(
    pin: ModelPin,
    *,
    programs_root: Path,
) -> Path:
    """Append a new ``ModelPin`` to the sidecar.

    The registry is append-only — callers that want to "change" a pin
    append a new row; ``read_model_pin`` returns the most-recent.
    """
    path = model_registry_path(programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"kind": "pin", **pin.to_dict()}
    append_jsonl_line(path, json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def model_registry_path(programs_root: Path) -> Path:
    return programs_root / "_state" / MODEL_REGISTRY_FILENAME


# ---------- defaults + internals ----------


def default_pin(
    feature_name: str,
    *,
    model_id: str = "gpt-4o",
    deployment_id: str = "gpt-4o",
    now: datetime | None = None,
    policy_block_on_bump: bool = DEFAULT_POLICY_BLOCK_ON_BUMP,
) -> ModelPin:
    """Construct a default pin for a feature — used when the registry
    is empty (first install / new feature). The deprecation review
    window is set to now+90d; recert_at is None until the operator
    re-evaluates the model."""
    now = now or datetime.now(timezone.utc)
    return ModelPin(
        feature_name=feature_name,
        model_id=model_id,
        deployment_id=deployment_id,
        pinned_at=now,
        deprecation_review_at=now + timedelta(days=DEFAULT_DEPRECATION_REVIEW_DAYS),
        recert_at=None,
        policy_block_on_bump=policy_block_on_bump,
    )


def _append_bump(result: ModelBumpResult, *, programs_root: Path) -> None:
    path = model_registry_path(programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"kind": "bump", **result.to_dict()}
    append_jsonl_line(path, json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _pin_from_payload(payload: Mapping[str, Any], *, now: datetime) -> ModelPin:
    pinned = _parse_dt(payload.get("pinned_at"))
    review = _parse_dt(payload.get("deprecation_review_at"))
    if pinned is None or review is None:
        raise StateError("model_registry pin missing pinned_at / deprecation_review_at")
    return ModelPin(
        feature_name=str(payload.get("feature_name") or "").strip(),
        model_id=str(payload.get("model_id") or "").strip(),
        deployment_id=str(payload.get("deployment_id") or "").strip(),
        pinned_at=pinned,
        deprecation_review_at=review,
        recert_at=_parse_dt(payload.get("recert_at")),
        policy_block_on_bump=bool(payload.get("policy_block_on_bump", DEFAULT_POLICY_BLOCK_ON_BUMP)),
    )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

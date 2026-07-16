"""ADF-W2.8 (specs/arch-data-fix.md Section 8.9.1/8.9.2): AISchemaGateway.

The second stage of the AI safety boundary (``AITransport -> AISchemaGateway
-> feature SemanticValidator -> ...``). Purely structural/deterministic
checks -- no AI call, Zone-A-safe:

- bounded input size/depth/array length (this module);
- version selection, upcast, and re-validation against the current schema
  (``validate_and_upcast``);
- allowlisted coercion only (callers supply their own coercion map; this
  gateway does not silently coerce arbitrary types);
- sanitized errors (``SchemaGatewayError`` messages never echo raw payload
  content back -- only field names/paths and bound values).

Feature-specific *semantic* validation (evidence existence, entity
resolution, source authority, ...) is Section 8.9.3's job and is out of
this module's scope -- see ``SemanticValidator`` Protocol here for the
injection point a feature-specific validator plugs into, mirroring how
``ContextCompiler`` injects a ``TokenEstimator``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class SchemaGatewayError(Exception):
    """Sanitized: never includes raw payload values, only field
    paths/bounds, so a rejected payload never leaks its content into logs
    or error messages beyond what the caller already has."""


@dataclass(frozen=True, slots=True)
class BoundsPolicy:
    """Section 8.9.2 "bounded input size/depth/array length"."""

    max_depth: int = 12
    max_array_length: int = 1000
    max_string_length: int = 200_000
    max_object_keys: int = 500


def validate_bounded_payload(payload: Any, *, bounds: BoundsPolicy = BoundsPolicy(), _path: str = "$", _depth: int = 0) -> None:
    """Raises ``SchemaGatewayError`` on the first bound violation found via
    depth-first traversal. Structural check only -- does not validate
    field presence/types (that is ``validate_and_upcast``'s job, driven by
    each feature's own schema)."""
    if _depth > bounds.max_depth:
        raise SchemaGatewayError(f"payload exceeds max depth {bounds.max_depth} at {_path}")
    if isinstance(payload, dict):
        if len(payload) > bounds.max_object_keys:
            raise SchemaGatewayError(f"object at {_path} exceeds max_object_keys {bounds.max_object_keys} ({len(payload)} keys)")
        for key, value in payload.items():
            if not isinstance(key, str):
                raise SchemaGatewayError(f"non-string key at {_path}")
            validate_bounded_payload(value, bounds=bounds, _path=f"{_path}.{key}", _depth=_depth + 1)
    elif isinstance(payload, list):
        if len(payload) > bounds.max_array_length:
            raise SchemaGatewayError(f"array at {_path} exceeds max_array_length {bounds.max_array_length} ({len(payload)} items)")
        for index, item in enumerate(payload):
            validate_bounded_payload(item, bounds=bounds, _path=f"{_path}[{index}]", _depth=_depth + 1)
    elif isinstance(payload, str):
        if len(payload) > bounds.max_string_length:
            raise SchemaGatewayError(f"string at {_path} exceeds max_string_length {bounds.max_string_length} ({len(payload)} chars)")


UpcastFn = Callable[[dict[str, Any]], dict[str, Any]]
ValidatorFn = Callable[[dict[str, Any]], None]


def validate_and_upcast(
    payload: dict[str, Any],
    *,
    payload_version: str,
    current_version: str,
    upcasters: Mapping[str, UpcastFn],
    validate_old: ValidatorFn | None,
    validate_current: ValidatorFn,
    bounds: BoundsPolicy = BoundsPolicy(),
) -> dict[str, Any]:
    """Section 8.9.2's "version selection; validate old payload; upcast;
    validate current payload" sequence.

    ``upcasters`` maps a version string to the function that upcasts a
    payload AT that version to the NEXT version (e.g. ``{"1": upcast_1_to_2,
    "2": upcast_2_to_3}``); this walks the chain from ``payload_version`` to
    ``current_version``. Raises ``SchemaGatewayError`` if no chain exists,
    if the old payload fails ``validate_old`` (when supplied), or if the
    final upcast result fails ``validate_current``.
    """
    validate_bounded_payload(payload, bounds=bounds)

    if payload_version != current_version and validate_old is not None:
        try:
            validate_old(payload)
        except Exception as error:
            raise SchemaGatewayError(f"payload at version {payload_version!r} failed validate_old: {error}") from error

    current: dict[str, Any] = payload
    version = payload_version
    seen_versions = {version}
    while version != current_version:
        upcaster = upcasters.get(version)
        if upcaster is None:
            raise SchemaGatewayError(
                f"no upcast path from version {payload_version!r} to {current_version!r} "
                f"(stuck at {version!r}; no registered upcaster)"
            )
        current = upcaster(current)
        next_version = current.get("schema_version")
        if not isinstance(next_version, str):
            raise SchemaGatewayError(f"upcaster for version {version!r} did not set a string 'schema_version'")
        if next_version in seen_versions:
            raise SchemaGatewayError(f"upcast chain cycle detected at version {next_version!r}")
        seen_versions.add(next_version)
        version = next_version

    try:
        validate_current(current)
    except Exception as error:
        raise SchemaGatewayError(f"upcasted payload (version {current_version!r}) failed validate_current: {error}") from error

    validate_bounded_payload(current, bounds=bounds)
    return current


class SemanticValidator(Protocol):
    """Section 8.9.3: feature-specific semantic validation, injected the
    same way ``ContextCompiler`` injects a ``TokenEstimator`` -- Zone A
    defines the protocol, each AI feature (Zone B / commands) supplies its
    own concrete implementation. No concrete validator is built here; this
    item does not invent per-feature semantic rules (evidence existence,
    entity resolution, source authority, ...) without a live feature to
    validate against.
    """

    @property
    def validator_id(self) -> str:
        ...

    def validate(self, payload: dict[str, Any]) -> tuple[str, ...]:
        """Returns a tuple of finding descriptions (empty = no findings).
        Raising is reserved for infrastructure failure, not a semantic
        finding -- a finding is data the release decision consumes, not an
        exception the caller must catch."""
        ...


__all__ = [
    "BoundsPolicy",
    "SchemaGatewayError",
    "SemanticValidator",
    "UpcastFn",
    "ValidatorFn",
    "validate_and_upcast",
    "validate_bounded_payload",
]

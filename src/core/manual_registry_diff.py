"""Explicit, non-mutating diffs for manual-only workstream-registry changes."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.core.ncfl_models import ContextUpdateProposal


class ManualRegistryDiffError(ValueError):
    """The proposed manual change cannot be rendered safely."""


_NESTED_FIELDS = frozenset({"deep_context.why", "deep_context.what", "deep_context.how"})
_BASELINE_PREFIX = "intake-v1:"


def read_registry_baseline(
    program_id: str, target_key: str, target_field: str, *, programs_root: Path
) -> tuple[str, str]:
    """Capture a typed scalar baseline, including absence, without writing."""
    current, state = _read_registry_value(program_id, target_key, target_field, programs_root=programs_root)
    return current, _baseline_hash(current, state)


def _baseline_hash(current: str, state: str) -> str:
    serialized = json.dumps((state, current), ensure_ascii=False)
    return _BASELINE_PREFIX + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _read_registry_value(
    program_id: str, target_key: str, target_field: str, *, programs_root: Path,
    registry_documents: dict[str, Any] | None = None,
) -> tuple[str, str]:
    if not program_id or program_id in {".", ".."} or any(char in program_id for char in "/\\:"):
        raise ManualRegistryDiffError("invalid program identifier")
    if not target_key.strip() or not target_field.strip():
        raise ManualRegistryDiffError("manual registry diffs require a target key and field")
    if "." in target_field and target_field not in _NESTED_FIELDS:
        raise ManualRegistryDiffError("unsupported nested field path")
    path = programs_root / program_id / "workstream_registry.yaml"
    if not path.resolve().is_relative_to((programs_root / program_id).resolve()):
        raise ManualRegistryDiffError("registry path leaves the selected program")
    if registry_documents is not None and program_id in registry_documents:
        payload = registry_documents[program_id]
    else:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if registry_documents is not None:
            registry_documents[program_id] = payload
    if not isinstance(payload, dict) or not isinstance(payload.get("workstreams"), list):
        raise ManualRegistryDiffError(f"{path} must contain a workstreams list")
    entries = [entry for entry in payload["workstreams"] if isinstance(entry, dict) and entry.get("id") == target_key]
    if len(entries) != 1:
        raise ManualRegistryDiffError(f"workstream {target_key!r} was not found uniquely in {path}")
    entry = entries[0]
    field = target_field
    if target_field in _NESTED_FIELDS:
        field = target_field.split(".")[1]
        entry = entry.get("deep_context", {})
        if not isinstance(entry, dict):
            raise ManualRegistryDiffError("deep_context must be a mapping")
    if field not in entry:
        if target_field not in _NESTED_FIELDS:
            raise ManualRegistryDiffError(f"field {field!r} was not found for workstream {target_key!r}")
        return "<missing>", "missing"
    value = entry[field]
    if value is None:
        return "<null>", "null"
    return _scalar_value(value, field=target_field), type(value).__name__


@dataclass(frozen=True, slots=True)
class ManualRegistryDiff:
    path: Path
    proposal_id: str
    current_value: str
    proposed_value: str
    text: str


def render_workstream_registry_manual_diff(
    proposal: ContextUpdateProposal, *, programs_root: Path
) -> ManualRegistryDiff:
    """Render a stale-safe, copyable field diff without writing authored YAML.

    Registry changes deliberately remain manual until a separately approved
    canonical writer exists.  The proposal's optimistic-concurrency hash is
    checked against the current scalar value before exposing a patch, so an
    operator is never shown a falsely current edit.
    """
    if proposal.target_store != "workstream_registry":
        raise ManualRegistryDiffError("manual registry diffs require target_store='workstream_registry'")
    if proposal.source_type == "context_gap_reply" and (
        not isinstance(proposal.current_value_hash, str)
        or re.fullmatch(r"intake-v1:[0-9a-f]{64}", proposal.current_value_hash) is None
    ):
        raise ManualRegistryDiffError("reply manual diffs require a canonical intake-v1:<sha256> baseline")
    path = programs_root / proposal.program_id / "workstream_registry.yaml"
    try:
        current_value, state = _read_registry_value(
            proposal.program_id, proposal.target_key, proposal.target_field, programs_root=programs_root,
        )
    except OSError as exc:
        raise ManualRegistryDiffError(f"could not read {path}: {exc}") from exc
    if proposal.current_value_hash is not None:
        current_hash = (
            _baseline_hash(current_value, state) if proposal.current_value_hash.startswith(_BASELINE_PREFIX)
            else hashlib.sha256(("" if state == "null" else current_value).encode("utf-8")).hexdigest()
        )
        if current_hash != proposal.current_value_hash:
            raise ManualRegistryDiffError(
                "proposal is stale: the current registry value no longer matches its recorded hash"
            )

    nested = proposal.target_field in _NESTED_FIELDS
    if nested:
        leaf = proposal.target_field.split(".")[1]
        old_line = (
            f"# {leaf}: <missing>" if state == "missing"
            else f"{leaf}: null" if state == "null"
            else f"{leaf}: " + yaml.safe_dump(current_value, default_style='"', width=float("inf")).strip()
        )
        new_line = f"{leaf}: " + yaml.safe_dump(proposal.source_value, default_style='"', width=float("inf")).strip()
    else:
        if state == "null":
            current_value = ""
        old_line = _yaml_scalar_line(proposal.target_field, current_value)
        new_line = _yaml_scalar_line(proposal.target_field, proposal.source_value)
    text = "\n".join(
        (
            f"# Manual-only change; do not use NCFL apply (proposal {proposal.proposal_id}).",
            f"# Target: {path} → workstreams[id={proposal.target_key!r}].{proposal.target_field}",
            f"# Evidence: {proposal.source_artifact}#{proposal.source_field}",
            *(
                (f"# {proposal.extraction_method_rationale}",)
                if proposal.source_type == "context_gap_reply" else ()
            ),
            f"--- {path}",
            f"+++ {path} (manual proposal)",
            f"@@ workstreams[id={proposal.target_key!r}]{'.deep_context' if nested else ''} @@",
            f"-{'    ' if nested else '  '}{old_line}",
            f"+{'    ' if nested else '  '}{new_line}",
        )
    ) + "\n"
    return ManualRegistryDiff(
        path=path,
        proposal_id=proposal.proposal_id,
        current_value=current_value,
        proposed_value=proposal.source_value,
        text=text,
    )


def _scalar_value(value: Any, *, field: str) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        raise ManualRegistryDiffError(f"field {field!r} is not scalar and requires a dedicated manual review")
    return "" if value is None else str(value)


def _yaml_scalar_line(field: str, value: str) -> str:
    """Use PyYAML's scalar quoting rather than emitting an unsafe hand-quote."""
    rendered = yaml.safe_dump({field: value}, default_flow_style=False, sort_keys=False).strip()
    return rendered.replace("\n", " ")

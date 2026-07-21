"""Explicit, non-mutating diffs for manual-only workstream-registry changes."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.core.ncfl_models import ContextUpdateProposal


class ManualRegistryDiffError(ValueError):
    """The proposed manual change cannot be rendered safely."""


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
    if not proposal.target_key.strip() or not proposal.target_field.strip():
        raise ManualRegistryDiffError("manual registry diffs require a target key and field")
    if "." in proposal.target_field:
        raise ManualRegistryDiffError("manual registry diffs support one scalar field, not a nested field path")

    path = programs_root / proposal.program_id / "workstream_registry.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ManualRegistryDiffError(f"could not read {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("workstreams"), list):
        raise ManualRegistryDiffError(f"{path} must contain a workstreams list")
    entry = next(
        (
            candidate for candidate in payload["workstreams"]
            if isinstance(candidate, dict) and candidate.get("id") == proposal.target_key
        ),
        None,
    )
    if entry is None:
        raise ManualRegistryDiffError(f"workstream {proposal.target_key!r} was not found in {path}")
    if proposal.target_field not in entry:
        raise ManualRegistryDiffError(
            f"field {proposal.target_field!r} was not found for workstream {proposal.target_key!r}"
        )
    current_value = _scalar_value(entry[proposal.target_field], field=proposal.target_field)
    if proposal.current_value_hash is not None:
        current_hash = hashlib.sha256(current_value.encode("utf-8")).hexdigest()
        if current_hash != proposal.current_value_hash:
            raise ManualRegistryDiffError(
                "proposal is stale: the current registry value no longer matches its recorded hash"
            )

    old_line = _yaml_scalar_line(proposal.target_field, current_value)
    new_line = _yaml_scalar_line(proposal.target_field, proposal.source_value)
    text = "\n".join(
        (
            f"# Manual-only change; do not use NCFL apply (proposal {proposal.proposal_id}).",
            f"# Target: {path} → workstreams[id={proposal.target_key!r}].{proposal.target_field}",
            f"# Evidence: {proposal.source_artifact}#{proposal.source_field}",
            f"--- {path}",
            f"+++ {path} (manual proposal)",
            f"@@ workstreams[id={proposal.target_key!r}] @@",
            f"-  {old_line}",
            f"+  {new_line}",
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

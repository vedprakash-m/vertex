from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.manual_registry_diff import ManualRegistryDiffError, render_workstream_registry_manual_diff
from src.core.ncfl_models import ContextUpdateProposal


def _proposal(*, current_value: str = "Old wording") -> ContextUpdateProposal:
    return ContextUpdateProposal(
        proposal_id="proposal-1", program_id="armada", issue_number=1, edition_id="armada_weekly",
        source_type="confirmed_overrides", extracted_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        extractor_version="1.0.0", source_artifact="overrides/issue_001.yaml", source_field="summary",
        extraction_method="overrides_yaml", target_store="workstream_registry", target_key="lane-a",
        target_field="background", source_value="New wording", current_value=current_value,
        current_value_hash=hashlib.sha256(current_value.encode("utf-8")).hexdigest(),
        confidence="high", batch_eligible=False, extraction_method_rationale="test",
        conflict_key="workstream_registry:lane-a:background",
    )


def _write_registry(root: Path, *, background: str = "Old wording") -> None:
    path = root / "armada" / "workstream_registry.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "schema_version: '1.0'\nworkstreams:\n  - id: lane-a\n    name: Lane A\n"
        f"    background: {background}\n",
        encoding="utf-8",
    )


def test_renders_explicit_non_mutating_registry_diff(tmp_path: Path) -> None:
    _write_registry(tmp_path)

    diff = render_workstream_registry_manual_diff(_proposal(), programs_root=tmp_path)

    assert "Manual-only change" in diff.text
    assert "-  background: Old wording" in diff.text
    assert "+  background: New wording" in diff.text
    assert (tmp_path / "armada" / "workstream_registry.yaml").read_text(encoding="utf-8").endswith("background: Old wording\n")


def test_rejects_stale_or_non_scalar_manual_registry_edits(tmp_path: Path) -> None:
    _write_registry(tmp_path, background="Changed elsewhere")
    with pytest.raises(ManualRegistryDiffError, match="stale"):
        render_workstream_registry_manual_diff(_proposal(), programs_root=tmp_path)

    path = tmp_path / "armada" / "workstream_registry.yaml"
    path.write_text(
        "schema_version: '1.0'\nworkstreams:\n  - id: lane-a\n    background: [not, scalar]\n",
        encoding="utf-8",
    )
    with pytest.raises(ManualRegistryDiffError, match="not scalar"):
        render_workstream_registry_manual_diff(
            _proposal(current_value="ignored"), programs_root=tmp_path
        )

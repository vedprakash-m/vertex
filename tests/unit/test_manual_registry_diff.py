from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.core.manual_registry_diff import ManualRegistryDiffError, render_workstream_registry_manual_diff
from src.core.ncfl_models import ContextUpdateProposal


def _proposal(*, current_value: str = "Old wording") -> ContextUpdateProposal:
    return ContextUpdateProposal(
        proposal_id="proposal-1", program_id="contoso", issue_number=1, edition_id="contoso_weekly",
        source_type="confirmed_overrides", extracted_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        extractor_version="1.0.0", source_artifact="overrides/issue_001.yaml", source_field="summary",
        extraction_method="overrides_yaml", target_store="workstream_registry", target_key="lane-a",
        target_field="background", source_value="New wording", current_value=current_value,
        current_value_hash=hashlib.sha256(current_value.encode("utf-8")).hexdigest(),
        confidence="high", batch_eligible=False, extraction_method_rationale="test",
        conflict_key="workstream_registry:lane-a:background",
    )


def _write_registry(root: Path, *, background: str = "Old wording") -> None:
    path = root / "contoso" / "workstream_registry.yaml"
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
    assert (tmp_path / "contoso" / "workstream_registry.yaml").read_text(encoding="utf-8").endswith("background: Old wording\n")


def test_rejects_stale_or_non_scalar_manual_registry_edits(tmp_path: Path) -> None:
    _write_registry(tmp_path, background="Changed elsewhere")
    with pytest.raises(ManualRegistryDiffError, match="stale"):
        render_workstream_registry_manual_diff(_proposal(), programs_root=tmp_path)

    path = tmp_path / "contoso" / "workstream_registry.yaml"
    path.write_text(
        "schema_version: '1.0'\nworkstreams:\n  - id: lane-a\n    background: [not, scalar]\n",
        encoding="utf-8",
    )
    with pytest.raises(ManualRegistryDiffError, match="not scalar"):
        render_workstream_registry_manual_diff(
            _proposal(current_value="ignored"), programs_root=tmp_path
        )


@pytest.mark.parametrize("field", ["deep_context.why", "deep_context.what", "deep_context.how"])
@pytest.mark.parametrize("value", ["new: value\nsecond line", ("long text 🎯 " * 30) + "\nend"])
def test_allowlisted_nested_diff_preserves_legacy_hash_and_quotes(tmp_path: Path, field: str, value: str) -> None:
    _write_registry(tmp_path)
    path = tmp_path / "contoso" / "workstream_registry.yaml"
    path.write_text(path.read_text() + f"    deep_context:\n      {field.split('.')[1]}: Old wording\n")
    proposal = replace(_proposal(), target_field=field, source_value=value)
    before = path.read_bytes()
    diff = render_workstream_registry_manual_diff(proposal, programs_root=tmp_path)
    assert field in diff.text
    assert f"+    {field.split('.')[1]}:" in diff.text
    assert f"+  {field}:" not in diff.text
    new_line = next(line for line in diff.text.splitlines() if line.startswith("+    "))
    assert yaml.safe_load(new_line[5:]) == {field.split(".")[1]: proposal.source_value}
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["deep_context.other", "roles.email", "deep_context.why.extra"])
def test_other_nested_paths_remain_rejected(tmp_path: Path, field: str) -> None:
    _write_registry(tmp_path)
    with pytest.raises(ManualRegistryDiffError, match="nested"):
        render_workstream_registry_manual_diff(replace(_proposal(), target_field=field), programs_root=tmp_path)


@pytest.mark.parametrize("change", ["store", "key", "field", "missing-file", "bad-shape", "missing-field", "bad-container"])
def test_manual_diff_reports_invalid_target(tmp_path: Path, change: str) -> None:
    _write_registry(tmp_path)
    proposal = _proposal()
    path = tmp_path / "contoso" / "workstream_registry.yaml"
    if change == "store":
        proposal = replace(proposal, target_store="workstreams")
    elif change == "key":
        proposal = replace(proposal, target_key="")
    elif change == "field":
        proposal = replace(proposal, target_field="")
    elif change == "missing-file":
        path.unlink()
    elif change == "bad-shape":
        path.write_text("[]")
    elif change == "missing-field":
        proposal = replace(proposal, target_field="missing")
    else:
        path.write_text("workstreams:\n- id: lane-a\n  deep_context: null\n")
        proposal = replace(proposal, target_field="deep_context.why")
    with pytest.raises(ManualRegistryDiffError):
        render_workstream_registry_manual_diff(proposal, programs_root=tmp_path)


@pytest.mark.parametrize("value", ["null", "'<null>'"])
def test_legacy_null_and_literal_null_label_hashes_remain_compatible(tmp_path: Path, value: str) -> None:
    _write_registry(tmp_path, background=value)
    proposal = _proposal(current_value="" if value == "null" else "<null>")
    diff = render_workstream_registry_manual_diff(proposal, programs_root=tmp_path)
    assert diff.current_value == ("" if value == "null" else "<null>")


def test_legacy_proposal_without_hash_keeps_nonmutating_diff(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    proposal = replace(_proposal(), current_value_hash=None)
    assert render_workstream_registry_manual_diff(proposal, programs_root=tmp_path).current_value == "Old wording"


@pytest.mark.parametrize("baseline", [
    None, "", "a" * 64, "intake-v2:" + "a" * 64, "intake-v1:" + "A" * 64,
    "intake-v1:" + "a" * 63, "intake-v1:" + "g" * 64,
    "intake-v1:" + "a" * 64 + "\n", False, 1, [],
])
def test_reply_requires_canonical_baseline_before_registry_read(
    tmp_path: Path, baseline: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_read(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid reply baseline must reject before registry read")

    monkeypatch.setattr("src.core.manual_registry_diff._read_registry_value", unexpected_read)
    proposal = replace(_proposal(), source_type="context_gap_reply", current_value_hash=baseline)
    with pytest.raises(ManualRegistryDiffError, match="canonical intake-v1"):
        render_workstream_registry_manual_diff(proposal, programs_root=tmp_path)


@pytest.mark.parametrize("program", ["..", "bad/name", "bad\\name", "", "sample:stream"])
def test_manual_baseline_rejects_invalid_program_identifier(tmp_path: Path, program: str) -> None:
    from src.core.manual_registry_diff import read_registry_baseline
    with pytest.raises(ManualRegistryDiffError, match="program"):
        read_registry_baseline(program, "lane-a", "deep_context.why", programs_root=tmp_path)

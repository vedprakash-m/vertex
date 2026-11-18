from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.test_context_extract_workspace_containment import (
    _bytes,
    _run,
    _seed,
    installation,
)


@pytest.mark.parametrize("case", ("selected", "explicit", "captured", "empty", "legacy", "stage"))
def test_actual_extract_selected_decisions_risks_and_empty(
    tmp_path: Path, installation: Path, case: str,
) -> None:
    root = tmp_path / "selected" / "programs"
    _seed(root, empty=case == "empty")
    args = ["context", "extract", "--edition", "synthetic_weekly", "--issue", "1"]
    if case != "stage":
        args.append("--dry-run")
    if case == "explicit":
        args += ["--programs-root", str(root), "--reports-root", str(root.parent / "reports")]
    before = _bytes(root, installation / "programs")
    result = _run(installation, None if case == "legacy" else root, case, args)
    assert result["code"] == 0, result
    assert not result["effects"], result
    assert _bytes(root, installation / "programs") == before
    if case == "empty":
        assert "extracted 0 proposal(s)" in result["output"]
    elif case == "stage":
        assert result["staged"] == [{
            "program": "synthetic", "issue": 1, "root": str(root),
            "values": ["SELECTED_DECISION", "high"],
        }]
        assert "Staged 2 extracted proposal(s)" in result["output"]
    else:
        marker = "AMBIENT" if case == "legacy" else "SELECTED"
        assert f"{marker}_DECISION" in result["output"]
        assert f"{marker}_DIMENSION.dimension_risk_level | high | high" in result["output"]
        assert ("SELECTED" if marker == "AMBIENT" else "AMBIENT") not in result["output"]
    if case != "legacy":
        assert not any(Path(path).is_relative_to(installation / "programs") for path in result["reads"])

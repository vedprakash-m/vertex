from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core import quality_gates


@pytest.mark.parametrize("content_hash_issue_count", [0, 1])
def test_hash_chain_failure_is_never_forceable(
    monkeypatch: pytest.MonkeyPatch, content_hash_issue_count: int,
) -> None:
    verifier = Mock(return_value=SimpleNamespace(
        ok=False, content_hash_issue_count=content_hash_issue_count,
        checked_event_count=2, issues=("broken chain",),
    ))
    monkeypatch.setattr(quality_gates, "verify_event_log", verifier)
    root = Path("selected-programs")
    gate = quality_gates._evaluate_hash_chain_integrity_gate(program_id="fixture", programs_root=root)
    assert not gate.passed
    assert not gate.forceable
    assert "--force" not in gate.message
    verifier.assert_called_once_with("fixture", programs_root=root)

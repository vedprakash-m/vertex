from __future__ import annotations

from types import SimpleNamespace

from src.commands.actuate import _dispatch_execution


def test_direct_dispatch_is_blocked_by_default(monkeypatch) -> None:
    monkeypatch.delenv("VERTEX_ALLOW_DIRECT_ACTUATION", raising=False)
    result = _dispatch_execution(
        SimpleNamespace(adapter="ado", operation="comment", payload={}, entity_ref="wi:1", rule_id="rule"),
        object(),
        object(),
    )
    assert result.success is False
    assert "VERTEX_ALLOW_DIRECT_ACTUATION" in (result.error_message or "")

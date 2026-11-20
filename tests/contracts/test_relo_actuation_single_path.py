from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from src.core.ado_actuation_adapter import AdoActuationAdapter
from src.commands.actuate import _dispatch_execution


def test_direct_dispatch_is_blocked_by_default(monkeypatch) -> None:
    monkeypatch.delenv("VERTEX_ALLOW_DIRECT_ACTUATION", raising=False)
    result = _dispatch_execution(
        SimpleNamespace(adapter="ado", operation="comment", payload={}, entity_ref="wi:1", rule_id="rule"),
        object(),
        object(),
    )
    assert result.success is False
    assert "outbox" in (result.error_message or "").lower()


def test_environment_override_cannot_reenable_direct_dispatch(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_ALLOW_DIRECT_ACTUATION", "1")
    result = _dispatch_execution(
        SimpleNamespace(adapter="ado", operation="comment", payload={}, entity_ref="wi:1", rule_id="rule"),
        object(),
        object(),
    )
    assert result.success is False
    assert "outbox" in (result.error_message or "").lower()


def test_adapter_live_execution_is_provider_contained() -> None:
    called = False

    def client_factory():
        nonlocal called
        called = True
        raise AssertionError("provider client must remain behind the outbox dispatcher")

    result = AdoActuationAdapter(ado_client_fn=client_factory).execute(
        "comment", {"record_id": "1", "text": "synthetic"}
    )
    assert result.success is False
    assert "outbox" in (result.error_message or "").lower()
    assert called is False


def test_retired_direct_provider_calls_are_absent_from_command_paths() -> None:
    repo = Path(__file__).resolve().parents[2]
    forbidden = {
        "AdoActuationAdapter",
        "GraphSendClient",
        "TeamsWebhookClient",
        "send_mail",
        "post_card",
    }
    paths = (
        repo / "src" / "commands" / "actuate.py",
        repo / "src" / "commands" / "report_email.py",
        repo / "src" / "commands" / "notify.py",
        repo / "src" / "commands" / "escalate.py",
        repo / "src" / "commands" / "review_full.py",
        repo / "src" / "commands" / "confirm_stages" / "weekly_summary_card.py",
    )
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        identifiers = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        assert not (identifiers & forbidden), path


def test_ado_writer_has_only_the_create_provider_primitive() -> None:
    path = Path(__file__).resolve().parents[2] / "src" / "m365" / "ado_writer.py"
    source = path.read_text(encoding="utf-8")
    assert "_dispatch_mutation_via_outbox" not in source
    assert "_dispatch_ado_mutation" not in source
    assert "_ROUTE_ADO_MUTATIONS_VIA_OUTBOX" not in source
    assert source.count("self._request_json(") == 1

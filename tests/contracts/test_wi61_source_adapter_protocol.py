"""WI-6.1: Contract tests — SourceAdapter Protocol + NullAdapter + factory.

Acceptance:
  - SourceAdapter Protocol is @runtime_checkable (isinstance works)
  - NullAdapter satisfies SourceAdapter (isinstance passes)
  - NullAdapter.fetch() returns empty signals (empty-yield legal — O-7)
  - ActuationAdapter Protocol is @runtime_checkable
  - NullActuationAdapter satisfies ActuationAdapter
  - build_source_adapter() factory returns SourceAdapter for any channel
  - integration_protocol.py does not import from src.ai or src.m365 (import ratchet)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.integration_protocol import ActuationAdapter, ActuationResult, SourceAdapter
from src.core.integration_types import RunContext
from src.core.null_adapter import NullAdapter, NullActuationAdapter, build_source_adapter

_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


class TestSourceAdapterProtocol:
    def test_null_adapter_is_source_adapter(self) -> None:
        adapter = NullAdapter()
        assert isinstance(adapter, SourceAdapter)

    def test_null_adapter_channel_property(self) -> None:
        adapter = NullAdapter(channel="test-channel")
        assert adapter.channel == "test-channel"

    def test_null_adapter_default_channel(self) -> None:
        adapter = NullAdapter()
        assert adapter.channel == "null"

    def test_null_adapter_fetch_returns_extraction_result(self) -> None:
        adapter = NullAdapter()
        result = adapter.fetch("test-prog", config=None, since=_NOW, run_ctx=RunContext())
        from src.core.integration_types import ExtractionResult
        assert isinstance(result, ExtractionResult)

    def test_null_adapter_fetch_empty_yield_is_legal(self) -> None:
        """O-7: NullAdapter returns empty signals — always legal."""
        adapter = NullAdapter()
        result = adapter.fetch("test-prog", config=None, since=_NOW)
        assert result.signals == ()
        assert result.errors == ()
        assert result.trajectory_points == ()
        assert result.side_artifacts == {}

    def test_null_adapter_channel_passes_through_to_result(self) -> None:
        adapter = NullAdapter(channel="kusto")
        result = adapter.fetch("prog", config=None, since=_NOW)
        assert result.channel == "kusto"

    def test_custom_object_missing_channel_is_not_source_adapter(self) -> None:
        class BadAdapter:
            def fetch(self, program_id, config, since, run_ctx=None):
                pass  # Missing channel property

        assert not isinstance(BadAdapter(), SourceAdapter)

    def test_object_with_both_channel_and_fetch_is_source_adapter(self) -> None:
        class MinimalAdapter:
            @property
            def channel(self) -> str:
                return "minimal"

            def fetch(self, program_id, config, since, run_ctx=RunContext()):
                from src.core.integration_types import ExtractionResult
                return ExtractionResult(channel="minimal", signals=(), trajectory_points=(), side_artifacts={}, errors=())

        assert isinstance(MinimalAdapter(), SourceAdapter)


class TestActuationAdapterProtocol:
    def test_null_actuation_adapter_is_actuation_adapter(self) -> None:
        adapter = NullActuationAdapter()
        assert isinstance(adapter, ActuationAdapter)

    def test_null_actuation_adapter_dry_run(self) -> None:
        adapter = NullActuationAdapter()
        result = adapter.execute("state_transition", {"work_item_id": 123}, dry_run=True)
        assert isinstance(result, ActuationResult)
        assert result.success is True
        assert result.dry_run is True
        assert result.error_message is None

    def test_null_actuation_adapter_live_is_also_safe(self) -> None:
        adapter = NullActuationAdapter()
        result = adapter.execute("comment", {"text": "test"}, dry_run=False)
        assert result.success is True
        assert result.dry_run is False


class TestBuildSourceAdapterFactory:
    def test_null_channel_returns_null_adapter(self) -> None:
        adapter = build_source_adapter("null")
        assert isinstance(adapter, SourceAdapter)
        assert isinstance(adapter, NullAdapter)

    def test_unknown_channel_returns_null_adapter_not_error(self) -> None:
        """Factory falls back to NullAdapter for unknown channels — never crashes."""
        adapter = build_source_adapter("some-future-channel-not-yet-wired")
        assert isinstance(adapter, SourceAdapter)

    def test_factory_result_satisfies_empty_yield(self) -> None:
        adapter = build_source_adapter("null")
        result = adapter.fetch("test-prog", config=None, since=_NOW)
        assert result.signals == ()

    def test_factory_preserves_channel_for_unknown(self) -> None:
        adapter = build_source_adapter("ado")
        assert adapter.channel == "ado"


class TestImportRatchet:
    def test_integration_protocol_does_not_import_from_ai(self) -> None:
        """INV-1: Zone A must not import from Zone B (src/ai)."""
        import ast
        from pathlib import Path
        src = (Path(__file__).parent.parent.parent / "src" / "core" / "integration_protocol.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = ""
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith("src.ai"), (
                            f"integration_protocol.py imports from src.ai: {alias.name}"
                        )
                assert not module.startswith("src.ai"), (
                    f"integration_protocol.py imports from src.ai: {module}"
                )
                assert not module.startswith("src.m365"), (
                    f"integration_protocol.py imports from src.m365: {module}"
                )

    def test_null_adapter_does_not_import_from_ai(self) -> None:
        """INV-1: NullAdapter (Zone A) must not import from Zone B."""
        import ast
        from pathlib import Path
        src = (Path(__file__).parent.parent.parent / "src" / "core" / "null_adapter.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("src.ai"), (
                    f"null_adapter.py imports from src.ai: {node.module}"
                )
                assert not node.module.startswith("src.m365"), (
                    f"null_adapter.py imports from src.m365: {node.module}"
                )

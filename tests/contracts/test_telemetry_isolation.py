from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.ai_telemetry import AiTelemetryRecord, AiTelemetryStatus, append_ai_telemetry


def test_synthetic_write_to_isolated_root_succeeds(tmp_path) -> None:
    append_ai_telemetry(
        AiTelemetryRecord(datetime.now(timezone.utc), "unit-test", "trace-model", AiTelemetryStatus.OK, "demo"),
        programs_root=tmp_path,
    )
    assert (tmp_path / "demo" / "_state" / "ai_telemetry.jsonl").exists()


def test_synthetic_write_to_live_program_tree_raises() -> None:
    with pytest.raises(ValueError, match="Synthetic AI telemetry"):
        append_ai_telemetry(
            AiTelemetryRecord(datetime.now(timezone.utc), "unit-test", "trace-model", AiTelemetryStatus.OK, "acme_weekly")
        )

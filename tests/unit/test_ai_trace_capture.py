from __future__ import annotations

import json
from pathlib import Path

from src.ai.safety.ai_trace_capture import capture_ai_io, is_full_io_capture_enabled


def test_disabled_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VERTEX_AI_TRACE_FULL_IO", raising=False)
    assert not is_full_io_capture_enabled()

    capture_file = tmp_path / "llm_trace_full_io.jsonl"
    capture_ai_io(
        edition="acme_weekly",
        run_id="run-1",
        caller="test_caller",
        prompt_text="raw prompt jane@example.com",
        response_text="raw response",
        capture_file=capture_file,
    )
    assert not capture_file.exists()


def test_enabled_writes_sanitized_record(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VERTEX_AI_TRACE_FULL_IO", "1")
    assert is_full_io_capture_enabled()

    capture_file = tmp_path / "llm_trace_full_io.jsonl"
    capture_ai_io(
        edition="acme_weekly",
        run_id="run-1",
        caller="test_caller",
        prompt_version="claim_extractor.v1",
        prompt_text="Contact jane.doe@example.com about the milestone.",
        response_text="Milestone confirmed on track.",
        capture_file=capture_file,
    )

    assert capture_file.exists()
    lines = capture_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["classification"] == "sanitized-excerpt"
    assert record["prompt_version"] == "claim_extractor.v1"
    assert "jane.doe@example.com" not in record["prompt"]["excerpt"]
    assert record["prompt"]["pii_detected"] is True
    assert record["response"]["excerpt"] == "Milestone confirmed on track."
    assert record["response"]["pii_detected"] is False


def test_various_truthy_env_values(monkeypatch) -> None:
    for value in ("1", "true", "True", "yes", "YES"):
        monkeypatch.setenv("VERTEX_AI_TRACE_FULL_IO", value)
        assert is_full_io_capture_enabled(), f"{value!r} should enable capture"
    monkeypatch.setenv("VERTEX_AI_TRACE_FULL_IO", "0")
    assert not is_full_io_capture_enabled()
    monkeypatch.setenv("VERTEX_AI_TRACE_FULL_IO", "")
    assert not is_full_io_capture_enabled()


def test_capture_failure_does_not_raise(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_AI_TRACE_FULL_IO", "1")
    # An unwritable path (a directory component that's actually a file)
    # should be swallowed, not raised — corpus capture must never break a
    # live AI call.
    bogus_parent = Path(__file__)  # a file, not a directory
    capture_ai_io(
        edition="acme_weekly",
        run_id="run-1",
        caller="test_caller",
        prompt_text="x",
        response_text="y",
        capture_file=bogus_parent / "sub" / "llm_trace_full_io.jsonl",
    )

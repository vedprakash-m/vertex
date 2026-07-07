from __future__ import annotations

import json

from src.ai.llm_trace import default_trace_path, llm_trace


EDITION_NAME = "acme_weekly"


def test_llm_trace_is_disabled_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VERTEX_LLM_TRACE", raising=False)
    monkeypatch.delenv("VERTEX_LLM_TRACE", raising=False)
    trace_file = tmp_path / "trace.jsonl"

    llm_trace(
        edition=EDITION_NAME,
        run_id="run-001",
        caller="src.ai.client.AIClient.chat",
        model="gpt-4o-mini",
        trace_file=trace_file,
    )

    assert trace_file.exists() is False


def test_llm_trace_writes_expected_record_when_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VERTEX_LLM_TRACE", raising=False)
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    trace_file = tmp_path / "trace.jsonl"

    llm_trace(
        edition=EDITION_NAME,
        run_id="run-001",
        caller="src.ai.client.AIClient.chat",
        model="gpt-4o-mini",
        deployment="exec-summary",
        prompt_version="exec_summary_drafter.v1",
        prompt_tokens=100,
        completion_tokens=25,
        latency_ms=123.456,
        cost_usd=0.0123456,
        metadata={"workstream": "StorageX"},
        trace_file=trace_file,
    )

    record = json.loads(trace_file.read_text(encoding="utf-8").strip())

    assert record["edition"] == EDITION_NAME
    assert record["run_id"] == "run-001"
    assert record["caller"] == "src.ai.client.AIClient.chat"
    assert record["model"] == "gpt-4o-mini"
    assert record["deployment"] == "exec-summary"
    assert record["prompt_version"] == "exec_summary_drafter.v1"
    assert record["prompt_tokens"] == 100
    assert record["completion_tokens"] == 25
    assert record["total_tokens"] == 125
    assert record["latency_ms"] == 123.5
    assert record["cost_usd"] == 0.012346
    assert record["metadata"] == {"workstream": "StorageX"}


def test_default_trace_path_uses_output_tree() -> None:
    path = default_trace_path(EDITION_NAME)

    assert path.as_posix().endswith("publications/acme_weekly/ai/llm_trace.jsonl")


def test_llm_trace_writes_expected_record_when_vertex_alias_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VERTEX_LLM_TRACE", raising=False)
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    trace_file = tmp_path / "trace.jsonl"

    llm_trace(
        edition=EDITION_NAME,
        run_id="run-vertex",
        caller="src.ai.client.AIClient.chat",
        model="gpt-4o-mini",
        trace_file=trace_file,
    )

    record = json.loads(trace_file.read_text(encoding="utf-8").strip())

    assert record["run_id"] == "run-vertex"
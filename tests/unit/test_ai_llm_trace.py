from __future__ import annotations

import json
import logging
from pathlib import Path

import portalocker
import pytest

from src.ai.llm_trace import default_trace_path, llm_trace


EDITION_NAME = "acme_weekly"


def test_llm_trace_is_disabled_by_default(tmp_path, monkeypatch, caplog) -> None:
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
    assert not [record for record in caplog.records if record.name == "src.ai.llm_trace"]


def test_llm_trace_writes_expected_record_when_enabled(tmp_path, monkeypatch, caplog) -> None:
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
    assert not [record for record in caplog.records if record.name == "src.ai.llm_trace"]


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


def test_llm_trace_reservation_id_is_optional_and_separate_from_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    trace_file = tmp_path / "trace.jsonl"
    common = {
        "edition": EDITION_NAME,
        "run_id": "run-001",
        "caller": "test",
        "model": "test-model",
        "metadata": {"cost_reservation_id": 999},
        "trace_file": trace_file,
    }

    llm_trace(**common)
    llm_trace(**common, cost_reservation_id=7)

    first, second = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
    assert "cost_reservation_id" not in first
    assert second["cost_reservation_id"] == 7
    assert first["metadata"] == second["metadata"] == {"cost_reservation_id": 999}


def test_llm_trace_reservation_id_preserves_prompt_learning_readers(tmp_path: Path) -> None:
    from src.ai.edit_learner import _parse_prompt_learning_trace_record as parse_learner
    from src.commands.audit import _parse_prompt_learning_trace_record as parse_audit

    record = {
        "timestamp": "2026-11-11T00:00:00+00:00",
        "edition": EDITION_NAME,
        "run_id": "run-001",
        "model": "test-model",
        "metadata": {"issue_number": 1, "section_id": "exec_summary", "task_type": "exec_summary"},
    }
    with_reservation = {**record, "cost_reservation_id": 7}

    assert parse_learner(with_reservation, trace_path=tmp_path / "trace.jsonl") == parse_learner(
        record, trace_path=tmp_path / "trace.jsonl",
    )
    assert parse_audit(with_reservation) == parse_audit(record)


@pytest.mark.parametrize("failure", ["open", "serialize", "lock", "fsync", "unlock"])
def test_llm_trace_write_failure_is_visible_without_sensitive_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str,
) -> None:
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    trace_file = tmp_path / "synthetic-private-path" / "trace.jsonl"
    metadata: dict[str, object] = {"value": "synthetic-private-metadata"}

    def fail(*_args: object, **_kwargs: object) -> None:
        if failure == "fsync":
            raise OSError("synthetic-private-exception")
        raise portalocker.exceptions.LockException("synthetic-private-exception")

    if failure == "open":
        trace_file.mkdir(parents=True)
    elif failure == "serialize":
        metadata["cycle"] = metadata
    elif failure == "lock":
        monkeypatch.setattr("src.ai.llm_trace.portalocker.lock", fail)
    elif failure == "fsync":
        monkeypatch.setattr("src.ai.llm_trace.os.fsync", fail)
    else:
        monkeypatch.setattr("src.ai.llm_trace.portalocker.unlock", fail)

    caplog.set_level(logging.WARNING, logger="src.ai.llm_trace")
    llm_trace(
        edition=EDITION_NAME,
        run_id="run-001",
        caller="test",
        model="test-model",
        error="synthetic-private-error",
        metadata=metadata,
        trace_file=trace_file,
    )

    records = [record for record in caplog.records if record.name == "src.ai.llm_trace"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage() == "LLM trace write failed."
    assert records[0].exc_info is None
    assert "synthetic-private" not in caplog.text
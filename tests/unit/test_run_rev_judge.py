"""Unit tests for scripts/run_rev_judge.py (specs/backlog.md WO-4 follow-up:
wiring src.ai.rev.judge.judge_extractions() into an actual runnable command,
since the inventory found it had no production/script caller anywhere).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from scripts.run_rev_judge import _extract_claims, _hydrate, _load_corpus, main
from src.ai.rev.extractor import DeterministicRevExtractor, LLMRevExtractor
from src.core.rev.result import Forbidden, Success


class _FakeJudgeClient:
    """Controllable LLMProvider for both the LLM extractor and the judge."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls = 0

    def chat(self, system: str, user: str, **kwargs: Any) -> str:
        return ""

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], Any],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> Any:
        self.calls += 1
        return parser(self._response)


def _write_corpus(tmp_path: Path) -> Path:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "msg-001.txt").write_text(
        "Contoso deployment update\n\n"
        "The Contoso production deployment completed successfully on 2026-07-20.\n",
        encoding="utf-8",
    )
    (corpus_dir / "ground_truth.json").write_text(
        json.dumps({"msg-001": ["Contoso production deployment completed on 2026-07-20"]}),
        encoding="utf-8",
    )
    return corpus_dir


def test_load_corpus_parses_messages_and_ground_truth(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)

    messages, canonical_texts, ground_truth = _load_corpus(corpus_dir)

    assert messages == [{"message_id": "msg-001", "subject": "Contoso deployment update"}]
    assert "Contoso production deployment completed successfully" in canonical_texts["msg-001"]
    assert ground_truth == {"msg-001": ["Contoso production deployment completed on 2026-07-20"]}


def test_load_corpus_without_ground_truth_file(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "msg-002.txt").write_text("Some subject\nBody text.\n", encoding="utf-8")

    messages, _canonical_texts, ground_truth = _load_corpus(corpus_dir)

    assert messages == [{"message_id": "msg-002", "subject": "Some subject"}]
    assert ground_truth == {}


def test_hydrate_produces_content_the_deterministic_extractor_can_consume() -> None:
    text = "The Contoso production deployment completed successfully on 2026-07-20."
    hydrated = _hydrate("msg-001", text)

    assert hydrated.canonical_text == text
    assert hydrated.chunks  # non-empty for non-trivial text

    result = DeterministicRevExtractor().extract(hydrated, correlation_id="test")
    assert isinstance(result, Success)


def test_extract_claims_degrades_gracefully_on_non_success_result() -> None:
    class _AlwaysForbidden:
        def extract(self, hydrated, *, correlation_id: str):
            return Forbidden(scope="test", reason="denied")

    claims = _extract_claims(_AlwaysForbidden(), _hydrate("msg-001", "text"), correlation_id="test")
    assert claims == ()


def test_main_runs_full_comparison_and_writes_report(tmp_path: Path, monkeypatch) -> None:
    corpus_dir = _write_corpus(tmp_path)
    out_path = tmp_path / "rev-judge-report.json"

    llm_client = _FakeJudgeClient({"events": []})
    judge_client = _FakeJudgeClient({
        "extractor_a": {"scores": [], "precision": 1.0, "recall": 1.0},
        "extractor_b": {"scores": [], "precision": 1.0, "recall": 1.0},
        "ground_truth_coverage": [{"fact": "Contoso production deployment completed on 2026-07-20", "captured_by": "both"}],
        "summary": "Both extractors agree.",
    })

    monkeypatch.setattr(LLMRevExtractor, "from_env", staticmethod(lambda **kwargs: LLMRevExtractor(client=llm_client)))
    monkeypatch.setattr("scripts.run_rev_judge._resolve_judge_client", lambda: judge_client)

    argv = ["--corpus-dir", str(corpus_dir), "--out", str(out_path)]
    monkeypatch.setattr("sys.argv", ["run_rev_judge.py", *argv])

    exit_code = main()

    assert exit_code == 0
    assert out_path.exists()
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["extractor_a_name"] == "deterministic"
    assert payload["extractor_b_name"] == "llm"
    assert judge_client.calls == 1


def test_main_fails_closed_when_corpus_dir_missing(tmp_path: Path, monkeypatch) -> None:
    argv = ["--corpus-dir", str(tmp_path / "does-not-exist")]
    monkeypatch.setattr("sys.argv", ["run_rev_judge.py", *argv])

    assert main() == 2

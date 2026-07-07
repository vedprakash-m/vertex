"""P2-9 — REV quality-floor pre-commit gate unit tests.

Exercises ``scripts/rev_precommit_quality.py``: change-relevance detection,
advisory-skip when no corpus exists, pass-through to the G-floor gate, and the
``--changed-files`` file-read path. The heavy stage/corpus helpers are reused
from ``test_rev_quality_check`` so the staged candidates reflect the real
pipeline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.rev_precommit_quality import (
    LATENCY_TARGET_SECONDS,
    RELEVANT_PATHS,
    is_relevant_change,
    main as pc_main,
)

# Reuse the real-cycle stage + corpus helpers from the quality-check suite.
from tests.unit.test_rev_quality_check import _label_rows, _stage, _write_corpus


class TestRelevanceDetection:
    def test_extractor_change_is_relevant(self) -> None:
        assert is_relevant_change(["src/ai/rev/extractor.py"]) is True

    def test_prompt_assets_are_relevant(self) -> None:
        assert is_relevant_change(["src/ai/prompts/rev_extractor.v1.txt"]) is True
        assert is_relevant_change(["src/ai/prompts/registry.yaml"]) is True

    def test_unrelated_change_is_not_relevant(self) -> None:
        assert is_relevant_change(["README.md", "src/core/rev/pipeline.py"]) is False

    def test_rev_package_py_change_is_relevant_broad_guard(self) -> None:
        # A sibling module change under src/ai/rev/ trips the broad guard so a
        # regression in a dependency is caught.
        assert is_relevant_change(["src/ai/rev/judge.py"]) is True

    def test_empty_and_none_safe(self) -> None:
        assert is_relevant_change([]) is False
        assert is_relevant_change([""]) is False

    def test_windows_backslash_paths_normalized(self) -> None:
        assert is_relevant_change(["src\\ai\\rev\\extractor.py"]) is True

    def test_relevant_paths_constant_covers_core_files(self) -> None:
        assert "src/ai/rev/extractor.py" in RELEVANT_PATHS


class TestGateSkipAndAdvisory:
    def test_no_relevant_change_skips_silently(self, tmp_path: Path) -> None:
        # No corpus needed — the gate short-circuits before touching disk.
        rc = pc_main(["--program", "p-skip", "--programs-root", str(tmp_path)],
                     changed_files=["docs/runbook.md"])
        assert rc == 0

    def test_relevant_change_but_no_corpus_is_advisory_exit_zero(
        self, tmp_path: Path, capsys
    ) -> None:
        rc = pc_main(
            ["--program", "p-nocorpus", "--programs-root", str(tmp_path)],
            changed_files=["src/ai/rev/extractor.py"],
        )
        assert rc == 0  # never block before OA-3 labels exist
        err = capsys.readouterr().err
        assert "advisory" in err.lower()


class TestGateRunsOnCorpus:
    def test_passing_corpus_exits_zero(self, tmp_path: Path) -> None:
        bodies = [f"Deployment completed 2026-06-{20+i:02d}." for i in range(5)]
        cands = _stage("p-pc-pass", tmp_path, bodies)
        proposed = {c.candidate_id: c.proposed_event_type for c in cands}
        _write_corpus(tmp_path, "p-pc-pass", _label_rows(cands, proposed, label="accept"))
        rc = pc_main(
            ["--program", "p-pc-pass", "--programs-root", str(tmp_path)],
            changed_files=["src/ai/rev/extractor.py"],
        )
        assert rc == 0

    def test_failing_corpus_exits_one(self, tmp_path: Path) -> None:
        # 5 candidates; label 3 with a wrong expected type → xtract-prec 2/5 < 0.80.
        bodies = [f"Deployment completed 2026-06-{20+i:02d}." for i in range(5)]
        cands = _stage("p-pc-fail", tmp_path, bodies)
        proposed = {c.candidate_id: c.proposed_event_type for c in cands}
        cids = list(proposed)
        expected_map = dict(proposed)
        for cid in cids[2:]:
            expected_map[cid] = "some.wrong.type.v1"
        _write_corpus(tmp_path, "p-pc-fail", _label_rows(cands, expected_map, label="accept"))
        rc = pc_main(
            ["--program", "p-pc-fail", "--programs-root", str(tmp_path)],
            changed_files=["src/ai/rev/extractor.py"],
        )
        assert rc == 1


class TestChangedFilesPath:
    def test_changed_files_file_is_read(self, tmp_path: Path) -> None:
        # Relevant change recorded in a file → advisory skip (no corpus).
        cf = tmp_path / "staged.txt"
        cf.write_text("src/ai/rev/extractor.py\nsrc/core/rev/pipeline.py\n",
                      encoding="utf-8")
        rc = pc_main(
            ["--program", "p-cf", "--programs-root", str(tmp_path),
             "--changed-files", str(cf)],
        )
        assert rc == 0  # advisory (no corpus)

    def test_changed_files_file_irrelevant_skips(self, tmp_path: Path) -> None:
        cf = tmp_path / "staged.txt"
        cf.write_text("README.md\n", encoding="utf-8")
        rc = pc_main(
            ["--program", "p-cf2", "--programs-root", str(tmp_path),
             "--changed-files", str(cf)],
        )
        assert rc == 0

    def test_missing_changed_files_file_does_not_block(self, tmp_path: Path) -> None:
        rc = pc_main(
            ["--program", "p-cf3", "--programs-root", str(tmp_path),
             "--changed-files", str(tmp_path / "nope.txt")],
        )
        assert rc == 0


def test_latency_target_constant_is_30_seconds() -> None:
    assert LATENCY_TARGET_SECONDS == 30.0
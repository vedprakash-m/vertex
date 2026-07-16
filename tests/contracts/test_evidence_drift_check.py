"""Contract test for ADF-W0.13: evidence-drift check on frozen corpus artifacts.

This test file *is* the CI/pre-commit hook for
``scripts/adf_evidence_drift_check.py``: it runs in the same pytest suite
CI already executes, so no separate pre-commit framework or workflow step
is introduced (this repo has no ``.pre-commit-config.yaml`` and adding one
for a single check would be more machinery than the check warrants).
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.adf_evidence_drift_check import check_corpus_freeze_drift, check_counterfactual_freeze_drift, main
from scripts.verify_activation import (
    build_corpus_freeze_manifest,
    write_corpus_freeze_manifest,
    write_counterfactual_freeze_manifest,
)


def test_never_frozen_program_is_not_reported_as_drift(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    outcome, message = check_corpus_freeze_drift(program="fixture_prog", programs_root=programs_root, repo_root=tmp_path)
    assert outcome == "never_frozen"
    assert "missing" in message


def test_clean_freeze_matches_current_corpus(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    quality_dir = programs_root / "fixture_prog" / "_quality"
    quality_dir.mkdir(parents=True)
    (quality_dir / "rev_labeled_corpus.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (quality_dir / "corpus_manifest.jsonl").write_text('{"b": 2}\n', encoding="utf-8")

    write_corpus_freeze_manifest(program="fixture_prog", programs_root=programs_root, repo_root=tmp_path)

    outcome, message = check_corpus_freeze_drift(program="fixture_prog", programs_root=programs_root, repo_root=tmp_path)
    assert outcome == "clean"
    assert "matches" in message


def test_tampered_corpus_is_reported_as_drift_with_regeneration_command(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    quality_dir = programs_root / "fixture_prog" / "_quality"
    quality_dir.mkdir(parents=True)
    (quality_dir / "rev_labeled_corpus.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (quality_dir / "corpus_manifest.jsonl").write_text('{"b": 2}\n', encoding="utf-8")

    write_corpus_freeze_manifest(program="fixture_prog", programs_root=programs_root, repo_root=tmp_path)

    # Tamper the corpus after the freeze was written.
    (quality_dir / "rev_labeled_corpus.jsonl").write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")

    outcome, message = check_corpus_freeze_drift(program="fixture_prog", programs_root=programs_root, repo_root=tmp_path)
    assert outcome == "drift"
    assert "scripts/verify_activation.py --write-corpus-freeze --program fixture_prog" in message


def test_tampered_freeze_manifest_file_itself_is_reported_as_drift(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    quality_dir = programs_root / "fixture_prog" / "_quality"
    quality_dir.mkdir(parents=True)
    (quality_dir / "rev_labeled_corpus.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (quality_dir / "corpus_manifest.jsonl").write_text('{"b": 2}\n', encoding="utf-8")
    write_corpus_freeze_manifest(program="fixture_prog", programs_root=programs_root, repo_root=tmp_path)

    freeze_path = quality_dir / "corpus_freeze.json"
    manifest = json.loads(freeze_path.read_text(encoding="utf-8"))
    manifest["files"]["rev_labeled_corpus.jsonl"]["sha256"] = "0" * 64
    freeze_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    outcome, message = check_corpus_freeze_drift(program="fixture_prog", programs_root=programs_root, repo_root=tmp_path)
    assert outcome == "drift"


def test_main_exits_0_on_clean_and_1_on_drift(tmp_path: Path, capsys) -> None:
    programs_root = tmp_path / "programs"
    quality_dir = programs_root / "fixture_prog" / "_quality"
    quality_dir.mkdir(parents=True)
    (quality_dir / "rev_labeled_corpus.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (quality_dir / "corpus_manifest.jsonl").write_text('{"b": 2}\n', encoding="utf-8")
    write_corpus_freeze_manifest(program="fixture_prog", programs_root=programs_root, repo_root=tmp_path)

    args = ["--program", "fixture_prog", "--programs-root", str(programs_root), "--repo-root", str(tmp_path)]
    exit_code = main(args)
    assert exit_code == 0

    (quality_dir / "rev_labeled_corpus.jsonl").write_text('{"a": 99}\n', encoding="utf-8")
    exit_code = main(args)
    assert exit_code == 1


# ADF-W0.13's counterfactual-artifact half: build_counterfactual_freeze_check
# pins the SHA-256 of the AG-1 counterfactual diff artifact (not just the
# raw input files) so a regression in diff-generation logic itself is
# detected, distinct from the inputs changing. No real counterfactual pair
# exists for any program today (confirmed before building this: XPF's risk
# register has zero candidate/hygiene rows to generate one from) -- these
# tests exercise the mechanism against synthetic fixtures, honestly.


def _write_render_pair(tmp_path: Path) -> tuple[Path, Path]:
    with_fact = tmp_path / "with_fact.txt"
    without_fact = tmp_path / "without_fact.txt"
    with_fact.write_text("Milestone M1 is ON_TRACK per fact xyz.\n", encoding="utf-8")
    without_fact.write_text("Milestone M1 status is UNKNOWN.\n", encoding="utf-8")
    return with_fact, without_fact


def test_counterfactual_never_frozen_is_not_reported_as_drift(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    with_fact, without_fact = _write_render_pair(tmp_path)
    outcome, message = check_counterfactual_freeze_drift(
        program="fixture_prog",
        with_fact_render=with_fact,
        without_fact_render=without_fact,
        source_document_key="doc-key-1",
        programs_root=programs_root,
        repo_root=tmp_path,
    )
    assert outcome == "never_frozen"
    assert "missing" in message


def test_counterfactual_clean_freeze_matches_current_artifact(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    with_fact, without_fact = _write_render_pair(tmp_path)
    freeze_path = programs_root / "fixture_prog" / "_quality" / "counterfactual_freeze" / "doc-key-1.json"
    write_counterfactual_freeze_manifest(
        output_path=freeze_path,
        with_fact_path=with_fact,
        without_fact_path=without_fact,
        source_document_key="doc-key-1",
        repo_root=tmp_path,
    )

    outcome, message = check_counterfactual_freeze_drift(
        program="fixture_prog",
        with_fact_render=with_fact,
        without_fact_render=without_fact,
        source_document_key="doc-key-1",
        programs_root=programs_root,
        repo_root=tmp_path,
    )
    assert outcome == "clean"
    assert "matches" in message


def test_counterfactual_tampered_render_is_reported_as_drift_with_regeneration_command(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    with_fact, without_fact = _write_render_pair(tmp_path)
    freeze_path = programs_root / "fixture_prog" / "_quality" / "counterfactual_freeze" / "doc-key-1.json"
    write_counterfactual_freeze_manifest(
        output_path=freeze_path,
        with_fact_path=with_fact,
        without_fact_path=without_fact,
        source_document_key="doc-key-1",
        repo_root=tmp_path,
    )

    # Tamper the with-fact render after the freeze was written.
    with_fact.write_text("Milestone M1 is ON_TRACK per a completely different fact.\n", encoding="utf-8")

    outcome, message = check_counterfactual_freeze_drift(
        program="fixture_prog",
        with_fact_render=with_fact,
        without_fact_render=without_fact,
        source_document_key="doc-key-1",
        programs_root=programs_root,
        repo_root=tmp_path,
    )
    assert outcome == "drift"
    assert "--write-counterfactual-freeze" in message
    assert "doc-key-1" in message


def test_counterfactual_missing_render_inputs_is_reported_as_drift_not_crash(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    outcome, message = check_counterfactual_freeze_drift(
        program="fixture_prog",
        with_fact_render=tmp_path / "does_not_exist_with.txt",
        without_fact_render=tmp_path / "does_not_exist_without.txt",
        source_document_key="doc-key-1",
        programs_root=programs_root,
        repo_root=tmp_path,
    )
    assert outcome == "drift"
    assert "missing" in message

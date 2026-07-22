"""specs/backlog.md BL-L1 action (2): tests for the explicit, attested
evidence-regeneration operation (scripts/regenerate_activation_evidence.py).

Exercises the CLI wrapper's own behavior -- argument validation, the
attestation log record shape, and durability across multiple runs -- not
the underlying freeze-manifest writers, which already have their own
tests in test_verify_activation.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.regenerate_activation_evidence import main as regenerate_main


def _write_corpus_files(programs_root: Path, program: str) -> None:
    quality_dir = programs_root / program / "_quality"
    quality_dir.mkdir(parents=True, exist_ok=True)
    (quality_dir / "rev_labeled_corpus.jsonl").write_text('{"candidate_id": "c1"}\n', encoding="utf-8")


def test_requires_exactly_one_operation_flag(tmp_path: Path) -> None:
    rc = regenerate_main(["--program", "xpf", "--programs-root", str(tmp_path), "--reason", "test"])
    assert rc == 2


def test_rejects_two_operation_flags_at_once(tmp_path: Path) -> None:
    rc = regenerate_main([
        "--program", "xpf", "--programs-root", str(tmp_path), "--reason", "test",
        "--corpus-freeze", "--counterfactual-pair",
    ])
    assert rc == 2


def test_corpus_freeze_regenerates_manifest_and_attests(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_corpus_files(tmp_path, "xpf")

    rc = regenerate_main([
        "--program", "xpf", "--programs-root", str(tmp_path),
        "--corpus-freeze", "--reason", "quarterly corpus refresh",
    ])

    assert rc == 0
    manifest_path = tmp_path / "xpf" / "_quality" / "corpus_freeze.json"
    assert manifest_path.exists()

    log_path = tmp_path / "xpf" / "_quality" / "evidence_regeneration_log.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["program"] == "xpf"
    assert record["operation"] == "corpus_freeze"
    assert record["reason"] == "quarterly corpus refresh"
    assert record["operator"]
    assert record["git_sha"]
    assert record["manifest_sha256"] is not None
    assert record["manifest_path"] == str(manifest_path)

    out = capsys.readouterr().out
    assert "Regenerated" in out
    assert "attested" in out


def test_multiple_regenerations_append_not_overwrite(tmp_path: Path) -> None:
    _write_corpus_files(tmp_path, "xpf")

    for reason in ("first pass", "second pass"):
        rc = regenerate_main([
            "--program", "xpf", "--programs-root", str(tmp_path),
            "--corpus-freeze", "--reason", reason,
        ])
        assert rc == 0

    log_path = tmp_path / "xpf" / "_quality" / "evidence_regeneration_log.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    reasons = [json.loads(line)["reason"] for line in lines]
    assert reasons == ["first pass", "second pass"]


def test_counterfactual_freeze_requires_all_three_render_args(tmp_path: Path) -> None:
    rc = regenerate_main([
        "--program", "xpf", "--programs-root", str(tmp_path), "--reason", "test",
        "--counterfactual-freeze",
    ])
    assert rc == 2


def test_counterfactual_freeze_regenerates_and_attests(tmp_path: Path) -> None:
    with_render = tmp_path / "with.txt"
    without_render = tmp_path / "without.txt"
    with_render.write_text("Milestone X shipped on 2026-06-01.\n", encoding="utf-8")
    without_render.write_text("Milestone X status pending.\n", encoding="utf-8")

    rc = regenerate_main([
        "--program", "xpf", "--programs-root", str(tmp_path),
        "--counterfactual-freeze",
        "--with-fact-render", str(with_render),
        "--without-fact-render", str(without_render),
        "--source-document-key", "doc-123",
        "--reason", "AG-1 counterfactual re-baseline",
    ])

    assert rc == 0
    manifest_path = tmp_path / "xpf" / "_quality" / "counterfactual_freeze" / "doc-123.json"
    assert manifest_path.exists()

    log_path = tmp_path / "xpf" / "_quality" / "evidence_regeneration_log.jsonl"
    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["operation"] == "counterfactual_freeze"
    assert record["detail"]["source_document_key"] == "doc-123"


def test_counterfactual_pair_reports_error_when_fact_not_attributable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = regenerate_main([
        "--program", "xpf", "--programs-root", str(tmp_path),
        "--counterfactual-pair", "--fact-id", "does-not-exist",
        "--reason", "test",
    ])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Could not generate an attributable diff" in err
    # No attestation is written when the operation itself never happened.
    log_path = tmp_path / "xpf" / "_quality" / "evidence_regeneration_log.jsonl"
    assert not log_path.exists()

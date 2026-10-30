from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from scripts import verify_spec_citations as verifier


def test_missing_file(tmp_path: Path) -> None:
    citation = verifier.Citation("missing evidence", "missing.py", 1, "anchor")

    result = verifier._verify_one(citation, tmp_path)

    assert result.citation == asdict(citation)
    assert result.status == "fail"
    assert result.detail == "file not found: missing.py"


@pytest.mark.parametrize("line", [0, -1, 3])
def test_out_of_range(tmp_path: Path, line: int) -> None:
    (tmp_path / "source.py").write_text("first\nanchor\n", encoding="utf-8")
    citation = verifier.Citation("invalid line", "source.py", line, "anchor")

    result = verifier._verify_one(citation, tmp_path)

    assert result.status == "fail"
    assert result.detail == f"line {line} out of range (file has 2 lines)"


@pytest.mark.parametrize("offset", [-3, 0, 3])
def test_exact_default_window(tmp_path: Path, offset: int) -> None:
    lines = ["unrelated"] * 9
    lines[4 + offset] = "exact evidence"
    (tmp_path / "source.py").write_text("\n".join(lines), encoding="utf-8")
    citation = verifier.Citation("bounded evidence", "source.py", 5, "exact evidence")

    result = verifier._verify_one(citation, tmp_path)

    assert citation.window == 3
    assert result.status == "pass"
    assert result.detail == "line 5 (source.py) contains 'exact evidence'"


@pytest.mark.parametrize("offset", [-4, 4])
def test_outside_window_is_not_evidence(tmp_path: Path, offset: int) -> None:
    lines = ["unrelated"] * 9
    lines[4 + offset] = "exact evidence"
    (tmp_path / "source.py").write_text("\n".join(lines), encoding="utf-8")
    citation = verifier.Citation("bounded evidence", "source.py", 5, "exact evidence")

    result = verifier._verify_one(citation, tmp_path)

    assert result.status == "fail"
    assert result.detail == "line 5 ±3 does not contain 'exact evidence'"


def test_changed_content_fails(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("original evidence", encoding="utf-8")
    citation = verifier.Citation("changed evidence", "source.py", 1, "original evidence")
    assert verifier._verify_one(citation, tmp_path).status == "pass"

    source.write_text("replacement evidence", encoding="utf-8")

    result = verifier._verify_one(citation, tmp_path)
    assert result.status == "fail"
    assert result.detail == "line 1 ±3 does not contain 'original evidence'"


@pytest.mark.parametrize("output_format", ["human", "json"])
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("missing", [False, True])
def test_cli_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
    strict: bool,
    missing: bool,
) -> None:
    citation = verifier.Citation("CLI evidence", "source.py", 1, "anchor")
    (tmp_path / "source.py").write_text(
        "changed" if missing else "anchor", encoding="utf-8",
    )
    monkeypatch.setattr(verifier, "CITATIONS", (citation,))
    arguments = ["--repo-root", str(tmp_path), "--format", output_format]
    if strict:
        arguments.append("--strict")

    assert verifier.main(arguments) == int(missing)
    captured = capsys.readouterr()

    assert captured.err == ""
    result = verifier._verify_one(citation, tmp_path)
    if output_format == "json":
        assert json.loads(captured.out) == {
            "overall": "fail" if missing else "pass",
            "passed": int(not missing),
            "failed": int(missing),
            "results": [asdict(result)],
        }
    else:
        prefix = "FAIL" if missing else "PASS"
        expected = (
            f"Spec citation checks: {int(not missing)}/1 resolved\n"
            f"{prefix:4} source.py:1  {result.detail}\n"
        )
        if missing:
            expected += "        claim: CLI evidence\n"
        assert captured.out == expected


def test_current_registry() -> None:
    assert len(verifier.CITATIONS) == 24
    assert all(citation.window == 3 for citation in verifier.CITATIONS)
    results = verifier.run()
    assert len(results) == 24
    assert all(result.status == "pass" for result in results), [
        asdict(result) for result in results if result.status != "pass"
    ]

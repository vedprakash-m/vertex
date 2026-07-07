"""GAP-35: ``scripts/derive_operational_completion.py`` operational-signal tooling.

Verifies the tool reports honest on-disk operational artifacts (counts /
presence) rather than a synthesized percentage, is read-only, and emits
stable human + JSON output.
"""
from __future__ import annotations

import json

import pytest

from scripts.derive_operational_completion import derive, main


def test_derive_returns_contract_keys_with_honest_types() -> None:
    derived = derive()
    expected = {
        "ai_graduations",
        "ai_safety_approver_role_md",
        "ai_safety_approver_role_yaml",
        "dpa_scope_artifact",
        "test_evidence_rows",
        "primary_confirmed_issues",
        "secondary_confirmed_issues",
        "multi_program_operational_proof",
    }
    assert set(derived.keys()) == expected
    # Counts are non-negative ints; presence is bool.
    assert isinstance(derived["ai_graduations"], int) and derived["ai_graduations"] >= 0
    assert isinstance(derived["test_evidence_rows"], int) and derived["test_evidence_rows"] >= 0
    assert isinstance(derived["primary_confirmed_issues"], int) and derived["primary_confirmed_issues"] >= 0
    assert isinstance(derived["secondary_confirmed_issues"], int) and derived["secondary_confirmed_issues"] >= 0
    assert isinstance(derived["ai_safety_approver_role_md"], bool)
    assert isinstance(derived["dpa_scope_artifact"], bool)
    assert isinstance(derived["multi_program_operational_proof"], bool)


def test_multi_program_proof_follows_secondary_count() -> None:
    derived = derive()
    assert derived["multi_program_operational_proof"] == (derived["secondary_confirmed_issues"] > 0)


def test_main_json_emits_valid_json_and_exits_zero(capsys: pytest.CaptureFixture) -> None:
    rc = main(["--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "ai_graduations" in payload
    assert "primary_confirmed_issues" in payload


def test_main_human_emits_table_and_exits_zero(capsys: pytest.CaptureFixture) -> None:
    rc = main(["--format", "human"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "signal" in out
    assert "ai_graduations" in out
    assert "primary_confirmed_issues" in out


def test_main_default_is_human(capsys: pytest.CaptureFixture) -> None:
    rc = main([])
    assert rc == 0
    assert "signal" in capsys.readouterr().out


def test_graduation_artifact_is_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A graduation markdown dropped into ``governance/graduations/`` raises the
    count — proving the probe actually scans the dir (not a hardcoded number)."""
    import scripts.derive_operational_completion as doc

    fake_root = tmp_path / "repo"
    (fake_root / "governance" / "graduations").mkdir(parents=True)
    (fake_root / "governance" / "graduations" / "feature_alpha.md").write_text("x", encoding="utf-8")
    (fake_root / "governance" / "graduations" / "feature_beta.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(doc, "REPO_ROOT", fake_root)
    assert doc.derive()["ai_graduations"] == 2


def test_confirmed_issues_counted_from_archive_manifests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``programs/<id>/archive/<edition>/manifests/issue_NNN.json`` files raise the
    confirmed-issue count — proving the probe scans archive manifests."""
    import scripts.derive_operational_completion as doc

    fake_root = tmp_path / "repo"
    manifests = fake_root / "programs" / "fabrikam" / "archive" / "fabrikam_weekly" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "issue_001.json").write_text("{}", encoding="utf-8")
    (manifests / "issue_002.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doc, "REPO_ROOT", fake_root)
    derived = doc.derive()
    assert derived["secondary_confirmed_issues"] == 2
    assert derived["multi_program_operational_proof"] is True
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    path = REPO_ROOT / "scripts" / "verify_disclosure_candidate.py"
    spec = importlib.util.spec_from_file_location("verify_disclosure_candidate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)


def _run(module, capsys: pytest.CaptureFixture[str], repo_root: Path, *args: str) -> tuple[int, dict[str, object]]:
    exit_code = module.main(list(args), repo_root=repo_root)
    payload = json.loads(capsys.readouterr().out)
    return exit_code, payload


def test_requires_explicit_candidate_and_emits_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    exit_code, payload = _run(module, capsys, tmp_path)

    assert exit_code == 2
    assert payload["status"] == "blocked"
    assert payload["candidate_count"] == 0
    assert payload["findings"] == [
        {
            "code": "SELECTION_REQUIRED",
            "severity": "blocker",
            "path": None,
            "line": None,
            "message": "Select at least one candidate or a candidate manifest.",
        }
    ]


def test_rejects_candidate_outside_repo_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic content\n", encoding="utf-8")

    exit_code, payload = _run(module, capsys, repo_root, "--candidate", str(outside))

    assert exit_code == 2
    assert payload["candidate_count"] == 0
    assert payload["findings"][0]["code"] == "CANDIDATE_OUTSIDE_ROOT"
    assert str(tmp_path) not in json.dumps(payload)


def test_clean_candidate_has_stable_machine_readable_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    _init_repo(tmp_path)
    candidate = tmp_path / "output" / "safe.txt"
    candidate.parent.mkdir()
    candidate.write_text(
        'contact = "synthetic@microsoft.com"\n'
        'connection = "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=abcdEFGH1234567890+/="\n',
        encoding="utf-8",
    )

    first_code = module.main(["--candidate", "output/safe.txt"], repo_root=tmp_path)
    first = capsys.readouterr().out
    second_code = module.main(["--candidate", "output/safe.txt"], repo_root=tmp_path)
    second = capsys.readouterr().out

    assert first_code == second_code == 0
    assert first == second
    assert json.loads(first) == {
        "candidate_count": 1,
        "findings": [],
        "manifest": None,
        "schema_version": 1,
        "status": "checked",
    }


def test_candidate_composes_publication_content_checks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    _init_repo(tmp_path)
    private_program = "Arm" + "ada"
    candidate = tmp_path / private_program / "release.txt"
    candidate.parent.mkdir()
    candidate.write_text(
        'control = "synthetic@microsoft.com"; token = "'
        + "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
        + '"\ncontact = "person@private' + '.local"\n'
        + 'local_path = "' + "C:" + r"\\Users\\" + 'operator\\report.txt"\n',
        encoding="utf-8",
    )

    exit_code, payload = _run(
        module, capsys, tmp_path, "--candidate", f"{private_program}/release.txt"
    )
    findings = payload["findings"]

    assert exit_code == 1
    assert {finding["code"] for finding in findings} >= {
        "PRIVATE_PROGRAM_NAME",
        "SECRET_PATTERN",
        "PRIVATE_OR_PII",
    }
    assert all(finding["path"] == f"{private_program}/release.txt" for finding in findings)
    assert all(finding["severity"] == "blocker" for finding in findings)
    assert findings == sorted(findings, key=lambda finding: (finding["path"], finding["code"], finding["line"]))


def test_ignored_and_unavailable_candidates_are_blockers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("content\n", encoding="utf-8")

    ignored_code, ignored = _run(
        module,
        capsys,
        tmp_path,
        "--candidate",
        "ignored.txt",
    )
    missing_code, missing = _run(
        module,
        capsys,
        tmp_path,
        "--candidate",
        "missing.txt",
    )

    assert ignored_code == 1
    assert ignored["findings"][0]["code"] == "IGNORED_INPUT"
    assert missing_code == 2
    assert missing["findings"][0]["code"] == "INPUT_UNAVAILABLE"


def test_unscannable_binary_candidate_is_disclosed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    _init_repo(tmp_path)
    (tmp_path / "artifact.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    exit_code, payload = _run(module, capsys, tmp_path, "--candidate", "artifact.png")

    assert exit_code == 1
    assert payload["findings"][0]["code"] == "UNSCANNED_BINARY"


def test_publication_scanner_allowlist_skip_is_disclosed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    _init_repo(tmp_path)
    candidate = tmp_path / "tests" / "unit" / "test_ai_trace_sanitizer.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("synthetic fixture\n", encoding="utf-8")

    exit_code, payload = _run(
        module,
        capsys,
        tmp_path,
        "--candidate",
        "tests/unit/test_ai_trace_sanitizer.py",
    )

    assert exit_code == 1
    assert payload["findings"][0]["code"] == "UNSCANNED_ALLOWLISTED_PATH"


def test_manifest_validates_outgoing_blob_hash(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    _init_repo(tmp_path)
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("approved candidate bytes\n", encoding="utf-8")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    manifest = tmp_path / "candidate-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": [{"path": "candidate.txt", "sha256": digest}],
            }
        ),
        encoding="utf-8",
    )

    clean_code, clean = _run(module, capsys, tmp_path, "--manifest", "candidate-manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": [{"path": "candidate.txt", "sha256": "0" * 64}],
            }
        ),
        encoding="utf-8",
    )
    mismatch_code, mismatch = _run(module, capsys, tmp_path, "--manifest", "candidate-manifest.json")

    assert clean_code == 0
    assert clean["findings"] == []
    assert clean["manifest"] == "candidate-manifest.json"
    assert mismatch_code == 1
    assert mismatch["findings"][0]["code"] == "OUTGOING_BLOB_HASH_MISMATCH"


@pytest.mark.parametrize(
    "manifest",
    [
        {"schema_version": 2, "artifacts": []},
        {"schema_version": 1, "artifacts": []},
        {"schema_version": 1, "artifacts": [{"path": "candidate.txt"}]},
        {"schema_version": 1, "artifacts": "candidate.txt"},
    ],
)
def test_invalid_manifest_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    manifest: object,
) -> None:
    module = _load_module()
    (tmp_path / "candidate-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    exit_code, payload = _run(module, capsys, tmp_path, "--manifest", "candidate-manifest.json")

    assert exit_code == 2
    assert payload["candidate_count"] == 0
    assert payload["findings"][0]["code"] == "MANIFEST_INVALID"

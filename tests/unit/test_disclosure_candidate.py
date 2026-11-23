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


def _git(repo: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )


def _configure_git_repo(repo: Path) -> None:
    _init_repo(repo)
    _git(repo, "config", "user.email", "tester@users.noreply.github.com")
    _git(repo, "config", "user.name", "Vertex Tester")


def test_staged_reads_exact_index_blob_across_working_tree_divergence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    _configure_git_repo(tmp_path)
    target = tmp_path / "notes.txt"
    secret = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"

    # Direction A: secret staged in index, working tree scrubbed -> MUST block
    target.write_text(f"token = '{secret}'\n", encoding="utf-8")
    _git(tmp_path, "add", "notes.txt")
    target.write_text("clean working tree\n", encoding="utf-8")
    blocked_code, blocked = _run(module, capsys, tmp_path, "--staged")
    assert blocked_code == 1
    assert blocked["findings"][0]["code"] == "SECRET_PATTERN"
    assert blocked["findings"][0]["path"] == "notes.txt"

    # Direction B: clean blob staged in index, working tree dirty with secret -> MUST pass
    target.write_text("clean staged blob\n", encoding="utf-8")
    _git(tmp_path, "add", "notes.txt")
    target.write_text(f"token = '{secret}'\n", encoding="utf-8")
    clean_code, clean = _run(module, capsys, tmp_path, "--staged")
    assert clean_code == 0
    assert clean["findings"] == []
    assert clean["candidate_count"] == 1


def test_staged_skips_deletions_handles_skip_paths_and_binary_allowlist(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    _configure_git_repo(tmp_path)

    # Initial tracked file to test deletion skip
    doomed = tmp_path / "obsolete.txt"
    doomed.write_text("bye\n", encoding="utf-8")
    _git(tmp_path, "add", "obsolete.txt")
    _git(tmp_path, "commit", "-m", "initial")
    _git(tmp_path, "rm", "--quiet", "obsolete.txt")

    # SKIP_PATHS file staged cleanly without false UNSCANNED_ALLOWLISTED_PATH block
    skip_file = tmp_path / "tests" / "unit" / "test_publication_content_scan.py"
    skip_file.parent.mkdir(parents=True)
    skip_file.write_text("PATTERN = '" + "Arm" + "ada'\n", encoding="utf-8")
    _git(tmp_path, "add", "tests/unit/test_publication_content_scan.py")

    skip_code, skip_payload = _run(module, capsys, tmp_path, "--staged")
    assert skip_code == 0, skip_payload
    assert skip_payload["findings"] == []

    # Unallowlisted binary blocks
    png = tmp_path / "assets" / "logo.png"
    png.parent.mkdir(parents=True)
    png_bytes = b"\x89PNG\r\n\x1a\n"
    png.write_bytes(png_bytes)
    _git(tmp_path, "add", "assets/logo.png")
    bin_code, bin_payload = _run(module, capsys, tmp_path, "--staged")
    assert bin_code == 1
    assert any(f["code"] == "UNSCANNED_BINARY" and f["path"] == "assets/logo.png" for f in bin_payload["findings"])

    # Manifest-allowlisted binary with matching sha256 passes
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": [
                    {
                        "path": "assets/logo.png",
                        "sha256": hashlib.sha256(png_bytes).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    allowed_code, allowed_payload = _run(
        module, capsys, tmp_path, "--staged", "--binary-allowlist", "allowlist.json"
    )
    assert allowed_code == 0, allowed_payload

    # Modified binary whose sha256 disagrees with allowlist fails closed
    png.write_bytes(b"\x89PNG\r\n\x1a\nMUTATED")
    _git(tmp_path, "add", "assets/logo.png")
    mismatch_code, mismatch_payload = _run(
        module, capsys, tmp_path, "--staged", "--binary-allowlist", "allowlist.json"
    )
    assert mismatch_code == 1
    assert any(
        f["code"] == "OUTGOING_BLOB_HASH_MISMATCH" and f["path"] == "assets/logo.png"
        for f in mismatch_payload["findings"]
    )


def test_push_refs_scans_outgoing_commits_and_hooks_wire_disclosure_checks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    module = _load_module()
    _configure_git_repo(tmp_path)
    doc = tmp_path / "doc.txt"
    doc.write_text("clean v1\n", encoding="utf-8")
    _git(tmp_path, "add", "doc.txt")
    _git(tmp_path, "commit", "-m", "v1")
    base_oid = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    secret = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
    doc.write_text(f"leaked = '{secret}'\n", encoding="utf-8")
    _git(tmp_path, "add", "doc.txt")
    _git(tmp_path, "commit", "-m", "v2 leak")
    head_oid = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(f"refs/heads/main {head_oid} refs/heads/main {base_oid}\n"),
    )
    push_code, push_payload = _run(module, capsys, tmp_path, "--push-refs")
    assert push_code == 1
    assert push_payload["findings"][0]["code"] == "SECRET_PATTERN"
    assert push_payload["findings"][0]["path"] == "doc.txt"

    pre_commit = (REPO_ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    pre_push = (REPO_ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")
    assert "scripts/rev_precommit_quality.py" in pre_commit
    assert "--diff-filter=ACMRT" in pre_commit
    assert "scripts/verify_disclosure_candidate.py --staged" in pre_commit
    assert "scripts/verify_disclosure_candidate.py --push-refs" in pre_push

    # Deleted branch (0*40 local oid) and new branch (0*40 remote oid) handling
    clean_doc = tmp_path / "clean_branch.txt"
    clean_doc.write_text("safe branch commit\n", encoding="utf-8")
    _git(tmp_path, "add", "clean_branch.txt")
    _git(tmp_path, "commit", "-m", "v3 clean")
    v3_oid = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    # This ref says nothing about which objects the destination has.
    _git(tmp_path, "update-ref", "refs/remotes/unrelated/main", head_oid)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            f"(delete) {'0' * 40} refs/heads/old {head_oid}\n"
            f"refs/heads/new {v3_oid} refs/heads/new {'0' * 40}\n"
        ),
    )
    new_branch_code, new_branch_payload = _run(module, capsys, tmp_path, "--push-refs")
    assert new_branch_code == 1  # detects prior leaked v2 commit in branch ancestry
    assert any(f["code"] == "SECRET_PATTERN" and f["path"] == "doc.txt"
               for f in new_branch_payload["findings"])


def test_push_refs_rejects_nonblank_malformed_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("malformed ref update\n"))
    code, payload = _run(_load_module(), capsys, tmp_path, "--push-refs")
    assert code == 2
    assert payload["findings"][0]["code"] == "SELECTION_INVALID"


def test_identity_accepts_only_github_noreply_email(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    _configure_git_repo(tmp_path)

    clean_code, clean = _run(module, capsys, tmp_path, "--identity")
    _git(tmp_path, "config", "user.email", "operator@example.invalid")
    blocked_code, blocked = _run(module, capsys, tmp_path, "--identity")

    assert clean_code == 0
    assert clean["findings"] == []
    assert blocked_code == 1
    assert blocked["findings"][0]["code"] == "GIT_IDENTITY_UNAPPROVED"


def test_commit_range_rejects_non_noreply_author_or_committer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    _configure_git_repo(tmp_path)
    (tmp_path / "safe.txt").write_text("safe\n", encoding="utf-8")
    _git(tmp_path, "add", "safe.txt")
    _git(tmp_path, "commit", "-m", "safe")
    first = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    (tmp_path / "next.txt").write_text("next\n", encoding="utf-8")
    _git(tmp_path, "add", "next.txt")
    _git(
        tmp_path,
        "commit",
        "-m",
        "disallowed identity",
        "--author=Synthetic Person <person@example.invalid>",
    )
    second = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    code, payload = _run(
        module,
        capsys,
        tmp_path,
        "--commit-range",
        f"{first}..{second}",
    )

    assert code == 1
    assert payload["candidate_count"] == 0
    assert {finding["code"] for finding in payload["findings"]} == {
        "COMMIT_AUTHOR_UNAPPROVED"
    }


def test_staged_scans_type_changes_and_uses_index_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    _configure_git_repo(tmp_path)
    doc = tmp_path / "doc.txt"
    doc.write_text("safe initial file\n", encoding="utf-8")
    _git(tmp_path, "add", "doc.txt")
    _git(tmp_path, "commit", "-m", "baseline")
    value = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
    blob = _git(tmp_path, "hash-object", "-w", "--stdin", input_text=value).stdout.strip()
    _git(tmp_path, "update-index", "--cacheinfo", f"120000,{blob},doc.txt")

    real_resolve = Path.resolve

    def resolve_with_worktree_alias(self, *args, **kwargs):
        if self == doc:
            return tmp_path / "different.txt"
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_with_worktree_alias)
    code, payload = _run(module, capsys, tmp_path, "--staged")
    assert code == 1
    assert any(f["code"] == "SECRET_PATTERN" and f["path"] == "doc.txt"
               for f in payload["findings"])


def test_staged_scans_literal_blob_with_local_replacement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    _configure_git_repo(tmp_path)
    (tmp_path / "doc.txt").write_text("ghp_" + "abcdefghijklmnopqrstuvwxyz123456", encoding="utf-8")
    _git(tmp_path, "add", "doc.txt")
    original = _git(tmp_path, "rev-parse", ":doc.txt").stdout.strip()
    replacement = _git(tmp_path, "hash-object", "-w", "--stdin", input_text="safe").stdout.strip()
    _git(tmp_path, "replace", original, replacement)

    code, payload = _run(module, capsys, tmp_path, "--staged")

    assert code == 1
    assert any(f["code"] == "SECRET_PATTERN" for f in payload["findings"])


def test_push_refs_scans_content_created_by_the_merge(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    module = _load_module()
    _configure_git_repo(tmp_path)
    (tmp_path / "base.txt").write_text("safe baseline\n", encoding="utf-8")
    _git(tmp_path, "add", "base.txt")
    _git(tmp_path, "commit", "-m", "baseline")
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _git(tmp_path, "branch", "side")
    _git(tmp_path, "checkout", "side")
    (tmp_path / "side.txt").write_text("safe side change\n", encoding="utf-8")
    _git(tmp_path, "add", "side.txt")
    _git(tmp_path, "commit", "-m", "side change")
    _git(tmp_path, "checkout", "-")
    _git(tmp_path, "merge", "--no-ff", "--no-commit", "side")
    (tmp_path / "merge.txt").write_text("ghp_" + "abcdefghijklmnopqrstuvwxyz123456", encoding="utf-8")
    _git(tmp_path, "add", "merge.txt")
    _git(tmp_path, "commit", "-m", "merge with additional content")
    head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"refs/heads/main {head} refs/heads/main {base}\n"))

    code, payload = _run(module, capsys, tmp_path, "--push-refs")

    assert code == 1
    assert any(
        finding["path"] == "merge.txt" and finding["code"] == "SECRET_PATTERN"
        for finding in payload["findings"]
    )

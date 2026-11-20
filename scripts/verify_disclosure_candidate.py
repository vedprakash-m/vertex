#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import NoReturn, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.check_publication_content import BINARY_SUFFIXES, SKIP_PATHS, scan_lines  # noqa: E402


SCHEMA_VERSION = 1
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_UNAVAILABLE = {"IGNORE_STATUS_UNAVAILABLE", "INPUT_UNAVAILABLE"}
_MESSAGES = {
    "CANDIDATE_DUPLICATE": "Candidate path was selected more than once.",
    "CANDIDATE_INVALID": "Candidate must select a file.",
    "CANDIDATE_OUTSIDE_ROOT": "Candidate path must resolve inside the repository root.",
    "DISCLOSURE_FINDING": "Existing publication content scan reported a disclosure finding.",
    "IGNORED_INPUT": "Selected input is ignored by Git.",
    "IGNORE_STATUS_UNAVAILABLE": "Git ignore status could not be determined for the selected input.",
    "INPUT_UNAVAILABLE": "Selected input is unavailable, unreadable, or not a regular file.",
    "MANIFEST_INVALID": "Manifest is invalid for disclosure candidate schema version 1.",
    "OUTGOING_BLOB_HASH_MISMATCH": "Selected candidate does not match its manifest sha256.",
    "PRIVATE_OR_PII": "Potential private path or PII value appears in the selected candidate.",
    "PRIVATE_PROGRAM_NAME": "Private program name appears in the selected candidate.",
    "SELECTION_INVALID": "Command arguments do not select a valid candidate or manifest.",
    "SELECTION_REQUIRED": "Select at least one candidate or a candidate manifest.",
    "SECRET_PATTERN": "Potential secret appears in the selected candidate.",
    "UNSCANNED_BINARY": "Selected binary candidate cannot be reviewed by the text disclosure scanner.",
    "UNSCANNED_ALLOWLISTED_PATH": "Selected input is allowlisted by the publication scanner and was not content-scanned.",
    "UNSCANNED_TEXT": "Selected candidate is not decodable as UTF-8 text.",
}
_SCAN_CODES = {
    "private program name": "PRIVATE_PROGRAM_NAME",
    "potential secret": "SECRET_PATTERN",
    "potential private/PII value": "PRIVATE_OR_PII",
}


class _ArgumentError(Exception):
    pass


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgumentError(message)


class _SelectionError(Exception):
    def __init__(self, code: str, path: str | None = None) -> None:
        super().__init__(_MESSAGES[code])
        self.code = code
        self.path = path


@dataclass(frozen=True, slots=True)
class Candidate:
    path: Path
    relative_path: str
    expected_sha256: str | None = None
    git_object: str | None = None


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: str
    path: str | None
    line: int | None
    message: str


def _finding(code: str, path: str | None = None, line: int | None = None) -> Finding:
    return Finding(code, "blocker", path, line, _MESSAGES[code])


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _JsonArgumentParser(
        description="Check an explicitly selected outgoing disclosure candidate."
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--candidate", action="append", dest="candidates")
    selection.add_argument("--manifest")
    selection.add_argument("--staged", action="store_true")
    selection.add_argument("--push-refs", action="store_true")
    parser.add_argument("--binary-allowlist")
    return parser.parse_args(argv)


def _resolve(raw_path: str, root: Path) -> Candidate:
    if not isinstance(raw_path, str) or not raw_path:
        raise _SelectionError("CANDIDATE_INVALID")
    try:
        selected = Path(raw_path)
        resolved = (selected if selected.is_absolute() else root / selected).resolve()
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise _SelectionError("CANDIDATE_OUTSIDE_ROOT") from error
    if relative == Path("."):
        raise _SelectionError("CANDIDATE_INVALID")
    return Candidate(resolved, relative.as_posix())


def _load_manifest(
    raw_path: str,
    root: Path,
) -> tuple[str, tuple[Candidate, ...], list[str]]:
    manifest = _resolve(raw_path, root)
    try:
        lines = manifest.path.read_text(encoding="utf-8").splitlines()
        payload = json.loads("\n".join(lines))
    except FileNotFoundError as error:
        raise _SelectionError("INPUT_UNAVAILABLE", manifest.relative_path) from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _SelectionError("MANIFEST_INVALID", manifest.relative_path) from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "artifacts"}
        or payload["schema_version"] != SCHEMA_VERSION
        or not isinstance(payload["artifacts"], list)
        or not payload["artifacts"]
    ):
        raise _SelectionError("MANIFEST_INVALID", manifest.relative_path)

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for artifact in payload["artifacts"]:
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise _SelectionError("MANIFEST_INVALID", manifest.relative_path)
        path = artifact["path"]
        digest = artifact["sha256"]
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise _SelectionError("MANIFEST_INVALID", manifest.relative_path)
        candidate = _resolve(path, root)
        if candidate.relative_path in seen:
            raise _SelectionError("MANIFEST_INVALID", manifest.relative_path)
        seen.add(candidate.relative_path)
        candidates.append(Candidate(candidate.path, candidate.relative_path, digest))
    return manifest.relative_path, tuple(candidates), lines


def _load_staged_candidates(root: Path) -> tuple[Candidate, ...]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise _SelectionError("INPUT_UNAVAILABLE")
    raw_paths = result.stdout.decode("utf-8", errors="strict").split("\0")
    candidates: list[Candidate] = []
    for rel in raw_paths:
        if not rel:
            continue
        candidate = _resolve(rel, root)
        candidates.append(Candidate(candidate.path, candidate.relative_path, git_object=f":{candidate.relative_path}"))
    return tuple(candidates)


def _load_push_ref_candidates(root: Path) -> tuple[Candidate, ...]:
    zero_oid = "0" * 40
    commits: list[str] = []
    for raw_line in sys.stdin.read().splitlines():
        parts = raw_line.strip().split()
        if len(parts) != 4:
            continue
        _local_ref, local_oid, _remote_ref, remote_oid = parts
        if local_oid == zero_oid:
            continue
        cmd = (
            ["git", "rev-list", local_oid, "--not", "--remotes"]
            if remote_oid == zero_oid
            else ["git", "rev-list", f"{remote_oid}..{local_oid}"]
        )
        rev = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
        if rev.returncode != 0:
            raise _SelectionError("INPUT_UNAVAILABLE")
        commits.extend(line.strip() for line in rev.stdout.splitlines() if line.strip())

    seen_objects: set[tuple[str, str]] = set()
    candidates: list[Candidate] = []
    for oid in commits:
        diff = subprocess.run(
            ["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "--diff-filter=ACMR", "-r", "-z", oid],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if diff.returncode != 0:
            raise _SelectionError("INPUT_UNAVAILABLE")
        for rel in diff.stdout.decode("utf-8", errors="strict").split("\0"):
            if not rel:
                continue
            resolved = _resolve(rel, root)
            key = (oid, resolved.relative_path)
            if key in seen_objects:
                continue
            seen_objects.add(key)
            candidates.append(
                Candidate(
                    resolved.path,
                    resolved.relative_path,
                    git_object=f"{oid}:{resolved.relative_path}",
                )
            )
    return tuple(candidates)


def _load_binary_allowlist(raw_path: str | None, root: Path) -> dict[str, str]:
    default_path = root / "governance" / "disclosure-binary-allowlist.json"
    selected = raw_path or ("governance/disclosure-binary-allowlist.json" if default_path.is_file() else None)
    if selected is None:
        return {}
    _, allow_candidates, _ = _load_manifest(selected, root)
    return {c.relative_path: c.expected_sha256 for c in allow_candidates if c.expected_sha256 is not None}


def _load_selection(
    args: argparse.Namespace,
    root: Path,
) -> tuple[str | None, tuple[Candidate, ...], list[str]]:
    if args.staged:
        return None, _load_staged_candidates(root), []
    if args.push_refs:
        return None, _load_push_ref_candidates(root), []
    if args.manifest is not None:
        return _load_manifest(args.manifest, root)
    if not args.candidates:
        raise _SelectionError("SELECTION_REQUIRED")
    candidates = tuple(_resolve(path, root) for path in args.candidates)
    if len({candidate.relative_path for candidate in candidates}) != len(candidates):
        raise _SelectionError("CANDIDATE_DUPLICATE")
    return None, candidates, []


def _is_ignored(relative_path: str, root: Path) -> bool | None:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", "--", relative_path],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    return False if result.returncode == 1 else None


def _scan(relative_path: str, lines: list[str], *, allow_skip_paths: bool = False) -> list[Finding]:
    if relative_path in SKIP_PATHS:
        return [] if allow_skip_paths else [_finding("UNSCANNED_ALLOWLISTED_PATH", relative_path)]
    findings: list[Finding] = []
    for result in scan_lines(relative_path, lines):
        prefix, separator, detail = result.rpartition(": ")
        line_text = prefix.rpartition(":")[2]
        line = int(line_text) if separator and line_text.isdigit() else None
        findings.append(
            _finding(_SCAN_CODES.get(detail, "DISCLOSURE_FINDING"), relative_path, line)
        )
    return findings


def _read_candidate_bytes(candidate: Candidate, root: Path) -> bytes | None:
    if candidate.git_object is not None:
        result = subprocess.run(
            ["git", "show", candidate.git_object],
            cwd=root,
            capture_output=True,
            check=False,
        )
        return result.stdout if result.returncode == 0 else None
    if not candidate.path.is_file():
        return None
    try:
        return candidate.path.read_bytes()
    except OSError:
        return None


def _inspect(
    candidate: Candidate,
    root: Path,
    *,
    allow_skip_paths: bool = False,
    binary_allowlist: dict[str, str] | None = None,
) -> list[Finding]:
    content = _read_candidate_bytes(candidate, root)
    if content is None:
        return [_finding("INPUT_UNAVAILABLE", candidate.relative_path)]
    ignored = _is_ignored(candidate.relative_path, root)
    if ignored is None:
        return [_finding("IGNORE_STATUS_UNAVAILABLE", candidate.relative_path)]
    findings = _scan(candidate.relative_path, [candidate.relative_path], allow_skip_paths=allow_skip_paths)
    if ignored:
        findings.append(_finding("IGNORED_INPUT", candidate.relative_path))
    digest = hashlib.sha256(content).hexdigest()
    if candidate.expected_sha256 is not None and digest != candidate.expected_sha256:
        findings.append(_finding("OUTGOING_BLOB_HASH_MISMATCH", candidate.relative_path))
    if Path(candidate.relative_path).suffix.lower() in BINARY_SUFFIXES:
        if binary_allowlist and candidate.relative_path in binary_allowlist:
            if digest != binary_allowlist[candidate.relative_path]:
                findings.append(_finding("OUTGOING_BLOB_HASH_MISMATCH", candidate.relative_path))
            return findings
        findings.append(_finding("UNSCANNED_BINARY", candidate.relative_path))
        return findings
    try:
        lines = content.decode("utf-8", errors="strict").splitlines()
    except UnicodeError:
        findings.append(_finding("UNSCANNED_TEXT", candidate.relative_path))
        return findings
    return findings + _scan(candidate.relative_path, lines, allow_skip_paths=allow_skip_paths)


def _render(
    candidate_count: int,
    manifest: str | None,
    findings: list[Finding],
    exit_code: int,
) -> None:
    ordered = sorted(
        set(findings),
        key=lambda item: (
            item.path or "",
            item.code,
            item.line if item.line is not None else -1,
            item.message,
        ),
    )
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "manifest": manifest,
                "candidate_count": candidate_count,
                "status": "checked" if exit_code == 0 else "blocked",
                "findings": [asdict(finding) for finding in ordered],
            },
            indent=2,
            sort_keys=True,
        )
    )


def main(argv: Sequence[str] | None = None, *, repo_root: Path = REPO_ROOT) -> int:
    root = repo_root.resolve()
    try:
        args = _parse_args(argv)
        manifest, candidates, manifest_lines = _load_selection(args, root)
        allow_git_mode = bool(args.staged or args.push_refs)
        binary_allowlist = (
            _load_binary_allowlist(args.binary_allowlist, root)
            if allow_git_mode
            else None
        )
    except (_ArgumentError, _SelectionError) as error:
        finding = (
            _finding(error.code, error.path)
            if isinstance(error, _SelectionError)
            else _finding("SELECTION_INVALID")
        )
        _render(0, None, [finding], 2)
        return 2

    findings: list[Finding] = []
    if manifest is not None:
        ignored = _is_ignored(manifest, root)
        if ignored is None:
            findings.append(_finding("IGNORE_STATUS_UNAVAILABLE", manifest))
        else:
            findings.extend(_scan(manifest, manifest_lines))
            if ignored:
                findings.append(_finding("IGNORED_INPUT", manifest))
    for candidate in candidates:
        findings.extend(
            _inspect(
                candidate,
                root,
                allow_skip_paths=allow_git_mode,
                binary_allowlist=binary_allowlist,
            )
        )
    exit_code = (
        2
        if any(finding.code in _UNAVAILABLE for finding in findings)
        else 1 if findings else 0
    )
    _render(len(candidates), manifest, findings, exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

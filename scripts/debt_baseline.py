from __future__ import annotations

import argparse
import ast
from dataclasses import is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from src.commands.confirm import confirm_issue
from src.commands.doctor import ADOProbeResult, _build_doctor_payload, render_doctor_output, run_doctor
from src.core.store_factory import build_signal_store_for_program_id


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "output" / "debt_baseline.json"
_EDITION_PROGRAMS = {
    "acme_weekly": "acme",
    "fabrikam_weekly": "fabrikam",
}
_KNOWN_TERMINAL_STATES = frozenset(
    {
        "closed",
        "complete",
        "completed",
        "done",
        "removed",
        "resolved",
        "cancelled",
        "canceled",
    }
)
_POLICY_CONST_RE = re.compile(r"^_?[A-Z][A-Z0-9_]+$")
_ISSUE_ARTIFACT_RE = re.compile(r"^issue_(\d{3})\.(draft\.json|html|manifest\.json)$", re.IGNORECASE)


def build_debt_baseline(
    *,
    repo_root: Path = REPO_ROOT,
    require_clean_tree: bool = True,
) -> dict[str, Any]:
    if require_clean_tree and _working_tree_dirty(repo_root):
        raise RuntimeError("Working tree must be clean before capturing the debt baseline.")

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_head_sha(repo_root),
        "modules": _collect_module_locs(repo_root),
        "terminal_state_sites": _collect_terminal_state_sites(repo_root),
        "sidecar_read_sites": _collect_sidecar_read_sites(repo_root),
        "ai_call_sites": _collect_ai_call_sites(repo_root),
        "policy_constants": _collect_policy_constants(repo_root),
        "golden_outputs": {
            edition: _capture_edition_outputs(repo_root, edition=edition, program_id=program_id)
            for edition, program_id in _EDITION_PROGRAMS.items()
        },
    }


def write_debt_baseline(
    *,
    repo_root: Path = REPO_ROOT,
    output_path: Path = OUTPUT_PATH,
    require_clean_tree: bool = True,
) -> Path:
    baseline = build_debt_baseline(repo_root=repo_root, require_clean_tree=require_clean_tree)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(baseline, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return output_path


def _capture_edition_outputs(repo_root: Path, *, edition: str, program_id: str) -> dict[str, Any]:
    reports_root = repo_root / "reports"
    output_root = repo_root / "output"
    archive_root = repo_root / "archive"
    editions_root = repo_root / "editions"
    programs_root = repo_root / "programs"

    if not (editions_root / f"{edition}.yaml").exists() or not (programs_root / program_id).exists():
        return {"status": "unavailable_local_config"}

    issue_number = _find_latest_issue_number(output_root / edition)
    if issue_number is None:
        return {"status": "unavailable_local_artifacts"}

    edition_output_dir = output_root / edition
    html_path = edition_output_dir / f"issue_{issue_number:03d}.html"
    draft_path = edition_output_dir / f"issue_{issue_number:03d}.draft.json"
    manifest_path = edition_output_dir / f"issue_{issue_number:03d}.manifest.json"

    if not html_path.exists() or not draft_path.exists() or not manifest_path.exists():
        return {"status": "unavailable_local_artifacts"}

    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    signals = signal_store.read(program_id)
    confirm_result = confirm_issue(
        edition,
        issue_number,
        dry_run=True,
        reports_root=reports_root,
        archive_root=archive_root,
        output_root=output_root,
    )
    doctor_payload = _build_doctor_payload(
        report=run_doctor(
            edition,
            reports_root=reports_root,
            archive_root=archive_root,
            editions_root=editions_root,
            programs_root=programs_root,
            output_root=output_root,
            ado_probe=_deterministic_ado_probe,
        ),
        tip=None,
    )
    doctor_json = render_doctor_output(doctor_payload, format="json")

    return {
        "issue_number": issue_number,
        "report_html_sha256": _sha256_bytes(html_path.read_bytes()),
        "draft_json_sha256": _sha256_bytes(draft_path.read_bytes()),
        "manifest_json_sha256": _sha256_bytes(manifest_path.read_bytes()),
        "signal_count": len(signals),
        "signal_id_set_sha256": _sha256_json(sorted(signal.id for signal in signals)),
        "signal_content_sha256": _sha256_json(
            [
                {
                    "id": signal.id,
                    "timestamp": signal.timestamp.isoformat(),
                    "source": signal.source,
                    "program_id": signal.program_id,
                    "workstream_id": signal.workstream_id,
                    "entity_refs": list(signal.entity_refs),
                    "text": signal.text,
                    "raw_ref": signal.raw_ref,
                    "confidence": signal.confidence.value,
                    "metadata": signal.metadata,
                    "thread_id": signal.thread_id,
                    "review_policy": signal.review_policy.value if signal.review_policy is not None else None,
                }
                for signal in signals
            ]
        ),
        "confirm_snapshot_sha256": _sha256_json(confirm_result.snapshot),
        "confirm_manifest_sha256": _sha256_json(confirm_result.manifest),
        "doctor_json_sha256": _sha256_text(doctor_json),
    }


def _find_latest_issue_number(output_dir: Path) -> int | None:
    if not output_dir.exists():
        return None
    issues: dict[int, set[str]] = {}
    for path in output_dir.iterdir():
        match = _ISSUE_ARTIFACT_RE.match(path.name)
        if match is None:
            continue
        issue_number = int(match.group(1))
        issues.setdefault(issue_number, set()).add(match.group(2))
    ready_issues = [
        issue_number
        for issue_number, suffixes in issues.items()
        if {"draft.json", "html", "manifest.json"} <= suffixes
    ]
    return max(ready_issues) if ready_issues else None


def _collect_module_locs(repo_root: Path) -> dict[str, dict[str, int]]:
    modules: dict[str, dict[str, int]] = {}
    for path in sorted((repo_root / "src").rglob("*.py")):
        relative = path.relative_to(repo_root).as_posix()
        with path.open("r", encoding="utf-8") as handle:
            modules[relative] = {"loc": sum(1 for _ in handle)}
    return modules


def _collect_terminal_state_sites(repo_root: Path) -> list[str]:
    matches: list[str] = []
    for path in sorted((repo_root / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if any(isinstance(target, ast.Name) and target.id in {"TERMINAL_WORK_ITEM_STATES", "TERMINAL_WORK_ITEM_STATES_ADO"} for target in node.targets):
                    matches.append(f"{path.relative_to(repo_root).as_posix()}:{node.lineno}")
                    continue
                if _contains_terminal_state_collection(node.value):
                    matches.append(f"{path.relative_to(repo_root).as_posix()}:{node.lineno}")
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id in {"TERMINAL_WORK_ITEM_STATES", "TERMINAL_WORK_ITEM_STATES_ADO"}:
                    matches.append(f"{path.relative_to(repo_root).as_posix()}:{node.lineno}")
                elif node.value is not None and _contains_terminal_state_collection(node.value):
                    matches.append(f"{path.relative_to(repo_root).as_posix()}:{node.lineno}")
    return sorted(set(matches))


def _contains_terminal_state_collection(node: ast.AST) -> bool:
    values = _string_literals_from_collection(node)
    if len(values) < 2:
        return False
    return len(_KNOWN_TERMINAL_STATES.intersection(value.strip().lower() for value in values)) >= 2


def _string_literals_from_collection(node: ast.AST) -> set[str]:
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return {
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"set", "tuple", "list", "frozenset"} and node.args:
        return _string_literals_from_collection(node.args[0])
    return set()


def _collect_sidecar_read_sites(repo_root: Path) -> list[str]:
    matches: list[str] = []
    for path in sorted((repo_root / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = _call_name(node.func)
            if call_name not in {"open", "read_text", "load_yaml_mapping", "load_yaml_list", "safe_load"}:
                continue
            for arg in list(node.args) + [keyword.value for keyword in node.keywords]:
                if _contains_sidecar_suffix(arg):
                    matches.append(f"{path.relative_to(repo_root).as_posix()}:{node.lineno}")
                    break
    return sorted(set(matches))


def _contains_sidecar_suffix(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.endswith((".yaml", ".yml", ".json", ".jsonl"))
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and value.value.endswith((".yaml", ".yml", ".json", ".jsonl"))
            for value in node.values
        )
    return False


def _collect_ai_call_sites(repo_root: Path) -> list[str]:
    matches: list[str] = []
    for path in sorted((repo_root / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            chain = _attribute_chain(node.func)
            if chain == ("structured",) or chain[-3:] == ("chat", "completions", "create"):
                matches.append(f"{path.relative_to(repo_root).as_posix()}:{node.lineno}")
    return sorted(set(matches))


def _collect_policy_constants(repo_root: Path) -> dict[str, Any]:
    locations: dict[str, list[str]] = {}
    for base in (repo_root / "src" / "core", repo_root / "src" / "ai"):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                        continue
                    target_name = node.targets[0].id
                    if _POLICY_CONST_RE.match(target_name) and _is_literal_policy_value(node.value):
                        locations.setdefault(target_name, []).append(f"{path.relative_to(repo_root).as_posix()}:{node.lineno}")
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    target_name = node.target.id
                    if node.value is not None and _POLICY_CONST_RE.match(target_name) and _is_literal_policy_value(node.value):
                        locations.setdefault(target_name, []).append(f"{path.relative_to(repo_root).as_posix()}:{node.lineno}")
    result: dict[str, Any] = {}
    for name, refs in sorted(locations.items()):
        result[name] = refs[0] if len(refs) == 1 else refs
    return result


def _is_literal_policy_value(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int, float, bool)) or node.value is None
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal_policy_value(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is not None and _is_literal_policy_value(key) and _is_literal_policy_value(value) for key, value in zip(node.keys, node.values))
    return False


def _deterministic_ado_probe(*_args: Any, **_kwargs: Any) -> ADOProbeResult:
    return ADOProbeResult(
        reachable=True,
        auth_method="baseline",
        item_count=0,
        token_minutes_remaining=60,
        detail="baseline deterministic probe",
    )


def _working_tree_dirty(repo_root: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def _git_head_sha(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_json(value: Any) -> str:
    rendered = json.dumps(_to_jsonable(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _sha256_text(rendered)


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field_name: _to_jsonable(getattr(value, field_name))
            for field_name in value.__dataclass_fields__
        }
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, set):
        return [_to_jsonable(item) for item in sorted(value, key=lambda item: json.dumps(_to_jsonable(item), sort_keys=True, ensure_ascii=False))]
    return value


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _attribute_chain(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return tuple(reversed(parts))


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture the reproducible Vertex debt baseline.")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH, help="Path to write output/debt_baseline.json.")
    args = parser.parse_args()
    path = write_debt_baseline(output_path=args.output)
    print(path)


if __name__ == "__main__":
    main()

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

from src.core.archive_store import verify_archive_integrity
from src.core.exceptions import ConfigError, StateError
from src.core.models import ConfirmedDimension, EditionType, RiskLevel, Snapshot, SnapshotItem
from src.core.snapshot_store import ArchiveLock, write_confirmed


def _load_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "reconcile_archive_index.py"
    spec = importlib.util.spec_from_file_location("reconcile_archive_index", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("reconcile_archive_index", module)
    spec.loader.exec_module(module)
    return module


def _build_snapshot(issue_number: int) -> Snapshot:
    return Snapshot(
        issue_number=issue_number,
        generated_at=datetime(2026, 6, issue_number, 8, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 6, issue_number, 7, 30, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=1000 + issue_number,
                type="Feature",
                title=f"Issue {issue_number:03d}",
                state="Active",
                assigned_to="owner",
                area_path="One\\Demo",
                target_date=date(2026, 6, 30),
                risk_level=RiskLevel.LOW,
                tags=["demo"],
            ),
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Demo",
                name="Execution",
                risk=RiskLevel.LOW,
                prior_risk=None,
                item_count=1,
                ado_query_url="https://example.test/query",
            ),
        ),
    )


def _seed_canonical_issue(program_root: Path, edition: str, issue_number: int) -> None:
    archive_root = program_root / "archive"
    edition_root = archive_root / edition
    write_confirmed(
        edition=edition,
        issue_number=issue_number,
        snapshot=_build_snapshot(issue_number),
        archive_root=archive_root,
        acquire_lock=False,
    )
    (edition_root / "html").mkdir(parents=True, exist_ok=True)
    (edition_root / "md").mkdir(parents=True, exist_ok=True)
    (edition_root / "manifests").mkdir(parents=True, exist_ok=True)
    (edition_root / "eml").mkdir(parents=True, exist_ok=True)
    (edition_root / "html" / f"issue_{issue_number:03d}.html").write_text("<html/>", encoding="utf-8")
    (edition_root / "md" / f"issue_{issue_number:03d}.md").write_text("# Demo", encoding="utf-8")
    (edition_root / "manifests" / f"issue_{issue_number:03d}.json").write_text(
        json.dumps(
            {
                "issue_number": issue_number,
                "edition": edition,
                "started_at": datetime(2026, 6, issue_number, 7, 0, tzinfo=timezone.utc).isoformat(),
                "ended_at": datetime(2026, 6, issue_number, 8, 0, tzinfo=timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (edition_root / "eml" / f"issue_{issue_number:03d}.eml").write_text("From: demo", encoding="utf-8")


def test_apply_readd_restores_missing_index_entry(tmp_path: Path) -> None:
    module = _load_module()
    module.REPO_ROOT = tmp_path
    program_root = tmp_path / "programs" / "demo"
    edition = "demo_weekly"
    issue_number = 12
    _seed_canonical_issue(program_root, edition, issue_number)

    index_path = program_root / "archive" / edition / "index.json"
    index_path.write_text(json.dumps({"schema_version": "1.0", "edition": edition, "issues": []}), encoding="utf-8")

    result = module.main(
        [
            "--program",
            "demo",
            "--edition",
            edition,
            "--issue",
            str(issue_number),
            "--strategy",
            "readd",
            "--apply",
        ]
    )

    assert result == 0
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert [entry["issue_number"] for entry in payload["issues"]] == [issue_number]
    assert payload["issues"][0]["snapshot_path"].endswith(f"issue_{issue_number:03d}.snapshot.json")


def test_apply_drop_with_wipe_removes_index_and_files(tmp_path: Path) -> None:
    module = _load_module()
    module.REPO_ROOT = tmp_path
    program_root = tmp_path / "programs" / "demo"
    edition = "demo_weekly"
    issue_number = 13
    _seed_canonical_issue(program_root, edition, issue_number)

    index_path = program_root / "archive" / edition / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": edition,
                "issues": [
                    {
                        "issue_number": issue_number,
                        "generated_at": datetime(2026, 6, issue_number, 8, 0, tzinfo=timezone.utc).isoformat(),
                        "kind": "confirmed",
                        "snapshot_path": str(program_root / "archive" / edition / "snapshots" / f"issue_{issue_number:03d}.snapshot.json"),
                        "html_path": str(program_root / "archive" / edition / "html" / f"issue_{issue_number:03d}.html"),
                        "md_path": str(program_root / "archive" / edition / "md" / f"issue_{issue_number:03d}.md"),
                        "manifest_path": str(program_root / "archive" / edition / "manifests" / f"issue_{issue_number:03d}.json"),
                        "eml_path": str(program_root / "archive" / edition / "eml" / f"issue_{issue_number:03d}.eml"),
                        "metadata": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = module.main(
        [
            "--program",
            "demo",
            "--edition",
            edition,
            "--issue",
            str(issue_number),
            "--strategy",
            "drop",
            "--apply",
            "--wipe",
        ]
    )

    assert result == 0
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["issues"] == []
    assert not (program_root / "archive" / edition / "snapshots" / f"issue_{issue_number:03d}.snapshot.json").exists()
    assert verify_archive_integrity(edition, archive_root=program_root / "archive").ok


@pytest.fixture
def configured_archive(tmp_path: Path) -> tuple[ModuleType, Path, Path]:
    module = _load_module()
    module.REPO_ROOT = tmp_path
    program_root = tmp_path / "programs" / "demo"
    edition = "demo_weekly"
    _seed_canonical_issue(program_root, edition, 12)
    editions = program_root / "editions"
    editions.mkdir()
    (editions / f"{edition}.yaml").write_text("program_id: demo\n", encoding="utf-8")
    edition_root = program_root / "archive" / edition
    (edition_root / "staging").mkdir()
    entry = {
        "issue_number": 12,
        "generated_at": _build_snapshot(12).generated_at.isoformat(),
        "kind": "confirmed",
        "reason": "Retain reviewed reason",
        "metadata": {"review": "retained"},
    }
    unrelated = dict(entry, issue_number=11, reason="Unrelated issue")
    (edition_root / "index.json").write_text(
        json.dumps({"schema_version": "1.0", "edition": edition, "issues": [unrelated, entry]}),
        encoding="utf-8",
    )
    return module, program_root, edition_root


def _archive_bytes(program_root: Path) -> dict[str, bytes]:
    from tests.unit.test_archive_workspace_context import _bytes

    return _bytes(program_root)


@pytest.mark.parametrize("strategy", ["readd", "drop"])
def test_configured_archive_dry_run_uses_selected_canonical_paths(
    configured_archive: tuple[ModuleType, Path, Path], capsys: pytest.CaptureFixture[str], strategy: str,
) -> None:
    module, program_root, edition_root = configured_archive
    before = _archive_bytes(program_root)
    result = module.main([
        "--program", "demo", "--edition", "demo_weekly", "--issue", "12", "--strategy", strategy,
    ])
    assert result == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["issue_present_in_index"] is True
    if strategy == "readd":
        assert plan["missing"] == []
        assert plan["candidates"]["snapshot"] == str(edition_root / "snapshots" / "issue_012.snapshot.json")
    else:
        assert plan["files_at_risk"]["snapshot"] == str(edition_root / "snapshots" / "issue_012.snapshot.json")
    assert _archive_bytes(program_root) == before
    assert not (program_root / "programs").exists()


@pytest.mark.parametrize("strategy", ["readd", "drop"])
def test_configured_archive_apply_preserves_other_entries_and_metadata(
    configured_archive: tuple[ModuleType, Path, Path], strategy: str,
) -> None:
    module, program_root, edition_root = configured_archive
    index_path = edition_root / "index.json"
    before = json.loads(index_path.read_text(encoding="utf-8"))
    assert module.main([
        "--program", "demo", "--edition", "demo_weekly", "--issue", "12",
        "--strategy", strategy, "--apply",
    ]) == 0
    after = json.loads(index_path.read_text(encoding="utf-8"))
    assert after["issues"][0] == dict(
        before["issues"][0],
        eml_path=None, html_path=None, md_path=None, snapshot_path=None, manifest_path=None,
    )
    if strategy == "readd":
        assert after["issues"][1]["metadata"] == {"review": "retained"}
        assert after["issues"][1]["reason"] == "Retain reviewed reason"
        assert after["issues"][1]["snapshot_path"] == str(edition_root / "snapshots" / "issue_012.snapshot.json")
    else:
        assert len(after["issues"]) == 1
    assert not (program_root / "programs").exists()


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("invalid", ["snapshot", "html", "md", "manifest", "directory", "corrupt", "invalid_snapshot", "invalid_time"])
def test_readd_refuses_unusable_files_without_writes(
    configured_archive: tuple[ModuleType, Path, Path], capsys: pytest.CaptureFixture[str], invalid: str, apply: bool,
) -> None:
    module, program_root, edition_root = configured_archive
    paths = {
        "snapshot": edition_root / "snapshots" / "issue_012.snapshot.json",
        "html": edition_root / "html" / "issue_012.html",
        "md": edition_root / "md" / "issue_012.md",
        "manifest": edition_root / "manifests" / "issue_012.json",
    }
    if invalid in paths:
        paths[invalid].unlink()
    elif invalid == "directory":
        paths["html"].unlink()
        paths["html"].mkdir()
    elif invalid == "invalid_time":
        snapshot = json.loads(paths["snapshot"].read_text(encoding="utf-8"))
        snapshot["generated_at"] = "not-a-time"
        paths["snapshot"].write_text(json.dumps(snapshot), encoding="utf-8")
    else:
        paths["snapshot"].write_text("{" if invalid == "corrupt" else "{}", encoding="utf-8")
    before = _archive_bytes(program_root)
    args = [
        "--program", "demo", "--edition", "demo_weekly", "--issue", "12", "--strategy", "readd",
    ]
    assert module.main(args + (["--apply"] if apply else [])) == 1
    captured = capsys.readouterr()
    plan = json.loads(captured.out)
    assert "ERROR:" in captured.err
    if invalid in paths:
        assert plan["missing"] == [invalid]
    elif invalid == "directory":
        assert plan["missing"] == ["html"]
    else:
        assert plan["missing"] == []
        assert "snapshot" in captured.err.lower()
    expected = before | ({str((edition_root / ".archive.guard").relative_to(program_root)): b""} if apply else {})
    assert _archive_bytes(program_root) == expected
    assert not (program_root / "programs").exists()


@pytest.mark.parametrize("strategy", ["readd", "drop"])
def test_configured_archive_apply_refuses_orphaned_staging(
    configured_archive: tuple[ModuleType, Path, Path], strategy: str,
) -> None:
    module, program_root, edition_root = configured_archive
    staging = edition_root / "staging"
    (staging / "incomplete.json").write_text("{}", encoding="utf-8")
    before = _archive_bytes(program_root)
    assert module.main([
        "--program", "demo", "--edition", "demo_weekly", "--issue", "12",
        "--strategy", strategy, "--apply",
    ]) == 1
    assert _archive_bytes(program_root) == before | {
        str((edition_root / ".archive.guard").relative_to(program_root)): b"",
    }


@pytest.mark.parametrize("strategy", ["readd", "drop"])
def test_configured_archive_apply_retains_live_lock(
    configured_archive: tuple[ModuleType, Path, Path], strategy: str,
) -> None:
    module, program_root, edition_root = configured_archive
    with ArchiveLock(edition_root):
        before = _archive_bytes(program_root)
        with pytest.raises(StateError, match="locked"):
            module.main([
                "--program", "demo", "--edition", "demo_weekly", "--issue", "12",
                "--strategy", strategy, "--apply",
            ])
        assert _archive_bytes(program_root) == before


def test_reconcile_retains_edition_identifier_validation(
    configured_archive: tuple[ModuleType, Path, Path],
) -> None:
    module, program_root, _ = configured_archive
    before = _archive_bytes(program_root)
    with pytest.raises(ConfigError, match="Invalid edition identifier"):
        module.main([
            "--program", "demo", "--edition", "..", "--issue", "12", "--strategy", "readd",
        ])
    assert _archive_bytes(program_root) == before


@pytest.mark.parametrize("published", [False, True])
def test_readd_missing_index_and_optional_published_email(
    configured_archive: tuple[ModuleType, Path, Path], published: bool,
) -> None:
    module, _, edition_root = configured_archive
    index_path = edition_root / "index.json"
    index_path.unlink()
    published_path = edition_root / "published_eml" / "issue_012.published.eml"
    if published:
        published_path.parent.mkdir()
        published_path.write_text("From: demo", encoding="utf-8")
    args = [
        "--program", "demo", "--edition", "demo_weekly", "--issue", "12", "--strategy", "readd",
    ]
    assert module.main(args) == 0
    assert not index_path.exists()
    assert module.main(args + ["--apply"]) == 0
    entry = json.loads(index_path.read_text(encoding="utf-8"))["issues"][0]
    assert entry["metadata"] == ({"published_eml_path": str(published_path)} if published else {})

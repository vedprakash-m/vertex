import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core import snapshot_store
from src.core.program_fact_store import _build_skip_issue_facts
from src.core.program_reality import ProgramReality
from tests.support.synthetic_report_source import build_synthetic_report_source


@pytest.mark.parametrize("explicit_archive", [False, True])
def test_skip_issue_facts_keep_selected_workspace_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_archive: bool,
) -> None:
    source = build_synthetic_report_source(tmp_path / "source")
    programs_root = source / "selected-programs"
    (source / "programs").rename(programs_root)
    edition = "acme_weekly"
    archive_root = source / "selected-archive" if explicit_archive else None
    resolve_paths = snapshot_store.resolve_edition_paths
    paths = resolve_paths(edition, programs_root=programs_root)
    assert paths is not None
    index = (archive_root / edition if archive_root is not None else paths.archive_dir) / "index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps({
        "edition": edition,
        "issues": [{"issue_number": 2, "generated_at": "2026-05-05T00:00:00+00:00",
                    "kind": "skipped", "reason": "Synthetic skipped issue"}],
    }), encoding="utf-8")
    observed_roots = []

    def resolve_selected(edition_id: str, *, programs_root: Path):
        assert programs_root.resolve().is_relative_to(source.resolve())
        observed_roots.append(programs_root)
        return resolve_paths(edition_id, programs_root=programs_root)

    monkeypatch.setattr(snapshot_store, "resolve_edition_paths", resolve_selected)
    facts = _build_skip_issue_facts(
        "acme", recorded_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
        programs_root=programs_root, editions_root=None, archive_root=archive_root,
    )
    assert len(facts) == 1
    assert facts[0].payload["issue_number"] == 2
    if explicit_archive:
        ProgramReality.load("acme", programs_root=programs_root, edition_name=edition, archive_root=archive_root)
    else:
        ProgramReality.load("acme", programs_root=programs_root, edition_name=edition)
    assert observed_roots
    if not explicit_archive:
        assert set(observed_roots) == {programs_root}

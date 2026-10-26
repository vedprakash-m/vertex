from __future__ import annotations

from pathlib import Path

import pytest

from src.core import narrative_store


@pytest.mark.parametrize("explicit_reports_root", [False, True])
def test_archived_placeholders_use_selected_reports_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_reports_root: bool,
) -> None:
    archive_root = tmp_path / "archive"
    reports_root = tmp_path / ("drafts" if explicit_reports_root else "reports")
    directory = narrative_store.get_archive_root("synthetic_weekly", archive_root) / "narratives" / "issue_001"
    directory.mkdir(parents=True)
    (directory / "ws_delivery.md").write_text("Synthetic narrative.", encoding="utf-8")
    observed: list[Path] = []

    def inject(
        edition: str, issue_number: int, narratives: dict[str, str], reports_root: Path,
    ) -> dict[str, str]:
        assert edition == "synthetic_weekly"
        assert issue_number == 1
        observed.append(reports_root)
        return narratives

    monkeypatch.setattr(narrative_store, "inject_narrative_placeholders", inject)
    kwargs = {"reports_root": reports_root} if explicit_reports_root else {}
    result = narrative_store.load_archived_narratives("synthetic_weekly", 1, archive_root=archive_root, **kwargs)
    assert result == {"ws_delivery.md": "Synthetic narrative."}
    assert observed == [reports_root]

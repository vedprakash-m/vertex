from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.commands import report_ai
from src.commands.report_pipeline import assemble_stage


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_shared_report_signal_readers_preserve_selected_and_legacy_archive_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guarded: bool, explicit: bool,
) -> None:
    monkeypatch.setattr(report_ai, "load_bundle_with_mode", Mock(return_value=SimpleNamespace(mode="v2")))
    monkeypatch.setattr(report_ai, "resolve_edition", Mock(return_value=SimpleNamespace(
        program=SimpleNamespace(id="fixture"), workstreams=(),
        edition=SimpleNamespace(workstream_filter=None),
    )))
    monkeypatch.setattr(report_ai, "filter_workstreams", Mock(return_value=()))
    store = Mock()
    store.read.return_value = ()
    store.read_reviews.return_value = {}
    monkeypatch.setattr(report_ai, "build_signal_store_for_program_id", Mock(return_value=store))
    monkeypatch.setattr(report_ai, "load_program_knowledge", Mock())
    reality = Mock(side_effect=RuntimeError("Selected reality boundary"))
    monkeypatch.setattr(report_ai.ProgramReality, "load", reality)
    reader = assemble_stage._load_guarded_review_evidence if guarded else assemble_stage._load_report_signal_context
    archive_options = {"archive_root": tmp_path / "archive"} if explicit else {}
    with pytest.raises(RuntimeError, match="Selected reality boundary"):
        reader(
            edition_name="fixture", bundle=SimpleNamespace(config=SimpleNamespace(
                ado=SimpleNamespace(date_window_days=14),
            )), items=(), as_of=datetime(2026, 5, 24, tzinfo=timezone.utc),
            previous_snapshot=None, reports_root=tmp_path / "reports", **archive_options,
        )
    reality.assert_called_once_with("fixture", programs_root=tmp_path / "programs", **archive_options)
    assert not list(tmp_path.iterdir())


def test_legacy_injected_signal_loader_does_not_receive_new_archive_keyword(tmp_path: Path) -> None:
    def legacy_loader(*, edition_name, bundle, items, as_of, previous_snapshot, reports_root):
        return None

    result = report_ai._load_guarded_review_evidence(
        edition_name="fixture", bundle=Mock(), items=(),
        as_of=datetime(2026, 5, 24, tzinfo=timezone.utc),
        previous_snapshot=None, reports_root=tmp_path / "reports",
        load_report_signal_context=legacy_loader,
    )
    assert result.approved_signals == ()
    assert result.drift_patterns == ()


@pytest.mark.parametrize(("source", "reader", "selected"), [
    (Path("diff.py"), "_load_report_signal_context", "archive_root"),
    (Path("review_full.py"), "_load_guarded_review_evidence", "resolved_archive_root"),
    (Path("report_pipeline", "assemble_stage.py"), "_load_report_signal_context", "resolved_archive_root"),
])
def test_existing_consumers_forward_their_selected_archive(
    source: Path, reader: str, selected: str,
) -> None:
    command_root = Path(report_ai.__file__).parent
    tree = ast.parse((command_root / source).read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == reader
    ]
    assert len(calls) == 1
    assert any(
        keyword.arg == "archive_root" and isinstance(keyword.value, ast.Name)
        and keyword.value.id == selected for keyword in calls[0].keywords
    )

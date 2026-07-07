from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from src.commands.confirm_stages import post_confirm_support
from src.core.models import ReportData


def test_write_context_snapshot_for_issue_reads_workstreams_from_program_facts(tmp_path: Path, monkeypatch) -> None:
    milestone = object()
    risk = object()
    decision = object()
    workstream = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(post_confirm_support, "load_current_milestones", lambda *_args, **_kwargs: (milestone,))
    monkeypatch.setattr(post_confirm_support, "load_current_risk_entries", lambda *_args, **_kwargs: (risk,))
    monkeypatch.setattr(post_confirm_support, "load_current_decision_entries", lambda *_args, **_kwargs: (decision,))
    monkeypatch.setattr(post_confirm_support, "load_current_workstreams", lambda *_args, **_kwargs: (workstream,))
    monkeypatch.setattr(
        post_confirm_support,
        "_load_program_context",
        lambda *_args, **_kwargs: SimpleNamespace(maturity_level=SimpleNamespace(value=0)),
    )
    monkeypatch.setattr(
        post_confirm_support,
        "write_context_snapshot",
        lambda *args, **kwargs: captured.update(kwargs),
    )

    post_confirm_support.write_context_snapshot_for_issue(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=1,
        confirmed_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        prior_issue_entry=None,
    )

    assert captured["program_id"] == "acme"
    assert captured["milestones"] == [milestone]
    assert captured["risks"] == [risk]
    assert captured["decisions"] == [decision]
    assert captured["workstreams"] == [workstream]


def test_build_confirmed_eml_bytes_marks_draft_and_uses_default_subject(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(post_confirm_support, "_distribution_to", lambda _bundle: ("to@example.com",))
    monkeypatch.setattr(
        post_confirm_support,
        "_resolve_email_subject",
        lambda *, suggested_subject, default_subject: default_subject if not suggested_subject else suggested_subject,
    )
    monkeypatch.setattr(
        post_confirm_support,
        "build_eml_bytes",
        lambda **kwargs: captured.update(kwargs) or b"eml",
    )

    bundle = SimpleNamespace(
        config=SimpleNamespace(
            distribution=SimpleNamespace(cc=("cc@example.com",)),
            author=SimpleNamespace(display_name="Vertex", email="vertex@example.com"),
        )
    )

    result = post_confirm_support.build_confirmed_eml_bytes(
        bundle,
        issue_number=7,
        as_of=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc),
        html_body="<p>Hello</p>",
        markdown_body="Hello",
        suggested_subject="",
        generated_at=datetime(2026, 6, 6, 12, 5, tzinfo=timezone.utc),
        format_edition_title_fn=lambda _bundle, issue_number, _as_of: f"Issue {issue_number:03d}",
    )

    assert result == b"eml"
    assert captured["subject"] == "Issue 007"
    assert captured["mark_as_draft"] is True


def test_load_draft_continuation_contract_path_returns_existing_path(tmp_path: Path) -> None:
    contract_path = tmp_path / "acme_weekly" / "output" / "acme_weekly" / "issue_001" / "issue_001.continuation_contract.json"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text("{}", encoding="utf-8")

    assert (
        post_confirm_support.load_draft_continuation_contract_path(
            "acme_weekly",
            1,
            programs_root=tmp_path,
        )
        == contract_path
    )


def test_next_issue_number_advances_from_archive_index() -> None:
    index = SimpleNamespace(
        issues=(
            SimpleNamespace(issue_number=3),
            SimpleNamespace(issue_number=7),
        )
    )

    assert post_confirm_support.next_issue_number(index) == 8


def test_next_issue_narrative_templates_uses_continuity_prefix() -> None:
    report = cast(
        ReportData,
        SimpleNamespace(
            exec_summary_text="Summary",
            workstream_blurbs={"networking": "Narrative"},
        ),
    )

    templates = post_confirm_support.next_issue_narrative_templates(
        report,
        bundle=SimpleNamespace(),
        is_continuity_layout_fn=lambda _bundle: True,
    )

    assert templates == {
        "exec_summary.md": "Summary",
        "chapter_networking.md": "Narrative",
    }

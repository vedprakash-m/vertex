"""RELO-W4.7 / OD-9: observe-only communication budget and cooldown evaluator."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands.report import generate_report_draft
from src.commands.report_email import (
    assess_notify_communication_budgets,
    assess_report_email_communication_budget,
    format_communication_budget_observations,
)
from src.core.archive_store import write_confirmed_issue
from src.core.communication_budget import (
    DEFAULT_MAX_ASKS,
    CommunicationAudienceManifest,
    CommunicationBudgetFinding,
    assert_communication_budget,
    build_communication_audience_manifest,
    evaluate_communication_budget,
)
from src.core.exceptions import StateError
from src.core.models import (
    ConfirmedDimension,
    DRISummary,
    EditionType,
    FreshnessItem,
    NotifyPreview,
    RiskLevel,
    RunManifest,
    Snapshot,
    SnapshotItem,
)
from src.core.notification_state_store import ConfirmedNotification, append_confirmed_notify_run
from src.core.view_models import Top3Item
from tests.support.report_test_setup import disable_kusto_in_report_copy, reset_overrides_to_seed_state, stage_v2_report_workspace
from tests.support.synthetic_report_source import EDITION_NAME, build_synthetic_report_source
from tests.unit.test_commands_report import _sample_items

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def _codes(findings):
    return [f.code for f in findings]


def test_default_max_asks_is_five():
    assert DEFAULT_MAX_ASKS == 5


def test_clean_artifact_has_no_findings():
    findings = evaluate_communication_budget(
        audience_id="leads",
        asks=["a", "b", "c", "d", "e"],
        prior_sends=[NOW - timedelta(days=15)],
        cooldown_days=14,
        now=NOW,
    )
    assert findings == ()


def test_sixth_ask_is_over_budget():
    findings = evaluate_communication_budget(
        audience_id="leads",
        asks=["a", "b", "c", "d", "e", "f"],
        prior_sends=[],
        cooldown_days=14,
        now=NOW,
    )
    assert _codes(findings) == ["over_ask_budget"]
    assert findings[0].audience_id == "leads"
    assert "6" in findings[0].detail and "5" in findings[0].detail


def test_custom_max_asks():
    findings = evaluate_communication_budget(
        audience_id="leads", asks=["a", "b", "c"], prior_sends=[],
        cooldown_days=14, now=NOW, max_asks=2,
    )
    assert _codes(findings) == ["over_ask_budget"]


def test_audience_manifest_normalizes_recipients_and_material_changes() -> None:
    jordan_email = "jordan@" + "example.com"
    lead_email = "lead@" + "example.com"
    jordan_variant = " Jordan@" + "Example.com "
    manifest = build_communication_audience_manifest(
        audience=(jordan_variant, jordan_email, lead_email),
        asks=("  Ask one  ", "Ask one", "", "Change two"),
    )

    assert manifest == CommunicationAudienceManifest(
        audience_id=f"{jordan_email}, {lead_email}",
        recipients=(jordan_email, lead_email),
        material_asks=("Ask one", "Change two"),
    )


def test_budget_assertion_fails_before_send_with_audience_manifest() -> None:
    manifest = build_communication_audience_manifest(
        audience=("jordan@example.com",),
        asks=tuple(f"Ask {index}" for index in range(6)),
    )

    with pytest.raises(StateError, match="jordan@example.com.*6.*5"):
        assert_communication_budget(
            manifest=manifest,
            prior_sends=(),
            cooldown_days=14,
            now=NOW,
        )


def test_recent_prior_send_is_within_cooldown_using_latest_send():
    findings = evaluate_communication_budget(
        audience_id="leads",
        asks=["a"],
        prior_sends=[NOW - timedelta(days=30), NOW - timedelta(days=3)],
        cooldown_days=14,
        now=NOW,
    )
    assert _codes(findings) == ["within_cooldown"]
    assert "2026-11-12" in findings[0].detail


def test_cooldown_boundary_is_exclusive():
    findings = evaluate_communication_budget(
        audience_id="leads", asks=["a"], prior_sends=[NOW - timedelta(days=14)],
        cooldown_days=14, now=NOW,
    )
    assert findings == ()


def test_zero_cooldown_disables_cooldown_check():
    findings = evaluate_communication_budget(
        audience_id="leads", asks=["a"], prior_sends=[NOW],
        cooldown_days=0, now=NOW,
    )
    assert findings == ()


def test_findings_sorted_and_deterministic():
    kwargs = dict(
        audience_id="leads",
        asks=["a", "b", "c", "d", "e", "f"],
        prior_sends=[NOW - timedelta(days=1)],
        cooldown_days=14,
        now=NOW,
    )
    first = evaluate_communication_budget(**kwargs)
    assert _codes(first) == ["over_ask_budget", "within_cooldown"]
    assert first == evaluate_communication_budget(**kwargs)


def test_inputs_not_mutated_and_finding_frozen():
    asks = ["a", "b", "c", "d", "e", "f"]
    sends = [NOW - timedelta(days=1)]
    findings = evaluate_communication_budget(
        audience_id="leads", asks=asks, prior_sends=sends, cooldown_days=14, now=NOW,
    )
    assert asks == ["a", "b", "c", "d", "e", "f"]
    assert sends == [NOW - timedelta(days=1)]
    assert isinstance(findings[0], CommunicationBudgetFinding)
    with pytest.raises(FrozenInstanceError):
        findings[0].code = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prior_sends": [datetime(2026, 11, 1)]},
        {"now": datetime(2026, 11, 15)},
        {"cooldown_days": -1},
        {"max_asks": -1},
    ],
)
def test_invalid_inputs_rejected(kwargs):
    base = dict(audience_id="leads", asks=["a"], prior_sends=[], cooldown_days=14, now=NOW)
    base.update(kwargs)
    with pytest.raises(ValueError):
        evaluate_communication_budget(**base)


def test_notify_observe_communication_budget_surfaces_findings_without_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = build_synthetic_report_source(tmp_path / "src")
    ws = tmp_path / "ws"
    reports_root = stage_v2_report_workspace(source, ws)
    programs_root = ws / "programs"
    archive_root = ws / "archive"
    reset_overrides_to_seed_state(reports_root)

    six_findings = tuple(
        FreshnessItem(
            work_item_id=100 + idx,
            rule_id="stale",
            severity="warn",
            message=f"Need update {idx}",
            suggested_fix=None,
            action_label="Update",
            action_message=f"Update item {100 + idx}",
        )
        for idx in range(6)
    )
    summary = DRISummary(
        dri_name="Jordan",
        dri_email="jordan@example.com",
        items=six_findings,
        stale_count=6,
        overdue_count=0,
        open_count=6,
    )
    preview = NotifyPreview(
        to=("jordan@example.com",),
        cc=("author@example.com",),
        subject="Acme Weekly: 6 items need your update",
        html_body="<p>Hi</p>",
        md_body="Hi",
        attachments=(),
    )
    append_confirmed_notify_run(
        edition=EDITION_NAME,
        issue_number=1,
        confirmed_at=NOW - timedelta(days=2),
        notifications=(
            ConfirmedNotification(
                dri_email="jordan@example.com",
                to=("jordan@example.com",),
                cc=(),
                subject="Prior",
                work_item_ids=(100,),
            ),
        ),
        programs_root=programs_root,
    )

    findings = assess_notify_communication_budgets(
        edition_name=EDITION_NAME,
        previews=(preview,),
        dri_summaries=(summary,),
        programs_root=programs_root,
        now=NOW,
        cooldown_days=14,
    )
    assert _codes(findings) == ["over_ask_budget", "within_cooldown"]
    rendered = format_communication_budget_observations(findings)
    assert len(rendered) == 2
    assert all(line.startswith("[observe] Communication budget") for line in rendered)

    # Corrupt notification log fails closed rather than silently swallowing state errors
    bad_log = next((programs_root / "acme" / "publications" / EDITION_NAME / "notifications").glob("*.json"))
    bad_log.write_text("{not-json", encoding="utf-8")
    with pytest.raises(StateError):
        assess_notify_communication_budgets(
            edition_name=EDITION_NAME,
            previews=(preview,),
            dri_summaries=(summary,),
            programs_root=programs_root,
            now=NOW,
            cooldown_days=14,
        )

    # End-to-end notify --dry-run remains non-blocking and echoes observe findings
    bad_log.unlink()
    append_confirmed_notify_run(
        edition=EDITION_NAME,
        issue_number=1,
        confirmed_at=datetime.now(timezone.utc) - timedelta(days=2),
        notifications=(
            ConfirmedNotification(
                dri_email="maintainer@example.test",
                to=("maintainer@example.test",),
                cc=(),
                subject="Prior",
                work_item_ids=(100,),
            ),
        ),
        programs_root=programs_root,
    )
    monkeypatch.setattr("src.commands.notify.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.notify.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.notify.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.freshness._load_live_freshness_items",
        lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )
    res = CliRunner().invoke(app, ["notify", "--edition", EDITION_NAME, "--issue", "1", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "[observe] Communication budget (over_ask_budget): maintainer@example.test" in res.output
    assert "[observe] Communication budget (within_cooldown): maintainer@example.test" in res.output

    # freshness --notify --dry-run also surfaces observe findings cleanly without blocking
    monkeypatch.setattr("src.commands.freshness.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.freshness.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.freshness.PROGRAMS_ROOT", programs_root)
    fresh_res = CliRunner().invoke(app, ["freshness", "--edition", EDITION_NAME, "--notify", "--dry-run"])
    assert fresh_res.exit_code in {0, 3}, fresh_res.output
    assert "[observe] Communication budget (within_cooldown): maintainer@example.test" in fresh_res.output


def test_report_email_observe_communication_budget_records_manifest_without_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = build_synthetic_report_source(tmp_path / "src")
    ws = tmp_path / "ws"
    reports_root = stage_v2_report_workspace(source, ws)
    programs_root = ws / "programs"
    archive_root = ws / "archive"
    disable_kusto_in_report_copy(reports_root)
    reset_overrides_to_seed_state(reports_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(ws / "vertex-db"))

    prior_as_of = NOW - timedelta(days=2)
    snap1 = Snapshot(
        issue_number=77,
        generated_at=prior_as_of,
        ado_data_as_of=prior_as_of,
        edition_type=EditionType.DETAILED,
        items=(SnapshotItem(1, "Task", "Item", "Active", None, "Lane-0", NOW.date(), RiskLevel.LOW, []),),
        scorecards=(ConfirmedDimension("Lane-0", "Lane-0", RiskLevel.LOW, None, 1, ""),),
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=77,
        snapshot=snap1,
        html_body="<html><body>Issue 77</body></html>",
        markdown_body="# Issue 77",
        manifest=RunManifest(
            manifest_id="m-77",
            issue_number=77,
            edition=EDITION_NAME,
            started_at=prior_as_of,
            ended_at=prior_as_of,
            config_hash="sha256:" + "b" * 64,
            snapshot_hash="sha256:" + "a" * 64,
            html_hash="sha256:" + "c" * 64,
            md_hash="sha256:" + "d" * 64,
            ado_calls=0,
            ai_calls=0,
            ai_cost_usd=0.0,
            freshness_summary={},
            qg_results={"QG-8": True},
            git_sha=None,
        ),
        archive_root=archive_root,
    )

    draft = generate_report_draft(
        EDITION_NAME,
        issue_number=78,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        dry_run=True,
        as_of=NOW,
        work_item_loader=lambda _b, ts: (_sample_items(ts), 0),
        open_browser=False,
    )
    observations = draft.manifest.metadata.get("communication_budget_observations", [])
    assert any(obs["code"] == "within_cooldown" for obs in observations), observations

    six_top = tuple(
        Top3Item(
            item_type="Decision",
            text=f"Ask {idx}",
            owner="Jordan",
            ado_link=f"https://example.invalid/wi/{idx}",
            anchor=f"top-{idx}",
        )
        for idx in range(6)
    )
    over_findings = assess_report_email_communication_budget(
        edition_name=EDITION_NAME,
        audience_id="leadership@example.com",
        top_items=six_top,
        archive_root=archive_root,
        now=NOW,
        cooldown_days=7,
    )
    assert _codes(over_findings) == ["over_ask_budget", "within_cooldown"]

    # report --send-draft refuses a blocked communication artifact before dispatch.
    sent_messages = []
    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)
    monkeypatch.setattr("src.commands.report._build_draft_email_sender", lambda: sent_messages.append)
    monkeypatch.setattr("src.commands.report._load_live_work_items", lambda _b, ts: (_sample_items(ts), 0))
    send_res = CliRunner().invoke(
        app,
        ["report", "--edition", EDITION_NAME, "--issue", "78", "--as-of", "2026-11-15T12:00:00", "--send-draft"],
    )
    assert send_res.exit_code == 3, send_res.output
    assert sent_messages == [], send_res.output
    assert "Communication budget blocks report draft send" in send_res.output
    assert "within_cooldown" in send_res.output

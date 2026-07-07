from __future__ import annotations

import difflib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from src.commands.confirm import confirm_issue
from src.commands.report import generate_report_draft
from src.core.narrative_store import get_narratives_dir
from src.core.overrides_store import get_overrides_path
from src.core.snapshot_store import read_snapshot
from tests.support.ado_cassettes import load_cassette_payload, load_cassette_work_items
from tests.support.report_test_setup import disable_kusto_in_report_copy, reset_overrides_to_seed_state, stage_v2_report_workspace
from tests.unit.test_commands_confirm import _seed_high_risk_signal_coverage


GOLDEN_DIR = Path(__file__).resolve().parent / "snapshots"
FROZEN_NOW = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
FROZEN_MANIFEST_ID = UUID("11111111-1111-1111-1111-111111111111")
EDITION_NAME = "acme_weekly"


class GoldenFileMismatchError(AssertionError):
    pass


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.fromtimestamp(FROZEN_NOW.timestamp())
        return cls(
            FROZEN_NOW.year,
            FROZEN_NOW.month,
            FROZEN_NOW.day,
            FROZEN_NOW.hour,
            FROZEN_NOW.minute,
            FROZEN_NOW.second,
            FROZEN_NOW.microsecond,
            tzinfo=tz,
        )


def _load_golden(name: str) -> str | None:
    golden_path = GOLDEN_DIR / f"{name}.golden"
    if golden_path.exists():
        return golden_path.read_text(encoding="utf-8")
    return None


def _save_golden(name: str, content: str) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    (GOLDEN_DIR / f"{name}.golden").write_text(content, encoding="utf-8")


def _compare_with_golden(name: str, actual: str, update: bool) -> None:
    golden = _load_golden(name)
    if update or golden is None:
        _save_golden(name, actual)
        if golden is None:
            pytest.skip(f"Created new golden file: {name}.golden")
        return

    if actual != golden:
        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{name}.golden",
                tofile="actual",
            )
        )
        raise GoldenFileMismatchError(
            f"Output does not match golden file: {name}.golden\n\nDiff:\n{diff}"
        )


def test_report_and_confirm_html_snapshots(update_golden: bool, monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    monkeypatch.setattr("src.commands.report.datetime", FrozenDateTime)
    monkeypatch.setattr("src.commands.confirm.datetime", FrozenDateTime)
    monkeypatch.setattr("uuid.uuid4", lambda: FROZEN_MANIFEST_ID)

    draft = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=FROZEN_NOW,
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=FROZEN_NOW, work_item_id=910005)

    _compare_with_golden("report_draft_issue_001", draft.html_body, update_golden)

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    overrides_payload["decision_strip_ack"] = {
        "no_leadership_ask": True,
        "reason": "Freshness signals are already tracked in ADO and do not require new leadership action for this golden snapshot.",
    }
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    exec_summary_path = get_narratives_dir(EDITION_NAME, 1, reports_root) / "exec_summary.md"
    exec_summary_path.write_text("Confirmed executive summary.\n", encoding="utf-8")

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.archive_paths is not None

    archived_html = result.archive_paths.html_path.read_text(encoding="utf-8")
    archived_snapshot = read_snapshot(result.archive_paths.snapshot_path)

    _compare_with_golden("confirm_archive_issue_001", archived_html, update_golden)

    cold_start_payload = load_cassette_payload("cold_start")
    assert archived_snapshot.issue_number == 1
    assert archived_snapshot.generated_at == FROZEN_NOW
    assert archived_snapshot.ado_data_as_of == FROZEN_NOW
    assert archived_snapshot.schema_version == "1.0"
    assert len(archived_snapshot.items) == len(cold_start_payload["work_items"])


def test_report_kusto_html_snapshot(update_golden: bool, monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)
    config_path = tmp_path / "knowledge" / "golden_queries.yaml"
    config_payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    for query in config_payload["queries"]:
        if query["id"] == "fleet-health":
            query["render_as"] = "chart_image"
            break
    config_path.write_text(yaml.safe_dump(config_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    monkeypatch.setattr("src.commands.report.datetime", FrozenDateTime)
    monkeypatch.setattr("uuid.uuid4", lambda: FROZEN_MANIFEST_ID)
    monkeypatch.setattr("src.core.kusto_rendering._build_chart_image_data_url", lambda query, rows: "data:image/png;base64,ZmFrZQ==")

    draft = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=FROZEN_NOW,
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        kusto_query_executor=_golden_kusto_query_results,
        open_browser=False,
    )

    _compare_with_golden("report_draft_issue_001_kusto", draft.html_body, update_golden)


def test_report_narrative_html_snapshot(update_golden: bool, monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    monkeypatch.setattr("src.commands.report.datetime", FrozenDateTime)
    monkeypatch.setattr("uuid.uuid4", lambda: FROZEN_MANIFEST_ID)

    draft = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=FROZEN_NOW,
        edition_type_override="narrative",
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        open_browser=False,
    )

    _compare_with_golden("report_draft_issue_001_narrative", draft.html_body, update_golden)


def test_report_condensed_html_snapshot(update_golden: bool, monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    monkeypatch.setattr("src.commands.report.datetime", FrozenDateTime)
    monkeypatch.setattr("uuid.uuid4", lambda: FROZEN_MANIFEST_ID)

    draft = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=FROZEN_NOW,
        edition_type_override="condensed",
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        open_browser=False,
    )

    _compare_with_golden("report_draft_issue_001_condensed", draft.html_body, update_golden)


def _golden_kusto_query_results(query) -> list[dict[str, object]]:
    if query.id == "velocity-p50":
        return [{"Snapshot": "Current", "P50Hours": 4.2, "P90Hours": 7.8}]
    if query.id == "fleet-health":
        return [
            {"Date": "2026-05-01", "HealthyPct": 98.2, "Nodes": 1200},
            {"Date": "2026-05-02", "HealthyPct": 98.7, "Nodes": 1218},
            {"Date": "2026-05-03", "HealthyPct": 99.1, "Nodes": 1231},
        ]
    if query.id == "icm-active":
        return [
            {
                "IncidentId": "ICM-12345",
                "Severity": "3",
                "Title": "Fleet capacity alert",
                "Status": "Active",
                "IncidentUrl": "https://portal.microsofticm.com/imp/v3/incidents/details/12345",
            }
        ]
    if query.id == "icm-mttr":
        return [{"Severity": "SEV3", "AvgHours": 3.8, "Count": 4}]
    if query.id == "readiness_observability_coverage":
        return [{"coverage_pct": 97.4, "CoveredTenantCount": 148, "ExpectedTenantCount": 152}]
    if query.id == "readiness_capacity_headroom":
        return [{"headroom_pct": 91.2, "WithinTarget": 83, "TotalDeployments": 91}]
    if query.id == "readiness_dora_fail_rate":
        return [{"fail_rate_pct": 2.1, "FailCount": 4, "ObservedChecks": 190}]
    if query.id == "bios-ap-shared-service-pct":
        return [{"IsGoodStorageTotal": 95.0, "IsGoodStorageGen7": 92.0, "IsGoodStorageGen8": 96.0, "IsGoodStorageGen9": 98.0}]
    if query.id == "wingtip-fleet-rollout-pct":
        return [{"RolloutPct": 88.5}]
    raise AssertionError(f"Unexpected Kusto query id: {query.id}")


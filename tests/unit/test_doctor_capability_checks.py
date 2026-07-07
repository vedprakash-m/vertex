from __future__ import annotations

from datetime import date
from pathlib import Path

from src.commands.doctor_checks.capability_checks import capability_review_latest_detail, run_capability_review_check


def test_capability_review_latest_detail_formats_date() -> None:
    assert capability_review_latest_detail(date(2026, 5, 17)) == " Latest review: 2026-05-17."


def test_run_capability_review_check_warns_on_missing_review_dates(tmp_path: Path) -> None:
    program_root = tmp_path / "programs" / "demo"
    program_root.mkdir(parents=True)
    (program_root / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo Program",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (program_root / "capability_status.yaml").write_text(
        "\n".join(
            (
                "schema_version: '1.0'",
                "capabilities:",
                "  - id: kusto_activation",
                "    status: in_progress",
                "    summary: Kusto activation is explicitly in progress for this doctor test.",
                "    degradation: Live cluster validation is still pending.",
                "    last_reviewed_on: 2026-05-17",
                "  - id: m365_activation",
                "    status: deferred",
                "    summary: M365 activation is explicitly deferred for this doctor test.",
                "    degradation: WorkIQ enrichment remains inactive.",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    check = run_capability_review_check("demo", tmp_path / "programs")

    assert check is not None
    assert check.status == "warn"
    assert "M365 activation" in check.detail
    assert "Latest review: 2026-05-17" in check.detail
    assert check.metadata is not None
    assert check.metadata["missing_review_labels"] == ["M365 activation"]

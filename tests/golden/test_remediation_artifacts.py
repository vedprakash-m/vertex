from __future__ import annotations
import pytest
from pathlib import Path
pytestmark = pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "editions").exists(), reason="Requires private data")

import difflib
import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.config_loader import load_report_bundle
from src.core.models import Revision, RiskLevel, WorkItem
from src.core.quality_matrix_engine import build_quality_matrix
from src.core.remediation_engine import build_remediation_report


GOLDEN_DIR = Path(__file__).resolve().parent / "snapshots"


class GoldenFileMismatchError(AssertionError):
    pass


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


def test_quality_and_remediation_json_snapshots(update_golden: bool, repo_root: Path, tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    shutil.copytree(repo_root / "reports" / "schemas", reports_root / "schemas")
    bundle = load_report_bundle("acme_weekly")
    as_of = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    items = (
        _dd_performance_item(as_of, target_date=None, changed_date=as_of - timedelta(days=10)),
        _nova_networking_item(as_of),
    )

    matrix = build_quality_matrix(
        bundle=bundle,
        issue_number=1,
        generated_at=as_of,
        current_items=items,
        previous_issue_number=76,
    )
    remediation = build_remediation_report(matrix)

    quality_json = json.dumps(_to_jsonable(matrix), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    remediation_json = json.dumps(_to_jsonable(remediation), indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    _compare_with_golden("quality_matrix_issue_001", quality_json, update_golden)
    _compare_with_golden("remediation_issue_001", remediation_json, update_golden)


def _dd_performance_item(
    as_of: datetime,
    *,
    target_date: date | None = date(2026, 5, 22),
    changed_date: datetime | None = None,
) -> WorkItem:
    change_time = changed_date or as_of
    return WorkItem(
        id=910001,
        type="Feature",
        title="[Acme-DD] Performance Signoff",
        state="Active",
        assigned_to="Fixture Owner",
        assigned_to_email="fixture.owner@example.com",
        area_path="One\\Adventure\\XDirect\\Storage",
        iteration_path="FY26\\Sprint 20",
        target_date=target_date,
        risk_level=RiskLevel.HIGH,
        tags=["DDPFPilot", "DDPFReportGenerator", "PerfTesting", "NOVADD Perf"],
        custom_fields={"changed_date": change_time.isoformat()},
        revisions=[
            Revision(
                work_item_id=910001,
                rev_number=2,
                changed_by="Fixture Owner",
                changed_by_email="fixture.owner@example.com",
                changed_date=change_time,
                fields_changed={"State": ("New", "Active")},
            )
        ],
        comments=[],
        fetched_at=as_of,
    )


def _nova_networking_item(as_of: datetime) -> WorkItem:
    return WorkItem(
        id=920001,
        type="Feature",
        title="RDMA parity validation for networking",
        state="Active",
        assigned_to="Sam Rivera",
        assigned_to_email="sam@example.com",
        area_path="One\\Adventure\\Networking",
        iteration_path="FY26\\Sprint 20",
        target_date=date(2026, 5, 19),
        risk_level=RiskLevel.MEDIUM,
        tags=["repo:Networking-NMAgent"],
        custom_fields={"changed_date": as_of.isoformat()},
        revisions=[
            Revision(
                work_item_id=920001,
                rev_number=5,
                changed_by="Sam Rivera",
                changed_by_email="sam@example.com",
                changed_date=as_of,
                fields_changed={"State": ("New", "Active")},
            )
        ],
        comments=[],
        fetched_at=as_of,
    )


def _to_jsonable(value):
    if hasattr(value, "__dataclass_fields__"):
        return {field_name: _to_jsonable(getattr(value, field_name)) for field_name in value.__dataclass_fields__}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    return value

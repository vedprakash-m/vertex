"""FR-SG-27: Measurement spine — per-issue program metrics (schema + YAML store)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.core.config_loader import PROGRAMS_ROOT

# FR-SG-52: Explicit numerator/denominator definitions for each IssueMetrics field.
METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "claim_coverage": {
        "numerator": "approved claims with an ADO-traceable backing entity",
        "denominator": "material claims (excludes config echoes and plane-1 paraphrases)",
        "range": "[0.0, 1.0]",
    },
    "source_health_pct": {
        "numerator": "healthy required signal sources (freshness ≤ threshold, no auth failures)",
        "denominator": "total required signal sources in the slice contract",
        "range": "[0.0, 1.0]",
    },
    "provenance_confidence": {
        "numerator": "claims with high or medium source_confidence_tier",
        "denominator": "total open claims",
        "range": "[0.0, 1.0]",
    },
    "baseline_parity_score": {
        "numerator": "dimensions whose current value matches the trusted baseline ± tolerance",
        "denominator": "total auditable dimensions (requires FR-SG-26 corpus)",
        "range": "[0.0, 1.0]",
    },
    "manual_rewrite_rate": {
        "numerator": "AI-proposed sections accepted after manual edits",
        "denominator": "total AI-proposed sections accepted (requires edit_patterns.jsonl)",
        "range": "[0.0, 1.0]",
    },
}


@dataclass(frozen=True, slots=True)
class IssueMetrics:
    program_id: str
    issue_number: int
    edition_id: str
    computed_at: datetime
    override_count: int
    # The following metrics require richer context not available at initial wiring;
    # they are None until the full measurement spine is populated.
    claim_coverage: float | None
    source_health_pct: float | None
    provenance_confidence: float | None
    baseline_parity_score: float | None  # requires FR-SG-26 corpus
    manual_rewrite_rate: float | None     # requires edit_patterns.jsonl


def _metrics_path(
    program_id: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    return programs_root / program_id / "metrics" / f"issue_{issue_number}.yaml"


def write_issue_metrics(
    metrics: IssueMetrics,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Persist IssueMetrics to programs/<prog>/metrics/issue_<n>.yaml."""
    path = _metrics_path(metrics.program_id, metrics.issue_number, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "program_id": metrics.program_id,
        "issue_number": metrics.issue_number,
        "edition_id": metrics.edition_id,
        "computed_at": metrics.computed_at.isoformat(),
        "override_count": metrics.override_count,
        "claim_coverage": metrics.claim_coverage,
        "source_health_pct": metrics.source_health_pct,
        "provenance_confidence": metrics.provenance_confidence,
        "baseline_parity_score": metrics.baseline_parity_score,
        "manual_rewrite_rate": metrics.manual_rewrite_rate,
    }
    path.write_text(yaml.safe_dump(record, sort_keys=False, allow_unicode=True), encoding="utf-8")


def load_issue_metrics(
    program_id: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> IssueMetrics | None:
    """Load IssueMetrics for a given issue, or None if not yet computed."""
    path = _metrics_path(program_id, issue_number, programs_root=programs_root)
    if not path.exists():
        return None
    record = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(record, dict):
        raise ValueError(f"Expected YAML mapping in {path}.")
    return IssueMetrics(
        program_id=_required_string(record.get("program_id"), field_name="program_id"),
        issue_number=_required_int(record.get("issue_number"), field_name="issue_number"),
        edition_id=_required_string(record.get("edition_id"), field_name="edition_id"),
        computed_at=_parse_required_datetime(record.get("computed_at"), field_name="computed_at"),
        override_count=_required_int(record.get("override_count"), field_name="override_count"),
        claim_coverage=_optional_float(record.get("claim_coverage"), field_name="claim_coverage"),
        source_health_pct=_optional_float(record.get("source_health_pct"), field_name="source_health_pct"),
        provenance_confidence=_optional_float(record.get("provenance_confidence"), field_name="provenance_confidence"),
        baseline_parity_score=_optional_float(record.get("baseline_parity_score"), field_name="baseline_parity_score"),
        manual_rewrite_rate=_optional_float(record.get("manual_rewrite_rate"), field_name="manual_rewrite_rate"),
    )


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _required_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _parse_required_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone information")
    return parsed.astimezone(timezone.utc)


def _optional_float(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be numeric")
    return float(value)

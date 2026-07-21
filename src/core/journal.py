from __future__ import annotations

from dataclasses import replace
from collections.abc import Iterator, Mapping
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import json
from src.core.jsonl_utils import append_jsonl_line, parse_jsonl_line
from pathlib import Path
import re
import shutil
from typing import Any, Literal, cast

from src.core.models import Confidence
from src.core.models_v2 import ReviewPolicy, Signal, SignalReviewDecision, SignalThreadLink, SignalUsageMarker
from src.core.signal_classification import classify_signal


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"

# High-risk append-only files — grow with every review decision / usage marker
# (reviews.jsonl) and every signal thread link (signal_threads.jsonl).  The
# weekly journal files (????-W??.jsonl) already have natural rotation via
# ``archive_weekly_journal_files`` and don't need ``max_bytes``.  Rotated at
# 10 MB (spec §11.3 Phase 5 / D-23) to bound on-disk footprint.
_REVIEWS_MAX_BYTES = 10 * 1024 * 1024
_SIGNAL_THREADS_MAX_BYTES = 10 * 1024 * 1024


def get_program_journal_dir(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal"


def get_program_journal_archive_dir(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal_archive"


def get_reviews_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_program_journal_dir(program_id, programs_root) / "reviews.jsonl"


def get_signal_threads_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_program_journal_dir(program_id, programs_root) / "signal_threads.jsonl"


def get_week_key(timestamp: datetime) -> str:
    resolved = _require_utc_timestamp(timestamp)
    iso_year, iso_week, _ = resolved.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def archive_weekly_journal_files(
    program_id: str,
    *,
    before_week: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[Path, ...]:
    cutoff_key = _parse_week_key(before_week)
    journal_dir = get_program_journal_dir(program_id, programs_root)
    archive_dir = get_program_journal_archive_dir(program_id, programs_root)
    moved_paths: list[Path] = []

    if not journal_dir.exists():
        return ()

    archive_dir.mkdir(parents=True, exist_ok=True)
    for source_path in sorted(path for path in journal_dir.glob("????-W??.jsonl") if path.is_file()):
        week_key = _parse_week_key(source_path.stem)
        if week_key >= cutoff_key:
            continue
        destination_path = archive_dir / source_path.name
        if destination_path.exists():
            raise FileExistsError(f"Archive target already exists: {destination_path}")
        shutil.move(str(source_path), str(destination_path))
        moved_paths.append(destination_path)

    return tuple(moved_paths)


def archive_weekly_journal_files_by_retention(
    program_id: str,
    *,
    as_of: datetime,
    retention_days_by_source: Mapping[str, int] | None = None,
    default_retention_days: int = 365,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[Path, ...]:
    resolved_as_of = _require_utc_timestamp(as_of)
    resolved_retention_days = dict(retention_days_by_source or {})
    journal_dir = get_program_journal_dir(program_id, programs_root)
    archive_dir = get_program_journal_archive_dir(program_id, programs_root)
    moved_paths: list[Path] = []

    if not journal_dir.exists():
        return ()

    archive_dir.mkdir(parents=True, exist_ok=True)
    for source_path in sorted(path for path in journal_dir.glob("????-W??.jsonl") if path.is_file()):
        if not _weekly_journal_file_is_retention_eligible(
            source_path,
            as_of=resolved_as_of,
            retention_days_by_source=resolved_retention_days,
            default_retention_days=default_retention_days,
        ):
            continue
        destination_path = archive_dir / source_path.name
        if destination_path.exists():
            raise FileExistsError(f"Archive target already exists: {destination_path}")
        shutil.move(str(source_path), str(destination_path))
        moved_paths.append(destination_path)

    return tuple(moved_paths)


def get_weekly_journal_path(program_id: str, timestamp: datetime, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_program_journal_dir(program_id, programs_root) / f"{get_week_key(timestamp)}.jsonl"


def append_signal(
    signal: Signal,
    programs_root: Path = PROGRAMS_ROOT,
    *,
    partition_at: datetime | None = None,
) -> Path:
    target = get_weekly_journal_path(
        signal.program_id,
        partition_at or datetime.now(timezone.utc),
        programs_root,
    )
    _append_jsonl(target, _signal_to_record(classify_signal(signal)))
    return target


def append_review_decision(program_id: str, decision: SignalReviewDecision, programs_root: Path = PROGRAMS_ROOT) -> Path:
    target = get_reviews_path(program_id, programs_root)
    _append_jsonl(target, _review_decision_to_record(decision), max_bytes=_REVIEWS_MAX_BYTES)
    return target


def append_usage_marker(program_id: str, marker: SignalUsageMarker, programs_root: Path = PROGRAMS_ROOT) -> Path:
    target = get_reviews_path(program_id, programs_root)
    _append_jsonl(target, _usage_marker_to_record(marker), max_bytes=_REVIEWS_MAX_BYTES)
    return target


def append_signal_thread_link(program_id: str, link: SignalThreadLink, programs_root: Path = PROGRAMS_ROOT) -> Path:
    target = get_signal_threads_path(program_id, programs_root)
    _append_jsonl(target, _signal_thread_link_to_record(link), max_bytes=_SIGNAL_THREADS_MAX_BYTES)
    return target


def read_signals(
    program_id: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    workstream_id: str | None = None,
    require_committed_gather_run: bool = False,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[Signal, ...]:
    start_ts = _require_utc_timestamp(start) if start is not None else None
    end_ts = _require_utc_timestamp(end) if end is not None else None
    thread_links = load_latest_signal_threads(program_id, programs_root=programs_root)
    committed_run_ids: set[str] | None = None
    if require_committed_gather_run:
        from src.core.gather_run_manifest import get_verified_committed_run_ids

        committed_run_ids = set(get_verified_committed_run_ids(program_id, programs_root=programs_root))

    signals: list[Signal] = []
    for path in _iter_weekly_journal_paths(program_id, programs_root):
        for record in _read_jsonl(path):
            signal = _signal_from_record(record)
            if committed_run_ids is not None and signal.gather_run_id is not None and signal.gather_run_id not in committed_run_ids:
                continue
            thread_link = thread_links.get(signal.id)
            if thread_link is not None and signal.thread_id != thread_link.thread_id:
                signal = replace(signal, thread_id=thread_link.thread_id)
            if start_ts is not None and signal.timestamp < start_ts:
                continue
            if end_ts is not None and signal.timestamp > end_ts:
                continue
            if workstream_id is not None and signal.workstream_id != workstream_id:
                continue
            signals.append(signal)
    signals.sort(key=lambda entry: (entry.timestamp, entry.id))
    return tuple(signals)


def read_review_log(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[SignalReviewDecision | SignalUsageMarker, ...]:
    path = get_reviews_path(program_id, programs_root)
    if not path.exists():
        return ()
    entries: list[SignalReviewDecision | SignalUsageMarker] = []
    for record in _read_jsonl(path):
        record_type = str(record.get("record_type") or "review")
        if record_type == "usage_marker":
            entries.append(_usage_marker_from_record(record))
        else:
            entries.append(_review_decision_from_record(record))
    entries.sort(key=_review_log_sort_key)
    return tuple(entries)


def load_latest_review_decisions(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, SignalReviewDecision]:
    decisions: dict[str, SignalReviewDecision] = {}
    for entry in read_review_log(program_id, programs_root=programs_root):
        if isinstance(entry, SignalReviewDecision):
            decisions[entry.signal_id] = entry
    return decisions


def read_signal_thread_log(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[SignalThreadLink, ...]:
    path = get_signal_threads_path(program_id, programs_root)
    if not path.exists():
        return ()
    entries = [_signal_thread_link_from_record(record) for record in _read_jsonl(path)]
    entries.sort(key=lambda entry: (entry.linked_at, entry.signal_id))
    return tuple(entries)


def load_latest_signal_threads(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, SignalThreadLink]:
    links: dict[str, SignalThreadLink] = {}
    for entry in read_signal_thread_log(program_id, programs_root=programs_root):
        links[entry.signal_id] = entry
    return links


def _iter_weekly_journal_paths(program_id: str, programs_root: Path) -> tuple[Path, ...]:
    journal_dir = get_program_journal_dir(program_id, programs_root)
    archive_dir = get_program_journal_archive_dir(program_id, programs_root)
    ordered_paths: list[Path] = []
    seen_names: set[str] = set()

    for root_dir in (journal_dir, archive_dir):
        if not root_dir.exists():
            continue
        for path in sorted(path for path in root_dir.glob("????-W??.jsonl") if path.is_file()):
            if path.name in seen_names:
                continue
            seen_names.add(path.name)
            ordered_paths.append(path)

    return tuple(sorted(ordered_paths, key=lambda path: path.name))


_WEEK_KEY_PATTERN = re.compile(r"^(\d{4})-W(\d{2})$")


def _weekly_journal_file_is_retention_eligible(
    path: Path,
    *,
    as_of: datetime,
    retention_days_by_source: Mapping[str, int],
    default_retention_days: int,
) -> bool:
    signals = tuple(_signal_from_record(record) for record in _read_jsonl(path))
    if not signals:
        return False

    for signal in signals:
        retention_days = _retention_days_for_source(
            signal.source,
            retention_days_by_source=retention_days_by_source,
            default_retention_days=default_retention_days,
        )
        retention_cutoff = as_of - timedelta(days=retention_days)
        if signal.timestamp > retention_cutoff:
            return False
    return True


def _retention_days_for_source(
    source: str,
    *,
    retention_days_by_source: Mapping[str, int],
    default_retention_days: int,
) -> int:
    normalized_source = source.strip()
    if normalized_source in retention_days_by_source:
        return retention_days_by_source[normalized_source]

    prefix = normalized_source.split("/", 1)[0]
    if prefix in retention_days_by_source:
        return retention_days_by_source[prefix]

    return default_retention_days


def _parse_week_key(value: str) -> tuple[int, int]:
    match = _WEEK_KEY_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"Invalid week key '{value}'. Expected YYYY-Www.")
    year = int(match.group(1))
    week = int(match.group(2))
    date.fromisocalendar(year, week, 1)
    return year, week


def _append_jsonl(path: Path, record: dict[str, Any], *, max_bytes: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, default=_json_default) + "\n"
    append_jsonl_line(path, payload, max_bytes=max_bytes)


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            payload = parse_jsonl_line(line)
            if not isinstance(payload, dict):
                raise ValueError(f"Expected JSON object in {path}, found {type(payload).__name__}.")
            yield payload


def _signal_to_record(signal: Signal) -> dict[str, Any]:
    return {
        "id": signal.id,
        "ts": _require_utc_timestamp(signal.timestamp).isoformat(),
        "src": signal.source,
        "prog": signal.program_id,
        "ws": signal.workstream_id,
        "refs": list(signal.entity_refs),
        "text": signal.text,
        "raw_ref": signal.raw_ref,
        "conf": signal.confidence.value,
        "meta": signal.metadata,
        "thread_id": signal.thread_id,
        "review_policy": signal.review_policy.value if signal.review_policy is not None else None,
        "gather_run_id": signal.gather_run_id,
    }


def _signal_from_record(record: dict[str, Any]) -> Signal:
    refs = tuple(str(entry) for entry in record.get("refs") or ())
    metadata = record.get("meta")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("Signal metadata must be a JSON object or null.")
    return classify_signal(Signal(
        id=str(record["id"]),
        timestamp=_parse_datetime(record["ts"]),
        source=str(record["src"]),
        program_id=str(record["prog"]),
        workstream_id=(_optional_string(record.get("ws"))),
        entity_refs=refs,
        text=str(record["text"]),
        raw_ref=_optional_string(record.get("raw_ref")),
        confidence=Confidence.from_string(str(record["conf"])),
        metadata={str(key): value for key, value in metadata.items()} if metadata is not None else None,
        thread_id=_optional_string(record.get("thread_id")),
        review_policy=(
            ReviewPolicy.from_string(str(record["review_policy"]))
            if record.get("review_policy") is not None
            else None
        ),
        gather_run_id=_optional_string(record.get("gather_run_id")),
    ))


def signal_to_record(signal: Signal) -> dict[str, Any]:
    """Public alias of ``_signal_to_record`` -- ADF-W1.5's prefetch snapshot
    payload serialization reuses this exact round-trip rather than inventing
    a second one."""
    return _signal_to_record(signal)


def signal_from_record(record: dict[str, Any]) -> Signal:
    """Public alias of ``_signal_from_record`` (see ``signal_to_record``)."""
    return _signal_from_record(record)


def _review_decision_to_record(decision: SignalReviewDecision) -> dict[str, Any]:
    return {
        "signal_id": decision.signal_id,
        "decision": decision.decision,
        "reviewed_at": _require_utc_timestamp(decision.reviewed_at).isoformat(),
        "reviewed_by": decision.reviewed_by,
        "note": decision.note,
        "record_type": decision.record_type,
    }


def _review_decision_from_record(record: dict[str, Any]) -> SignalReviewDecision:
    decision = str(record["decision"]).strip().lower()
    if decision not in {"approved", "dismissed", "deferred"}:
        raise ValueError(f"Unsupported review decision '{record['decision']}'.")
    return SignalReviewDecision(
        signal_id=str(record["signal_id"]),
        decision=cast(Literal["approved", "dismissed", "deferred"], decision),
        reviewed_at=_parse_datetime(record["reviewed_at"]),
        reviewed_by=str(record["reviewed_by"]),
        note=_optional_string(record.get("note")),
    )


def _usage_marker_to_record(marker: SignalUsageMarker) -> dict[str, Any]:
    return {
        "signal_id": marker.signal_id,
        "issue_number": marker.issue_number,
        "edition_id": marker.edition_id,
        "manifest_id": marker.manifest_id,
        "used_at": _require_utc_timestamp(marker.used_at).isoformat(),
        "record_type": marker.record_type,
    }


def _usage_marker_from_record(record: dict[str, Any]) -> SignalUsageMarker:
    return SignalUsageMarker(
        signal_id=str(record["signal_id"]),
        issue_number=int(record["issue_number"]),
        edition_id=str(record["edition_id"]),
        manifest_id=str(record["manifest_id"]),
        used_at=_parse_datetime(record["used_at"]),
    )


def _signal_thread_link_to_record(link: SignalThreadLink) -> dict[str, Any]:
    return {
        "signal_id": link.signal_id,
        "thread_id": link.thread_id,
        "linked_at": _require_utc_timestamp(link.linked_at).isoformat(),
        "linked_by": link.linked_by,
        "record_type": link.record_type,
    }


def _signal_thread_link_from_record(record: dict[str, Any]) -> SignalThreadLink:
    return SignalThreadLink(
        signal_id=str(record["signal_id"]),
        thread_id=str(record["thread_id"]),
        linked_at=_parse_datetime(record["linked_at"]),
        linked_by=str(record["linked_by"]),
    )


def _review_log_sort_key(entry: SignalReviewDecision | SignalUsageMarker) -> tuple[datetime, str, int]:
    if isinstance(entry, SignalUsageMarker):
        return (entry.used_at, entry.signal_id, 1)
    return (entry.reviewed_at, entry.signal_id, 0)


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Expected ISO timestamp string, found {type(value).__name__}.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include timezone information.")
    return parsed.astimezone(timezone.utc)


def _require_utc_timestamp(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("Timestamp is required.")
    if value.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _require_utc_timestamp(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from src.core.jsonl_utils import parse_jsonl_line
import os
from pathlib import Path
import portalocker
import subprocess
from typing import Any
from uuid import uuid4

import yaml

from src.core.feedback._advisory_yaml import load_advisory_yaml, write_advisory_yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
PROGRAMS_ROOT = REPO_ROOT / "programs"
SCHEMA_VERSION = "1.0"
DEFAULT_MIN_WEIGHT = 0.2
DEFAULT_EMA_ALPHA = 0.1
DEFAULT_CONFIRMATION_WEIGHT = 2.0


class SalienceModelError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EditPatternObservation:
    workstream_id: str
    recorded_at: datetime
    author_override_magnitude: float


@dataclass(frozen=True, slots=True)
class SalienceEvent:
    event_id: str
    recorded_at: datetime
    anomaly_id: str
    workstream_id: str
    action: str
    work_item_id: int | None = None
    decision_latency_ms: int | None = None
    weight_before: float | None = None
    weight_after: float | None = None
    confirmed_within_30d: bool | None = None


@dataclass(frozen=True, slots=True)
class WorkstreamSalience:
    workstream_id: str
    attention_weight: float
    sample_count: int
    average_override_magnitude: float
    last_event_at: datetime


@dataclass(frozen=True, slots=True)
class AuthorSalienceModel:
    program_id: str
    schema_version: str
    updated_at: datetime
    author_alias: str
    ema_alpha: float
    min_weight: float
    workstreams: tuple[WorkstreamSalience, ...]


def get_author_salience_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_feedback" / "author_salience.yaml"


def get_salience_events_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_feedback" / "salience_events.jsonl"


def refresh_author_salience(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    author_alias: str | None = None,
    min_weight: float | None = None,
    ema_alpha: float | None = None,
    as_of: datetime | None = None,
    dry_run: bool = False,
) -> tuple[AuthorSalienceModel, Path | None]:
    observations = read_edit_pattern_observations(program_id, programs_root=programs_root)
    salience_events = read_salience_events(program_id, programs_root=programs_root)
    settings = _load_salience_settings(program_id, programs_root=programs_root)
    model = build_author_salience(
        program_id,
        observations=observations,
        salience_events=salience_events,
        author_alias=author_alias,
        min_weight=settings.min_weight if min_weight is None else min_weight,
        ema_alpha=settings.ema_alpha if ema_alpha is None else ema_alpha,
        confirmation_weight=settings.confirmation_weight,
        as_of=as_of,
    )
    if dry_run:
        return model, None
    path = write_author_salience(
        program_id,
        model,
        observations=observations,
        salience_events=salience_events,
        programs_root=programs_root,
    )
    return model, path


def build_author_salience(
    program_id: str,
    *,
    observations: tuple[EditPatternObservation, ...],
    salience_events: tuple[SalienceEvent, ...] = (),
    author_alias: str | None = None,
    min_weight: float = DEFAULT_MIN_WEIGHT,
    ema_alpha: float = DEFAULT_EMA_ALPHA,
    confirmation_weight: float = DEFAULT_CONFIRMATION_WEIGHT,
    as_of: datetime | None = None,
) -> AuthorSalienceModel:
    resolved_min_weight = _clamp(min_weight)
    resolved_alpha = _clamp(ema_alpha)
    resolved_confirmation_weight = max(0.0, confirmation_weight)
    grouped: dict[str, list[tuple[datetime, str, EditPatternObservation | SalienceEvent]]] = {}
    for observation in observations:
        grouped.setdefault(observation.workstream_id, []).append((observation.recorded_at, "observation", observation))
    for event in salience_events:
        grouped.setdefault(event.workstream_id, []).append((event.recorded_at, "event", event))

    workstreams: list[WorkstreamSalience] = []
    for workstream_id, grouped_entries in grouped.items():
        ordered = sorted(grouped_entries, key=lambda item: item[0])
        weight = resolved_min_weight
        total_override = 0.0
        observation_count = 0
        for _recorded_at, _entry_type, payload in ordered:
            if isinstance(payload, EditPatternObservation):
                target_weight = _clamp(max(resolved_min_weight, payload.author_override_magnitude))
                weight = round(weight + resolved_alpha * (target_weight - weight), 4)
                total_override += payload.author_override_magnitude
                observation_count += 1
                continue
            if isinstance(payload, SalienceEvent):
                weight = round(
                    _apply_salience_event(
                        current_weight=weight,
                        action=payload.action,
                        min_weight=resolved_min_weight,
                        ema_alpha=resolved_alpha,
                        confirmation_weight=resolved_confirmation_weight,
                    ),
                    4,
                )
        workstreams.append(
            WorkstreamSalience(
                workstream_id=workstream_id,
                attention_weight=round(weight, 4),
                sample_count=len(ordered),
                average_override_magnitude=round(total_override / observation_count, 4) if observation_count else 0.0,
                last_event_at=ordered[-1][0],
            )
        )

    workstreams.sort(key=lambda item: (-item.attention_weight, item.workstream_id))
    return AuthorSalienceModel(
        program_id=program_id,
        schema_version=SCHEMA_VERSION,
        updated_at=_ensure_utc(as_of or _utc_now()),
        author_alias=(author_alias or detect_current_author_alias() or "unknown"),
        ema_alpha=resolved_alpha,
        min_weight=resolved_min_weight,
        workstreams=tuple(workstreams),
    )


def load_author_salience(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> AuthorSalienceModel | None:
    payload = load_advisory_yaml(get_author_salience_path(program_id, programs_root=programs_root))
    if payload is None:
        return None
    try:
        return AuthorSalienceModel(
            program_id=program_id,
            schema_version=str(payload.get("schema_version") or SCHEMA_VERSION),
            updated_at=_parse_datetime(payload.get("updated_at"), field_name="updated_at"),
            author_alias=str(payload.get("author_alias") or "unknown"),
            ema_alpha=_coerce_float(payload.get("ema_alpha"), field_name="ema_alpha", default=DEFAULT_EMA_ALPHA),
            min_weight=_coerce_float(payload.get("min_weight"), field_name="min_weight", default=DEFAULT_MIN_WEIGHT),
            workstreams=_load_workstream_salience(payload.get("workstreams")),
        )
    except ValueError as error:
        raise SalienceModelError(f"Invalid author salience model for {program_id}.") from error


def write_author_salience(
    program_id: str,
    model: AuthorSalienceModel,
    *,
    observations: tuple[EditPatternObservation, ...],
    salience_events: tuple[SalienceEvent, ...],
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    path = get_author_salience_path(program_id, programs_root=programs_root)
    payload = {
        "schema_version": model.schema_version,
        "updated_at": model.updated_at.isoformat(),
        "author_alias": model.author_alias,
        "ema_alpha": model.ema_alpha,
        "min_weight": model.min_weight,
        "workstreams": {
            workstream.workstream_id: {
                "attention_weight": workstream.attention_weight,
                "sample_count": workstream.sample_count,
                "average_override_magnitude": workstream.average_override_magnitude,
                "last_event_at": workstream.last_event_at.isoformat(),
            }
            for workstream in model.workstreams
        },
        "dimensions": {},
    }
    evidence_hash = hashlib.sha256(
        json.dumps(
            [
                {
                    "workstream_id": observation.workstream_id,
                    "recorded_at": observation.recorded_at.isoformat(),
                    "author_override_magnitude": observation.author_override_magnitude,
                }
                for observation in observations
            ]
            + [
                {
                    "event_id": event.event_id,
                    "recorded_at": event.recorded_at.isoformat(),
                    "anomaly_id": event.anomaly_id,
                    "workstream_id": event.workstream_id,
                    "action": event.action,
                    "work_item_id": event.work_item_id,
                    "decision_latency_ms": event.decision_latency_ms,
                    "weight_before": event.weight_before,
                    "weight_after": event.weight_after,
                    "confirmed_within_30d": event.confirmed_within_30d,
                }
                for event in salience_events
            ],
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return write_advisory_yaml(
        path,
        payload,
        module_name="salience_modeler",
        evidence_hash=evidence_hash,
        generation_run_id=str(uuid4()),
        timestamp=model.updated_at,
    )


def read_edit_pattern_observations(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[EditPatternObservation, ...]:
    path = programs_root / program_id / "journal" / "edit_patterns.jsonl"
    if not path.exists():
        return ()

    observations: list[EditPatternObservation] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw_line = line.strip()
            if not raw_line:
                continue
            try:
                payload = parse_jsonl_line(raw_line)
            except json.JSONDecodeError as error:
                raise SalienceModelError(f"Edit pattern journal at {path} contains invalid JSON.") from error
            if not isinstance(payload, dict):
                raise SalienceModelError(f"Edit pattern journal at {path} must contain JSON objects.")
            if payload.get("task_type") != "workstream_blurb":
                continue
            workstream_id = str(payload.get("section_id") or "").strip()
            if not workstream_id or workstream_id == "exec_summary":
                continue
            magnitude = payload.get("author_override_magnitude")
            if magnitude is None:
                continue
            observations.append(
                EditPatternObservation(
                    workstream_id=workstream_id,
                    recorded_at=_parse_datetime(payload.get("recorded_at"), field_name="recorded_at"),
                    author_override_magnitude=_coerce_float(
                        magnitude,
                        field_name="author_override_magnitude",
                        default=0.0,
                    ),
                )
            )
    return tuple(observations)


def read_salience_events(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[SalienceEvent, ...]:
    path = get_salience_events_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()

    events: list[SalienceEvent] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw_line = line.strip()
            if not raw_line:
                continue
            try:
                payload = parse_jsonl_line(raw_line)
            except json.JSONDecodeError as error:
                raise SalienceModelError(f"Salience events at {path} contain invalid JSON.") from error
            if not isinstance(payload, dict):
                raise SalienceModelError(f"Salience events at {path} must contain JSON objects.")
            workstream_id = str(payload.get("workstream_id") or "").strip()
            anomaly_id = str(payload.get("anomaly_id") or "").strip()
            action = str(payload.get("action") or "").strip().lower()
            if not workstream_id or not anomaly_id or not action:
                continue
            work_item_id = payload.get("work_item_id")
            decision_latency_ms = payload.get("decision_latency_ms")
            raw_weight_before = payload.get("weight_before")
            raw_weight_after = payload.get("weight_after")
            events.append(
                SalienceEvent(
                    event_id=str(payload.get("event_id") or uuid4()),
                    recorded_at=_parse_datetime(payload.get("recorded_at"), field_name="recorded_at"),
                    anomaly_id=anomaly_id,
                    workstream_id=workstream_id,
                    action=action,
                    work_item_id=int(work_item_id) if isinstance(work_item_id, (int, float)) and not isinstance(work_item_id, bool) else None,
                    decision_latency_ms=int(decision_latency_ms) if isinstance(decision_latency_ms, (int, float)) and not isinstance(decision_latency_ms, bool) else None,
                    weight_before=float(raw_weight_before) if isinstance(raw_weight_before, (int, float)) and not isinstance(raw_weight_before, bool) else None,
                    weight_after=float(raw_weight_after) if isinstance(raw_weight_after, (int, float)) and not isinstance(raw_weight_after, bool) else None,
                    confirmed_within_30d=payload.get("confirmed_within_30d") if isinstance(payload.get("confirmed_within_30d"), bool) else None,
                )
            )
    return tuple(sorted(events, key=lambda item: item.recorded_at))


def append_salience_event(program_id: str, event: SalienceEvent, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    path = get_salience_events_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event_id": event.event_id,
        "recorded_at": _ensure_utc(event.recorded_at).isoformat(),
        "anomaly_id": event.anomaly_id,
        "work_item_id": event.work_item_id,
        "workstream_id": event.workstream_id,
        "action": event.action,
        "decision_latency_ms": event.decision_latency_ms,
        "weight_before": event.weight_before,
        "weight_after": event.weight_after,
        "confirmed_within_30d": event.confirmed_within_30d,
    }
    with portalocker.Lock(path, mode="a", timeout=5, encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")
        handle.flush()
    return path


def predict_salience_event_weights(
    program_id: str,
    *,
    workstream_id: str,
    action: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[float, float]:
    settings = _load_salience_settings(program_id, programs_root=programs_root)
    model = build_author_salience(
        program_id,
        observations=read_edit_pattern_observations(program_id, programs_root=programs_root),
        salience_events=read_salience_events(program_id, programs_root=programs_root),
        min_weight=settings.min_weight,
        ema_alpha=settings.ema_alpha,
        confirmation_weight=settings.confirmation_weight,
    )
    current_weight = settings.min_weight
    for workstream in model.workstreams:
        if workstream.workstream_id == workstream_id:
            current_weight = workstream.attention_weight
            break
    next_weight = _apply_salience_event(
        current_weight=current_weight,
        action=action,
        min_weight=settings.min_weight,
        ema_alpha=settings.ema_alpha,
        confirmation_weight=settings.confirmation_weight,
    )
    return round(current_weight, 4), round(next_weight, 4)


def render_author_salience(model: AuthorSalienceModel) -> str:
    lines = [
        f"Author Salience - {model.program_id}",
        f"Updated: {model.updated_at.isoformat()}",
        f"Author: {model.author_alias}",
        f"EMA alpha: {model.ema_alpha:.2f} | Min weight: {model.min_weight:.2f}",
    ]
    if not model.workstreams:
        lines.append("No workstream salience observations yet.")
        return "\n".join(lines)
    lines.append("Workstreams:")
    for workstream in model.workstreams:
        lines.append(
            f"- {workstream.workstream_id}: weight={workstream.attention_weight:.4f} | samples={workstream.sample_count} | avg_override={workstream.average_override_magnitude:.4f}"
        )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _SalienceSettings:
    min_weight: float = DEFAULT_MIN_WEIGHT
    ema_alpha: float = DEFAULT_EMA_ALPHA
    confirmation_weight: float = DEFAULT_CONFIRMATION_WEIGHT


def _load_salience_settings(program_id: str, *, programs_root: Path) -> _SalienceSettings:
    path = programs_root / program_id / "program.yaml"
    if not path.exists():
        return _SalienceSettings()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return _SalienceSettings()
    if not isinstance(document, dict):
        return _SalienceSettings()
    salience = document.get("salience")
    if not isinstance(salience, dict):
        return _SalienceSettings()
    return _SalienceSettings(
        min_weight=_coerce_salience_setting(salience.get("min_weight"), default=DEFAULT_MIN_WEIGHT),
        ema_alpha=_coerce_salience_setting(salience.get("ema_alpha"), default=DEFAULT_EMA_ALPHA),
        confirmation_weight=_coerce_salience_setting(salience.get("confirmation_weight"), default=DEFAULT_CONFIRMATION_WEIGHT),
    )


def _apply_salience_event(
    *,
    current_weight: float,
    action: str,
    min_weight: float,
    ema_alpha: float,
    confirmation_weight: float,
) -> float:
    normalized_action = action.strip().lower()
    if normalized_action == "dismissed":
        return _clamp(current_weight + ema_alpha * (min_weight - current_weight))
    if normalized_action in {"acted", "escalated"}:
        return _clamp(current_weight + ema_alpha * (1.0 - current_weight))
    if normalized_action == "confirmed_slip":
        return _clamp(current_weight + (ema_alpha * confirmation_weight) * (1.0 - current_weight))
    return _clamp(current_weight)


def _clamp(value: float) -> float:
    return round(min(max(float(value), 0.0), 1.0), 4)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def detect_current_author_alias() -> str | None:
    env_email = os.environ.get("GIT_AUTHOR_EMAIL") or os.environ.get("EMAIL")
    if env_email:
        return _alias_from_email(env_email)
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return _alias_from_email(result.stdout)


def _load_workstream_salience(value: Any) -> tuple[WorkstreamSalience, ...]:
    if not isinstance(value, dict):
        return ()
    loaded: list[WorkstreamSalience] = []
    for workstream_id, payload in value.items():
        if not isinstance(workstream_id, str) or not isinstance(payload, dict):
            continue
        loaded.append(
            WorkstreamSalience(
                workstream_id=workstream_id,
                attention_weight=_coerce_float(payload.get("attention_weight"), field_name="attention_weight", default=DEFAULT_MIN_WEIGHT),
                sample_count=int(payload.get("sample_count", 0)),
                average_override_magnitude=_coerce_float(
                    payload.get("average_override_magnitude"),
                    field_name="average_override_magnitude",
                    default=0.0,
                ),
                last_event_at=_parse_datetime(payload.get("last_event_at"), field_name="last_event_at"),
            )
        )
    loaded.sort(key=lambda item: (-item.attention_weight, item.workstream_id))
    return tuple(loaded)


def _parse_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO-8601 datetime string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime string.") from error
    return _ensure_utc(parsed)


def _coerce_float(value: Any, *, field_name: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric.")
    try:
        return round(float(value), 4)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be numeric.") from error


def _coerce_salience_setting(value: Any, *, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return default


def _alias_from_email(value: str) -> str | None:
    text = value.strip()
    if not text or "@" not in text:
        return None
    alias = text.split("@", 1)[0].strip()
    return alias or None
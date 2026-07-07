from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

from src.core.models import WorkItem
from src.core.models_v2 import CatchupEvent, Program, Signal, Workstream


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"

WatchLoader = Callable[[Program, tuple[Workstream, ...], datetime, datetime | None], tuple[tuple[WorkItem, ...], int]]
SignalTransform = Callable[[Signal], Signal]
SignalSummaryBuilder = Callable[[tuple[Signal, ...]], tuple[str, ...]]
CatchupEventBuilder = Callable[[tuple[Signal, ...]], tuple[CatchupEvent, ...]]


class WatchSource(str, Enum):
    ADO = "ado"
    WORKIQ = "workiq"
    KUSTO = "kusto"
    ANALYTICS = "analytics"
    SPRINTS = "sprints"
    ICM = "icm"


class WatchCadence(str, Enum):
    INTRADAY = "intraday"
    DAILY = "daily"
@dataclass(frozen=True, slots=True)
class WatchPollResult:
    program_id: str
    since: datetime
    polled_at: datetime
    scanned_items: int
    discovered_signals: int
    new_signals: int
    auto_reviews_written: int
    trajectory_updates: int
    ado_calls: int
    new_signal_summaries: tuple[str, ...] = ()
    total_changed_items: int | None = None
    catchup_events: tuple[CatchupEvent, ...] = ()
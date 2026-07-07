# Adapted from Shiproom src/observability/logger.py
from __future__ import annotations

import json
import logging
import sys
from collections.abc import MutableMapping
from datetime import datetime, timezone
from io import TextIOBase
from pathlib import Path
from typing import Any

from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT


_RESERVED_LOG_RECORD_KEYS = frozenset(logging.makeLogRecord({}).__dict__.keys())


class RunLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: MutableMapping[str, Any]) -> tuple[str, MutableMapping[str, Any]]:
        extra = dict(self.extra or {})
        call_extra = kwargs.get("extra")
        if isinstance(call_extra, MutableMapping):
            extra.update(call_extra)
        kwargs["extra"] = extra
        return msg, kwargs


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
            "logger": record.name,
            "run_id": getattr(record, "run_id", ""),
        }
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _RESERVED_LOG_RECORD_KEYS or key in {"message", "asctime"}:
                continue
            entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        run_id = getattr(record, "run_id", "")
        stage = getattr(record, "stage", record.name.split(".")[-1])
        extras: list[str] = []
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _RESERVED_LOG_RECORD_KEYS or key in {"message", "asctime", "stage", "run_id"}:
                continue
            extras.append(f"{key}={value}")
        suffix = f" [{' '.join(extras)}]" if extras else ""
        return f"[{timestamp}] [run_id={run_id}] {stage}: {record.getMessage()}{suffix}"


def configure_logging(
    run_id: str,
    *,
    level: str = "INFO",
    json_output: bool = False,
    stream: TextIOBase | None = None,
    logger_name: str = "vertex",
) -> RunLoggerAdapter:
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(StructuredFormatter() if json_output else HumanFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return RunLoggerAdapter(logger, {"run_id": run_id[:8]})


def configure_file_logging(
    run_id: str,
    *,
    trace_path: Path,
    level: str = "INFO",
    stream: TextIOBase | None = None,
    human_output: bool = False,
    logger_name: str = "vertex",
) -> RunLoggerAdapter:
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    if human_output:
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(HumanFormatter())
        logger.addHandler(handler)
    file_handler = logging.FileHandler(trace_path, encoding="utf-8")
    file_handler.setFormatter(StructuredFormatter())
    logger.addHandler(file_handler)
    logger.propagate = False
    return RunLoggerAdapter(logger, {"run_id": run_id[:8]})


def get_command_trace_path(scope_id: str, command_name: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_program_output_dir(scope_id, programs_root=programs_root) / "observability" / f"{command_name}.trace.jsonl"

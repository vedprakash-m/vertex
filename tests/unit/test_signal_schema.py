from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from src.core.journal import _signal_to_record
from src.core.models import Confidence
from src.core.models_v2 import Signal


def test_signal_schema_accepts_journal_signal_record(repo_root: Path) -> None:
    validator = _validator(repo_root)
    record = _signal_to_record(
        Signal(
            id="a1b2c3",
            timestamp=datetime(2026, 5, 8, 14, 30, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:12345", "P:priya_raghavan"),
            text="Priya: UD chunking fix delayed to June 15",
            raw_ref="msg:AAMkAD",
            confidence=Confidence.MEDIUM,
            metadata={"source_type": "email", "message_id": "AAMkAD", "sender_alias": "priya"},
            thread_id=None,
        )
    )

    validator.validate(record)


def test_signal_schema_rejects_text_longer_than_500_chars(repo_root: Path) -> None:
    validator = _validator(repo_root)
    record = _signal_to_record(
        Signal(
            id="d4e5f6",
            timestamp=datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:36830830",),
            text="x" * 501,
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata={"field": "TargetDate", "prior": "2026-06-01", "current": "2026-07-10"},
            thread_id=None,
        )
    )

    errors = list(validator.iter_errors(record))

    assert errors
    assert any("is too long" in error.message for error in errors)


def _validator(repo_root: Path) -> Draft202012Validator:
    schema = json.loads((repo_root / "reports" / "schemas" / "signal.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema)
from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ai.synthesizer import WorkstreamSynthesizer
from src.core.models import Confidence
from src.core.models_v2 import Program, Signal, Workstream


SNAPSHOT_PATH = Path(__file__).resolve().parent / "synthesize_networking.json"


class SnapshotMismatchError(AssertionError):
    pass


class _FakeAIClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
        del system, user, max_tokens, prompt_version
        return parser(json.loads(self.response_text))


def test_workstream_synthesis_snapshot(update_golden: bool, tmp_path: Path) -> None:
    result = WorkstreamSynthesizer(
        client=_FakeAIClient(
            json.dumps(
                {
                    "overall_assessment": "Networking remains the gating lane until servicing validation closes.",
                    "proposed_risk": "high",
                    "confidence": "medium",
                    "key_findings": [
                        "Target date slipped twice within one week.",
                        "No confirming recovery signal is present.",
                    ],
                    "evidence_refs": ["sig-1", "sig-2"],
                    "open_questions": ["Who owns the servicing validation exit criteria?"],
                    "recommended_actions": ["Lock the servicing validation owner and date."],
                }
            )
        )
    ).generate(
        program=Program(schema_version="2.0", id="acme", name="Acme"),
        workstream=Workstream(id="networking", name="Networking", description="Networking lane"),
        signals=(_signal("sig-1"), _signal("sig-2")),
        drift_patterns=(),
        programs_root=tmp_path,
    )

    assert result is not None
    actual = json.dumps(
        {
            "confidence": result.synthesis.confidence.value,
            "evidence_refs": list(result.synthesis.evidence_refs),
            "key_findings": list(result.synthesis.key_findings),
            "open_questions": list(result.synthesis.open_questions),
            "overall_assessment": result.synthesis.overall_assessment,
            "prompt_version": result.prompt_version,
            "proposed_risk": result.synthesis.proposed_risk.value,
            "recommended_actions": list(result.synthesis.recommended_actions),
            "workstream_id": result.synthesis.workstream_id,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
    _compare_with_snapshot(actual, update_golden)


def _compare_with_snapshot(actual: str, update: bool) -> None:
    if update or not SNAPSHOT_PATH.exists():
        SNAPSHOT_PATH.write_text(actual, encoding="utf-8")
        if not update:
            pytest.skip(f"Created new golden file: {SNAPSHOT_PATH.name}")
        return

    expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
    if not expected.endswith("\n"):
        expected += "\n"
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=SNAPSHOT_PATH.name,
                tofile="actual",
            )
        )
        raise SnapshotMismatchError(f"Output does not match snapshot {SNAPSHOT_PATH.name}\n\nDiff:\n{diff}")


def _signal(signal_id: str) -> Signal:
    return Signal(
        id=signal_id,
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="manual",
        program_id="acme",
        workstream_id="networking",
        entity_refs=("WI:1234",),
        text="Servicing validation moved to 2026-05-17.",
        raw_ref=None,
        confidence=Confidence.HIGH,
    )
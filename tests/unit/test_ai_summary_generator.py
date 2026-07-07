from __future__ import annotations

from src.ai.ai_mode import AIMode, set_ai_mode
from src.ai.summary_generator import SummaryGenerator
from src.core.models_v2 import Program, Workstream


def test_summary_generator_from_program_returns_none_when_ai_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.summary_generator.FallbackStructuredClient",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("FallbackStructuredClient should not be constructed")),
    )
    set_ai_mode(AIMode.DISABLED)
    try:
        generator = SummaryGenerator.from_program(Program(schema_version="2.0", id="acme", name="Acme"))
        draft = generator.generate(
            program=Program(schema_version="2.0", id="acme", name="Acme"),
            workstream=Workstream(id="networking", name="Networking"),
            prior_summary="Previously generated summary.",
            signals=(),
            drift_patterns=(),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert draft is None

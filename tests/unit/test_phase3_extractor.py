"""Tests for Phase 3: ContentExtractionAgent (BL-20) and local_kb_reader (BL-25)."""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.ai.content_extractor import ContentExtractionAgent, ExtractionContext, _extract_json
from src.core.models import Enrichment, RiskLevel


_AS_OF = datetime(2026, 6, 17, tzinfo=timezone.utc)


def _make_enrichment(body_text: str | None = None, source: str = "mail") -> Enrichment:
    return Enrichment(
        source=source,
        source_id="src-001",
        author="test@example.com",
        timestamp=_AS_OF,
        excerpt="Subject: Test",
        permalink=None,
        body_text=body_text,
    )


def _make_ctx(body_text: str | None = "Performance is HIGH; ADO:34705323 blocking.") -> ExtractionContext:
    return ExtractionContext(
        lane_id="test.lane",
        lane_why="Track performance risk",
        lane_what="ETA slippage, blocking ADOs, owner commitments",
        lane_name="Performance",
        enrichments=(_make_enrichment(body_text),),
    )


# ── _extract_json ─────────────────────────────────────────────────────────────

def test_extract_json_plain_json():
    raw = '{"risk_level": "high", "confidence": 0.8}'
    result = _extract_json(raw)
    assert result["risk_level"] == "high"


def test_extract_json_strips_markdown_fence():
    raw = '```json\n{"risk_level": "medium"}\n```'
    result = _extract_json(raw)
    assert result["risk_level"] == "medium"


def test_extract_json_strips_code_fence_no_lang():
    raw = '```\n{"risk_level": "low"}\n```'
    result = _extract_json(raw)
    assert result["risk_level"] == "low"


def test_extract_json_finds_embedded_json():
    raw = 'Here is the result:\n{"risk_level": "blocked"}\nEnd.'
    result = _extract_json(raw)
    assert result["risk_level"] == "blocked"


# ── ContentExtractionAgent.extract ───────────────────────────────────────────

def test_extract_returns_none_when_no_body_text():
    agent = ContentExtractionAgent(ask_ai_fn=lambda p: '{"risk_level": "high"}')
    ctx = _make_ctx(body_text=None)
    assert agent.extract(ctx) is None


def test_extract_returns_none_when_ai_returns_none():
    agent = ContentExtractionAgent(ask_ai_fn=lambda p: None)
    ctx = _make_ctx()
    assert agent.extract(ctx) is None


def test_extract_returns_none_on_invalid_json():
    agent = ContentExtractionAgent(ask_ai_fn=lambda p: "not json at all")
    ctx = _make_ctx()
    assert agent.extract(ctx) is None


def test_extract_parses_risk_level():
    response = json.dumps({"risk_level": "high", "etas": [], "blocking_items": [],
                           "owners": [], "raw_excerpts": [], "confidence": 0.8})
    agent = ContentExtractionAgent(ask_ai_fn=lambda p: response)
    ev = agent.extract(_make_ctx())
    assert ev is not None
    assert ev.risk_level == RiskLevel.HIGH
    assert ev.confidence == 0.8


def test_extract_parses_etas():
    response = json.dumps({
        "risk_level": "high",
        "etas": [{"label": "Perf Signoff", "eta_date": "2026-06-12",
                  "owner": "Sukumar", "status": "missed", "ado_id": "34705323"}],
        "blocking_items": [],
        "owners": ["Sukumar"],
        "raw_excerpts": [],
        "confidence": 0.9,
    })
    agent = ContentExtractionAgent(ask_ai_fn=lambda p: response)
    ev = agent.extract(_make_ctx())
    assert ev is not None
    assert len(ev.etas) == 1
    assert ev.etas[0].label == "Perf Signoff"
    assert ev.etas[0].eta_date == date(2026, 6, 12)
    assert ev.etas[0].owner == "Sukumar"
    assert ev.etas[0].status == "missed"
    assert ev.etas[0].ado_id == "34705323"


def test_extract_parses_blocking_items():
    response = json.dumps({
        "risk_level": "blocked",
        "etas": [],
        "blocking_items": ["ADO:37777539", "IcM:788471726", "PR:4312", "PIPELINE:88123"],
        "owners": [],
        "raw_excerpts": [],
        "confidence": 0.85,
    })
    agent = ContentExtractionAgent(ask_ai_fn=lambda p: response)
    ev = agent.extract(_make_ctx())
    assert ev is not None
    assert "ADO:37777539" in ev.blocking_items
    assert "IcM:788471726" in ev.blocking_items
    assert "PR:4312" in ev.blocking_items
    assert "PIPELINE:88123" in ev.blocking_items


def test_extract_rejects_invalid_blocking_items():
    response = json.dumps({
        "risk_level": "high",
        "etas": [],
        "blocking_items": ["invalid-item", "ADO:12345678"],
        "owners": [],
        "raw_excerpts": [],
        "confidence": 0.7,
    })
    agent = ContentExtractionAgent(ask_ai_fn=lambda p: response)
    ev = agent.extract(_make_ctx())
    assert ev is not None
    assert "invalid-item" not in ev.blocking_items
    assert "ADO:12345678" in ev.blocking_items


def test_extract_confidence_clamped_to_0_1():
    response = json.dumps({"risk_level": "low", "etas": [], "blocking_items": [],
                           "owners": [], "raw_excerpts": [], "confidence": 1.5})
    agent = ContentExtractionAgent(ask_ai_fn=lambda p: response)
    ev = agent.extract(_make_ctx())
    assert ev is not None
    assert ev.confidence == 1.0


def test_extract_eta_with_invalid_date_skipped():
    response = json.dumps({
        "risk_level": "high",
        "etas": [{"label": "Bad ETA", "eta_date": "not-a-date", "owner": None,
                  "status": "open", "ado_id": None}],
        "blocking_items": [],
        "owners": [],
        "raw_excerpts": [],
        "confidence": 0.6,
    })
    agent = ContentExtractionAgent(ask_ai_fn=lambda p: response)
    ev = agent.extract(_make_ctx())
    assert ev is not None
    assert len(ev.etas) == 0


def test_build_prompt_contains_lane_context():
    prompt_captured = []
    def capture(p):
        prompt_captured.append(p)
        return json.dumps({"risk_level": "unknown", "etas": [], "blocking_items": [],
                           "owners": [], "raw_excerpts": [], "confidence": 0.5})
    ctx = _make_ctx()
    ContentExtractionAgent(ask_ai_fn=capture).extract(ctx)
    assert "Track performance risk" in prompt_captured[0]
    assert "Performance" in prompt_captured[0]


# ── BL-25: local_kb_reader ────────────────────────────────────────────────────

def test_local_kb_reads_markdown_file():
    from src.m365.local_kb_reader import read_local_kb_enrichments
    with tempfile.TemporaryDirectory() as tmp:
        kb_file = Path(tmp) / "test.md"
        kb_file.write_text("# Test\nTao Peng owns 7 RAMPP1 items.", encoding="utf-8")
        result = read_local_kb_enrichments(kb_paths=[tmp], stale_threshold_days=365)
    assert len(result) == 1
    assert result[0].source == "local_kb"
    assert "Tao Peng" in result[0].body_text


def test_local_kb_skips_unsupported_extensions():
    from src.m365.local_kb_reader import read_local_kb_enrichments
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "test.pdf").write_bytes(b"PDF content")
        Path(tmp, "test.md").write_text("# Valid", encoding="utf-8")
        result = read_local_kb_enrichments(kb_paths=[tmp], stale_threshold_days=365)
    assert len(result) == 1
    assert result[0].excerpt == "test.md"


def test_local_kb_skips_missing_path():
    from src.m365.local_kb_reader import read_local_kb_enrichments
    result = read_local_kb_enrichments(kb_paths=["/nonexistent/path/xyz"])
    assert result == ()


def test_local_kb_truncates_body():
    from src.m365.local_kb_reader import read_local_kb_enrichments, _KB_MAX_BODY_CHARS
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "big.md").write_text("x" * (_KB_MAX_BODY_CHARS + 500), encoding="utf-8")
        result = read_local_kb_enrichments(kb_paths=[tmp], stale_threshold_days=365)
    assert result[0].body_text is not None
    assert len(result[0].body_text) == _KB_MAX_BODY_CHARS


def test_local_kb_reads_yaml_file():
    from src.m365.local_kb_reader import read_local_kb_enrichments
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "config.yaml").write_text("key: value\nnotes: test", encoding="utf-8")
        result = read_local_kb_enrichments(kb_paths=[tmp], stale_threshold_days=365)
    assert len(result) == 1
    assert result[0].source == "local_kb"

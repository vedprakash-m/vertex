"""WS-24 contract tests: threat model + model cards.

These tests enforce that the governance docs are present, non-empty,
and structurally aligned with the code. The threat-model test catches
truncation (5+ threats, every threat has a mitigation). The model-cards
test catches template drift (a feature inventory row exists, re-cert
workflow is described).
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
THREAT_MODEL = REPO_ROOT / "governance" / "threat-model.md"
MODEL_CARDS = REPO_ROOT / "governance" / "model-cards.md"


# ---------------------------------------------------------------------------
# Threat model
# ---------------------------------------------------------------------------


def test_threat_model_exists() -> None:
    assert THREAT_MODEL.exists(), f"{THREAT_MODEL} not found"


def test_threat_model_has_at_least_five_threats() -> None:
    text = THREAT_MODEL.read_text(encoding="utf-8")
    # Count "### T-N —" threat headers (T-1 through T-N).
    threat_count = sum(1 for line in text.splitlines() if line.startswith("### T-") and "—" in line)
    assert threat_count >= 5, f"threat-model.md lists {threat_count} threats (need >= 5)"


def test_threat_model_every_threat_has_mitigations() -> None:
    text = THREAT_MODEL.read_text(encoding="utf-8")
    # Each threat section must contain a "Mitigations in place" subhead.
    sections = text.split("### T-")
    # The first split is the preamble; the rest are the threat sections.
    for section in sections[1:]:
        section_head, _, section_body = section.partition("\n")
        threat_id = section_head.split(" —", 1)[0].strip()
        assert "Mitigations in place" in section_body, (
            f"threat {threat_id!r} missing 'Mitigations in place' subhead"
        )
        assert "Residual risk" in section_body, (
            f"threat {threat_id!r} missing 'Residual risk' subhead"
        )


def test_threat_model_has_owner_and_status() -> None:
    text = THREAT_MODEL.read_text(encoding="utf-8")
    sections = text.split("### T-")
    for section in sections[1:]:
        section_head, _, _ = section.partition("\n")
        threat_id = section_head.split(" —", 1)[0].strip()
        assert "**Owner**" in section, f"threat {threat_id!r} missing **Owner**"
        assert "**Status**" in section, f"threat {threat_id!r} missing **Status**"


def test_threat_model_has_kill_chain_section() -> None:
    text = THREAT_MODEL.read_text(encoding="utf-8")
    assert "## 4. Kill chain" in text
    assert "T-1" in text  # the worked example references T-1


def test_threat_model_has_mitigations_summary_table() -> None:
    text = THREAT_MODEL.read_text(encoding="utf-8")
    assert "## 5. Mitigations summary" in text
    # The summary table must include all 7 threats.
    for tid in ("T-1", "T-2", "T-3", "T-4", "T-5", "T-6", "T-7"):
        assert tid in text, f"mitigations summary missing {tid}"


# ---------------------------------------------------------------------------
# Model cards
# ---------------------------------------------------------------------------


def test_model_cards_exists() -> None:
    assert MODEL_CARDS.exists(), f"{MODEL_CARDS} not found"


def test_model_cards_has_template() -> None:
    text = MODEL_CARDS.read_text(encoding="utf-8")
    # Template is a code block containing feature_name, deployment,
    # recert_at, deprecation_review_at.
    assert "```yaml" in text
    assert "feature_name:" in text
    assert "deployment_id:" in text
    assert "recert_at:" in text
    assert "deprecation_review_at:" in text


def test_model_cards_has_inventory_table() -> None:
    text = MODEL_CARDS.read_text(encoding="utf-8")
    assert "## 3. Model inventory" in text
    # The inventory must list the major features.
    for feat in ("blurb_generator", "claim_extractor", "summary_generator", "exec_summary_drafter"):
        assert feat in text, f"inventory missing {feat!r}"


def test_model_cards_has_recert_workflow() -> None:
    text = MODEL_CARDS.read_text(encoding="utf-8")
    assert "## 4. Re-certification workflow" in text
    # The workflow must reference the WS-24 hook points.
    assert "record_model_deployment_used" in text
    assert "ModelBumpDetectedError" in text
    assert "FallbackAIClient" in text


def test_model_cards_known_limitations_section() -> None:
    text = MODEL_CARDS.read_text(encoding="utf-8")
    # Operators must be told what the registry does NOT cover.
    assert "## 5. Known limitations" in text
    assert "per-program" in text  # first limitation

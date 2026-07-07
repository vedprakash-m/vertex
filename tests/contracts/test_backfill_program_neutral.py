"""GAP-3/WP-7: Contract tests verifying that backfill newsletter category
filtering is program-neutral (driven by BackfillPlan, not hardcoded names).
"""
from __future__ import annotations

import textwrap
import tempfile
from pathlib import Path

import pytest

from src.core.backfill_loader import BackfillPlan, BackfillExtractSettings, load_backfill_plan


def _write_backfill_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "backfill.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. BackfillPlan derives newsletter_source_categories from sources by default
# ---------------------------------------------------------------------------

def test_backfill_plan_derives_newsletter_categories_from_sources(tmp_path: Path) -> None:
    """When newsletter_source_categories is absent, BackfillPlan infers it
    from sources by excluding known non-newsletter kinds."""
    path = _write_backfill_yaml(tmp_path, """\
        sources:
          - kind: lt_decks
            glob: "docs/*.pptx"
          - kind: my_newsletters
            glob: "docs/newsletters/*.eml"
          - kind: transcripts
            glob: "backfill/transcripts/*.vtt"
        extract:
          workstream_blurbs: true
          scorecard_dimensions: false
    """)
    plan = load_backfill_plan(path)
    # lt_decks and transcripts are non-newsletter; my_newsletters should be inferred
    assert "my_newsletters" in plan.newsletter_source_categories
    assert "lt_decks" not in plan.newsletter_source_categories
    assert "transcripts" not in plan.newsletter_source_categories


def test_backfill_plan_respects_explicit_newsletter_source_categories(tmp_path: Path) -> None:
    """When newsletter_source_categories is explicit, only listed kinds are used."""
    path = _write_backfill_yaml(tmp_path, """\
        sources:
          - kind: newsletter_a
            glob: "docs/a/*.eml"
          - kind: newsletter_b
            glob: "docs/b/*.eml"
          - kind: newsletters_misc
            glob: "docs/misc/*.eml"
        extract:
          workstream_blurbs: true
          scorecard_dimensions: false
        newsletter_source_categories:
          - newsletter_a
    """)
    plan = load_backfill_plan(path)
    assert plan.newsletter_source_categories == ("newsletter_a",)
    assert "newsletter_b" not in plan.newsletter_source_categories


def test_backfill_plan_empty_newsletter_source_categories_when_all_non_newsletter(tmp_path: Path) -> None:
    """A backfill.yaml with only non-newsletter sources yields an empty tuple."""
    path = _write_backfill_yaml(tmp_path, """\
        sources:
          - kind: lt_decks
            glob: "docs/*.pptx"
          - kind: transcripts
            glob: "backfill/transcripts/*.vtt"
        extract:
          workstream_blurbs: false
          scorecard_dimensions: false
    """)
    plan = load_backfill_plan(path)
    assert plan.newsletter_source_categories == ()


# ---------------------------------------------------------------------------
# 2. newsletter_source_categories flows into BackfillPlan.newsletter_source_categories
# ---------------------------------------------------------------------------

def test_backfill_plan_nova_compat_includes_nova_newsletter_kinds(tmp_path: Path) -> None:
    """Acme-style backfill.yaml (no explicit newsletter_source_categories) should
    infer acme_newsletters, contoso_newsletters, contoso_daily as newsletter kinds."""
    path = _write_backfill_yaml(tmp_path, """\
        sources:
          - kind: lt_decks
            glob: "docs/*.pptx"
          - kind: acme_newsletters
            glob: "docs/newsletters/Acme/*.eml"
          - kind: contoso_newsletters
            glob: "docs/newsletters/Contoso/*.eml"
          - kind: contoso_daily
            glob: "docs/newsletters/DDPF_daily/*.eml"
          - kind: transcripts
            glob: "backfill/transcripts/*.vtt"
        extract:
          workstream_blurbs: true
          scorecard_dimensions: true
    """)
    plan = load_backfill_plan(path)
    cats = set(plan.newsletter_source_categories)
    assert "acme_newsletters" in cats
    assert "contoso_newsletters" in cats
    assert "contoso_daily" in cats
    assert "lt_decks" not in cats
    assert "transcripts" not in cats

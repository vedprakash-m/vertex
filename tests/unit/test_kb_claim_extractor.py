from __future__ import annotations

from datetime import datetime, timezone

from src.ai.discovery.kb_claim_extractor import extract_claim_candidates_from_markdown


def test_extract_claim_candidates_from_markdown_parses_explicit_claim_lines() -> None:
    batch = extract_claim_candidates_from_markdown(
        markdown_text="""
Intro
Claim: subject=sku_generation:gen9; predicate=first_deployment; value=2025-H2; valid_from=2025-07-01
Claim: subject=sku_generation:gen9; predicate=launch_blocker; value=Firmware signoff; section=fw
""",
        scope="domain:storage-platform",
        vault_hash="sha256:abc123",
        original_filename="dd-acme-kb.md",
        origin_path="C:/kb/dd-acme-kb.md",
        ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert len(batch.candidates) == 2
    assert batch.candidates[0].proposed_claim.predicate == "first_deployment"
    assert batch.candidates[0].entity_resolution[0].resolved_entity_id == "sku_generation:gen9"
    assert batch.candidates[1].source_ref.section == "fw"


def test_extract_claim_candidates_from_markdown_reuses_supplied_batch_id() -> None:
    batch = extract_claim_candidates_from_markdown(
        markdown_text="Claim: subject=sku_generation:gen9; predicate=first_deployment; value=2025-H2\n",
        scope="domain:storage-platform",
        vault_hash="sha256:abc123",
        original_filename="dd-acme-kb.md",
        origin_path="C:/kb/dd-acme-kb.md",
        ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        batch_id="batch-shared",
    )

    assert batch.batch_id == "batch-shared"
    assert batch.candidates[0].batch_id == "batch-shared"


def test_extract_claim_candidates_from_markdown_honors_explicit_confidence() -> None:
    batch = extract_claim_candidates_from_markdown(
        markdown_text=(
            "Claim: subject=program:acme; predicate=service_tier; "
            "value=umbrella-platform-migration; confidence=operator_confirmed\n"
        ),
        scope="program:acme",
        vault_hash="sha256:abc123",
        original_filename="nova_claim_markers.md",
        origin_path="Q:/markers/nova_claim_markers.md",
        ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert batch.candidates[0].proposed_confidence == "operator_confirmed"

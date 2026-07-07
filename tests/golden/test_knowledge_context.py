from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.knowledge_claim_store import KnowledgeClaimRevision, resolve_knowledge_context
from src.core.ledger.event_log import ConfidenceTier
from src.core.ledger.source_refs import KnowledgeDocumentRef, LTDeckRef, OperatorAssertionRef


GOLDEN_DIR = Path(__file__).resolve().parent / "snapshots"


class GoldenFileMismatchError(AssertionError):
    pass


def _load_golden(name: str) -> str | None:
    golden_path = GOLDEN_DIR / f"{name}.golden"
    if golden_path.exists():
        return golden_path.read_text(encoding="utf-8")
    return None


def _save_golden(name: str, content: str) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    (GOLDEN_DIR / f"{name}.golden").write_text(content, encoding="utf-8")


def _compare_with_golden(name: str, actual: str, update: bool) -> None:
    golden = _load_golden(name)
    if update or golden is None:
        _save_golden(name, actual)
        if golden is None:
            pytest.skip(f"Created new golden file: {name}.golden")
        return

    if actual != golden:
        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{name}.golden",
                tofile="actual",
            )
        )
        raise GoldenFileMismatchError(
            f"Output does not match golden file: {name}.golden\n\nDiff:\n{diff}"
        )


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="docs/Monthly_LT_Review/gen9.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=14)


def _kb_ref(vault_hash: str, section: str) -> KnowledgeDocumentRef:
    return KnowledgeDocumentRef(
        vault_hash=vault_hash,
        original_filename="gen9-kb.md",
        origin_kind="local_path",
        origin_path="Q:/knowledge/gen9-kb.md",
        origin_url=None,
        ingested_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
        section=section,
    )


def _golden_revisions() -> tuple[KnowledgeClaimRevision, ...]:
    return (
        KnowledgeClaimRevision(
            claim_id="01ORGGEN8",
            scope="org",
            subject="sku_generation:gen8",
            predicate="status",
            value="retiring",
            valid_from=datetime(2023, 1, 1, tzinfo=timezone.utc),
            valid_until=None,
            recorded_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            source_ref=_kb_ref("sha256:kb-gen8", "line:10"),
            supersedes=None,
            natural_key="org/sku_generation:gen8/status",
        ),
        KnowledgeClaimRevision(
            claim_id="01DOMAINGEN9",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="status",
            value="pilot",
            valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
            valid_until=datetime(2025, 10, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 2, 1, tzinfo=timezone.utc),
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            source_ref=_kb_ref("sha256:kb-gen9-pilot", "line:20"),
            supersedes=None,
            natural_key="domain:storage-platform/sku_generation:gen9/status",
        ),
        KnowledgeClaimRevision(
            claim_id="01DOMAINGEN9OP",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="status",
            value="launch_ready",
            valid_from=datetime(2025, 10, 1, tzinfo=timezone.utc),
            valid_until=None,
            recorded_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 10, tzinfo=timezone.utc)),
            supersedes=None,
            natural_key="domain:storage-platform/sku_generation:gen9/status",
        ),
        KnowledgeClaimRevision(
            claim_id="01PROGRAMGEN9",
            scope="program:acme",
            subject="sku_generation:gen9",
            predicate="status",
            value="delayed",
            valid_from=datetime(2025, 10, 1, tzinfo=timezone.utc),
            valid_until=None,
            recorded_at=datetime(2025, 12, 15, tzinfo=timezone.utc),
            confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=_deck_ref(),
            supersedes=None,
            natural_key="program:acme/sku_generation:gen9/status",
        ),
        KnowledgeClaimRevision(
            claim_id="01PROGRAMBLOCKER",
            scope="program:acme",
            subject="sku_generation:gen9",
            predicate="launch_blocker",
            value=None,
            valid_from=datetime(2026, 1, 15, tzinfo=timezone.utc),
            valid_until=None,
            recorded_at=datetime(2026, 1, 16, tzinfo=timezone.utc),
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 16, tzinfo=timezone.utc)),
            supersedes=None,
            natural_key="program:acme/sku_generation:gen9/launch_blocker",
        ),
        KnowledgeClaimRevision(
            claim_id="01DOMAINBLOCKER",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="launch_blocker",
            value="wingtip",
            valid_from=datetime(2025, 9, 1, tzinfo=timezone.utc),
            valid_until=None,
            recorded_at=datetime(2025, 9, 20, tzinfo=timezone.utc),
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2025, 9, 20, tzinfo=timezone.utc)),
            supersedes=None,
            natural_key="domain:storage-platform/sku_generation:gen9/launch_blocker",
        ),
    )


def test_qg_dm_11_knowledge_context_golden(update_golden: bool) -> None:
    revisions = _golden_revisions()
    scope_chain = ("program:acme", "domain:storage-platform", "org")
    coverage = {
        "sku_generation:gen8": "absent",
        "sku_generation:gen9": "present",
    }
    checkpoints = (
        {
            "name": "2025_06_period_correct",
            "entity_ids": ("sku_generation:gen8", "sku_generation:gen9"),
            "as_of": datetime(2025, 6, 1, tzinfo=timezone.utc),
            "knowledge_as_of": datetime(2025, 6, 1, tzinfo=timezone.utc),
        },
        {
            "name": "2026_02_current_knowledge",
            "entity_ids": ("sku_generation:gen9",),
            "as_of": datetime(2026, 2, 1, tzinfo=timezone.utc),
            "knowledge_as_of": datetime(2026, 2, 1, tzinfo=timezone.utc),
        },
        {
            "name": "2026_02_prior_knowledge",
            "entity_ids": ("sku_generation:gen9",),
            "as_of": datetime(2026, 2, 1, tzinfo=timezone.utc),
            "knowledge_as_of": datetime(2025, 12, 31, tzinfo=timezone.utc),
        },
    )

    rendered = []
    for checkpoint in checkpoints:
        context = resolve_knowledge_context(
            checkpoint["entity_ids"],
            scope_chain=scope_chain,
            revisions=revisions,
            projection_coverage=coverage,
            as_of=checkpoint["as_of"],
            knowledge_as_of=checkpoint["knowledge_as_of"],
        )
        rendered.append(
            {
                "gate_id": "QG-DM-11",
                "checkpoint": checkpoint["name"],
                "context": context.to_dict(),
            }
        )

    actual = json.dumps(rendered, indent=2, sort_keys=True) + "\n"
    _compare_with_golden("knowledge_context_qg_dm_11", actual, update_golden)
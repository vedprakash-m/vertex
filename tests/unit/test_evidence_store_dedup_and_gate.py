"""P4-0 / P4-3 / P4-2 unit tests: evidence dedup, backing-signal hook, approval gate,
and enrich-path provenance/quality recording.

These are pure functions operating on a programs_root path — no program context,
no M365, no AI. They verify the §17.8 Option A enforcement chain:

  persist_evidence (dedup + backing-signal link)
    → evidence_store.jsonl row carries _dedup_key + _backing_signal_ids
    → load_approved_evidence_by_lane admits only approved-backed evidence
    → enrich path writes provenance + quality journals (P4-2)
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from src.commands.enrich import (
    _enrich_signal_id,
    _record_enrich_provenance_and_quality,
)
from src.commands.gather_pipeline.evidence_extraction_stage import (
    _evidence_dedup_key,
    persist_evidence,
)
from src.core.evidence_models import EtaRecord, SourceRef, WorkstreamEvidence
from src.core.evidence_provenance import record_provenance  # noqa: F401  (ensures import path)
from src.core.evidence_quality import load_evidence_quality
from src.core.evidence_store import (
    evidence_store_path,
    load_approved_evidence_by_lane,
    load_evidence_records,
    load_latest_evidence_by_lane,
)
from src.core.jsonl_utils import read_jsonl_records
from src.core.models import RiskLevel


def _build_evidence(
    *,
    lane_id: str = "lane-ap-bios",
    narrative: str = "BIOS AP rollout is on track for Gen9.",
    blocking: tuple[str, ...] = ("ADO:37777539",),
    confidence: float = 0.82,
    synthesized_at: datetime | None = None,
) -> WorkstreamEvidence:
    return WorkstreamEvidence(
        lane_id=lane_id,
        synthesized_at=synthesized_at or datetime(2026, 6, 18, tzinfo=timezone.utc),
        risk_level=RiskLevel.MEDIUM,
        etas=(
            EtaRecord(
                label="Gen9 firmware sign-off",
                eta_date=date(2026, 6, 25),
                owner="operator",
                status="open",
                ado_id="37777539",
            ),
        ),
        blocking_items=blocking,
        owners=("operator",),
        source_refs=(
            SourceRef(
                source_type="workiq_email",
                description="BIOS sync email",
                source_date=date(2026, 6, 17),
                author="operator@example.com",
                permalink="https://outlook/x",
            ),
        ),
        raw_excerpts=("Gen9 is on track",),
        confidence=confidence,
        narrative_summary=narrative,
        stale_after=datetime(2026, 6, 25, tzinfo=timezone.utc),
    )


def test_p4_3_persist_evidence_dedups_identical_content(tmp_path: Path) -> None:
    """P4-3: identical re-extraction returns False and does not double-write."""
    programs_root = tmp_path
    evidence = _build_evidence()

    wrote1 = persist_evidence(evidence, program_id="acme", programs_root=programs_root)
    wrote2 = persist_evidence(evidence, program_id="acme", programs_root=programs_root)

    assert wrote1 is True
    assert wrote2 is False   # identical content → deduped

    records = read_jsonl_records(evidence_store_path("acme", programs_root))
    assert len(records) == 1   # only one row despite two calls


def test_p4_3_different_content_does_not_dedup(tmp_path: Path) -> None:
    """P4-3: genuinely different extraction (different narrative) writes a new row."""
    programs_root = tmp_path
    e1 = _build_evidence(narrative="BIOS AP rollout is on track.")
    e2 = _build_evidence(narrative="BIOS AP rollout is BLOCKED on Gen9 sign-off.")

    assert _evidence_dedup_key(e1) != _evidence_dedup_key(e2)
    assert persist_evidence(e1, program_id="acme", programs_root=programs_root) is True
    assert persist_evidence(e2, program_id="acme", programs_root=programs_root) is True

    records = read_jsonl_records(evidence_store_path("acme", programs_root))
    assert len(records) == 2


def test_p4_0_persist_evidence_records_backing_signal_ids_and_dedup_key(tmp_path: Path) -> None:
    """P4-0: backing_signal_ids are written as _backing_signal_ids and _dedup_key is present."""
    programs_root = tmp_path
    evidence = _build_evidence()
    backing = ("workiq/enrich/lane-ap-bios/2026-06-18/abcd1234abcd",)

    persist_evidence(
        evidence,
        program_id="acme",
        programs_root=programs_root,
        backing_signal_ids=backing,
    )

    records = read_jsonl_records(evidence_store_path("acme", programs_root))
    assert records[0]["_backing_signal_ids"] == list(backing)
    assert isinstance(records[0]["_dedup_key"], str)
    assert len(records[0]["_dedup_key"]) == 64   # sha256 hex


def test_p4_0_legacy_record_without_backing_admitted_when_gate_disabled(tmp_path: Path) -> None:
    """P4-0: when approved_signal_ids is None the gate is off (doctor/legacy path)."""
    programs_root = tmp_path
    evidence = _build_evidence()
    persist_evidence(evidence, program_id="acme", programs_root=programs_root)  # no backing

    latest = load_latest_evidence_by_lane("acme", programs_root=programs_root)
    assert latest["lane-ap-bios"].narrative_summary == evidence.narrative_summary


def test_p4_0_backed_evidence_admitted_only_when_signal_approved(tmp_path: Path) -> None:
    """P4-0 §17.8 Option A: unapproved-backed evidence is excluded from synthesis.

    Legacy (no backing) records are always admitted; backed records require at
    least one backing id in the approved set.
    """
    programs_root = tmp_path
    as_of = datetime(2026, 6, 18, tzinfo=timezone.utc)

    # Legacy record (ME-02 pre-P4-0): no backing signal.
    legacy = _build_evidence(lane_id="lane-legacy", narrative="legacy gather evidence")
    persist_evidence(legacy, program_id="acme", programs_root=programs_root)

    # Backed record (ME-03 enrich) whose backing signal is NOT yet approved.
    backed = _build_evidence(lane_id="lane-enrich", narrative="enrich evidence pending review")
    pending_id = _enrich_signal_id(lane_id="lane-enrich", evidence=backed, as_of=as_of)
    persist_evidence(
        backed,
        program_id="acme",
        programs_root=programs_root,
        backing_signal_ids=(pending_id,),
    )

    # Gate ON with empty approved set → legacy admitted, enrich excluded.
    gated = load_approved_evidence_by_lane(
        "acme",
        programs_root=programs_root,
        approved_signal_ids=frozenset(),
    )
    assert "lane-legacy" in gated          # legacy has no backing → admitted
    assert "lane-enrich" not in gated      # backed, unapproved → excluded (the §17.8 fix)

    # Once the backing signal is approved, the enrich evidence is admitted.
    gated_approved = load_approved_evidence_by_lane(
        "acme",
        programs_root=programs_root,
        approved_signal_ids=frozenset({pending_id}),
    )
    assert "lane-enrich" in gated_approved
    assert gated_approved["lane-enrich"].narrative_summary == backed.narrative_summary


def test_p4_0_evidence_store_decodes_source_refs_and_stale_after(tmp_path: Path) -> None:
    """The shared Zone A loader fully reconstructs source_refs + stale_after."""
    programs_root = tmp_path
    evidence = _build_evidence()
    persist_evidence(evidence, program_id="acme", programs_root=programs_root)

    records = load_evidence_records("acme", programs_root=programs_root)
    assert len(records) == 1
    rec = records[0]
    assert rec.evidence.source_refs == evidence.source_refs
    assert rec.evidence.stale_after == evidence.stale_after
    assert rec.evidence.etas[0].ado_id == "37777539"


def test_p4_2_enrich_path_writes_provenance_and_quality(tmp_path: Path) -> None:
    """P4-2: the enrich path records provenance + quality journals (were gather-only)."""
    programs_root = tmp_path
    as_of = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    evidence = _build_evidence()
    raw_response = "BIOS AP Gen9 rollout on track. ADO:37777539 sign-off ETA 2026-06-25."

    _record_enrich_provenance_and_quality(
        lane_id="lane-ap-bios",
        program_id="acme",
        evidence=evidence,
        raw_response=raw_response,
        as_of=as_of,
        programs_root=programs_root,
    )

    # Provenance journal
    prov_path = programs_root / "acme" / "journal" / "evidence_provenance.jsonl"
    assert prov_path.exists()
    prov_rows = list(read_jsonl_records(prov_path))
    assert len(prov_rows) == 1
    assert prov_rows[0]["source_type"] == "workiq_email"
    assert prov_rows[0]["lane_id"] == "lane-ap-bios"
    assert "narrative_summary" in prov_rows[0]["fields_populated"]

    # Quality journal
    qrecs = load_evidence_quality("acme", programs_root=programs_root)
    assert len(qrecs) == 1
    assert qrecs[0].source_type == "workiq_email"
    assert qrecs[0].extractor == "ContentExtractionAgent"
    assert qrecs[0].body_text_chars == len(raw_response)
    assert qrecs[0].etas_found == 1


def test_p4_0_enrich_signal_id_is_stable_for_identical_content() -> None:
    """Identical lane/date/narrative → identical signal id (enables skip-on-dedup)."""
    as_of = datetime(2026, 6, 18, tzinfo=timezone.utc)
    e = _build_evidence()
    id_a = _enrich_signal_id(lane_id="lane-ap-bios", evidence=e, as_of=as_of)
    id_b = _enrich_signal_id(lane_id="lane-ap-bios", evidence=e, as_of=as_of)
    assert id_a == id_b
    # Different narrative → different id.
    id_c = _enrich_signal_id(
        lane_id="lane-ap-bios",
        evidence=_build_evidence(narrative="different"),
        as_of=as_of,
    )
    assert id_a != id_c
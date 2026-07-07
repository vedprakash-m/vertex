from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.core.knowledge_candidate_store import active_candidates as active_knowledge_candidates
from src.core.knowledge_claim_store import load_all_claim_revisions, summarize_knowledge_status
from src.core.knowledge.vault import load_all_scope_sources, load_all_vault_entries
from src.core.knowledge_store import get_shared_knowledge_root


@dataclass(frozen=True, slots=True)
class KnowledgeVaultIntegritySummary:
    file_count: int
    missing_meta_count: int
    hash_mismatch_count: int
    missing_source_record_count: int
    missing_claim_ref_count: int
    missing_candidate_ref_count: int

    def issue_records(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        if self.missing_meta_count:
            records.append({"kind": "missing_metadata", "count": self.missing_meta_count})
        if self.hash_mismatch_count:
            records.append({"kind": "hash_mismatch", "count": self.hash_mismatch_count})
        if self.missing_source_record_count:
            records.append({"kind": "missing_source_record", "count": self.missing_source_record_count})
        if self.missing_claim_ref_count:
            records.append({"kind": "missing_claim_ref", "count": self.missing_claim_ref_count})
        if self.missing_candidate_ref_count:
            records.append({"kind": "missing_candidate_ref", "count": self.missing_candidate_ref_count})
        return records


def summarize_knowledge_vault_integrity(*, programs_root: Path) -> KnowledgeVaultIntegritySummary:
    shared_root = get_shared_knowledge_root(programs_root)
    summary = summarize_knowledge_status(knowledge_root=shared_root)
    present_vault_hashes = {entry.vault_hash for entry in load_all_vault_entries(programs_root=programs_root)}
    missing_source_record_count = 0
    for source in load_all_scope_sources(programs_root=programs_root):
        if source.vault_hash not in present_vault_hashes:
            missing_source_record_count += 1

    missing_claim_ref_count = 0
    for revision in load_all_claim_revisions(knowledge_root=shared_root):
        if any(vault_hash not in present_vault_hashes for vault_hash in _vault_hashes_for_source_refs((revision.source_ref,))):
            missing_claim_ref_count += 1

    missing_candidate_ref_count = 0
    for candidate in active_knowledge_candidates(programs_root=programs_root):
        candidate_refs = (candidate.source_ref, *candidate.corroborating_refs)
        if any(vault_hash not in present_vault_hashes for vault_hash in _vault_hashes_for_source_refs(candidate_refs)):
            missing_candidate_ref_count += 1

    return KnowledgeVaultIntegritySummary(
        file_count=summary.vault.file_count,
        missing_meta_count=summary.vault.missing_meta_count,
        hash_mismatch_count=summary.vault.hash_mismatch_count,
        missing_source_record_count=missing_source_record_count,
        missing_claim_ref_count=missing_claim_ref_count,
        missing_candidate_ref_count=missing_candidate_ref_count,
    )


def _vault_hashes_for_source_refs(source_refs: tuple[object, ...]) -> tuple[str, ...]:
    hashes: list[str] = []
    for source_ref in source_refs:
        vault_hash = getattr(source_ref, "vault_hash", None)
        if isinstance(vault_hash, str) and vault_hash:
            hashes.append(vault_hash)
    return tuple(hashes)
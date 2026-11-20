"""Claim-to-evidence map and QG-05 truth-consistency check (Zone A).

Pure and deterministic: no I/O, no clock, no AI/M365 imports. Findings never
raise; ``blocked`` applies the selected observe-only or release-block mode.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from enum import Enum


class FindingKind(str, Enum):
    UNSUPPORTED_CLAIM = "unsupported_claim"
    DANGLING_EVIDENCE_REF = "dangling_evidence_ref"


class TruthConsistencyMode(str, Enum):
    OBSERVE_ONLY = "observe_only"
    RELEASE_BLOCK = "release_block"


@dataclass(frozen=True, slots=True)
class ClaimEvidenceLink:
    claim_id: str
    claim_text: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClaimEvidenceFinding:
    kind: FindingKind
    claim_id: str
    evidence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimEvidenceReport:
    supported: tuple[ClaimEvidenceLink, ...]
    unsupported: tuple[ClaimEvidenceLink, ...]
    findings: tuple[ClaimEvidenceFinding, ...]


@dataclass(frozen=True, slots=True)
class TruthConsistencyResult:
    mode: TruthConsistencyMode
    would_block: bool
    findings: tuple[ClaimEvidenceFinding, ...]
    report: ClaimEvidenceReport

    @property
    def blocked(self) -> bool:
        return self.mode is TruthConsistencyMode.RELEASE_BLOCK and self.would_block


def build_claim_evidence_map(
    claims: Iterable[ClaimEvidenceLink],
    known_evidence_refs: Collection[str],
) -> ClaimEvidenceReport:
    known = {ref.strip() for ref in known_evidence_refs if ref.strip()}
    supported: list[ClaimEvidenceLink] = []
    unsupported: list[ClaimEvidenceLink] = []
    findings: list[ClaimEvidenceFinding] = []
    for claim in claims:
        refs = tuple(dict.fromkeys(ref.strip() for ref in claim.evidence_refs if ref.strip()))
        for ref in refs:
            if ref not in known:
                findings.append(
                    ClaimEvidenceFinding(FindingKind.DANGLING_EVIDENCE_REF, claim.claim_id, ref)
                )
        if any(ref in known for ref in refs):
            supported.append(claim)
        else:
            unsupported.append(claim)
            findings.append(ClaimEvidenceFinding(FindingKind.UNSUPPORTED_CLAIM, claim.claim_id))
    return ClaimEvidenceReport(tuple(supported), tuple(unsupported), tuple(findings))


def evaluate_truth_consistency(
    claims: Iterable[ClaimEvidenceLink],
    known_evidence_refs: Collection[str],
    mode: TruthConsistencyMode = TruthConsistencyMode.OBSERVE_ONLY,
) -> TruthConsistencyResult:
    report = build_claim_evidence_map(claims, known_evidence_refs)
    would_block = bool(report.findings)
    return TruthConsistencyResult(mode, would_block, report.findings, report)

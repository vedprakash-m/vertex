from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.models import EvidencePacket, RunManifest, ScorecardEvidencePacket


def build_compact_manifest(
    manifest: RunManifest,
    eml_path: Path | None,
    html_path: Path | None,
    md_path: Path | None,
    manifest_path: Path | None,
    snapshot_path: Path | None,
    quality_matrix_md_path: Path | None,
    quality_matrix_json_path: Path | None,
    remediation_md_path: Path | None,
    remediation_json_path: Path | None,
    overrides_path: Path,
    narratives_dir: Path,
    review_status_path: Path,
    warnings: tuple[str, ...],
    suggested_subject: str = "",
    suggested_preheader: str = "",
    verbose_evidence: dict[str, Any] | None = None,
    trace_path: Path | None = None,
) -> dict[str, Any]:
    payload = {
        "manifest_id": manifest.manifest_id,
        "issue_number": manifest.issue_number,
        "edition": manifest.edition,
        "qg_results": dict(manifest.qg_results),
        "freshness_summary": dict(manifest.freshness_summary),
        "suggested_subject": suggested_subject,
        "suggested_preheader": suggested_preheader,
        "paths": {
            "eml": str(eml_path) if eml_path is not None else None,
            "html": str(html_path) if html_path is not None else None,
            "md": str(md_path) if md_path is not None else None,
            "manifest": str(manifest_path) if manifest_path is not None else None,
            "snapshot": str(snapshot_path) if snapshot_path is not None else None,
            "quality_matrix_md": str(quality_matrix_md_path) if quality_matrix_md_path is not None else None,
            "quality_matrix_json": str(quality_matrix_json_path) if quality_matrix_json_path is not None else None,
            "remediation_md": str(remediation_md_path) if remediation_md_path is not None else None,
            "remediation_json": str(remediation_json_path) if remediation_json_path is not None else None,
            "trace": str(trace_path) if trace_path is not None else None,
            "overrides": str(overrides_path),
            "narratives": str(narratives_dir),
            "review_status": str(review_status_path),
        },
        "warnings": list(warnings),
    }
    if verbose_evidence is not None:
        payload["evidence"] = verbose_evidence
    return payload


def build_verbose_evidence_payload(
    evidence_by_item: dict[int, EvidencePacket],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
) -> dict[str, Any]:
    return {
        "items": {
            str(work_item_id): {
                "confidence": packet.confidence.value,
                "tier": packet.tier.value,
                "summary": packet.summary_for_reviewer,
                "revision_count": len(packet.revisions),
                "comment_count": len(packet.comments),
                "enrichment_count": len(packet.enrichments),
            }
            for work_item_id, packet in sorted(evidence_by_item.items())
        },
        "scorecards": {
            scorecard_name: {
                dimension_name: {
                    "derived_risk": packet.derived_risk.value,
                    "prior_confirmed_risk": packet.prior_confirmed_risk.value if packet.prior_confirmed_risk is not None else None,
                    "total_items": packet.total_items,
                    "blocked_count": packet.blocked_count,
                    "overdue_count": packet.overdue_count,
                    "stale_count": packet.stale_count,
                    "unowned_count": packet.unowned_count,
                    "item_ids": list(packet.item_ids),
                    "ado_query_url": packet.ado_query_url,
                }
                for dimension_name, packet in sorted(dimension_packets.items())
            }
            for scorecard_name, dimension_packets in sorted(scorecard_packets.items())
        },
    }


def render_stdout_payload(
    output_format: str,
    compact_manifest: dict[str, Any],
    html_body: str,
    markdown_body: str,
) -> str:
    normalized = output_format.strip().lower()
    if normalized == "html":
        return html_body
    if normalized in {"md", "markdown"}:
        return markdown_body
    return json.dumps(compact_manifest, separators=(",", ":"), ensure_ascii=False)
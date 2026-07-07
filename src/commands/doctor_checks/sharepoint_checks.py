"""SharePoint quality gate checks (QG-SP-1 through QG-SP-8).

Spec: specs/sharepoint.md §12 (Doctor Quality Gates)
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.edition_resolver import resolve_edition
from src.core.gather_state_store import load_gather_state


def run_sharepoint_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    strict_lt_alignment: bool = False,
) -> DoctorReport:
    """Run QG-SP-1 through QG-SP-8 for SharePoint/LT deck integration health."""
    resolved = resolve_edition(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("SharePoint", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    program_id = resolved.program.id
    checks: list[DoctorCheck] = []

    # Load program config (raw)
    raw_program: dict[str, Any] = resolved.raw_program

    checks.extend(_check_qg_sp_1(raw_program))
    checks.extend(_check_qg_sp_2(raw_program))
    checks.extend(_check_qg_sp_3(program_id, programs_root=programs_root))
    checks.extend(_check_qg_sp_4(program_id, programs_root=programs_root))
    if strict_lt_alignment:
        checks.extend(_check_qg_sp_5(program_id, programs_root=programs_root))
    checks.extend(_check_qg_sp_6(program_id, programs_root=programs_root))
    checks.extend(_check_qg_sp_7(program_id, programs_root=programs_root))
    checks.extend(_check_qg_sp_8(raw_program))

    return DoctorReport(edition=edition_name, checks=tuple(checks))


# ---------------------------------------------------------------------------
# QG-SP-1: m365.sharepoint.lt_deck block present in program config
# ---------------------------------------------------------------------------

def _check_qg_sp_1(raw_program: dict[str, Any]) -> list[DoctorCheck]:
    m365 = raw_program.get("m365") or {}
    sharepoint = m365.get("sharepoint") or {}
    lt_deck = sharepoint.get("lt_deck")
    if lt_deck:
        return [DoctorCheck("QG-SP-1: LT deck configured", "ok", "m365.sharepoint.lt_deck block is present.")]
    return [DoctorCheck(
        "QG-SP-1: LT deck configured",
        "warn",
        "m365.sharepoint.lt_deck is not configured. "
        "SharePoint LT deck extraction will be skipped. "
        "Add m365.sharepoint.lt_deck.url and current_deck_date to your program config.",
    )]


# ---------------------------------------------------------------------------
# QG-SP-2: current_deck_date ≤ 45 days old
# ---------------------------------------------------------------------------

def _check_qg_sp_2(raw_program: dict[str, Any]) -> list[DoctorCheck]:
    m365 = raw_program.get("m365") or {}
    sharepoint = m365.get("sharepoint") or {}
    lt_deck = sharepoint.get("lt_deck") or {}
    deck_date_raw = lt_deck.get("current_deck_date")
    if deck_date_raw is None:
        return [DoctorCheck(
            "QG-SP-2: LT deck date freshness",
            "warn",
            "m365.sharepoint.lt_deck.current_deck_date is not set. Cannot validate deck freshness.",
        )]
    try:
        deck_date = date.fromisoformat(str(deck_date_raw))
    except (ValueError, TypeError):
        return [DoctorCheck(
            "QG-SP-2: LT deck date freshness",
            "warn",
            f"m365.sharepoint.lt_deck.current_deck_date '{deck_date_raw}' is not a valid ISO date.",
        )]
    today = datetime.now(timezone.utc).date()
    age_days = (today - deck_date).days
    if age_days <= 45:
        return [DoctorCheck(
            "QG-SP-2: LT deck date freshness",
            "ok",
            f"LT deck date {deck_date} is {age_days} day(s) old (≤45 day threshold).",
        )]
    return [DoctorCheck(
        "QG-SP-2: LT deck date freshness",
        "warn",
        f"LT deck date {deck_date} is {age_days} day(s) old (>45 day threshold). "
        "Update m365.sharepoint.lt_deck.current_deck_date.",
    )]


# ---------------------------------------------------------------------------
# QG-SP-3: ≥1 approved SharePoint signal in last gather cycle
# ---------------------------------------------------------------------------

def _check_qg_sp_3(program_id: str, *, programs_root: Path) -> list[DoctorCheck]:
    journal_path = programs_root / program_id / "signals" / "journal.jsonl"
    if not journal_path.exists():
        return [DoctorCheck(
            "QG-SP-3: Approved SharePoint signal",
            "warn",
            "No signal journal found. Run 'vertex gather' first.",
        )]

    try:
        from src.core.jsonl_utils import read_jsonl_records
        approved_sp = [
            r for r in read_jsonl_records(journal_path)
            if isinstance(r, dict)
            and r.get("review_policy") != "pending"
            and r.get("source") in ("sharepoint", "lt_deck", "sharepoint_pptx", "sharepoint_docx")
        ]
        if approved_sp:
            return [DoctorCheck(
                "QG-SP-3: Approved SharePoint signal",
                "ok",
                f"{len(approved_sp)} approved SharePoint signal(s) found in journal.",
            )]
        # Check for any pending SharePoint signals
        pending_sp = [
            r for r in read_jsonl_records(journal_path)
            if isinstance(r, dict)
            and r.get("review_policy") == "pending"
            and r.get("source") in ("sharepoint", "lt_deck", "sharepoint_pptx", "sharepoint_docx")
        ]
        if pending_sp:
            return [DoctorCheck(
                "QG-SP-3: Approved SharePoint signal",
                "warn",
                f"{len(pending_sp)} PENDING SharePoint signal(s) found but none approved. "
                "Run 'vertex approve' to approve SharePoint signals.",
            )]
        return [DoctorCheck(
            "QG-SP-3: Approved SharePoint signal",
            "warn",
            "No SharePoint signals found in journal. "
            "Run 'vertex gather --sharepoint' to gather SharePoint signals.",
        )]
    except Exception as exc:
        return [DoctorCheck("QG-SP-3: Approved SharePoint signal", "warn", f"Could not read signal journal: {exc}")]


# ---------------------------------------------------------------------------
# QG-SP-4: ≥1 source_subtype: lt_deck in engms_pages.yaml
# ---------------------------------------------------------------------------

def _check_qg_sp_4(program_id: str, *, programs_root: Path) -> list[DoctorCheck]:
    pages_path = programs_root / program_id / "kb" / "engms_pages.yaml"
    if not pages_path.exists():
        return [DoctorCheck(
            "QG-SP-4: LT deck in engms_pages",
            "info",
            "No engms_pages.yaml found. Add an lt_deck entry for LT deck integration.",
        )]
    try:
        import yaml
        with open(pages_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        pages = data.get("engms_pages", []) or []
        lt_deck_pages = [p for p in pages if isinstance(p, dict) and p.get("source_subtype") == "lt_deck"]
        if lt_deck_pages:
            return [DoctorCheck(
                "QG-SP-4: LT deck in engms_pages",
                "ok",
                f"{len(lt_deck_pages)} lt_deck page(s) configured in engms_pages.yaml.",
            )]
        return [DoctorCheck(
            "QG-SP-4: LT deck in engms_pages",
            "info",
            "No lt_deck source_subtype entries in engms_pages.yaml. "
            "Add a page with source_subtype: lt_deck for LT deck integration.",
        )]
    except Exception as exc:
        return [DoctorCheck("QG-SP-4: LT deck in engms_pages", "warn", f"Could not parse engms_pages.yaml: {exc}")]


# ---------------------------------------------------------------------------
# QG-SP-5: lt_deck_alignment == "diverged" (advisory; enforced with --strict-lt-alignment)
# ---------------------------------------------------------------------------

def _check_qg_sp_5(program_id: str, *, programs_root: Path) -> list[DoctorCheck]:
    """Check for LT deck alignment divergence. Only run when --strict-lt-alignment passed."""
    evidence_path = programs_root / program_id / "journal" / "evidence_store.jsonl"
    if not evidence_path.exists():
        return [DoctorCheck(
            "QG-SP-5: LT deck alignment",
            "info",
            "No evidence store found. Run 'vertex report' to generate evidence.",
        )]
    try:
        from src.core.jsonl_utils import read_jsonl_records
        diverged = [
            r for r in read_jsonl_records(evidence_path)
            if isinstance(r, dict) and r.get("lt_deck_alignment") == "diverged"
        ]
        if diverged:
            workstreams = list({r.get("workstream_id", "unknown") for r in diverged})
            return [DoctorCheck(
                "QG-SP-5: LT deck alignment",
                "warn",
                f"LT deck alignment diverged in {len(diverged)} evidence record(s) "
                f"across workstream(s): {', '.join(sorted(workstreams))}. "
                "LT deck signals conflict with ADO/WorkIQ signals.",
            )]
        return [DoctorCheck(
            "QG-SP-5: LT deck alignment",
            "ok",
            "No LT deck alignment divergence found in evidence store.",
        )]
    except Exception as exc:
        return [DoctorCheck("QG-SP-5: LT deck alignment", "warn", f"Could not read evidence store: {exc}")]


# ---------------------------------------------------------------------------
# QG-SP-6: last_extracted > cadence_days * 1.5 (stale extraction)
# ---------------------------------------------------------------------------

def _check_qg_sp_6(program_id: str, *, programs_root: Path) -> list[DoctorCheck]:
    """Check if any lt_deck pages are overdue for re-extraction."""
    gather_state = load_gather_state(program_id, programs_root=programs_root)
    if gather_state is None:
        return []  # No state yet — not an error

    try:
        sp_state = (getattr(gather_state, "m365_discovery", None) or {}).get("sharepoint") or {}
        doc_states = sp_state.get("doc_states") or {}

        now = datetime.now(timezone.utc)
        overdue: list[str] = []
        for doc_id, doc_info in doc_states.items():
            if not isinstance(doc_info, dict):
                continue
            cadence_days = doc_info.get("cadence_days")
            last_extracted_raw = doc_info.get("last_extracted")
            if cadence_days is None or last_extracted_raw is None:
                continue
            try:
                last_extracted = datetime.fromisoformat(last_extracted_raw)
                if last_extracted.tzinfo is None:
                    last_extracted = last_extracted.replace(tzinfo=timezone.utc)
                age_days = (now - last_extracted).days
                if age_days > cadence_days * 1.5:
                    overdue.append(f"{doc_id} ({age_days}d > {cadence_days * 1.5:.0f}d threshold)")
            except (ValueError, TypeError):
                continue

        if overdue:
            return [DoctorCheck(
                "QG-SP-6: LT deck extraction staleness",
                "fail",
                f"{len(overdue)} SharePoint document(s) overdue for re-extraction: "
                + "; ".join(overdue)
                + ". Run 'vertex gather --sharepoint --force-refresh'.",
            )]
        return [DoctorCheck(
            "QG-SP-6: LT deck extraction staleness",
            "ok",
            "All SharePoint documents are within their extraction cadence.",
        )]
    except Exception as exc:
        return [DoctorCheck("QG-SP-6: LT deck extraction staleness", "warn", f"Could not read gather state: {exc}")]


# ---------------------------------------------------------------------------
# QG-SP-7: programs/<prog>/backfill/sharepoint/ exists
# ---------------------------------------------------------------------------

def _check_qg_sp_7(program_id: str, *, programs_root: Path) -> list[DoctorCheck]:
    backfill_path = programs_root / program_id / "backfill" / "sharepoint"
    if backfill_path.exists():
        pptx_files = list(backfill_path.glob("*.pptx"))
        return [DoctorCheck(
            "QG-SP-7: Backfill directory",
            "ok",
            f"Backfill directory exists with {len(pptx_files)} .pptx file(s).",
        )]
    return [DoctorCheck(
        "QG-SP-7: Backfill directory",
        "info",
        f"No backfill directory at programs/{program_id}/backfill/sharepoint/. "
        "Create it and add .pptx files for offline LT deck backfill (SP4-2).",
    )]


# ---------------------------------------------------------------------------
# QG-SP-8: gather_timeout_seconds >= 300
# ---------------------------------------------------------------------------

def _check_qg_sp_8(raw_program: dict[str, Any]) -> list[DoctorCheck]:
    gather_cfg = raw_program.get("gather") or {}
    timeout = gather_cfg.get("gather_timeout_seconds")
    if timeout is None:
        return [DoctorCheck(
            "QG-SP-8: Gather timeout",
            "warn",
            "gather.gather_timeout_seconds is not set. "
            "WorkIQ LT deck extraction requires ≥300s timeout (P4-22). "
            "Set gather.gather_timeout_seconds: 300 in your program config.",
        )]
    try:
        timeout_int = int(timeout)
    except (ValueError, TypeError):
        return [DoctorCheck(
            "QG-SP-8: Gather timeout",
            "warn",
            f"gather.gather_timeout_seconds '{timeout}' is not a valid integer.",
        )]
    if timeout_int >= 300:
        return [DoctorCheck(
            "QG-SP-8: Gather timeout",
            "ok",
            f"gather.gather_timeout_seconds={timeout_int} meets the ≥300s requirement for WorkIQ LT deck extraction.",
        )]
    return [DoctorCheck(
        "QG-SP-8: Gather timeout",
        "warn",
        f"gather.gather_timeout_seconds={timeout_int} is below the 300s minimum for WorkIQ LT deck extraction. "
        "Increase to at least 300.",
    )]

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import portalocker

from src.ai.action_extractor import ActionExtractor, ActionExtractorError
from src.commands.gather_pipeline.models import PersistenceStageInput, PersistenceStageResult
from src.core.action_extractor_basic import extract_actions_from_signals
from src.core.action_tracker import append_action
from src.core.decision_extractor_basic import extract_decisions_from_signals
from src.core.decision_register import upsert_decisions
from src.core.incident_journal_store import append_incident_entry
from src.core.models_v2 import IncidentEntry, Program, ReviewPolicy, Signal, SignalReviewDecision
from src.core.program_fact_store import load_program_facts, project_action_items
from src.core.program_paths import get_dedup_drop_log_path
from src.core.signal_classification import classify_signal
from src.core.signal_dedup import dedupe_signals_with_audit
from src.core.signal_ref_utils import widen_ws_wi_refs
from src.core.signal_review import compute_auto_approval_policies, signal_can_be_auto_approved, write_autonomy_audit_entries


log = logging.getLogger(__name__)


def run_persistence_stage(stage_input: PersistenceStageInput) -> PersistenceStageResult:
    dedup_result = dedupe_signals_with_audit(
        stage_input.candidate_signals,
        existing_signals=stage_input.existing_signals,
    )
    new_signals = tuple(widen_ws_wi_refs(signal, stage_input.workstreams) for signal in dedup_result.signals)
    if not stage_input.dry_run and dedup_result.drop_log:
        write_dedup_drop_log(
            stage_input.program_id,
            dedup_result.drop_log,
            programs_root=stage_input.programs_root,
        )

    if not stage_input.dry_run:
        for signal in new_signals:
            stage_input.signal_store.append(classify_signal(signal))
        for entry in build_incident_entries(new_signals, recorded_at=stage_input.current_time):
            append_incident_entry(entry, programs_root=stage_input.programs_root)

    extracted_actions = list(extract_actions_from_signals(new_signals, program_id=stage_input.program_id))
    if not stage_input.dry_run and stage_input.program.ai is not None and stage_input.program.ai.enabled:
        extracted_actions.extend(
            (stage_input.ai_action_extractor or extract_actions_with_ai)(stage_input.program, new_signals)
        )

    if not stage_input.dry_run:
        existing_action_ids = {
            action.id
            for action in project_action_items(
                load_program_facts(
                    stage_input.program_id,
                    programs_root=stage_input.programs_root,
                    fact_types=("action.item",),
                )
            )
        }
        for action in extracted_actions:
            if action.id in existing_action_ids:
                continue
            append_action(stage_input.program_id, action, programs_root=stage_input.programs_root)
            existing_action_ids.add(action.id)

        extracted_decisions = extract_decisions_from_signals(new_signals, program_id=stage_input.program_id)
        if extracted_decisions:
            upsert_decisions(stage_input.program_id, extracted_decisions, programs_root=stage_input.programs_root)

    auto_reviews_written = 0
    pending_review = 0
    for signal in new_signals:
        if not signal_can_be_auto_approved(signal):
            pending_review += 1
            continue
        if stage_input.dry_run:
            continue
        stage_input.signal_store.append_review(
            stage_input.program_id,
            SignalReviewDecision(
                signal_id=signal.id,
                decision="approved",
                reviewed_at=stage_input.current_time,
                reviewed_by="system",
                note=None,
            ),
        )
        auto_reviews_written += 1

    if not stage_input.dry_run:
        try:
            all_signals_for_review = stage_input.signal_store.read(stage_input.program_id)
            gathered_review_states = stage_input.signal_store.read_reviews(stage_input.program_id)
            auto_policy_changes = compute_auto_approval_policies(all_signals_for_review, gathered_review_states)
            if auto_policy_changes:
                for signal_id, new_policy in auto_policy_changes.items():
                    if new_policy != ReviewPolicy.AUTO_APPROVED or signal_id in gathered_review_states:
                        continue
                    matched_signal = next((candidate for candidate in all_signals_for_review if candidate.id == signal_id), None)
                    if matched_signal is None:
                        continue
                    stage_input.signal_store.append_review(
                        stage_input.program_id,
                        SignalReviewDecision(
                            signal_id=signal_id,
                            decision="approved",
                            reviewed_at=stage_input.current_time,
                            reviewed_by="auto_enforcement",
                            note="Promoted by adaptive auto-approval policy (FR-SG-38)",
                        ),
                    )
                write_autonomy_audit_entries(
                    stage_input.program_id,
                    auto_policy_changes,
                    programs_root=stage_input.programs_root,
                )
        except Exception as exc:
            # WS-13 PB-5: governance writes MUST be loud. The autonomy audit
            # is the substrate for trust decisions; a silent failure here
            # would let auto-approved actions land without an audit trail.
            # Surface the failure as a structured warning the stage result
            # can carry into gather_state.json + doctor, and re-raise
            # (the stage caller decides whether to force through).
            log.error(
                "WS-13 PB-5: autonomy audit write failed for program %s: %s",
                stage_input.program_id,
                exc,
            )
            raise

    return PersistenceStageResult(
        new_signals=new_signals,
        pending_review=pending_review,
        auto_reviews_written=auto_reviews_written,
        extracted_action_count=len(extracted_actions),
    )


def write_dedup_drop_log(program_id: str, drop_log: tuple, *, programs_root: Path) -> None:
    path = get_dedup_drop_log_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            for event in drop_log:
                handle.write(json.dumps(asdict(event)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)


def build_incident_entries(
    signals: tuple[Signal, ...],
    *,
    recorded_at: datetime,
) -> tuple[IncidentEntry, ...]:
    entries: list[IncidentEntry] = []
    for signal in signals:
        if signal.source.strip().lower() != "icm":
            continue
        incident_id = incident_id_from_signal(signal)
        if incident_id is None:
            continue
        metadata = signal.metadata or {}
        ado_entity_refs = tuple(
            ref
            for ref in signal.entity_refs
            if ref.upper().startswith("WI:")
        )
        linked_work_item_ids = tuple(
            int(ref.split(":", 1)[1])
            for ref in ado_entity_refs
            if ref.split(":", 1)[1].isdigit()
        )
        entries.append(
            IncidentEntry(
                program_id=signal.program_id,
                incident_id=incident_id,
                signal_id=signal.id,
                observed_at=signal.timestamp,
                recorded_at=recorded_at,
                belief_change_summary=signal.text,
                workstream_id=signal.workstream_id,
                owning_team=str(metadata.get("owning_team", "")).strip() or None,
                severity=_severity_from_value(metadata.get("severity")),
                source_path=str(metadata.get("source_path", "")).strip() or None,
                query_id=str(metadata.get("query_id", "")).strip() or None,
                linked_work_item_ids=linked_work_item_ids,
                ado_entity_refs=ado_entity_refs,
                raw_ref=signal.raw_ref,
                confidence=signal.confidence,
            )
        )
    return tuple(entries)


def incident_id_from_signal(signal: Signal) -> str | None:
    metadata = signal.metadata or {}
    incident_value = metadata.get("incident_id")
    if incident_value not in (None, ""):
        return str(incident_value).strip() or None
    for entity_ref in signal.entity_refs:
        if entity_ref.upper().startswith("ICM:"):
            return entity_ref.split(":", 1)[1].strip() or None
    if signal.raw_ref is not None and signal.raw_ref.lower().startswith("icm:"):
        return signal.raw_ref.split(":", 1)[1].strip() or None
    return None


def extract_actions_with_ai(program: Program, signals: tuple[Signal, ...]) -> tuple:
    if program.ai is None or not program.ai.enabled or not signals:
        return ()
    try:
        extractor = ActionExtractor.from_program(program)
        return extractor.extract_actions(program_id=program.id, signals=signals)
    except (ActionExtractorError, RuntimeError) as error:
        log.warning("AI action extraction skipped for %s: %s", program.id, error)
        return ()


def _severity_from_value(raw_value: object) -> int:
    if raw_value is None:
        return 0
    text = str(raw_value).strip()
    match = next((char for char in text if char.isdigit()), None)
    return int(match) if match is not None else 0

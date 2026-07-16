from __future__ import annotations

from dataclasses import dataclass, field, make_dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EventPayloadSchema:
    event_type: str
    payload_type: type
    required_fields: frozenset[str]
    optional_fields: frozenset[str]
    field_types: dict[str, str]
    entity_ref_fields: frozenset[str]
    affects_support_tables: frozenset[str]
    dedupe_core_fields: frozenset[str]
    is_control: bool
    support_table_updater: dict[str, Any]

    def validate(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError(f"{self.event_type} payload must be a mapping.")
        for field_name in self.required_fields:
            if field_name not in payload:
                raise ValueError(f"{self.event_type} missing required field: {field_name}")
        allowed_fields = self.required_fields | self.optional_fields
        for field_name, value in payload.items():
            if field_name not in allowed_fields:
                continue
            _validate_field_type(self.event_type, field_name, self.field_types.get(field_name), value)


@dataclass(frozen=True, slots=True)
class StubPayloadSchema(EventPayloadSchema):
    def validate(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError(f"{self.event_type} payload must be a mapping.")


def _validate_field_type(event_type: str, field_name: str, field_type: str | None, value: Any) -> None:
    if field_type is None:
        return
    if field_type == "dict_or_none":
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{event_type}.{field_name} must be a mapping or null.")
        return
    if field_type == "str" and not isinstance(value, str):
        raise ValueError(f"{event_type}.{field_name} must be a string.")
    if field_type == "int" and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{event_type}.{field_name} must be an integer.")
    if field_type == "float" and not isinstance(value, (int, float)):
        raise ValueError(f"{event_type}.{field_name} must be numeric.")
    if field_type == "bool" and not isinstance(value, bool):
        raise ValueError(f"{event_type}.{field_name} must be a boolean.")
    if field_type == "dict" and not isinstance(value, dict):
        raise ValueError(f"{event_type}.{field_name} must be a mapping.")
    if field_type == "list" and not isinstance(value, list):
        raise ValueError(f"{event_type}.{field_name} must be a list.")


def _payload_class_name(event_type: str) -> str:
    parts = event_type.replace(".v1", "").replace(".", " ").replace("_", " ").split()
    return "".join(part.capitalize() for part in parts) + "Payload"


def _build_payload_type(event_type: str, required_fields: tuple[str, ...], optional_fields: tuple[str, ...]) -> type:
    fields: list[Any] = []
    for field_name in required_fields:
        fields.append((field_name, object))
    for field_name in optional_fields:
        fields.append((field_name, object, field(default=None)))
    return make_dataclass(_payload_class_name(event_type), fields, frozen=True, slots=True)


def _build_schema(
    event_type: str,
    *,
    required_fields: tuple[str, ...] = (),
    optional_fields: tuple[str, ...] = (),
    field_types: dict[str, str] | None = None,
    entity_ref_fields: tuple[str, ...] = (),
    affects_support_tables: tuple[str, ...] = (),
    dedupe_core_fields: tuple[str, ...] = (),
    is_control: bool = False,
    support_table_updater: dict[str, Any] | None = None,
    stub: bool = False,
) -> EventPayloadSchema:
    payload_type = _build_payload_type(event_type, required_fields, optional_fields)
    schema_type = StubPayloadSchema if stub else EventPayloadSchema
    return schema_type(
        event_type=event_type,
        payload_type=payload_type,
        required_fields=frozenset(required_fields),
        optional_fields=frozenset(optional_fields),
        field_types=dict(field_types or {}),
        entity_ref_fields=frozenset(entity_ref_fields),
        affects_support_tables=frozenset(affects_support_tables),
        dedupe_core_fields=frozenset(dedupe_core_fields),
        is_control=is_control,
        support_table_updater=dict(support_table_updater or {}),
    )


_REAL_EVENT_SCHEMAS: dict[str, EventPayloadSchema] = {
    "program.charter_established.v1": _build_schema(
        "program.charter_established.v1",
        required_fields=("charter_id", "mission", "scope_statement", "success_criteria"),
        optional_fields=("sponsor",),
        field_types={"charter_id": "str", "mission": "str", "scope_statement": "str", "success_criteria": "list", "sponsor": "str"},
        entity_ref_fields=("charter_id", "sponsor"),
        dedupe_core_fields=("charter_id",),
    ),
    "program.charter_revised.v1": _build_schema(
        "program.charter_revised.v1",
        required_fields=("charter_id", "revision_summary", "changed_fields", "prior_charter_event_id"),
        field_types={"charter_id": "str", "revision_summary": "str", "changed_fields": "dict", "prior_charter_event_id": "str"},
        entity_ref_fields=("charter_id",),
        dedupe_core_fields=("charter_id", "changed_fields"),
    ),
    "program.phase_entered.v1": _build_schema(
        "program.phase_entered.v1",
        required_fields=("phase_id", "phase_name"),
        optional_fields=("entry_criteria_met",),
        field_types={"phase_id": "str", "phase_name": "str", "entry_criteria_met": "list"},
        entity_ref_fields=("phase_id",),
        dedupe_core_fields=("phase_id", "phase_name"),
    ),
    "program.phase_exited.v1": _build_schema(
        "program.phase_exited.v1",
        required_fields=("phase_id", "exit_reason"),
        optional_fields=("outcome",),
        field_types={"phase_id": "str", "exit_reason": "str", "outcome": "str"},
        entity_ref_fields=("phase_id",),
        dedupe_core_fields=("phase_id", "exit_reason"),
    ),
    "program.scope_changed.v1": _build_schema(
        "program.scope_changed.v1",
        required_fields=("change_kind", "description"),
        optional_fields=("affected_entities",),
        field_types={"change_kind": "str", "description": "str", "affected_entities": "list"},
        entity_ref_fields=("affected_entities",),
        dedupe_core_fields=("change_kind", "description"),
    ),
    "program.sub_program_added.v1": _build_schema(
        "program.sub_program_added.v1",
        required_fields=("sub_program_id", "name", "relationship"),
        optional_fields=("cadence",),
        field_types={"sub_program_id": "str", "name": "str", "relationship": "str", "cadence": "str"},
        entity_ref_fields=("sub_program_id",),
        dedupe_core_fields=("sub_program_id", "name", "relationship"),
    ),
    "schedule.baseline_set.v1": _build_schema(
        "schedule.baseline_set.v1",
        required_fields=("schedule_id", "baseline_name", "milestone_dates"),
        optional_fields=("supersedes_baseline_id",),
        field_types={"schedule_id": "str", "baseline_name": "str", "milestone_dates": "dict", "supersedes_baseline_id": "str"},
        entity_ref_fields=("schedule_id", "supersedes_baseline_id"),
        dedupe_core_fields=("schedule_id", "baseline_name", "milestone_dates"),
    ),
    "workstream.created.v1": _build_schema(
        "workstream.created.v1",
        required_fields=("workstream_id", "name"),
        optional_fields=("owner_person_id", "parent_workstream_id"),
        field_types={"workstream_id": "str", "name": "str", "owner_person_id": "str", "parent_workstream_id": "str"},
        entity_ref_fields=("workstream_id", "owner_person_id", "parent_workstream_id"),
        dedupe_core_fields=("workstream_id", "name"),
    ),
    "workstream.owner_changed.v1": _build_schema(
        "workstream.owner_changed.v1",
        required_fields=("workstream_id", "new_owner_person_id"),
        optional_fields=("prior_owner_person_id",),
        field_types={"workstream_id": "str", "new_owner_person_id": "str", "prior_owner_person_id": "str"},
        entity_ref_fields=("workstream_id", "new_owner_person_id", "prior_owner_person_id"),
        dedupe_core_fields=("workstream_id", "new_owner_person_id"),
    ),
    "workstream.status_changed.v1": _build_schema(
        "workstream.status_changed.v1",
        required_fields=("workstream_id", "new_status"),
        optional_fields=("prior_status", "reason"),
        field_types={"workstream_id": "str", "new_status": "str", "prior_status": "str", "reason": "str"},
        entity_ref_fields=("workstream_id",),
        dedupe_core_fields=("workstream_id", "new_status"),
    ),
    "milestone.created.v1": _build_schema(
        "milestone.created.v1",
        required_fields=("milestone_id", "name", "target_date"),
        optional_fields=("workstream_id", "exit_criteria"),
        field_types={"milestone_id": "str", "name": "str", "target_date": "str", "workstream_id": "str", "exit_criteria": "list"},
        entity_ref_fields=("milestone_id", "workstream_id"),
        dedupe_core_fields=("milestone_id", "name", "target_date"),
    ),
    "milestone.date_revised.v1": _build_schema(
        "milestone.date_revised.v1",
        required_fields=("milestone_id", "new_target_date"),
        optional_fields=("prior_target_date", "reason"),
        field_types={"milestone_id": "str", "new_target_date": "str", "prior_target_date": "str", "reason": "str"},
        entity_ref_fields=("milestone_id",),
        dedupe_core_fields=("milestone_id", "new_target_date"),
    ),
    "milestone.status_changed.v1": _build_schema(
        "milestone.status_changed.v1",
        required_fields=("milestone_id", "new_status"),
        optional_fields=("reason",),
        field_types={"milestone_id": "str", "new_status": "str", "reason": "str"},
        entity_ref_fields=("milestone_id",),
        dedupe_core_fields=("milestone_id", "new_status"),
    ),
    "milestone.completed.v1": _build_schema(
        "milestone.completed.v1",
        required_fields=("milestone_id", "completed_on"),
        optional_fields=("evidence",),
        field_types={"milestone_id": "str", "completed_on": "str", "evidence": "str"},
        entity_ref_fields=("milestone_id",),
        dedupe_core_fields=("milestone_id", "completed_on"),
    ),
    "deliverable.created.v1": _build_schema(
        "deliverable.created.v1",
        required_fields=("deliverable_id", "name"),
        optional_fields=("workstream_id", "due_date"),
        field_types={"deliverable_id": "str", "name": "str", "workstream_id": "str", "due_date": "str"},
        entity_ref_fields=("deliverable_id", "workstream_id"),
        dedupe_core_fields=("deliverable_id", "name", "due_date"),
    ),
    "deliverable.status_changed.v1": _build_schema(
        "deliverable.status_changed.v1",
        required_fields=("deliverable_id", "new_status"),
        optional_fields=("reason",),
        field_types={"deliverable_id": "str", "new_status": "str", "reason": "str"},
        entity_ref_fields=("deliverable_id",),
        dedupe_core_fields=("deliverable_id", "new_status"),
    ),
    "sku_generation.added.v1": _build_schema(
        "sku_generation.added.v1",
        required_fields=("sku_generation_id", "name"),
        optional_fields=("first_deployment_date", "products"),
        field_types={"sku_generation_id": "str", "name": "str", "first_deployment_date": "str", "products": "list"},
        entity_ref_fields=("sku_generation_id", "products"),
        dedupe_core_fields=("sku_generation_id", "name", "first_deployment_date"),
    ),
    # Deployment lifecycle (v2.22, ADR-0006 R2): faithful event types for REV
    # deployment claims. Previously these were silently shoehorned into
    # milestone.completed/deliverable.status_changed/incident.opened, producing
    # wrong-type false-positives. They are "detected-but-not-v1-authoritative"
    # per S-0g — surfaced with their true type so the quality metric measures
    # type-correctness cleanly, while authority_scope keeps them non-authoritative.
    "deployment.completed.v1": _build_schema(
        "deployment.completed.v1",
        required_fields=("deployment_id", "artifact_name"),
        optional_fields=("completed_on", "target_milestone_id"),
        field_types={"deployment_id": "str", "artifact_name": "str", "completed_on": "str", "target_milestone_id": "str"},
        entity_ref_fields=("deployment_id", "target_milestone_id"),
        dedupe_core_fields=("deployment_id", "artifact_name", "completed_on"),
    ),
    "deployment.rollback.v1": _build_schema(
        "deployment.rollback.v1",
        required_fields=("deployment_id", "artifact_name"),
        optional_fields=("reason", "rolled_back_on"),
        field_types={"deployment_id": "str", "artifact_name": "str", "reason": "str", "rolled_back_on": "str"},
        entity_ref_fields=("deployment_id",),
        dedupe_core_fields=("deployment_id", "artifact_name", "rolled_back_on"),
    ),
    "deployment.started.v1": _build_schema(
        "deployment.started.v1",
        required_fields=("deployment_id", "artifact_name"),
        optional_fields=("started_on", "target_milestone_id"),
        field_types={"deployment_id": "str", "artifact_name": "str", "started_on": "str", "target_milestone_id": "str"},
        entity_ref_fields=("deployment_id", "target_milestone_id"),
        dedupe_core_fields=("deployment_id", "artifact_name", "started_on"),
    ),
    "risk.raised.v1": _build_schema(
        "risk.raised.v1",
        required_fields=("risk_id", "title", "severity"),
        optional_fields=("description", "likelihood", "owner_person_id", "workstream_id"),
        field_types={"risk_id": "str", "title": "str", "severity": "str", "description": "str", "likelihood": "str", "owner_person_id": "str", "workstream_id": "str"},
        entity_ref_fields=("risk_id", "owner_person_id", "workstream_id"),
        dedupe_core_fields=("risk_id", "title", "severity"),
    ),
    "risk.status_changed.v1": _build_schema(
        "risk.status_changed.v1",
        required_fields=("risk_id", "new_status"),
        optional_fields=("severity", "reason"),
        field_types={"risk_id": "str", "new_status": "str", "severity": "str", "reason": "str"},
        entity_ref_fields=("risk_id",),
        dedupe_core_fields=("risk_id", "new_status", "severity"),
    ),
    "risk.owner_changed.v1": _build_schema(
        "risk.owner_changed.v1",
        required_fields=("risk_id", "new_owner_person_id"),
        optional_fields=("prior_owner_person_id", "reason"),
        field_types={"risk_id": "str", "new_owner_person_id": "str", "prior_owner_person_id": "str", "reason": "str"},
        entity_ref_fields=("risk_id", "new_owner_person_id", "prior_owner_person_id"),
        dedupe_core_fields=("risk_id", "new_owner_person_id"),
    ),
    "risk.mitigated.v1": _build_schema(
        "risk.mitigated.v1",
        required_fields=("risk_id", "mitigation_summary"),
        optional_fields=("mitigated_by",),
        field_types={"risk_id": "str", "mitigation_summary": "str", "mitigated_by": "str"},
        entity_ref_fields=("risk_id", "mitigated_by"),
        dedupe_core_fields=("risk_id", "mitigated_by"),
    ),
    "risk.closed.v1": _build_schema(
        "risk.closed.v1",
        required_fields=("risk_id", "closure_reason"),
        field_types={"risk_id": "str", "closure_reason": "str"},
        entity_ref_fields=("risk_id",),
        dedupe_core_fields=("risk_id", "closure_reason"),
    ),
    "decision.made.v1": _build_schema(
        "decision.made.v1",
        required_fields=("decision_id", "title", "decision_text", "decided_by"),
        optional_fields=("forum", "alternatives_considered"),
        field_types={"decision_id": "str", "title": "str", "decision_text": "str", "decided_by": "list", "forum": "str", "alternatives_considered": "list"},
        entity_ref_fields=("decision_id",),
        dedupe_core_fields=("decision_id", "decision_text"),
    ),
    "decision.revised.v1": _build_schema(
        "decision.revised.v1",
        required_fields=("decision_id", "revision_text", "reason"),
        field_types={"decision_id": "str", "revision_text": "str", "reason": "str"},
        entity_ref_fields=("decision_id",),
        dedupe_core_fields=("decision_id", "revision_text"),
    ),
    "decision.superseded.v1": _build_schema(
        "decision.superseded.v1",
        required_fields=("decision_id", "supersedes_decision_id", "reason"),
        field_types={"decision_id": "str", "supersedes_decision_id": "str", "reason": "str"},
        entity_ref_fields=("decision_id", "supersedes_decision_id"),
        dedupe_core_fields=("decision_id", "supersedes_decision_id"),
    ),
    "assumption.stated.v1": _build_schema(
        "assumption.stated.v1",
        required_fields=("assumption_id", "statement"),
        optional_fields=("validation_plan",),
        field_types={"assumption_id": "str", "statement": "str", "validation_plan": "str"},
        entity_ref_fields=("assumption_id",),
        dedupe_core_fields=("assumption_id", "statement"),
    ),
    "assumption.validated.v1": _build_schema(
        "assumption.validated.v1",
        required_fields=("assumption_id", "evidence"),
        field_types={"assumption_id": "str", "evidence": "str"},
        entity_ref_fields=("assumption_id",),
        dedupe_core_fields=("assumption_id",),
    ),
    "assumption.invalidated.v1": _build_schema(
        "assumption.invalidated.v1",
        required_fields=("assumption_id", "evidence"),
        optional_fields=("impact",),
        field_types={"assumption_id": "str", "evidence": "str", "impact": "str"},
        entity_ref_fields=("assumption_id",),
        dedupe_core_fields=("assumption_id",),
    ),
    "dependency.declared.v1": _build_schema(
        "dependency.declared.v1",
        required_fields=("dependency_id", "from_entity", "to_entity"),
        optional_fields=("description", "needed_by"),
        field_types={"dependency_id": "str", "from_entity": "str", "to_entity": "str", "description": "str", "needed_by": "str"},
        entity_ref_fields=("dependency_id", "from_entity", "to_entity"),
        dedupe_core_fields=("dependency_id", "from_entity", "to_entity", "needed_by"),
    ),
    "dependency.status_changed.v1": _build_schema(
        "dependency.status_changed.v1",
        required_fields=("dependency_id", "new_status"),
        optional_fields=("reason",),
        field_types={"dependency_id": "str", "new_status": "str", "reason": "str"},
        entity_ref_fields=("dependency_id",),
        dedupe_core_fields=("dependency_id", "new_status"),
    ),
    "commitment.made.v1": _build_schema(
        "commitment.made.v1",
        required_fields=("commitment_id", "text", "owner_person_id", "due_date"),
        optional_fields=("made_in",),
        field_types={"commitment_id": "str", "text": "str", "owner_person_id": "str", "due_date": "str", "made_in": "str"},
        entity_ref_fields=("commitment_id", "owner_person_id"),
        dedupe_core_fields=("commitment_id", "text", "owner_person_id", "due_date"),
    ),
    "commitment.slipped.v1": _build_schema(
        "commitment.slipped.v1",
        required_fields=("commitment_id",),
        optional_fields=("new_due_date", "reason"),
        field_types={"commitment_id": "str", "new_due_date": "str", "reason": "str"},
        entity_ref_fields=("commitment_id",),
        dedupe_core_fields=("commitment_id", "new_due_date"),
    ),
    "commitment.fulfilled.v1": _build_schema(
        "commitment.fulfilled.v1",
        required_fields=("commitment_id", "fulfilled_on"),
        optional_fields=("evidence",),
        field_types={"commitment_id": "str", "fulfilled_on": "str", "evidence": "str"},
        entity_ref_fields=("commitment_id",),
        dedupe_core_fields=("commitment_id", "fulfilled_on"),
    ),
    "kpi.defined.v1": _build_schema(
        "kpi.defined.v1",
        required_fields=("kpi_id", "name"),
        optional_fields=("definition", "unit", "owner_person_id", "thresholds"),
        field_types={"kpi_id": "str", "name": "str", "definition": "str", "unit": "str", "owner_person_id": "str", "thresholds": "dict"},
        entity_ref_fields=("kpi_id", "owner_person_id"),
        dedupe_core_fields=("kpi_id", "name", "unit", "thresholds"),
    ),
    "kpi.decommissioned.v1": _build_schema(
        "kpi.decommissioned.v1",
        required_fields=("kpi_id",),
        optional_fields=("reason",),
        field_types={"kpi_id": "str", "reason": "str"},
        entity_ref_fields=("kpi_id",),
        dedupe_core_fields=("kpi_id",),
    ),
    "metric.observed.v1": _build_schema(
        "metric.observed.v1",
        required_fields=("kpi_id", "value"),
        optional_fields=("unit", "window_start", "window_end", "dimensions"),
        field_types={"kpi_id": "str", "value": "float", "unit": "str", "window_start": "str", "window_end": "str", "dimensions": "dict"},
        entity_ref_fields=("kpi_id",),
        dedupe_core_fields=("kpi_id", "value", "unit", "window_end", "dimensions"),
    ),
    "incident.opened.v1": _build_schema(
        "incident.opened.v1",
        required_fields=("incident_id", "severity", "title"),
        optional_fields=("impacted_entities",),
        field_types={"incident_id": "str", "severity": "str", "title": "str", "impacted_entities": "list"},
        entity_ref_fields=("incident_id", "impacted_entities"),
        dedupe_core_fields=("incident_id", "severity", "title"),
    ),
    "incident.resolved.v1": _build_schema(
        "incident.resolved.v1",
        required_fields=("incident_id", "resolved_on"),
        optional_fields=("mttr_minutes", "root_cause"),
        field_types={"incident_id": "str", "resolved_on": "str", "mttr_minutes": "int", "root_cause": "str"},
        entity_ref_fields=("incident_id",),
        dedupe_core_fields=("incident_id", "resolved_on", "mttr_minutes"),
    ),
    "incident.severity_changed.v1": _build_schema(
        # v2.22, ADR-0006 R2: faithful type for the REV incident.severity_changed
        # claim. Previously mis-mapped to incident.opened.v1 (a severity change is
        # not an incident opening). Detected-but-not-v1-authoritative per S-0g.
        "incident.severity_changed.v1",
        required_fields=("incident_id", "new_severity"),
        optional_fields=("prior_severity", "reason"),
        field_types={"incident_id": "str", "new_severity": "str", "prior_severity": "str", "reason": "str"},
        entity_ref_fields=("incident_id",),
        dedupe_core_fields=("incident_id", "new_severity", "prior_severity"),
    ),
    "kpi.threshold_crossed.v1": _build_schema(
        "kpi.threshold_crossed.v1",
        required_fields=("kpi_id", "threshold", "direction", "observed_value"),
        field_types={"kpi_id": "str", "threshold": "str", "direction": "str", "observed_value": "float"},
        entity_ref_fields=("kpi_id",),
        dedupe_core_fields=("kpi_id", "threshold", "direction", "observed_value"),
    ),
    "knowledge.article_added.v1": _build_schema(
        "knowledge.article_added.v1",
        required_fields=("article_id", "title", "location"),
        optional_fields=("topics",),
        field_types={"article_id": "str", "title": "str", "location": "str", "topics": "list"},
        entity_ref_fields=("article_id",),
        dedupe_core_fields=("article_id", "title", "location"),
    ),
    "knowledge.article_revised.v1": _build_schema(
        "knowledge.article_revised.v1",
        required_fields=("article_id", "revision_summary", "location"),
        field_types={"article_id": "str", "revision_summary": "str", "location": "str"},
        entity_ref_fields=("article_id",),
        dedupe_core_fields=("article_id", "location"),
    ),
    "knowledge.article_removed.v1": _build_schema(
        "knowledge.article_removed.v1",
        required_fields=("article_id",),
        optional_fields=("reason",),
        field_types={"article_id": "str", "reason": "str"},
        entity_ref_fields=("article_id",),
        dedupe_core_fields=("article_id",),
    ),
    "playbook.created.v1": _build_schema(
        "playbook.created.v1",
        required_fields=("playbook_id", "title", "location"),
        optional_fields=("trigger_conditions",),
        field_types={"playbook_id": "str", "title": "str", "location": "str", "trigger_conditions": "list"},
        entity_ref_fields=("playbook_id",),
        dedupe_core_fields=("playbook_id", "title", "location"),
    ),
    "artifact.published.v1": _build_schema(
        "artifact.published.v1",
        required_fields=("artifact_id", "artifact_kind", "title", "location"),
        optional_fields=("period_start", "period_end"),
        field_types={"artifact_id": "str", "artifact_kind": "str", "title": "str", "location": "str", "period_start": "str", "period_end": "str"},
        entity_ref_fields=("artifact_id",),
        dedupe_core_fields=("artifact_id", "artifact_kind", "title", "period_start", "period_end"),
    ),
    "operator.correction.v1": _build_schema(
        "operator.correction.v1",
        required_fields=("corrects_event_id", "corrected_payload", "reason"),
        field_types={"corrects_event_id": "str", "corrected_payload": "dict_or_none", "reason": "str"},
        entity_ref_fields=(),
        dedupe_core_fields=("corrects_event_id",),
        is_control=True,
    ),
    "operator.field_lock.v1": _build_schema(
        "operator.field_lock.v1",
        required_fields=("entity_id", "field"),
        optional_fields=("locked_value", "valid_until", "reason", "override_session_id"),
        field_types={"entity_id": "str", "field": "str", "valid_until": "str", "reason": "str", "override_session_id": "str"},
        entity_ref_fields=("entity_id",),
        dedupe_core_fields=("entity_id", "field", "locked_value"),
        is_control=True,
    ),
    "operator.field_unlock.v1": _build_schema(
        "operator.field_unlock.v1",
        required_fields=("entity_id", "field"),
        optional_fields=("reason", "override_session_id"),
        field_types={"entity_id": "str", "field": "str", "reason": "str", "override_session_id": "str"},
        entity_ref_fields=("entity_id",),
        dedupe_core_fields=("entity_id", "field"),
        is_control=True,
    ),
    "operator.baseline_hardlock.v1": _build_schema(
        "operator.baseline_hardlock.v1",
        required_fields=("issue_number", "snapshot_hash", "event_id_watermark", "contributing_event_count"),
        field_types={"issue_number": "int", "snapshot_hash": "str", "event_id_watermark": "str", "contributing_event_count": "int"},
        entity_ref_fields=(),
        dedupe_core_fields=("issue_number", "snapshot_hash"),
    ),
    "pipeline.gap_detected.v1": _build_schema(
        "pipeline.gap_detected.v1",
        required_fields=("pipeline", "gap_kind", "detail"),
        optional_fields=("window_start", "window_end"),
        field_types={"pipeline": "str", "gap_kind": "str", "detail": "str", "window_start": "str", "window_end": "str"},
        affects_support_tables=("gaps",),
        dedupe_core_fields=("pipeline", "gap_kind", "window_start", "window_end"),
        support_table_updater={
            "gaps": lambda event: {
                "event_id": event.event_id,
                "pipeline": event.payload["pipeline"],
                "gap_kind": event.payload["gap_kind"],
                "window_start": event.payload.get("window_start"),
                "window_end": event.payload.get("window_end"),
                "detail": event.payload["detail"],
                "acknowledged": 0,
            }
        },
    ),
    "discovery.candidate_proposed.v1": _build_schema(
        "discovery.candidate_proposed.v1",
        required_fields=("batch_id", "pipeline", "candidate_count"),
        optional_fields=("event_type_histogram",),
        field_types={"batch_id": "str", "pipeline": "str", "candidate_count": "int", "event_type_histogram": "dict"},
        affects_support_tables=(),
        dedupe_core_fields=("batch_id",),
    ),
    "discovery.candidate_approved.v1": _build_schema(
        "discovery.candidate_approved.v1",
        required_fields=("candidate_id", "resulting_event_id", "triage_actor", "edited"),
        field_types={"candidate_id": "str", "resulting_event_id": "str", "triage_actor": "str", "edited": "bool"},
        entity_ref_fields=(),
        affects_support_tables=(),
        dedupe_core_fields=("candidate_id", "resulting_event_id", "edited"),
    ),
    "discovery.candidate_rejected.v1": _build_schema(
        "discovery.candidate_rejected.v1",
        required_fields=("candidate_id", "triage_actor"),
        optional_fields=("reason",),
        field_types={"candidate_id": "str", "triage_actor": "str", "reason": "str"},
        entity_ref_fields=(),
        affects_support_tables=(),
        dedupe_core_fields=("candidate_id",),
    ),
    "discovery.candidate_revoked.v1": _build_schema(
        "discovery.candidate_revoked.v1",
        required_fields=("candidate_id", "resulting_event_id", "revocation_event_id", "triage_actor"),
        optional_fields=("reason", "approval_event_id"),
        field_types={
            "candidate_id": "str",
            "resulting_event_id": "str",
            "revocation_event_id": "str",
            "triage_actor": "str",
            "reason": "str",
            "approval_event_id": "str",
        },
        entity_ref_fields=(),
        affects_support_tables=(),
        dedupe_core_fields=("candidate_id", "resulting_event_id", "revocation_event_id"),
    ),
    # ── ADF-W0.18: specs/arch-data-fix.md Appendix A.2 event payload contracts ──
    # Registered here (schema validation, Zone A) so build_event_envelope/
    # write_event stop raising "Unknown ledger event type" for these sixteen
    # types. None of these are TPM business facts (risk/decision/milestone/...);
    # they are measurement, AI-lifecycle, and actuation-audit events. Their
    # fact-bridge disposition (event_type_registry.py) is PASSTHROUGH for all
    # sixteen -- see the ADF-W0.18 block there, including the explicit
    # decision.outcome_recorded. prefix override needed to avoid colliding with
    # the pre-existing "decision." TPM fact-bridge family.
    "value.workflow_started.v1": _build_schema(
        "value.workflow_started.v1",
        required_fields=("measurement_id", "edition_id", "workflow", "mode", "actor", "started_at"),
        field_types={
            "measurement_id": "str", "edition_id": "str", "workflow": "str",
            "mode": "str", "actor": "str", "started_at": "str",
        },
        entity_ref_fields=("measurement_id", "edition_id"),
        dedupe_core_fields=("measurement_id",),
    ),
    "value.workflow_completed.v1": _build_schema(
        "value.workflow_completed.v1",
        required_fields=(
            "measurement_id", "edition_id", "workflow", "mode", "actor", "completed_at",
            "active_seconds", "machine_wait_seconds", "external_wait_seconds",
            "review_seconds", "manual_acquisition_seconds",
        ),
        field_types={
            "measurement_id": "str", "edition_id": "str", "workflow": "str", "mode": "str",
            "actor": "str", "completed_at": "str", "active_seconds": "float",
            "machine_wait_seconds": "float", "external_wait_seconds": "float",
            "review_seconds": "float", "manual_acquisition_seconds": "float",
        },
        entity_ref_fields=("measurement_id", "edition_id"),
        dedupe_core_fields=("measurement_id",),
    ),
    "value.manual_step_attested.v1": _build_schema(
        "value.manual_step_attested.v1",
        required_fields=("measurement_id", "step", "seconds", "attested_by", "attested_at"),
        field_types={
            "measurement_id": "str", "step": "str", "seconds": "float",
            "attested_by": "str", "attested_at": "str",
        },
        entity_ref_fields=("measurement_id", "attested_by"),
        dedupe_core_fields=("measurement_id", "step"),
    ),
    "value.review_edit_recorded.v1": _build_schema(
        "value.review_edit_recorded.v1",
        required_fields=(
            "proposal_class", "proposal_id", "outcome", "review_seconds", "edit_magnitude",
            "reviewer", "artifact_ref",
        ),
        field_types={
            "proposal_class": "str", "proposal_id": "str", "outcome": "str",
            "review_seconds": "float", "edit_magnitude": "float", "reviewer": "str",
            "artifact_ref": "str",
        },
        entity_ref_fields=("proposal_id", "reviewer", "artifact_ref"),
        dedupe_core_fields=("proposal_id", "outcome"),
    ),
    "value.gap_closed.v1": _build_schema(
        "value.gap_closed.v1",
        required_fields=("gap_id", "closed_by", "evidence_refs", "closed_at"),
        field_types={"gap_id": "str", "closed_by": "str", "evidence_refs": "list", "closed_at": "str"},
        entity_ref_fields=("gap_id",),
        dedupe_core_fields=("gap_id",),
    ),
    "quality.confirmed_defect_prevented.v1": _build_schema(
        "quality.confirmed_defect_prevented.v1",
        required_fields=("gate_id", "artifact_ref", "defect_summary", "confirmed_by", "evidence_refs"),
        field_types={
            "gate_id": "str", "artifact_ref": "str", "defect_summary": "str",
            "confirmed_by": "str", "evidence_refs": "list",
        },
        entity_ref_fields=("gate_id", "artifact_ref", "confirmed_by"),
        dedupe_core_fields=("gate_id", "artifact_ref"),
    ),
    "source.acquisition_completed.v1": _build_schema(
        "source.acquisition_completed.v1",
        required_fields=(
            "acquisition_id", "channel", "run_id", "completeness", "watermark_before",
            "watermark_after", "provider_summary",
        ),
        optional_fields=("prefetch_snapshot_ref",),
        field_types={
            "acquisition_id": "str", "channel": "str", "run_id": "str", "completeness": "str",
            "watermark_before": "str", "watermark_after": "str", "provider_summary": "dict",
            "prefetch_snapshot_ref": "str",
        },
        entity_ref_fields=("acquisition_id", "run_id", "prefetch_snapshot_ref"),
        dedupe_core_fields=("acquisition_id", "channel", "run_id"),
    ),
    "operation.trace_linked.v1": _build_schema(
        "operation.trace_linked.v1",
        required_fields=("correlation_id", "workflow_id", "run_id", "stage", "ref_type", "ref_id"),
        optional_fields=("parent_event_id",),
        field_types={
            "correlation_id": "str", "workflow_id": "str", "run_id": "str", "stage": "str",
            "ref_type": "str", "ref_id": "str", "parent_event_id": "str",
        },
        entity_ref_fields=("correlation_id", "workflow_id", "run_id", "ref_id", "parent_event_id"),
        dedupe_core_fields=("correlation_id", "ref_type", "ref_id"),
    ),
    "decision.outcome_recorded.v1": _build_schema(
        "decision.outcome_recorded.v1",
        required_fields=("decision_id", "outcome", "recorded_by", "evidence_refs"),
        field_types={
            "decision_id": "str", "outcome": "str", "recorded_by": "str", "evidence_refs": "list",
        },
        entity_ref_fields=("decision_id", "recorded_by"),
        dedupe_core_fields=("decision_id", "outcome"),
    ),
    "action.closed.v1": _build_schema(
        "action.closed.v1",
        required_fields=("action_id", "closed_state", "closed_by", "evidence_refs"),
        optional_fields=("work_item_id",),
        field_types={
            "action_id": "str", "closed_state": "str", "closed_by": "str",
            "work_item_id": "str", "evidence_refs": "list",
        },
        entity_ref_fields=("action_id", "closed_by", "work_item_id"),
        dedupe_core_fields=("action_id", "closed_state"),
    ),
    "ai.run_lifecycle.v1": _build_schema(
        "ai.run_lifecycle.v1",
        required_fields=(
            "ai_run_id", "feature", "state", "prompt_version", "policy_version",
            "model_deployment", "context_manifest_ref",
        ),
        field_types={
            "ai_run_id": "str", "feature": "str", "state": "str", "prompt_version": "str",
            "policy_version": "str", "model_deployment": "str", "context_manifest_ref": "str",
        },
        entity_ref_fields=("ai_run_id", "context_manifest_ref"),
        dedupe_core_fields=("ai_run_id", "state"),
    ),
    "ai.release_decision.v1": _build_schema(
        "ai.release_decision.v1",
        required_fields=("ai_run_id", "terminal", "reason", "validator_finding_count"),
        optional_fields=("released_content_hash",),
        field_types={
            "ai_run_id": "str", "terminal": "str", "reason": "str",
            "validator_finding_count": "int", "released_content_hash": "str",
        },
        entity_ref_fields=("ai_run_id",),
        dedupe_core_fields=("ai_run_id", "terminal"),
    ),
    "ai.application_receipt.v1": _build_schema(
        "ai.application_receipt.v1",
        required_fields=("ai_run_id", "receipt"),
        optional_fields=("artifact_ref", "proposal_id"),
        field_types={
            "ai_run_id": "str", "receipt": "str", "artifact_ref": "str", "proposal_id": "str",
        },
        entity_ref_fields=("ai_run_id", "artifact_ref", "proposal_id"),
        dedupe_core_fields=("ai_run_id", "receipt"),
    ),
    "actuation.intent_created.v1": _build_schema(
        "actuation.intent_created.v1",
        required_fields=(
            "operation_intent_id", "idempotency_key", "operation_type", "target_identity",
            "proposal_id", "approval_event_ref",
        ),
        field_types={
            "operation_intent_id": "str", "idempotency_key": "str", "operation_type": "str",
            "target_identity": "str", "proposal_id": "str", "approval_event_ref": "str",
        },
        entity_ref_fields=("operation_intent_id", "proposal_id", "approval_event_ref"),
        dedupe_core_fields=("operation_intent_id", "idempotency_key"),
    ),
    "actuation.receipt_recorded.v1": _build_schema(
        "actuation.receipt_recorded.v1",
        required_fields=("operation_intent_id", "receipt_state", "provider_summary"),
        optional_fields=("remote_id", "remote_rev"),
        field_types={
            "operation_intent_id": "str", "receipt_state": "str", "remote_id": "str",
            "remote_rev": "str", "provider_summary": "dict",
        },
        entity_ref_fields=("operation_intent_id", "remote_id"),
        dedupe_core_fields=("operation_intent_id", "receipt_state"),
    ),
    "actuation.duplicate_prevented.v1": _build_schema(
        "actuation.duplicate_prevented.v1",
        required_fields=("operation_intent_id", "detection", "evidence"),
        optional_fields=("existing_remote_id",),
        field_types={
            "operation_intent_id": "str", "detection": "str", "evidence": "str",
            "existing_remote_id": "str",
        },
        entity_ref_fields=("operation_intent_id", "existing_remote_id"),
        dedupe_core_fields=("operation_intent_id", "detection"),
    ),
}


EVENT_TYPE_REGISTRY: dict[str, EventPayloadSchema] = dict(_REAL_EVENT_SCHEMAS)


def get_event_schema(event_type: str) -> EventPayloadSchema:
    try:
        return EVENT_TYPE_REGISTRY[event_type]
    except KeyError as error:
        raise ValueError(f"Unknown ledger event type: {event_type}") from error


def validate_event_payload(event_type: str, payload: dict[str, Any]) -> None:
    get_event_schema(event_type).validate(payload)


def get_registered_event_types() -> frozenset[str]:
    return frozenset(EVENT_TYPE_REGISTRY.keys())


def is_known_event_type(event_type: str) -> bool:
    return event_type in EVENT_TYPE_REGISTRY


def count_control_event_types() -> tuple[int, int]:
    control = sum(1 for registration in EVENT_TYPE_REGISTRY.values() if registration.is_control)
    return len(EVENT_TYPE_REGISTRY) - control, control


def support_table_update(event: Any) -> dict[str, dict[str, Any]]:
    schema = get_event_schema(event.event_type)
    updates: dict[str, dict[str, Any]] = {}
    for table_name, updater in schema.support_table_updater.items():
        payload = updater(event)
        if payload is not None:
            updates[table_name] = payload
    return updates

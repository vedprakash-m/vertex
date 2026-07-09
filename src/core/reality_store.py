from __future__ import annotations

from typing import Any, Literal, cast
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3

from src.core.digest_cache import compute_digest_sha256, serialize_digest_model
from src.core.hypothesis_models import (
    AssertionOperator,
    AssertionEvaluation,
    ChallengeKind,
    ChallengeSeverity,
    ChallengeState,
    CompositeAssertion,
    CompositeAssertionOperator,
    DigestDelta,
    Hypothesis,
    HypothesisAnnotation,
    HypothesisKind,
    HypothesisStatus,
    RealityChallenge,
    RealityDigestModel,
    SuppressionSummary,
    TelemetryAssertion,
)
from src.core.metric_models import MetricObservation, MetricQualityState, MetricSourceBinding, ObservationWindow
from src.core.source_models import IngestionRun, MaintenanceWindow, MetricBindingHealth, SourceKind, SourceRef


_DB_FILENAME = "vertex.sqlite3"

log = logging.getLogger(__name__)


def get_program_reality_db_path(
    program_id: str,
    *,
    home_root: Path | None = None,
    db_root: Path | None = None,
) -> Path:
    base_root = db_root or _resolve_reality_db_root(home_root=home_root)
    if base_root.suffix.lower() == ".sqlite3":
        return base_root
    return base_root / program_id / _DB_FILENAME


def _resolve_reality_db_root(*, home_root: Path | None = None) -> Path:
    configured_root = os.environ.get("VERTEX_DB_PATH")
    if configured_root:
        return Path(configured_root)
    # PS-14 / Track K root-cause fix (fix-data-flow.md §6.11): this fallback
    # previously chose ``~/.vertex`` with zero logging whenever a caller
    # threaded neither ``db_root`` nor ``VERTEX_DB_PATH`` — the exact
    # mechanism that produced a silent split-brain fact-store database for
    # `xpf` (a stray home-directory DB, invisible to any caller that always
    # supplies `programs_root`/`db_root` explicitly, per production code's
    # real path). Any future script, test, or refactor that omits both now
    # gets a loud, unmissable signal instead of silently reading/writing the
    # wrong database.
    resolved = (home_root or Path.home()) / ".vertex"
    log.critical(
        "reality_store: no db_root/VERTEX_DB_PATH supplied — falling back to "
        "%s. This is very likely NOT the canonical fact-store location production "
        "code uses (which threads an explicit programs_root/db_root). See "
        "fix-data-flow.md PS-14/Track K — run `vertex doctor --fact-bridge` or "
        "check for multiple vertex.sqlite3 files for this program before trusting "
        "reads/writes through this path.",
        resolved,
    )
    return resolved


class RealityStore:
    def __init__(
        self,
        program_id: str,
        *,
        home_root: Path | None = None,
        db_root: Path | None = None,
    ) -> None:
        self._program_id = program_id.strip()
        self._db_path = get_program_reality_db_path(self._program_id, home_root=home_root, db_root=db_root)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def program_id(self) -> str:
        return self._program_id

    def initialize(self) -> Path:
        with _connect_reality_db(self._db_path):
            pass
        return self._db_path

    def upsert_metric_source_binding(self, binding: MetricSourceBinding) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO reality_metric_source_bindings (
                    binding_id, metric_id, program_id, source_kind, priority, cluster, database_name,
                    kql_template, result_column, dimension_defaults_json, validated, last_validated_at,
                    last_validated_kql_hash, owner_alias, owner_entity_ref, binding_version, kql_template_hash,
                    evidence_url_template, valid_from, valid_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(binding_id) DO UPDATE SET
                    metric_id = excluded.metric_id,
                    program_id = excluded.program_id,
                    source_kind = excluded.source_kind,
                    priority = excluded.priority,
                    cluster = excluded.cluster,
                    database_name = excluded.database_name,
                    kql_template = excluded.kql_template,
                    result_column = excluded.result_column,
                    dimension_defaults_json = excluded.dimension_defaults_json,
                    validated = excluded.validated,
                    last_validated_at = excluded.last_validated_at,
                    last_validated_kql_hash = excluded.last_validated_kql_hash,
                    owner_alias = excluded.owner_alias,
                    owner_entity_ref = excluded.owner_entity_ref,
                    binding_version = excluded.binding_version,
                    kql_template_hash = excluded.kql_template_hash,
                    evidence_url_template = excluded.evidence_url_template,
                    valid_from = excluded.valid_from,
                    valid_until = excluded.valid_until
                """,
                (
                    binding.binding_id,
                    binding.metric_id,
                    binding.program_id,
                    binding.source_kind,
                    binding.priority,
                    binding.cluster,
                    binding.database,
                    binding.kql_template,
                    binding.result_column,
                    _encode_json(binding.dimension_defaults),
                    int(binding.validated),
                    _encode_datetime(binding.last_validated_at),
                    binding.last_validated_kql_hash,
                    binding.owner_alias,
                    binding.owner_entity_ref,
                    binding.binding_version,
                    binding.kql_template_hash,
                    binding.evidence_url_template,
                    _encode_datetime(binding.valid_from),
                    _encode_datetime(binding.valid_until),
                ),
            )

    def get_metric_source_binding(self, binding_id: str) -> MetricSourceBinding | None:
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(
                "SELECT * FROM reality_metric_source_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        return _metric_source_binding_from_row(row)

    def list_active_metric_source_bindings(self, *, metric_id: str | None = None) -> tuple[MetricSourceBinding, ...]:
        with _connect_reality_db(self._db_path) as connection:
            if metric_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM reality_metric_source_bindings
                    WHERE program_id = ? AND valid_until IS NULL
                    ORDER BY metric_id ASC, priority ASC, binding_id ASC
                    """,
                    (self._program_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM reality_metric_source_bindings
                    WHERE program_id = ? AND metric_id = ? AND valid_until IS NULL
                    ORDER BY priority ASC, binding_id ASC
                    """,
                    (self._program_id, metric_id),
                ).fetchall()
        return tuple(_metric_source_binding_from_row(row) for row in rows)

    def upsert_telemetry_assertion(self, assertion: "TelemetryAssertion") -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO reality_telemetry_assertions (
                    id, program_id, metric_id, window_days, window_aggregation, window_dimensions_json,
                    window_kind, anchor_event_ref_json, minimum_observations, operator, threshold,
                    threshold_upper,
                    tolerance_rel, tolerance_abs, baseline_value, baseline_captured_at,
                    sustain_min_observations, cooldown_hours, severity_override, description,
                    linked_hypothesis_id, linked_claim_id, linked_assumption_id, re_evaluate_by,
                    policy_version, valid_from, valid_until, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    program_id = excluded.program_id,
                    metric_id = excluded.metric_id,
                    window_days = excluded.window_days,
                    window_aggregation = excluded.window_aggregation,
                    window_dimensions_json = excluded.window_dimensions_json,
                    window_kind = excluded.window_kind,
                    anchor_event_ref_json = excluded.anchor_event_ref_json,
                    minimum_observations = excluded.minimum_observations,
                    operator = excluded.operator,
                    threshold = excluded.threshold,
                    threshold_upper = excluded.threshold_upper,
                    tolerance_rel = excluded.tolerance_rel,
                    tolerance_abs = excluded.tolerance_abs,
                    baseline_value = excluded.baseline_value,
                    baseline_captured_at = excluded.baseline_captured_at,
                    sustain_min_observations = excluded.sustain_min_observations,
                    cooldown_hours = excluded.cooldown_hours,
                    severity_override = excluded.severity_override,
                    description = excluded.description,
                    linked_hypothesis_id = excluded.linked_hypothesis_id,
                    linked_claim_id = excluded.linked_claim_id,
                    linked_assumption_id = excluded.linked_assumption_id,
                    re_evaluate_by = excluded.re_evaluate_by,
                    policy_version = excluded.policy_version,
                    valid_from = excluded.valid_from,
                    valid_until = excluded.valid_until,
                    created_by = excluded.created_by
                """,
                (
                    assertion.id,
                    assertion.program_id,
                    assertion.metric_id,
                    assertion.window.days,
                    assertion.window.aggregation.value,
                    _encode_json(assertion.window.dimensions),
                    assertion.window.window_kind,
                    _encode_source_ref(assertion.window.anchor_event_ref),
                    assertion.window.minimum_observations,
                    assertion.operator.value,
                    assertion.threshold,
                    assertion.threshold_upper,
                    assertion.tolerance_rel,
                    assertion.tolerance_abs,
                    assertion.baseline_value,
                    _encode_datetime(assertion.baseline_captured_at),
                    assertion.sustain_min_observations,
                    assertion.cooldown_hours,
                    assertion.severity_override,
                    assertion.description,
                    assertion.linked_hypothesis_id,
                    assertion.linked_claim_id,
                    assertion.linked_assumption_id,
                    _encode_date(assertion.re_evaluate_by),
                    assertion.policy_version,
                    _encode_datetime(assertion.valid_from),
                    _encode_datetime(assertion.valid_until),
                    assertion.created_by,
                ),
            )

    def upsert_composite_assertion(self, assertion: CompositeAssertion) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO reality_composite_assertions (
                    id, program_id, operator, child_assertion_ids_json, description,
                    linked_hypothesis_id, policy_version, valid_from, valid_until, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    program_id = excluded.program_id,
                    operator = excluded.operator,
                    child_assertion_ids_json = excluded.child_assertion_ids_json,
                    description = excluded.description,
                    linked_hypothesis_id = excluded.linked_hypothesis_id,
                    policy_version = excluded.policy_version,
                    valid_from = excluded.valid_from,
                    valid_until = excluded.valid_until,
                    created_by = excluded.created_by
                """,
                (
                    assertion.id,
                    assertion.program_id,
                    assertion.operator.value,
                    _encode_json(assertion.child_assertion_ids),
                    assertion.description,
                    assertion.linked_hypothesis_id,
                    assertion.policy_version,
                    _encode_datetime(assertion.valid_from),
                    _encode_datetime(assertion.valid_until),
                    assertion.created_by,
                ),
            )

    def get_composite_assertion(self, assertion_id: str) -> CompositeAssertion | None:
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(
                "SELECT * FROM reality_composite_assertions WHERE id = ?",
                (assertion_id,),
            ).fetchone()
        if row is None:
            return None
        return _composite_assertion_from_row(row)

    def list_active_composite_assertions(self) -> tuple[CompositeAssertion, ...]:
        return self.list_composite_assertions()

    def list_composite_assertions(self, *, include_archived: bool = False) -> tuple[CompositeAssertion, ...]:
        clauses = ["program_id = ?"]
        parameters: list[object] = [self._program_id]
        if not include_archived:
            clauses.append("valid_until IS NULL")
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM reality_composite_assertions
                WHERE {' AND '.join(clauses)}
                ORDER BY (CASE WHEN valid_until IS NULL THEN 1 ELSE 0 END) DESC, policy_version DESC, valid_from DESC, id ASC
                """,
                tuple(parameters),
            ).fetchall()
        return tuple(_composite_assertion_from_row(row) for row in rows)

    def get_telemetry_assertion(self, assertion_id: str) -> TelemetryAssertion | None:
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(
                "SELECT * FROM reality_telemetry_assertions WHERE id = ?",
                (assertion_id,),
            ).fetchone()
        if row is None:
            return None
        return _telemetry_assertion_from_row(row)

    def list_active_telemetry_assertions(self) -> tuple[TelemetryAssertion, ...]:
        return self.list_telemetry_assertions()

    def list_telemetry_assertions(
        self,
        *,
        metric_id: str | None = None,
        include_archived: bool = False,
    ) -> tuple[TelemetryAssertion, ...]:
        clauses = ["program_id = ?"]
        parameters: list[object] = [self._program_id]
        if metric_id is not None:
            clauses.append("metric_id = ?")
            parameters.append(metric_id)
        if not include_archived:
            clauses.append("valid_until IS NULL")
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM reality_telemetry_assertions
                WHERE {' AND '.join(clauses)}
                ORDER BY metric_id ASC, (CASE WHEN valid_until IS NULL THEN 1 ELSE 0 END) DESC, policy_version DESC, valid_from DESC, id ASC
                """,
                tuple(parameters),
            ).fetchall()
        return tuple(_telemetry_assertion_from_row(row) for row in rows)

    def upsert_hypothesis(self, hypothesis: Hypothesis) -> None:
        expected_value_num, expected_value_date, expected_value_text = _split_expected_value(
            hypothesis.kind,
            hypothesis.expected_value,
        )
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO reality_hypotheses (
                    id, short_id, program_id, kind, statement, expected_value_num, expected_value_date,
                    expected_value_text, expected_value_frozen_at, as_of_date, telemetry_assertion_id, composite_assertion_id,
                    workstream_id, sensitivity_label, proposed_by, proposed_at, status, depends_on_json,
                    confirmed_by, confirmed_at, review_due, superseded_by, supersedes_id,
                    rejection_reason, linked_claim_id, linked_assumption_id, linked_ado_item_id,
                    linked_doc_section_id, source_refs_json, expires_at, policy_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    short_id = excluded.short_id,
                    program_id = excluded.program_id,
                    kind = excluded.kind,
                    statement = excluded.statement,
                    expected_value_num = excluded.expected_value_num,
                    expected_value_date = excluded.expected_value_date,
                    expected_value_text = excluded.expected_value_text,
                    expected_value_frozen_at = excluded.expected_value_frozen_at,
                    as_of_date = excluded.as_of_date,
                    telemetry_assertion_id = excluded.telemetry_assertion_id,
                    composite_assertion_id = excluded.composite_assertion_id,
                    workstream_id = excluded.workstream_id,
                    sensitivity_label = excluded.sensitivity_label,
                    proposed_by = excluded.proposed_by,
                    proposed_at = excluded.proposed_at,
                    status = excluded.status,
                    depends_on_json = excluded.depends_on_json,
                    confirmed_by = excluded.confirmed_by,
                    confirmed_at = excluded.confirmed_at,
                    review_due = excluded.review_due,
                    superseded_by = excluded.superseded_by,
                    supersedes_id = excluded.supersedes_id,
                    rejection_reason = excluded.rejection_reason,
                    linked_claim_id = excluded.linked_claim_id,
                    linked_assumption_id = excluded.linked_assumption_id,
                    linked_ado_item_id = excluded.linked_ado_item_id,
                    linked_doc_section_id = excluded.linked_doc_section_id,
                    source_refs_json = excluded.source_refs_json,
                    expires_at = excluded.expires_at,
                    policy_version = excluded.policy_version
                """,
                (
                    hypothesis.id,
                    hypothesis.short_id,
                    hypothesis.program_id,
                    hypothesis.kind.value,
                    hypothesis.statement,
                    expected_value_num,
                    expected_value_date,
                    expected_value_text,
                    _encode_datetime(hypothesis.expected_value_frozen_at),
                    hypothesis.as_of_date.isoformat(),
                    hypothesis.telemetry_assertion_id,
                    hypothesis.composite_assertion_id,
                    hypothesis.workstream_id,
                    hypothesis.sensitivity_label,
                    hypothesis.proposed_by,
                    _encode_datetime(hypothesis.proposed_at),
                    hypothesis.status.value,
                    _encode_json(hypothesis.depends_on),
                    hypothesis.confirmed_by,
                    _encode_datetime(hypothesis.confirmed_at),
                    _encode_date(hypothesis.review_due),
                    hypothesis.superseded_by,
                    hypothesis.supersedes_id,
                    hypothesis.rejection_reason,
                    hypothesis.linked_claim_id,
                    hypothesis.linked_assumption_id,
                    hypothesis.linked_ado_item_id,
                    hypothesis.linked_doc_section_id,
                    _encode_source_refs(hypothesis.source_refs),
                    _encode_datetime(hypothesis.expires_at),
                    hypothesis.policy_version,
                ),
            )

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(
                "SELECT * FROM reality_hypotheses WHERE id = ?",
                (hypothesis_id,),
            ).fetchone()
        if row is None:
            return None
        return _hypothesis_from_row(row)

    def get_hypothesis_by_short_id(self, short_id: str) -> Hypothesis | None:
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(
                "SELECT * FROM reality_hypotheses WHERE program_id = ? AND short_id = ?",
                (self._program_id, short_id),
            ).fetchone()
        if row is None:
            return None
        return _hypothesis_from_row(row)

    def list_hypotheses(
        self,
        *,
        statuses: tuple[HypothesisStatus, ...] | None = None,
    ) -> tuple[Hypothesis, ...]:
        with _connect_reality_db(self._db_path) as connection:
            if not statuses:
                rows = connection.execute(
                    """
                    SELECT * FROM reality_hypotheses
                    WHERE program_id = ?
                    ORDER BY proposed_at ASC, id ASC
                    """,
                    (self._program_id,),
                ).fetchall()
            else:
                placeholders = ", ".join("?" for _ in statuses)
                rows = connection.execute(
                    f"""
                    SELECT * FROM reality_hypotheses
                    WHERE program_id = ? AND status IN ({placeholders})
                    ORDER BY proposed_at ASC, id ASC
                    """,
                    (self._program_id, *(status.value for status in statuses)),
                ).fetchall()
        return tuple(_hypothesis_from_row(row) for row in rows)

    def upsert_hypothesis_annotation(self, annotation: HypothesisAnnotation) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO reality_hypothesis_annotations (
                    id, program_id, hypothesis_id, kind, title, locator, locator_kind,
                    media_type, sha256, note, tags_json, source_ref_json, added_by,
                    added_at, archived_at, archived_by, archive_reason, record_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    program_id = excluded.program_id,
                    hypothesis_id = excluded.hypothesis_id,
                    kind = excluded.kind,
                    title = excluded.title,
                    locator = excluded.locator,
                    locator_kind = excluded.locator_kind,
                    media_type = excluded.media_type,
                    sha256 = excluded.sha256,
                    note = excluded.note,
                    tags_json = excluded.tags_json,
                    source_ref_json = excluded.source_ref_json,
                    added_by = excluded.added_by,
                    added_at = excluded.added_at,
                    archived_at = excluded.archived_at,
                    archived_by = excluded.archived_by,
                    archive_reason = excluded.archive_reason,
                    record_type = excluded.record_type
                """,
                (
                    annotation.id,
                    annotation.program_id,
                    annotation.hypothesis_id,
                    annotation.kind,
                    annotation.title,
                    annotation.locator,
                    annotation.locator_kind,
                    annotation.media_type,
                    annotation.sha256,
                    annotation.note,
                    _encode_json(annotation.tags),
                    _encode_source_ref(annotation.source_ref),
                    annotation.added_by,
                    _encode_datetime(annotation.added_at),
                    _encode_datetime(annotation.archived_at),
                    annotation.archived_by,
                    annotation.archive_reason,
                    annotation.record_type,
                ),
            )

    def get_hypothesis_annotation(self, annotation_id: str) -> HypothesisAnnotation | None:
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(
                "SELECT * FROM reality_hypothesis_annotations WHERE program_id = ? AND id = ?",
                (self._program_id, annotation_id),
            ).fetchone()
        if row is None:
            return None
        return _hypothesis_annotation_from_row(row)

    def list_hypothesis_annotations(
        self,
        hypothesis_id: str,
        *,
        include_archived: bool = False,
    ) -> tuple[HypothesisAnnotation, ...]:
        query = """
            SELECT * FROM reality_hypothesis_annotations
            WHERE program_id = ? AND hypothesis_id = ?
        """
        parameters: list[object] = [self._program_id, hypothesis_id]
        if not include_archived:
            query += " AND archived_at IS NULL"
        query += " ORDER BY added_at ASC, id ASC"
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return tuple(_hypothesis_annotation_from_row(row) for row in rows)

    def archive_hypothesis_annotation(
        self,
        annotation_id: str,
        *,
        archived_at: datetime,
        archived_by: str,
        archive_reason: str,
    ) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                UPDATE reality_hypothesis_annotations
                SET archived_at = ?, archived_by = ?, archive_reason = ?
                WHERE program_id = ? AND id = ?
                """,
                (
                    _encode_datetime(archived_at),
                    archived_by,
                    archive_reason,
                    self._program_id,
                    annotation_id,
                ),
            )

    def get_latest_claim_hypothesis_state(self, claim_id: str) -> tuple[HypothesisStatus, datetime | None] | None:
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(
                """
                SELECT status, state_changed_at, proposed_at
                FROM reality_hypotheses
                WHERE program_id = ? AND linked_claim_id = ?
                ORDER BY COALESCE(state_changed_at, proposed_at) DESC, proposed_at DESC, id DESC
                LIMIT 1
                """,
                (self._program_id, claim_id),
            ).fetchone()
        if row is None:
            return None
        changed_at = _parse_datetime(row["state_changed_at"])
        if changed_at is None:
            changed_at = _parse_datetime(row["proposed_at"])
        return HypothesisStatus.from_string(str(row["status"])), changed_at

    def next_hypothesis_short_id(self) -> str:
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(
                "SELECT short_id FROM reality_hypotheses WHERE program_id = ? AND short_id IS NOT NULL",
                (self._program_id,),
            ).fetchall()
        next_number = 1
        for row in rows:
            short_id = str(row["short_id"])
            match = re.fullmatch(r"H-(\d+)", short_id)
            if match is None:
                continue
            next_number = max(next_number, int(match.group(1)) + 1)
        width = max(3, len(str(next_number)))
        return f"H-{next_number:0{width}d}"

    def list_active_hypotheses(self, *, include_proposed: bool = False) -> tuple[Hypothesis, ...]:
        statuses = [
            HypothesisStatus.CONFIRMED.value,
            HypothesisStatus.CHALLENGED.value,
            HypothesisStatus.STALE.value,
        ]
        if include_proposed:
            statuses.append(HypothesisStatus.PROPOSED.value)
        placeholders = ", ".join("?" for _ in statuses)
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM reality_hypotheses
                WHERE program_id = ? AND status IN ({placeholders})
                ORDER BY short_id ASC, id ASC
                """,
                (self._program_id, *statuses),
            ).fetchall()
        return tuple(_hypothesis_from_row(row) for row in rows)

    def update_hypothesis_status(self, hypothesis_id: str, status: HypothesisStatus, changed_at: datetime) -> None:
        self.set_hypothesis_state(hypothesis_id, status, changed_at, actor="vertex/system")

    def set_hypothesis_state(
        self,
        hypothesis_id: str,
        status: HypothesisStatus,
        changed_at: datetime,
        *,
        actor: str = "vertex/system",
        reason: str | None = None,
    ) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                UPDATE reality_hypotheses
                SET status = ?,
                    confirmed_at = CASE WHEN ? = 'confirmed' AND confirmed_at IS NULL THEN ? ELSE confirmed_at END,
                    state_actor = ?,
                    state_reason = ?,
                    state_changed_at = ?
                WHERE id = ? AND program_id = ?
                """,
                (
                    status.value,
                    status.value,
                    _encode_datetime(changed_at),
                    actor,
                    reason,
                    _encode_datetime(changed_at),
                    hypothesis_id,
                    self._program_id,
                ),
            )

    def reassign_snoozed_challenges(self, from_hypothesis_id: str, to_hypothesis_id: str) -> int:
        with _connect_reality_db(self._db_path) as connection:
            cursor = connection.execute(
                """
                UPDATE reality_challenges
                SET hypothesis_id = ?
                WHERE program_id = ? AND hypothesis_id = ? AND current_state = ?
                """,
                (
                    to_hypothesis_id,
                    self._program_id,
                    from_hypothesis_id,
                    ChallengeState.SNOOZED.value,
                ),
            )
        return max(cursor.rowcount, 0)

    def upsert_challenge(self, challenge: RealityChallenge) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO reality_challenges (
                    id, program_id, hypothesis_id, assertion_id, composite_assertion_id, observation_id, challenge_kind,
                    observed_value, expected_value, delta_magnitude, severity, source, detected_at,
                    note, evidence_url, ado_current_target, snoozed_until, snooze_reason,
                    current_state, state_actor, state_reason, state_changed_at, last_event_at, policy_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    program_id = excluded.program_id,
                    hypothesis_id = excluded.hypothesis_id,
                    assertion_id = excluded.assertion_id,
                    composite_assertion_id = excluded.composite_assertion_id,
                    observation_id = excluded.observation_id,
                    challenge_kind = excluded.challenge_kind,
                    observed_value = excluded.observed_value,
                    expected_value = excluded.expected_value,
                    delta_magnitude = excluded.delta_magnitude,
                    severity = excluded.severity,
                    source = excluded.source,
                    detected_at = excluded.detected_at,
                    note = excluded.note,
                    evidence_url = excluded.evidence_url,
                    ado_current_target = excluded.ado_current_target,
                    snoozed_until = excluded.snoozed_until,
                    snooze_reason = excluded.snooze_reason,
                    current_state = excluded.current_state,
                    state_actor = excluded.state_actor,
                    state_reason = excluded.state_reason,
                    state_changed_at = excluded.state_changed_at,
                    last_event_at = excluded.last_event_at,
                    policy_version = excluded.policy_version
                """,
                (
                    challenge.id,
                    challenge.program_id,
                    challenge.hypothesis_id,
                    challenge.assertion_id,
                    challenge.composite_assertion_id,
                    challenge.observation_id,
                    challenge.challenge_kind.value,
                    challenge.observed_value,
                    challenge.expected_value,
                    challenge.delta_magnitude,
                    challenge.severity.value,
                    challenge.source,
                    _encode_datetime(challenge.detected_at),
                    challenge.note,
                    challenge.evidence_url,
                    challenge.ado_current_target,
                    _encode_datetime(challenge.snoozed_until),
                    challenge.snooze_reason,
                    challenge.current_state.value,
                    challenge.state_actor,
                    challenge.state_reason,
                    _encode_datetime(challenge.state_changed_at),
                    _encode_datetime(challenge.last_event_at),
                    challenge.policy_version,
                ),
            )

    def get_challenge(self, challenge_id: str) -> RealityChallenge | None:
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(
                "SELECT * FROM reality_challenges WHERE id = ?",
                (challenge_id,),
            ).fetchone()
        if row is None:
            return None
        return _challenge_from_row(row)

    def get_latest_challenge_for_assertion(self, hypothesis_id: str, assertion_id: str) -> RealityChallenge | None:
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM reality_challenges
                WHERE program_id = ? AND hypothesis_id = ? AND assertion_id = ?
                ORDER BY detected_at DESC, id DESC
                LIMIT 1
                """,
                (self._program_id, hypothesis_id, assertion_id),
            ).fetchone()
        if row is None:
            return None
        return _challenge_from_row(row)

    def get_latest_challenge_for_composite_assertion(
        self,
        hypothesis_id: str,
        composite_assertion_id: str,
    ) -> RealityChallenge | None:
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM reality_challenges
                WHERE program_id = ? AND hypothesis_id = ? AND composite_assertion_id = ?
                ORDER BY detected_at DESC, id DESC
                LIMIT 1
                """,
                (self._program_id, hypothesis_id, composite_assertion_id),
            ).fetchone()
        if row is None:
            return None
        return _challenge_from_row(row)

    def get_latest_challenge_for_hypothesis(
        self,
        hypothesis_id: str,
        *,
        challenge_kind: ChallengeKind | None = None,
    ) -> RealityChallenge | None:
        query = """
            SELECT * FROM reality_challenges
            WHERE program_id = ? AND hypothesis_id = ?
        """
        parameters: list[object] = [self._program_id, hypothesis_id]
        if challenge_kind is not None:
            query += " AND challenge_kind = ?"
            parameters.append(challenge_kind.value)
        query += " ORDER BY detected_at DESC, id DESC LIMIT 1"
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(query, tuple(parameters)).fetchone()
        if row is None:
            return None
        return _challenge_from_row(row)

    def list_open_challenges(self) -> tuple[RealityChallenge, ...]:
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM reality_challenges
                WHERE program_id = ? AND current_state IN ('open', 'acknowledged', 'reopened')
                ORDER BY detected_at DESC, id DESC
                """,
                (self._program_id,),
            ).fetchall()
        return tuple(_challenge_from_row(row) for row in rows)

    def list_active_challenges(self, *, include_snoozed: bool = False) -> tuple[RealityChallenge, ...]:
        states = [ChallengeState.OPEN.value, ChallengeState.ACKNOWLEDGED.value, ChallengeState.REOPENED.value]
        if include_snoozed:
            states.append(ChallengeState.SNOOZED.value)
        placeholders = ", ".join("?" for _ in states)
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM reality_challenges
                WHERE program_id = ? AND current_state IN ({placeholders})
                ORDER BY detected_at DESC, id DESC
                """,
                (self._program_id, *states),
            ).fetchall()
        return tuple(_challenge_from_row(row) for row in rows)

    def list_challenges(self) -> tuple[RealityChallenge, ...]:
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM reality_challenges
                WHERE program_id = ?
                ORDER BY detected_at DESC, id DESC
                """,
                (self._program_id,),
            ).fetchall()
        return tuple(_challenge_from_row(row) for row in rows)

    def update_challenge_state(
        self,
        challenge_id: str,
        state: ChallengeState,
        changed_at: datetime,
        *,
        actor: str = "vertex/system",
        reason: str | None = None,
        snoozed_until: datetime | None = None,
        snooze_reason: str | None = None,
    ) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                UPDATE reality_challenges
                SET current_state = ?,
                    state_actor = ?,
                    state_reason = ?,
                    state_changed_at = ?,
                    last_event_at = ?,
                    snoozed_until = ?,
                    snooze_reason = ?
                WHERE id = ? AND program_id = ?
                """,
                (
                    state.value,
                    actor,
                    reason,
                    _encode_datetime(changed_at),
                    _encode_datetime(changed_at),
                    _encode_datetime(snoozed_until),
                    snooze_reason,
                    challenge_id,
                    self._program_id,
                ),
            )

    def write_metric_observation(
        self,
        observation: MetricObservation,
        *,
        corrected_reason: str | None = None,
    ) -> str:
        with _connect_reality_db(self._db_path) as connection:
            existing = connection.execute(
                """
                SELECT observation_id FROM reality_metric_observations
                WHERE program_id = ? AND metric_id = ? AND dimensions_json = ? AND measurement_period_end = ?
                  AND ((source_binding_id IS NULL AND ? IS NULL) OR source_binding_id = ?)
                """,
                (
                    observation.program_id,
                    observation.metric_id,
                    observation.dimensions_json,
                    _encode_datetime(observation.measurement_period_end),
                    observation.source_binding_id,
                    observation.source_binding_id,
                ),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO reality_metric_observations (
                        observation_id, program_id, metric_id, dimensions_json, measurement_period_start,
                        measurement_period_end, observed_at, value_num, value_text, sample_count,
                        quality_state, source_binding_id, binding_version, ingestion_run_id,
                        corrected_at, corrected_reason, inserted_at, is_pinned, pinned_at, pin_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation.observation_id,
                        observation.program_id,
                        observation.metric_id,
                        observation.dimensions_json,
                        _encode_datetime(observation.measurement_period_start),
                        _encode_datetime(observation.measurement_period_end),
                        _encode_datetime(observation.observed_at),
                        observation.value_num,
                        observation.value_text,
                        observation.sample_count,
                        observation.quality_state.value,
                        observation.source_binding_id,
                        observation.binding_version,
                        observation.ingestion_run_id,
                        _encode_datetime(observation.corrected_at),
                        observation.corrected_reason,
                        _encode_datetime(observation.inserted_at),
                        int(observation.is_pinned),
                        _encode_datetime(observation.pinned_at),
                        observation.pin_reason,
                    ),
                )
                return observation.observation_id

            existing_id = str(existing["observation_id"])
            if corrected_reason is None:
                return existing_id

            corrected_at = observation.corrected_at or datetime.now(timezone.utc)
            connection.execute(
                """
                UPDATE reality_metric_observations
                SET observed_at = ?,
                    value_num = ?,
                    value_text = ?,
                    sample_count = ?,
                    quality_state = ?,
                    binding_version = ?,
                    ingestion_run_id = ?,
                    corrected_at = ?,
                    corrected_reason = ?
                WHERE observation_id = ?
                """,
                (
                    _encode_datetime(observation.observed_at),
                    observation.value_num,
                    observation.value_text,
                    observation.sample_count,
                    MetricQualityState.LATE_CORRECTED.value,
                    observation.binding_version,
                    observation.ingestion_run_id,
                    _encode_datetime(corrected_at),
                    corrected_reason,
                    existing_id,
                ),
            )
            return existing_id

    def find_manual_observation(
        self,
        metric_id: str,
        *,
        measurement_period_end: datetime,
        dimensions_json: str,
    ) -> MetricObservation | None:
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM reality_metric_observations
                WHERE program_id = ?
                  AND metric_id = ?
                  AND dimensions_json = ?
                  AND measurement_period_end = ?
                  AND source_binding_id IS NULL
                  AND quality_state = ?
                ORDER BY observed_at DESC, inserted_at DESC
                LIMIT 1
                """,
                (
                    self._program_id,
                    metric_id,
                    dimensions_json,
                    _encode_datetime(measurement_period_end),
                    MetricQualityState.MANUAL.value,
                ),
            ).fetchone()
        if row is None:
            return None
        return _metric_observation_from_row(row)

    def update_observation_pin(
        self,
        observation_id: str,
        *,
        pinned: bool,
        reason: str | None = None,
        pinned_at: datetime | None = None,
    ) -> None:
        if pinned and (reason is None or not reason.strip()):
            raise ValueError("Pinned observations require a non-empty reason")
        with _connect_reality_db(self._db_path) as connection:
            cursor = connection.execute(
                """
                UPDATE reality_metric_observations
                SET is_pinned = ?,
                    pinned_at = ?,
                    pin_reason = ?
                WHERE observation_id = ?
                  AND program_id = ?
                  AND quality_state = ?
                """,
                (
                    int(pinned),
                    _encode_datetime(pinned_at if pinned else None),
                    reason.strip() if pinned and reason is not None else None,
                    observation_id,
                    self._program_id,
                    MetricQualityState.MANUAL.value,
                ),
            )
        if cursor.rowcount == 0:
            raise KeyError(observation_id)

    def overwrite_manual_observation(
        self,
        observation_id: str,
        observation: MetricObservation,
    ) -> str:
        with _connect_reality_db(self._db_path) as connection:
            cursor = connection.execute(
                """
                UPDATE reality_metric_observations
                SET measurement_period_start = ?,
                    measurement_period_end = ?,
                    observed_at = ?,
                    value_num = ?,
                    value_text = ?,
                    sample_count = ?,
                    quality_state = ?,
                    corrected_at = NULL,
                    corrected_reason = NULL,
                    inserted_at = ?,
                    is_pinned = 0,
                    pinned_at = NULL,
                    pin_reason = NULL
                WHERE observation_id = ?
                  AND program_id = ?
                  AND source_binding_id IS NULL
                  AND quality_state = ?
                """,
                (
                    _encode_datetime(observation.measurement_period_start),
                    _encode_datetime(observation.measurement_period_end),
                    _encode_datetime(observation.observed_at),
                    observation.value_num,
                    observation.value_text,
                    observation.sample_count,
                    MetricQualityState.MANUAL.value,
                    _encode_datetime(observation.inserted_at),
                    observation_id,
                    self._program_id,
                    MetricQualityState.MANUAL.value,
                ),
            )
        if cursor.rowcount == 0:
            raise KeyError(observation_id)
        return observation_id

    def list_metric_observations(self, metric_id: str) -> tuple[MetricObservation, ...]:
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM reality_metric_observations
                WHERE program_id = ? AND metric_id = ?
                ORDER BY measurement_period_end ASC, observed_at ASC, inserted_at ASC
                """,
                (self._program_id, metric_id),
            ).fetchall()
        return tuple(_metric_observation_from_row(row) for row in rows)

    def read_latest_metric_observation(self, metric_id: str, program_id: str | None = None) -> MetricObservation | None:
        # The store is bound to a single program (DB path + every query filters on
        # self._program_id). The optional program_id argument exists so callers can assert
        # the store they hold matches the program they intend to read. Silently ignoring a
        # mismatched value would return another program's latest observation; fail loud instead.
        if program_id is not None and program_id.strip() != self._program_id:
            raise ValueError(
                f"read_latest_metric_observation: program_id {program_id!r} does not match "
                f"this reality store's program {self._program_id!r}"
            )
        observations = self.list_metric_observations(metric_id)
        if not observations:
            return None
        return observations[-1]


    def append_assertion_evaluation(self, evaluation: AssertionEvaluation) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO reality_assertion_evaluations (
                    id, program_id, hypothesis_id, assertion_id, composite_assertion_id, observation_id,
                    evaluated_at, violated, value_num, expected_value, quality_state, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.id,
                    evaluation.program_id,
                    evaluation.hypothesis_id,
                    evaluation.assertion_id,
                    evaluation.composite_assertion_id,
                    evaluation.observation_id,
                    _encode_datetime(evaluation.evaluated_at),
                    int(evaluation.violated),
                    evaluation.value_num,
                    evaluation.expected_value,
                    evaluation.quality_state.value if evaluation.quality_state is not None else None,
                    evaluation.note,
                ),
            )

    def list_assertion_evaluations(
        self,
        *,
        assertion_ids: tuple[str, ...] | None = None,
        composite_assertion_ids: tuple[str, ...] | None = None,
    ) -> tuple[AssertionEvaluation, ...]:
        clauses = ["program_id = ?"]
        parameters: list[object] = [self._program_id]
        if assertion_ids is not None:
            normalized_assertion_ids = tuple(assertion_id for assertion_id in assertion_ids if assertion_id)
            if not normalized_assertion_ids:
                return ()
            placeholders = ", ".join("?" for _ in normalized_assertion_ids)
            clauses.append(f"assertion_id IN ({placeholders})")
            parameters.extend(normalized_assertion_ids)
        if composite_assertion_ids:
            normalized_composite_ids = tuple(assertion_id for assertion_id in composite_assertion_ids if assertion_id)
            if normalized_composite_ids:
                placeholders = ", ".join("?" for _ in normalized_composite_ids)
                clauses.append(f"composite_assertion_id IN ({placeholders})")
                parameters.extend(normalized_composite_ids)
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM reality_assertion_evaluations
                WHERE {' AND '.join(clauses)}
                ORDER BY evaluated_at ASC, id ASC
                """,
                tuple(parameters),
            ).fetchall()
        return tuple(_assertion_evaluation_from_row(row) for row in rows)

    def record_ingestion_run(self, run: IngestionRun) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO reality_ingestion_runs (
                    id, program_id, source_kind, source_ref, binding_id, started_at, heartbeat_at,
                    completed_at, status, expected_rows, metrics_observed, signals_written,
                    query_hash, captured_window, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    heartbeat_at = excluded.heartbeat_at,
                    completed_at = excluded.completed_at,
                    status = excluded.status,
                    expected_rows = excluded.expected_rows,
                    metrics_observed = excluded.metrics_observed,
                    signals_written = excluded.signals_written,
                    query_hash = excluded.query_hash,
                    captured_window = excluded.captured_window,
                    error_message = excluded.error_message
                """,
                (
                    run.id,
                    run.program_id,
                    run.source_kind,
                    run.source_ref,
                    run.binding_id,
                    _encode_datetime(run.started_at),
                    _encode_datetime(run.heartbeat_at),
                    _encode_datetime(run.completed_at),
                    run.status,
                    run.expected_rows,
                    run.metrics_observed,
                    run.signals_written,
                    run.query_hash,
                    run.captured_window,
                    _truncate_error_message(run.error_message),
                ),
            )

    def upsert_binding_health(self, health: MetricBindingHealth) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO reality_metric_binding_health (
                    program_id, binding_id, metric_id, last_success_at, last_attempt_at,
                    last_successful_observation_at, last_failure_at, consecutive_failures,
                    last_error_class, last_validation_error, is_degraded, degraded_since, watermark
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(program_id, binding_id) DO UPDATE SET
                    metric_id = excluded.metric_id,
                    last_success_at = excluded.last_success_at,
                    last_attempt_at = excluded.last_attempt_at,
                    last_successful_observation_at = excluded.last_successful_observation_at,
                    last_failure_at = excluded.last_failure_at,
                    consecutive_failures = excluded.consecutive_failures,
                    last_error_class = excluded.last_error_class,
                    last_validation_error = excluded.last_validation_error,
                    is_degraded = excluded.is_degraded,
                    degraded_since = excluded.degraded_since,
                    watermark = excluded.watermark
                """,
                (
                    health.program_id,
                    health.binding_id,
                    health.metric_id,
                    _encode_datetime(health.last_success_at),
                    _encode_datetime(health.last_attempt_at),
                    _encode_datetime(health.last_successful_observation_at),
                    _encode_datetime(health.last_failure_at),
                    health.consecutive_failures,
                    health.last_error_class,
                    health.last_validation_error,
                    int(health.is_degraded),
                    _encode_datetime(health.degraded_since),
                    _encode_datetime(health.watermark),
                ),
            )

    def get_binding_health(self, binding_id: str) -> MetricBindingHealth | None:
        with _connect_reality_db(self._db_path) as connection:
            row = connection.execute(
                "SELECT * FROM reality_metric_binding_health WHERE program_id = ? AND binding_id = ?",
                (self._program_id, binding_id),
            ).fetchone()
        if row is None:
            return None
        return _metric_binding_health_from_row(row)

    def upsert_maintenance_window(self, window: MaintenanceWindow) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO reality_maintenance_windows (
                    id, program_id, title, starts_at, ends_at, scope_kind, scope_value,
                    suppress_kinds_json, created_by, created_at, reference
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    program_id = excluded.program_id,
                    title = excluded.title,
                    starts_at = excluded.starts_at,
                    ends_at = excluded.ends_at,
                    scope_kind = excluded.scope_kind,
                    scope_value = excluded.scope_value,
                    suppress_kinds_json = excluded.suppress_kinds_json,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at,
                    reference = excluded.reference
                """,
                (
                    window.id,
                    window.program_id,
                    window.title,
                    _encode_datetime(window.starts_at),
                    _encode_datetime(window.ends_at),
                    window.scope_kind,
                    window.scope_value,
                    _encode_json(window.suppress_kinds),
                    window.created_by,
                    _encode_datetime(window.created_at),
                    window.reference,
                ),
            )

    def list_active_maintenance_windows(self, as_of: datetime) -> tuple[MaintenanceWindow, ...]:
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM reality_maintenance_windows
                WHERE program_id = ? AND starts_at <= ? AND ends_at > ?
                ORDER BY starts_at ASC, id ASC
                """,
                (self._program_id, _encode_datetime(as_of), _encode_datetime(as_of)),
            ).fetchall()
        return tuple(_maintenance_window_from_row(row) for row in rows)

    def record_suppression_event(
        self,
        *,
        hypothesis_id: str,
        assertion_id: str | None,
        observation_id: str | None,
        would_be_kind: ChallengeKind,
        would_be_severity: ChallengeSeverity,
        maintenance_window_id: str,
        suppressed_at: datetime,
    ) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO reality_suppression_events (
                    id, program_id, hypothesis_id, assertion_id, observation_id,
                    would_be_kind, would_be_severity, maintenance_window_id, suppressed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"supp-{hypothesis_id}-{assertion_id or 'none'}-{suppressed_at.timestamp()}",
                    self._program_id,
                    hypothesis_id,
                    assertion_id,
                    observation_id,
                    would_be_kind.value,
                    would_be_severity.value,
                    maintenance_window_id,
                    _encode_datetime(suppressed_at),
                ),
            )

    def list_suppression_summaries(self, as_of: datetime) -> tuple[SuppressionSummary, ...]:
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(
                """
                SELECT mw.id AS maintenance_window_id,
                       mw.title AS title,
                       mw.starts_at AS starts_at,
                       mw.ends_at AS ends_at,
                       COUNT(se.id) AS suppressed_count
                FROM reality_maintenance_windows mw
                LEFT JOIN reality_suppression_events se
                  ON se.maintenance_window_id = mw.id
                 AND se.program_id = mw.program_id
                WHERE mw.program_id = ? AND mw.starts_at <= ? AND mw.ends_at > ?
                GROUP BY mw.id, mw.title, mw.starts_at, mw.ends_at
                ORDER BY mw.starts_at ASC, mw.id ASC
                """,
                (self._program_id, _encode_datetime(as_of), _encode_datetime(as_of)),
            ).fetchall()
        return tuple(
            SuppressionSummary(
                maintenance_window_id=str(row["maintenance_window_id"]),
                title=str(row["title"]),
                suppressed_count=int(row["suppressed_count"]),
                starts_at=_parse_required_datetime(row["starts_at"]),
                ends_at=_parse_required_datetime(row["ends_at"]),
            )
            for row in rows
        )

    def write_digest_cache(self, digest: RealityDigestModel) -> None:
        payload_json = serialize_digest_model(digest)
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO digest_cache (program_id, as_of, digest_sha256, policy_version, payload_json, refreshed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(program_id) DO UPDATE SET
                    as_of = excluded.as_of,
                    digest_sha256 = excluded.digest_sha256,
                    policy_version = excluded.policy_version,
                    payload_json = excluded.payload_json,
                    refreshed_at = excluded.refreshed_at
                """,
                (
                    digest.program_id,
                    _encode_datetime(digest.as_of),
                    compute_digest_sha256(payload_json),
                    digest.policy_version,
                    payload_json,
                    _encode_datetime(digest.cache_built_at),
                ),
            )

    def build_digest_delta(self, *, since: datetime, to: datetime) -> DigestDelta:
        with _connect_reality_db(self._db_path) as connection:
            challenge_counts = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN detected_at > ? AND detected_at <= ? THEN 1 ELSE 0 END) AS challenges_opened,
                    SUM(CASE WHEN current_state = 'resolved' AND state_changed_at > ? AND state_changed_at <= ? THEN 1 ELSE 0 END) AS challenges_resolved,
                    SUM(CASE WHEN current_state = 'dismissed' AND state_changed_at > ? AND state_changed_at <= ? THEN 1 ELSE 0 END) AS challenges_dismissed,
                    SUM(CASE WHEN current_state = 'snoozed' AND state_changed_at > ? AND state_changed_at <= ? THEN 1 ELSE 0 END) AS challenges_snoozed
                FROM reality_challenges
                WHERE program_id = ?
                """,
                (
                    _encode_datetime(since),
                    _encode_datetime(to),
                    _encode_datetime(since),
                    _encode_datetime(to),
                    _encode_datetime(since),
                    _encode_datetime(to),
                    _encode_datetime(since),
                    _encode_datetime(to),
                    self._program_id,
                ),
            ).fetchone()
            hypothesis_counts = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN proposed_at > ? AND proposed_at <= ? THEN 1 ELSE 0 END) AS hypotheses_proposed,
                    SUM(CASE WHEN confirmed_at > ? AND confirmed_at <= ? THEN 1 ELSE 0 END) AS hypotheses_confirmed,
                    SUM(
                        CASE
                            WHEN status = 'confirmed'
                                AND confirmed_at IS NOT NULL
                                AND state_changed_at IS NOT NULL
                                AND state_changed_at > confirmed_at
                                AND state_changed_at > ?
                                AND state_changed_at <= ?
                            THEN 1
                            ELSE 0
                        END
                    ) AS hypotheses_recovered,
                    SUM(CASE WHEN status = 'superseded' AND state_changed_at > ? AND state_changed_at <= ? THEN 1 ELSE 0 END) AS hypotheses_superseded
                FROM reality_hypotheses
                WHERE program_id = ?
                """,
                (
                    _encode_datetime(since),
                    _encode_datetime(to),
                    _encode_datetime(since),
                    _encode_datetime(to),
                    _encode_datetime(since),
                    _encode_datetime(to),
                    _encode_datetime(since),
                    _encode_datetime(to),
                    self._program_id,
                ),
            ).fetchone()
        return DigestDelta(
            since=since,
            to=to,
            challenges_opened=int(challenge_counts["challenges_opened"] or 0),
            challenges_resolved=int(challenge_counts["challenges_resolved"] or 0),
            challenges_dismissed=int(challenge_counts["challenges_dismissed"] or 0),
            challenges_snoozed=int(challenge_counts["challenges_snoozed"] or 0),
            hypotheses_proposed=int(hypothesis_counts["hypotheses_proposed"] or 0),
            hypotheses_confirmed=int(hypothesis_counts["hypotheses_confirmed"] or 0),
            hypotheses_recovered=int(hypothesis_counts["hypotheses_recovered"] or 0),
            hypotheses_superseded=int(hypothesis_counts["hypotheses_superseded"] or 0),
        )

    def read_digest_cache_row(self) -> sqlite3.Row | None:
        with _connect_reality_db(self._db_path) as connection:
            return connection.execute(
                "SELECT * FROM digest_cache WHERE program_id = ?",
                (self._program_id,),
            ).fetchone()

    def record_schema_version(self, migration_id: str, applied_by: str, *, applied_at: datetime | None = None) -> None:
        with _connect_reality_db(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO schema_versions (migration_id, applied_at, applied_by)
                VALUES (?, ?, ?)
                ON CONFLICT(migration_id) DO NOTHING
                """,
                (
                    migration_id,
                    _encode_datetime(applied_at or datetime.now(timezone.utc)),
                    applied_by,
                ),
            )

    def read_schema_versions(self) -> tuple[str, ...]:
        with _connect_reality_db(self._db_path) as connection:
            rows = connection.execute(
                "SELECT migration_id FROM schema_versions ORDER BY migration_id ASC"
            ).fetchall()
        return tuple(str(row["migration_id"]) for row in rows)


def resolve_binding_owner(
    binding: "MetricSourceBinding",
    registry: Any,
) -> str | None:
    """WI-2.5: Resolve binding.owner_alias to a canonical entity ID via the registry.

    Returns the canonical entity ID if resolved, else None. The result can be
    stored back as binding.owner_entity_ref to adopt canonical IDs.

    Args:
        binding: A MetricSourceBinding (may already have owner_entity_ref set).
        registry: An EntityRegistry instance with a .resolve() method.

    Returns:
        Resolved canonical entity ID string, or None if unresolvable.
    """
    if binding.owner_entity_ref is not None:
        return binding.owner_entity_ref
    if not binding.owner_alias:
        return None
    resolve_fn = getattr(registry, "resolve", None)
    if resolve_fn is None:
        return None
    try:
        return resolve_fn(binding.owner_alias)
    except Exception:
        return None


@contextmanager
def _connect_reality_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    _ensure_schema(connection)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_versions (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            applied_by TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reality_metric_source_bindings (
            binding_id TEXT PRIMARY KEY,
            metric_id TEXT NOT NULL,
            program_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            cluster TEXT,
            database_name TEXT,
            kql_template TEXT,
            result_column TEXT,
            dimension_defaults_json TEXT NOT NULL DEFAULT '[]',
            validated INTEGER NOT NULL DEFAULT 0,
            last_validated_at TEXT,
            last_validated_kql_hash TEXT,
            owner_alias TEXT,
            binding_version INTEGER NOT NULL DEFAULT 1,
            kql_template_hash TEXT,
            evidence_url_template TEXT,
            valid_from TEXT NOT NULL,
            valid_until TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_rmsb_metric
            ON reality_metric_source_bindings(program_id, metric_id, priority, binding_id);

        CREATE TABLE IF NOT EXISTS reality_metric_observations (
            observation_id TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            metric_id TEXT NOT NULL,
            dimensions_json TEXT NOT NULL,
            measurement_period_start TEXT NOT NULL,
            measurement_period_end TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            value_num REAL,
            value_text TEXT,
            sample_count INTEGER,
            quality_state TEXT NOT NULL DEFAULT 'ok',
            source_binding_id TEXT,
            binding_version INTEGER,
            ingestion_run_id TEXT,
            corrected_at TEXT,
            corrected_reason TEXT,
            inserted_at TEXT NOT NULL,
            is_pinned INTEGER NOT NULL DEFAULT 0,
            pinned_at TEXT,
            pin_reason TEXT,
            FOREIGN KEY (source_binding_id) REFERENCES reality_metric_source_bindings(binding_id),
            FOREIGN KEY (ingestion_run_id) REFERENCES reality_ingestion_runs(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rmo_identity
            ON reality_metric_observations(
                program_id,
                metric_id,
                dimensions_json,
                measurement_period_end,
                COALESCE(source_binding_id, '')
            );
        CREATE INDEX IF NOT EXISTS idx_rmo_lookup
            ON reality_metric_observations(program_id, metric_id, observed_at DESC);

        CREATE TABLE IF NOT EXISTS reality_telemetry_assertions (
            id TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            metric_id TEXT NOT NULL,
            window_days INTEGER NOT NULL,
            window_aggregation TEXT NOT NULL,
            window_dimensions_json TEXT NOT NULL DEFAULT '[]',
            window_kind TEXT NOT NULL DEFAULT 'trailing',
            anchor_event_ref_json TEXT,
            minimum_observations INTEGER NOT NULL DEFAULT 1,
            operator TEXT NOT NULL,
            threshold REAL NOT NULL,
            threshold_upper REAL,
            tolerance_rel REAL NOT NULL DEFAULT 0.10,
            tolerance_abs REAL,
            baseline_value REAL,
            baseline_captured_at TEXT,
            sustain_min_observations INTEGER NOT NULL DEFAULT 3,
            cooldown_hours INTEGER NOT NULL DEFAULT 24,
            severity_override TEXT,
            description TEXT NOT NULL DEFAULT '',
            linked_hypothesis_id TEXT,
            linked_claim_id TEXT,
            linked_assumption_id TEXT,
            re_evaluate_by TEXT,
            policy_version INTEGER NOT NULL DEFAULT 1,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            created_by TEXT NOT NULL DEFAULT 'vertex/system'
        );
        CREATE INDEX IF NOT EXISTS idx_rta_active
            ON reality_telemetry_assertions(program_id, metric_id, valid_until)
            WHERE valid_until IS NULL;

        CREATE TABLE IF NOT EXISTS reality_composite_assertions (
            id TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            operator TEXT NOT NULL,
            child_assertion_ids_json TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            linked_hypothesis_id TEXT,
            policy_version INTEGER NOT NULL DEFAULT 1,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            created_by TEXT NOT NULL DEFAULT 'vertex/system'
        );
        CREATE INDEX IF NOT EXISTS idx_rca_active
            ON reality_composite_assertions(program_id, valid_until)
            WHERE valid_until IS NULL;

        CREATE TABLE IF NOT EXISTS reality_hypotheses (
            id TEXT PRIMARY KEY,
            short_id TEXT NOT NULL,
            program_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            statement TEXT NOT NULL,
            expected_value_num REAL,
            expected_value_date TEXT,
            expected_value_text TEXT,
            expected_value_frozen_at TEXT,
            as_of_date TEXT NOT NULL,
            telemetry_assertion_id TEXT,
            composite_assertion_id TEXT,
            workstream_id TEXT,
            sensitivity_label TEXT NOT NULL DEFAULT 'internal',
            proposed_by TEXT NOT NULL,
            proposed_at TEXT,
            status TEXT NOT NULL,
            depends_on_json TEXT NOT NULL DEFAULT '[]',
            confirmed_by TEXT,
            confirmed_at TEXT,
            review_due TEXT,
            superseded_by TEXT,
            supersedes_id TEXT,
            rejection_reason TEXT,
            linked_claim_id TEXT,
            linked_assumption_id TEXT,
            linked_ado_item_id INTEGER,
            linked_doc_section_id TEXT,
            source_refs_json TEXT NOT NULL DEFAULT '[]',
            expires_at TEXT,
            policy_version INTEGER NOT NULL DEFAULT 1,
            state_actor TEXT,
            state_reason TEXT,
            state_changed_at TEXT,
            FOREIGN KEY (telemetry_assertion_id) REFERENCES reality_telemetry_assertions(id),
            FOREIGN KEY (composite_assertion_id) REFERENCES reality_composite_assertions(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rh_short_id
            ON reality_hypotheses(program_id, short_id);
        DROP INDEX IF EXISTS idx_rh_linked_claim_active;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rh_linked_claim_active
            ON reality_hypotheses(linked_claim_id)
            WHERE linked_claim_id IS NOT NULL AND status NOT IN ('rejected', 'invalidated', 'superseded');

        CREATE TABLE IF NOT EXISTS reality_challenges (
            id TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            hypothesis_id TEXT NOT NULL,
            assertion_id TEXT,
            composite_assertion_id TEXT,
            observation_id TEXT,
            challenge_kind TEXT NOT NULL,
            observed_value REAL,
            expected_value REAL,
            delta_magnitude REAL,
            severity TEXT NOT NULL,
            source TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            note TEXT,
            evidence_url TEXT,
            ado_current_target TEXT,
            snoozed_until TEXT,
            snooze_reason TEXT,
            current_state TEXT NOT NULL DEFAULT 'open',
            state_actor TEXT,
            state_reason TEXT,
            state_changed_at TEXT,
            last_event_at TEXT NOT NULL,
            policy_version INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (hypothesis_id) REFERENCES reality_hypotheses(id),
            FOREIGN KEY (assertion_id) REFERENCES reality_telemetry_assertions(id),
            FOREIGN KEY (composite_assertion_id) REFERENCES reality_composite_assertions(id),
            FOREIGN KEY (observation_id) REFERENCES reality_metric_observations(observation_id)
        );
        CREATE INDEX IF NOT EXISTS idx_rc_open
            ON reality_challenges(program_id, severity, detected_at DESC)
            WHERE current_state IN ('open', 'acknowledged', 'reopened');

        CREATE TABLE IF NOT EXISTS reality_hypothesis_annotations (
            id TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            hypothesis_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            locator TEXT NOT NULL,
            locator_kind TEXT NOT NULL,
            media_type TEXT,
            sha256 TEXT,
            note TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_ref_json TEXT,
            added_by TEXT NOT NULL,
            added_at TEXT NOT NULL,
            archived_at TEXT,
            archived_by TEXT,
            archive_reason TEXT,
            record_type TEXT NOT NULL DEFAULT 'hypothesis_annotation',
            FOREIGN KEY (hypothesis_id) REFERENCES reality_hypotheses(id)
        );
        CREATE INDEX IF NOT EXISTS idx_rha_hypothesis_added
            ON reality_hypothesis_annotations(program_id, hypothesis_id, added_at ASC, id ASC);

        CREATE TABLE IF NOT EXISTS reality_ingestion_runs (
            id TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            binding_id TEXT,
            started_at TEXT NOT NULL,
            heartbeat_at TEXT,
            completed_at TEXT,
            status TEXT NOT NULL,
            expected_rows INTEGER,
            metrics_observed INTEGER NOT NULL DEFAULT 0,
            signals_written INTEGER NOT NULL DEFAULT 0,
            query_hash TEXT,
            captured_window TEXT,
            error_message TEXT,
            FOREIGN KEY (binding_id) REFERENCES reality_metric_source_bindings(binding_id)
        );

        CREATE TABLE IF NOT EXISTS reality_metric_binding_health (
            program_id TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            metric_id TEXT NOT NULL,
            last_success_at TEXT,
            last_attempt_at TEXT,
            last_successful_observation_at TEXT,
            last_failure_at TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_error_class TEXT,
            last_validation_error TEXT,
            is_degraded INTEGER NOT NULL DEFAULT 0,
            degraded_since TEXT,
            watermark TEXT,
            PRIMARY KEY (program_id, binding_id)
        );

        CREATE TABLE IF NOT EXISTS reality_assertion_evaluations (
            id TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            hypothesis_id TEXT NOT NULL,
            assertion_id TEXT,
            composite_assertion_id TEXT,
            observation_id TEXT,
            evaluated_at TEXT NOT NULL,
            violated INTEGER NOT NULL,
            value_num REAL,
            expected_value REAL,
            quality_state TEXT,
            note TEXT,
            FOREIGN KEY (hypothesis_id) REFERENCES reality_hypotheses(id),
            FOREIGN KEY (assertion_id) REFERENCES reality_telemetry_assertions(id),
            FOREIGN KEY (composite_assertion_id) REFERENCES reality_composite_assertions(id),
            FOREIGN KEY (observation_id) REFERENCES reality_metric_observations(observation_id)
        );

        CREATE TABLE IF NOT EXISTS reality_maintenance_windows (
            id TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            title TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value TEXT NOT NULL,
            suppress_kinds_json TEXT NOT NULL DEFAULT '["threshold_breach","staleness","data_loss","source_degraded"]',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reference TEXT
        );

        CREATE TABLE IF NOT EXISTS reality_suppression_events (
            id TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            hypothesis_id TEXT NOT NULL,
            assertion_id TEXT,
            observation_id TEXT,
            would_be_kind TEXT NOT NULL,
            would_be_severity TEXT NOT NULL,
            maintenance_window_id TEXT NOT NULL,
            suppressed_at TEXT NOT NULL,
            FOREIGN KEY (hypothesis_id) REFERENCES reality_hypotheses(id),
            FOREIGN KEY (maintenance_window_id) REFERENCES reality_maintenance_windows(id)
        );

        CREATE TABLE IF NOT EXISTS digest_cache (
            program_id TEXT PRIMARY KEY,
            as_of TEXT NOT NULL,
            digest_sha256 TEXT NOT NULL,
            policy_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            refreshed_at TEXT NOT NULL
        );
        """
    )
    _ensure_table_columns(
        connection,
        "reality_metric_source_bindings",
        {
            "owner_entity_ref": "TEXT",
        },
    )
    _ensure_table_columns(
        connection,
        "reality_metric_observations",
        {
            "is_pinned": "INTEGER NOT NULL DEFAULT 0",
            "pinned_at": "TEXT",
            "pin_reason": "TEXT",
        },
    )
    _ensure_table_columns(
        connection,
        "reality_telemetry_assertions",
        {
            "threshold_upper": "REAL",
        },
    )
    _ensure_table_columns(
        connection,
        "reality_hypotheses",
        {
            "composite_assertion_id": "TEXT",
        },
    )
    _ensure_table_columns(
        connection,
        "reality_challenges",
        {
            "composite_assertion_id": "TEXT",
        },
    )
    _ensure_table_columns(
        connection,
        "reality_assertion_evaluations",
        {
            "composite_assertion_id": "TEXT",
        },
    )
    _ensure_table_columns(
        connection,
        "reality_ingestion_runs",
        {
            "query_hash": "TEXT",
            "captured_window": "TEXT",
        },
    )


def _ensure_table_columns(
    connection: sqlite3.Connection,
    table_name: str,
    columns: dict[str, str],
) -> None:
    existing_columns = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    for column_name, definition in columns.items():
        if column_name in existing_columns:
            continue
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _metric_source_binding_from_row(row: sqlite3.Row) -> MetricSourceBinding:
    return MetricSourceBinding(
        binding_id=str(row["binding_id"]),
        metric_id=str(row["metric_id"]),
        program_id=str(row["program_id"]),
        source_kind=cast(Literal["kusto", "wiql"], str(row["source_kind"])),
        priority=int(row["priority"]),
        cluster=_optional_string(row["cluster"]),
        database=_optional_string(row["database_name"]),
        kql_template=_optional_string(row["kql_template"]),
        result_column=_optional_string(row["result_column"]),
        dimension_defaults=_decode_string_pairs(row["dimension_defaults_json"]),
        validated=bool(row["validated"]),
        last_validated_at=_parse_datetime(row["last_validated_at"]),
        last_validated_kql_hash=_optional_string(row["last_validated_kql_hash"]),
        owner_alias=_optional_string(row["owner_alias"]),
        owner_entity_ref=_optional_string(row["owner_entity_ref"]) if "owner_entity_ref" in row.keys() else None,
        binding_version=int(row["binding_version"]),
        kql_template_hash=_optional_string(row["kql_template_hash"]),
        evidence_url_template=_optional_string(row["evidence_url_template"]),
        valid_from=_parse_required_datetime(row["valid_from"]),
        valid_until=_parse_datetime(row["valid_until"]),
    )


def _telemetry_assertion_from_row(row: sqlite3.Row) -> TelemetryAssertion:
    return TelemetryAssertion(
        id=str(row["id"]),
        program_id=str(row["program_id"]),
        metric_id=str(row["metric_id"]),
        window=ObservationWindow(
            days=int(row["window_days"]),
            aggregation=_parse_metric_aggregation(str(row["window_aggregation"])),
            dimensions=_decode_string_pairs(row["window_dimensions_json"]),
            window_kind=cast(Literal["trailing", "anchored_after"], str(row["window_kind"])),
            anchor_event_ref=_decode_source_ref(row["anchor_event_ref_json"]),
            minimum_observations=int(row["minimum_observations"]),
        ),
        operator=AssertionOperator.from_string(str(row["operator"])),
        threshold=float(row["threshold"]),
        tolerance_rel=float(row["tolerance_rel"]),
        tolerance_abs=_optional_float(row["tolerance_abs"]),
        baseline_value=_optional_float(row["baseline_value"]),
        baseline_captured_at=_parse_datetime(row["baseline_captured_at"]),
        sustain_min_observations=int(row["sustain_min_observations"]),
        cooldown_hours=int(row["cooldown_hours"]),
        severity_override=cast(Literal["info", "warn", "alert"] | None, _optional_string(row["severity_override"])),
        description=str(row["description"]),
        linked_hypothesis_id=_optional_string(row["linked_hypothesis_id"]),
        linked_claim_id=_optional_string(row["linked_claim_id"]),
        linked_assumption_id=_optional_string(row["linked_assumption_id"]),
        re_evaluate_by=_parse_date(row["re_evaluate_by"]),
        policy_version=int(row["policy_version"]),
        valid_from=_parse_required_datetime(row["valid_from"]),
        valid_until=_parse_datetime(row["valid_until"]),
        created_by=str(row["created_by"]),
        threshold_upper=_optional_float(row["threshold_upper"]),
    )


def _composite_assertion_from_row(row: sqlite3.Row) -> CompositeAssertion:
    return CompositeAssertion(
        id=str(row["id"]),
        program_id=str(row["program_id"]),
        operator=CompositeAssertionOperator.from_string(str(row["operator"])),
        child_assertion_ids=tuple(str(value) for value in json.loads(str(row["child_assertion_ids_json"]))),
        description=str(row["description"] or ""),
        linked_hypothesis_id=_optional_string(row["linked_hypothesis_id"]),
        valid_from=_parse_required_datetime(row["valid_from"]),
        valid_until=_parse_datetime(row["valid_until"]),
        policy_version=int(row["policy_version"]),
        created_by=str(row["created_by"] or "vertex/system"),
    )


def _hypothesis_from_row(row: sqlite3.Row) -> Hypothesis:
    return Hypothesis(
        id=str(row["id"]),
        short_id=str(row["short_id"]),
        program_id=str(row["program_id"]),
        kind=HypothesisKind.from_string(str(row["kind"])),
        statement=str(row["statement"]),
        expected_value=_join_expected_value(row),
        expected_value_frozen_at=_parse_datetime(row["expected_value_frozen_at"]),
        as_of_date=date.fromisoformat(str(row["as_of_date"])),
        telemetry_assertion_id=_optional_string(row["telemetry_assertion_id"]),
        source_refs=_decode_source_refs(row["source_refs_json"]),
        workstream_id=_optional_string(row["workstream_id"]),
        proposed_by=str(row["proposed_by"]),
        proposed_at=_parse_datetime(row["proposed_at"]),
        status=HypothesisStatus.from_string(str(row["status"])),
        sensitivity_label=cast(Literal["public", "internal", "confidential", "secret"], str(row["sensitivity_label"])),
        depends_on=tuple(str(value) for value in json.loads(str(row["depends_on_json"]))),
        confirmed_by=_optional_string(row["confirmed_by"]),
        confirmed_at=_parse_datetime(row["confirmed_at"]),
        review_due=_parse_date(row["review_due"]),
        superseded_by=_optional_string(row["superseded_by"]),
        supersedes_id=_optional_string(row["supersedes_id"]),
        rejection_reason=_optional_string(row["rejection_reason"]),
        linked_claim_id=_optional_string(row["linked_claim_id"]),
        linked_assumption_id=_optional_string(row["linked_assumption_id"]),
        linked_ado_item_id=_optional_int(row["linked_ado_item_id"]),
        linked_doc_section_id=_optional_string(row["linked_doc_section_id"]),
        expires_at=_parse_datetime(row["expires_at"]),
        policy_version=int(row["policy_version"]),
        composite_assertion_id=_optional_string(row["composite_assertion_id"]),
    )


def _challenge_from_row(row: sqlite3.Row) -> RealityChallenge:
    return RealityChallenge(
        id=str(row["id"]),
        program_id=str(row["program_id"]),
        hypothesis_id=str(row["hypothesis_id"]),
        assertion_id=_optional_string(row["assertion_id"]),
        observation_id=_optional_string(row["observation_id"]),
        challenge_kind=ChallengeKind.from_string(str(row["challenge_kind"])),
        observed_value=_optional_float(row["observed_value"]),
        expected_value=_optional_float(row["expected_value"]),
        delta_magnitude=_optional_float(row["delta_magnitude"]),
        severity=ChallengeSeverity.from_string(str(row["severity"])),
        source=str(row["source"]),
        detected_at=_parse_required_datetime(row["detected_at"]),
        note=_optional_string(row["note"]),
        evidence_url=_optional_string(row["evidence_url"]),
        ado_current_target=_optional_string(row["ado_current_target"]),
        snoozed_until=_parse_datetime(row["snoozed_until"]),
        snooze_reason=_optional_string(row["snooze_reason"]),
        current_state=ChallengeState.from_string(str(row["current_state"])),
        state_changed_at=_parse_datetime(row["state_changed_at"]),
        state_actor=_optional_string(row["state_actor"]),
        state_reason=_optional_string(row["state_reason"]),
        last_event_at=_parse_required_datetime(row["last_event_at"]),
        policy_version=int(row["policy_version"]),
        composite_assertion_id=_optional_string(row["composite_assertion_id"]),
    )


def _hypothesis_annotation_from_row(row: sqlite3.Row) -> HypothesisAnnotation:
    return HypothesisAnnotation(
        id=str(row["id"]),
        program_id=str(row["program_id"]),
        hypothesis_id=str(row["hypothesis_id"]),
        kind=cast(Literal["pdf", "markdown", "url", "file"], str(row["kind"])),
        title=str(row["title"]),
        locator=str(row["locator"]),
        locator_kind=cast(Literal["url", "repo_path", "local_path"], str(row["locator_kind"])),
        media_type=_optional_string(row["media_type"]),
        sha256=_optional_string(row["sha256"]),
        note=_optional_string(row["note"]),
        tags=_decode_string_list(row["tags_json"]),
        source_ref=_decode_source_ref(row["source_ref_json"]),
        added_by=str(row["added_by"]),
        added_at=_parse_required_datetime(row["added_at"]),
        archived_at=_parse_datetime(row["archived_at"]),
        archived_by=_optional_string(row["archived_by"]),
        archive_reason=_optional_string(row["archive_reason"]),
        record_type=cast(Literal["hypothesis_annotation"], str(row["record_type"])),
    )


def _metric_observation_from_row(row: sqlite3.Row) -> MetricObservation:
    return MetricObservation(
        observation_id=str(row["observation_id"]),
        program_id=str(row["program_id"]),
        metric_id=str(row["metric_id"]),
        dimensions_json=str(row["dimensions_json"]),
        measurement_period_start=_parse_required_datetime(row["measurement_period_start"]),
        measurement_period_end=_parse_required_datetime(row["measurement_period_end"]),
        observed_at=_parse_required_datetime(row["observed_at"]),
        value_num=_optional_float(row["value_num"]),
        value_text=_optional_string(row["value_text"]),
        sample_count=_optional_int(row["sample_count"]),
        quality_state=MetricQualityState.from_string(str(row["quality_state"])),
        source_binding_id=_optional_string(row["source_binding_id"]),
        binding_version=_optional_int(row["binding_version"]),
        ingestion_run_id=_optional_string(row["ingestion_run_id"]),
        corrected_at=_parse_datetime(row["corrected_at"]),
        corrected_reason=_optional_string(row["corrected_reason"]),
        inserted_at=_parse_required_datetime(row["inserted_at"]),
        is_pinned=bool(row["is_pinned"]),
        pinned_at=_parse_datetime(row["pinned_at"]),
        pin_reason=_optional_string(row["pin_reason"]),
    )


def _metric_binding_health_from_row(row: sqlite3.Row) -> MetricBindingHealth:
    return MetricBindingHealth(
        program_id=str(row["program_id"]),
        binding_id=str(row["binding_id"]),
        metric_id=str(row["metric_id"]),
        last_success_at=_parse_datetime(row["last_success_at"]),
        last_attempt_at=_parse_datetime(row["last_attempt_at"]),
        last_successful_observation_at=_parse_datetime(row["last_successful_observation_at"]),
        last_failure_at=_parse_datetime(row["last_failure_at"]),
        consecutive_failures=int(row["consecutive_failures"]),
        last_error_class=_optional_string(row["last_error_class"]),
        last_validation_error=_optional_string(row["last_validation_error"]),
        is_degraded=bool(row["is_degraded"]),
        degraded_since=_parse_datetime(row["degraded_since"]),
        watermark=_parse_datetime(row["watermark"]),
    )


def _maintenance_window_from_row(row: sqlite3.Row) -> MaintenanceWindow:
    return MaintenanceWindow(
        id=str(row["id"]),
        program_id=str(row["program_id"]),
        title=str(row["title"]),
        starts_at=_parse_required_datetime(row["starts_at"]),
        ends_at=_parse_required_datetime(row["ends_at"]),
        scope_kind=cast(Literal["program", "metric", "binding", "workstream"], str(row["scope_kind"])),
        scope_value=str(row["scope_value"]),
        suppress_kinds=cast(
            tuple[Literal["threshold_breach", "staleness", "data_loss", "source_degraded"], ...],
            tuple(str(value) for value in json.loads(str(row["suppress_kinds_json"]))),
        ),
        created_by=str(row["created_by"]),
        created_at=_parse_datetime(row["created_at"]),
        reference=_optional_string(row["reference"]),
    )


def _assertion_evaluation_from_row(row: sqlite3.Row) -> AssertionEvaluation:
    quality_state = row["quality_state"]
    return AssertionEvaluation(
        id=str(row["id"]),
        program_id=str(row["program_id"]),
        hypothesis_id=str(row["hypothesis_id"]),
        assertion_id=_optional_string(row["assertion_id"]),
        observation_id=_optional_string(row["observation_id"]),
        evaluated_at=_parse_required_datetime(row["evaluated_at"]),
        violated=bool(row["violated"]),
        value_num=_optional_float(row["value_num"]),
        expected_value=_optional_float(row["expected_value"]),
        quality_state=MetricQualityState.from_string(str(quality_state)) if quality_state is not None else None,
        note=_optional_string(row["note"]),
        composite_assertion_id=_optional_string(row["composite_assertion_id"]),
    )


def _split_expected_value(kind: HypothesisKind, value: float | str | None) -> tuple[float | None, str | None, str | None]:
    if value is None:
        return None, None, None
    if kind == HypothesisKind.DELIVERY_DATE:
        return None, str(value), None
    if isinstance(value, (int, float)):
        return float(value), None, None
    return None, None, str(value)


def _join_expected_value(row: sqlite3.Row) -> float | str | None:
    if row["expected_value_num"] is not None:
        return float(row["expected_value_num"])
    if row["expected_value_date"] is not None:
        return str(row["expected_value_date"])
    if row["expected_value_text"] is not None:
        return str(row["expected_value_text"])
    return None


def _encode_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _parse_required_datetime(value: object) -> datetime:
    parsed = _parse_datetime(value)
    if parsed is None:
        raise ValueError("Expected datetime value.")
    return parsed


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _encode_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(str(value))


def _encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _encode_source_ref(value: SourceRef | None) -> str | None:
    if value is None:
        return None
    return _encode_json(
        {
            "kind": value.kind.value,
            "ref": value.ref,
            "captured_at": _encode_datetime(value.captured_at),
        }
    )


def _decode_source_ref(value: object) -> SourceRef | None:
    if value is None:
        return None
    payload = json.loads(str(value))
    return SourceRef(
        kind=SourceKind.from_string(str(payload["kind"])),
        ref=str(payload["ref"]),
        captured_at=_parse_datetime(payload.get("captured_at")),
    )


def _encode_source_refs(values: tuple[SourceRef, ...]) -> str:
    return _encode_json(
        [
            {"kind": value.kind.value, "ref": value.ref, "captured_at": _encode_datetime(value.captured_at)}
            for value in values
        ]
    )


def _decode_source_refs(value: object) -> tuple[SourceRef, ...]:
    payload = json.loads(str(value)) if value is not None else []
    return tuple(
        SourceRef(
            kind=SourceKind.from_string(str(entry["kind"])),
            ref=str(entry["ref"]),
            captured_at=_parse_datetime(entry.get("captured_at")),
        )
        for entry in payload
    )


def _decode_string_pairs(value: object) -> tuple[tuple[str, str], ...]:
    payload = json.loads(str(value)) if value is not None else []
    return tuple((str(pair[0]), str(pair[1])) for pair in payload)


def _decode_string_list(value: object) -> tuple[str, ...]:
    payload = json.loads(str(value)) if value is not None else []
    return tuple(str(entry) for entry in payload)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value))


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return int(value)
    return int(str(value))


def _truncate_error_message(message: str | None) -> str | None:
    if message is None:
        return None
    return message[:4096]


def _parse_metric_aggregation(value: str):
    from src.core.metric_models import MetricAggregation

    return MetricAggregation.from_string(value)
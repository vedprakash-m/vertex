"""WS-15: privacy & data governance matrix (canonical runtime source of truth).

Spec: `governance/privacy-matrix.md` (tracked). This module is the
**runtime** source of truth; the contract test
`tests/contracts/test_privacy_matrix_contract.py` is the ratchet that prevents
drift between the markdown spec and the Python constants.

Design rules:
1. **Stable identifiers only.** Every classification, retention class, and
   channel has a string identifier. Downstream code MUST compare against
   these constants, not against the human-readable label.
2. **One source of truth per fact.** The markdown spec lists the
   *intent*; this module is the *executable* matrix. The contract test
   asserts the two are in sync.
3. **No hardcoded program literals** (D-24). Channel names use the
   platform-neutral `Channel` enum, not program-specific names.
4. **Append-only evolution.** New classifications/retention classes may
   be added; existing identifiers must never be re-purposed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class DataClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PII = "pii"
    SECRET = "secret"


class RetentionClass(str, Enum):
    EPHEMERAL = "ephemeral"
    FORTY_FIVE_DAYS = "45d"
    NINETY_DAYS = "90d"
    ONE_YEAR = "1y"
    SEVEN_YEARS = "7y"
    INDEFINITE = "indefinite"


class Channel(str, Enum):
    ADO = "ado"
    KUSTO = "kusto"
    ICM = "icm"
    TEAMS = "teams"
    WORKIQ = "workiq"
    TRANSCRIPT = "transcript"


@dataclass(frozen=True, slots=True)
class ChannelDataPosture:
    """Per-channel data-handling posture.

    `read_default_class` is the maximum classification of the *read* payload
    (e.g. ADO work-item titles are CONFIDENTIAL by default).
    `write_default_class` is the maximum classification of any payload Vertex
    writes *back* to the channel; it is `None` if Vertex does not write.
    `retention` is the canonical retention class for the channel's records.
    `rbac_model` is the AAD model used: `user-context` (delegated, operator),
    `application` (app-only, requires tenant admin consent), or
    `managed-identity` (unattended, scoped RBAC).
    `least_privilege_scopes` is a tuple of Graph/Kusto/ADO scope strings that
    are the **least-privilege** set required for the channel.
    """

    channel: Channel
    read_default_class: DataClassification
    write_default_class: DataClassification | None
    retention: RetentionClass
    rbac_model: str
    least_privilege_scopes: tuple[str, ...]


# Canonical channel posture. Source of truth for `vertex doctor --privacy`
# channel checks AND the privacy-matrix contract test.
CHANNEL_POSTURE: dict[Channel, ChannelDataPosture] = {
    Channel.ADO: ChannelDataPosture(
        channel=Channel.ADO,
        read_default_class=DataClassification.CONFIDENTIAL,
        write_default_class=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        rbac_model="user-context",
        least_privilege_scopes=("vso.work_read", "vso.work_write"),
    ),
    Channel.KUSTO: ChannelDataPosture(
        channel=Channel.KUSTO,
        read_default_class=DataClassification.CONFIDENTIAL,
        write_default_class=None,
        retention=RetentionClass.ONE_YEAR,
        rbac_model="managed-identity",
        least_privilege_scopes=("cluster.viewer",),
    ),
    Channel.ICM: ChannelDataPosture(
        channel=Channel.ICM,
        read_default_class=DataClassification.CONFIDENTIAL,
        write_default_class=None,
        retention=RetentionClass.ONE_YEAR,
        rbac_model="application",
        least_privilege_scopes=("IcMIncidentRead.All",),
    ),
    Channel.TEAMS: ChannelDataPosture(
        channel=Channel.TEAMS,
        read_default_class=DataClassification.INTERNAL,
        write_default_class=DataClassification.INTERNAL,
        retention=RetentionClass.ONE_YEAR,
        rbac_model="application",
        least_privilege_scopes=("ChannelMessage.Send", "Channel.ReadBasic.All"),
    ),
    Channel.WORKIQ: ChannelDataPosture(
        channel=Channel.WORKIQ,
        read_default_class=DataClassification.CONFIDENTIAL,
        write_default_class=None,
        retention=RetentionClass.EPHEMERAL,
        rbac_model="user-context",
        least_privilege_scopes=("Calendars.Read", "Mail.Read"),
    ),
    Channel.TRANSCRIPT: ChannelDataPosture(
        channel=Channel.TRANSCRIPT,
        read_default_class=DataClassification.CONFIDENTIAL,
        write_default_class=None,
        retention=RetentionClass.EPHEMERAL,
        rbac_model="application",
        least_privilege_scopes=("OnlineMeetings.Read", "CallRecords.Read.All"),
    ),
}


@dataclass(frozen=True, slots=True)
class SidecarRetentionRule:
    """Per-sidecar retention + class rule. Drives WS-18 retention policy."""

    artifact_path: str
    classification: DataClassification
    retention: RetentionClass
    supports_excise: bool  # True if `[EXCISED]` tombstone is supported for PII scrub
    #: ADF-W5.9: when set, `privacy_purge.py` checks THIS field's timestamp
    #: instead of the generic created_at/recorded_at/timestamp/ts/sent_at
    #: priority list, and treats a row where the field is absent/null as
    #: never-eligible (kept forever) rather than falling through to another
    #: field. Needed for alerts.jsonl's "open-forever/90d-resolved" policy:
    #: eligibility_field="resolved_at" means an open alert (resolved_at=None)
    #: is never purged regardless of its created_at age.
    eligibility_field: str | None = None
    #: ADF-W5.9: when set, `artifact_path` names a DIRECTORY of individual
    #: content-addressed JSON files (not one JSONL file of many rows) --
    #: e.g. `runtime/context_manifests/` holding one `<hash>.json` per
    #: compile. This glob (e.g. "*.json") selects which files to age-check;
    #: `eligibility_field` names the timestamp field READ FROM INSIDE each
    #: matched file (not the row itself, since there is no "row" here).
    directory_glob: str | None = None


# Canonical sidecar retention rules. Source of truth for the journal
# rotation policy (rev. 323) and the WS-18 audit-query retention cutoffs.
SIDECAR_RETENTION: tuple[SidecarRetentionRule, ...] = (
    SidecarRetentionRule(
        # Armada D-10/D-14: immutable gather-run evidence is CONFIDENTIAL
        # and retained one year. privacy_purge handles this manifest-shaped
        # directory specially, preserving latest pointers/current runs.
        artifact_path="runtime/gather_runs",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=False,
    ),
    SidecarRetentionRule(
        artifact_path="journal/signals.jsonl",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        artifact_path="journal/reviews.jsonl",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        artifact_path="journal/autonomy_audit.jsonl",
        classification=DataClassification.PII,
        retention=RetentionClass.SEVEN_YEARS,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        artifact_path="journal/actions.jsonl",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        artifact_path="journal/ai_proposals.jsonl",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.SEVEN_YEARS,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        artifact_path="journal/risk_updates.jsonl",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        artifact_path="journal/edit_patterns.jsonl",
        classification=DataClassification.PII,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        artifact_path="people_profiles.yaml",
        classification=DataClassification.PII,
        retention=RetentionClass.INDEFINITE,
        supports_excise=False,  # full-record deletion only; no in-place redact
    ),
    SidecarRetentionRule(
        # Phase 1-B sweep: corrected from the stale "analytics.sqlite3" filename
        # to the actual file ("vertex_analytics.sqlite3") and its new runtime/ path.
        artifact_path="runtime/vertex_analytics.sqlite3",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        artifact_path="migration_log.jsonl",
        classification=DataClassification.INTERNAL,
        retention=RetentionClass.INDEFINITE,
        supports_excise=False,
    ),
    SidecarRetentionRule(
        artifact_path="archive/<edition>/snapshots/issue_NNN.snapshot.json",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.INDEFINITE,
        supports_excise=True,  # metadata-only excise (file is immutable)
    ),
    SidecarRetentionRule(
        artifact_path="archive/<edition>/manifests/issue_NNN.json",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.INDEFINITE,
        supports_excise=True,  # metadata-only excise (file is immutable)
    ),
    SidecarRetentionRule(
        # Phase 1-B sweep: updated to runtime/ path (declutter.md §6 1-B).
        artifact_path="runtime/gather_state.json",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=False,
    ),
    SidecarRetentionRule(
        artifact_path="external_dependencies.jsonl",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=False,
    ),
    SidecarRetentionRule(
        # arch-fix.md Phase 0 corpus prerequisite (§A.0): opt-in, sanitized,
        # size-bounded AI prompt/response excerpts (src/ai/safety/ai_trace_capture.py).
        # Short TTL because it exists only to bake the AF-1/AF-4 eval corpus,
        # not as a long-term audit-of-record (that's the AF-3 fail-closed
        # audit trail, a separate durable store landing in Phase 2b).
        artifact_path="ai/llm_trace_full_io.jsonl",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.NINETY_DAYS,
        supports_excise=True,
    ),
    # ADF-W0.16 (ADR-0015, 2026-07-13): the following five entries cover
    # artifacts introduced by specs/arch-data-fix.md this session that had
    # no privacy-matrix coverage before this pass.
    SidecarRetentionRule(
        # context_gap_solicitation.py: outbound solicitation draft, human-
        # reviewed/sent via the existing nudge drafts pipeline (never auto-sent).
        artifact_path="nudge/drafts/<solicitation_id>.eml",
        classification=DataClassification.PII,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        # context_gap_reply_import.py: raw, unfiltered inbound stakeholder
        # reply .eml, manually dropped by the operator. Highest-sensitivity
        # new artifact this pass -- unredacted sender + body.
        artifact_path="nudge/replies/<message_id>.eml",
        classification=DataClassification.PII,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        # context_gap_solicitation.py cooldown log: id/fingerprint/timestamp
        # only, no PII (verified against the module's own write call).
        artifact_path="_feedback/context_gap_solicitations.jsonl",
        classification=DataClassification.INTERNAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=False,
    ),
    SidecarRetentionRule(
        # program_synthesis.py: one file per ai_run_id, aggregated CONFIDENTIAL
        # business content only (no person-identifying fields).
        artifact_path="runtime/program_synthesis/<ai_run_id>.json",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=False,
    ),
    SidecarRetentionRule(
        # workstream_registry.yaml: live, operator-authored config file (like
        # program.yaml/editions/*.yaml, not a sidecar), but context_gap_reply.py
        # (Decision 3b, 2026-07-13) can now write verbatim stakeholder reply
        # text into its deep_context fields, which may incidentally carry PII
        # (e.g. a signature block). Full-document overwrite-in-place with a
        # single non-rotating .bak backup -- not a rotating audit log, so the
        # rotating [EXCISED] tombstone mechanism does not apply; the operator
        # can directly edit/redact the field in place instead (same rationale
        # as runtime/gather_state.json below).
        artifact_path="workstream_registry.yaml",
        classification=DataClassification.PII,
        retention=RetentionClass.INDEFINITE,
        supports_excise=False,
    ),
    # ADF-W5.9 (Section 9.7, 2026-07-14): the four raw-telemetry JSONL
    # sidecars this session's ADF work introduced, previously unregistered.
    SidecarRetentionRule(
        # measurement_store.py: routing-decision telemetry, no PII.
        artifact_path="runtime/tier_decisions.jsonl",
        classification=DataClassification.INTERNAL,
        retention=RetentionClass.FORTY_FIVE_DAYS,
        supports_excise=False,
    ),
    SidecarRetentionRule(
        # ai_telemetry.py: provider/latency/cost telemetry, no PII.
        artifact_path="_state/ai_telemetry.jsonl",
        classification=DataClassification.INTERNAL,
        retention=RetentionClass.NINETY_DAYS,
        supports_excise=False,
    ),
    SidecarRetentionRule(
        # run_telemetry.py: per-gather-run channel performance telemetry, no PII.
        artifact_path="runtime/run_telemetry.jsonl",
        classification=DataClassification.INTERNAL,
        retention=RetentionClass.NINETY_DAYS,
        supports_excise=False,
    ),
    SidecarRetentionRule(
        # alerts.py: open alerts never expire regardless of age -- only a
        # RESOLVED alert becomes purge-eligible, 90 days after resolution.
        artifact_path="_alerts/alerts.jsonl",
        classification=DataClassification.INTERNAL,
        retention=RetentionClass.NINETY_DAYS,
        supports_excise=False,
        eligibility_field="resolved_at",
    ),
    SidecarRetentionRule(
        # context_compiler.py: one content-addressed JSON file per compile
        # (runtime/context_manifests/<hash>.json), no PII (evidence ids,
        # token counts, classification labels -- not raw content).
        artifact_path="runtime/context_manifests",
        classification=DataClassification.INTERNAL,
        retention=RetentionClass.NINETY_DAYS,
        supports_excise=False,
        eligibility_field="compiled_at",
        directory_glob="*.json",
    ),
    # specs/people.md PPL-W1.8 (§7.8): the workspace-global registry root's
    # artifacts. Unlike every rule above, these live under the shared
    # `knowledge/` workspace root, not a per-program directory --
    # `<workspace_root>` is a distinct placeholder from `<program_id>`,
    # resolved by `privacy_purge.py::_resolve_artifact_paths` against
    # `get_shared_knowledge_root(programs_root)` instead of
    # `programs_root/<program_id>/`.
    SidecarRetentionRule(
        # people_registry_identity.py: workspace/customer identity + write
        # mode. No PII (IDs and a customer-boundary label only).
        artifact_path="<workspace_root>/registry.yaml",
        classification=DataClassification.INTERNAL,
        retention=RetentionClass.INDEFINITE,
        supports_excise=False,
    ),
    SidecarRetentionRule(
        # people_registry_identity.py: generation/fencing/hash metadata only.
        artifact_path="<workspace_root>/registry_manifest.json",
        classification=DataClassification.INTERNAL,
        retention=RetentionClass.INDEFINITE,
        supports_excise=False,
    ),
    SidecarRetentionRule(
        # people_registry_storage_class.py: storage-class diagnostics only.
        artifact_path="<workspace_root>/registry_capability_status.yaml",
        classification=DataClassification.INTERNAL,
        retention=RetentionClass.INDEFINITE,
        supports_excise=False,
    ),
    SidecarRetentionRule(
        # people_registry_lease.py: force-release audit trail. Classified
        # PII because it names an authenticated_principal (an identity),
        # matching §7.8's "--reveal-pii ... writes a minimal PII-classified
        # audit record naming ... authenticated principal" precedent.
        artifact_path="<workspace_root>/.state/registry_lease_audit.jsonl",
        classification=DataClassification.PII,
        retention=RetentionClass.SEVEN_YEARS,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        # people_change_journal.py: field-level person/team change events
        # (§7.7) -- the highest-sensitivity new artifact this feature
        # introduces once Phase 2a's real schemas replace PPL-W1.4's
        # synthetic placeholder.
        artifact_path="<workspace_root>/_journal/people_changes.jsonl",
        classification=DataClassification.PII,
        retention=RetentionClass.SEVEN_YEARS,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        artifact_path="<workspace_root>/_journal/people_conflicts.jsonl",
        classification=DataClassification.PII,
        retention=RetentionClass.SEVEN_YEARS,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        # PPL-W4.7: one audited record per --apply provider-refresh run.
        # PII since it carries authenticated_principal, same classification
        # rule PPL-W1.8 established for that field.
        artifact_path="<workspace_root>/_journal/people_refresh_telemetry.jsonl",
        classification=DataClassification.PII,
        retention=RetentionClass.SEVEN_YEARS,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        # Rotated, immutable, HMAC-signed segments (archive_signing.py) --
        # same classification as the active stream they were rotated from.
        artifact_path="<workspace_root>/_journal/archive/<year>/people_changes_<end_sequence>.jsonl",
        classification=DataClassification.PII,
        retention=RetentionClass.SEVEN_YEARS,
        supports_excise=True,  # metadata-only excise (file is immutable), matching the archive/manifest precedent above.
    ),
    SidecarRetentionRule(
        artifact_path="<workspace_root>/_journal/archive/<year>/people_conflicts_<end_sequence>.jsonl",
        classification=DataClassification.PII,
        retention=RetentionClass.SEVEN_YEARS,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        artifact_path="<workspace_root>/_journal/archive/<year>/people_refresh_telemetry_<end_sequence>.jsonl",
        classification=DataClassification.PII,
        retention=RetentionClass.SEVEN_YEARS,
        supports_excise=True,
    ),
    SidecarRetentionRule(
        # people_registry_transaction.py: staged (not-yet-committed) or
        # crash-recovered candidate registry data plus a checkpoint copy of
        # the prior live state -- transient, but classified PII since it
        # holds the same content class as the live registry it stages.
        # Deliberately no directory_glob: each transaction directory holds
        # a MIX of YAML data files, a JSON state record, and a checkpoint/
        # subdirectory -- genuinely not the "one content-addressed JSON
        # file per hash with a single timestamp field" shape
        # `_process_content_addressed_directory` purges, and deleting only
        # the files that happen to match a glob would leave the rest
        # orphaned. Its actual lifecycle is governed by
        # `recover_registry_transactions` (PPL-W1.5), not the generic
        # purge engine -- registered here for classification/backup
        # coverage only, matching `run_purge`'s existing "non-JSONL
        # sidecar governed by its own rotation/migration path" no-op
        # bucket (see `workstream_registry.yaml`/`runtime/gather_state.json`
        # above for the same pattern).
        artifact_path="<workspace_root>/.transactions",
        classification=DataClassification.PII,
        retention=RetentionClass.NINETY_DAYS,
        supports_excise=False,
    ),
)


# Retention class → days. Used by `vertex audit query --since-days` filters and
# the WS-18 retention cutoff enforcer.
RETENTION_DAYS: dict[RetentionClass, int | None] = {
    RetentionClass.EPHEMERAL: 0,  # 0 days = do not persist beyond live gather
    RetentionClass.FORTY_FIVE_DAYS: 45,
    RetentionClass.NINETY_DAYS: 90,
    RetentionClass.ONE_YEAR: 365,
    RetentionClass.SEVEN_YEARS: 365 * 7,
    RetentionClass.INDEFINITE: None,  # never auto-delete
}


# Classification ordering (least → most sensitive). Used to assert that a
# write payload does not exceed the channel's `write_default_class`.
CLASSIFICATION_ORDER: dict[DataClassification, int] = {
    DataClassification.PUBLIC: 0,
    DataClassification.INTERNAL: 1,
    DataClassification.CONFIDENTIAL: 2,
    DataClassification.PII: 3,
    DataClassification.SECRET: 4,
}


def classification_at_least(
    candidate: DataClassification,
    floor: DataClassification,
) -> bool:
    """Return True if `candidate` is at least as sensitive as `floor`.

    Used to assert that a payload's classification does not exceed a channel's
    `write_default_class` (i.e. the floor is the maximum the channel accepts).
    """
    return CLASSIFICATION_ORDER[candidate] >= CLASSIFICATION_ORDER[floor]


def channels() -> tuple[Channel, ...]:
    """Stable ordered tuple of every known channel."""
    return tuple(CHANNEL_POSTURE.keys())


def posture_for(channel: Channel) -> ChannelDataPosture:
    return CHANNEL_POSTURE[channel]


def sidecar_rules() -> Iterable[SidecarRetentionRule]:
    return SIDECAR_RETENTION


def known_sidecar_paths() -> frozenset[str]:
    """Frozen set of canonical artifact paths covered by SIDECAR_RETENTION.

    Used by the contract test to ensure every tracked sidecar has a rule.
    """
    return frozenset(rule.artifact_path for rule in SIDECAR_RETENTION)

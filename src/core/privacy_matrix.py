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


# Canonical sidecar retention rules. Source of truth for the journal
# rotation policy (rev. 323) and the WS-18 audit-query retention cutoffs.
SIDECAR_RETENTION: tuple[SidecarRetentionRule, ...] = (
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
)


# Retention class → days. Used by `vertex audit query --since-days` filters and
# the WS-18 retention cutoff enforcer.
RETENTION_DAYS: dict[RetentionClass, int | None] = {
    RetentionClass.EPHEMERAL: 0,  # 0 days = do not persist beyond live gather
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

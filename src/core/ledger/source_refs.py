from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, TypeAlias


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_to_wire(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return _ensure_utc(parsed)


def _parse_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    return date.fromisoformat(value)


def _optional_str(payload: dict[str, Any], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string when present.")
    return value


def _required_str(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value


@dataclass(frozen=True, slots=True)
class OperatorAssertionRef:
    asserted_by: str
    asserted_at: datetime
    context: str | None = None
    vault_hash: str | None = None
    # activation.md §6.15.2 / AG-17 — authenticated operator identity attestation
    # (the trust root behind ``write_authority == "human"``). Minimum v1: the OS
    # principal + machine that executed the approve/edit/revoke, captured
    # immutably in lineage (forge-approval mitigation). ``principal`` is the OS
    # user (getpass.getuser); ``machine`` is the host (platform.node); ``session``
    # is a CLI-process-unique id so two terminals on the same host are distinct.
    # All default None so existing records and headless/test runs round-trip.
    principal: str | None = None
    machine: str | None = None
    session: str | None = None
    ref_type: str = field(init=False, default="operator_assertion")


@dataclass(frozen=True, slots=True)
class ADOWorkItemRef:
    org: str
    project: str
    work_item_id: int
    revision: int | None = None
    url: str | None = None
    vault_hash: str | None = None
    ref_type: str = field(init=False, default="ado_work_item")


@dataclass(frozen=True, slots=True)
class KustoQueryRef:
    cluster: str
    database: str
    query_id: str
    executed_at: datetime
    vault_hash: str | None = None
    ref_type: str = field(init=False, default="kusto_query")


@dataclass(frozen=True, slots=True)
class ManualEntryRef:
    entered_by: str
    entered_at: datetime
    note: str | None = None
    vault_hash: str | None = None
    ref_type: str = field(init=False, default="manual_entry")


@dataclass(frozen=True, slots=True)
class MeetingTranscriptRef:
    meeting_subject: str
    meeting_date: date
    transcript_path: str | None = None
    speaker: str | None = None
    offset: str | None = None
    vault_hash: str | None = None
    ref_type: str = field(init=False, default="meeting_transcript")


@dataclass(frozen=True, slots=True)
class LTDeckRef:
    file_path: str
    deck_date: date
    slide_number: int | None = None
    slide_title: str | None = None
    vault_hash: str | None = None
    ref_type: str = field(init=False, default="lt_deck")


@dataclass(frozen=True, slots=True)
class NewsletterRef:
    file_path: str
    publication_date: date
    issue_number: int | None = None
    section: str | None = None
    vault_hash: str | None = None
    ref_type: str = field(init=False, default="newsletter")


@dataclass(frozen=True, slots=True)
class EmailRef:
    subject: str
    sent_at: datetime
    sender: str
    message_id: str | None = None
    folder: str | None = None
    vault_hash: str | None = None
    # activation.md §6.12 / O-21 — thread-aware dedup: the RFC-2822
    # conversation index (Thread-Index / References / In-Reply-To normalized).
    # When present, two replies in the same thread that assert the *same* fact
    # dedupe even though their body hashes differ ("Thanks!" vs the original).
    # Defaults to None so old records and non-threaded mail round-trip unchanged.
    thread_id: str | None = None
    ref_type: str = field(init=False, default="email")


@dataclass(frozen=True, slots=True)
class TeamsMessageRef:
    posted_at: datetime
    team: str | None = None
    channel: str | None = None
    message_id: str | None = None
    thread_id: str | None = None
    vault_hash: str | None = None
    ref_type: str = field(init=False, default="teams_message")


@dataclass(frozen=True, slots=True)
class SharePointDocRef:
    site: str
    doc_path: str
    version: str | None = None
    modified_at: datetime | None = None
    vault_hash: str | None = None
    ref_type: str = field(init=False, default="sharepoint_doc")


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentRef:
    vault_hash: str
    original_filename: str
    origin_kind: str
    origin_path: str | None = None
    origin_url: str | None = None
    ingested_at: datetime | None = None
    section: str | None = None
    ref_type: str = field(init=False, default="knowledge_document")


@dataclass(frozen=True, slots=True)
class WorkIQRef:
    artifact_id: str
    artifact_kind: str
    retrieved_at: datetime
    query: str | None = None
    vault_hash: str | None = None
    ref_type: str = field(init=False, default="workiq")


@dataclass(frozen=True, slots=True)
class AIInferenceRef:
    model: str
    inputs: tuple[str, ...]
    inference_at: datetime
    prompt_id: str | None = None
    vault_hash: str | None = None
    ref_type: str = field(init=False, default="ai_inference")


SourceRef: TypeAlias = (
    OperatorAssertionRef
    | ADOWorkItemRef
    | KustoQueryRef
    | ManualEntryRef
    | MeetingTranscriptRef
    | LTDeckRef
    | NewsletterRef
    | EmailRef
    | TeamsMessageRef
    | SharePointDocRef
    | KnowledgeDocumentRef
    | WorkIQRef
    | AIInferenceRef
)


_MANDATORY_VAULT_REF_TYPES = frozenset(
    {
        "meeting_transcript",
        "email",
        "teams_message",
        "sharepoint_doc",
        "workiq",
    }
)


_SOURCE_REF_PRIORITY = {
    "operator_assertion": 1,
    "ado_work_item": 2,
    "kusto_query": 2,
    "manual_entry": 3,
    "meeting_transcript": 4,
    "lt_deck": 5,
    "newsletter": 5,
    "email": 6,
    "teams_message": 6,
    "sharepoint_doc": 6,
    "knowledge_document": 6,
    "workiq": 7,
    "ai_inference": 8,
}


def source_ref_priority(source_ref: SourceRef) -> int:
    return _SOURCE_REF_PRIORITY[source_ref.ref_type]


def source_document_key(source_ref: SourceRef) -> str:
    if isinstance(source_ref, OperatorAssertionRef):
        return f"operator_assertion:{source_ref.asserted_by}:{_datetime_to_wire(source_ref.asserted_at)}"
    if isinstance(source_ref, ADOWorkItemRef):
        revision = source_ref.revision if source_ref.revision is not None else "latest"
        return f"ado_work_item:{source_ref.org}:{source_ref.project}:{source_ref.work_item_id}:{revision}"
    if isinstance(source_ref, KustoQueryRef):
        return f"kusto_query:{source_ref.cluster}:{source_ref.database}:{source_ref.query_id}:{_datetime_to_wire(source_ref.executed_at)}"
    if isinstance(source_ref, ManualEntryRef):
        return f"manual_entry:{source_ref.entered_by}:{_datetime_to_wire(source_ref.entered_at)}"
    if isinstance(source_ref, MeetingTranscriptRef):
        transcript_path = source_ref.transcript_path or ""
        return f"meeting_transcript:{source_ref.meeting_subject}:{source_ref.meeting_date.isoformat()}:{transcript_path}"
    if isinstance(source_ref, LTDeckRef):
        slide_number = source_ref.slide_number if source_ref.slide_number is not None else "all"
        return f"lt_deck:{source_ref.file_path}:{source_ref.deck_date.isoformat()}:{slide_number}"
    if isinstance(source_ref, NewsletterRef):
        issue_number = source_ref.issue_number if source_ref.issue_number is not None else source_ref.file_path
        return f"newsletter:{issue_number}:{source_ref.publication_date.isoformat()}"
    if isinstance(source_ref, EmailRef):
        message_id = source_ref.message_id or source_ref.subject
        return f"email:{message_id}:{_datetime_to_wire(source_ref.sent_at)}"
    if isinstance(source_ref, TeamsMessageRef):
        message_id = source_ref.message_id or source_ref.thread_id or "unthreaded"
        return f"teams_message:{message_id}:{_datetime_to_wire(source_ref.posted_at)}"
    if isinstance(source_ref, SharePointDocRef):
        version = source_ref.version or "latest"
        return f"sharepoint_doc:{source_ref.site}:{source_ref.doc_path}:{version}"
    if isinstance(source_ref, KnowledgeDocumentRef):
        section = source_ref.section or "document"
        return f"knowledge_document:{source_ref.vault_hash}:{section}"
    if isinstance(source_ref, WorkIQRef):
        return f"workiq:{source_ref.artifact_kind}:{source_ref.artifact_id}:{_datetime_to_wire(source_ref.retrieved_at)}"
    inputs = ",".join(source_ref.inputs)
    return f"ai_inference:{source_ref.model}:{inputs}:{_datetime_to_wire(source_ref.inference_at)}"


def source_ref_to_dict(source_ref: SourceRef) -> dict[str, Any]:
    payload = asdict(source_ref)
    for key, value in tuple(payload.items()):
        if isinstance(value, datetime):
            payload[key] = _datetime_to_wire(value)
        elif isinstance(value, date):
            payload[key] = value.isoformat()
        elif isinstance(value, tuple):
            payload[key] = list(value)
    return payload


def validate_typed_source_ref(source_ref: SourceRef) -> None:
    try:
        payload = source_ref_to_dict(source_ref)
        source_ref_from_dict(payload)
    except Exception as error:
        raise ValueError("source_ref must be a schema-valid typed SourceRef.") from error
    if source_ref_requires_vault_hash(source_ref):
        vault_hash = getattr(source_ref, "vault_hash", None)
        if not isinstance(vault_hash, str) or not vault_hash.strip():
            raise ValueError("source_ref must include vault_hash for external-origin references.")


def source_ref_requires_vault_hash(source_ref: SourceRef) -> bool:
    return source_ref.ref_type in _MANDATORY_VAULT_REF_TYPES


def source_ref_from_dict(payload: dict[str, Any]) -> SourceRef:
    ref_type = payload.get("ref_type")
    if not isinstance(ref_type, str):
        raise ValueError("source ref payload must include ref_type.")

    if ref_type == "operator_assertion":
        return OperatorAssertionRef(
            asserted_by=_required_str(payload, "asserted_by"),
            asserted_at=_parse_datetime(payload.get("asserted_at"), "asserted_at"),
            context=_optional_str(payload, "context"),
            vault_hash=_optional_str(payload, "vault_hash"),
            principal=_optional_str(payload, "principal"),
            machine=_optional_str(payload, "machine"),
            session=_optional_str(payload, "session"),
        )
    if ref_type == "ado_work_item":
        work_item_id = payload.get("work_item_id")
        revision = payload.get("revision")
        if not isinstance(work_item_id, int) or isinstance(work_item_id, bool):
            raise ValueError("work_item_id must be an integer.")
        if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool)):
            raise ValueError("revision must be an integer when present.")
        return ADOWorkItemRef(
            org=_required_str(payload, "org"),
            project=_required_str(payload, "project"),
            work_item_id=work_item_id,
            revision=revision,
            url=_optional_str(payload, "url"),
            vault_hash=_optional_str(payload, "vault_hash"),
        )
    if ref_type == "kusto_query":
        return KustoQueryRef(
            cluster=_required_str(payload, "cluster"),
            database=_required_str(payload, "database"),
            query_id=_required_str(payload, "query_id"),
            executed_at=_parse_datetime(payload.get("executed_at"), "executed_at"),
            vault_hash=_optional_str(payload, "vault_hash"),
        )
    if ref_type == "manual_entry":
        return ManualEntryRef(
            entered_by=_required_str(payload, "entered_by"),
            entered_at=_parse_datetime(payload.get("entered_at"), "entered_at"),
            note=_optional_str(payload, "note"),
            vault_hash=_optional_str(payload, "vault_hash"),
        )
    if ref_type == "meeting_transcript":
        return MeetingTranscriptRef(
            meeting_subject=_required_str(payload, "meeting_subject"),
            meeting_date=_parse_date(payload.get("meeting_date"), "meeting_date"),
            transcript_path=_optional_str(payload, "transcript_path"),
            speaker=_optional_str(payload, "speaker"),
            offset=_optional_str(payload, "offset"),
            vault_hash=_optional_str(payload, "vault_hash"),
        )
    if ref_type == "lt_deck":
        slide_number = payload.get("slide_number")
        if slide_number is not None and (not isinstance(slide_number, int) or isinstance(slide_number, bool)):
            raise ValueError("slide_number must be an integer when present.")
        return LTDeckRef(
            file_path=_required_str(payload, "file_path"),
            deck_date=_parse_date(payload.get("deck_date"), "deck_date"),
            slide_number=slide_number,
            slide_title=_optional_str(payload, "slide_title"),
            vault_hash=_optional_str(payload, "vault_hash"),
        )
    if ref_type == "newsletter":
        issue_number = payload.get("issue_number")
        if issue_number is not None and (not isinstance(issue_number, int) or isinstance(issue_number, bool)):
            raise ValueError("issue_number must be an integer when present.")
        return NewsletterRef(
            file_path=_required_str(payload, "file_path"),
            publication_date=_parse_date(payload.get("publication_date"), "publication_date"),
            issue_number=issue_number,
            section=_optional_str(payload, "section"),
            vault_hash=_optional_str(payload, "vault_hash"),
        )
    if ref_type == "email":
        return EmailRef(
            subject=_required_str(payload, "subject"),
            sent_at=_parse_datetime(payload.get("sent_at"), "sent_at"),
            sender=_required_str(payload, "sender"),
            message_id=_optional_str(payload, "message_id"),
            folder=_optional_str(payload, "folder"),
            vault_hash=_optional_str(payload, "vault_hash"),
            thread_id=_optional_str(payload, "thread_id"),
        )
    if ref_type == "teams_message":
        return TeamsMessageRef(
            posted_at=_parse_datetime(payload.get("posted_at"), "posted_at"),
            team=_optional_str(payload, "team"),
            channel=_optional_str(payload, "channel"),
            message_id=_optional_str(payload, "message_id"),
            thread_id=_optional_str(payload, "thread_id"),
            vault_hash=_optional_str(payload, "vault_hash"),
        )
    if ref_type == "sharepoint_doc":
        modified_at = payload.get("modified_at")
        return SharePointDocRef(
            site=_required_str(payload, "site"),
            doc_path=_required_str(payload, "doc_path"),
            version=_optional_str(payload, "version"),
            modified_at=_parse_datetime(modified_at, "modified_at") if modified_at is not None else None,
            vault_hash=_optional_str(payload, "vault_hash"),
        )
    if ref_type == "workiq":
        return WorkIQRef(
            artifact_id=_required_str(payload, "artifact_id"),
            artifact_kind=_required_str(payload, "artifact_kind"),
            retrieved_at=_parse_datetime(payload.get("retrieved_at"), "retrieved_at"),
            query=_optional_str(payload, "query"),
            vault_hash=_optional_str(payload, "vault_hash"),
        )
    if ref_type == "knowledge_document":
        ingested_at = payload.get("ingested_at")
        return KnowledgeDocumentRef(
            vault_hash=_required_str(payload, "vault_hash"),
            original_filename=_required_str(payload, "original_filename"),
            origin_kind=_required_str(payload, "origin_kind"),
            origin_path=_optional_str(payload, "origin_path"),
            origin_url=_optional_str(payload, "origin_url"),
            ingested_at=_parse_datetime(ingested_at, "ingested_at") if ingested_at is not None else None,
            section=_optional_str(payload, "section"),
        )
    if ref_type == "ai_inference":
        raw_inputs = payload.get("inputs")
        if not isinstance(raw_inputs, list) or any(not isinstance(item, str) for item in raw_inputs):
            raise ValueError("inputs must be a list of strings.")
        return AIInferenceRef(
            model=_required_str(payload, "model"),
            inputs=tuple(raw_inputs),
            inference_at=_parse_datetime(payload.get("inference_at"), "inference_at"),
            prompt_id=_optional_str(payload, "prompt_id"),
            vault_hash=_optional_str(payload, "vault_hash"),
        )

    raise ValueError(f"Unsupported source ref type: {ref_type}")
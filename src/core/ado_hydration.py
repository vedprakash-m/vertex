from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.core.ado_client import ADOClient
from src.core.ado_enrichment import ADO_RISK_ASSESSMENT_COMMENT_FIELD, ADO_RISK_ASSESSMENT_FIELD, normalize_risk_assessment
from src.core.ado_relations import parse_relations_payload, traverse_relations
from src.core.ado_schema_drift import assert_row_shape
from src.core.config_loader import PROGRAMS_ROOT
from src.core.exceptions import QueryError
from src.core.integration_types import (
    ADOHydrationOutput,
    ChannelConfig,
    ChannelRegistration,
    HydrationMode,
    HydrationResult,
    IntegrationError,
    ProviderCapability,
    DiscoveryCompleteness,
    WorkItemRelation,
)
from src.core.models import Comment, Revision, RiskLevel, WorkItem
from src.core.models_v2 import Program, Workstream


ADO_BATCH_FIELDS = (
    "System.Id",
    "System.WorkItemType",
    "System.Title",
    "System.State",
    "System.AreaPath",
    "System.IterationPath",
    "System.AssignedTo",
    "System.Tags",
    "System.ChangedDate",
    "Microsoft.VSTS.Scheduling.TargetDate",
    ADO_RISK_ASSESSMENT_FIELD,
    ADO_RISK_ASSESSMENT_COMMENT_FIELD,
)
WORK_ITEM_BATCH_SIZE = 200


@dataclass(frozen=True, slots=True)
class ADOHydrationConfig:
    batch_size: int = WORK_ITEM_BATCH_SIZE


class ADOHydrationProvider:
    def __init__(self, client: ADOClient):
        self._client = client

    @classmethod
    def from_program(
        cls,
        program: Program,
        channel_config: ChannelConfig,
        workstreams: tuple[Workstream, ...],
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> tuple["ADOHydrationProvider", ADOHydrationConfig]:
        del workstreams, channel_config, programs_root
        if program.ado is None:
            raise ValueError(f"Program '{program.id}' has no ADO config")
        client = ADOClient(
            organization=program.ado.organization,
            project=program.ado.project,
            timeout=program.ado.api_timeout_seconds,
        )
        return cls(client), ADOHydrationConfig()

    @property
    def channel(self) -> str:
        return "ado"

    @property
    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            channel="ado",
            discovery_modes=(DiscoveryCompleteness.FULL, DiscoveryCompleteness.PARTIAL),
            hydration_modes=(HydrationMode.FULL, HydrationMode.FRESHNESS_ONLY),
            supports_since=True,
            max_batch_size=WORK_ITEM_BATCH_SIZE,
            rate_limit_rpm=None,
            retry_max_attempts=2,
            retry_backoff_seconds=0.5,
            privacy_class="internal_content",
            timeout_seconds=45,
            # ADF-W4.1 (Section 8.4.3): the ADO provider hydrates typed relations.
            supports_relations=True,
        )

    def hydrate(
        self,
        registrations: tuple[ChannelRegistration, ...],
        since: datetime,
        program_id: str,
        config: ADOHydrationConfig,
        mode: HydrationMode = HydrationMode.FULL,
        run_ctx: object = None,
    ) -> HydrationResult[ADOHydrationOutput]:
        del program_id, run_ctx
        work_item_regs = tuple(reg for reg in registrations if reg.ref_kind == "work_item")
        repo_regs = tuple(reg for reg in registrations if reg.ref_kind == "repository")
        _int_ids: list[int] = []
        for reg in work_item_regs:
            _wid = _parse_int(reg.ref_id)
            if _wid is not None:
                _int_ids.append(_wid)
        work_item_ids = tuple(_int_ids)
        if not work_item_ids and not repo_regs:
            return HydrationResult(
                channel="ado",
                resources=ADOHydrationOutput(work_items=(), freshness_items=(), pull_requests=()),
                api_call_count=0,
                errors=(),
                hydrated_ref_ids=(),
                failed_ref_ids=(),
            )

        rows: list[dict[str, Any]] = []
        api_calls = 0
        errors: list[IntegrationError] = []
        failed_ref_ids: list[tuple[str, str]] = []
        hydrated_ref_ids: list[tuple[str, str]] = []

        if work_item_ids:
            try:
                for batch in _chunks(work_item_ids, max(config.batch_size, 1)):
                    rows.extend(self._client.query_work_items_batch(list(batch), ADO_BATCH_FIELDS))
                    api_calls += 1
            except QueryError as error:
                errors.append(
                    IntegrationError(
                        source="ado",
                        stage="hydration",
                        message=str(error),
                        retryable=True,
                        operator_action="Verify Azure DevOps access and retry hydration.",
                    )
                )
                failed_ref_ids.extend((str(work_item_id), "work_item") for work_item_id in work_item_ids)

        items: list[WorkItem] = []
        if work_item_ids and rows:
            # activation.md §6.14.13 — inspect contract drift (non-fatal) so a
            # renamed/removed ADO field alerts the maintainer even when the
            # fail-closed required-field guard is satisfied.
            from src.core.ado_schema_drift import inspect_contract_drift

            inspect_contract_drift(rows, contract_fields=ADO_BATCH_FIELDS)
            rows_by_id = {_row_work_item_id(row): row for row in rows}
            fetched_at = datetime.now(timezone.utc)
            registrations_by_id = {int(reg.ref_id): reg for reg in work_item_regs if reg.ref_id.isdigit()}
            for work_item_id in work_item_ids:
                row = rows_by_id.get(work_item_id)
                if row is None:
                    failed_ref_ids.append((str(work_item_id), "work_item"))
                    continue
                registration = registrations_by_id.get(work_item_id)
                revision_rows: list[dict[str, Any]] = []
                comment_rows: list[dict[str, Any]] = []
                # ADF-W2.2 (Section 8.4.1): "use the last verified watermark,
                # not only a rolling lookback" -- an item already verified
                # more recently than the caller's `since` window only needs
                # re-detailing if it changed after ITS OWN last verification,
                # not merely within the broader window. On a fully-stable
                # program (nothing changed since last gather) this collapses
                # the revision/comment fetch to zero on a repeat run, instead
                # of re-fetching detail for every item touched within the
                # rolling lookback every single time.
                effective_since = since
                if (
                    registration is not None
                    and registration.last_verified_at is not None
                    and registration.last_verified_at > since
                ):
                    effective_since = registration.last_verified_at
                # ADF-W2.2 (Section 8.4.1): "on first registration ... fetch
                # complete bounded revisions and comments" -- a registration
                # that has never been verified before (brand new this cycle,
                # or never successfully hydrated) must get a full detail
                # fetch regardless of the changed-since check: the item's own
                # ChangedDate may predate the gather's rolling `since` window
                # (e.g. a work item created months ago, untouched recently),
                # in which case `_row_changed_since` would otherwise report
                # "unchanged" and silently skip its very first hydration.
                never_verified = registration is None or registration.last_verified_at is None
                if mode is HydrationMode.FULL and (never_verified or _row_changed_since(row, effective_since)):
                    try:
                        revision_rows = self._client.list_work_item_revisions(work_item_id)
                        api_calls += 1
                        comment_loader = getattr(self._client, "list_work_item_comments", None)
                        if callable(comment_loader):
                            comment_rows = comment_loader(work_item_id)
                            api_calls += 1
                    except QueryError as error:
                        errors.append(
                            IntegrationError(
                                source="ado",
                                stage="hydration",
                                message=f"Failed to hydrate work item {work_item_id}: {error}",
                                retryable=True,
                                ref_id=str(work_item_id),
                                ref_kind="work_item",
                            )
                        )
                        failed_ref_ids.append((str(work_item_id), "work_item"))
                        continue
                item = _work_item_from_batch_row(row, revision_rows=revision_rows, comment_rows=comment_rows, fetched_at=fetched_at)
                if registration is not None:
                    item.custom_fields["workstream_ids"] = tuple(registration.workstream_ids)
                items.append(item)
                hydrated_ref_ids.append((str(work_item_id), "work_item"))

        pull_requests_list = []
        if repo_regs:
            from src.core.ado_pr_client import ADOPRClient
            pr_client = ADOPRClient(self._client)
            for reg in repo_regs:
                repo_id = reg.ref_id
                try:
                    prs = pr_client.list_pull_requests(repo_id, top=100)
                    mapped_prs = []
                    for pr in prs:
                        mapped_pr = replace(pr, workstream_ids=tuple(reg.workstream_ids))
                        mapped_prs.append(mapped_pr)
                    pull_requests_list.extend(mapped_prs)
                    api_calls += 1
                    hydrated_ref_ids.append((repo_id, "repository"))
                except Exception as error:
                    errors.append(
                        IntegrationError(
                            source="ado",
                            stage="hydration",
                            message=f"Failed to fetch pull requests for repository {repo_id}: {error}",
                            retryable=True,
                            ref_id=repo_id,
                            ref_kind="repository",
                        )
                    )
                    failed_ref_ids.append((repo_id, "repository"))

        output = ADOHydrationOutput(
            work_items=tuple(items),
            freshness_items=tuple(items),
            pull_requests=tuple(pull_requests_list),
        )
        # ADF-W4.1 (Section 8.4.3): hydrate typed relations in FULL mode only.
        # Depth 1 is the normal-gather budget; deeper investigation is a separate
        # targeted call. A failed relations fetch never blocks the work items
        # already hydrated -- it surfaces as a non-fatal error and empty relations.
        relations: tuple[WorkItemRelation, ...] = ()
        relation_truncation = None
        if mode is HydrationMode.FULL and work_item_ids:
            relation_loader = getattr(self._client, "get_work_item_relations", None)
            if callable(relation_loader):
                try:
                    raw_relations = relation_loader(list(work_item_ids))
                    api_calls += 1
                    parsed = parse_relations_payload(raw_relations)
                    traversal = traverse_relations(
                        parsed,
                        start_ids=list(work_item_ids),
                        max_depth=1,
                    )
                    relations = traversal.edges
                    relation_truncation = traversal.truncation
                except QueryError as error:
                    errors.append(
                        IntegrationError(
                            source="ado",
                            stage="hydration",
                            message=f"Failed to hydrate work item relations: {error}",
                            retryable=True,
                        )
                    )
        output = replace(output, relations=relations, relation_truncation=relation_truncation)
        return HydrationResult(
            channel="ado",
            resources=output,
            api_call_count=api_calls,
            errors=tuple(errors),
            hydrated_ref_ids=tuple(hydrated_ref_ids),
            failed_ref_ids=tuple(failed_ref_ids),
        )


def _chunks(values: tuple[int, ...], size: int) -> tuple[tuple[int, ...], ...]:
    return tuple(tuple(values[index:index + size]) for index in range(0, len(values), size))


def _extract_fields(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("fields")
    if isinstance(raw, dict):
        return raw
    return {}


def _row_work_item_id(row: dict[str, Any]) -> int:
    fields = _extract_fields(row)
    return int(row.get("id") or fields.get("System.Id") or 0)


def _row_changed_since(row: dict[str, Any], since: datetime) -> bool:
    fields = _extract_fields(row)
    changed_at = _parse_datetime(fields.get("System.ChangedDate") or row.get("ChangedDate"))
    return changed_at is None or changed_at >= since


def _work_item_from_batch_row(
    row: dict[str, Any],
    *,
    revision_rows: list[dict[str, Any]],
    comment_rows: list[dict[str, Any]],
    fetched_at: datetime,
) -> WorkItem:
    # activation.md §6.14.13 / O-16 — ADO schema-drift guard: fail closed (when
    # the guard is enabled) if a required identity/state field is absent, so the
    # AG-9 conflict check cannot silently degrade on a vanished System.State.
    assert_row_shape(row)
    fields = _extract_fields(row)
    work_item_id = _row_work_item_id(row)
    assigned_to, assigned_to_email = _parse_identity(fields.get("System.AssignedTo"))
    tags = _parse_tags(fields.get("System.Tags"))
    state = str(fields.get("System.State") or "Active")
    risk_assessment = normalize_risk_assessment(fields.get(ADO_RISK_ASSESSMENT_FIELD))
    custom_fields: dict[str, object] = {}
    changed_date_raw = fields.get("System.ChangedDate")
    if changed_date_raw is not None:
        custom_fields["System.ChangedDate"] = changed_date_raw
    commitment_status_raw = fields.get("Custom.CommitmentStatus")
    if commitment_status_raw is not None:
        custom_fields["Custom.CommitmentStatus"] = str(commitment_status_raw)
    item = WorkItem(
        id=work_item_id,
        type=str(fields.get("System.WorkItemType") or "WorkItem"),
        title=str(fields.get("System.Title") or f"Work Item {work_item_id}"),
        state=state,
        assigned_to=assigned_to,
        assigned_to_email=assigned_to_email,
        area_path=str(fields.get("System.AreaPath") or ""),
        iteration_path=str(fields.get("System.IterationPath") or ""),
        target_date=_parse_date(fields.get("Microsoft.VSTS.Scheduling.TargetDate")),
        risk_level=_infer_risk_level(state, tags, risk_assessment),
        tags=tags,
        custom_fields=custom_fields,
        revisions=_parse_revisions(work_item_id, revision_rows),
        comments=_parse_comments(work_item_id, comment_rows),
        fetched_at=fetched_at,
        risk_assessment=risk_assessment,
        risk_assessment_comment=_optional_string(fields.get(ADO_RISK_ASSESSMENT_COMMENT_FIELD)),
    )
    return item


def _parse_revisions(work_item_id: int, rows: list[dict[str, Any]]) -> list[Revision]:
    revisions: list[Revision] = []
    previous_fields: dict[str, Any] | None = None
    for row in sorted(rows, key=lambda entry: int(entry.get("rev") or 0)):
        fields = _extract_fields(row)
        changed_date = _parse_datetime(fields.get("System.ChangedDate"))
        if changed_date is None:
            continue
        changed_by, changed_by_email = _parse_identity(fields.get("System.ChangedBy"))
        field_changes: dict[str, tuple[str | None, str | None]] = {}
        if previous_fields is not None:
            for key in set(previous_fields) | set(fields):
                prior = _field_value(previous_fields.get(key))
                current = _field_value(fields.get(key))
                if prior != current:
                    field_changes[key] = (prior, current)
        revisions.append(
            Revision(
                work_item_id=work_item_id,
                rev_number=int(row.get("rev") or 0),
                changed_by=changed_by or "Unknown",
                changed_by_email=changed_by_email or "",
                changed_date=changed_date,
                fields_changed=field_changes,
            )
        )
        previous_fields = fields
    return revisions


def _parse_comments(work_item_id: int, rows: list[dict[str, Any]]) -> list[Comment]:
    comments: list[Comment] = []
    for row in rows:
        created_at = _parse_datetime(row.get("publishedDate") or row.get("createdDate"))
        if created_at is None:
            continue
        created_by, created_by_email = _parse_identity(row.get("createdBy"))
        comments.append(
            Comment(
                work_item_id=work_item_id,
                comment_id=int(row.get("id") or row.get("commentId") or 0),
                created_by=created_by or "Unknown",
                created_by_email=created_by_email or "",
                created_date=created_at,
                text=_normalize_ado_comment_html(str(row.get("text") or row.get("renderedText") or "")),
            )
        )
    return comments


_HTML_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_LINK_RE = re.compile(r'<a\b[^>]*\bhref\s*=\s*"([^"]*)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _normalize_ado_comment_html(raw: str) -> str:
    """ADF-W2.1 (Section 8.4.5): strip HTML/presentation markup ADO comments
    carry (``<div>``, ``<span>``, ``<a href>``, ...) before signal
    extraction, while preserving plain-text work-item references, mentions,
    dates, and links -- an ``<a href="URL">text</a>`` becomes
    ``"text (URL)"`` so the link target survives as plain text instead of
    being silently discarded along with the tag.
    """
    if not raw:
        return raw

    without_script_style = _HTML_SCRIPT_STYLE_RE.sub(" ", raw)

    def _link_replacement(match: re.Match[str]) -> str:
        href = html.unescape(match.group(1)).strip()
        link_text = html.unescape(_HTML_TAG_RE.sub("", match.group(2))).strip()
        if not link_text or link_text == href:
            return href
        return f"{link_text} ({href})"

    with_links_preserved = _HTML_LINK_RE.sub(_link_replacement, without_script_style)
    without_tags = _HTML_TAG_RE.sub(" ", with_links_preserved)
    unescaped = html.unescape(without_tags)
    return " ".join(unescaped.split())


def _parse_identity(raw_value: Any) -> tuple[str | None, str | None]:
    if isinstance(raw_value, dict):
        return _optional_string(raw_value.get("displayName")), _optional_string(raw_value.get("uniqueName"))
    return _optional_string(raw_value), None


def _parse_tags(raw_value: Any) -> list[str]:
    if not isinstance(raw_value, str):
        return []
    return [part.strip() for part in raw_value.split(";") if part.strip()]


def _parse_datetime(raw_value: Any) -> datetime | None:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    normalized = raw_value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_date(raw_value: Any) -> date | None:
    if isinstance(raw_value, date) and not isinstance(raw_value, datetime):
        return raw_value
    if isinstance(raw_value, datetime):
        return raw_value.date()
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        return date.fromisoformat(raw_value.strip()[:10])
    except ValueError:
        return None


def _parse_int(raw_value: Any) -> int | None:
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _optional_string(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    return value or None


def _field_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return _optional_string(value.get("displayName")) or _optional_string(value.get("uniqueName")) or str(value)
    return str(value)


def _infer_risk_level(state: str, tags: list[str], risk_assessment: str | None) -> RiskLevel:
    del risk_assessment
    normalized_state = state.strip().lower()
    normalized_tags = {tag.strip().lower() for tag in tags}
    if normalized_state in {"closed", "done", "resolved", "removed"}:
        return RiskLevel.LOW
    if {"blocked", "blocker", "high risk"} & normalized_tags:
        return RiskLevel.HIGH
    if {"risk", "at risk"} & normalized_tags:
        return RiskLevel.MEDIUM
    return RiskLevel.UNKNOWN

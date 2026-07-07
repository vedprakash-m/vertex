from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any, Callable, Mapping
import yaml

from src.commands.doctor_checks.models import ADOProbeResult, DoctorCheck
from src.core.ado_client import ADOClient, ADO_RESOURCE
from src.core.exceptions import AuthError, ConfigError, QueryError, QueryTimeoutError
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.query_builder import build_odata_filter
from src.core.snapshot_store import read_snapshot
from src.core.archive_store import find_latest_confirmed_entry
from src.m365.agency_bridge import AgencyCapabilities
from src.core.overrides_store import merge_overrides, save_overrides


def load_milestone_owner_aliases(program_id: str, *, programs_root: Path) -> tuple[str, ...]:
    shared_people_path = get_shared_knowledge_root(programs_root) / "people_directory.yaml"
    program_people_path = programs_root / program_id / "knowledge" / "people_directory.yaml"
    for path in (shared_people_path, program_people_path):
        if not path.exists():
            continue
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as error:
            raise ConfigError(f"Invalid YAML in {path}: {error}") from error
        people = document.get("people") or []
        if not isinstance(people, list):
            raise ConfigError(f"Expected 'people' list in {path}.")
        aliases: list[str] = []
        for index, entry in enumerate(people, start=1):
            if not isinstance(entry, dict):
                raise ConfigError(f"Person entry #{index} in {path} must be a mapping.")
            alias = str(entry.get("alias") or "").strip()
            if not alias:
                raise ConfigError(f"Person entry #{index} in {path} is missing alias.")
            aliases.append(alias)
        return tuple(sorted(set(aliases)))
    return ()


def probe_ado_access(bundle: Any) -> ADOProbeResult:
    try:
        client = ADOClient(
            organization=bundle.config.ado.organization,
            project=bundle.config.ado.project,
            timeout=bundle.config.ado.api_timeout_seconds or 30,
        )
        since = datetime.now(timezone.utc) - timedelta(days=bundle.config.ado.date_window_days)
        rows = client.query_all(
            filter_expression=build_odata_filter(
                area_paths=bundle.config.ado.area_paths,
                work_item_types=bundle.config.ado.work_item_types,
                since=since,
                states_excluded=bundle.config.ado.excluded_states,
            ),
            select_fields=("WorkItemId",),
            top=50,
        )
        token_minutes_remaining = None
        credential = client._credential
        if credential is not None:
            token = credential.get_token(ADO_RESOURCE)
            token_minutes_remaining = max(int((token.expires_on - datetime.now(timezone.utc).timestamp()) // 60), 0)
        detail = (
            f"{bundle.config.ado.organization}/{bundle.config.ado.project} reachable "
            f"(auth: {client.auth_method}, {len(rows)} sampled items in scope)"
        )
        return ADOProbeResult(True, client.auth_method, len(rows), token_minutes_remaining, detail)
    except (AuthError, QueryError, QueryTimeoutError) as error:
        return ADOProbeResult(False, "unknown", None, None, f"ADO probe failed: {error}")


def token_check(probe_result: ADOProbeResult) -> DoctorCheck:
    if not probe_result.reachable:
        return DoctorCheck("Token", "fail", "Credential check skipped because ADO access failed.")
    if probe_result.token_minutes_remaining is None:
        return DoctorCheck("Token", "ok", f"Credential active via {probe_result.auth_method}.")
    if probe_result.token_minutes_remaining < 10:
        return DoctorCheck("Token", "warn", f"Azure token expires in {probe_result.token_minutes_remaining} min")
    return DoctorCheck("Token", "ok", f"Azure token valid (expires in {probe_result.token_minutes_remaining} min)")


def mail_preview_check(
    *,
    environ: Mapping[str, str] | None = None,
    find_spec_fn: Callable[[str], object | None] | None = None,
) -> DoctorCheck:
    env = os.environ if environ is None else environ
    resolver = find_spec_fn
    if resolver is None:
        from importlib.util import find_spec as _find_spec  # noqa: PLC0415

        resolver = _find_spec
    missing_env: list[str] = []
    if not env.get("GRAPH_TENANT_ID", "").strip():
        missing_env.append("GRAPH_TENANT_ID")
    if not env.get("GRAPH_CLIENT_ID", "").strip():
        missing_env.append("GRAPH_CLIENT_ID")
    if missing_env:
        joined = ", ".join(missing_env)
        return DoctorCheck("Mail Preview", "warn", f"--send-draft unavailable; missing {joined}.")
    if resolver("azure.identity") is None:
        return DoctorCheck(
            "Mail Preview",
            "warn",
            "--send-draft unavailable; missing azure-identity. Run: pip install -r requirements.txt",
        )
    if resolver("requests") is None:
        return DoctorCheck("Mail Preview", "warn", "--send-draft unavailable; requests is missing from the environment.")
    return DoctorCheck(
        "Mail Preview",
        "ok",
        "Graph preview-send prerequisites are present; use `vertex draft --edition myprogram_weekly --dry-run --send-draft` for Outlook validation.",
    )


def agency_cli_check(caps: AgencyCapabilities) -> DoctorCheck:
    if not caps.available:
        return DoctorCheck("Agency CLI", "warn", "Agency CLI unavailable; WorkIQ, Bluebird, and IcM probes are disabled.")

    servers: list[str] = []
    if caps.has_workiq:
        servers.append("workiq")
    if caps.has_bluebird:
        servers.append("bluebird")
    if caps.has_ado:
        servers.append("ado")
    if caps.has_icm:
        servers.append("icm")

    if not servers:
        return DoctorCheck("Agency CLI", "warn", f"Agency CLI reachable (tier: {caps.tier}) but no known MCP servers were detected.")

    return DoctorCheck("Agency CLI", "ok", f"Agency CLI reachable (tier: {caps.tier}; servers: {', '.join(servers)}).")


def latest_snapshot_check(edition: str, archive_root_path: Path, archive_index: Any) -> DoctorCheck:
    latest_entry = find_latest_confirmed_entry(archive_index)
    if latest_entry is None:
        return DoctorCheck("Snapshots", "warn", "No confirmed snapshots yet.")
    latest_issue = latest_entry.issue_number
    snapshot_path = archive_root_path / "snapshots" / f"issue_{latest_issue:03d}.snapshot.json"
    if not snapshot_path.exists():
        return DoctorCheck("Snapshots", "fail", f"Missing latest snapshot for issue {latest_issue:03d}.")
    snapshot = read_snapshot(snapshot_path)
    return DoctorCheck("Snapshots", "ok", f"Last confirmed: Issue {latest_issue} ({snapshot.generated_at.date().isoformat()})")


def seed_overrides(edition: str, bundle: Any, reports_root: Path) -> Path:
    expected_scorecards = {
        scorecard.name: tuple(dimension.name for dimension in scorecard.dimensions)
        for scorecard in bundle.config.scorecards
    }
    document, _ = merge_overrides(issue_number=1, expected_scorecards=expected_scorecards, existing=None)
    return save_overrides(edition, document, reports_root=reports_root)


def template_check(templates_root: Path) -> DoctorCheck:
    base_template = templates_root / "base.email.j2"
    partials_dir = templates_root / "partials"
    partial_count = len(list(partials_dir.glob("*.j2"))) if partials_dir.exists() else 0
    if not base_template.exists():
        return DoctorCheck("Templates", "fail", "base.email.j2 is missing.")
    return DoctorCheck("Templates", "ok", f"base.email.j2 + {partial_count} partials present")


def template_contract_edition_check(bundle: Any, *, edition_name: str, program_id: str | None, programs_root: Path) -> DoctorCheck:
    resolved_program_id = program_id or "<unknown program>"
    edition_type = bundle.config.edition.type
    contract = bundle.template_contract
    contract_path = programs_root / resolved_program_id / "template_contract.yaml"

    if contract is None:
        return DoctorCheck(
            "Template Contract",
            "warn",
            f"programs/{resolved_program_id}/template_contract.yaml is missing; edition '{edition_name}' cannot validate type '{edition_type}' against edition_family.allowed.",
        )

    if edition_type not in contract.allowed_families:
        return DoctorCheck(
            "Template Contract",
            "fail",
            (
                f"programs/{resolved_program_id}/template_contract.yaml does not allow edition type '{edition_type}' for '{edition_name}'. "
                f"Allowed: {', '.join(contract.allowed_families)}."
            ),
            metadata={
                "edition_type": edition_type,
                "edition_name": edition_name,
                "allowed_families": list(contract.allowed_families),
                "contract_path": str(contract_path),
            },
        )

    return DoctorCheck(
        "Template Contract",
        "ok",
        f"programs/{resolved_program_id}/template_contract.yaml allows edition type '{edition_type}' for '{edition_name}'.",
        metadata={
            "edition_type": edition_type,
            "edition_name": edition_name,
            "allowed_families": list(contract.allowed_families),
            "contract_path": str(contract_path),
        },
    )

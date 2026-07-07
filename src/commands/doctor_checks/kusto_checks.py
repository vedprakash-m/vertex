from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import AuthError, ConfigError, QueryError
from src.core.kusto_client import KustoClient
from src.core.kusto_query_loader import load_kpi_queries
from src.core.kusto_templates import KustoTemplateContext, render_kusto_query
from src.core.models_v2 import KustoQuery


_ICM_KUSTO_CLUSTER = "https://icmcluster.kusto.windows.net"


def run_kusto_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    kusto_probe: Callable[[KustoQuery], None] | None,
    live_kusto_probe_fn: Callable[[], Callable[[KustoQuery], None]],
) -> DoctorReport:
    resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Kusto Queries", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    program_id = resolved.paths.program_id
    ado = resolved.program.ado
    try:
        queries = load_doctor_kusto_queries(
            program_id,
            template_context=KustoTemplateContext(
                program_id=resolved.program.id,
                area_paths=ado.area_paths if ado is not None else (),
                date_window_days=ado.date_window_days if ado is not None else None,
            ),
            direct_queries=resolved.program.kusto.queries if resolved.program.kusto is not None else (),
            programs_root=programs_root,
        )
    except ConfigError as error:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Kusto Queries", "fail", str(error)),),
        )

    if not queries:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Kusto Queries", "warn", f"No applicable Kusto queries are configured for program '{program_id}'."),),
        )

    targets = kusto_target_labels(queries)
    query_metadata = {
        "query_count": len(queries),
        "query_ids": [query.id for query in queries],
        "cluster_targets": list(targets),
    }

    problems = validate_kusto_query_definitions(queries)
    if problems:
        detail = "; ".join(problems[:2])
        if len(problems) > 2:
            detail = f"{detail}; +{len(problems) - 2} more"
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Kusto Queries", "fail", detail),),
        )

    query_label = "query" if len(queries) == 1 else "queries"
    checks = [
        DoctorCheck(
            "Kusto Queries",
            "ok",
            f"Loaded {len(queries)} applicable Kusto {query_label} for program '{program_id}' across {summarize_kusto_targets(targets)}; required fields present.",
            metadata=query_metadata,
        )
    ]
    validation_check = kusto_validation_check(queries)
    if validation_check is not None:
        checks.append(validation_check)
    freshness_check = kusto_freshness_check(queries)
    if freshness_check is not None:
        checks.append(freshness_check)
    icm_check = icm_kusto_check(queries)
    if icm_check is not None:
        checks.append(icm_check)

    if resolved.program.kusto is None or not resolved.program.kusto.enabled:
        checks.append(
            DoctorCheck(
                "Kusto Probe",
                "warn",
                f"programs/{program_id}/program.yaml has kusto.enabled=false; skipped live probe for {len(queries)} configured {query_label}.",
                metadata=query_metadata,
            )
        )
        return DoctorReport(edition=edition_name, checks=tuple(checks))

    failures = probe_kusto_queries(queries, probe=kusto_probe or live_kusto_probe_fn())
    if failures:
        detail = "; ".join(failures[:2])
        if len(failures) > 2:
            detail = f"{detail}; +{len(failures) - 2} more"
        checks.append(DoctorCheck("Kusto Probe", "fail", f"Probe failures: {detail}", metadata=query_metadata))
    else:
        checks.append(
            DoctorCheck(
                "Kusto Probe",
                "ok",
                f"Validated {len(queries)} {query_label} across {summarize_kusto_targets(targets)} with lightweight take-0 execution.",
                metadata=query_metadata,
            )
        )

    return DoctorReport(edition=edition_name, checks=tuple(checks))


def load_doctor_kusto_queries(
    program_id: str,
    *,
    template_context: KustoTemplateContext,
    direct_queries: tuple[KustoQuery, ...],
    programs_root: Path,
) -> tuple[KustoQuery, ...]:
    merged: dict[str, KustoQuery] = {
        query.id: query
        for query in load_kpi_queries(program_id, programs_root=programs_root)
        if query.engine == "kusto" and kusto_query_applies_to_program(query, program_id)
    }
    for query in direct_queries:
        if kusto_query_applies_to_program(query, program_id):
            merged[query.id] = query
    return tuple(render_kusto_query(query, context=template_context) for query in merged.values())


def kusto_validation_check(queries: tuple[KustoQuery, ...]) -> DoctorCheck | None:
    probe_eligible = tuple(query for query in queries if is_kusto_probe_eligible(query))
    unvalidated = tuple(query.id for query in probe_eligible if not query.validated)
    metadata = {
        "query_count": len(queries),
        "probe_eligible_query_ids": [query.id for query in probe_eligible],
        "probe_eligible_query_count": len(probe_eligible),
        "excluded_query_ids": [query.id for query in queries if not is_kusto_probe_eligible(query)],
        "unvalidated_query_ids": list(unvalidated),
        "validated_query_ids": [query.id for query in probe_eligible if query.validated],
    }
    if not probe_eligible:
        return DoctorCheck(
            "Kusto Validation",
            "ok",
            "No probe-eligible Kusto queries apply to this program.",
            metadata=metadata,
        )

    if not unvalidated:
        return DoctorCheck(
            "Kusto Validation",
            "ok",
            f"All {len(probe_eligible)} probe-eligible Kusto quer{'y' if len(probe_eligible) == 1 else 'ies'} are marked validated.",
            metadata=metadata,
        )

    detail = ", ".join(unvalidated[:3])
    if len(unvalidated) > 3:
        detail = f"{detail}, +{len(unvalidated) - 3} more"
    return DoctorCheck(
        "Kusto Validation",
        "warn",
        f"{len(unvalidated)} probe-eligible Kusto quer{'y is' if len(unvalidated) == 1 else 'ies are'} still marked validated=false ({detail}).",
        metadata=metadata,
    )


def kusto_freshness_check(queries: tuple[KustoQuery, ...]) -> DoctorCheck | None:
    wired_queries = tuple(query for query in queries if query.refresh_on_gather)
    if not wired_queries:
        return None

    current_time = datetime.now(timezone.utc)
    stale_query_ids: list[str] = []
    missing_query_ids: list[str] = []
    for query in wired_queries:
        if query.validated_at is None:
            missing_query_ids.append(query.id)
            continue
        age = current_time - query.validated_at.astimezone(timezone.utc)
        if age > timedelta(days=7):
            stale_query_ids.append(query.id)

    metadata = {
        "wired_query_ids": [query.id for query in wired_queries],
        "stale_query_ids": stale_query_ids,
        "missing_query_ids": missing_query_ids,
        "freshness_window_days": 7,
    }
    if not stale_query_ids and not missing_query_ids:
        return DoctorCheck(
            "Kusto Freshness",
            "ok",
            f"All {len(wired_queries)} wired Kusto quer{'y' if len(wired_queries) == 1 else 'ies'} succeeded within the last 7 days.",
            metadata=metadata,
        )

    details: list[str] = []
    if missing_query_ids:
        detail = ", ".join(missing_query_ids[:3])
        if len(missing_query_ids) > 3:
            detail = f"{detail}, +{len(missing_query_ids) - 3} more"
        details.append(f"no successful gather recorded for {detail}")
    if stale_query_ids:
        detail = ", ".join(stale_query_ids[:3])
        if len(stale_query_ids) > 3:
            detail = f"{detail}, +{len(stale_query_ids) - 3} more"
        details.append(f"stale >7d for {detail}")

    return DoctorCheck(
        "Kusto Freshness",
        "warn",
        "; ".join(details),
        metadata=metadata,
    )


def icm_kusto_check(queries: tuple[KustoQuery, ...]) -> DoctorCheck | None:
    icm_queries = tuple(query for query in queries if is_icm_kusto_query(query))
    if not icm_queries:
        return None

    cluster_targets = sorted({f"{query.cluster}/{query.database}" for query in icm_queries})
    unvalidated = tuple(query.id for query in icm_queries if not query.validated)
    metadata = {
        "query_count": len(icm_queries),
        "icm_query_ids": [query.id for query in icm_queries],
        "unvalidated_query_ids": list(unvalidated),
        "cluster_targets": cluster_targets,
    }
    query_label = "query" if len(icm_queries) == 1 else "queries"
    if not unvalidated:
        return DoctorCheck(
            "IcM via Kusto",
            "ok",
            f"{len(icm_queries)} applicable IcM-via-Kusto {query_label} target {', '.join(cluster_targets)} and are marked validated.",
            metadata=metadata,
        )

    detail = ", ".join(unvalidated[:3])
    if len(unvalidated) > 3:
        detail = f"{detail}, +{len(unvalidated) - 3} more"
    return DoctorCheck(
        "IcM via Kusto",
        "warn",
        f"{len(icm_queries)} applicable IcM-via-Kusto {query_label} target {', '.join(cluster_targets)}, but {len(unvalidated)} remain validated=false ({detail}).",
        metadata=metadata,
    )


def is_icm_kusto_query(query: KustoQuery) -> bool:
    cluster = query.cluster.strip().lower().rstrip("/")
    return cluster == _ICM_KUSTO_CLUSTER.removesuffix("/")


def is_kusto_probe_eligible(query: KustoQuery) -> bool:
    return query.refresh_on_gather and not is_icm_kusto_query(query)


def kusto_target_labels(queries: tuple[KustoQuery, ...]) -> tuple[str, ...]:
    return tuple(
        sorted({f"{query.cluster.strip().rstrip('/')}/{query.database.strip()}" for query in queries})
    )


def summarize_kusto_targets(targets: tuple[str, ...]) -> str:
    if not targets:
        return "0 cluster/database targets"
    if len(targets) == 1:
        return f"1 cluster/database target ({targets[0]})"
    if len(targets) == 2:
        return f"2 cluster/database targets ({targets[0]}, {targets[1]})"
    return f"{len(targets)} cluster/database targets ({targets[0]}, {targets[1]}, +{len(targets) - 2} more)"


def kusto_query_applies_to_program(query: KustoQuery, program_id: str) -> bool:
    return not query.program_ids or program_id in query.program_ids


def validate_kusto_query_definitions(queries: tuple[KustoQuery, ...]) -> tuple[str, ...]:
    problems: list[str] = []
    for query in queries:
        query_label = query.id or "<missing id>"
        if not query.id.strip():
            problems.append("A configured Kusto query is missing its id.")
        if not query.cluster.strip():
            problems.append(f"Kusto query '{query_label}' is missing cluster.")
        if not query.database.strip():
            problems.append(f"Kusto query '{query_label}' is missing database.")
        if not query.kql.strip():
            problems.append(f"Kusto query '{query_label}' is missing kql.")
    return tuple(problems)


def build_live_kusto_probe() -> Callable[[KustoQuery], None]:
    client = KustoClient()

    def probe(query: KustoQuery) -> None:
        client.execute(query.cluster, query.database, build_kusto_probe_kql(query.kql))

    return probe


def probe_kusto_queries(
    queries: tuple[KustoQuery, ...],
    *,
    probe: Callable[[KustoQuery], None],
) -> tuple[str, ...]:
    failures: list[str] = []
    for query in queries:
        try:
            probe(query)
        except AuthError as error:
            failures.append(f"{query.id}: {error}")
            break
        except QueryError as error:
            failures.append(f"{query.id}: {error}")
            if len(failures) >= 2:
                break
    return tuple(failures)


def build_kusto_probe_kql(kql: str) -> str:
    normalized = kql.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    return f"{normalized}\n| take 0"

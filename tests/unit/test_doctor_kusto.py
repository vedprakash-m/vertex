from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.commands.doctor_checks.kusto_checks import kusto_freshness_check as _kusto_freshness_check, kusto_validation_check as _kusto_validation_check
from src.core.models_v2 import KustoQuery


def test_kusto_validation_check_warns_on_unvalidated_queries() -> None:
    check = _kusto_validation_check(
        (
            KustoQuery(
                id="acme-deployment-p50-p90",
                cluster="https://adventure.kusto.windows.net",
                database="adventure",
                kql="StormEvents | take 1",
                section="Deployment Velocity",
                render_as="metric",
                confidence="high",
                validated=False,
                refresh_on_gather=True,
            ),
            KustoQuery(
                id="acme-fleet-health",
                cluster="https://adventure.kusto.windows.net",
                database="adventure",
                kql="StormEvents | take 1",
                section="Fleet Health",
                render_as="metric",
                confidence="high",
                validated=True,
                refresh_on_gather=True,
            ),
        )
    )

    assert check is not None
    assert check.status == "warn"
    assert "validated=false" in check.detail
    assert check.metadata == {
        "query_count": 2,
        "probe_eligible_query_ids": ["acme-deployment-p50-p90", "acme-fleet-health"],
        "probe_eligible_query_count": 2,
        "excluded_query_ids": [],
        "unvalidated_query_ids": ["acme-deployment-p50-p90"],
        "validated_query_ids": ["acme-fleet-health"],
    }


def test_kusto_validation_check_excludes_icm_and_non_refresh_queries() -> None:
    check = _kusto_validation_check(
        (
            KustoQuery(
                id="acme-eligible",
                cluster="https://adventure.kusto.windows.net",
                database="adventure",
                kql="StormEvents | take 1",
                section="Fleet Health",
                render_as="metric",
                confidence="high",
                validated=False,
                refresh_on_gather=True,
            ),
            KustoQuery(
                id="acme-placeholder",
                cluster="https://adventure.kusto.windows.net",
                database="adventure",
                kql="StormEvents | take 1",
                section="Placeholder",
                render_as="metric",
                confidence="high",
                validated=False,
                refresh_on_gather=False,
            ),
            KustoQuery(
                id="acme-icm",
                cluster="https://icmcluster.kusto.windows.net",
                database="IcMDataWarehouse",
                kql="StormEvents | take 1",
                section="Incidents",
                render_as="metric",
                confidence="high",
                validated=False,
                refresh_on_gather=True,
            ),
        )
    )

    assert check is not None
    assert check.status == "warn"
    assert check.metadata == {
        "query_count": 3,
        "probe_eligible_query_ids": ["acme-eligible"],
        "probe_eligible_query_count": 1,
        "excluded_query_ids": ["acme-placeholder", "acme-icm"],
        "unvalidated_query_ids": ["acme-eligible"],
        "validated_query_ids": [],
    }


def test_kusto_freshness_check_warns_on_stale_and_missing_validations() -> None:
    now = datetime.now(timezone.utc)
    check = _kusto_freshness_check(
        (
            KustoQuery(
                id="acme-stale",
                cluster="https://adventure.kusto.windows.net",
                database="adventure",
                kql="StormEvents | take 1",
                section="Deployment Velocity",
                render_as="metric",
                confidence="high",
                refresh_on_gather=True,
                validated_at=now - timedelta(days=8),
            ),
            KustoQuery(
                id="acme-missing",
                cluster="https://adventure.kusto.windows.net",
                database="adventure",
                kql="StormEvents | take 1",
                section="Fleet Health",
                render_as="metric",
                confidence="high",
                refresh_on_gather=True,
                validated_at=None,
            ),
        )
    )

    assert check is not None
    assert check.status == "warn"
    assert "no successful gather recorded for acme-missing" in check.detail
    assert "stale >7d for acme-stale" in check.detail
    assert check.metadata == {
        "wired_query_ids": ["acme-stale", "acme-missing"],
        "stale_query_ids": ["acme-stale"],
        "missing_query_ids": ["acme-missing"],
        "freshness_window_days": 7,
    }

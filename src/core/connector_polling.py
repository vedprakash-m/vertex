"""FR-SG-48: Poll external connectors and persist ExternalDependency records."""

from __future__ import annotations

import logging
from pathlib import Path

from src.core.connector_config import ExternalConnectorConfig
from src.core.external_connector import make_connector
from src.core.external_dependency import ExternalDependency, save_external_dependency
from src.core.config_loader import PROGRAMS_ROOT

_log = logging.getLogger(__name__)


def poll_and_save_external_connectors(
    program_id: str,
    configs: tuple[ExternalConnectorConfig, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> list[ExternalDependency]:
    """Poll all configured external connectors and persist results.

    Errors per-connector are logged and skipped; does not abort the run.
    Returns the list of successfully polled ExternalDependency records.
    """
    results: list[ExternalDependency] = []
    for cfg in configs:
        try:
            connector = make_connector(cfg)
            dep = connector.poll()
            save_external_dependency(program_id, dep, programs_root=programs_root)
            results.append(dep)
            _log.debug("external_connector polled dep_id=%s connector=%s", cfg.dep_id, cfg.connector_type)
        except NotImplementedError as exc:
            _log.info("external_connector skipped dep_id=%s: %s", cfg.dep_id, exc)
        except Exception as exc:  # noqa: BLE001
            _log.warning("external_connector error dep_id=%s: %s", cfg.dep_id, exc)
    return results

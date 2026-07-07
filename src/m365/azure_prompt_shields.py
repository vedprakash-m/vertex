"""Azure AI Content Safety — PromptShields adapter (Zone C / M365).

Production adapter for S-10b. Requires operator IT provisioning (S-10a):
- ``AZURE_CONTENT_SAFETY_ENDPOINT``  e.g. https://{name}.cognitiveservices.azure.com
- ``AZURE_CONTENT_SAFETY_KEY``       Ocp-Apim-Subscription-Key header value

When the endpoint is unavailable or not configured:
  - Every chunk's ``external_verdict`` is ``unavailable`` with a ``degrade_reason``.
  - ``shield_degrade=True`` is recorded on the run for operator surfacing.
  - This is a *visible degrade*, never a silent block.

Zone C: This module may import from ``src.core`` (Protocol only).
``src.core`` must **not** import from this module (zone boundary contract).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from src.core.rev.entity_types import EntityType
from src.core.rev.ports import Chunk
from src.core.rev.privacy import run_local_checks
from src.core.rev.prompt_shields import (
    VERDICT_CLEAN,
    VERDICT_FLAGGED,
    VERDICT_UNAVAILABLE,
    ChunkShieldResult,
)
from src.core.rev.result import PortResult, Success

log = logging.getLogger(__name__)

_API_VERSION = "2024-02-15-preview"
_SHIELD_PATH = "/contentsafety/text:shieldPrompt"
_DEFAULT_TIMEOUT_SECONDS = 10

AZURE_POLICY_VERSION = "prompt_shields.azure.v1"


@dataclass(frozen=True, slots=True)
class AzureShieldConfig:
    """Runtime config for ``AzurePromptShields``."""

    endpoint: str      # e.g. https://myresource.cognitiveservices.azure.com
    api_key: str       # Ocp-Apim-Subscription-Key value
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS


def load_azure_shield_config(
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> AzureShieldConfig | None:
    """Read config from explicit args or env vars. Returns ``None`` if unconfigured."""
    ep = (endpoint or os.environ.get("AZURE_CONTENT_SAFETY_ENDPOINT", "")).strip()
    key = (api_key or os.environ.get("AZURE_CONTENT_SAFETY_KEY", "")).strip()
    if not ep or not key:
        return None
    return AzureShieldConfig(endpoint=ep.rstrip("/"), api_key=key, timeout_seconds=timeout_seconds)


def _call_shield_api(config: AzureShieldConfig, user_prompt: str) -> bool:
    """POST to Azure AI Content Safety shieldPrompt API.

    Returns ``True`` if an attack was detected, ``False`` for clean.
    Raises ``urllib.error.HTTPError`` / ``OSError`` on failure — caller degrades.
    """
    url = f"{config.endpoint}{_SHIELD_PATH}?api-version={_API_VERSION}"
    body = json.dumps({"userPrompt": user_prompt, "documents": []}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": config.api_key,
        },
    )
    with urllib.request.urlopen(req, timeout=config.timeout_seconds) as resp:
        result: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
    return bool(result.get("userPromptAnalysis", {}).get("attackDetected", False))


class AzurePromptShields:
    """Azure AI Content Safety PromptShields adapter (S-10b).

    When the Azure endpoint is reachable, each chunk is scanned with the
    ``shieldPrompt`` API and receives ``VERDICT_CLEAN`` or ``VERDICT_FLAGGED``.

    When unavailable (endpoint unconfigured, network failure, HTTP error),
    the adapter falls back to local-only checks with ``VERDICT_UNAVAILABLE``
    and a ``degrade_reason`` so the evidence metadata and ``doctor --rev-health``
    surface the degrade.  Unavailability is **never** treated as "complete"
    (``shield_degrade=False`` would imply Azure confirmed clean — it didn't).

    Local checks always run first and are fail-closed on credentials.
    """

    policy_version = AZURE_POLICY_VERSION

    def __init__(
        self,
        *,
        config: AzureShieldConfig | None = None,
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._config = config if config is not None else load_azure_shield_config()
        self._max_chunk_bytes = max_chunk_bytes
        if self._config is None:
            log.warning(
                "AzurePromptShields: AZURE_CONTENT_SAFETY_ENDPOINT or "
                "AZURE_CONTENT_SAFETY_KEY not set — local-only degrade mode active. "
                "Provision Azure AI Content Safety (S-10a) to enable the full classifier."
            )

    def scan_chunks(
        self,
        chunks: tuple[Chunk, ...],
        *,
        source_type: EntityType,
        correlation_id: str,
    ) -> PortResult[tuple[ChunkShieldResult, ...]]:
        if not chunks:
            return Success(())
        results: list[ChunkShieldResult] = []
        for chunk in chunks:
            # Always run local checks first (credential / size gate).
            local = run_local_checks(
                chunk.text,
                source_type=source_type,
                max_bytes=self._max_chunk_bytes,
            )
            if not local.passed:
                # Local check blocked — skip external scan (chunk already denied).
                results.append(ChunkShieldResult(
                    chunk_id=chunk.chunk_id,
                    local_check=local,
                    external_verdict=VERDICT_UNAVAILABLE,
                    degrade_reason="local_check_blocked:skipping_external_scan",
                    policy_version=self.policy_version,
                ))
                continue

            if self._config is None:
                results.append(ChunkShieldResult(
                    chunk_id=chunk.chunk_id,
                    local_check=local,
                    external_verdict=VERDICT_UNAVAILABLE,
                    degrade_reason="azure_content_safety_not_configured:s10a_pending",
                    policy_version=self.policy_version,
                ))
                continue

            # Attempt Azure API call; degrade on any failure.
            try:
                attack_detected = _call_shield_api(self._config, chunk.text)
                verdict = VERDICT_FLAGGED if attack_detected else VERDICT_CLEAN
                results.append(ChunkShieldResult(
                    chunk_id=chunk.chunk_id,
                    local_check=local,
                    external_verdict=verdict,
                    degrade_reason="",
                    policy_version=self.policy_version,
                ))
            except urllib.error.HTTPError as exc:
                degrade_reason = f"azure_http_error:{exc.code}"
                log.warning(
                    "AzurePromptShields: HTTP %s scanning chunk %s (correlation=%s) — degrading",
                    exc.code, chunk.chunk_id, correlation_id,
                )
                results.append(ChunkShieldResult(
                    chunk_id=chunk.chunk_id,
                    local_check=local,
                    external_verdict=VERDICT_UNAVAILABLE,
                    degrade_reason=degrade_reason,
                    policy_version=self.policy_version,
                ))
            except Exception as exc:  # noqa: BLE001
                degrade_reason = f"azure_unavailable:{type(exc).__name__}"
                log.warning(
                    "AzurePromptShields: %s scanning chunk %s (correlation=%s) — degrading",
                    exc, chunk.chunk_id, correlation_id,
                )
                results.append(ChunkShieldResult(
                    chunk_id=chunk.chunk_id,
                    local_check=local,
                    external_verdict=VERDICT_UNAVAILABLE,
                    degrade_reason=degrade_reason,
                    policy_version=self.policy_version,
                ))
        return Success(tuple(results))


__all__ = [
    "AzurePromptShields",
    "AzureShieldConfig",
    "AZURE_POLICY_VERSION",
    "load_azure_shield_config",
]

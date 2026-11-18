from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

SELECTION_IDENTITY_VERSION = "selection_identity_v1"
UNVERIFIED = "unverified"


def selection_identity_v1(records: Iterable[tuple[str, str, str]]) -> str:
    """Hash the canonical sorted (source_ref, fact_id, span_sha256) selection records."""
    canonical = sorted((str(source_ref), str(fact_id), str(span_sha256)) for source_ref, fact_id, span_sha256 in records)
    payload = json.dumps([SELECTION_IDENTITY_VERSION, canonical], separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

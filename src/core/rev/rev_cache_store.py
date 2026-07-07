"""REV result caches — extraction-result cache (P2-12) + judge cache (P2-8).

Both caches are **per-program, content-addressed, TTL-expiring, and
LRU-bounded**. They mirror the ``chart_cache_store.py`` pattern:

* atomic writes (``tmp`` + ``os.replace``) so a partial write is never read;
* a hard per-entry byte ceiling (``MAX_ENTRY_BYTES``) guards unbounded growth;
* ``load`` returns ``None`` on a corrupt/expired entry (logged, never raised)
  — a bad cache never breaks the pipeline;
* a ``schema_version`` stamp lets future migrations reject stale entries;
* eviction is best-effort: stale-by-TTL first, then surplus-by-count with the
  oldest-accessed entry evicted first (LRU). A read *touches*
  ``accessed_at`` so frequently-used entries survive eviction.

**Keys (specs/gaps.md):**
* extraction cache → ``(source_hash, prompt_version)`` where ``source_hash`` is
  ``sha256(canonical_text)`` and ``prompt_version`` is the extractor prompt
  version (e.g. ``rev_extractor.v1``). TTL 90 days; maxsize 500.
* judge cache → ``(source_document_key, prompt_version, ground_truth_hash)``
  where ``ground_truth_hash`` is ``sha256`` of the JSON-serialised ground-truth
  fact list for the message. TTL 90 days; maxsize 500.

**Prompt-caching note (orthogonal):** the fixed system-prompt + few-shot prefix
can additionally use the provider's ephemeral prompt cache
(``cache_control: ephemeral``) — that reduces cost/latency orthogonally to this
on-disk result cache and is configured at the provider call site, not here.

Zone A — no AI or M365 imports. Pure JSON persistence keyed by content hashes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# --- kinds -----------------------------------------------------------------
EXTRACTION_CACHE_KIND = "extraction"
JUDGE_CACHE_KIND = "judge"

# --- policy (specs/gaps.md P2-8 / P2-12) ------------------------------------
EXTRACTION_TTL_DAYS = 90
EXTRACTION_MAXSIZE = 500
JUDGE_TTL_DAYS = 90
JUDGE_MAXSIZE = 500

CACHE_SCHEMA_VERSION = "rev_cache.v1"
MAX_ENTRY_BYTES = 5 * 1024 * 1024  # 5 MiB — guard against pathological payloads
_DAY_SECONDS = 86_400


# ---------------------------------------------------------------------------
# Core store
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CacheStats:
    kind: str
    count: int
    total_bytes: int
    oldest_accessed_epoch: float | None


def _cache_root(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / "rev_cache"


def _cache_dir(program_id: str, kind: str, programs_root: Path) -> Path:
    return _cache_root(program_id, programs_root) / kind


def _key_fingerprint(key_values: tuple[str, ...]) -> str:
    """Content-addressed fingerprint over the ordered key values."""
    joined = "|".join(key_values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def _entry_path(program_id: str, kind: str, key_values: tuple[str, ...],
                programs_root: Path) -> Path:
    return _cache_dir(program_id, kind, programs_root) / f"{_key_fingerprint(key_values)}.json"


def _now(now_epoch: float | None) -> float:
    return time.time() if now_epoch is None else float(now_epoch)


def _read_entry(path: Path) -> dict[str, Any] | None:
    """Load + validate a cache entry. Returns None on any defect (never raises)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("rev_cache: corrupt JSON at %s — treating as miss", path)
        return None
    if not isinstance(data, dict):
        log.warning("rev_cache: non-object entry at %s — treating as miss", path)
        return None
    if data.get("schema_version") != CACHE_SCHEMA_VERSION:
        # Stale-schema entry → miss (a future migration would rewrite these).
        return None
    return data


def _is_expired(data: dict[str, Any], *, ttl_days: int, now_epoch: float) -> bool:
    captured = data.get("captured_at_epoch")
    if not isinstance(captured, (int, float)):
        return True
    return (now_epoch - float(captured)) > (ttl_days * _DAY_SECONDS)


def _touch_accessed(path: Path, data: dict[str, Any], *, now_epoch: float) -> None:
    """Update ``accessed_at`` on a read hit (best-effort, never raises)."""
    data["accessed_at_epoch"] = now_epoch
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("rev_cache: could not touch accessed_at for %s: %s", path, exc)


def get_cached(
    *,
    program_id: str,
    kind: str,
    key_values: tuple[str, ...],
    programs_root: Path,
    ttl_days: int,
    now_epoch: float | None = None,
) -> Any | None:
    """Return the cached payload for the key, or None on miss/expiry/corrupt.

    On a hit the entry's ``accessed_at`` is touched (LRU freshness). An expired
    entry is pruned best-effort and reported as a miss.
    """
    path = _entry_path(program_id, kind, key_values, programs_root)
    if not path.exists():
        return None
    now = _now(now_epoch)
    data = _read_entry(path)
    if data is None:
        return None
    if _is_expired(data, ttl_days=ttl_days, now_epoch=now):
        _safe_unlink(path)
        return None
    _touch_accessed(path, data, now_epoch=now)
    return data.get("payload")


def put_cached(
    *,
    program_id: str,
    kind: str,
    key_values: tuple[str, ...],
    key_labels: tuple[str, ...],
    payload: Any,
    programs_root: Path,
    set_at_epoch: float | None = None,
    now_epoch: float | None = None,
) -> Path:
    """Persist a cache entry atomically. Returns the entry path.

    Prunes stale entries + LRU-surplus after writing so the cache stays bounded.
    Raises ``ValueError`` if the serialised payload exceeds ``MAX_ENTRY_BYTES``.
    """
    captured = set_at_epoch if set_at_epoch is not None else _now(now_epoch)
    accessed = _now(now_epoch)
    entry = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": kind,
        "key": dict(zip(key_labels, key_values)),
        "captured_at_epoch": float(captured),
        "accessed_at_epoch": float(accessed),
        "payload": payload,
    }
    blob = json.dumps(entry, ensure_ascii=False)
    if len(blob.encode("utf-8")) > MAX_ENTRY_BYTES:
        raise ValueError(
            f"rev_cache {kind}: payload {len(blob)}B exceeds {MAX_ENTRY_BYTES}B ceiling"
        )
    d = _cache_dir(program_id, kind, programs_root)
    d.mkdir(parents=True, exist_ok=True)
    path = _entry_path(program_id, kind, key_values, programs_root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(blob, encoding="utf-8")
    os.replace(tmp, path)

    # Bound the cache after the write (stale first, then LRU surplus).
    ttl = EXTRACTION_TTL_DAYS if kind == EXTRACTION_CACHE_KIND else JUDGE_TTL_DAYS
    maxsize = EXTRACTION_MAXSIZE if kind == EXTRACTION_CACHE_KIND else JUDGE_MAXSIZE
    evict_stale(program_id, kind, programs_root, ttl_days=ttl, now_epoch=now_epoch)
    evict_lru(program_id, kind, programs_root, maxsize=maxsize, now_epoch=now_epoch)
    return path


def evict_stale(
    program_id: str,
    kind: str,
    programs_root: Path,
    *,
    ttl_days: int,
    now_epoch: float | None = None,
) -> int:
    """Remove entries older than ``ttl_days``. Returns the count evicted."""
    d = _cache_dir(program_id, kind, programs_root)
    if not d.is_dir():
        return 0
    now = _now(now_epoch)
    evicted = 0
    for path in sorted(d.glob("*.json")):
        data = _read_entry(path)
        if data is None:
            # Corrupt/legacy — prune it so it does not occupy an LRU slot.
            if _safe_unlink(path):
                evicted += 1
            continue
        if _is_expired(data, ttl_days=ttl_days, now_epoch=now):
            if _safe_unlink(path):
                evicted += 1
    return evicted


def evict_lru(
    program_id: str,
    kind: str,
    programs_root: Path,
    *,
    maxsize: int,
    now_epoch: float | None = None,
) -> int:
    """Evict oldest-accessed entries beyond ``maxsize``. Returns count evicted."""
    if maxsize <= 0:
        raise ValueError("maxsize must be > 0")
    d = _cache_dir(program_id, kind, programs_root)
    if not d.is_dir():
        return 0
    entries: list[tuple[float, Path]] = []
    for path in d.glob("*.json"):
        data = _read_entry(path)
        if data is None:
            continue
        acc = data.get("accessed_at_epoch")
        if not isinstance(acc, (int, float)):
            acc = 0.0
        entries.append((float(acc), path))
    surplus = len(entries) - maxsize
    if surplus <= 0:
        return 0
    entries.sort(key=lambda pair: pair[0])  # oldest accessed first
    evicted = 0
    for _, path in entries[:surplus]:
        if _safe_unlink(path):
            evicted += 1
    return evicted


def cache_stats(program_id: str, kind: str, programs_root: Path) -> CacheStats:
    """Report count + on-disk size + oldest accessed (for doctor --rev-health)."""
    d = _cache_dir(program_id, kind, programs_root)
    if not d.is_dir():
        return CacheStats(kind=kind, count=0, total_bytes=0, oldest_accessed_epoch=None)
    count = 0
    total = 0
    oldest: float | None = None
    for path in d.glob("*.json"):
        try:
            total += path.stat().st_size
        except OSError:
            pass
        data = _read_entry(path)
        if data is None:
            continue
        count += 1
        acc = data.get("accessed_at_epoch")
        if isinstance(acc, (int, float)):
            oldest = acc if oldest is None else min(oldest, float(acc))
    return CacheStats(kind=kind, count=count, total_bytes=total,
                      oldest_accessed_epoch=oldest)


def clear_cache(program_id: str, kind: str, programs_root: Path) -> int:
    """Remove every entry of one kind for a program. Returns count removed."""
    d = _cache_dir(program_id, kind, programs_root)
    if not d.is_dir():
        return 0
    removed = 0
    for path in d.glob("*.json"):
        if _safe_unlink(path):
            removed += 1
    return removed


def _safe_unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError as exc:
        log.warning("rev_cache: could not unlink %s: %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# Extraction-result cache (P2-12) — key: (source_hash, prompt_version)
# ---------------------------------------------------------------------------

def extraction_key_values(source_hash: str, prompt_version: str) -> tuple[str, ...]:
    return (source_hash, prompt_version)


def get_extraction_result(
    *,
    program_id: str,
    source_hash: str,
    prompt_version: str,
    programs_root: Path,
    now_epoch: float | None = None,
) -> list[dict[str, Any]] | None:
    """Return cached extraction claims (list of ExtractedClaim.to_dict()) or None."""
    payload = get_cached(
        program_id=program_id,
        kind=EXTRACTION_CACHE_KIND,
        key_values=extraction_key_values(source_hash, prompt_version),
        programs_root=programs_root,
        ttl_days=EXTRACTION_TTL_DAYS,
        now_epoch=now_epoch,
    )
    if payload is None:
        return None
    if not isinstance(payload, dict):
        return None
    claims = payload.get("claims")
    if not isinstance(claims, list):
        return None
    # Prompt-version stamp inside payload must match the requested version.
    if payload.get("prompt_version") != prompt_version:
        return None
    return claims


def put_extraction_result(
    *,
    program_id: str,
    source_hash: str,
    prompt_version: str,
    claims: list[dict[str, Any]],
    programs_root: Path,
    set_at_epoch: float | None = None,
    now_epoch: float | None = None,
) -> Path:
    """Persist extraction claims keyed by (source_hash, prompt_version)."""
    payload = {"prompt_version": prompt_version, "claims": claims}
    return put_cached(
        program_id=program_id,
        kind=EXTRACTION_CACHE_KIND,
        key_values=extraction_key_values(source_hash, prompt_version),
        key_labels=("source_hash", "prompt_version"),
        payload=payload,
        programs_root=programs_root,
        set_at_epoch=set_at_epoch,
        now_epoch=now_epoch,
    )


# ---------------------------------------------------------------------------
# Judge cache (P2-8) — key: (source_document_key, prompt_version, ground_truth_hash)
# ---------------------------------------------------------------------------

def judge_key_values(source_document_key: str, prompt_version: str,
                     ground_truth_hash: str) -> tuple[str, ...]:
    return (source_document_key, prompt_version, ground_truth_hash)


def hash_ground_truth(ground_truth_facts: list[str]) -> str:
    """Stable hash of the ground-truth fact list for the judge cache key."""
    blob = json.dumps(sorted(ground_truth_facts), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def get_judge_result(
    *,
    program_id: str,
    source_document_key: str,
    prompt_version: str,
    ground_truth_hash: str,
    programs_root: Path,
    now_epoch: float | None = None,
) -> dict[str, Any] | None:
    """Return the cached raw judge verdict dict, or None on miss/expiry/corrupt."""
    payload = get_cached(
        program_id=program_id,
        kind=JUDGE_CACHE_KIND,
        key_values=judge_key_values(source_document_key, prompt_version, ground_truth_hash),
        programs_root=programs_root,
        ttl_days=JUDGE_TTL_DAYS,
        now_epoch=now_epoch,
    )
    if not isinstance(payload, dict):
        return None
    if payload.get("prompt_version") != prompt_version:
        return None
    return payload.get("verdict")


def put_judge_result(
    *,
    program_id: str,
    source_document_key: str,
    prompt_version: str,
    ground_truth_hash: str,
    verdict: dict[str, Any],
    programs_root: Path,
    set_at_epoch: float | None = None,
    now_epoch: float | None = None,
) -> Path:
    """Persist a judge verdict keyed by (source_document_key, prompt_version, gt_hash)."""
    payload = {"prompt_version": prompt_version, "verdict": verdict}
    return put_cached(
        program_id=program_id,
        kind=JUDGE_CACHE_KIND,
        key_values=judge_key_values(source_document_key, prompt_version, ground_truth_hash),
        key_labels=("source_document_key", "prompt_version", "ground_truth_hash"),
        payload=payload,
        programs_root=programs_root,
        set_at_epoch=set_at_epoch,
        now_epoch=now_epoch,
    )


__all__ = [
    "CacheStats",
    "EXTRACTION_CACHE_KIND",
    "JUDGE_CACHE_KIND",
    "EXTRACTION_TTL_DAYS",
    "EXTRACTION_MAXSIZE",
    "JUDGE_TTL_DAYS",
    "JUDGE_MAXSIZE",
    "MAX_ENTRY_BYTES",
    "get_cached",
    "put_cached",
    "evict_stale",
    "evict_lru",
    "cache_stats",
    "clear_cache",
    "get_extraction_result",
    "put_extraction_result",
    "get_judge_result",
    "put_judge_result",
    "extraction_key_values",
    "judge_key_values",
    "hash_ground_truth",
]
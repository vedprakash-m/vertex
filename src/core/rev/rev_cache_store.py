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
import math
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.exceptions import StateError

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


def _key_fingerprint(key_values: tuple[str, ...], *, canonical: bool = False) -> str:
    """Content-addressed fingerprint over the ordered key values."""
    if canonical or not key_values or key_values == ("",) or any("|" in value for value in key_values):
        encoded = json.dumps(key_values, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return "v2-" + hashlib.sha256(encoded).hexdigest()[:32]
    joined = "|".join(key_values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def _entry_path(program_id: str, kind: str, key_values: tuple[str, ...],
                programs_root: Path, *, canonical: bool = False) -> Path:
    return _cache_dir(program_id, kind, programs_root) / f"{_key_fingerprint(key_values, canonical=canonical)}.json"


def _labeled_key(key_values: tuple[str, ...], key_labels: tuple[str, ...]) -> dict[str, str]:
    if len(key_labels) != len(key_values) or len(set(key_labels)) != len(key_labels):
        raise ValueError("Cache key labels must be unique and match key values.")
    return dict(zip(key_labels, key_values, strict=True))


def _entry_matches_key(
    data: dict[str, Any], kind: str, key_values: tuple[str, ...], expected_key: dict[str, str] | None,
) -> bool:
    actual_key = data.get("key")
    if data.get("kind") != kind or not isinstance(actual_key, dict):
        return False
    return actual_key == expected_key if expected_key is not None else tuple(actual_key.values()) == key_values


def _now(now_epoch: float | None) -> float:
    return time.time() if now_epoch is None else float(now_epoch)


def _read_entry(path: Path, *, strict_read: bool = False) -> dict[str, Any] | None:
    """Load and validate a cache entry using the selected error policy."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        if strict_read:
            raise StateError("Cache entry cannot be read.") from None
        if isinstance(error, UnicodeError):
            raise
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError) as error:
        if strict_read:
            raise StateError("Cache entry has invalid JSON.") from None
        if isinstance(error, RecursionError):
            raise
        log.warning("rev_cache: corrupt JSON at %s — treating as miss", path)
        return None
    if not isinstance(data, dict):
        if strict_read:
            raise StateError("Cache entry is not an object.")
        log.warning("rev_cache: non-object entry at %s — treating as miss", path)
        return None
    if data.get("schema_version") != CACHE_SCHEMA_VERSION:
        if strict_read:
            raise StateError("Cache entry has an incompatible schema.")
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
    strict_read: bool = False,
    key_labels: tuple[str, ...] | None = None,
) -> Any | None:
    """Return the cached payload for the key, or None on miss/expiry/corrupt.

    On a hit the entry's ``accessed_at`` is touched (LRU freshness). An expired
    entry is pruned best-effort and reported as a miss. A strict read instead
    raises on identifiable corrupt entries and never touches or prunes them.
    Labeled reads require one unique label per key value.
    """
    expected_key = _labeled_key(key_values, key_labels) if key_labels is not None else None
    path = _entry_path(program_id, kind, key_values, programs_root)
    canonical_path = _entry_path(program_id, kind, key_values, programs_root, canonical=True)
    candidates = (path,) if path == canonical_path else (canonical_path, path)
    if strict_read:
        if expected_key is None:
            raise ValueError("A strict cache read requires key labels.")
        for path in candidates:
            try:
                entry_stat = path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise StateError("Cache entry cannot be inspected.") from None
            if not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_size > MAX_ENTRY_BYTES:
                raise StateError("Cache entry is not a bounded regular file.")
            break
        else:
            return None
    else:
        for path in candidates:
            if path.exists():
                break
        else:
            return None
    now = _now(now_epoch)
    data = _read_entry(path, strict_read=strict_read)
    if data is None:
        return None
    if strict_read:
        epochs = (data.get("captured_at_epoch"), data.get("accessed_at_epoch"))
        valid_epochs = True
        for value in epochs:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                valid_epochs = False
                break
            try:
                if not math.isfinite(value):
                    valid_epochs = False
                    break
            except OverflowError:
                valid_epochs = False
                break
        if (
            not _entry_matches_key(data, kind, key_values, expected_key)
            or data.get("payload") is None
            or not valid_epochs
        ):
            raise StateError("Cache entry has invalid metadata.")
    elif not _entry_matches_key(data, kind, key_values, expected_key):
        log.warning("rev_cache: cache entry identity mismatch; treating as miss")
        return None
    if _is_expired(data, ttl_days=ttl_days, now_epoch=now):
        if not strict_read:
            _safe_unlink(path)
        return None
    if not strict_read:
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
    Raises ``ValueError`` for invalid key labels or an oversized payload.
    """
    cache_key = _labeled_key(key_values, key_labels)
    captured = set_at_epoch if set_at_epoch is not None else _now(now_epoch)
    accessed = _now(now_epoch)
    entry = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": kind,
        "key": cache_key,
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
    canonical_path = _entry_path(program_id, kind, key_values, programs_root, canonical=True)
    if path != canonical_path:
        if canonical_path.exists():
            path = canonical_path
        elif path.exists():
            existing = _read_entry(path)
            if existing is None or not _entry_matches_key(existing, kind, key_values, cache_key):
                path = canonical_path
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
        key_labels=("source_hash", "prompt_version"),
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
        key_labels=("source_document_key", "prompt_version", "ground_truth_hash"),
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
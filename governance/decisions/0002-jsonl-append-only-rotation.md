# ADR-0002: JSONL Append-Only Rotation for High-Risk Sidecars

**Date:** 2026-06-09
**Status:** Accepted
**Workstream:** WS-13 (JSONL rotation — rev. 323)
**Author(s):** Vertex engineering
**Approver(s):** n/a (structural convention)

## Context

Seven high-risk sidecar files (`edit_patterns`, `actions`, `ai_proposals`, `risk_updates`,
`autonomy_audit`, `reviews`, `signal_threads`) were growing unbounded with in-place writes. A corrupt
or truncated write could destroy the entire audit trail. Simultaneous writes from concurrent processes
(gather + doctor) caused occasional corruption under Windows file locking.

## Decision

All seven sidecars rotate at 10 MB per stem via `rotate_jsonl_if_oversize` (shared helper in
`src/core/jsonl_rotation.py`). Rotation retains the 5 most recent files (stem, stem.1, …, stem.4).
Every write uses `portalocker` + `fsync` before `unlock` to prevent partial-write corruption. The
shared helper was extracted to eliminate the 7 duplicated portalocker+fsync code blocks. INV-3 and
INV-4 (journal/trajectory append-only) are enforced by the same helper.

## Consequences

Easier: audit trail is bounded and recoverable; concurrent write safety is centralized; adding a new
high-risk sidecar costs one line (call the helper). Harder: disk usage is slightly higher (5 × 10 MB
cap per sidecar); readers that assumed a single-file stem must handle glob patterns. Explicitly
rejected: SQLite per sidecar (schema migration overhead); single giant JSONL (no rotation isolation).

## References

- `src/core/jsonl_rotation.py` — shared rotate helper
- `tests/contracts/test_architecture_fitness.py` (INV-3/4)
- `specs/vertex-tech-spec.md §9.x` — persisted-state grounding
- ADR-0001 (zone boundary enforced for the helper)

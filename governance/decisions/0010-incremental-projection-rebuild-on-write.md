# ADR-0010: Incremental projection rebuild on write

**Status:** Accepted
**Date:** 2026-07-07
**Decider:** Vertex engineering
**Companion:** `specs/fix-data-flow.md` §6.4 / §7 PR-8; `src/commands/ledger.py`; `src/core/ledger/program_views.py`; `tests/contracts/test_incremental_projection_write_hook.py`

## Context

`project_events_incremental_to_sqlite()` already existed and was production-tested,
but `current.sqlite3` only refreshed when an operator explicitly ran the manual
`vertex ledger replay` path. Normal ledger writes (`_persist_event` /
`_persist_events`) could therefore leave the on-disk projection stale even though
the source event log was already durably appended.

Track D requires that the projection refresh automatically after writes, with an
explicit concurrency evaluation order: WAL first, mutex second, queue third.

One important implementation detail was already in place before this decision:
`connect_projection_db()` sets `PRAGMA journal_mode=WAL` and
`PRAGMA synchronous=NORMAL` on projection-writer connections. Read helpers such as
`canonical_projection_dump()` do not set those pragmas themselves; they simply
open the already-created projection database.

## Decision

1. Wire an automatic post-write hook in `src/commands/ledger.py` so every
   `_persist_event` / `_persist_events` call:
   - appends the event(s),
   - runs `_maybe_bridge_event_to_fact_store(...)`,
   - then refreshes the projection by calling
     `project_events_incremental_to_sqlite(...)`.

2. Reuse the same event-loading shape as the manual replay command:
   `events = read_events(program_id, programs_root=...)` and
   `projection_path = get_current_projection_path(program_id, programs_root=...)`.
   The hook does not invent a second projection path or alternate fold path.

3. Choose **WAL-only** as the concurrency mechanism for PR-8. We did **not** add
   a per-program file mutex or async queue in this change.

4. Add an operator rollback/opt-out switch:
   `VERTEX_DISABLE_AUTO_PROJECTION_REBUILD=1`.
   When set, ledger writes still succeed, the manual replay command still exists,
   and the automatic hook is skipped.

5. Projection refresh failures are fail-open for the write path: log at `ERROR`,
   do not re-raise, and rely on the persisted event plus manual replay for retry.

## Why WAL-only was chosen

The PR-8 contract test
`tests/contracts/test_incremental_projection_write_hook.py::test_wal_mode_is_sufficient_for_concurrent_rebuilds_on_same_program`
ran **25 two-thread concurrent-rebuild trials** for the same program after paired
appends (**50 concurrent hook invocations total**) with:

- **0** `sqlite3.OperationalError: database is locked` failures
- no hangs
- final `current.sqlite3` state matching a fresh full rebuild

The companion concurrent append-vs-read test in the same file also passed without
corrupt or partial reads.

Because the measured WAL-enabled path already satisfied the acceptance gate, an
application-layer mutex would have added complexity and filesystem-lock
assumptions without a demonstrated need. Queueing remains the escalation path if
future production evidence shows WAL alone is insufficient.

## Consequences

- **+** `current.sqlite3` is now kept fresh automatically on the main ledger
  write path and is no longer dependent on an explicit replay command for
  day-to-day correctness.
- **+** The same hook covers direct writes, lock/unlock writes, and candidate
  triage paths that already funnel through `_persist_event` / `_persist_events`.
- **+** No new locking dependency or per-program lockfile protocol was needed.
- **−** Projection refresh is still synchronous on the write path, so rebuild
  latency is now part of append latency.
- **−** If production later shows contention or unacceptable latency, the next
  escalation remains the spec's recorded order: file mutex first, deferred queue
  only after that.

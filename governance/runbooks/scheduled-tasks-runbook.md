# Scheduled Tasks Runbook — `vertex prefetch` / `vertex cockpit build` / `vertex admin metrics-rollup`

**Status:** v1.1 — 2026-07-14 (ADF-W5.10 + ADF-W5.13, `specs/arch-data-fix.md` §10.6/§9.7)
**Owner:** Program operator
**Scope:** running Vertex's out-of-band acquisition/rendering commands on a
recurring schedule via Windows Task Scheduler or cron, so a human never has
to remember to run them manually and `report`/`gather` never wait on a live
WorkIQ call (Section 10.6, INV-ADF-2).

---

## What gets scheduled

| Command | Purpose | Suggested cadence |
|---|---|---|
| `vertex prefetch --program <id> [--edition <edition>] [--ttl-seconds N]` | Runs the slow live WorkIQ NL-search step out-of-band and commits a snapshot `gather` prefers over a live call | Every 1–4 hours during business hours (WorkIQ historical p50 latency is minutes, so more than a few runs/day has diminishing value) |
| `vertex cockpit build --program <id> [--open]` | Refreshes the local HTML dashboard | Once per gather cycle, or daily |
| `vertex cockpit show --program <id> --no-persist` | Read-only health check without writing history (safe to run more often than the above) | As needed |
| `vertex admin metrics-rollup --program <id>` *(ADF-W5.13)* | Rolls up the current ISO week's raw tier-decision/AI-telemetry/run-telemetry rows into `runtime/metrics/weekly/<family>.jsonl` (Section 9.7's 13-month aggregate) | Once per week, after the week has fully elapsed (e.g. Monday morning for the prior week) |

`vertex prefetch` and `vertex cockpit build` are both plain, idempotent CLI
invocations — no daemon, no background process, no network listener (NG-3).
Each run acquires the program's `actuation_dispatch` workspace lease and
exits cleanly if another operation is already using it
(`LeaseHeldByAnotherOwner`, exit code 1) — safe to schedule aggressively
without building your own locking.

## Windows Task Scheduler

1. Open Task Scheduler → **Create Task** (not "Basic Task" — you need the
   "Start in" directory field).
2. **General** tab: name it `vertex-prefetch-<program>`; "Run whether user
   is logged on or not" if this machine stays signed in; check "Run with
   highest privileges" only if your Vertex install needs it (usually not).
3. **Triggers** tab → New → Daily, recur every 1 day, repeat task every
   1–4 hours for a duration of 12 hours (covers a business day without
   running overnight).
4. **Actions** tab → New → Program/script:

   ```
   Program/script:  C:\path\to\venv\Scripts\python.exe
   Add arguments:   cli.py prefetch --program xpf --edition xpf_weekly
   Start in:        Q:\Ved\myProjects\MS\vertex
   ```

5. **Conditions** tab: uncheck "Start the task only if the computer is on
   AC power" if this runs on a laptop that may be on battery.
6. **Settings** tab: check "If the task fails, restart every" 15 minutes,
   up to 2 attempts (transient WorkIQ/network failures self-heal; a
   persisting failure degrades to a `"degraded"` snapshot rather than
   blocking the next `gather`, per `prefetch.py`'s design — see Section
   10.6).

Repeat with a second task for `vertex cockpit build --program xpf --open`
on whatever cadence you want the HTML dashboard refreshed (`--open` will
try to launch a browser window even in a non-interactive session — omit it
for a scheduled task and just open `runtime/cockpit/cockpit.html` manually
when needed).

## cron (WSL / Linux dev environment)

```cron
# Prefetch WorkIQ every 2 hours, 8am-8pm.
0 8-20/2 * * * cd /path/to/vertex && /path/to/venv/bin/python cli.py prefetch --program xpf --edition xpf_weekly >> /path/to/vertex/runtime/cron.log 2>&1

# Rebuild the HTML cockpit once a day at 7am.
0 7 * * * cd /path/to/vertex && /path/to/venv/bin/python cli.py cockpit build --program xpf >> /path/to/vertex/runtime/cron.log 2>&1

# Roll up the prior ISO week's telemetry every Monday at 6am (before the
# 7am cockpit rebuild, so the weekly aggregate is fresh for that build).
0 6 * * 1 cd /path/to/vertex && /path/to/venv/bin/python cli.py admin metrics-rollup --program xpf >> /path/to/vertex/runtime/cron.log 2>&1
```

## Verifying a scheduled run actually happened

```bash
# Confirm the last prefetch snapshot's age and completeness:
cat programs/xpf/runtime/prefetch/workiq/latest.json | python -m json.tool

# Confirm the cockpit HTML was rebuilt recently:
python -c "import os,time; p='programs/xpf/runtime/cockpit/cockpit.html'; print('age (min):', (time.time()-os.path.getmtime(p))/60)"
```

If a scheduled `vertex prefetch` run silently stopped happening (task
disabled, credential expired, machine asleep during the trigger window),
`gather --workiq` degrades to a live WorkIQ call automatically (Section
10.6's fallback) rather than failing outright — you will notice via a
slower `gather` run, not a hard error. Check the Task Scheduler task's
"Last Run Result" column (or `journalctl`/cron mail for cron) if `gather`
seems to have slowed back down.

## Explicit scope note

A dedicated `vertex doctor`-integrated "schedule health" check (comparing
the prefetch/cockpit snapshot ages against an expected cadence and failing
loudly on staleness) was scoped for this item but not wired into the main
`doctor.py` orchestrator this pass — `doctor.py` is at its architecture-
fitness line budget with no headroom, and a rushed integration into that
large composition pipeline was judged higher-risk than the value of
landing it in the same change as this runbook. The underlying primitive
(`src/core/schedule_health.py::evaluate_schedule_health`) is built and
tested standalone; wiring it into `doctor.py`'s check list is a small,
well-scoped follow-up.

# Armada Manual Gather Runbook

**Status:** v1.0 — 2026-07-27 (BL-I1, `specs/backlog.md`)
**Owner:** Program operator
**Scope:** running Armada's gather cycle by hand, on the operator's own
cadence, instead of registering `scripts/register_armada_gather_task.ps1`
with Windows Task Scheduler. This is a deliberate alternative, not a
degraded fallback — it reuses the exact same launcher and missed-attempt
detection the scheduled task would have used, so nothing about auth
hygiene or staleness detection is lost by staying manual.

---

## Why manual instead of Task Scheduler

ADR-0019 ([`governance/decisions/0019-armada-gather-refresh.md`](../decisions/0019-armada-gather-refresh.md))
offered OS-level scheduling as one path to unattended gather. The operator
decided against adding a Windows Task Scheduler dependency for this —
consistent with the same reasoning already applied to the people-registry
enrichment cadence (`specs/backlog.md` BL-E4): fewer OS-level dependencies
managing Vertex's operational rhythm, in favor of a lightweight command the
operator runs themselves. This runbook is that alternative, made as
efficient as the scheduled path by reusing its own tooling rather than
inventing a separate one.

`governance/runbooks/scheduled-tasks-runbook.md` remains the reference if
OS-level scheduling is ever reconsidered later; nothing here conflicts
with it.

## The two-command cycle

**1. Check whether gather is overdue** (no Task Scheduler required — this
check is Task-Scheduler-agnostic by design, per `gather_schedule_monitor.py`'s
own docstring: "the same check is deterministic in a runbook, test, or
future scheduler implementation"):

```bash
python scripts/run_armada_gather_scheduled.py --check-missed-attempt
```

- Prints `Armada scheduled gather attempt is current.` and exits `0` if the
  last recorded gather attempt (of any outcome — committed, failed, or
  quarantined; a failed attempt still proves an attempt happened) was
  within the last 26 hours.
- Prints `Armada scheduled gather is overdue; recorded a gather_missed_attempt alert.`
  and exits `1` otherwise, and durably records a `gather_missed_attempt`
  alert (visible via `vertex doctor`/the alert ledger) — the same alert a
  registered scheduled task's missed-attempt monitor would have raised.
  Running gather (step 2) below resolves it automatically on the next
  successful attempt.

**2. Run the gather**, using the same launcher the scheduled task would
have used — not a hand-typed `vertex gather` — so the Azure CLI auth
posture ADR-0019 ratified (never inherit a stray `ADO_PAT`) is identical
either way:

```bash
python scripts/run_armada_gather_scheduled.py
```

Equivalent to `vertex gather --program armada` under Azure CLI auth, with
`ADO_PAT` explicitly stripped from the child environment first. Add
`--auth-mode pat` only if you've provisioned the Credential Manager PAT
fallback via `vertex admin auth armada-scheduled-pat` (not the default
posture; see the ADR).

**Exit codes** (same contract as the scheduled-tasks runbook): `0` clean,
`2` optional degradation (a source degraded gracefully — fine to treat as
current), `3`/`4`/anything else unexpected — investigate the committed
gather manifest under `programs/armada/_gather_runs/` before trusting the
result.

## Suggested cadence

No enforced schedule — run it whenever convenient. As a practical rhythm,
once daily keeps step 1 reporting current (the 26-hour window has slack
for a day you skip). If you go longer between runs, that's fine too — step
1 will just tell you it's overdue next time you check, rather than failing
silently.

## Verifying a run actually happened

```bash
python -m vertex doctor --edition armada_weekly --storage
```

Confirms the fact-store/gather-related checks (including `_armada_leakage_hygiene_check`,
BL-F1) report against genuinely fresh data. For the manifest itself:

```bash
python -c "from src.core.gather_schedule_monitor import latest_gather_attempt_at; print(latest_gather_attempt_at('armada'))"
```

## Relationship to ADR-0019's acceptance checklist

This runbook resolves the "unattended/recurring gather" *goal* behind the
scheduler-registration checklist item without registering the OS-level
task itself. The checklist item ("Register and validate the `Vertex/Armada`
Event Log source, then perform one scheduled gather and one missed-attempt
monitor canary") is specifically about the *Task Scheduler* mechanism and
stays correctly un-checked — this is a recorded alternative decision, not
a completion of that literal item. Manual gather remains ADR-0019's own
documented supported operating mode either way (see its "Consequences"
section), so nothing here is out of policy.

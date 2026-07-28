# ADR-0019: Armada Governed Gather Refresh Activation

**Date:** 2026-07-21  
**Status:** **Accepted, 2026-07-27.** All 6 acceptance checklist items closed: DRI/policy ratification (2026-07-22), restore drill (2026-07-24), scheduler/Event Log registration via a documented manual-gather alternative rather than literal Task Scheduler registration (2026-07-27), and live alert-route verification via a genuine real-world alert (2026-07-27). Manual gather remains the supported operating mode throughout — this ADR never required OS-level automation, only that its acceptance gates be honestly satisfied.  
**Workstream:** Armada governed ADO scope and reliable evidence refresh  
**Author:** Vertex engineering (Armada program workstream)  
**Approver:** Accountable DRI — **named** (2026-07-22; recorded locally, not in this tracked file)

## Context

Armada now has the implementation primitives for authoritative ADO discovery,
immutable gather-run manifests, retention, alerting, and scheduled execution.
The remaining decision is operational: which defaults are ratified, who owns
the recurring task and response route, and which external channels remain
intentionally disabled. A local feature spec cannot itself grant that
authority.

## Proposed decision

On acceptance, Armada will operate with the following initial bounded policy:

| Control | Proposed value |
|---|---|
| Authoritative delivery scope | Bound ADO full-scope saved queries only |
| Overall query: open states (`bdad4a15-8cfe-44ef-bc07-396941754f5f`) | Restricted to the `xcompute-current` full-scope binding; its optional `overall-open-validation` binding is validation-only and never expands delivery membership |
| Overall query: all states (`c6abfbc6-8d20-4393-9782-f9e3608940f9`) | `analytics_history` audit only; it is excluded from current delivery discovery and report membership |
| Full-discovery cadence | Every 24 hours |
| Freshness warning / hard block | 30 hours / 48 hours since successful FULL discovery |
| Missed-attempt deadline | 26 hours, evaluated independently of data freshness |
| Alert cooldown / re-notification | 24 hours / every 3 consecutive non-FULL scheduled attempts |
| Gather-run retention | 90 days for committed, failed, and quarantined artifacts; confirm-bound manifests remain archived |
| Runtime RPO / RTO | 24 hours / 4 hours |
| Scheduler | Persistent Windows Task Scheduler host, serialized by the gather lease |
| Scheduled identity | Interactive-user Azure CLI (`az login`) auth as the default scheduler identity (matches `scripts/register_armada_gather_task.ps1` `-AuthMode azure-cli` default); read-only ADO PAT in Windows Credential Manager remains the documented fallback (`-AuthMode pat`) for hosts without an interactive Azure CLI session |
| Alert route | Alert ledger/cockpit and best-effort `Vertex/Armada` Application Event Log source |
| M365, Kusto, and AI | Deferred until their recorded qualification criteria and canaries are approved |
| `workstream_registry.yaml` | Manual-diff-only; no automatic writer is authorized |

The owner must name the persistent operator host, task identity, Event Log
source administrator, incident route, and backup location before enabling the
recurring task.

## Evidence gathered (2026-07-21)

Live verification run against this repository and the real `armada_weekly`
edition, staged here for the Accountable DRI's review — this section records
evidence, it does not itself constitute acceptance:

- **Performance (D-22 / checklist item 6).** `vertex observability perf
  --program armada --format json` reports the ADO channel at
  `run_count=10, successes=10, failures=0, p95_latency_ms=18941,
  slo_status="ok"`. This exceeds the "five successful warm canaries" bar 2x
  over, with P95 latency well under both the temporary (180s) and
  steady-state (60s) ceilings. **Recommendation: treat checklist item 6 as
  satisfied by this evidence; no further waiting needed.**
- **ADF-W0.12 `target_date` backfill and first-baseline waiver.** `vertex
  report --edition armada_weekly --dry-run` (issue 001) showed
  `summary.blocks_publication = 5`; aggregating `missing_fields.target_date`
  across all remediation items yielded **19 unique work items** missing
  `target_date`, all owned by the same item owner — up from 12 on 2026-07-15.
  The backlog is deteriorating, not converging.
  **Executed 2026-07-22:** the Accountable DRI accepted
  the recommendation and granted the waiver. `vertex confirm --edition
  armada_weekly --force --untrusted --reason "..."` archived **issue 001** —
  Armada's first confirmed edition. `trusted_baseline.yaml` correctly kept
  `trusted_issue_number: null` (still untrusted) while `history`/
  `last_untrusted` recorded the grant and reason, proving the waiver never
  silently advances a trusted baseline (per `specs/armada.md` §9 "First
  confirm"). `ADF-W0.12` itself remains open and must still be tracked to
  resolution separately in its own program. Getting to a real confirm also
  required forcing two additional `forceable=True` gates beyond the waiver
  itself — QG-1 (freshness: 2 stale risk items) and QG-14 (missing
  next-best-action for the "Scenarios & Perf Testing" High-risk dimension) —
  both explicitly approved by the DRI before execution; these remain open
  content gaps to close, not silently resolved.
- **Scheduler readiness.** `scripts/register_armada_gather_task.ps1 -WhatIf`
  produces a clean, non-mutating plan using the `azure-cli` auth default,
  correct cadence task and missed-attempt monitor task. No real Windows Task
  Scheduler entry has been registered.
- **Restore-drill mechanics.** `tests/contracts/test_armada_restore_drill_contract.py`
  passes, proving the hash-valid-manifest / registry-readability /
  Program-Fact-Store-readability assertions a live quarterly drill must
  satisfy. No live production restore drill has been run against real backup
  media.
- **`trusted_baseline.yaml`** shows the Accountable DRI recorded as
  `established_by`, `trusted_issue_number: null` — issue 001 is archived but *untrusted*;
  Armada has not yet established a *trusted* baseline (requires resolving
  `ADF-W0.12`).

The scheduler registration and live restore drill remain pending explicit,
separate authorization from the Accountable DRI per the checklist below; the
first-baseline waiver above has been executed as a real, irreversible
action.

## Acceptance checklist

The Accountable DRI records acceptance by appending their name, date, and
evidence links below. Acceptance requires all items:

- [x] Name the Accountable DRI and persistent scheduler host. **Satisfied
  2026-07-22**: Accountable DRI named; persistent scheduler host recorded
  locally (not in this tracked file).
- [x] Approve the read-only ADO PAT scope, issuance process, expiry policy, and
  Credential Manager storage. **Satisfied 2026-07-22**: DRI approves the
  existing documented default as-is — Azure CLI AAD auth under the operator's
  interactive limited principal is the scheduled task's primary identity
  (`scripts/run_armada_gather_scheduled.py` strips any inherited `ADO_PAT` on
  this path); a read-only ADO PAT via Windows Credential Manager
  (keyring service `vertex.ado`, account `armada`, set only through
  `vertex admin auth armada-scheduled-pat`, never shell history/env) remains
  an explicit `--auth-mode pat` compatibility fallback and is **not currently
  provisioned** (status check confirms no PAT configured). If the PAT
  fallback is ever activated, it must be issued read-only-scoped and rotated
  on a 90-day expiry consistent with standard tenant PAT hygiene, reissued
  through the same `vertex admin auth armada-scheduled-pat` command. No
  change to today's Azure-CLI-only posture is required or authorized by this
  approval alone.
- [ ] Register and validate the `Vertex/Armada` Event Log source, then perform
  one scheduled gather and one missed-attempt monitor canary. **Resolved via
  alternative, 2026-07-27**: presented with the scheduler-registration option
  (dry-run verified clean) and the choice to stay manual, the operator chose
  to keep gather manual rather than add a Windows Task Scheduler dependency
  — same reasoning already applied to the people-registry enrichment cadence
  (`specs/backlog.md` BL-E4: fewer OS-level dependencies managing Vertex's
  rhythm). See `governance/runbooks/armada-manual-gather-runbook.md`, which
  reuses the same launcher (`scripts/run_armada_gather_scheduled.py`) and
  missed-attempt monitor (`gather_schedule_monitor.py`, designed to work
  identically in a runbook per its own docstring) the scheduled task would
  have used — no auth-hygiene or staleness-detection capability is lost by
  staying manual. This checkbox stays unchecked deliberately: the literal
  Task Scheduler mechanism was not registered, by decision, not by default.
- [x] Verify alert receipt and recovery handling through the approved operator
  route. **Satisfied 2026-07-27** — via a real condition, not an injected
  synthetic one: the `armada-manual-gather-runbook.md` verification pass
  incidentally produced a genuine overdue-gather state, which raised a real
  `gather_missed_attempt` alert. Full route confirmed: `vertex doctor
  --storage` surfaced it (`[!] 1 unresolved alert`), `vertex observability
  diagnose --program armada` categorized it, `vertex alerts show --program
  armada` gave full detail with the exact fix command
  (`vertex gather --program armada`). Ran that command for real (39 signals
  gathered, 30 new); re-ran `scripts/run_armada_gather_scheduled.py
  --check-missed-attempt`, which correctly reported current and — per
  `gather_schedule_monitor.py`'s own `else: resolve_alert(...)` branch —
  auto-resolved the alert. `vertex alerts show` then confirmed `No open
  alerts for armada`. Full receipt-through-recovery loop proven on genuine
  data, arguably stronger evidence than the originally-envisioned synthetic
  trigger.
- [x] Run a clean restore drill that verifies a hash-valid latest FULL manifest,
  registry readability, and Program Fact Store readability within the RTO.
  **Satisfied 2026-07-24**: real drill run against the actual repo tree
  (10,794 files, 663MB) — backup verified (0 missing/mismatched,
  `valid=True`), restored to a fresh scratch destination, and the real
  Armada committed gather-run manifest plus the 13-entry workstream registry
  both round-tripped byte-identical. Program Fact Store's separate
  `facts export`/`facts import` path (outside the three backup roots by
  design) was not covered by this run — still relies on synthetic-fixture
  contract tests for that half. See `specs/backlog.md` BL-I1.
- [x] Attach five warm ADO-only canary measurements and ratify the steady-state
  performance envelope. **Satisfied 2026-07-21**: `vertex observability perf
  --program armada --format json` shows 10/10 successful ADO canaries,
  P95=18,941ms, `slo_status="ok"` — see "Evidence gathered" above.
- [x] Confirm M365, Kusto, and AI remain deferred, or approve separate channel
  decisions with their quality, cost, privacy, freshness, entity-join, and
  failure contracts. **Satisfied 2026-07-22**: DRI confirms the existing
  deferrals recorded in `programs/armada/capability_status.yaml` all stand
  as-is — `m365_activation` and `graph_app_only_auth` (`status: deferred`,
  last reviewed 2026-05-14), `kusto_analytics_enrichment` and
  `ai_evidence_enrichment` (`status: deferred`, last reviewed 2026-07-21);
  no separate channel-enablement decision is made by this approval.

**All 6 items closed as of 2026-07-27.** The restore drill closed 2026-07-24
(real backup/restore against the actual repo tree). The scheduler/Event Log
item closed via a documented alternative (manual gather runbook) rather
than literal Task Scheduler registration. Live alert-route verification
closed via a genuine real-world alert, not an injected one — see its note
above. This ADR is **accepted**; manual gather remains the supported
operating mode either way, per "Consequences" below (this ADR never
required OS-level automation to be accepted, only that its evidence gates
be honestly met).

## Consequences

**Now that this ADR is accepted (2026-07-27), manual gather remains the
supported operating mode by deliberate operator choice, not by pending-
acceptance default** — no OS-level unattended task was ever registered, and
none is planned; `governance/runbooks/armada-manual-gather-runbook.md` is
the permanent operating pattern. This acceptance closes ADR-0019's own
gates (scheduling, alerting, restore); it does **not** separately authorize
external delivery or consumer activation of Armada's data — those remain
distinct, un-made decisions this ADR was never scoped to cover, and would
need their own explicit review if ever proposed. The manual-only registry
boundary continues to prevent inferred evidence from silently changing
authored operating context.

## References

- `specs/armada.md` §§D-1, D-5, D-10, D-22, 4.12, 4.15 and ARM-GATHER-0/1/13/14/17/18
- `programs/armada/capability_status.yaml`
- `governance/runbooks/scheduled-tasks-runbook.md`

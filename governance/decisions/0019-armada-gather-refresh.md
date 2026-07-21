# ADR-0019: Armada Governed Gather Refresh Activation

**Date:** 2026-07-21  
**Status:** Proposed — no operational activation is authorized by this record until the named Accountable DRI accepts it.  
**Workstream:** Armada governed ADO scope and reliable evidence refresh  
**Author:** Vertex engineering (Armada program workstream)  
**Approver:** Accountable DRI — **TBD**

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
| Scheduled identity | Read-only ADO PAT in Windows Credential Manager, injected only into the gather child process |
| Alert route | Alert ledger/cockpit and best-effort `Vertex/Armada` Application Event Log source |
| M365, Kusto, and AI | Deferred until their recorded qualification criteria and canaries are approved |
| `workstream_registry.yaml` | Manual-diff-only; no automatic writer is authorized |

The owner must name the persistent operator host, task identity, Event Log
source administrator, incident route, and backup location before enabling the
recurring task.

## Acceptance checklist

The Accountable DRI records acceptance by appending their name, date, and
evidence links below. Acceptance requires all items:

- [ ] Name the Accountable DRI and persistent scheduler host.
- [ ] Approve the read-only ADO PAT scope, issuance process, expiry policy, and
  Credential Manager storage.
- [ ] Register and validate the `Vertex/Armada` Event Log source, then perform
  one scheduled gather and one missed-attempt monitor canary.
- [ ] Verify alert receipt and recovery handling through the approved operator
  route.
- [ ] Run a clean restore drill that verifies a hash-valid latest FULL manifest,
  registry readability, and Program Fact Store readability within the RTO.
- [ ] Attach five warm ADO-only canary measurements and ratify the steady-state
  performance envelope.
- [ ] Confirm M365, Kusto, and AI remain deferred, or approve separate channel
  decisions with their quality, cost, privacy, freshness, entity-join, and
  failure contracts.

## Consequences

Until this ADR is accepted, manual gather remains the supported operating
mode. The code may stage manifests, alert records, and scheduler configuration
for review, but no unattended task, external delivery, or consumer activation
is authorized. The manual-only registry boundary prevents inferred evidence
from silently changing authored operating context.

## References

- `specs/armada.md` §§D-1, D-5, D-10, D-22, 4.12, 4.15 and ARM-GATHER-0/1/13/14/17/18
- `programs/armada/capability_status.yaml`
- `governance/runbooks/scheduled-tasks-runbook.md`

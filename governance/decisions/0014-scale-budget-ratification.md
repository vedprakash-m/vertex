# ADR-0014: Scale envelope, retention, and budget ratification (ADF-W0.6)

**Date:** 2026-07-13
**Status:** Accepted (2026-07-13) for the "ratify as-is" set and Decisions
1-2 (Kusto sequential-realistic/450s; encryption-at-rest OS-level
reliance). Decisions 3 (RTBF period N) and 4 (push-alerting defer) remain
open per their own sections below — not blocking, tracked separately.
**Workstream (from `specs/arch-data-fix.md`):** ADF-W0.6
**Author(s):** Vertex engineering (drafted per live decision-by-decision
session)
**Approver(s):** the Platform DRI (per ADR-0013) — 2026-07-13

## Context

`governance/nfr-budgets.yaml` already scaffolds every candidate budget
row from `specs/arch-data-fix.md` §8/§A.0 with `status: candidate`. A
ratified budget's numeric value can never be loosened later (enforced by
`tests/contracts/test_nfr_budget_freeze_contract.py`), so ratification is
a real commitment, not a formality — this ADR proposes concrete values
built from the evidence already gathered this session (Section 4
baselines, the §8 candidate-budget table) and flags the rows that
genuinely need the user's judgment rather than an evidence-derived
number.

## Recommendation: ratify as-is (evidence already matches the candidate)

These candidates already match a value stated elsewhere in
`arch-data-fix.md` §8's own candidate-budget table, or are conservative
defaults with no counter-evidence. No new number is proposed — just
`status: candidate -> ratified`.

| id | Value | Why it's ready |
|---|---|---|
| `audit-write-latency-local` | 25 ms ceiling | Matches §8 "AI release audit p95 <=25 ms local" verbatim. |
| `audit-write-latency-network-lease` | 150 ms ceiling | Matches §8 "<=150 ms network-via-lease" verbatim. |
| `context-compile-latency` | 300 ms ceiling | Matches §8 "Context compile p95 <=300 ms" verbatim. |
| `startup-regression` | 150 ms ceiling | Matches §8 "No-AI startup regression <=150 ms" verbatim. |
| `local-encode-latency` | 80 ms ceiling | No local-encode caller exists yet (Tier-1 local calls = 0 production callers, per Section 4). Ratifying now sets the design target before ADF-W5.x builds it, consistent with "no later phase may lower a budget to pass." |
| `capacity-snapshot-cadence-events` | 5,000 events ceiling | Reasonable default; no counter-evidence surfaced this session. |
| `capacity-trace-metadata-retention` | 90 days ceiling | Reasonable default; aligns with typical internal-tool retention norms. |
| `capacity-sanitized-excerpt-retention` | 90 days ceiling | Same rationale; keeps AI I/O excerpt retention aligned with trace-metadata retention. |
| `reliability-rpo-authorization-execution` | RPO = 0 (qualitative) | Already the de facto behavior: INV-3/INV-4 append-only ledgers, the AF-7 outbox, and the Slice-1 lease/idempotency work this session built all target zero data loss for authorization/execution records. Ratifying formalizes existing behavior, not a new commitment. |
| `compatibility-schema-version-window` | >=2 versions floor | Reasonable default; matches the project's existing `schema_version` major-version patterns (program.yaml, editions, readiness.yaml all currently at their respective major with no breaking bump in flight). |
| `operations-accountable-role` | Platform DRI (single operator, qualitative) | Resolved by ADR-0013; this row just points at that ADR rather than naming an individual. |

## Blocked on engineering, not ratifiable yet

| id | Why it can't be ratified today |
|---|---|
| `concurrency-sqlite-busy-backoff-cap` | The candidate's own comment says it correctly: "numeric bound not yet proposed; needs a Phase-1 CPK measurement spike." No fault-injection measurement exists yet. Leave `status: candidate`, `value: null` until that spike runs. |
| `opex-max-cost-per-call-usd` | Per-feature, ratified once AF-4 lands (not yet built this session). Leave open. |

## Stale reference — needs a fresh decision, not a number

`opex-frontier-cost-per-cycle-reduction`'s comment cites "ratified in
Phase 0 per arch-fix.md §1 Goals (G3)" — but that references the **prior,
now-archived** `arch-fix.md` spec (superseded by `arch-data-fix.md`,
per the 2026-07-11 changelog entries). `arch-data-fix.md`'s own `ADF-G3`
is a different goal ("make optional-source latency bounded and
non-blocking," §1) — it does not define a frontier cost-reduction
percentage. This candidate needs either a fresh target tied to
`arch-data-fix.md`'s actual economics goals (Section 5's AI-economics
material, Tier-0/1/2 routing) or an explicit decision that it is
superseded and should be removed from the candidate set. **Recommend:
defer this row until ADF-W5.1-W5.3 (tier routing graduation) produce a
real measured baseline to set a floor against** — setting a percentage
target with zero current tier-1/tier-2 production traffic (Section 4:
"Tier-1 local calls: 0 production callers") would be an arbitrary
number, not an evidence-based ratification.

## Decisions needed (the user's judgment, not evidence-derivable)

### 1. Kusto sequential vs. bounded-parallel budget (ADF-W1.6)

**Evidence:** Section 4 baseline — XPF Kusto required-set gather, 20
historical samples: p50 = 229.2s, max = 430.9s (sequential, current
behavior). The §8 candidate budget offers two shapes: "a
sequential-realistic budget" or "bounded-parallel <=180-second budget
after measurement."

The current sequential p50 (229s) already exceeds a 180s target by ~27%;
the max (431s) exceeds it by ~140%. Reaching <=180s requires either
building bounded Kusto parallelism (ADF-W1.6 is real M-effort engineering
— per-cluster throttling/circuit-breaker respect, side-by-side latency
and failure-rate comparison against the sequential baseline before
enforce mode) or accepting a looser sequential-realistic ceiling.

| Option | Ceiling | Cost |
|---|---|---|
| **A. Sequential-realistic (Recommended)** | 450 s ceiling (rounds up from observed 430.9s max with headroom) | Zero new engineering; ratifies actual current behavior. Reopens later if XPF's query set grows. |
| **B. Bounded-parallel target** | 180 s ceiling | Requires building and validating ADF-W1.6's bounded-parallelism engineering (throttle-aware, circuit-breaker-respecting, measured against the sequential baseline) before the budget can be met — real scope, not just a number change. |

Recommendation leans **A** for now: nothing currently depends on
sub-3-minute Kusto latency (the WorkIQ prefetch/report-blocking problem
that motivated ADF-W1.4/W1.5 is the dominant latency source at p50
3,927.7s — over 8x the Kusto figure — and is already being solved by the
prefetch/TTL/lease work, independent of Kusto). Revisit Option B if a
future work item needs a tighter interactive-latency guarantee.

### 2. Encryption-at-rest scope (`security-encryption-at-rest`)

Vertex today runs single-operator, on a corp-managed Windows machine,
against a network drive (`Q:`), with no multi-tenant or external-facing
deployment. The candidate is currently fully open (`value: null`,
qualitative).

| Option | Scope | Cost |
|---|---|---|
| **A. Rely on OS/disk-level encryption (Recommended for current deployment shape)** | Require BitLocker (or platform-equivalent full-disk encryption) on any machine hosting `programs/*` data and the fact-store SQLite databases; no additional application-level encryption-at-rest. | Zero new engineering; a policy statement plus a `doctor` check that can warn (not block) if disk encryption is undetectable. |
| **B. Application-level encryption-at-rest for PII/confidential excerpts** | Encrypt specific fields/files (e.g., sanitized AI I/O excerpts, EML bodies) independent of disk state, with key storage/rotation and ACLs as the candidate's description already specifies. | Real new engineering: key management, rotation policy, and a migration for every existing plaintext store. Only clearly justified once Vertex moves beyond single-operator/single-machine deployment. |

Recommendation: **A now, revisit B if/when Vertex moves to shared or
cloud-hosted deployment** (tracked as a trigger condition, not a
deadline). This needs the user's confirmation of the actual deployment
shape (is BitLocker/equivalent already enabled on the host machine?) —
that fact is not something this session can verify from the repo.

### 3. Privacy retention / RTBF period (`privacy-retention-rtbf`) — SUPERSEDED

**Superseded by ADR-0015 (2026-07-13).** The recommendation below assumed
`PseudonymTable` could be the on-disk purge hook; a file-path inventory
found that assumption wrong (`PseudonymTable` is in-memory/non-persistent
by design). ADR-0015 resolves this using the platform's existing PII
default (N=365 days) and the existing `[EXCISED]` tombstone mechanism
instead. Left below for the historical record only.

The pseudonymization infrastructure (`PseudonymTable`, W5-3, already
built and shipped) exists, but no retention *period* or RTBF trigger is
ratified. This is a genuine policy question tied to what real people's
data flows through Vertex (stakeholder names/emails in `.eml`/`.ics`
imports, ADO work-item ownership, meeting-action assignees).

Recommendation (needs confirmation, not evidence-derivable): retain
personally-identifying fields **while the owning program is active**,
purge/pseudonymize-irreversibly **N days after program archival** — the
open question is the value of N. `governance/privacy-matrix.md` (ADF-
W0.16, tracked separately) is the right place to encode the final
per-artifact-type answer; this row can ratify the *policy shape*
("active-program retention + N-day post-archival purge, keyed off the
existing `PseudonymTable`") now and leave N to be set alongside
ADF-W0.16.

### 4. Push alerting (`observability-push-alerting`)

Recommendation: **defer formally** until fleet certification
(ADF-W5.7's ">=3 operational programs" gate) — single-operator,
single-to-two-program operation is adequately served by `doctor`/
`cockpit` self-review (both already built and used every session). Set
`status: candidate` with a note "deferred pending >=3-program fleet"
rather than `null`-forever; revisit at ADF-W5.7.

## Consequences

**Easier:** eleven of seventeen candidate rows move from ambiguous
"someday" placeholders to either a ratified, freeze-tested number or an
explicitly-tracked reason they can't be ratified yet (spike-blocked or
engineering-blocked) — no more silent `null`s with no path forward.

**Harder:** four rows still require the user's direct input (Kusto
strategy, encryption-at-rest scope, RTBF period N, push-alerting defer
confirmation) — this ADR does not invent those answers, consistent with
the project's standing rule that budget ratification is a human decision
(`nfr-budgets.yaml`'s own header comment).

## References

- `governance/nfr-budgets.yaml`
- `tests/contracts/test_nfr_budget_freeze_contract.py`
- `specs/arch-data-fix.md` §4.1 (evidence baseline), §8 (candidate
  budget table), ADF-W0.6/ADF-W1.6 rows
- `governance/privacy-matrix.md` (ADF-W0.16, RTBF period target)
- Related: ADR-0012 (governance tracking), ADR-0013 (RACI — Platform DRI
  naming referenced by `operations-accountable-role`)

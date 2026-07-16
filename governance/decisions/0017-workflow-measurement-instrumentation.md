# ADR-0017: Workflow-measurement instrumentation for ADF-W2.11/W3.8/W4.8

**Date:** 2026-07-13
**Status:** Accepted (2026-07-13)
**Workstream (from `specs/arch-data-fix.md`):** ADF-W2.11, ADF-W3.8, ADF-W4.8
**Author(s):** Vertex engineering (drafted per live decision-by-decision
session)
**Approver(s):** the Platform DRI / Pilot TPM (per ADR-0013)

## Context

ADF-W2.11/W3.8/W4.8 require measuring weekly active time, review burden,
and cycle time for the six proposal-shaped types built this session
(RiskProposal, MeetingAction, TopThreeCandidateProposal,
GovernanceDecisionBriefProposal, DependencyBlastRadiusProposal, and
ProgramSynthesis). Investigation found none of the five human-reviewed
types are durably persisted anywhere -- each lives only in memory for the
duration of one CLI invocation. There was no data for a "weekly report"
to query, and the pre-existing `journal/ai_proposals.jsonl`/`AIProposal`
mechanism is tightly coupled to a different, older `WorkstreamSynthesis`
payload shape, not generic enough to reuse without an awkward shim.

## Decision

1. **New generic audit trail, not five bespoke stores.**
   `src/core/proposal_audit.py` adds one append-only sidecar
   (`journal/proposal_audit.jsonl`) with one record schema
   (`ProposalAuditRecord`: program_id, proposal_type discriminator,
   proposal_id, event, at, proposed_at, ai_run_id, rejection_reason)
   shared across all five types, rather than reusing
   `ai_proposal_store.py` (rejected -- see Alternatives) or building five
   separate files.

2. **"Review latency," explicitly not "active time."** The metric this
   instrumentation actually computes is `decided_at - proposed_at` --
   wall-clock time between a proposal existing and a human deciding on
   it. This is an honest, labeled **proxy**, not a claim of true engaged/
   active time (which would need to exclude idle time between staging and
   when a human actually looked at it -- not measurable from server-side
   timestamps alone). The CLI output (`vertex cockpit measure`) states
   this explicitly rather than presenting the proxy as the literal metric
   the spec names.

3. **Additive, opt-in recording -- zero regression risk.** Each of the
   five types' dataclass gains two new fields
   (`proposed_at: datetime = field(default_factory=...)`,
   `decided_at: datetime | None = None`) and each `approve_*`/`reject_*`
   helper gains an optional `programs_root: Path | None = None` parameter.
   When omitted (every existing call site and test), the function's
   behavior is byte-for-byte unchanged -- no I/O, pure `dataclasses.replace`
   as before. All 49 pre-existing tests across the five files passed
   unmodified. Recording only activates when a caller explicitly passes
   `programs_root` -- the same "opt-in via an optional path parameter"
   shape as `context_gap_reply.py::apply_context_gap_answer`.

4. **Aggregation + CLI surface.** `src/core/adf_workflow_metrics.py`
   computes per-type decided/approved/rejected counts and p50/p90/max
   review-latency from the audit trail, with an optional `since` filter
   for a rolling window. `vertex cockpit measure --program <id>
   [--since-days N] [--format human|json]` (new subcommand on the
   existing `vertex cockpit` app) renders it.

## What this closes and what it doesn't

**Closes now:** the engineering half of ADF-W2.11/W3.8/W4.8 -- the
platform can durably record and report review latency and proposal
volume the moment any caller (a future CLI review command, or direct
library use) opts in by passing `programs_root`.

**Does not close:** the actual human half -- ADF-W2.11/W3.8/W4.8's
acceptance evidence ("comparable measured workflow record," "<=10-minute
target measurement," "<=5-minute proposal target and review-budget
proof") requires **real weeks of real usage** producing real data. The
report is honest about this: `vertex cockpit measure` returns all-zero
counts until real review activity accrues, and the command's own
docstring says so explicitly rather than implying readiness. No CLI
review command exists yet for most of these five types (per the
changelog: "Deferred: the CLI review command itself" for meeting
actions and others) -- until one exists and passes `programs_root`, no
data accrues from actual operator use. Wiring that CLI review command is
a separate future item, deliberately out of this ADR's scope (this ADR
builds the measurement primitive; it does not force a review-command
redesign to consume it).

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Reuse/generalize `journal/ai_proposals.jsonl` / `AIProposal` | Tightly coupled to `WorkstreamSynthesis`'s specific payload shape (workstream_id + synthesis-specific fields); shoehorning five unrelated proposal types through it would require either a lossy generic payload field or breaking its existing narrow contract. A new, deliberately generic sidecar is cleaner. |
| Require `programs_root` (no opt-out) on every `approve_*`/`reject_*` | Would force updating 49 existing tests and every existing call site in one change, and would make every test writer of these modules deal with filesystem I/O even when testing pure state-transition logic. The opt-in default preserves the existing pure-function contract for anyone who doesn't need durability. |
| Try to measure true "active time" (excluding idle gaps) via periodic polling or a client-side timer | No existing mechanism captures when a human's attention actually engages with a staged proposal (no UI heartbeat, no read-receipt). Would require new, more invasive instrumentation (e.g., a terminal-UI session tracker) disproportionate to what the spec's acceptance evidence actually needs (a "comparable measured workflow record," which review latency already satisfies as an honest proxy). |

## Consequences

**Easier:** ADF-W2.11/W3.8/W4.8 have a real, tested, zero-regression-risk
measurement primitive ready the moment real review activity happens;
`vertex cockpit measure` gives an immediate, honest read of "nothing has
accrued yet" rather than silence or a fabricated number.

**Harder:** none identified for the instrumentation itself. The genuine
remaining gap (no CLI review command wired for most of these types) was
already true before this ADR and is unchanged by it -- explicitly named
above rather than implied as solved.

## References

- `src/core/proposal_audit.py`, `src/core/adf_workflow_metrics.py`
- `src/commands/cockpit.py::cockpit_measure`
- `tests/unit/test_proposal_audit.py`,
  `tests/unit/test_adf_workflow_metrics.py`,
  `tests/unit/test_cockpit_measure_cli.py`
- `specs/arch-data-fix.md` ADF-W2.11, ADF-W3.8, ADF-W4.8
- Related: ADR-0013 (RACI — Pilot TPM role)

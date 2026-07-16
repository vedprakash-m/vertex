# ADR-0013: RACI / decision-rights naming for `arch-data-fix.md` (ADF-W0.2)

**Date:** 2026-07-13
**Status:** Accepted (2026-07-13) — role binding accepted as drafted,
including reuse of `ai-safety-approver.md` for the Independent Evaluator
role.
**Workstream (from `specs/arch-data-fix.md`):** ADF-W0.2
**Author(s):** Vertex engineering (drafted per live decision-by-decision
session)
**Approver(s):** the Platform DRI — 2026-07-13

## Context

`specs/arch-data-fix.md` §3.6.2-3.6.3 already defines the **shape** of
the RACI (deferral-escalation pairs and an independence-collapse table)
but names no people:

| Role (as defined in the spec) | Appears in |
|---|---|
| Vertex platform DRI | §3.6.2 deferral table (5 of 6 rows); §3.6.3 independence table |
| Affected program owner | §3.6.2 (reliability-deferral escalation target) |
| Program governance owner | §3.6.2 (autonomy graduation/demotion; senior/skip-level solicitation) |
| Independent evaluator | §3.6.2, §3.6.3 (autonomy graduation co-signer; model/prompt certification) |
| TPM/EM owner | §3.6.2 (senior/skip-level solicitation escalation target) |
| Audience-policy owner | §3.6.2 (senior/skip-level solicitation escalation target) |
| Business/operations validator | §3.6.2 (certified value claim outside pilot) |
| Pilot TPM | §3.6.2, §3.6.3 (value-claim escalation target; may collapse with Primary annotator) |
| Primary annotator | §3.6.3 (may collapse with pilot TPM; must NOT collapse with second annotator/adjudicator) |
| Second annotator and adjudicator | §3.6.3 (must remain independent of primary annotator/pilot TPM) |
| UX reviewer | §3.6.3 (may collapse with second-program TPM/EM; must NOT collapse with pilot TPM for their own usability acceptance) |
| Security reviewer | §3.6.3 (may collapse with Platform DRI for *advisory* review only; must remain independent for *enforce-mode threat-model approval*) |

Every work item's `Owner` column in §11's tables (Platform DRI, Platform
engineer, Evaluator, Security reviewer, Program operators, Pilot TPM/EM)
refers back to these same names. ADF-W0.2 cannot close until real people
(or an explicit single-operator acknowledgment, see below) are bound to
each role.

Separately, `governance/roles/ai-safety-approver.md` already exists
(filed 2026-06-11, still `UNNAMED`/unfilled) for a materially similar
function — approving the AI governance framework and co-signing feature
graduation records — under an earlier spec (`prod-vis.md` WS-5). Its
scope overlaps significantly with ADF's **Independent evaluator** role
(§3.6.2/3.6.3: autonomy graduation co-signer, model/prompt
certification).

## Current operating reality (as observed, not decided)

Vertex is currently operated end-to-end by a single person
(the Platform DRI) acting as engineer, program owner, and TPM
simultaneously. The spec's own independence table (§3.6.3) already
anticipates this and draws the line explicitly: some collapses are fine
(DRI + pilot operator; pilot TPM + primary annotator), others are not
(no self-certification of enforce-mode threat-model approval; no
solo dual-labeling for corpus κ; no self-certification of a TPM's own
usability acceptance).

## Decision (proposed — pending user sign-off)

1. **Bind every collapsible role to the single named operator.**

   | Role | Named as |
   |---|---|
   | Vertex platform DRI | Single operator (Platform DRI) |
   | Affected program owner (XPF, Armada) | Single operator (Platform DRI) |
   | Program governance owner | Single operator (Platform DRI) |
   | TPM/EM owner | Single operator (Platform DRI) |
   | Audience-policy owner | Single operator (Platform DRI) |
   | Business/operations validator | Single operator (Platform DRI) |
   | Pilot TPM | Single operator (Platform DRI) |
   | Primary annotator | Single operator (Platform DRI) |
   | UX reviewer (for programs other than the one under acceptance test) | Single operator (Platform DRI) |
   | Security reviewer — **advisory only** | Single operator (Platform DRI) |

2. **Explicitly leave independence-required roles UNFILLED, not
   force-collapsed**, and record what stays gated as a result:

   | Role | Status | What it blocks while unfilled |
   |---|---|---|
   | Second annotator and adjudicator | **UNFILLED** | Corpus κ can never be measured solo (ADR-0006 Amendment A2/A3/A4 already documents this for the `nova`/REV corpus; the same constraint applies to any ADF-W0.4/W6.2 fleet-certification corpus). Single-annotator results stay "preliminary," never "certified." |
   | Security reviewer — **enforce-mode threat-model approval** | **UNFILLED** | ADF-W0.11's threat-model re-review may be performed and drafted solo, but cannot be marked *approved for enforce mode* until a second reviewer signs. Observe-mode operation is unaffected. |
   | UX reviewer for the pilot program's own usability acceptance | **UNFILLED** (structural — no one can review their own usability acceptance) | Pilot-program usability sign-off (ADF-W5.14) stays self-reported, not independently reviewed, until a second-program TPM/EM exists to fill this. |
   | Independent evaluator (model/prompt certification) | **Proposed: reuse/rename `governance/roles/ai-safety-approver.md`** rather than create a duplicate charter — see Action Items. Remains UNFILLED either way until a named person accepts it. | Feature graduation to `frontier_eligible: false`-by-default / any AI output relied on beyond advisory-with-review stays ungraduated. This is already the current state (PB-32: 14/14 features `frontier_eligible: true`, unreviewed). |

3. **Record, do not hide, the resulting scope limit.** Every ADF work
   item whose acceptance evidence depends on an independence-required
   role (ADF-W0.4 sample-plan/κ, ADF-W0.11 enforce-mode security
   approval, ADF-W5.12 autonomy graduation, ADF-W6.2 fleet certification)
   stays honestly labeled "single-operator / advisory / preliminary" in
   `specs/arch-data-fix.md` §11 until a second person is named. This
   mirrors the precedent already accepted in ADR-0006 Amendments A2-A4
   for the REV corpus.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Force-collapse every role including independence-required ones | Explicitly forbidden by the spec's own §3.6.3 table; would let one person self-certify their own AI-safety and security sign-off, defeating the purpose of the gate. |
| Recruit a second reviewer now to unblock everything | Not something this session can decide or execute — it is calendar/organizational, tracked as an open item below rather than assumed. |
| Leave every role `UNNAMED` (status quo) | Leaves ADF-W0.2 permanently open and every dependent work item's `Owner` column meaningless; the spec cannot progress past Phase 0 exit criteria without at least the collapsible roles bound. |

## Action items (to close on Accept)

1. User confirms or amends the name binding in the table above.
2. Decide: does `governance/roles/ai-safety-approver.md` get renamed/
   repurposed to serve as ADF's "Independent evaluator" charter (single
   role, two spec references), or does a second, separate
   `governance/roles/adf-independent-evaluator.md` get filed? Recommend
   renaming/repurposing — the function (approve AI governance framework,
   co-sign graduation records, remain independent of the implementer) is
   identical; a duplicate charter would just be two unfilled files
   tracking the same open gap.
3. `specs/arch-data-fix.md` §11 rows that depend on an independence-
   required role get an explicit "advisory/preliminary, pending second
   reviewer" annotation (not a blocking change — most already say
   "human-owned"/"Not started").

## Consequences

**Easier:** every ADF work item's `Owner` column now resolves to a real
person; Phase-0 exit no longer has an unresolvable "who signs this"
ambiguity for the roles that legitimately can collapse to one person.

**Harder:** the roles that legitimately cannot collapse stay honestly
blocked rather than rubber-stamped — corpus certification, enforce-mode
security approval, and independent usability review all remain
"advisory/preliminary" until a second person is recruited. This is a
real capability gap, not a paperwork gap, and this ADR does not attempt
to paper over it.

## Follow-up resolution (2026-07-14)

As part of a live decision sequence continuing Phase-0/Slice-5 closeout,
the Platform DRI was asked directly: name a second person for the three
UNFILLED independence-required roles, accept advisory-only status
indefinitely, or use time-separated re-annotation as a partial stopgap.

**Decision: accept advisory-only status indefinitely.** No second person
exists today. This is not a new limitation — Decision 2's own text above
already named exactly what stays gated (corpus κ/fleet certification,
enforce-mode security approval, independent-evaluator AI graduation) — but
this follow-up formally closes the question rather than leaving it as a
recurring open item each session. Concretely:

- **Second annotator/adjudicator**: stays UNFILLED. Fleet certification
  (ADF-W6.2) and any autonomy-ladder evidence requiring measured κ remain
  unavailable. Single-annotator results stay "preliminary" per Decision 2.
- **Enforce-mode security reviewer**: stays UNFILLED. The threat-model
  review (`governance/threat-model.md` v1.1) remains advisory-only;
  observe-mode operation (see Changelog v1.23's XPF mode flip) is
  unaffected, since observe carries no blocking consequence to approve.
- **Independent evaluator**: stays UNFILLED (`governance/roles/ai-safety-approver.md`
  remains unnamed). No AI feature graduates past advisory-with-review;
  this changes nothing operationally today since zero features have
  attempted that graduation yet (ADF-W5.1/W5.3's tier-graduation work is
  itself gated on the same evaluation-harness evidence ADF-W0.15 partially
  built).

**What this does NOT change**: nothing currently built or running is
affected — every quality gate, cockpit, alert, and autonomy-ladder
mechanism operates identically whether this role is filled or not, because
none of them currently require independent-reviewer sign-off to function
in observe mode or below. This decision only formally closes the "is this
still open" question; it does not relax any safety floor.

**Reopening condition**: if a real second person becomes available (a
second-program TPM/EM, a security co-reviewer, or a named AI evaluator),
this ADR should be revisited — the role bindings above are additive, not a
permanent structural ceiling.

## References

- `specs/arch-data-fix.md` §3.6.2 (deferral/escalation), §3.6.3
  (independence-collapse table), §11.1a row ADF-W0.2
- `governance/roles/ai-safety-approver.md` (existing unfilled charter,
  proposed reuse target)
- `governance/decisions/0006-consolidated-human-decision-gates.md`
  Amendments A2-A4 (precedent: single-annotator results stay
  "preliminary" until κ is measured)
- Related: ADR-0012 (governance tracking, ADF-W0.1)

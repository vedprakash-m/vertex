# ADR-0018: ADF-W5.6 status/brief/cockpit duplication finding (explicit deferral)

**Date:** 2026-07-13
**Status:** Accepted (documented finding; consolidation explicitly
deferred; risk-state dual-store follow-up investigated and resolved as
"no fix needed" by the user, 2026-07-13)
**Workstream (from `specs/arch-data-fix.md`):** ADF-W5.6
**Author(s):** Vertex engineering (investigation delegated, findings reviewed
and synthesized before recording)

## Context

ADF-W5.6's consolidation clause is explicitly conditional: *"Consolidate
status/brief builders only if cockpit evidence shows duplicate computation
or inconsistent output."* This ADR is that documented finding, satisfying
the acceptance evidence's "Documented duplication finding or explicit
deferral."

## Finding

**No genuine duplicate computation exists between `status.py`/`brief.py`
and `cockpit_builder.py`.**

- `brief.py` (692 lines) has zero conceptual overlap with cockpit --
  it builds prioritized now/watch/staged action lines from claims/
  decision-asks/contradictions/incident-learnings, a materially different
  feature surface. No shared formula, no shared metric name.
- `status.py` (807 lines) computes `blocker_count` as a count of
  `issue_projection` entries with `severity == "block"` (freshness/
  overdue/IcM-derived). `cockpit_builder.py`'s `ProgramCockpitSummary.
  blocker_count` (539 lines) counts active risks with
  `compute_risk_score >= 9`, sourced from `load_risk_register()`. These
  are **different concepts** answering different questions ("what's
  operationally blocking right now" vs. "how many risks are severe"), not
  the same computation implemented twice.
- `status.py`'s `readiness_percent` (a real, always-computed QG pass-rate)
  has no cockpit counterpart at all -- `ProgramCockpitSummary.
  readiness_percent` is currently hardcoded `None` in
  `cockpit_builder.py` (a documented gap, not a duplication).

**A real, but separate, issue was found, investigated further, and found
to be already substantially mitigated** (follow-up investigation,
2026-07-13, prompted by the user starting the flagged follow-up task):
`status.py`'s `risk_register_summary` reads risk state via
`project_risk_entries(load_program_facts(...))` (the fact-store
projection), while `cockpit_builder.py` reads risk state via
`load_risk_register()` (the canonical `risk_register.yaml` file) --
different stores for the same underlying concept.

**This session's initial framing overstated the risk.** The follow-up
investigation found `save_risk_register()`
(`src/core/risk_register_engine.py:109-122`) already performs a
**synchronous dual-write** on every save: it writes the YAML file, then
calls `_sync_risk_facts()` in the same call, which appends an ACTIVE
`risk.entry` fact per current risk and CLOSES any fact whose risk no
longer exists. `upsert_risk_from_signal` never writes facts independently
-- it always routes through `save_risk_register`. On XPF (the only
program with real data), the two stores are **exactly in sync**: 1,569
risk rows in `risk_register.yaml`, 1,569 active `risk.entry` facts,
1,569 distinct natural keys -- a perfect 1:1 match, verified by direct
read-only inspection. `risk.entry`'s authority family (`judgment`, per
`vertex/policies/source_authority.yaml:21`) is explicitly pinned to
`legacy` mode on XPF (`programs/xpf/fact_store_sor.yaml`), so no
reality-backed override path is even active.

**Revised assessment**: the theoretical divergence window is narrow (a
crash between the YAML write and the fact-sync call inside the same
function, not an ongoing structural gap) and has never manifested on the
one real program this platform operates against today. This is closer to
"no active bug, dual-write already does the job" than "two authorities
that can disagree" -- the original framing was corrected once real
evidence existed to check it against, per this session's own discipline
of not building fixes for problems that aren't observed to occur.

This remains a data-layer question, not a presentation-layer
duplication -- still out of ADF-W5.6's scope either way. Folding it into
a builder-consolidation pass would conflate two different problems.

## Decision

**Consolidation is explicitly deferred** -- the conditional trigger
("duplicate computation or inconsistent output") was investigated and not
met in the form the item anticipated (shared formulas producing divergent
answers). `status.py` (807 lines, larger than `cockpit_builder.py` itself)
and `brief.py` (692 lines) are NOT rewritten to delegate to cockpit
builders; both remain their own specialist surfaces per Section 10.5
("`status` remains the fast edition summary... `cockpit` composes and
explains; it does not replace the specialized commands").

**`vertex cockpit show` is confirmed as the canonical human entry point**
(Section 10.5's Slice-5 target) -- already the default lightweight path
since ADF-W0.8, now reinforced by ADF-W5.6's own onboarding walkthrough
(new `_ONBOARDING_WALKTHROUGH` in `src/commands/cockpit.py`, shown on a
program's first `cockpit show`/`build` run per Section 10.7) actively
directing new operators there.

**The dual-store risk-state finding was flagged, investigated further,
and found to be already substantially mitigated** by the existing
synchronous dual-write in `save_risk_register()` -- confirmed via direct
inspection of XPF's real data (exact 1:1 match, 1,569 rows each side).
No urgent fix is required. What remains a genuine open question for the
user (not autonomously decided, per this session's established
human-gate discipline for authority-type decisions) is documented
separately below rather than folded into an autonomous implementation.

**No code changed in `status.py`/`brief.py`** -- "status p95 non-
regression" is trivially satisfied since nothing about `status.py`'s
computation or call path was touched.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Rewrite `status.py`/`brief.py` to delegate to `cockpit_builder.py` | The investigation found no genuine duplicate formula to consolidate -- `status.py` computes real facts (`readiness_percent`, issue-projection blockers) `cockpit_builder.py` doesn't compute at all; forcing a delegation would mean EXPANDING `cockpit_builder.py`'s scope to match `status.py`'s (a much larger, riskier change than this item's own conditional trigger anticipated), not removing duplication. |
| Fix the dual-store risk-state gap as part of this item | Conflates a presentation-layer question (should two CLI surfaces share a builder) with a data-layer question (which store is canonical for risk facts) -- the latter is real, tracked debt scope, not this item's job. |

## Follow-up resolution (2026-07-13)

The user started the flagged follow-up task. Investigation found:
`save_risk_register()` already performs a synchronous dual-write (YAML
then `_sync_risk_facts()`, same call); XPF's real data shows an exact
1,569/1,569 match between `risk_register.yaml` rows and active
`risk.entry` facts; `risk.entry`'s authority family (`judgment`) is
pinned to `legacy` mode on XPF, so no reality-backed override is even
active. Presented three options (leave as-is / lightweight consistency
check / full read-path overlay matching the milestone-commitment
pattern) -- the user chose **leave as-is, no fix needed**. No code
changed as a result of the follow-up.

## Consequences

**Easier:** `status.py`/`brief.py` stay stable, untouched, zero
regression risk; the dual-store question was investigated to a real,
evidence-based conclusion (not left as an open theoretical concern) and
closed without unnecessary new engineering.

**Harder:** none -- the narrow crash-window risk (a failure between the
YAML write and the fact-sync call inside `save_risk_register`) remains
theoretically possible but unaddressed; the user explicitly accepted
this as acceptable risk given zero observed occurrence.

## References

- `src/commands/status.py`, `src/commands/brief.py`, `src/core/cockpit_builder.py`
- `src/core/program_fact_store.py::project_risk_entries`,
  `src/core/risk_register_engine.py::load_risk_register`
- `specs/arch-data-fix.md` §10.5, §10.7, ADF-W5.6
- Related: the fact-store SoR-flip debt track (D-05 and related items,
  pre-existing, tracked separately from this spec)

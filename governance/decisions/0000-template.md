# ADR template — Architectural Decision Record

Copy to `decisions/NNNN-<short-slug>.md` and fill in. Numbering is sequential
and never reused.

```markdown
# ADR-NNNN: <Title>

**Date:** YYYY-MM-DD
**Status:** Proposed | Accepted | Superseded (by ADR-NNNN)
**Workstream (from `specs/prod-vis.md`):** WS-N
**Author(s):** name(s)
**Approver(s):** name(s) — required for Accepted

## Context

What is the issue we're seeing that motivates this decision? What forces are
at play (technical, organizational, time, regulatory)?

## Decision

What did we decide? Be specific — name files, modules, and config keys.

## Consequences

What becomes easier? What becomes harder? What did we explicitly reject and
why?

## References

- `specs/prod-vis.md` §<X> (workstream / appendix)
- Related ADRs (by number)
- TRACKED specs: PRD §<X>, Tech §<X>, UX §<X>
```

## Why this template

- **Tracked:** every ADR is git-tracked (the drift test in
  `scripts/check_spec_drift.py` fails on ADRs placed under ignored paths).
- **Immutable once accepted:** status changes are appended, not edited.
- **Cross-referenced by ID:** the WS that drove the decision references the ADR
  by number, not by content (avoids the "stale link" anti-pattern).

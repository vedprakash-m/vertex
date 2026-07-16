# ADR-0012: Governance tracking for `specs/arch-data-fix.md` (ADF-W0.1)

**Date:** 2026-07-13
**Status:** Accepted (2026-07-13)
**Workstream (from `specs/arch-data-fix.md`):** ADF-W0.1
**Author(s):** Vertex engineering (drafted per live decision-by-decision session)
**Approver(s):** the Platform DRI (per ADR-0013) — 2026-07-13, Option B

## Context

`specs/arch-data-fix.md` v1.4 is the governing spec for the current
implementation wave (~66 work items, Sections 11.1a-11.6a). Per its own
§0 "Governance prerequisite":

> This file is ignored by the repository's current `specs/*` policy. Before
> implementation begins, the approved specification must either be
> explicitly tracked or every binding decision, work item, gate, and
> acceptance criterion must be copied into tracked canonical specs, ADRs,
> and backlog records. An ignored machine-local file cannot be the sole
> authority for implementation.

Only three specs are tracked in git: `specs/vertex-prd.md`,
`specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md` (see
`.gitignore`'s `specs/*` + three explicit `!specs/vertex-*.md` allow
rules). Every other spec, including `arch-data-fix.md`, is gitignored and
local-only by long-standing project convention (this pattern predates ADF
— see the `.archive/specs/` handling for `backlog.md`, `onboard.md`,
`prod-vis.md`, `consolidated.md`, all of which were reconciled into the
three tracked specs rather than tracked themselves).

Two paths satisfy the prerequisite:

| Option | What it means | Cost | Precedent |
|---|---|---|---|
| **A. Track the file itself** | Add an explicit `!specs/arch-data-fix.md` allow-rule to `.gitignore`, alongside the three canonical specs. | One-time; keeps ADF's ~3,600-line internal detail (fault taxonomy, event schemas, work-item tables) in git going forward, including in-flight edits. | None — every prior working spec (`backlog.md`, `prod-vis.md`, `consolidated.md`, `remains.md`) was reconciled into the three canonical specs and archived, never tracked directly. |
| **B. Copy-forward binding content** | Leave `arch-data-fix.md` gitignored (consistent with the established convention). As each work item is designed or completed, copy its binding decision — the gate, invariant, schema, or acceptance criterion, not the full prose — into a tracked ADR (`governance/decisions/`), the tech spec, or `tests/contracts/`. | Ongoing, incremental; already the pattern in use this session. | `governance/decisions/adf-w3-1-provider-capability-report.md`, `adf-cpk-dependencies.md`, `adf-gate-policy.md`; every INV-ADF-* invariant enforced by a tracked contract test; every QG-2x/3x gate registered in `src/core/quality_gates/gate_registry.py` (itself tracked). |

## Decision

**Adopt Option B**, formalized retroactively: `arch-data-fix.md` stays
gitignored, matching the established convention for in-flight working
specs. Every binding decision continues to be copied into a tracked
artifact at the point it is designed or implemented, not deferred to a
final reconciliation pass. Concretely, "binding" means any of:

- an **invariant** (`INV-ADF-*`) — must have a corresponding contract
  test under `tests/contracts/`;
- a **quality gate** (`QG-2x`/`QG-3x`) — must be registered in
  `src/core/quality_gates/gate_registry.py`;
- an **event schema** (Appendix A.2) — must be registered in the ledger's
  schema registry with a contract test;
- a **human decision gate** (RACI, budget ratification, threat-model
  sign-off) — must produce its own ADR or governance record, not just a
  spec-file row;
- the **Appendix D agent operating contract** — copied verbatim into
  `CLAUDE.md` or an equivalent tracked operating-contract file so a fresh
  clone has the binding constraints even without `arch-data-fix.md`.

Rejected: Option A. Tracking the file directly would be the first
exception to the project's own established working-spec convention
(three tracked canonical specs; everything else gitignored and
reconciled-then-archived). It would also freeze a ~3,600-line
in-progress document mid-edit in git history rather than its stable,
distilled outcomes — noisier diffs for no governance benefit over Option
B, which already produces the same tracked evidence per binding item.

## Consequences

**Easier:** the tracked-artifact surface stays small and stable (ADRs,
contract tests, gate registry, canonical specs) instead of growing by
one more large gitignored-but-somehow-authoritative file; the existing
per-item evidence discipline (already used for every ADF item landed
this session) satisfies the prerequisite without new process.

**Harder:** there is no single tracked file a reviewer can diff to see
"all of arch-data-fix.md's history" — provenance lives across multiple
ADRs and contract tests. Mitigated by `specs/arch-data-fix.md` §11's
per-item status tables (11.1a-11.6a), which each cite the tracked
artifact (test file, ADR, module) as evidence; those tables are the
local index, this ADR is what makes the underlying claim ("every binding
item is copied forward") a checked commitment rather than an assumption.

**Explicitly closes ADF-W0.1** once Accepted: the acceptance evidence
required ("Fresh clone contains the approved implementation contract")
is satisfied by (a) this ADR being tracked, (b) the Appendix D operating
contract being copied into a tracked file (tracked separately — see
Action Item below), and (c) the existing per-item tracked-evidence
pattern continuing for all remaining work items.

## Action items (to close on Accept)

1. Copy `specs/arch-data-fix.md` Appendix D (agent operating contract)
   into a tracked file — recommend `governance/adf-agent-operating-contract.md`
   (new) or a dedicated section of `CLAUDE.md`. Not yet done as of this
   ADR's Proposed status.
2. Confirm `specs/arch-data-fix.md` §11's status tables continue citing
   tracked evidence per row (already the practice for every row landed
   this session — no change needed, just continued discipline).

## References

- `specs/arch-data-fix.md` §0 (Governance prerequisite), §11.1a row
  ADF-W0.1
- `.gitignore` (`specs/*` policy + three canonical allow-rules)
- `governance/decisions/adf-w3-1-provider-capability-report.md`,
  `adf-cpk-dependencies.md`, `adf-gate-policy.md` (existing copy-forward
  precedent)
- Related: ADR-0013 (RACI/decision-rights naming, ADF-W0.2)

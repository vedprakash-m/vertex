# Fact Store Source-of-Record (SoR) Cutover Rehearsal Runbook

**Tracked, generic counterpart of a private, workspace-specific runbook.**
This file ships with the repo so a fresh clone has an operator runbook for
the fact-store SoR flip without any program-specific detail (issue numbers,
program IDs, incident dates). If your local workspace has a more detailed
version tailored to a specific program under `docs/` (gitignored), prefer
it for day-to-day operation and treat this file as the canonical baseline
to keep in sync.

**Relevant design docs:** `specs/vertex-tech-spec.md` (`fact_sor_state.py` —
FactSorState legacy/shadow/primary SoR flip, per-family resolution, clean-cycle
gate evaluation) and the archived architecture-remediation spec's AF-5 (Fact
Store Convergence — `.archive/specs/arch-fix.md`, Part B, not yet authorized;
see `specs/backlog.md` §7), which extends this flow with a dedicated
`SoRDriftPolicy` and a per-family cutover ledger.

## Why this runbook exists

The fact store supports three SoR modes (below), but `resolve_fact_sor_mode()`
defaults to `legacy` when no explicit mode is configured. Flipping a program to
`primary` is real, high-blast-radius operator execution against live program
state — this runbook makes that execution concrete, reversible, and
proof-bearing.

> Do **not** run any step in this runbook against live program state without
> the baseline hardlock in place (`vertex admin baseline --lock`) and an
> explicit go-decision from the program's accountable operator/DRI.

## SoR modes (recap)

| Mode | Meaning | Set via |
|------|---------|---------|
| `legacy` | YAML/JSONL snapshots authoritative; Fact Store not read | default |
| `shadow` | Fact Store written alongside legacy; legacy still authoritative; parity observable | `VERTEX_FACT_SOR=shadow` or per-program state |
| `primary` | Fact Store authoritative; legacy becomes the projection | `VERTEX_FACT_SOR=primary` or per-program state (post-cutover) |

## Pre-flight (do once, on a COPY of program data)

1. **Snapshot the workspace.** Copy the target program's `programs/<program_id>/`
   and `output/` trees to a scratch location. Rehearse there first — never on
   the live tree.
2. **Confirm baseline integrity.** Ensure the program's most recent issue has
   a valid archive snapshot (`issue_<n>.snapshot.json`). If it does not,
   either confirm that issue into the archive, or name the prior confirmed
   issue as the parity-window anchor in `trusted_baseline.yaml`. The flip's
   parity window needs a real anchor.
3. **Lock the baseline.** `vertex admin baseline --lock` so no flip step can
   overwrite a trusted/locked issue.

## Phase A — Backfill + shadow (idempotent, reversible)

```bash
# A1. Idempotent legacy -> fact backfill ETL. Safe to re-run.
vertex admin migrate-legacy-state --program <program_id> --dry-run
vertex admin migrate-legacy-state --program <program_id>

# A2. Enter shadow mode (Fact Store written, legacy still authoritative).
export VERTEX_FACT_SOR=shadow      # PowerShell: $env:VERTEX_FACT_SOR = "shadow"

# A3. Broad parity across all fact families.
vertex facts parity-check --program <program_id>           # expect 0 divergences
vertex doctor --flip-parity                                # issue-anchored parity window
```

**Gate A (must all hold before proceeding):**
- `facts parity-check` → 0 signal-count / signal-ID / family divergences.
- `doctor --flip-parity` → green for every mutable family (including
  `workstream_associations`).
- Capture the output of both as proof artifacts (see "Proof capture" below).

## Phase B — Sustained dual-read shadow window

```bash
# B1. Run a sustained dual-read window across normal operations (gather/report/confirm).
vertex facts dual-read-log --program <program_id> --window <N-cycles>

# B2. Pin the current fact snapshot to the confirmed anchor issue and watch for drift.
vertex facts pin-snapshot --program <program_id> --issue <anchor_issue>
vertex facts detect-drift --program <program_id>            # expect: no post-pin drift
```

**Gate B:** the dual-read log shows zero quarantined divergences across the
window, and `detect-drift` reports none after the pin. This is the evidence
that shadow and legacy agree on *live* operations, not just a point-in-time
parity snapshot.

## Phase C — Rollback drill (prove reversibility BEFORE the flip)

```bash
# C1. Prove the rollback path works end-to-end on the rehearsal copy.
vertex admin fact-store-flip --program <program_id> --preview
vertex admin fact-store-flip --program <program_id> --execute
# ...verify primary reads...
vertex rollback --to shadow                          # or admin baseline rollback drill
# C2. Confirm post-restore consistency.
vertex facts parity-check --program <program_id>     # expect 0 divergences after restore
```

**Gate C:** the system returns to a byte-consistent shadow state after
rollback, and parity is still 0. **Do not proceed to the real cutover until
the rollback drill passes on the rehearsal copy.**

## Phase D — Cutover (preview → execute → commit)

Only after Gates A–C pass on the rehearsal copy, and with an explicit
go-decision:

```bash
vertex admin fact-store-flip --program <program_id> --preview   # shows supported_families + checkpoint plan
vertex admin fact-store-flip --program <program_id> --execute    # captures checkpoint, flips to primary
vertex doctor --flip-status                                      # confirm mode == primary, integrity OK
vertex admin fact-store-flip --program <program_id> --commit     # finalize; legacy becomes projection
```

**Gate D (sign-off):** `doctor --flip-status` reports `primary` with
WAL/integrity OK and no shim divergence; operator records sign-off in
`platform_proof_log.yaml` via `vertex admin platform-proof --plan`.

## Proof capture (required artifacts)

Save each to `output/<edition>/proofs/sor-cutover/` (or attach to the
platform-proof log):

- `parity-check.<ts>.json` (Gate A)
- `flip-parity.<ts>.txt` (Gate A)
- `dual-read-log.<ts>.jsonl` (Gate B)
- `detect-drift.<ts>.txt` (Gate B)
- `rollback-drill.<ts>.txt` (Gate C)
- `flip-status.<ts>.json` (Gate D)
- operator sign-off entry in `platform_proof_log.yaml`

## Abort / rollback at any point

```bash
vertex admin fact-store-flip --program <program_id> --abort     # discard an in-flight flip
vertex rollback --to <legacy|shadow>                             # restore prior SoR mode
unset VERTEX_FACT_SOR                                            # PowerShell: Remove-Item Env:VERTEX_FACT_SOR
```

If a checkpoint restore is needed, the flip captured a SQLite backup-API
checkpoint; restore it and re-run `facts parity-check` to confirm consistency
before resuming.

## Definition of done

- Gates A–D all passed on the live program with captured proof artifacts.
- `doctor --flip-status` reports `primary` and remains green across a
  subsequent gather→report→confirm cycle.
- Operator sign-off recorded for the program.

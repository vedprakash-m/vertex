# Operator & On-Call Runbook — Vertex REV / Activation

**Status:** v1.0 — 2026-06-30 (activation.md §6.14.15 / §6.15.5 / O-17)
**Owner:** Vertex platform on-call
**Scope:** the **operator-paced failure modes** of the REV activation substrate
(the cycle, the ladder, the triage loop, the render). This is the *operations*
runbook — distinct from Track F's *automation* runbook (which covers the
manual-export → deposit step and the Graph-API roadmap).

> **Governing spec:** [`specs/activation.md`](../specs/activation.md). Every
> scenario below maps to a named acceptance gate (AG-N) and a problem
> statement (PS-N). When a scenario fires, the on-call's job is to **return
> the cycle to a clean (`authority_valid`) state or explicitly hold
> publication** — never to silently work around the signal.

---

## 0. Health-check commands (first stop)

```bash
# One-shot activation + evidence status (the §6.13 spine).
python scripts/verify_activation.py --program xpf

# Latest REV cycle snapshot (cycle_status, shield_degrade, extraction_degraded,
# source_unreachable, enumerated/candidates_staged).
cat programs/xpf/_rev/last_cycle.json

# Bounded cycle history (last 10) — look for flaps / trends.
cat programs/xpf/_rev/cycle_history.jsonl

# Triage telemetry summary (accept/reject/edit rates, time-to-triage).
# (Rendered by the triage_telemetry summarizer; see §6.10.)
```

The cycle classes (§6.14.3) a cycle can be in:

| Class | Meaning | Counts toward authority? |
|---|---|---|
| `authority_valid` | clean + LLM-used + real EML — the only class that advances the ladder | ✅ |
| `quality_valid_not_clean` | LLM used, no degrade, but empty or otherwise not clean | ❌ |
| `publication_valid_degraded` | completed but degraded — publishable **with a banner**, never authority-valid | ❌ |
| `incomplete` | did not complete | ❌ |

---

## 1. Scenario: `shield_degrade` flaps (Prompt Shields unavailable)

**Signal:** `last_cycle.json` → `shield_degrade: true`; `verify_activation.py`
AG-3-CLEAN-CYCLE fails with `shield_degrade=true`.

**Meaning:** Azure Content Safety returned `VERDICT_UNAVAILABLE` (or the scan
raised), so chunks were admitted local-only. This is **visible degrade, never
silent** — but a degraded cycle is not `authority_valid`.

**Action:**
1. Confirm Azure CS is provisioned (`vertex doctor --shields`). If unprovisioned,
   this is the P1/§6.3 blocker — file/escalate the IT ticket; do not advance the ladder.
2. If Azure CS is provisioned but flapping, treat it as a transient upstream
   outage: the cycle is still **publication-valid** (it completed). Publish with
   the visible degrade banner if the issue is time-critical; otherwise re-run
   `vertex gather` once Azure CS recovers and confirm a clean cycle.
3. **Never** count a `shield_degrade` cycle toward AG-3/AG-4. The RK-1
   pilot-local exception applies *only* to the AG-1 proof, not to rollout
   authority.

---

## 2. Scenario: recurring `extraction_degraded` (LLLM falling back)

**Signal:** `last_cycle.json` → `extraction_degraded: true` (or
`cycle_status: extraction_degraded`); `llm_fallback_count > 0`.

**Meaning:** one or more EMLs hit the ≤5s LLM timeout / 5xx and fell back to
the deterministic extractor (§6.14.3 / AG-12). Degradation is **per-item**;
the cycle completes so publication never deadlocks, but it is not authority-valid.

**Action:**
1. Check the LLM deployment (`VERTEX_AI_DEPLOYMENT`) health + latency. A
   30s/EML extraction severely degrades `vertex gather` — this is the §6.9 SLO.
2. If the timeout is too aggressive for long multi-thread EMLs, evaluate the
   bounded-async / token-scaled timeout model (§6.9) rather than silently
   raising the budget.
3. Re-run once the LLM recovers; confirm `extraction_degraded: false` and
   `llm_fallback_count: 0` before any ladder advance.
4. On an **already-primary** family, a degraded cycle publishes **with the
   "downgraded evidence" banner** and the operator may hold (§6.14.3).

---

## 3. Scenario: `source_unreachable` (ADO/Kusto/IcM outage)

**Signal:** `last_cycle.json` → `source_unreachable: true` (stop_category
`provider_limited`, breached_budget `rate_limit`/`forbidden`).

**Meaning:** a counter-source was down/throttling during `gather`. Per §6.14.4,
the cycle succeeds from the last-known `ProgramReality` cache and **bypasses
the AG-9 conflict check** (no `disputed` verdict on stale data).

**Action:**
1. **Publication is safe** — the weekly path is never blocked by an upstream
   outage (AG-12). Serve cached reality.
2. Note that **conflict detection was bypassed this cycle** — re-run `gather`
   once the source recovers so `disputed` flags carry fresh `as_of` timestamps.
   Do not make authority claims based on a `source_unreachable` cycle.

---

## 4. Scenario: κ / precision drop on a sustaining re-check

**Signal:** the monthly sustaining re-label (§6.14.16) shows κ < 0.7 or the
Wilson lower bound precision < 0.80 for a flipped family.

**Meaning:** the corpus that legitimized the flip has rotted (templates/
semantics drifted). This is the §6.14.20 corpus-rollback trigger.

**Action:**
1. **Auto-demote** the family shadow→ (un-flip) via the §6.14.10 rollback path.
2. **Supersede** (do not delete) facts projected under the bad authority.
3. Alert the annotator + 2nd labeler; re-label a batch; recompute κ. Do not
   re-flip until the certified corpus clears the floor again.

---

## 5. Scenario: revoke not reflected in the next report

**Signal:** `vertex ledger triage revoke` succeeded but the cited claim still
appears in the next `vertex report`.

**Meaning:** revoke→reflected-in-render exceeded the synchronous budget, or
projection did not refresh. Per §6.14.7, revoke = supersede + retraction event
+ synchronous projection refresh.

**Action:**
1. Re-run `vertex ledger project` (or `vertex report`) to force projection.
2. If render still shows the claim, check the `operator.correction.v1`
   tombstone landed and the supersession chain resolved.
3. **Already-delivered newsletters are immutable** — a now-retracted fact that
   reached an already-sent issue triggers the **operator correction protocol**
   (§6.14.10): a flagged correction note in the *next* issue + an operator
   alert. Use the wording in §7 below. Never silently remove from a sent issue.

---

## 6. Scenario: authority-flip rollback drill / real rollback

**Signal:** parity regression, quality breach, or operator call to reverse a
shadow→primary flip (§6.14.10 / AG-18).

**Action:**
1. The rollback **drill** is run *before* the real flip. Trigger / decider /
   RPO / RTO are pre-defined in the family's flip checkpoint.
2. Facts projected under primary during the rolled-back window are
   **superseded, not deleted**.
3. Run the post-rollback counterfactual render and confirm it is correct.
4. If a now-retracted fact reached an already-delivered newsletter, follow §7.

---

## 7. Operator correction protocol (delivered-issue retraction)

When a fact that has already been **sent** in a newsletter is later retracted
(revoke, rollback, or corpus-rollback), it **cannot be un-sent**. The protocol
(§6.14.10):

1. Emit an **operator alert** to the program DRI immediately.
2. In the **next** issue, render a visible **correction note** in the affected
   section, e.g.:
   > *Correction: the [date] issue cited [fact] as completed, sourced from
   > [EML]. That citation has been retracted ([reason]). Current status: [state].*
3. Never silently drop the claim from the historical record — the ledger is
   append-only; the correction is itself an `operator.correction.v1` event
   with full lineage.

---

## 8. Scenario: ADO schema drift

**Signal:** `SchemaDriftError` during `gather` (with
`VERTEX_ADO_SCHEMA_DRIFT_GUARD=1`), or a warning log line
`ADO schema drift detected` (§6.14.13).

**Meaning:** an upstream ADO field/status/shape changed. The fail-closed guard
refuses to default a vanished required field (protecting the AG-9 conflict
check from a degenerate state digest).

**Action:**
1. Run `vertex doctor --channels` to compare the field map against the
   expected schema.
2. If a field was renamed/removed upstream, update `ADO_BATCH_FIELDS` /
   `ADO_REQUIRED_FIELDS` in `src/core/ado_schema_drift.py` and the reads in
   `ado_hydration.py` to match the new contract.
3. Do **not** disable the guard to "unblock" — a silent state default is
   exactly the failure §6.14.13 prevents.

---

## 9. Scenario: multiple candidate fact-store databases for one program (PS-14 split-brain)

**Governing spec:** `specs/fix-data-flow.md` PS-14 / Track K (§6.11).

**Signal:** `vertex doctor --storage` reports the "Fact Store Location" check
as `warn`, listing one or more stray `vertex.sqlite3` files besides the
canonical, `db_root`-resolved path — or `reality_store.py`'s
`_resolve_reality_db_root` fallback logs a `CRITICAL` line
(`"no db_root/VERTEX_DB_PATH supplied — falling back to ..."`) anywhere in
recent logs.

**Meaning:** More than one candidate SQLite database exists for a single
program (typically: the canonical path production code always resolves via
an explicit `programs_root`/`db_root`, plus a stray copy at
`~/.vertex/<id>/vertex.sqlite3` created by some script, test, or ad-hoc
session that omitted both). Which one gets read/written depends silently on
which path arguments a caller supplies — a real operational hazard, not just
housekeeping clutter, since work done through a stray database is invisible
to every stage that uses the canonical path.

**Action:**
1. Run `vertex doctor --storage` and read the "Fact Store Location" check's
   `stray_databases` metadata — it lists each stray path and its row count.
2. **Confirm canonicality before touching anything.** The canonical path is
   whichever one production code actually resolves for this program — for
   the standard layout, that's `<programs_root>.parent/<program_id>/vertex.sqlite3`
   (i.e., `programs_root.parent`, NOT `programs_root` itself — see
   `program_fact_store.py`'s `resolved_db_root = programs_root.parent`). The
   doctor check's `canonical_path` field already resolves this the same way
   production code does — trust it over any manual guess.
3. **For each stray database, before archiving or deleting it:**
   - Confirm it is *not* read by any production call path (grep for any
     script, cron job, or manual command that might construct
     `ProgramFactStore`/call `load_program_facts()` with a `programs_root`/
     `db_root` argument that resolves to the stray path specifically).
   - If its row count is 0, it is very likely a dead artifact from a test run
     or an aborted manual command — safe to delete outright.
   - If its row count is non-zero, inspect its content
     (`sqlite3 <stray-path> "SELECT fact_type, COUNT(*) FROM program_fact_revisions GROUP BY fact_type"`)
     before deleting — a non-empty stray database may represent real work
     that never reached the canonical store (e.g., a script that
     accidentally wrote to the home-directory fallback). If so, consider
     whether that data needs to be re-migrated into the canonical database
     (via `vertex admin fact-store migrate-legacy-state`, if the content
     maps back to a legacy source) before deleting it — do not silently
     discard non-empty data.
4. **Archive, don't delete outright, unless you're certain.** Move the stray
   file to a clearly-labeled location (e.g.
   `<stray-path>.stray-archived-<date>`) rather than `rm`-ing it immediately,
   in case the investigation above was wrong.
5. **Re-run `vertex doctor --storage`** to confirm the "Fact Store Location"
   check now reports `ok`.
6. **If the root cause was a script/test omitting `programs_root`/`db_root`**,
   fix that call site to thread an explicit root — the loud `CRITICAL` log
   from `_resolve_reality_db_root`'s fallback (Track K's root-cause fix) is
   what should have surfaced this before it produced orphaned data; if it
   didn't fire, check whether the call path bypassed `reality_store.py`
   entirely (e.g. a raw `sqlite3.connect()` against a hand-constructed path).

**Known follow-up (not blocking, tracked in `specs/fix-data-flow.md` §10 item
15):** `xpf`'s own `~/.vertex/xpf/vertex.sqlite3` (a stray, non-canonical
database discovered during this effort's PS-11/PS-14 verification work) has
not yet been cleaned up as of this runbook's last update — apply the
procedure above to it as a concrete first real application of this scenario.

---

## 10. Escalation / RACI

| Scenario | First responder | Escalates to |
|---|---|---|
| Azure CS / LLM outage (§1, §2) | on-call | AI platform |
| Source outage (§3) | on-call | source-system owner |
| κ drop / corpus rollback (§4) | annotator + 2nd labeler | eng+AI |
| Revoke / rollback (§5, §6) | on-call | eng (if supersession breaks) |
| Delivered-issue correction (§7) | program DRI | — |
| ADO schema drift (§8) | on-call | eng (contract update) |
| Multi-database split-brain (§9) | on-call | eng (root-cause the omitted programs_root/db_root) |

---

**Review cadence:** re-reviewed every quarter and on every material activation
change (new family flip, new external gate, rollback drill outcome). Owned by
the Vertex platform on-call rotation.

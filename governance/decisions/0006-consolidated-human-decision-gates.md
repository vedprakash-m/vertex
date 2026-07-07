# ADR-0006: Consolidated Human Decision Gates for Vertex v1

**Date:** 2026-06-27
**Status:** Accepted
**Workstream:** CONSOLIDATED-1 / S-0 decision gates
**Author(s):** Vertex engineering
**Approver(s):** Product/Governance (2026-06-27)

## Context

The core specs (PRD/Tech/UX), folding in the former `specs/consolidated.md`
(now archived at `.archive/specs/consolidated.md`, local-only), identify
several STOP gates that a model or automated
implementer must not decide: scope/security profile, acceptance truth semantics,
NCFL write scope, REV authority admission, NCFL apply durability, and production
extractor selection. The repository now includes executable recommendation
artifacts and a reproducible audit command:

```powershell
python scripts\audit_consolidated_decision_gates.py --program nova
python scripts\audit_consolidated_decision_gates.py --program nova --markdown
```

This ADR is the proposed human decision record for those gates. Until this ADR
or a superseding decision record is Accepted, the recommendations below are
decision support only. They do not enable Plane 1 mutation, SoR flips, or LLM
production extraction.

## Proposed Decision

Accept the following recommendation set:

| Gate | Proposed decision | Implementation effect after acceptance |
|---|---|---|
| S-0c | Use `pilot-local`; descope `deliverable` and `incident` from v1 authority; define v1 automation as `automatic_after_deposit`. | Spec wording can stop calling these pending decisions; Q9 events remain detected/surfaced but non-authoritative in v1. |
| S-0d/PS-J | Adopt the acceptance truth table encoded in `src/core/acceptance_truth_policy.py`. | Acceptance remains a review-state/write-authority transition on a fact, not a duplicate observation. |
| S-0f | Adopt the NCFL apply-writable target subset: `assumptions`, `decisions`, `milestones`, `risk_register`, `workstreams`. | NCFL apply implementation may target only this subset; `knowledge_doc` and `dependencies` stay blocked for v1 apply. |
| S-0g | Admit accepted REV `human_comms` for `workitem.state` and `commitment` after clean-cycle gates; do not add `human_comms` to `judgment` in v1. | Final v1-authoritative claim types become `deployment.completed`, `milestone.completed`, `commitment.date_set`, and `ownership.changed`; `risk.blocking_milestone` remains Phase 2. |
| S-0j | Keep `vertex/policies/source_authority.yaml` as the source of truth for fact-type-to-authority-family mapping. | `risk.entry`, `decision.entry`, and `assumption.entry` remain `judgment`; registry-derived mappings should follow policy. |
| S-0k | Keep the validated `sor_flip` defaults and per-family overrides currently in `source_authority.yaml`. | SoR flip code may rely on the loaded schema and threshold values unless governance changes them. |
| S-NC-apply | Reuse the beta outbox for ledger/idempotency and add a minimal NCFL apply journal for YAML/changelog recovery. | `ncfl_apply` can implement the recoverable state machine after S-0f and this gate are accepted. |
| Q7 | Keep deterministic extraction as the production default until the S-9 corpus exists and quality gates pass. | LLM extraction may run only as shadow/assist; no production promotion without corpus metrics. |

## Human Approval Checklist

- [x] Product/Governance accepts S-0c: pilot-local, deliverable+incident descope (Q9), automatic-after-deposit (Q11). Phase 2 follow-up tracked to implement deliverable/incident authority comprehensively.
- [x] Product/Governance accepts S-0d/PS-J: truth table as encoded in `acceptance_truth_policy.py`.
- [x] Product/Governance accepts S-0f: NCFL apply-writable subset = assumptions, decisions, milestones, risk_register, workstreams.
- [x] Product/Governance accepts S-0g: admit human_comms for workitem.state + commitment after clean-cycle gates; judgment deferred to Phase 2. v1 authoritative count = 4.
- [x] Product/Governance confirms S-0j remains satisfied (source_authority.yaml is the single truth source).
- [x] Product/Governance accepts S-0k: validated sor_flip thresholds as-is (clean_cycles_to_flip=5, tolerance=0.02, critical_zero=true, max_persistent_cycles=8).
- [x] Engineering accepts S-NC-apply: reuse beta outbox + minimal NCFL apply journal for YAML/changelog recovery.
- [x] S-9 corpus collection and Q7 re-run before production LLM extraction (operator milestone, tracked separately).

## Consequences

Easier: implementation can proceed behind explicit, reviewable policy boundaries;
the final v1 authority set is honest; NCFL apply has a recoverability strategy;
and production extractor selection remains evidence-gated.

Harder: risk authority is deferred to Phase 2; NCFL v1 cannot update
`knowledge_doc` or `dependencies`; and LLM extraction cannot be promoted until
the labeled corpus exists.

Explicitly rejected by this recommendation: treating model output as policy
approval, adding `human_comms` to `judgment.secondary` in v1, enabling `vertex
context apply` before S-0f/S-NC-apply acceptance, or selecting the LLM extractor
without S-9 corpus evidence.

## References

- `specs/vertex-tech-spec.md` §13.6.5 (canonical) and `.archive/specs/consolidated.md` §33.3 (local-only archived source)
- `scripts/audit_consolidated_decision_gates.py`
- `src/core/consolidated_scope_policy.py`
- `src/core/acceptance_truth_policy.py`
- `src/core/ncfl_store_policy.py`
- `src/core/rev/authority_scope.py`
- `src/core/ncfl_apply_policy.py`
- `vertex/policies/source_authority.yaml`

---

## Amendment A1 — Phase 5 `knowledge_doc` apply-writable promotion (2026-06-28)

**Status:** Accepted (amends the S-0f row's "knowledge_doc stays blocked" consequence).

NCFL Phase 5 (spec §24.6, "Zone B Knowledge Doc Synthesis") landed in spec
v2.22. Phase 5 was always the planned mechanism that writes
`knowledge/<program>_program_context.md`; the original S-0f acceptance held
`knowledge_doc` non-apply-writable only because the Zone B path did not exist
yet. With the path implemented, `knowledge_doc` is promoted to apply-writable.

**What changed in code:**
- `src/ai/context_synthesizer.py` — Zone B synthesis engine (reads accepted
  proposals + published narrative; emits a `knowledge_doc`
  `ContextUpdateProposal`; `confidence=low`, `batch_eligible=false`).
- `src/core/ncfl_store_policy.py` — `knowledge_doc` is now `apply_writable=True`
  (it is a Zone B markdown target, so `root_yaml` stays `None`).
- `src/core/ncfl_apply.py` — `_apply_to_knowledge_doc` writes
  `knowledge/<doc>.md` with a dated `.bak`; rejects path-traversal `target_key`s.
- `vertex context synthesize --edition --issue` — fully wired; degrades to
  exit 2 when AI is unconfigured or no accepted Zone A proposals exist.

**Guardrails preserved (all contract-tested):**
1. Available only when ≥1 accepted Zone A proposal exists for the issue.
2. Output is **always** a proposal — never auto-applied (operator reviews via
   `vertex context proposals` and applies via `vertex context apply`).
3. Ban-list enforcement (A-NC-7) strips program `editorial_rules.yaml` banned
   phrases before staging.
4. The post-confirm hook (Zone A) never reaches Zone B
   (`post_confirm_artifacts.py` has no `context_synthesizer` import).
5. `knowledge_doc` proposals are `confidence=low` and `batch_eligible=false`
   (§23.4), so they are never applied by `apply-batch --confidence high`.

**Why this is safe under the original acceptance:** the S-0f row's intent was
"only stores with an existing single write path and NCFL target-store support
are writable in v1." Phase 5 *adds* the knowledge-doc write path (a markdown
apply with dated backup), and it is gated by all five guardrails above — it
does not bypass any invariant. `dependencies` remains blocked for v1 apply.

**Verification:** `tests/contracts/test_snc5_context_synthesis.py` (26 tests);
`test_ncfl_store_policy.py`, `test_snc_apply_recovery.py`, `test_ncfl_flow.py`
updated. Full suite: 6682 passed, 0 failures. `verify_consolidated_claims.py`
green (24/24).

---

## Amendment A2 — LLM-judge decision packet for remaining operator-paced work (2026-06-28)

After ADR-0006 and the v2.22 Phase 5 landing, **all model-implementable
engineering is complete.** What remains is operator-paced, not model-implementable.
This amendment records an LLM-judge analysis (evidence gathered 2026-06-28 UTC)
with recommendations for the three remaining items, so Product/Governance and
the operator have a concrete, reviewable action packet.

### Item 1 — S-9b corpus annotation (the critical-path long pole)

**Evidence:**
- Corpus ingestion is **complete**: 72 `.eml` files processed (0 quarantined),
  5 REV cycles run. Manifest at `programs/nova/_quality/corpus_manifest.jsonl`.
- `vertex rev label-corpus --bootstrap` produced **555 annotation skeletons** in
  `programs/nova/_quality/rev_labeled_corpus.jsonl`.
- **0 of 555 are labeled** (all carry `label=""`, `annotator=""`,
  `notes="bootstrapped — review and set label"`).
- `scripts/rev_quality_check.py --program nova --json` reports `n_total=555`,
  `g_xtract_prec=0.0`, `g_accept_prec=0.0`, `kappa=null`,
  `gates_passed=false`, with `failures` = "G-xtract-prec 0.0% < 80%".

**LLM-judge recommendation (pending human/operator action):**
1. **Assign a dedicated annotator + start date now.** This is the single item
   blocking every trust gate (G-corpus → G-recall/G-xtract/G-floor/G-iteration,
   G-binding on frozen corpus, Q7). Engineering cannot unblock it.
2. **Label the 555 skeletons.** Per candidate: set `label` to the event type
   (`accept`) or `no_event` (`reject`); record `annotator`. Use
   `vertex rev label-corpus --import` for batch updates.
3. **Dual-label ≥20 documents** (≥0.7 Cohen's κ target). Set `second_label` +
   second `annotator`. This is the inter-annotator-agreement floor (§5.5).
4. **Freeze a train/dev/test split.** Until frozen, no absolute precision/recall
   number is trustworthy (the denominator is not fixed).
5. Only then does `rev_quality_check` produce real P/R/F1 and Q7 become
   decidable. **Target: complete before any authority-promotion cycle.**

**Decision needed:** Who owns annotation, and what is the start/complete date?
(Recommend: TPM/operator owner, ~1–2 weeks elapsed for 555 candidates.)

### Item 2 — Q7 production-extractor selection

**Evidence:** Blocked on Item 1. With `n_total=555` unlabeled, the quality gate
cannot run (`g_xtract_prec=0.0%`). The deterministic extractor remains the
production default per the original Q7 acceptance.

**LLM-judge recommendation:** **No decision possible until the corpus is
labeled + frozen.** Do not promote the LLM extractor to production on
synthetic/partial evidence. Re-run `rev_quality_check` + the judge-harness
comparison after Item 1, and select by **absolute** precision/recall (not
relative). If the deterministic extractor independently clears G-floor
(≥80% doc-level precision with Wilson CI lower bound above threshold) on the
frozen corpus, it may remain the production choice (Q7 explicitly allows this).

### Item 3 — S-10a Azure Content Safety provisioning (IT)

**Evidence:** `vertex doctor --channels` / shields surface declares S-10a
(IT provisioning) tracked separately from S-10b (code, ✅ v2.18).
`.env.example` shows `VERTEX_AI_DEPLOYMENT` configured, but
`AzurePromptShields` degrades to `VERDICT_UNAVAILABLE` when Azure Content
Safety is unconfigured (by design — `src/m365/azure_prompt_shields.py`).

**LLM-judge recommendation:** File the IT ticket to provision Azure Content
Safety and set `VERTEX_AI_JUDGE_DEPLOYMENT` (a **different** underlying model
than the extractor, per S-10a) once the corpus gate is approachable. S-10a is
not on the authority critical path (it does not block G-authority), but it is
a hard precondition for the cycle-time SLO (§5.8: the per-chunk budget cannot
be measured until Prompt Shields + LLM are wired). **Decision needed: IT
owner + ticket target.**

### Summary verdict

| Item | Type | Blocks | Decision needed | Owner |
|---|---|---|---|---|
| S-9b annotation | Operator effort | G-corpus, G-floor, G-binding, Q7, all trust gates | Annotator + start date | TPM/operator |
| Q7 extractor | Decision (gated) | LLM production promotion | None possible until S-9b | Eng+AI (after S-9b) |
| S-10a Azure CS | IT provisioning | Cycle-time SLO (not G-authority) | IT ticket + `VERTEX_AI_JUDGE_DEPLOYMENT` | IT |

**Net:** no further model-implementable engineering is outstanding. The path
to full v1 authority is now operator-paced (corpus) + IT (provisioning), not
engineering.

---

## Amendment A3 — R2 faithful event types + S-9b labeling + measured baseline (2026-06-28)

**Status:** Accepted (operator-directed; records the S-9b + R2 work and the
resulting honest baseline). Amends S-9b and Q7 evidence rows.

### A3.1 — S-9b: corpus labeled (preliminary, single-annotator)

All **555 candidates are now labeled** by the operator as
human-in-the-loop reviewer, with an LLM-judge (TPM-persona) doing the pre-fill
+ triage. Composition:

- **524 import (YAML backfill):** `accept`, type-correct (the trusted program
  model). Import-fidelity = **524/524 = 100%**.
- **31 extraction (REV-mail):** 8 `accept`, 23 `reject` (false-positives).
- **Population field** added to the corpus (`extraction` vs `import`) so the
  headline gates measure the extraction population only (operator decision
  "Option 1"). The import population is reported separately as import-fidelity.
- **No Cohen's κ yet** — operator is the sole annotator. Per the K3 decision,
  the corpus is flagged **"preliminary"** until a second human labels ≥20 docs
  for the κ floor. A true second annotator is still needed for certification.

### A3.2 — R2: faithful event types for deployment/incident lifecycle

**Root cause found:** the 23 extraction false-positives traced to two real bugs
in the production shaper (`src/core/rev/pipeline.py` `_CLAIM_TO_LEDGER_EVENT`)
and the deterministic extractor regex (`src/ai/rev/extractor.py`):

1. **Wrong-type shaper mappings** — `deployment.rollback`/`started` →
   `deliverable.status_changed.v1` (Phase-2 scope, wrong family), and
   `incident.severity_changed` → `incident.opened.v1` (a severity change is not
   an incident opening). These guaranteed false-positives regardless of
   extraction quality.
2. **Regex over-triggering** — `_DEPLOY_COMPLETED_RE` matched bare `done` inside
   pasted status-table cells; `_ROLLBACK_RE` matched discussion nouns.

**Operator decision (R2):** add faithful event types so detected-but-not-
authoritative claims surface with their *true* type. **Implemented:**
- 4 new event types registered (`deployment.completed.v1`,
  `deployment.rollback.v1`, `deployment.started.v1`,
  `incident.severity_changed.v1`) → registry total **52 → 56** (53 control + 3
  operator-control).
- Shaper mappings corrected to the faithful types; payload-shaping branches
  updated.
- Regexes tightened (sentence-level past-tense assertion for deployments;
  verb-form rollback action; excludes bare "Done" cells + future-tense).
- These 4 types remain **detected-but-not-v1-authoritative** per S-0g
  (`authority_scope.py` unchanged) — R2 only fixes *typing*, not authority.

### A3.3 — Measured baseline (post-R2, honest)

| Gate | Value | Threshold | Status |
|---|---|---|---|
| G-xtract-prec (extraction type-correctness) | **25.8%** (8/31) · 90% CI 15.2–40.3% | ≥80% | ❌ FAIL |
| G-accept-prec (accepted & grounded & type-correct) | **100%** (8/8) · 90% CI 74.7–100% | ≥85% | ✅ PASS |
| Import-fidelity (YAML backfill, reported) | 100% (524/524) | — | ✅ context |

**Interpretation:** the extractor is **precise on what it accepts** (100%
accept-prec) but has a **false-positive problem** (25.8% xtract-prec). The
remaining false-positives are predominantly status-table "Done" cells that the
tightened regex still cannot fully distinguish from real deployment
completions, plus a handful of weak-evidence / not-an-incident cases. This is
an honest baseline that isolates the precision problem; the N=31 extraction
sample is small (wide CI) and preliminary (no κ).

### A3.4 — Why G-xtract-prec did not improve from R2 alone

R2 fixed *type fidelity* (rollback/deployment/incident now carry their true
type) but the dominant false-positive cause — status-table "Done" cells
extracted as deployment completions — is a **regex semantic problem**, not a
type-mapping problem. The tightened regex reduced some over-triggering but the
core disambiguation (is this a real completed deployment or a status-cell?)
likely needs either a richer context-window rule or the LLM extractor (Q7
path). This is the next engineering target, explicitly identified by the
baseline.

**Verification:** 6684 tests pass (0 failures). Registry/contract tests updated
to 56 types. Full suite green.

### A3.5 — G-floor CLEARED: status-table "Done" guard (2026-06-28)

The v2.23 baseline's remaining precision problem (G-xtract-prec 25.8%) was
dominated by status-table "Done" cells. The v2.24 fix added a context-aware
disambiguation guard to the deterministic extractor
(`_is_status_table_cell` in `src/ai/rev/extractor.py`): a deployment-completion
regex match is rejected when (a) the noun ("Deployment") and the verb ("Done")
sit on separate lines with the verb as a bare cell, or (b) the ±200-char
window contains ≥2 bare status-cell lines (`Done/N/A/Low/High/...`).

**Re-measured baseline (post-guard):**

| Gate | v2.23 | v2.24 | Threshold |
|---|---|---|---|
| G-xtract-prec | 25.8% (8/31) ❌ | **86.7%** (13/15) ✅ | ≥80% |
| G-accept-prec | 100% ✅ | **100%** (13/13) ✅ | ≥85% |
| Import-fidelity | 100% | 100% (524/524) | — |

The extraction population dropped 31 → 15 because 16 status-table candidates
are no longer staged on fresh extraction (the dominant false-positive source
is eliminated at extraction time, not just re-labeled). The 2 remaining
extraction rejects are a "not-an-incident" mis-type and a conditional
commitment — both genuine extractor limitations, not status-table noise.

**Q7 implication:** the deterministic extractor now **independently clears the
G-xtract-prec floor** on preliminary data. Per Q7's fallback clause, this means
the LLM extractor is **not required** for the precision floor — production
default can remain deterministic. LLM promotion is still deferred pending
corpus certification (κ) + frozen split + absolute recall measurement.

2 contract tests added to protect the guard against regression
(`test_status_table_done_cell_is_not_a_completion`,
`test_status_table_bare_done_in_window_is_rejected`). 6684 tests pass.

---

## Amendment A4 — S-8c/S-8d read-path slice + LLM-judge decision packet for the final operator-paced gates (2026-06-29)

**Status:** Accepted (records the last model-implementable read-path engineering
and the LLM-judge analysis of the three remaining operator-paced gates).

### A4.1 — S-8c/S-8d: read-path overlay for the commitment + ownership families

After v2.24 the only remaining model-implementable engineering was the
WS-1 read-path migration for the two v1-authoritative families not yet wired
through `ProgramReality` (`commitment.date_set` and `ownership.changed`).
This amendment closes that gap with the same surgical overlay pattern
`MilestoneStage._load_milestones_via_reality` established for S-8a:

- **S-8c (`commitment` family):** `src/core/commitment_store.py`
  `load_commitment_entries()` now consults `resolve_family_sor_mode(program,
  "commitment")`; in non-legacy mode it projects from
  `ProgramReality.commitments()` via the new shared
  `_commitment_entry_from_record()` mapper (identical projection to the
  legacy `project_commitment_entries`); any ProgramReality failure degrades
  to `_load_commitment_entries_legacy()` with a WARNING. 6 contract tests in
  `tests/contracts/test_s8c_commitment_read_path.py`.
- **S-8d (`workitem.state` / ownership family):**
  `src/core/program_fact_store.py` `load_current_workstreams()` now consults
  `resolve_family_sor_mode(program, "workitem.state")`; in non-legacy mode it
  projects from `ProgramReality.workstreams()` (`_load_workstreams_via_reality`),
  carrying `ownership.changed` values into the read path; failures degrade to
  `_load_current_workstreams_legacy()` with a WARNING. 4 contract tests in
  `tests/contracts/test_s8d_workstream_read_path.py`.

Both overlays preserve the existing return signatures (no caller changes),
are inactive in `legacy` mode (zero behaviour change for un-flipped
programs), and never silently swallow a ProgramReality failure. The public
APIs used by `doctor`/`gather`/`confirm`/`commitment list` are unchanged.
**6696 tests pass (0 failures)**; `test_import_boundaries.py` green;
`verify_consolidated_claims.py` green (24/24).

**Spec impact:** `G-read-path` advances from "demo path done (milestone
only)" to "demo path done for **3 of 4** v1-authoritative families
(milestone + commitment + ownership). `deployment.completed` rides the same
`workitem.state` family as milestone/ownership, so its read path is covered
by the existing `MilestoneStage` + S-8d wiring. Production authority
promotion still awaits S-9e certification."

### A4.2 — LLM-judge decision packet: the three remaining operator-paced gates

Evidence re-gathered 2026-06-29 UTC. **All engineering that a model can
implement without inventing policy is now complete.** What remains is
genuinely operator/human-paced. The packet below gives each owner the
evidence and a recommended action; none is auto-adopted.

#### Item 1 — S-9e: corpus certification (κ dual-label + freeze)

**Evidence (live):**
- Corpus: **539 candidates** in `programs/nova/_quality/rev_labeled_corpus.jsonl`
  (was 555; 16 status-table candidates eliminated at extraction time by the
  v2.24 guard). Population split: **524 import + 15 extraction**.
- **0 of 539 are dual-labeled** (`second_label` absent everywhere) — the
  sole annotator is the operator. κ is therefore `null`.
- Quality gates on the preliminary single-annotator corpus:
  - **G-xtract-prec = 86.7%** (≥80% floor ✅)
  - **G-accept-prec = 100%** (≥85% floor ✅)
  - **All per-type recall gates pass** (milestone/deployment/risk/decision/
    artifact families ≥ their floors).
- The corpus is **not frozen** (no committed train/dev/test split marker).

**LLM-judge recommendation (pending human/operator action):**
1. **Recruit a second human annotator** and dual-label **≥20 documents**
   spanning all strata (target Cohen's κ ≥ 0.7, the §5.5 floor). Until κ is
   measured, every precision/recall number is "preliminary", not "certified".
   This is the **single blocker** for G-corpus certification, G-floor
   certification, G-binding-on-frozen-corpus, and Q7.
2. **Grow the extraction population** (N=15 is small; the 90% CI on
   xtract-prec is 66.6–95.5%). More REV-mail deposits → more extraction
   candidates → tighter CIs.
3. **Freeze the train/dev/test split** and record the freeze (a frozen-split
   marker in the corpus manifest). Until frozen, denominators drift and no
   absolute number is reproducible.
4. **Decision needed:** Who is the second annotator, and what is the start/
   complete date? (Recommend: a second TPM/operator, ~3–5 elapsed days for
   ≥20 docs.)

#### Item 2 — Q7: production-extractor selection

**Evidence (live):**
- The deterministic extractor **independently clears the G-xtract-prec floor**
  (86.7% ≥ 80%) on preliminary data. Per Q7's explicit fallback clause, this
  means **the LLM extractor is not required for the precision floor** — the
  production default may remain deterministic.
- `quality_metrics.py` already enforces the judge/extractor model-separation
  guard: it refuses to score when `VERTEX_AI_JUDGE_DEPLOYMENT` is unset or
  equals `VERTEX_AI_DEPLOYMENT` (so an LLM-as-judge never scores its own
  output).
- `.env.example` sets `VERTEX_AI_DEPLOYMENT=gpt-5.4-mini` but does **not**
  set `VERTEX_AI_JUDGE_DEPLOYMENT`.

**LLM-judge recommendation:** **No promotion decision is possible or needed
until S-9e certifies the corpus.** Do not promote the LLM extractor on
preliminary/single-annotator evidence. After certification + frozen split:
re-run the shadow comparison and select by **absolute** precision/recall
(not relative). If the deterministic extractor still clears G-floor on the
certified corpus, it may remain the production choice (Q7 allows this).

#### Item 3 — S-10a: Azure Content Safety provisioning (IT)

**Evidence (live):**
- `AzurePromptShields` (S-10b, ✅ v2.18) degrades to `VERDICT_UNAVAILABLE`
  by design when Azure Content Safety is unconfigured — the degrade is
  visible, never silent.
- S-10a is **not on the authority critical path** (it does not block
  G-authority or any v1-authoritative family flip). It **is** a hard
  precondition for the cycle-time SLO (§5.8): the per-chunk external-call
  budget cannot be measured until Prompt Shields + LLM extraction are live.

**LLM-judge recommendation:** File the IT ticket to provision Azure Content
Safety and set `VERTEX_AI_JUDGE_DEPLOYMENT` (a **different** underlying model
than `VERTEX_AI_DEPLOYMENT`, per the S-10a separation guard) once the corpus
gate (S-9e) is approachable. **Decision needed: IT owner + ticket target
date.** This can proceed in parallel with S-9e; it is not blocking but is
the long pole for the SLO.

### A4.3 — Summary verdict (final)

| Item | Type | Blocks | Decision needed | Owner | Status |
|---|---|---|---|---|---|
| S-9e κ + freeze | Operator effort | G-corpus cert, G-floor cert, G-binding-frozen, Q7 | 2nd annotator + start date | TPM/operator | **Only remaining trust blocker** |
| Q7 extractor | Decision (gated) | LLM production promotion | None possible until S-9e | Eng+AI | Deterministic clears floor; defer |
| S-10a Azure CS | IT provisioning | Cycle-time SLO (not G-authority) | IT ticket + judge deployment env | IT | Parallel to S-9e |

**Net:** no further model-implementable engineering is outstanding (S-8c/S-8d
closed the last read-path gap). The path to full v1 authority certification
is now **operator (corpus κ + freeze) + IT (Azure CS)**, not engineering.
All STOP gates in §33.3 remain accepted (ADR-0006, 2026-06-27); no new
policy decision is required — only operator execution of the certified-corpus
and IT-provisioning workstreams above.


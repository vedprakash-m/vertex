# AI provider-call inventory

*(specs/backlog.md WO-4, part of BL-C2's AI-boundary coverage audit.)*

Every provider-bound call site under `src/`, reconciled against the 26
`ai_features` in `vertex/policies/ai_policy.yaml` and the 30 prompt versions
registered in `src/ai/prompts/registry.yaml`. Produced by tracing every
`.structured(`/`.chat(` call (the only two methods `src/ai/client.py`
exposes — there is no `.complete()` anywhere in `src/`) and every
`route_through_tiers(` call site, then reading enough of each surrounding
module/caller to determine gateway/audit wiring and real-world consumption.

Analysis only — no call site was modified to produce this document.

**Update 2026-07-22 (BL-C2 Phase A):** two of the three feature-name
mismatches this inventory found were fixed (`report_lookback.py` given its
own registered feature and prompt, now 27 total; `kb.py`'s telemetry no
longer falls back to a caller-string) — see each row's `[FIXED]` note and
the reverse-check section below. A new CI ratchet,
`tests/contracts/test_ai_call_site_ratchet.py`, now fails the build if a
new file starts making direct provider calls without being added to this
document and to the ratchet's own known-file list in the same change —
this is BL-C2 step 5's "CI ratchet" requirement. `setup.py:205`'s
duplication and the full per-site `AISchemaGateway`/`ai_release_audit`
wiring for `production`-classified sites remain open; see
`specs/backlog.md`'s BL-C2 row for what's done vs. remaining.

**Classification legend:** `production` (output can reach a published
artifact or written fact), `advisory` (staged proposal requiring explicit
human accept/reject), `evaluation` (comparison/QA harness, not a production
consumer), `retired` (dead code — none found with enough confidence to
apply this label; see rev_judge's note).

## Inventory

| Feature | Call site (file:line) | Registry-managed prompt? | Through AISchemaGateway? | Records ai_release_audit decision? | Classification | Notes |
|---|---|---|---|---|---|---|
| action_extractor | `src/ai/action_extractor.py:104` (`.structured`) / `:124` (`route_through_tiers`) | Yes — `action_extractor.v1` | No | No | advisory | Called from `src/commands/meeting_close.py:312`; output feeds a queued/approved/dismissed/pending human-review flow before any ADO write. |
| anticipation_engine | `src/ai/anticipation_engine.py:145` / `:142` | Yes — `anticipation_question.v1` | No | No | production | `program_id is None` branch of `_generate_with_ai`, used by `src/commands/review_full.py` (can legitimately run without a resolved program). Output is baked directly into the rendered review artifact but **skips the QG-29 audit trail entirely** by design. |
| anticipation_engine | `src/ai/anticipation_engine.py:218` / `:215` | Yes | Yes | Yes | production | Same function, `program_id` branch; used by `src/commands/prep.py`. Two call sites, same feature, materially different audit coverage — see `anticipation_engine.py:114-132`. |
| backfill_extractor | `src/ai/backfill_extractor.py:111` / `:108` | Yes — `backfill_extractor.v1` | No | No | advisory | Used by `src/commands/backfill.py`; gated behind interactive `typer.confirm`, writes a candidate export + summary, not a released fact. |
| blurb_generator | `src/ai/blurb_generator.py:179` / `:176` | Yes — `workstream_blurb.v1` | Yes | Yes | production | Used directly in `assemble_stage.py`/`report.py`/`report_ai.py` (auto-included once QG-29 `RELEASED`). Also reused as a proposal candidate in `src/commands/propose.py` — same call site serves both a direct-publish and a proposal path; dual-use ambiguity, not asserted confidently as one or the other. |
| claim_extractor | `src/ai/claim_extractor.py:219` / `:267` | Yes — `claim_extractor.v1` | Yes | Yes | production | Used by `src/commands/confirm_stages/claim_resolution.py`, part of the standard `vertex confirm` pipeline producing published newsletter claims. |
| context_synthesizer | `src/ai/context_synthesizer.py:150` / `:147` | Yes — `context_synthesizer.v1` | No | No | advisory | Module docstring: output is always a `ContextUpdateProposal`, "never auto-applied"; operator applies via `vertex context apply`. The only gateway/audit-eligible-looking feature that deliberately opts out of both — worth a human check on whether that's intentional. |
| decision_brief_advisor | `src/ai/decision_brief_advisor.py:205` / `:202` | Yes — `decision_brief_advisor.v1` | Yes | Yes | production | `advise_on_decision_brief`, called from `src/commands/decision_brief.py:180` — the real command path. |
| decision_brief_advisor | `src/ai/decision_brief_advisor.py:314` / `:311` | Yes — same version | Yes (via `_parse_advice_with_gateway`) | **No** | evaluation | `advise_on_decision_brief_via_context_gateway`. Docstring: "Not wired into any production command — only the blind A/B comparison harness (`src/commands/decision_brief_pilot.py`) calls this, pending real evidence before any production swap." |
| dependency_blast_radius_generator | `src/ai/dependency_blast_radius_generator.py:95` / `:92` | Yes — `dependency_blast_radius_generator.v1` | Yes | Yes | advisory | Only CLI caller is `src/commands/ai_proposals.py`'s `generate`→`stage_proposal`→accept/reject flow. |
| exec_summary_drafter | `src/ai/exec_summary_drafter.py:162` / `:159` | Yes — `exec_summary_drafter.v1` | Yes | Yes | production | Used directly in the report pipeline, auto-included once released. |
| governance_decision_brief_generator | `src/ai/governance_decision_brief_generator.py:100` / `:97` | Yes — `governance_decision_brief_generator.v1` | Yes | Yes | advisory | Same `ai_proposals.py` staged-proposal flow as dependency/risk/top-three/meeting_action. |
| intent_router | `src/ai/intent_router.py` (`_run_ai_route`) | Yes — `intent_router.v1` | **Yes** | **Yes** | production | Used by `src/commands/ask.py`; output (a `RoutedInvocation`) directly determines which CLI command executes. **[FIXED 2026-07-22]** Raw response now bounds-checked via `validate_bounded_payload` before `_parse_routed_invocation`'s existing route-catalog/args check (which serves as the semantic validator — no separate class, since that validation already exists and already raises the exact `IntentRouterError` callers depend on); every run gets a full `planned→...→semantically_validated` lifecycle plus a durable `released`/`rejected`/`discarded` terminal via `ai_release_audit`. `route()` gained a `programs_root` parameter for this. Tests: `tests/unit/test_ai_intent_router.py` (2 new: released-trail + rejected-trail assertions). |
| learning_distiller | `src/ai/learning_distiller.py:156` / `:153` | Yes — `learning_distiller.v1` | No | No | advisory | Used by `src/commands/confirm_stages/learning_distiller.py`; output is a `LearningDistillation` of editorial-rule proposals, written to a proposals artifact but not auto-applied to `EditorialRules`/ban-list. |
| m365_topic_router | `src/ai/m365_topic_router.py` (`_run_ai_route`) | Yes — `m365_topic_router.v1` | **Yes** | **Yes** | production | Used by `src/commands/gather.py`'s M365 discovery stage; feeds `promotion_candidates`/`promotion_blocked` artifact building. **[FIXED 2026-07-22]** Raw response now bounds-checked via `validate_bounded_payload` before `_parse_routing_decision`'s existing workstream-membership/confidence/topics/reasoning validation (which serves as the semantic validator — no separate class, matching the `intent_router` precedent). Full QG-29 lifecycle + `released`/`rejected`/`discarded` terminal recorded on every AI attempt; `route_artifact`'s existing graceful-degradation contract (never raises, falls back to the deterministic router on any AI failure) is unchanged. `M365TopicRouter` gained `program_id`/`programs_root` fields, set by `from_program`. Tests: `tests/unit/test_ai_m365_topic_router.py` (2 new: released-trail + rejected-trail assertions; 9 existing AI-path tests given `programs_root=tmp_path` isolation — a repo-pollution bug of the same class WO-3/BL-C2's `intent_router` fix found, caught before landing this time). |
| meeting_action_extractor | `src/ai/meeting_action_extractor.py:128` / `:125` | Yes — `meeting_action_extractor.v1` | Yes | Yes | advisory | Sole CLI wiring is `ai_proposals.py`'s `_generate_meeting_action` → `stage_proposal` → accept/reject. |
| onboard_assistant | `src/ai/onboard_assistant.py:181` / `:178` (structure) | Yes — `onboard_structure_assistant.v1` | No | No | advisory | Interactive `vertex setup`/onboarding suggestions; operator chooses whether to accept. |
| onboard_assistant | `src/ai/onboard_assistant.py:218` / `:215` (style) | Yes — `onboard_style_assistant.v1` | No | No | advisory | Same reasoning as above. |
| summary_generator | `src/ai/summary_generator.py:204` / `:201` | Yes — `summary_generator.v1` | Yes | Yes | production | Used by `src/commands/summarize.py`, feeds rolling workstream summaries directly into report content once released. |
| synthesizer | `src/ai/synthesizer.py:149` / `:146` | Yes — `synthesizer.v1` | Yes | Yes | production | Used by `src/commands/synthesize.py`. |
| risk_proposal_generator | `src/ai/risk_proposal_generator.py:178` / `:175` | Yes — `risk_proposal_generator.v1` | Yes | Yes | advisory | `ai_policy.yaml`'s own comment: "`apply_risk_proposal` only ever fires on human approval." Staged via `ai_proposals.py`. |
| top_three_candidate_generator | `src/ai/top_three_candidate_generator.py:118` / `:115` | Yes — `top_three_candidate_generator.v1` | Yes | Yes | advisory | Same `ai_proposals.py` staged-proposal flow. |
| setup_assistant | `src/ai/setup_assistant.py:219` / `:216` | Yes — `setup_ws_suggest.v1` | No | No | advisory | Workstream suggestions during setup; operator accepts/edits. |
| setup_assistant | `src/commands/setup.py:205` (direct `.structured`, no `route_through_tiers`) | **No** — hardcoded system-prompt string; only the `prompt_version="setup_ws_suggest.v1"` label is reused for telemetry | No | No | advisory | Near-duplicate of `setup_assistant.py`'s `_ai_suggest_workstreams`, reimplemented inline in the command layer with its own hardcoded prompt text rather than calling `load_prompt`. Latent drift risk between the two implementations — worth a human look. |
| program_synthesizer | `src/ai/program_synthesizer.py:141` / `:138` | Yes — `program_synthesis.v1` | Yes | Yes | production | `generate_program_synthesis`; on `RELEASED` calls `persist_program_synthesis(...)` directly (no separate accept/reject step) — feeds the cockpit. |
| program_synthesizer | `src/ai/program_synthesizer.py:268` / `:265` | Yes | Yes | Yes | evaluation | `generate_program_synthesis_via_context_gateway`. Docstring: "NOT a production swap… exists so a blind comparison harness can gather real evidence." Only caller is `src/commands/program_synthesizer_pilot.py`. |
| prose_event_extractor | `src/ai/discovery/prose_event_extractor.py:209` / `:206` | Yes — `prose_event_extractor.v1`–`v4` (wave-selected) | No | No | advisory | Feeds `CandidateEvent`s into `src/core/ledger/candidate_store.py` via `src/commands/discover.py`; candidates require triage/confirmation, not auto-published. |
| rev_extractor | `src/ai/rev/extractor.py:566` (`.structured`) / `:507` (`route_through_tiers`) | Yes — `rev_extractor.v1` | No | No | advisory | Real logic in `LLMRevExtractor`, used by `src/commands/rev.py --extractor llm`; `rev_run`'s own docstring: "stage candidates for triage" — not auto-published. |
| rev_judge | `src/ai/rev/judge.py:273` (`.structured`) / `:269` (`route_through_tiers`) | Yes — `rev_judge.v1` (WO-3, 2026-07-22 — was an inline constant when this inventory's underlying research ran) | No | No | evaluation | `judge_extractions()` compares two extractors' claims against ground truth (LLM-as-judge). **No production CLI/script caller found** anywhere in `src/commands/` or `scripts/`; only invoked from `tests/unit/test_rev_cache_store.py`. Close to "retired" but not confident enough to apply that label — it's clearly *designed* as an evaluation harness and is asserted-live by a deliberate AST anchor (see below). A human should confirm whether it's meant to be wired up somewhere or is dead. |
| activation_judge | `src/ai/activation_judge.py:305` / `:301` | Yes — `activation_judge.v1` | No | No | evaluation | Used by `scripts/run_activation_judge.py`, feeding `scripts/verify_activation.py`'s judge layer (WO-7 context) — a build/QA judge, not a program-published artifact. |
| default | `src/commands/kb.py:358` (`.chat`, no `route_through_tiers`) — feature resolved at `kb.py:315` | **No** — hardcoded system string, `prompt_version="kb_update_plan.v1"` (unregistered) | No | No | advisory | `vertex kb update`'s AI-assisted correction planner. Preview the operator must confirm with `--apply`. **[FIXED 2026-07-22]** Telemetry's `feature` metadata previously fell back to the caller string; `_build_kb_update_trace_context` now sets `metadata["feature"] = "default"` explicitly (`tests/unit/test_commands_kb.py`). Prompt/max_tokens are still an inline literal, not registry-managed — that part of the gap remains open. |
| lookback_retrospective | `src/commands/report_lookback.py:583` (`.structured`, via `route_through_tiers`) | **Yes** — `lookback_retrospective.v1` | No | No | production | `_build_lookback_ai_retrospective_rows`, called live from `assemble_stage.py:1535` and appended directly into `retrospective_intelligence.rows`, flowing straight into the rendered lookback report with no further gate. **[FIXED 2026-07-22]** Given its own registered feature (`vertex/policies/ai_policy.yaml`) and prompt (`lookback_retrospective.v1`, moved out of an inline string), routed through `route_through_tiers` (`tests/unit/test_report_lookback_ai.py`). Deployment *connectivity* resolution (`assemble_stage.py:1521`) remains intentionally shared with `exec_summary_drafter` — documented as deliberate in `ai_policy.yaml`'s comment, not a leftover mismatch. Still not wired through `AISchemaGateway`/`ai_release_audit` — that part of BL-C2 remains open. |

Two additional non-functional matches, noted for completeness but **not**
counted as real call sites above:

- `src/ai/rev/rev_extractor.py:76` and `src/ai/rev/rev_judge.py:39` each
  contain `route_through_tiers("rev_extractor"/"rev_judge", None, None,
  None)` inside an `if False:` block — an intentional dead-code AST anchor
  so a contract test (`test_router_adoption_ratchet`) can find the string in
  the module's AST. Not live code.
- `src/ai/deployment_fallback.py:131` (`.chat`) and `:159` (`.structured`)
  are the generic `FallbackAIClient`/`FallbackStructuredClient` passthrough
  implementations — infrastructure every feature-specific call site above
  routes through, not a feature call site in its own right.

## Reverse check against the 26 features

**Features with zero call sites found:** none. All 26 features in
`ai_policy.yaml` (`action_extractor, anticipation_engine,
backfill_extractor, blurb_generator, claim_extractor, context_synthesizer,
decision_brief_advisor, dependency_blast_radius_generator,
exec_summary_drafter, governance_decision_brief_generator, intent_router,
learning_distiller, m365_topic_router, meeting_action_extractor,
onboard_assistant, summary_generator, synthesizer, risk_proposal_generator,
top_three_candidate_generator, setup_assistant, program_synthesizer,
prose_event_extractor, rev_extractor, rev_judge, activation_judge,
default`) have at least one confirmed call site above.

**Call sites whose feature name does not appear in the 26 (mismatch
direction):**

- ~~`src/commands/report_lookback.py:583`~~ **[FIXED 2026-07-22]** — given
  its own registered feature (`lookback_retrospective`, now 27 features
  total) and prompt; deployment *connectivity* resolution remains
  intentionally shared with `exec_summary_drafter` (documented, not a
  mismatch).
- ~~`src/commands/kb.py:358`~~ **[FIXED 2026-07-22]** — `metadata["feature"]`
  now explicitly set to `"default"`; telemetry no longer falls back to the
  caller string. The prompt/max_tokens are still an inline literal
  (unregistered) — that narrower gap remains open.
- `src/commands/setup.py:205` — **still open.** Resolves under
  `"onboard_assistant"` (valid) for deployment, which is itself odd since
  the logic and prompt text are functionally `setup_assistant`'s
  (registered as `setup_ws_suggest.v1`), just reimplemented inline rather
  than sharing code. Not attempted in this pass — consolidating two
  near-duplicate implementations risks a subtle UX behavior change that
  needs its own dedicated review, not a drive-by fix.

No call site used a completely invented feature-name string outside the
26 (now 27) — all mismatches found were of the "borrowed/wrong policy
label for a distinct prompt" kind rather than a literal typo'd feature key.

## Prompt-registry side note

Two of the 30 registered prompt versions — `setup_discovery_assistant.v1`
and `setup_explainer.v1` — have no `load_prompt(...)` caller anywhere in
`src/` (only `setup_ws_suggest.v1` of the three `setup_*` entries is
actually loaded, from `src/ai/setup_assistant.py:213`). Worth a human check
on whether those two are pre-provisioned for planned-but-unbuilt call sites
or are stale registrations.

## What this inventory does *not* do

Per WO-4's explicit scope, this is analysis only: no call site was changed,
and nothing above is classified `retired` (the `rev_judge` finding is the
closest candidate, but is left as `evaluation` pending human confirmation).
Closing BL-C2's remaining scope — wiring every `advisory`/`production` call
site through `AISchemaGateway` and `ai_release_audit` where it currently
isn't, and resolving the three feature-name mismatches above — is separate,
not-yet-scoped follow-up work.

# Model Cards — Vertex Platform

**Status**: v1.0 — 2026-06-09 (WS-24)
**Owner**: Platform SRE
**Cadence**: Re-cert every 90 days; on every model bump; on every
prompt-card change that touches a feature's behavior contract.

## 1. What is a model card?

A model card is the **single source of truth** for the deployment
serving a single AI feature. It answers the four SRE questions:

1. **What** model is serving this feature? (`model_id` + base + version)
2. **Who** owns the deployment? (DRI + team)
3. **When** was it last re-certified? (`recert_at`)
4. **When** is the deprecation review? (`deprecation_review_at`)

The card also records the eval-set version, the prompt-card version,
and any known limitations. A card is required for every entry in
`ai_policy.yaml`.

## 2. How to file a model card

```yaml
# vertex/policies/model_cards/<feature_name>.yaml
schema_version: "1"
feature_name: blurb_generator
deployment:
  model_id: gpt-4o
  deployment_id: gpt-4o
  azure_openai_base: https://vertex-aoai-eastus.openai.azure.com
  model_version: 2024-05-13        # upstream checkpoint version
  pinned_at: 2026-06-09T00:00:00Z
  deprecation_review_at: 2026-09-09T00:00:00Z
  recert_at: 2026-06-09T00:00:00Z
owner:
  dri: operator@example.com
  team: platform-sre
eval_set:
  version: 5
  path: tests/eval/blurb_generator_v5.json
  pass_rate: 0.96
prompt_card:
  version: 7
  path: src/ai/prompts/blurb_generator.v7.txt
  last_changed: 2026-05-20T00:00:00Z
known_limitations:
  - "Struggles with multi-paragraph synthesis over 3+ sources"
  - "Occasionally invents ADO field names that don't exist"
```

## 3. Model inventory

| Feature | model_id | deployment_id | Last recert | Deprecation review | Owner |
|---|---|---|---|---|---|
| action_extractor | gpt-4o | gpt-4o | 2026-06-09 | 2026-09-09 | platform-sre |
| activation_judge | gpt-4o | env: `VERTEX_AI_JUDGE_DEPLOYMENT` | 2026-07-09 | 2026-10-07 | platform-sre |
| anticipation_engine | gpt-4o | gpt-4o | 2026-06-09 | 2026-09-09 | platform-sre |
| backfill_extractor | gpt-4o | gpt-4o | 2026-06-09 | 2026-09-09 | platform-sre |
| blurb_generator | gpt-4o | gpt-4o | 2026-06-09 | 2026-09-09 | platform-sre |
| claim_extractor | gpt-4o | gpt-4o | 2026-06-09 | 2026-09-09 | platform-sre |
| context_synthesizer | gpt-4o | gpt-4o | 2026-07-09 | 2026-10-07 | platform-sre |
| decision_brief_advisor | gpt-4o | gpt-4o | 2026-06-09 | 2026-09-09 | platform-sre |
| exec_summary_drafter | gpt-4o | gpt-4o | 2026-06-09 | 2026-09-09 | platform-sre |
| intent_router | gpt-4o-mini | gpt-4o-mini | 2026-06-09 | 2026-09-09 | platform-sre |
| learning_distiller | gpt-4o | gpt-4o | 2026-06-09 | 2026-09-09 | platform-sre |
| m365_topic_router | gpt-4o-mini | gpt-4o-mini | 2026-06-09 | 2026-09-09 | platform-sre |
| onboard_assistant | gpt-4o | gpt-4o | 2026-06-09 | 2026-09-09 | platform-sre |
| prose_event_extractor | gpt-4o | gpt-4o | 2026-07-09 | 2026-10-07 | platform-sre |
| rev_extractor | gpt-4o | gpt-4o | 2026-07-09 | 2026-10-07 | platform-sre |
| rev_judge | gpt-4o | gpt-4o | 2026-07-09 | 2026-10-07 | platform-sre |
| setup_assistant | gpt-4o | gpt-4o | 2026-07-09 | 2026-10-07 | platform-sre |
| summary_generator | gpt-4o | gpt-4o | 2026-06-09 | 2026-09-09 | platform-sre |
| synthesizer | gpt-4o | gpt-4o | 2026-06-09 | 2026-09-09 | platform-sre |

`intent_router` and `m365_topic_router` use `gpt-4o-mini` (cheaper,
sufficient for the routing task). `activation_judge` resolves its
deployment from `VERTEX_AI_JUDGE_DEPLOYMENT` at call time rather than a
fixed pin, because judge-independence (it must differ from whatever
extractor deployment it is grading) is enforced at construction
(`src/ai/activation_judge.py::verify_judge_independence`); if the env var
is unset the judge fails closed to `JUDGE_UNAVAILABLE` rather than
silently reusing the extractor's deployment. All other new/existing
features use `gpt-4o`.

### 3.1 Notes on the six cards added under arch-fix.md Phase 0 (2026-07-09)

These six features had `ai_policy.yaml` entries and prompt-registry
entries (where applicable) but no card, prior to arch-fix.md's
Phase 0 model-card reconciliation (§0.4; spec archived to
`.archive/specs/arch-fix.md`, remaining scope tracked in
`specs/backlog.md` §7):

- **activation_judge** — `src/ai/activation_judge.py`, prompt
  `activation_judge.v1`. LLM-as-judge over real activation evidence
  (`activation.md` v1.25 §6.16); fail-closed when no judge deployment is
  configured; verdicts are evidence-grounded and never auto-execute an
  authority flip (human-gate only, per AG-4/AG-18).
- **context_synthesizer** — `src/ai/context_synthesizer.py`, prompt
  `context_synthesizer.v1`. NCFL Phase 5 Zone B knowledge-doc synthesis;
  output is always a proposal, never auto-applied.
- **prose_event_extractor** — `src/ai/discovery/prose_event_extractor.py`,
  prompts `prose_event_extractor.v1`–`v4` (wave-selected). The only card
  entry with `deterministic_first: true` — a local/deterministic tier is
  attempted before frontier at `tier0_confidence_threshold: 0.9`, so
  eval/κ-gating for this feature must cover both tiers (relevant to
  arch-fix.md AF-4, `specs/backlog.md` §7 BL-C2).
- **rev_extractor** — `src/ai/rev/rev_extractor.py`, prompt
  `rev_extractor.v1`. REV pipeline evidence extraction; always frontier
  (`deterministic_first: false`).
- **rev_judge** — `src/ai/rev/judge.py` (imported for compatibility via
  the now-thin `src/ai/rev/rev_judge.py` shim). Scores extractor output
  (correct/partial/hallucinated claim counts) for the REV pipeline.
  **Known gap, tracked by arch-fix.md AF-2 (`specs/backlog.md` §7 BL-C2)**: its system prompt
  (`_JUDGE_SYSTEM_PROMPT`) is still a raw Python string constant, not a
  `prompt_registry.py`-managed version — it is the one provider-bound
  prompt AF-2 explicitly calls out for migration into the registry.
- **setup_assistant** — `src/ai/setup_assistant.py`, prompt
  `setup_ws_suggest.v1`. Workstream-suggestion assistant used during the
  onboarding wizard; distinct from `onboard_assistant` (structure/style
  drafting prompts) despite the similar name.

## 4. Re-certification workflow

When a model is bumped (e.g. `gpt-4o` → `gpt-4o-2024-08-06`):

1. **Detect**: WS-24 `record_model_deployment_used` raises
   `ModelBumpDetectedError` on the first AI call after the bump
   (assuming `policy_block_on_bump=True`, the default).
2. **Block**: `FallbackAIClient` routes the call to the deterministic
   backup; the new deployment is NOT used.
3. **Re-eval**: Platform SRE runs the WS-5 eval set against the new
   deployment. The eval-set version is bumped (`v5` → `v6`).
4. **Prompt-card recert**: Platform SRE reviews the prompt card for
   any behavior-contract changes; the prompt-card version is bumped
   if the new model interprets the card differently.
5. **Update pin**: SRE appends a new `ModelPin` row to
   `_state/model_registry.jsonl` with the new `model_id` +
   `deployment_id` + a fresh `recert_at`.
6. **Lift the block**: the next AI call passes the registry check;
   the new model is served.

The bump history is preserved in the sidecar forever; the operator
can always see who flipped what and when.

## 5. Known limitations of the registry

- The registry is **per-program**. A multi-program fleet shares the
  same `ai_policy.yaml` but each program has its own
  `_state/model_registry.jsonl`. This is intentional — different
  programs may be at different stages of the recert workflow.
- The registry does **not** enforce that the deployment is *available*
  on Azure. A pin that points to a deleted deployment will fail at
  call time with `AIClientError` (existing `FallbackAIClient`
  behavior). The registry catches the *wrong-model* class of bug,
  not the *no-model* class.
- The registry's `policy_block_on_bump=False` escape hatch is for
  emergency-response scenarios (e.g. upstream outage forces a
  deployment swap). The DRI must flip it back to `True` within
  24 hours and run a re-cert.

## 6. Cross-references

- `governance/threat-model.md` T-7 — the threat model entry for
  silent model bumps.
- `src/core/model_registry.py` — the registry implementation.
- `src/core/state_reader_registry.py` — the D-18 entry that
  declares the sidecar.
- `tests/contracts/test_model_registry_contract.py` — the contract
  suite that prevents silent regressions in the registry behavior.

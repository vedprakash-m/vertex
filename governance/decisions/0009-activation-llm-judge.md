# ADR-0009: Activation LLM-judge — deep-expertise assessment, fail-closed, human-owns-flips

**Status:** Accepted
**Date:** 2026-07-07
**Decider:** Vertex engineering
**Companion:** `specs/activation.md` v1.25 §6.16; `src/ai/activation_judge.py`; `scripts/run_activation_judge.py`

## Context

The activation verifier (`scripts/verify_activation.py`) reports scaffold PASS/FAIL using `_count_text` substring matching. v1.24 found this over-claims: scaffolds reported PASS while their machinery was defined and unit-tested but never invoked on the production path (AG-11 privacy, AG-15 entity binding, AG-9 conflict — all wired in v1.24/v1.25). Substring-matching cannot tell "code exists" from "code runs on the real path."

We need a higher-fidelity assessment: a deep-expertise judge that reads **real evidence data** (cycle counts, corpus κ/CI, freeze hashes, counterfactual diff status, flip-gate state, proof-log drills) and renders informed, falsifiable verdicts on activation gates, tests, and authority promotions — replacing the substring-scaffold with evidence-grounded reasoning. This judge is needed not once but at **every promotion milestone** (when corpus annotation lands, Azure CS provisions, a clean cycle runs) to re-verify with real evidence.

## Decision

Build an LLM-judge with these binding properties:

1. **Library + thin script, not a baked CLI command.** `src/ai/activation_judge.py` (result model + orchestration; LLM call delegated through an injected `LLMProvider`, response routed through the shared `process_generated_text` safety pipeline per D-26) + `scripts/run_activation_judge.py` (self-resolves `VERTEX_AI_JUDGE_DEPLOYMENT`, fail-closes when unset). No `vertex` subcommand — avoids surface bloat, removable if the workflow later standardizes.

2. **Fail-closed.** When the judge LLM is unavailable (no deployment set, offline, budget exceeded, judge-independence fails), every gate is `JUDGE_UNAVAILABLE` with the deterministic finding preserved — **never a silent pass** (mirrors `AzurePromptShields` `VERDICT_UNAVAILABLE`).

3. **Deterministic expert fallback.** When no judge deployment is provisioned, `assess_activation_deterministic` applies the same bedrock falsifiability rules directly to the real evidence, producing the optimal-sequence recommendation + human-decision packets. The assessment is always available; the LLM enhances it.

4. **Evidence-grounded verdicts.** Every verdict cites specific data points. Bedrock rules: evidence-absent ≠ evidence-passed; a scaffold-pass without wiring is not a real pass; Wilson CI lower bound not point estimate; the trust root is "a human approved a fact."

5. **Judge-independence enforced.** The judge deployment must differ from the extractor (`verify_judge_independence`); checked at client construction.

6. **Human owns authority flips.** Even when the judge endorses a shadow→primary flip, `auto_executable` is **always False**. The flip is the highest-blast-radius event (AG-4/AG-18) and requires a human decider + rollback drill. The judge *assesses and recommends* with full context; the flip itself stays a human action. Auto-execution is scoped to reversible, non-authority-state actions only (writing durable verdicts, recording proof-log concurrence/veto).

7. **Durable, auditable artifact.** `output/judge-verdicts.json` — commit + model stamped, the evidence the judge saw, the prompt version, every verdict with its reasoning. Diff-able across promotion milestones.

## Consequences

- **+** The substring-scaffold over-claim class of bug is structurally harder to repeat: the judge reads real evidence, not symbol existence.
- **+** The optimal-sequence + human-decision assessment is always available (deterministic fallback), even before any LLM deployment is provisioned.
- **+** Authority flips remain a deliberate human action — the trust-root model (AG-17) is preserved.
- **−** Running the LLM judge requires provisioning `VERTEX_AI_JUDGE_DEPLOYMENT` (a distinct deployment) — an IT dependency, same class as Azure CS. The deterministic fallback carries the assessment until then.
- **−** A new prompt (`activation_judge.v1`) to maintain as the gates evolve — mitigated by the registered, versioned prompt loader (`registry.yaml`).

## Trust boundary (explicit)

The judge **advises**; the human **decides** flips. An LLM auto-promoting the system's trust boundary — even a correct one — is not a reversible, auditable action and would undermine AG-17. The judge gives the human everything needed to flip in one action (full evidence + concurrence + rollback checkpoint ready), but the flip is not auto-executed.

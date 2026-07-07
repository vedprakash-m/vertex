# AI Safety Approver — Role Charter

**Status:** UNFILLED — This is a required gate for WS-5 (AI Evaluation & Governance).  
**Filed:** 2026-06-11  
**Required by:** `specs/prod-vis.md` §4 WS-5 step 0; `specs/remains.md` §6 WS-5.

---

## Purpose

The AI Safety Approver is the named individual who reviews and approves the Vertex AI governance
framework before any frontier AI feature is declared production-ready. This role satisfies the
`[HUMAN GATE]` in WS-5 step 0 and unblocks:

- `governance/roles/ai-safety-approver.md` (this file) must be signed by the sponsor
- WS-5: AI eval corpus, graded LLM-output scoring, adversarial/red-team loop
- WS-5a: Full graduation gate — `deployment_fallback.py` refusing frontier without graduation records
- PB-14: INV-SG-2/4/5/6/7/8 invariant activation
- PB-15/16/17: Graded faithfulness scorer, adversarial loop, governance artifacts
- PB-32: ≥3 features flipped to `frontier_eligible: false`

## Responsibilities

1. **Approve the AI governance framework** — Review `governance/ai-policy.md` mapping to
   ISO 42001 / NIST AI RMF / MS Responsible AI standards.
2. **Authorize feature graduation** — Co-sign each `governance/graduations/<feature>.md` record
   (jointly with the program lead) before a feature is promoted to `frontier_eligible: true`.
3. **Approve adversarial evaluation** — Review and accept the adversarial corpus in
   `tests/adversarial/` before it is promoted to the required-fail gate.
4. **Recertify on model bumps** — Per `governance/model-cards.md` recert workflow, approve any
   AOAI/Claude model-version change after re-evaluation.
5. **Maintain this charter** — Update `acknowledged_at` annually (or when role changes).

## Acceptance Criteria

Before WS-5 can be declared DONE:

- [ ] This file has a named `approver:` and `acknowledged_at:` (ISO 8601 date)
- [ ] `governance/ai-policy.md` clause-level mapping exists (ISO 42001 + NIST AI RMF + MS RAI)
- [ ] At least 1 `governance/graduations/<feature>.md` record is co-signed
- [ ] `tests/adversarial/` corpus has ≥20 samples reviewed and approved by the approver

## To Fill This Role

Replace the template values below and commit this file:

```yaml
# Fill in these fields to activate WS-5:
approver: "UNNAMED — fill in: firstname.lastname@microsoft.com"
title: "UNNAMED — fill in: Principal PM / Engineering Manager / etc."
program: "UNNAMED — fill in: Acme / Fabrikam / etc."
acknowledged_at: ""   # ISO 8601 date when the approver accepted this charter
sponsor: "UNNAMED — fill in: the person who nominated the approver"
```

**Note:** Until `approver` and `acknowledged_at` are filled, `vertex doctor --platform-readiness`
will report WS-5 as BLOCKED-HUMAN-GATE. The platform's AI features run in
`frontier_eligible: true` mode for all features today (PB-32 — all 14/14 features are true).
Filling this charter is the prerequisite for tiering those features properly.

---

*This file was created as a scaffold by the coding agent in v1.18. The role content is a template
and must be reviewed and accepted by a named individual before it takes effect.*

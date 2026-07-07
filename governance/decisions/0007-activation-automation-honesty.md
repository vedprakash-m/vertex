# ADR-0007: Activation Automation Honesty

**Status:** Accepted
**Date:** 2026-07-01
**Owner:** Vertex eng / activation operator

## Context

Activation v1 must not imply mailbox discovery is fully automatic when the
current reliable path is operator-mediated export and deposit of EML files.
The product contract accepted by ADR-0006 is `automatic_after_deposit`: once
mail is deposited into the program REV inbox, Vertex may gather, extract,
stage, triage, project, and render through the governed workflow.

## Decision

For Activation v1, the supported source-pickup model is:

1. The operator manually exports relevant EMLs into the program REV inbox.
2. Vertex automates the pipeline after deposit.
3. The operator runbook names the manual export step explicitly.
4. Any product/vision language for v1 must say `automatic_after_deposit`, not
   full inbox automation.
5. Full Graph API / WorkIQ mailbox discovery remains a roadmap item gated by
   tenant consent, privacy review, and source-health checks.

This satisfies AG-7 by documenting reality honestly instead of disguising the
manual step as automation.

## Consequences

- AG-20 time-motion measurements must include manual export time.
- If manual export plus triage is slower than manual typing, deployment pauses
  pending Graph automation.
- A future full automation promotion needs a new decision record, updated
  operator runbook, and live source-health proof.

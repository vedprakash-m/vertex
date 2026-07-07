# Activation Annotation And Adjudication Plan

> **Status note (2026-07-07):** This plan is currently **not actionable** for the XPF program — see `specs/backlog.md` §3 (BL-A2). Real extraction-sourced volume for the keystone family (`milestone.completed`) is 1 instance across the program's entire real history (vs. the 25-30 needed), a structural data-scarcity finding, not an annotation-labor gap. `recommended_v1_authoritative` has been accepted as the permanent practical bar for this single-program deployment. This template is retained for when/if fleet rollout (≥3 programs) makes pooled dual-annotation viable.

**Scope:** P2 / §6.15.3 corpus certification for the selected keystone family.

| Role | Assignment |
|---|---|
| Primary annotator | `annotator1` |
| Second annotator | `activation-secondary-labeler` |
| Adjudicator | `activation-adjudicator` |
| Guideline owner | `activation-tpm` |

**Target:** at least 30 keystone-family labels, including at least 20 dual-labeled documents.

**Quality bar:** κ >= 0.70, `g_xtract_prec_ci_low >= 0.80`, and `g_accept_prec_ci_low >= 0.85` before authority promotion.

**Guideline URI:** `governance/activation-annotation-plan.md`

**Due date:** 2026-07-15

**Adjudication protocol:** disagreements are adjudicated by the named adjudicator, with label-guideline updates applied before relabeling any disputed batch. The corpus is frozen only after the second-label and adjudication pass is complete.

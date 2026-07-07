# Activation Annotation And Adjudication Plan

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

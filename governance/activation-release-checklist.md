# Activation Binding Release Checklist

This checklist controls when activation evidence can become binding release evidence. It is deliberately stricter than local development status: a branch can demonstrate progress while still being unreleasable.

## Evidence Classes

- **branch-local evidence**: uncommitted or dirty working-tree files, local output, ad hoc render artifacts, and verifier output produced before the final release commit.
- **canonical evidence**: committed source, committed governance artifacts, committed quality/corpus manifests, and regenerated verifier output from the exact release commit.

`dirty_worktree` from `scripts/verify_activation.py` must be false before any binding release claim is made. If `dirty_worktree` is true, the release may be reviewed as branch-local progress only.

## Required Green Checks

Before a binding Activation-v1 release:

- `python scripts/verify_activation.py --self-test` passes.
- `scripts/verify_activation.py --program xpf --json output/activation-report.json --markdown output/activation-evidence.md` has been regenerated from the release commit.
- `P0-VERIFIER-SELF-TEST` and `P0-CONSOLIDATED-VERSION-PIN` are green.
- `P-1-RAW-DATA`, `O-0-DATA-SUFFICIENCY`, `AG-2-CORPUS-CERTIFICATION`, `AG-3-CLEAN-CYCLE`, `AG-3-CLEAN-CYCLE-STREAK`, and `AG-1-COUNTERFACTUAL-DIFF` are green for Activation-v1.
- Any remaining red row is either explicitly out-of-bar for the release or has a signed owner/date exception in governance.

## Release Review

- Release owner: activation-release-owner.
- Evidence reviewer: activation-tpm.
- Engineering reviewer: activation-em.
- Decision rule: fail closed on missing verifier output, branch-local-only evidence, stale corpus freeze, stale render artifacts, or unexpected red checks.

## Current Status

This branch is not a binding release while real-data gates remain red. The checklist exists so PS-9-style branch-local work can be reviewed without being mistaken for committed canonical proof.

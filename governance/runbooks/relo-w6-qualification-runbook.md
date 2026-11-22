# RELO-W6 qualification enrollment runbook

## Scope and refusal boundary

Use this runbook to enroll evidence for RELO-W6.1 through RELO-W6.12. The
scripts prepare and verify evidence; they do not contact providers, label data,
authenticate owners, run live Outlook clients, copy private data, execute a
restore drill, or qualify a production deployment.

The verifier classifies mechanically complete packets as `owner_review`. Live
or owner-gated acceptance remains open until the named owner supplies private
evidence and approval. Synthetic, test, or local-only records must never be
represented as real qualification.

## Generate an enrollment packet

```bash
python scripts/verify_relo_w6.py --template RELO-W6.1 > w6-1.json
```

Replace the card ID to generate any one of `RELO-W6.1` through
`RELO-W6.12`. Populate only references to approved private evidence; do not copy
mail, transcripts, recipient data, access tokens, or raw private labels into a
public repository packet.

## Verify an evidence packet

```bash
python scripts/verify_relo_w6.py \
  --input w6-1.json \
  --repo-root .
```

Exit 0 means the packet is mechanically admissible for owner review. Exit 1
means input, hash, gate, or enrollment evidence is missing. Neither exit
classifies live production as qualified.

Repository-local artifacts must carry a SHA-256 digest. Private artifacts use
an evidence-system reference in `external_evidence_ref`; their identity and
authenticity remain an owner-controlled review step.

## W6.1-W6.2 study enrollment

1. Freeze guidelines, corpus split, annotation protocol, model family, prompt,
   and blind ordering before labels are generated.
2. Record model-assisted annotation as
   `model-assisted annotation; single human adjudicator`.
3. Use at least 60 items per risk and dependency stratum; record 120-item
   re-verification after the second divergence.
4. Record Cohen-style human/model kappa, every adjudication, source families,
   and the separately sampled human-only audit.
5. Enroll at least eight matched weekly cycles across the applicable workflow
   taxonomy, covering at least 49 elapsed calendar days.
6. Record completion, displacement, correction, exception, downstream-error,
   and quality-adjusted time metrics for every cycle.
7. Verify the W6.1 and W6.2 packets separately.

Blocker: real labels, approved cohort, baseline representativeness, blind
adjudication, and independent qualification are owner/live evidence.

## W6.3 deterministic economics enrollment

Reduce existing checksum-protected tier telemetry without contacting a
provider:

```bash
python scripts/run_relo_w6_shadow_harness.py \
  --store programs/<program>/runtime/tier_decisions.jsonl \
  --window-start 2026-01-01T00:00:00Z \
  --window-end 2026-01-31T00:00:00Z \
  --eligible-routine-tasks 100 \
  --completed-routine-tasks 96 \
  --expected-tier-decisions 100 \
  --ratified-local-coverage-floor 0.40 \
  --ratified-routine-completion-floor 0.95
```

Copy the result into the W6.3 template. Cache reuse stays separate from
deterministic/local coverage. The reducer fails the packet when records are
missing, unclassified, remotely executed for certified routing, or below the
99% telemetry floor.

Blocker: live shadow cycles, owner-ratified coverage/completion floors, and
production-tier completeness cannot be manufactured by the reducer.

## W6.4 Outlook and projector UAT

Generate local artifacts first, then prepare the evidence package:

```bash
vertex report --edition <edition> --issue <n> --dry-run
python scripts/prepare_relo_w6_uat.py \
  --edition <edition> \
  --issue <n> \
  --artifact outlook_desktop <desktop.html> \
  --artifact outlook_web <web.html> \
  --artifact outlook_ios <ios.html> \
  --artifact projector_1920x1080 <projector.html> \
  --output-dir <private-output-directory>
```

The manifest contains only profile labels, file names, sizes, and hashes. Do
not place the generated package in a public publication candidate. The owner
must inspect live Outlook Desktop/Web/iOS and the projector output and provide
private approval and blind-review evidence.

Blocker: live clients, projectors, reviewer availability, and publication
approval are external gates.

## W6.5-W6.6 retirement and traceability

Each deletion candidate requires:

- the exact repository path and replacement path;
- caller inventory, shadow, and test artifact IDs;
- a qualified dependency gate and bounded LOC count;
- a rollback window;
- a canonical traceability mapping to a spec invariant, governance decision,
  budget, test, runbook, or compact evidence ledger.

The verifier resolves repository paths, verifies artifact references, and
checks that the declared LOC equals the current bounded file. A valid manifest
does not authorize deletion. Delete only one independently green slice at a
time.

Blocker: deletion still depends on qualified replacements and owner-reviewed
shadow evidence; the 3,500-5,000 LOC figure is planning context, never a reason
to remove safety, recovery, privacy, or idempotency code.

## W6.7-W6.8 maturity and governed feedback

Automatic and explicit proposal-class transitions append digest-checked records
to `programs/<id>/journal/maturity_transitions.jsonl`. Preserve the transition
history with its counters, ceiling, and reason. Supply the W6.7 packet's owner
approval, independent review, and rollback refs for L3/L4.

For feedback promotion, require passing privacy, contamination, development,
frozen-holdout, and shadow evaluations, a positive review-burden reduction,
human approval, and rollback evidence. `self_update` must remain false.

Blocker: actual reviewer approval, independent L3/L4 evidence, and live
holdout/shadow outcomes are owner evidence.

## W6.9 continuity holdout

Bind the frozen corpus, input snapshot, and full-rebuild hash. Report binding
precision, material recall, overall recall, and a long-horizon reproducibility
run. The verifier fails any rebuild mismatch or absent continuity run.

Blocker: a real independently labeled and time-held-out corpus is required.
Synthetic fixtures only exercise the contract.

## W6.10 scale and restore

Freeze the exact profile before measurement. Include host/storage profile,
minimum and maximum admitted programs, entity/relation/revision/document
dimensions, change rate, source concurrency, provider budgets, scheduler,
retention, p50/p95/p99 latency, queue wait, compiler envelope, storage growth,
economics, onboarding, and 80%/90% admission tripwires.

Run the restore path in an isolated replacement root. Measure RPO and RTO
without changing live state. Existing synthetic 40-program and backup tests
are regression evidence, not production qualification.

Blocker: live profile economics, capacity, restore drill, owner-ratified NFRs,
and provider qualification remain external.

## W6.11 learning disposition

Generate the explicit inventory:

```bash
python scripts/verify_relo_w6.py --learning-dispositions
```

The inventory must show every named legacy path as retained forensic,
retained governed, replaced, or deleted, with caller and privacy evidence.
Runtime mutation and runtime prompt consumption remain false for legacy
edit-pattern/distillation paths.

## W6.12 terminal state

Verify a W6.12 packet directly:

```bash
python scripts/verify_relo.py \
  --repo-root . \
  --spec <selected-private-spec> \
  --terminal-state \
  --w6-evidence w6-12.json \
  --json
```

The supported states are `PROPOSED`, `CONTAINED`, `ADVISORY_OPERATIONAL`,
`RELO_PROFILE_COMPLETE`, and `HALTED`. The verifier requires exact state
predicates and always reports `product_vision_complete: false`: RELO completion
is not product-vision completion.

`CONTAINED` and `HALTED` preserve containment, explicit hybrid/store
disposition, drained in-flight state, and evidence. `RELO_PROFILE_COMPLETE`
requires a frozen profile ID, fixed targets, ratified budgets, mandatory
sources, scheduled recovery, proficient operators, closed-loop certification,
independent labeling, and the >80% human-impact lower bound.

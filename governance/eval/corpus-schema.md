# ADF-W0.15: Evaluation Corpus Schema

Companion header: `specs/arch-data-fix.md` Section 3.6.2a / Section 8.15.3 /
Appendix C brief `ADF-W0.15`. Do not duplicate detail here that belongs in
the spec — this document is the corpus's own operational reference (where
items live, what fields they carry, how splits work), not a restatement of
the evaluation harness's design rationale.

## Location

`tests/eval/corpus/<family>.jsonl` — one file per evaluated family (e.g.
`risk.jsonl`, `dependency.jsonl`, `entity_binding.jsonl`, `milestone.jsonl`).
One JSON object per line. Loaded by `tests/eval/corpus_schema.py::load_corpus_family`.

Corpus files checked into `tests/eval/corpus/` must contain only synthetic
or already-PII-scrubbed excerpts (see "PII safety" below) — this directory
is tracked in git, unlike `programs/`/`.archive/` sidecars.

## Row schema

```python
@dataclass(frozen=True, slots=True)
class CorpusItem:
    item_id: str            # stable id, unique within (family, split)
    family: str              # "risk" | "dependency" | "entity_binding" | "milestone" | ...
    split: str                # "train" | "dev" | "holdout"
    input_excerpt: str       # PII-safe input text/excerpt the extractor sees
    label: str | dict         # the ground-truth label (family-specific shape)
    label_source: str        # "human" | "adjudicated" | "heuristic" | "llm_judge"
    annotator: str | None    # pseudonymous annotator id, None for heuristic/synthetic rows
    annotated_at: str | None  # ISO-8601, None for heuristic/synthetic rows
    notes: str | None        # optional free-text annotation context
```

`label_source` is the field Section 8.15.3 hinges on: "LLM-as-judge may
generate diagnostics but cannot be the sole quality label or promotion
authority." A row whose only `label_source` is `llm_judge` is a candidate
for review, not admissible ground truth — the holdout lane
(`tests/eval/holdout_lane.py::run_holdout_evaluation`) refuses to run
against a corpus with zero `human`/`adjudicated` rows.

## Splits

- `train`: not used by any evaluation lane today (reserved for a future
  fine-tuning/prompt-iteration use, per Section 8.15.3's own scope — this
  spec does not build model training).
- `dev`: used during prompt/policy iteration, outside CI (no lane wired to
  it yet).
- `holdout`: the certification-grade split. Never touched during iteration.
  Only `run_holdout_evaluation` reads it, and only when it also carries
  real (`human`/`adjudicated`) labels.

## CI regression lane vs. holdout lane

| Lane | Reads | Purpose | Marker |
|---|---|---|---|
| CI regression | Synthetic fixtures (any `label_source`, deterministic assertions only) + the Issue-079 regression corpus (`tests/unit/test_milestone_no_evidence.py`) | Catch structural/output regressions on every merge | `@pytest.mark.eval_ci` |
| Human holdout | `tests/eval/corpus/*.jsonl` `split=holdout` rows with `label_source in (human, adjudicated)` | Measure precision/recall/coverage/calibration for autonomy promotion (Section 8.15.3) | not yet wired to a pytest marker — see "Status" below |

## PII safety

`input_excerpt` must be a synthetic or already-scrubbed string before it is
committed to `tests/eval/corpus/*.jsonl`. This corpus is NOT the mechanism
for collecting real program excerpts — Section 3.6.2a's "how PII-safe
excerpts are collected" is a separate, still-open annotation-pipeline
question (ADF-W0.4/ADR-0016's staffing plan), not solved by this schema.

## Status (2026-07-13)

This schema and its loader (`tests/eval/corpus_schema.py`) are built and
tested. The corpus itself is empty except for a handful of synthetic
fixture rows proving the loader round-trips correctly — no real
independently-labeled holdout corpus exists yet (ADR-0016: one annotator,
zero independent second reviewer; `min_total_if_perfect` denominators
unmet). `run_holdout_evaluation` is real and tested against synthetic
fixtures but has never been run against genuine holdout data. This is an
honest, tracked gap, not a hidden one — see `specs/arch-data-fix.md`'s
ADF-W0.15 status row for the authoritative current state.

# `ledger backfill` Runbook

**Tracked, generic counterpart of a private, workspace-specific runbook.**
This file ships with the repo so a fresh clone has an operator runbook for
governed historical backfill without any program-specific detail (program
IDs, exact deck/issue counts, real date ranges). If your local workspace has
a more detailed version tailored to a specific program under `docs/`
(gitignored), prefer it for day-to-day operation — it should record real
expected-volume figures for that program's corpus — and treat this file as
the canonical baseline to keep in sync.

Operator guide for governed historical backfill into the program ledger. Run
from repo root with the repo venv active. Current implementation status is
mixed by tier:

- Tier A: live CLI support exists through `python cli.py ledger backfill`.
- Tier B: governed staging exists through `python cli.py ledger import` once
  extractor output is available.
- Tier C: knowledge-plane ingest/extract/triage commands are live; this tier
  lands into claims first, not directly into the ledger.

This runbook is intentionally procedural, not aspirational. If a step says
"prepare extractor output first", that means the one-command orchestration
path is not landed yet. Replace `<program>` below with the target program ID
and `<alias>` with the operator's identity.

---

## Prerequisites

- Python 3.11+ with `.venv` active.
- Repo root is the current working directory.
- `python cli.py doctor --kb` and `python cli.py doctor --ids` are green for
  the target workspace before starting bulk staging.
- Operator has decided the backfill history depth for the run (full history
  vs. a bounded window).
- For approval work: keep a scratch note of accepted batch IDs and
  spot-check results.

Recommended pre-flight:

```pwsh
python cli.py doctor --kb
python cli.py doctor --ids
python cli.py ledger status --program <program>
```

---

## Batch Gates

Every backfill batch uses the same deterministic QG-DM-9 gate before
approval:

- Entity-resolution rate must be at least `0.90`.
- Spot-check sample must be approved at `n >= 10` or `5%` of the batch,
  whichever is larger.
- No unresolved current-state field-lock conflicts may remain.
- PII must be checked during the operator sample, especially for email or
  transcript-derived batches.

Read the current gate state with:

```pwsh
python cli.py ledger triage batch-status --program <program> --batch-id <batch_id> --format json
```

Approve only after the gate is green:

```pwsh
python cli.py ledger triage batch-approve --program <program> --batch-id <batch_id> --actor <alias>
```

If the staged batch is wrong before approval, quarantine it instead of
editing `pending.jsonl`:

```pwsh
python cli.py ledger quarantine-batch --program <program> --batch-id <batch_id> --actor <alias> --reason "<why>"
```

The same reversal is also available on the backfill surface:

```pwsh
python cli.py ledger backfill --program <program> --quarantine-batch <batch_id> --actor <alias> --reason "<why>"
```

---

## Tier A — Historical decks / documents

### Expected volume

Workspace-specific — before running a real backfill, record the actual
corpus size (document count, date range, any known exclusions such as a
working-copy draft) for the target program in your local runbook copy under
`docs/`. Do not bulk-stage without first sizing the corpus via a dry run.

### Dry-run enumeration

Start with a dry run over the recursive source directory:

```pwsh
python cli.py ledger backfill --program <program> --source-dir <source_dir> --from <start_year> --dry-run
```

Expected outcome:

- The walk is recursive.
- Only the intended subdirectories/files are counted.
- Any known-excluded working copies are excluded.
- Output prints a generated batch ID and a few sample staged artifacts.

### Staging

Stage small chronological slices (roughly 10 documents per batch, earliest
first, is a reasonable default pace).

```pwsh
python cli.py ledger backfill --program <program> --source-dir <source_dir> --from <start_year>
```

Then review:

```pwsh
python cli.py ledger triage list --program <program> --batch-id <batch_id>
python cli.py ledger triage batch-status --program <program> --batch-id <batch_id> --format json
```

### Spot-check protocol

For the sample set, verify all of the following:

- `source_ref.file_path` matches the document actually reviewed.
- `occurred_at` uses the document's own date when the event is only
  approximately dated.
- Entity resolution is correct for milestone, decision, risk, workstream,
  and commitment identifiers.
- No obvious duplicate-source artifacts survived dedupe.
- No extracted text introduced PII that should not enter the ledger.

### Approval

Approve only after the sample is green and the batch-status gates pass:

```pwsh
python cli.py ledger triage batch-approve --program <program> --batch-id <batch_id> --actor <alias>
python cli.py ledger replay --program <program> --reindex
python cli.py ledger status --program <program>
```

### Failure / rollback path

Before approval: quarantine the batch.

After approval: correct the resulting events by tombstoning the
`resulting_event_id` rows recorded in `triaged.jsonl`. Do not mutate
candidate logs by hand.

---

## Tier B — Structured periodicals (e.g. newsletters)

### Expected volume

Workspace-specific — record the actual issue range, any known gaps, and the
coverage window for the target program in your local runbook copy.

### Current execution posture

The governed staging path is live, but bulk orchestration for this tier is
still partial in most deployments. Use this tier only when extractor output
already exists in JSONL form.

The staged source rows may be either:

- event-envelope-shaped rows, or
- candidate-like rows that `ledger import` can normalize into governed
  candidates.

### Staging

```pwsh
python cli.py ledger import --program <program> --source <extractor-output.jsonl> --dry-run
python cli.py ledger import --program <program> --source <extractor-output.jsonl>
```

Then run the same review and QG-DM-9 sequence used for Tier A:

```pwsh
python cli.py ledger triage list --program <program> --batch-id <batch_id>
python cli.py ledger triage batch-status --program <program> --batch-id <batch_id> --format json
```

### Spot-check protocol

During the sample, explicitly verify:

- Issue/edition identity is correct.
- Any known missing issue is represented as a gap window, not silently
  skipped as if it existed.
- Any non-structured-format (e.g. `.eml`-only) source preserves provenance
  and section context.
- KPI observations map to the intended metric or KPI identifiers.
- The overlap with current fact-store history is deduping rather than
  double-counting.

If the extractor output is low quality, quarantine the batch and fix
extraction upstream instead of bulk approving noisy candidates.

---

## Tier C — Knowledge corpus

### Expected volume

Workspace-specific — record the actual document/KB-file corpus size for the
target program in your local runbook copy.

### Current execution posture

Tier C uses the landed knowledge-plane workflow:

```pwsh
python cli.py knowledge ingest --scope <scope> --source <path>
python cli.py knowledge extract --scope <scope> --dry-run
python cli.py knowledge extract --scope <scope>
python cli.py knowledge triage list --scope <scope>
```

Use `batch-status`, `batch-approve`, and `quarantine-batch` on the knowledge
side the same way you use the ledger queue on Tier A/B.

### Shape rule

Before approving anything from Tier C, confirm the content belongs in the
right plane:

- Program happenings and historical events belong in the ledger.
- Product/domain/process facts belong in knowledge claims.

Do not force domain knowledge into ledger events just because it came from a
historical file.

---

## Spot-Check Checklist

Use this checklist for every sampled candidate set:

- Source provenance is present and points to the right artifact.
- Entity IDs are correct and stable.
- Event type matches the underlying source statement.
- Temporal confidence is honest.
- No obvious duplicate survived dedupe.
- No lock conflict is being ignored.
- No PII or sensitive excerpt should be excluded or redacted.

If two or more sampled candidates fail for the same upstream reason, stop
the batch and quarantine it.

---

## Post-Batch Validation

After each approved batch:

```pwsh
python cli.py ledger replay --program <program> --reindex
python cli.py ledger verify --program <program> --deep
python cli.py ledger gaps --program <program>
python cli.py ledger status --program <program>
```

Review for:

- replay/verify success,
- unexpected new gap growth,
- unreasonable candidate or gap counts,
- lock-conflict regressions.

---

## Operator Notes

- Chronological ordering matters. Earlier approvals improve later entity
  resolution and dedupe.
- Prefer many small reversible batches over one large irreversible cleanup.
- Never edit `pending.jsonl` or `triaged.jsonl` manually.
- For post-approval cleanup, write corrections; for pre-approval cleanup,
  quarantine the batch.
- Actual approval remains operator work even when staging is automated.

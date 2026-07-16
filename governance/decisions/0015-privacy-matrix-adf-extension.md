# ADR-0015: Privacy matrix extension for ADF artifacts + RTBF period correction (ADF-W0.16)

**Date:** 2026-07-13
**Status:** Accepted (2026-07-13)
**Workstream (from `specs/arch-data-fix.md`):** ADF-W0.16; corrects
ADR-0014 Decision 3.
**Author(s):** Vertex engineering (drafted per live decision-by-decision
session)
**Approver(s):** the Platform DRI (per ADR-0013;
the Security-reviewer role is also the Platform DRI in advisory mode per
ADR-0013 — this ADR is advisory-reviewed, not enforce-mode-approved)

## Context

`governance/privacy-matrix.md` / `src/core/privacy_matrix.py` (WS-15,
2026-06-09) predate this session's ADF work. A file-path inventory
(2026-07-13) found five new persistent artifacts introduced by ADF work
items with **no privacy-matrix coverage**:

| Artifact | Introduced by | Contains PII? |
|---|---|---|
| `nudge/drafts/<solicitation_id>.eml` | ADF-W3.7 (`context_gap_solicitation.py`) | Yes — recipient + gap text |
| `nudge/replies/<message_id>.eml` | ADF-W3.7 (`context_gap_reply_import.py`) | Yes — raw, unfiltered stakeholder reply |
| `_feedback/context_gap_solicitations.jsonl` | ADF-W3.7 (cooldown log) | No — id/fingerprint/timestamp only |
| `runtime/program_synthesis/<ai_run_id>.json` | ADF-W2.9 (`program_synthesis.py`) | No — aggregated business content only |
| `workstream_registry.yaml` | ADF-W3.7 (Decision 3b gave it its first writer, `context_gap_reply.py`) | Potentially — verbatim reply text applied into `deep_context` fields could carry an incidental signature block |

**Correction to ADR-0014 Decision 3:** that ADR's RTBF proposal assumed a
persistent `PseudonymTable` mapping could be the purge hook ("purge/
pseudonymize N days after archival via the existing PseudonymTable").
This session's file-path inventory found that assumption is **wrong**:
`PseudonymTable` (`src/core/rev/privacy.py`, pre-existing W5-3 code) is
**in-memory and thread-local only, never persisted to disk by design**
(`specs/arch-data-fix.md` §8.9.5: "no pseudonym reverse mapping is
written into the AI audit"). There is no on-disk pseudonym mapping to
purge. ADR-0014 Decision 3 is superseded by this ADR.

## Decision

1. **Register all five artifacts** in `src/core/privacy_matrix.py`'s
   `SIDECAR_RETENTION` tuple (the runtime source of truth that drives
   `vertex privacy purge`/`doctor --privacy`), with matching rows added to
   the tracked `governance/privacy-matrix.md` §3 table and
   `governance/data-classification.yaml`. Classifications and retention
   per the table above's PII column, all at the existing platform-standard
   **1-year (365-day)** retention except `workstream_registry.yaml`
   (indefinite, live config file, not a rotating log).

2. **Resolve the RTBF period question directly**, correcting ADR-0014
   Decision 3: **N = 365 days**, matching the platform's own pre-existing
   PII default (`DataClassification.PII` → `RetentionClass.ONE_YEAR` is
   already the standing default for every other PII sidecar in this
   matrix — `journal/edit_patterns.jsonl`, and now the two new `nudge/`
   PII entries). The purge mechanism is the **existing** WS-18
   `[EXCISED]` tombstone applied via `vertex privacy purge` (already
   built and used for every other PII sidecar), not a new mechanism. No
   new number needed to be invented — the platform already had the
   answer; ADR-0014 just cited the wrong hook (`PseudonymTable`) to hang
   it on.

3. **`workstream_registry.yaml` gets `supports_excise=False`**, matching
   the existing precedent for `runtime/gather_state.json`: it is a live,
   operator-authored config file overwritten in place with a single
   non-rotating `.bak`, not an append-only audit log. The `[EXCISED]`
   tombstone mechanism is designed for rotating JSONL logs; the correct
   remediation path for a bad field in a config file is a direct edit by
   the operator, which is already possible today.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Leave `PseudonymTable` as the RTBF hook, build a new on-disk pseudonym store to purge | Real new engineering with no current driver; contradicts the existing, deliberate design decision (§8.9.5) that the reverse mapping is never persisted, which is itself a privacy control (an attacker who reads disk cannot de-pseudonymize). Undoing that design to create something to "purge" would make privacy worse, not better. |
| Give `nudge/replies/*.eml` a shorter retention than other PII (since it's raw/unfiltered) | Considered, but 1 year matches every other PII sidecar and channel-level PII retention in this matrix (ADO/Kusto/IcM all at 1 year); introducing a bespoke shorter window for just this one artifact adds inconsistency without a clear driving requirement. Revisit if a real incident or DPA constraint calls for it. |
| Apply `[EXCISED]` tombstone semantics to `workstream_registry.yaml` | Rejected — the tombstone mechanism assumes an append-only line-oriented log where a row can be replaced with a marker while preserving hash-chain integrity elsewhere in the file. A single-document YAML config has no such structure; a direct field edit is simpler, already possible, and matches the `gather_state.json` precedent. |

## Consequences

**Easier:** `vertex privacy purge`/`doctor --privacy` now have real
coverage for every ADF-introduced artifact; ADR-0014's one remaining
open RTBF question is resolved instead of left as a TBD; the correction
prevents a future engineering pass from building unnecessary/counter-
productive on-disk pseudonym persistence.

**Harder:** none identified — this is additive registration using
already-existing infrastructure (`RetentionClass.ONE_YEAR`, the
`[EXCISED]` tombstone, `vertex privacy purge`) rather than new mechanism.

## References

- `src/core/privacy_matrix.py` (`SIDECAR_RETENTION`)
- `governance/privacy-matrix.md` §3
- `governance/data-classification.yaml`
- `tests/contracts/test_privacy_matrix_contract.py`
- `specs/arch-data-fix.md` §8.9.5 (PseudonymTable non-persistence design)
- Related: ADR-0013 (RACI — Security reviewer advisory scope),
  ADR-0014 (Decision 3, superseded by this ADR)

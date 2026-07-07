# Privacy & Data Governance Matrix

**Status:** v1.0 (WS-15, 2026-06-09). Approved-by: `[HUMAN GATE — DPO/Privacy review]`.
**Scope:** governs the data classification, retention, RBAC/consent posture, and export
boundaries for every channel Vertex reads from, every artifact it writes, and every
artifact it ships to a downstream system.

This matrix is the **canonical reference** for `vertex doctor --privacy` and
`vertex privacy show`. The Python source of truth lives in
`src/core/privacy_matrix.py` (driven by the constants in this file, and asserted
by contract tests in `tests/contracts/test_privacy_matrix_contract.py`).

---

## 1. Data classifications

Every field in every payload Vertex handles falls into exactly one of these classes.

| Class | Description | Examples | Default retention |
|-------|-------------|----------|-------------------|
| **PUBLIC** | Safe to publish; no confidentiality loss. | Public roadmap, public service status | indefinite |
| **INTERNAL** | Internal to the operating org; not for external distribution. | Engineering metrics, internal team names | 1 year |
| **CONFIDENTIAL** | Restricted to need-to-know; the leak is a real incident. | Work-item titles, narrative drafts, vital signs | 1 year |
| **PII** | Personally identifying data; subject to deletion rights. | Email addresses, person names, manager hierarchy | 1 year (subject to DPA) |
| **SECRET** | Credentials and tokens; leak is a security incident. | ADO PAT, Graph client secret, keyring material | ephemeral (rotated) |

## 2. Channel posture

| Channel | Default class for **read** payloads | Default class for **write** payloads | Retention | RBAC/consent |
|---------|-------------------------------------|---------------------------------------|-----------|--------------|
| `ado` (Azure DevOps) | CONFIDENTIAL (work-item titles, descriptions, PR titles) | CONFIDENTIAL (any PR/comment we add) | 1 year (matches Azure DevOps audit retention) | AAD user-context; the operator's own PAT (least-privilege `vso.work_write` for confirm) |
| `kusto` | CONFIDENTIAL (cluster metrics, fleet rollout) | n/a (Vertex does not write back to Kusto) | 1 year | AAD managed identity; cluster-scoped `Viewer` role (least-privilege); data-plane RBAC scoped to the program schema |
| `icm` | CONFIDENTIAL (incident metadata, severity) | n/a (Vertex does not write back to IcM) | 1 year (matches IcM retention) | AAD app-only; least-privilege `IcMIncidentRead` |
| `teams` | INTERNAL (channel messages, Adaptive Card payloads) | INTERNAL (any card we post) | 180 days (matches Teams message retention) | Graph application permissions `ChannelMessage.Send`, `Channel.ReadBasic.All`; tenant admin consent required |
| `workiq` (M365) | CONFIDENTIAL (calendar, mail subject, attendees) | n/a (read-only) | ephemeral; not persisted to disk beyond the live gather | Graph delegated `Calendars.Read`, `Mail.Read`; user-context only |
| `sharepoint` (M365 — LT deck / ref doc) | CONFIDENTIAL (slide content, program priorities, internal decisions) | n/a (read-only; local .pptx backfill files are operator-managed) | ephemeral for WorkIQ path; backfill .pptx files are at rest under `programs/<id>/backfill/sharepoint/` (operator-controlled) | Graph delegated `Sites.Read.All` or `Files.Read.All`; user-context; operator must hold share permissions on the target SharePoint site |
| `transcript` (M365 meetings) | CONFIDENTIAL (speech-to-text output) | n/a (read-only) | ephemeral; not persisted beyond the live gather | Graph `OnlineMeetings.Read`, `CallRecords.Read.All`; tenant admin consent |

## 3. Retention by sidecar / artifact

| Artifact / sidecar | Default class | Default retention | Excise / GDPR |
|-------------------|---------------|-------------------|----------------|
| `journal/signals.jsonl` | CONFIDENTIAL + PII (people mentions) | 1 year | `[EXCISED]` tombstone (WS-18) |
| `journal/reviews.jsonl` | CONFIDENTIAL + PII (reviewer) | 1 year | `[EXCISED]` tombstone (WS-18) |
| `journal/autonomy_audit.jsonl` | PII (operator who triggered the action) | 7 years (audit-of-record) | `[EXCISED]` tombstone (WS-18) |
| `journal/actions.jsonl` | CONFIDENTIAL | 1 year | `[EXCISED]` tombstone (WS-18) |
| `journal/ai_proposals.jsonl` | CONFIDENTIAL + PII (reviewer) | 7 years (audit-of-record) | `[EXCISED]` tombstone (WS-18) |
| `people_profiles.yaml` (encrypted) | PII | indefinite (subject to per-person deletion right) | full-record deletion on per-person request; keyring rotation on personnel change |
| `people_profiles.yaml` (plaintext) | **POLICY VIOLATION** | n/a — must be encrypted or deleted | immediate |
| `archive/<edition>/snapshots/issue_NNN.snapshot.json` | CONFIDENTIAL | indefinite (immutable audit-of-record) | `[EXCISED]` tombstone in metadata only (file is immutable) |
| `archive/<edition>/manifests/issue_NNN.json` | CONFIDENTIAL | indefinite | `[EXCISED]` tombstone in metadata only |
| `archive/<edition>/published/Issue_NNN.eml` | CONFIDENTIAL | indefinite | operator must redact + re-publish (no in-place edit) |
| `external_dependencies.jsonl` | CONFIDENTIAL | 1 year | n/a (no PII) |
| `narrative/*.md` | CONFIDENTIAL | 1 year | operator edit; no in-place automated scrub |
| `runtime/gather_state.json` | CONFIDENTIAL + PII (error messages with paths/identifiers) | 1 year | n/a (operator-side) |
| `migration_log.jsonl` | INTERNAL (no PII) | indefinite | n/a (no PII) |
| `_feedback/*.jsonl` (edit_patterns, brief_interventions, context_gaps) | CONFIDENTIAL + PII (operator edits) | 1 year | `[EXCISED]` tombstone (WS-18) |
| `runtime/vertex_analytics.sqlite3` | CONFIDENTIAL | 1 year | rebuild from journal after `[EXCISED]` run (WS-18) |
| `keyring entries` | SECRET | ephemeral (rotated on personnel change) | immediate rotation |

## 4. RBAC / consent matrix

Vertex uses **AAD user-context** for all operator-driven actions (gather, report, confirm)
and **AAD managed-identity** for unattended cron-style data plane reads.

| Capability | AAD model | Least-privilege role / scope | Source of consent |
|------------|-----------|------------------------------|-------------------|
| ADO read (work items, queries) | user-context | `vso.work_read` at the project scope | operator PAT issued by ADO admin |
| ADO write (PR comments, vote) | user-context | `vso.work_write` (only for confirm) | operator PAT (same as read) |
| Kusto read | managed identity | cluster `Viewer` at the program schema | cluster admin role assignment |
| Kusto write | n/a | n/a | n/a |
| IcM read | app-only | `IcMIncidentRead.All` | tenant admin consent |
| Teams read | delegated | `Channel.ReadBasic.All` | user-context (operator) |
| Teams write (Adaptive Card) | application | `ChannelMessage.Send` | tenant admin consent |
| WorkIQ | user-context | `Calendars.Read`, `Mail.Read` | user-context (operator) |
| SharePoint (LT deck / ref doc) | user-context | `Sites.Read.All` or `Files.Read.All` (minimum: share permission on target site) | user-context (operator); tenant admin may need to consent if `Sites.Read.All` is app-only |
| Transcript | application | `OnlineMeetings.Read`, `CallRecords.Read.All` | tenant admin consent |

## 5. Export boundaries

| Export target | Allowed classes | Required redaction | Audit |
|---------------|-----------------|--------------------|-------|
| Email (`.eml`) | PUBLIC, INTERNAL, CONFIDENTIAL | `process_generated_text` (ban-list); person name pseudonymization in body | `journal/actions.jsonl` entry with `recipient_hash` |
| Teams Adaptive Card | PUBLIC, INTERNAL, CONFIDENTIAL | no PII unless `consent.acquired: true` in card metadata | `journal/actions.jsonl` entry |
| ADO PR comment | PUBLIC, INTERNAL, CONFIDENTIAL | operator pre-flight review | `journal/actions.jsonl` entry |
| Published HTML | PUBLIC, INTERNAL, CONFIDENTIAL | ban-list applied | `journal/actions.jsonl` entry |
| Local CLI output | all classes (operator machine) | none | shell history (operator-side) |
| Remote log/metric sink (`doctor --diagnose` support bundle) | INTERNAL only | PII redacted (regex scrubber: email, name patterns) | bundle manifest + checksum |

## 6. Per-channel PII regression coverage

The contract `tests/contracts/test_privacy_matrix_contract.py` MUST enforce that for every
external channel, the extracted signals/observations do not contain:

- Unredacted email addresses in titles (unless the field is documented as `email_of`)
- Raw GUIDs that map to person records
- Free-form person names outside the `entity_refs` discipline
- Credential patterns (already covered by `tests/contracts/test_journal_privacy_contract.py`)

The matrix above is the spec; the contract test is the ratchet.

---

## Document control
| Field | Value |
|-------|-------|
| Tracked? | **YES** — `governance/privacy-matrix.md` |
| Source of truth (runtime) | `src/core/privacy_matrix.py` |
| Contract ratchet | `tests/contracts/test_privacy_matrix_contract.py` |
| Owner | DPO / Privacy reviewer `[HUMAN GATE]` |
| Next review | on classification change OR new channel onboarded |

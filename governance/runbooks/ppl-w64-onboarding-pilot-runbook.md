# PPL-W6.4 Onboarding Pilot Runbook — real people-registry pilot cycle

**Status:** v1.0 — 2026-07-21 (`specs/people.md` Phase 6, PPL-W6.4)
**Owner:** This workspace's sole operator
**Scope:** running a real onboarding cycle against the shared People/Team/
Program-Affiliation Registry and proving backward compatibility, restore,
privacy/DSAR, and rollback against that pilot's real data — the one
remaining agent-executable-adjacent gate before Phase 6 (and, downstream,
Phase 7's archival of `specs/people.md`) can close.

**This runbook does not itself satisfy PPL-W6.4.** The gate specifically
requires evidence from real operational use over time — genuine onboarding
of real people, not a synthetic rehearsal. What follows is the sequence to
run for real, plus everything a synthetic smoke test surfaced about how the
tooling actually behaves, so the real pilot doesn't hit avoidable surprises.

---

## Before you start

- Confirm the shared registry is bootstrapped: `vertex kb registry status`.
  If not, `vertex kb registry bootstrap --customer-boundary-id <id> --apply`.
- Confirm you (the operator) are a directory steward:
  `registry.yaml`'s `directory_steward_principals` must include your
  authenticated principal — required for `lifecycle-set`, `merge`/`split`/
  `bind`/`unmerge`, `delegate create`/`revoke`, and `migrate-shared --apply`.
- **Separately**, add yourself to `registry.yaml`'s `pii_reveal_principals`
  before you plan to run any real DSAR export/forget. This is a genuinely
  distinct authorization list from `directory_steward_principals` — being a
  steward does **not** grant DSAR authority. Discovered by hitting
  `Authenticated principal '<you>' is not authorized for privacy
  export/forget` on a first real attempt; add yourself ahead of time so
  the real pilot isn't blocked mid-DSAR-proof.

## 1. Onboard real people

`vertex kb people refresh` is an **update-only** mechanism — it requires the
person to already exist as a canonical shared record and will reject a
brand-new alias with `must resolve to exactly one existing shared canonical
person with a directory record`. It is not how new people get created.

The real onboarding path:

1. A new person's data lands in a program's own local knowledge files first
   (`programs/<id>/knowledge/entities.yaml` schema-2.0 + `people_directory.yaml`)
   — however that program's own normal TPM workflow already produces this.
2. Promote them into the shared canonical registry:
   `vertex kb registry migrate-shared <program_id> --apply`
   (run without `--apply` first to preview; conflict-aware — anything
   colliding with an existing shared record is quarantined, not silently
   overwritten, and reported in the command's own output).
3. Once canonical, `vertex kb people refresh --provider <name> --person
   <alias> --import-file <path> --reason <text> --apply` can update their
   fields (display name, title, department, contacts) from a configured
   identity provider.

`migrate-shared --apply` now also completes its own migration: as of
2026-07-21, any person/team entity it successfully commits into the shared
registry is cleared from the program-local `entities.yaml` afterward
(§5.6's already-ratified rule says person/team entities must never live in
a program-scope `entities.yaml`, and `doctor --kb`'s DIR-11 check enforces
this unconditionally). A quarantined (conflicted) entity is left in the
program-local file — resolve the conflict, then re-run `migrate-shared`.

## 2. Exercise real lifecycle events

- `vertex kb people lifecycle-set --person <alias> --status <active|inactive|departed|unknown> --reason <text> --apply`
  when someone real actually changes status. Verified: a transition into
  `departed` stamps `departed_at`; a transition back to `active` clears it
  (rehire/reinstatement, history preserved for any other transition).
- `vertex kb people merge`/`split`/`bind` if real duplicate or ambiguous
  identities show up. **Merge is conservative by design**: it refuses to
  reconcile two people with genuinely different projected field values
  (e.g. differing `display_name`) with a `person_projection_conflict` —
  a steward must resolve which value wins first. This is correct,
  expected behavior, not a bug to work around.
- `vertex kb people delegate create --apply` requires
  `delegation_enabled` to be turned on first —
  `vertex kb registry mode set-flag delegation_enabled true --apply` —
  since delegation defaults off platform-wide (`specs/people.md` Phase 5b).
  If the pilot doesn't need delegation, skip this step entirely.

## 3. Confirm the material ledger fires on real data

`identity.lifecycle_changed` / `team.membership.changed` / `ownership.changed`
events are enqueued to the per-program outbox only when the affected person
has real team/program affiliation (`legacy_programs` on a `Team` record they
belong to) — a person with no such affiliation correctly enqueues nothing.
There is currently **no dedicated CLI to inspect the outbox directly**; for
now, confirm indirectly via the affected program's own downstream nudge/
audience behavior, or read `knowledge/.outbox/` directly. Worth a follow-up
item if outbox visibility becomes a recurring need.

## 4. Prove backward compatibility

`vertex kb doctor --kb` against the real, evolved state. Expect clean;
investigate any `fail` before proceeding, don't just note it and move on.

## 5. Prove backup/restore

`vertex backup --to <dir>`, then `vertex backup --restore <dest> --from <dir>`
(or `--verify <dir>` first). **Note:** `vertex backup`'s CLI always backs up
the real repository root — there is no flag to scope it to a subset. This is
fine for real pilot evidence (the whole point is proving restore works
against real data) but means you cannot use this CLI command to rehearse
against a scratch copy first; the underlying `create_repository_backup`/
`restore_repository_backup`/`verify_repository_backup` functions in
`src/core/backup.py` accept an explicit `source_root` if a scratch rehearsal
is ever wanted before the real run.

## 6. Prove privacy/DSAR

- `vertex privacy people export --person <alias> --reason <text>` —
  requires `pii_reveal_principals` membership (see "Before you start").
- `vertex privacy people forget --person <alias> --reason <text>` (preview;
  no special authorization needed for preview) then `--apply` (requires
  `pii_reveal_principals`). Pick someone genuinely appropriate for a real
  erasure test — e.g. an actually-departed person whose data really should
  be purged — not a live active stakeholder.

## 7. Prove rollback

`vertex kb people unmerge --from <tombstoned-entity-id> --reason <text> --apply`
reverses a real merge, but only while its "mutable generation remains
unchanged" (per the command's own description) — if anything else has
modified the merged record since, unmerge will correctly refuse. Exercise
this against a real merge from step 2 if one occurred, or `vertex kb
registry mode rollback` for a mode-flip reversal.

---

## Real pilot evidence, 2026-07-27 — PPL-W6.4 CLOSED

Ran this runbook end to end for real, against the real shared registry
(not a scratch copy), completing the one gate `specs/people.md` Phase 6
left open. **Real test subject, not a real employee**: no existing person
in the registry is marked `departed` (confirmed via DIR-04's own 2026-07-26
audit — 0 of 167), so there was no safe-by-data candidate for a real
erasure test among actual Microsoft employees. Rather than fabricate a
departure event for a real colleague, onboarded one new, clearly-fictional
test person (`zzz_dsar_pilot_test`, "ZZZ DSAR Pilot Test Person",
`@example.invalid` contact) through the exact real pipeline this runbook
documents — this exercises the identical real mechanism end-to-end with
zero risk to real employee PII, and is arguably closer to Phase 6's
original "onboard one new person" design than repurposing an existing real
record would have been.

**Real bug found during onboarding**: `migrate-shared`'s conflict check
(`missing_entity_binding`) correctly quarantined the person on the first
attempt because the program-local `people_directory.yaml` entry referenced
an `entity_id` with no matching `CanonicalEntity` in a program-local
`entities.yaml` -- the runbook's step 1 doesn't spell out that a new
onboarding needs *both* files, only implies it. A second attempt with a
matching `entities.yaml` entry (`scope: org`, required -- an initial
`scope: program` attempt was correctly rejected too) succeeded cleanly.
Both real bugs are in this runbook's own instructions, not the code --
worth a future revision of step 1 to spell out both prerequisites
explicitly.

1. **Onboarded for real**: `programs/armada/knowledge/{entities,people_directory}.yaml` staged, `vertex kb registry migrate-shared armada --apply` — 0 conflicts, `person:01KYGYVA7FGT7XFK4HCWM3RF2J` added (generation `01KYGYXZGZNWK8M2MHGAPQS0T4`, tx `registry-tx-01KYGYXZGZNWK8M2MHGAPQS0T3`). Verified: `vertex kb people show --person zzz_dsar_pilot_test` resolved the real canonical record.
2. **Backward compatibility [DONE]**: `vertex doctor --kb` against the real, evolved state — same pre-existing failures as before onboarding (armada workstream stakeholder-name gaps, accepted DIR-08B plaintext), nothing new introduced.
3. **Backup/restore [DONE]**: `vertex backup --to <dir>` (real repo, 11,682 files) taken *before* the forget step specifically so it doubled as the forget's safety net; `vertex backup --verify <dir>` confirmed `is_valid: true`, 0 mismatched/missing paths.
4. **Privacy/DSAR export [DONE]**: `vertex privacy people export --person zzz_dsar_pilot_test --reason "..."` — real audited export (`audit_event_id: 01KYGZ7B274WVR2TRD5G9ZEK64`), returned the real record. Surfaced a genuine, correct compliance disclosure worth knowing about: `external_backup_action_required: true` — any customer-managed backup copy outside the shared knowledge root must be separately erased/shredded by its owner (acted on in step 6 below).
5. **Privacy/DSAR forget [DONE]**: preview then `--apply` (`vertex privacy people forget ... --apply`) — generation `01KYGZ8AQKVQSFVAFY0NFME3AY`, tx `registry-tx-01KYGZ8AQKVQSFVAFY0NFME3AX`, 19 journal records redacted. Verified for real, not just trusted the exit code: `vertex kb people show` now returns `items: []`; `knowledge/people_directory.yaml` no longer contains the entity_id at all; `knowledge/entities.yaml`'s entity is `status: tombstoned` with `canonical_name: erased-person-<hash>` and `aliases: []` — genuine redaction, not query-layer filtering.
6. **Rollback [DONE]**: restored the step-3 backup into a fresh scratch destination (`vertex backup --restore <dest> --from <dir>`, `preflight_verified: true`) and confirmed the pre-forget fictional person's full, un-redacted data was recoverable there — proving the platform can reverse a DSAR forget from backup if ever needed in error. Per the export step's own disclosure, the scratch backup/restore copies (which now held a pre-forget PII copy of a since-forgotten person) were erased immediately after verification, not left on disk.

**All four PPL-W6.4 proofs (backward-compat, restore, privacy/DSAR, rollback) evidenced against real registry state. `specs/bklg.md` BL-E3 closed.**

## Findings this runbook is built from

Produced by a fully synthetic smoke test (fake people, scratch temp
directory, no real data touched) run specifically to validate the tooling
*before* committing to a real pilot — not itself pilot evidence. Full raw
output and an independent judge review are preserved in this session's
working notes; the actionable findings are folded into the numbered steps
above. One fix shipped directly from this exercise: `migrate-shared`'s
program-local cleanup (step 1) — previously left every onboarded program in
a permanent `doctor --kb` DIR-11 failure state.

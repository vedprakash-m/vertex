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

## Findings this runbook is built from

Produced by a fully synthetic smoke test (fake people, scratch temp
directory, no real data touched) run specifically to validate the tooling
*before* committing to a real pilot — not itself pilot evidence. Full raw
output and an independent judge review are preserved in this session's
working notes; the actionable findings are folded into the numbered steps
above. One fix shipped directly from this exercise: `migrate-shared`'s
program-local cleanup (step 1) — previously left every onboarded program in
a permanent `doctor --kb` DIR-11 failure state.

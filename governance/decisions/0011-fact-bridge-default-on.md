# ADR-0011: Ledger fact-bridge default flipped to enabled

**Status:** Accepted
**Date:** 2026-07-07
**Decider:** Vertex engineering (product/operator sign-off recorded this session)
**Companion:** `specs/fix-data-flow.md` §6.1 / §7 PR-6; PS-2; `src/core/models_v2.py`; `src/core/edition_resolver.py`; `scripts/verify_activation.py`; `tests/contracts/test_fleet_isolation.py`

## Context

PS-2 (`specs/fix-data-flow.md`) identified that `fact_bridge_enabled` defaulted
to `False`, and that neither `vertex onboard` nor `vertex doctor` flagged its
absence. A new REV-configured program that ran gather → REV → triage →
approve without discovering and setting this one YAML key would see triage
approvals persist to the ledger with **zero facts ever reaching
`ProgramFactStore`** — silently, with no operator-visible signal.

Track A step 1 (this session, prior to this ADR) closed the *visibility* half
of this gap without changing the default:
- `vertex doctor --fact-bridge` now proactively WARNs for any REV-configured
  program whose bridge resolves disabled (`run_bridge_disabled_doctor`,
  `src/commands/doctor_checks/fact_store_flip_checks.py`).
- A reactive stderr warning now fires the moment a bridgeable event is
  actually silenced by a disabled bridge
  (`_warn_if_bridgeable_event_silenced_by_disabled_bridge`, `src/commands/ledger.py`).
- The previously-silent `PASSTHROUGH` disposition branch now logs at `debug`.
- A durable `bridge_failures.jsonl` backlog + `run_bridge_failure_backlog_doctor`
  check surfaces a bridge that is enabled but persistently failing.

Step 2 — flipping the *default* itself — was deliberately sequenced after step
1 (§6.1's own staged design) so the fail-loud signals would exist before
changing behavior. Per this session's explicit operator decision, step 2 is
being taken now, in the same session as step 1, rather than waiting for a
separate observation window: the fail-loud signals above are judged
sufficient justification, and per Assumption A2, flipping the default is
strictly additive (bridge failures were already non-fatal by
`EventDisposition` design before this change).

## Decision

1. **`RevRetrievalProfile.fact_bridge_enabled`'s dataclass default** (`src/core/models_v2.py`)
   changes from `False` to `True`.
2. **`_parse_rev_profile`'s YAML-parsing default** (`src/core/edition_resolver.py`)
   changes from `bool(value.get("fact_bridge_enabled", False))` to
   `bool(value.get("fact_bridge_enabled", True))` — the same default change,
   applied at the actual config-loading site.
3. **Explicit opt-outs are preserved and still win.** Any program that sets
   `fact_bridge_enabled: false` in `program.yaml`, or that has
   `VERTEX_LEDGER_FACT_BRIDGE` unset while explicitly configured false, is
   unaffected — this ADR changes the *default* only, not the resolution
   order (`_ledger_fact_bridge_enabled`'s env-var-then-program-config
   precedence in `src/commands/ledger.py` is unchanged).
4. **`scripts/verify_activation.py`'s `PS-2-BRIDGE-DEFAULT` self-check**
   updated to assert the new expected baseline (bridge defaults on), not the
   old off-by-default baseline it previously asserted as correct.
5. **Multi-program bridge-appender isolation test** added to
   `tests/contracts/test_fleet_isolation.py`
   (`test_bridge_appender_isolates_facts_between_two_concurrent_programs`) —
   runs the real `append_bridged_milestone_event` appender for two distinct
   programs against a shared `db_root` and asserts each program's
   `ProgramFactStore.snapshot()` contains only its own facts. This directly
   addresses R9 (a `program_id`-filtering bug in an appender would now leak
   across every REV-configured program, not just one explicitly opted-in
   program, once the default is flipped for the whole fleet).
6. **Invalid-SoR-mode-string regression coverage** (Assumption A7) — already
   correct behavior (`fact_sor_state.py` already raises `ConfigError` for an
   out-of-range mode string), now locked in by
   `tests/unit/test_ledger_fact_bridge_fail_loud.py`'s
   `test_load_fact_sor_state_rejects_invalid_program_level_mode` /
   `..._family_mode_string`.

## Non-fatal-disposition reasoning (Assumption A2)

Flipping the default does not introduce a new failure mode: bridge appender
exceptions were already caught and logged at `ERROR` without crashing the
write path (`_maybe_bridge_event_to_fact_store`'s existing `except Exception`
handler, `src/commands/ledger.py`), and `PASSTHROUGH`/`KNOWN_UNPROJECTEABLE`
dispositions already degrade gracefully. The default flip only changes
*whether facts get written at all* for programs that never touched this key —
strictly additive, not a new risk surface.

## Consequences

- **+** A new REV-configured program that never sets `fact_bridge_enabled`
  now has its approved facts reach `ProgramFactStore` automatically — closing
  PS-2's silent-onboarding gap at the source, not just via a visible warning.
- **+** Existing programs that explicitly set the key (either value) are
  completely unaffected.
- **+** The isolation test added alongside this change directly guards the
  wider blast radius this flip introduces (every REV-configured program is
  now bridging by default, not one explicitly opted-in program).
- **−** Any program relying on the old silent off-by-default behavior without
  having explicitly set `fact_bridge_enabled: false` will see a behavior
  change: facts it previously never wrote will now start appearing in
  `ProgramFactStore` (this is the intended fix, not a regression — see PS-2).
- **−** This session did not run a full `vertex gather` → `vertex rev run` →
  triage → approve cycle against a live disposable test program with the new
  default (Assumption A2's full validation recipe) before merging — the
  existing bridge test suite (`tests/contracts/test_ledger_fact_bridge.py`,
  ~50 tests) plus this session's new fail-loud and isolation tests are relied
  on as the pre-merge evidence instead. Running the full live-cycle
  validation remains a recommended follow-up, not a blocker, given the
  non-fatal-disposition reasoning above.

## References

- `specs/fix-data-flow.md` PS-2, §6.1, §7 PR-5/PR-6, Assumption A2/A5/A9, Risk R9.
- ADR-0010 (incremental projection rebuild on write) — the companion Track D
  decision landing in the same session.

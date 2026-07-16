# ADF Agent Operating Contract

**Source:** `specs/arch-data-fix.md` Appendix D (copied verbatim per
ADR-0012 / ADF-W0.1, so a fresh clone has the binding operating
constraints even without the gitignored spec file).
**Status:** Binding for every implementation agent (and human) executing
`specs/arch-data-fix.md` Section 11 work.
**Last synced from spec:** 2026-07-13 (spec v1.4, Appendix D).

If `specs/arch-data-fix.md` Appendix D changes, update this file in the
same change (per ADR-0012's copy-forward decision).

---

**Environment.** Windows 11; the repository lives on the `Q:` mapped
network drive (SMB) with a documented hang/latency pathology. Never add
per-test `Q:` I/O: the test suite primes a local `C:` cache
(`tests/conftest.py::_prime_local_repo_cache`,
`tests/support/report_test_setup.py`); seed helpers must resolve paths
through `get_source_root(repo_root)`, not bare `repo_root`.

**Python and CI.** Local development runs Python 3.13 with program data
present. CI runs a fresh `git clone` with Python 3.11 and a fresh
`pip install` — no program data, so data-dependent skip predicates
behave differently. Reproduce CI failures via a temp clone, never by
assuming local state.

**Tests.** Targeted runs: `python -m pytest tests/unit/<file> -q`,
`tests/contracts/<file> -q`, goldens under `tests/golden/`. Before
marking any brief done, additionally run
`tests/contracts/test_import_boundaries.py` and
`tests/contracts/test_architecture_fitness.py`. Line-budget ceilings are
enforced by architecture-fitness tests; fit within existing budgets or
change the budget deliberately in the same change with justification.

**Live-state guardrails (hard rules).** Never run mutating commands
(`vertex report` non-dry, `confirm`, `nudge --mark-sent`,
`facts flip|pin-snapshot`, `admin baseline`, `ado apply`) against live
`programs/<id>` state during implementation without explicit operator
instruction — a prior incident overwrote live overrides. Use fixtures,
scratch programs, and `--dry-run`. The baseline hardlock
(`vertex admin baseline --lock`) protects trusted issues; never unlock it
to make a test pass. `arch_data_fix.actuation.enabled` defaults to
`false`; tests use stub provider clients, never live ADO.

**Data hygiene.** `programs/`, `golden/`, `fixtures/`, `.archive/`, and
non-canonical `specs/*` are gitignored local-only: tests and tracked code
must not depend on their contents, and no personal identifiers may enter
tracked files.

**State discipline.** Append-only sidecars go through
`src/core/jsonl_utils.py` (locking, rotation, quarantine). New
authoritative events go only through the ledger per Section 9.6. Zone
boundaries per INV-ADF-17 are contract-tested; `src/core` must not import
`src.ai`, `src.m365`, or `src.commands`.

**Completion discipline.** A brief is done only when: all steps are
implemented, its tests exist and pass, the done-check command output is
recorded, rollback/kill-switch evidence is noted (Section 3.6.3), and no
unrelated files were modified. Scaffolding, TODOs, or skipped tests do
not count as done (operating principle, Section 3.5).

## References

- `specs/arch-data-fix.md` Appendix D (source of truth for future edits;
  this file must be re-synced whenever that appendix changes)
- `governance/decisions/0012-arch-data-fix-governance-tracking.md`
  (ADR-0012, the decision that created this file)

# Test Evidence Log

This is the **canonical location** for full-suite execution evidence. Local
artifacts like `output/__green_run.txt` and `output/__full_suite_v161.txt` are
git-ignored ephemeral runs and **must not be cited** in TRACKED specs
(see `scripts/check_spec_drift.py` `p9-dead-green-run`).

## How to record evidence

1. Run the suite (locally or in CI).
2. Append one line to the table below with: `date`, `runner` (local/CI), the
   exact command (e.g. `pytest tests/ -q`), the raw result line
   (e.g. `2504 passed, 15 skipped in 7254.28s`), and a tag (e.g. `pre-UIL`,
   `post-WS-1`, `release-vX.Y.Z`).
3. Commit the new line — never edit a previous line.

## Table

| Date | Runner | Command | Result | Tag |
|------|--------|---------|--------|-----|
| 2026-06-09 | local (session observation) | `pytest tests/contracts/ -q` | 437 passed, 6 skipped (full contract suite, WS-1..WS-25 complete) | post-WS-25 |
| 2026-11-18 | GitHub Actions run `44260791657` at `f1f335006034077e212fbba1a9312a732a8ebe59` | 12 disjoint functional shards, 6 workers each; 8 bounded perf nodes; 19 Windows checks; full `src/` + `cli.py` statement/branch coverage | 27,903 passed, 39 skipped, 31 subtests passed; aggregate 27,969 cases; 127,150/144,640 statements (87.91%) and 37,953/50,550 branches (75.08%); required result 5m47s | test-ci-optimization |
| _next_ | | `pytest tests/ -q` | _run full suite and append_ | _append rows above this line_ |

## Why this file

The PRD and Tech spec used to point at `output/__green_run.txt` directly. That
artifact is:
- git-ignored (line `output/` in `.gitignore`),
- local to a single developer's machine,
- not reproducible from a fresh clone (the spec's §0.3 rule).

This file replaces the dead pointer with a tracked, append-only log. Suite
**collection counts** (vs. passed counts) live in `scripts/derive_spec_counts.py`
output and are referenced by spec sections that need a current number; never
hardcode a count in a TRACKED spec.

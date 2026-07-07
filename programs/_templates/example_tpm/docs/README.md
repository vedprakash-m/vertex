# docs/ — one-time human documents

This directory holds **one-time human-authored documents** for this program:
decision records, analysis run logs, posture notes, and outreach packets that
record *why* a decision was made or *what* a one-off investigation found.

## What belongs here (T-8)

- Decision packets (e.g. `qg24_decision_packet_<date>.md`)
- Posture / source-of-record decision notes (e.g. `sor_posture_decision.md`)
- Onboarding run logs (e.g. `onboard_run_log.md`)
- One-off analysis or validation notes

## What does NOT belong here

`docs/` is for human documents only. Do **not** drop platform artifacts here —
`vertex doctor` (DC-03) warns when a `docs/` file matches a platform filename
pattern (`*.jsonl`, `*_state.*`, `*_registry.*`). Platform-internal runtime
files belong in `runtime/`, append-only logs belong in `journal/`, and rolling
summaries belong in `summaries/`.

See `specs/declutter.md` §5 for the full program-directory taxonomy.
# Activation Raw-Data Feasibility

**Scope:** P-1 / §6.15.1 for XPF activation.

**Current verifier state:** the default keystone family (`milestone.completed`) has 29 reachable milestone-dominant source documents against the activation floor of 30. It is close, but still below the floor and must remain red until an additional reachable real EML is acquired or the keystone is re-selected.

| Claim family | Bound accessor | Reachable real docs | Activation floor | Decision |
|---|---|---:|---:|---|
| `milestone.completed` | `milestones()` | 29 | 30 | Acquire at least 1 more reachable EML before activation proof |
| `deployment.completed` | `milestones()` | 29 | 30 | Shared accessor; does not independently solve the floor |
| `commitment.date_set` | `commitments()` | 29 | 30 | Viable fallback only after fresh annotation |
| `ownership.changed` | `workstreams()` | 8 | 30 | Not viable for first slice without substantial acquisition |

**Acquisition owner:** activation-tpm

**Acquisition path:** manual EML export into the REV drop folder, then `vertex gather` / REV ingestion.

**Go/no-go rule:** do not mark `P-1-RAW-DATA` green until the live verifier reports `reachable_document_count >= 30` for the selected keystone family. If no family reaches the floor, raw-data acquisition precedes any further authority-promotion work.

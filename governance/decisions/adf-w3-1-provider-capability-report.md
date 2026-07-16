# ADF-W3.1 Structured-Provider Capability Report

**Status:** Ratified (reconciles a decision already made and implemented in an
earlier session against `specs/arch-data-fix.md`'s newer Slice-3 work item).

`specs/arch-data-fix.md` ADF-W3.1 asks for "a structured-provider reachability
spike and implementation of the available path without blocking local
import." That spike already happened, and the available path is already
implemented — this document is the formal capability report the work item's
own acceptance evidence names, written to close the gap between that
already-shipped work and this spec's Section 11.4a status table.

## Providers evaluated

| Provider path | Scopes/mechanism | Status | Evidence |
|---|---|---|---|
| Direct Microsoft Graph delegated API | `Mail.Read`, `Calendars.Read`, `Chat.Read`, `Files.Read.All`, `Sites.Read.All` via `DeviceCodeCredential` | **Permanently blocked** by Microsoft IT policy, confirmed 2026-06-24; no approval path exists | `docs/adrs/adr-008-graph-api-pivot.md` |
| WorkIQ MCP surface | `AgencyBridge.ask_workiq`/`invoke_mcp_tool` (subprocess wrapper around the `agency` CLI, MCP tool allowlist) | **Live and already wired** — real production callers in `src/commands/gather.py`'s M365 discovery stage (`run_m365_discovery_stage`) via `src/m365/provider_facade.py`; also the mechanism behind `src/m365/transcript_reader.py` and `src/m365/teams_reader.py` (meeting-transcript and Teams retrieval specifically) | `src/m365/agency_bridge.py`, `src/m365/graph_mail_client.py`, `src/m365/graph_calendar_client.py`, `src/m365/transcript_reader.py` |
| Local file export import (`.eml`/`.ics`) | No API, no credentials, no consent, no expiry — user exports from Outlook/OWA into `programs/<id>/inbox/` | **Live and already wired** — `vertex rev run --eml-inbox <dir>` / `--ics-inbox <dir>`; 3-directory atomicity (`inbox/ -> claimed/ -> processed/`) survives process interruption | `src/commands/rev.py`, `src/m365/rev/eml_enumerator.py`, `src/m365/rev/eml_hydrator.py`, `src/m365/rev/ics_enumerator.py`, `src/m365/rev/ics_hydrator.py` |
| Local file export import (`.docx`/`.pdf`) | Same local-export pattern | **Built, not CLI-wired** — `src/m365/rev/local_file_enumerator.py` exists (Phase 3/P3-5) but `rev.py` has no `--doc-inbox`-style flag exposing it yet | `src/m365/rev/local_file_enumerator.py` |
| Local Teams-JSON export import | Same local-export pattern | **Not built** — no code path reads a locally-exported Teams JSON transcript; Teams content today only flows through the live WorkIQ MCP surface above | (absence confirmed by search) |

## Decision

**Selected path for Slice 3 ("Meeting to Action and Solicitation"): local
`.eml`/`.ics` import via the REV pipeline (`vertex rev run`) is the primary,
credential-free path, and does not block on the WorkIQ MCP surface's
availability.** `vertex rev run --eml-inbox <dir>` is fully self-contained —
it does not call `AgencyBridge`, does not require live network access, and
therefore is never blocked by the direct-Graph permanent block above. This
satisfies the work item's "without blocking local import" requirement
literally: local import already does not block on anything live.

The WorkIQ MCP surface remains available as a secondary, already-wired live
enrichment path (used today by `gather.py`'s M365 discovery stage and by
`transcript_reader.py`/`teams_reader.py` for meeting/Teams content
specifically) for programs where it is configured and reachable. It is not a
prerequisite for Slice 3's golden workflow.

## What this closes vs. what remains open

This report closes the "capability report and selected provider path"
acceptance evidence ADF-W3.1 asks for. It does **not** claim the rest of
Slice 3 is done — see `specs/arch-data-fix.md` Section 11.4a for the
per-item status of ADF-W3.2 through ADF-W3.7, none of which this report
addresses.

## References

- `specs/arch-data-fix.md` Section 11.4 (ADF-W3.1), Section 8.10.4 (action
  schema Slice 3 ultimately needs to produce)
- `docs/adrs/adr-008-graph-api-pivot.md`
- `src/commands/rev.py::P0_SPIKE_NOTE` (the same finding already stated
  inline in code)

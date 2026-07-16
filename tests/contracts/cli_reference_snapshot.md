# Vertex CLI Reference

Generated from the live Typer command tree. Do not edit manually.
Regenerate with `./.venv/Scripts/python.exe scripts/generate_cli_reference.py`.

Installed entry points: `vertex`, `vx`.

## `vertex`

**Usage:** `vertex [OPTIONS] COMMAND [ARGS]...`

Vertex hybrid journal automation CLI.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --skip-issue | boolean | No | False | Record the next issue as intentionally skipped. |
| --reason TEXT | text | No |  | Required with --skip-issue. |
| --no-catchup | boolean | No | False | Skip the session-start catchup scan for this invocation. |
| --edition TEXT | text | No |  | Default edition for natural-language routing (e.g. 'acme_weekly'). |
| --install-completion | boolean | No |  | Install completion for the current shell. |
| --show-completion | boolean | No |  | Show completion for the current shell, to copy it or customize the installation. |

**Subcommands**

| Command | Description |
|---|---|
| `context-diff` | Query Plane 1 configuration changes for a program. |
| `archive-journals` |  |
| `archive-verify` | Verify the HMAC signature sidecar for an archived manifest. |
| `ask` |  |
| `apply-proposals` |  |
| `apply-overrides` |  |
| `backup` |  |
| `backfill` |  |
| `bootstrap` |  |
| `bridge-status` |  |
| `brief` |  |
| `catchup` |  |
| `capture-lt-deck` | Validate and write an LT deck snapshot (γ-Read Phase 3). |
| `confirm` |  |
| `deck-companion` |  |
| `diff` |  |
| `doctor` |  |
| `edit` |  |
| `enrich` | Extract structured evidence from emails and transcripts via WorkIQ. |
| `escalate` |  |
| `evidence` |  |
| `freshness` |  |
| `fleet` |  |
| `gather` |  |
| `prefetch` |  |
| `history` |  |
| `investigate` |  |
| `ingest-update` | Parse a compact-update EML and stage ContextUpdateProposals (γ-Read Phase 3). |
| `manifest` |  |
| `meeting-close` |  |
| `maturity-check` |  |
| `migrate` |  |
| `notify` |  |
| `nudge` |  |
| `next` | Print up to 3 ranked CLI suggestions for the next step on the given edition. |
| `onboard` |  |
| `setup` | Conversational, AI-assisted setup for a new Vertex program. |
| `owner-pack` |  |
| `override` |  |
| `prep` |  |
| `decision-brief` |  |
| `propose` |  |
| `published-baseline` |  |
| `rollback` | Restore a program's mutable stores to a named checkpoint. |
| `review-proposals` |  |
| `publish-gate` |  |
| `draft` |  |
| `reconcile` |  |
| `report` |  |
| `review-debrief` |  |
| `review-full` |  |
| `probe-ado` |  |
| `status` |  |
| `summarize` |  |
| `synthesize` |  |
| `triage` |  |
| `trust` |  |
| `trust-bootstrap` | Apply cold-start trust grants from trust_policy.yaml to a program. |
| `hints` | Generate and interactively manage narrative delta hints. |
| `vitality` |  |
| `watch` |  |
| `list` | List configured Vertex resources. |
| `actions` | List and review extracted actions. |
| `actuate` | Governed actuation — review proposals and execute approved ones. |
| `ado` | ADO diagnostics and update workflows. |
| `ai-proposals` | Review AI-generated proposals: risk, meeting action, top-three, governance decision brief, dependency blast radius. |
| `admin` | Vertex operator and debug commands. |
| `assertion` | Author telemetry assertions for L1 reality evaluation. |
| `assumptions` | Manage the program assumptions register. |
| `claims` | List and resolve tracked claims and decision asks. |
| `cockpit` | Program/platform/economics/value cockpit (read-only projection). |
| `calibration` | Inspect historical claim calibration. |
| `commitment` | Manage program commitments (inbound/outbound). |
| `context` | NCFL context proposal extraction and review. |
| `config` | Inspect and update governed program configuration. |
| `decision-brief-pilot` | ADF-W2.9 P5: blind A/B comparison of decision-brief-advisor's ContextCompiler/AISchemaGateway-wired pilot path against the current baseline. |
| `program-synthesizer-pilot` | ADF-W2.9: blind A/B comparison of program_synthesizer's ContextCompiler/AISchemaGateway-wired pilot path against the current baseline. |
| `connectors` | External connector management (FR-SG-48). |
| `audit` | Inspect audit history and autonomy governance state. |
| `decisions` | Manage the program decision register. |
| `dependencies` | Inspect and manage inferred dependency proposals. |
| `discover` | Discovery pipeline orchestration commands. |
| `editor` | Editorial evaluation commands. |
| `entity-aliases` | Inspect unresolved entity aliases in a program's fact store. |
| `facts` | Manage program fact store (export, import, rebuild). |
| `kb` | Knowledge base diagnostics and history. |
| `knowledge` | Knowledge plane authoring and inspection commands. |
| `ledger` | Manage append-only program ledger state. |
| `inspect` | Inspect runtime state for deterministic command surfaces. |
| `integration` | Inspect and manage the unified integration registry. |
| `index` | Manage the local semantic archive index. |
| `milestones` | Manage milestone health and authored milestone data. |
| `hypothesis` | Manage L1 reality hypotheses. |
| `observation` | Inject manual telemetry observations into L1 reality state. |
| `policy` | Promote governed policy proposals into active local rules. |
| `privacy` | Privacy & data governance matrix (WS-15). |
| `observability` | SRE-grade observability: failure diagnosis, per-channel perf, support bundle. |
| `alerts` | Between-runs alert management (WS-17). |
| `readiness` | Manage launch readiness snapshots. |
| `registry` | Inspect M365 registry state. |
| `reality` | Inspect and act on L1 reality state. |
| `review-sections` | Manage per-section review status for the active issue. |
| `rev` | Program-Context Intelligence (REV) retrieval + verification. |
| `risks` | Manage the program risk register. |
| `salience` | Inspect author salience feedback state. |
| `signals` | List and review journal signals. |
| `storage` | Inspect and validate Vertex storage (read-only). |

### `vertex context-diff`

**Usage:** `vertex context-diff [OPTIONS]`

Query Plane 1 configuration changes for a program.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition id (e.g. acme_weekly). Required. |
| --since-issue INTEGER | integer | No |  | Show changes since confirmed issue N was published. |
| --since TEXT | text | No |  | Show changes since ISO date YYYY-MM-DD. |
| --between-start INTEGER | integer | No |  | Start issue for --between range. |
| --between INTEGER | integer | No |  | End issue for --between range (inclusive). Use with --between-start. |
| --format TEXT | text | No | text | Output format: text (default) or json. |
| --programs-root PATH | path | No |  |  |

### `vertex archive-journals`

**Usage:** `vertex archive-journals [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --before TEXT | text | No |  | Archive weekly journal files before YYYY-Www. |
| --retention | boolean | No | False | Archive weekly journal files that are fully past the configured retention policy. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex archive-verify`

**Usage:** `vertex archive-verify [OPTIONS]`

Verify the HMAC signature sidecar for an archived manifest.

Exits 0 iff the sidecar exists AND the HMAC tag matches a
re-canonicalized hash of the on-disk manifest. Exits 2 on signature
mismatch (the auditor should investigate). Exits 1 on missing
sidecar (legacy / pre-signing archive, or signing skipped).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition name (e.g. myprogram_weekly). |
| --issue INTEGER RANGE | integer range | No | 0 | Confirmed issue number to verify (e.g. 78). |
| --keyring-user TEXT | text | No | primary | Keyring username to verify with. |

### `vertex ask`

**Usage:** `vertex ask [OPTIONS] REQUEST`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Default edition for routed command. |
| --program TEXT | text | No |  | Program id. Enables named-intent answering. |
| --cluster-misses | boolean | No | False | Cluster unroutable questions from the miss log and propose new intent routes. |

### `vertex apply-proposals`

**Usage:** `vertex apply-proposals [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --issue INTEGER | integer | No |  | Issue number to update. Defaults to the latest issue with proposals. |
| --accept TEXT | text | No |  | Section ids to accept. |
| --accept-modified TEXT | text | No |  | Section edits to accept as <section_id>=<text>. |
| --reject TEXT | text | No |  | Section ids to reject. |
| --accept-all | boolean | No | False | Accept all pending proposals. |
| --interactive | boolean | No | False | Prompt through pending proposals one section at a time. |
| --undo | boolean | No | False | Restore the most recent narrative backup for the issue. |
| --yes | boolean | No | False | Skip confirmation prompts for --undo and --accept-all. |
| --dry-run | boolean | No | False | Preview apply/reject actions without mutating files or proposal status. |

### `vertex apply-overrides`

**Usage:** `vertex apply-overrides [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, for example acme_weekly. |

### `vertex backup`

**Usage:** `vertex backup [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --to PATH | path | No |  | Destination directory for a backup snapshot. |
| --verify PATH | path | No |  | Existing backup directory to verify. |
| --restore PATH | path | No |  | WS-23: restore a backup snapshot to a destination. Pass the destination directory; --from specifies the backup root. |
| --from PATH | path | No |  | WS-23: backup directory to restore from (used with --restore). |
| --skip-preflight | boolean | No | False | WS-23: skip the backup verify step before restore. Off by default; only the clean-machine drill may override. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex backfill`

**Usage:** `vertex backfill [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition used for the backfill run (e.g. myprogram_weekly). |
| --source TEXT | text | No | auto | Backfill source mode: auto, offline, m365, or hybrid. |
| --since TEXT | text | No |  | Optional ISO date filter (YYYY-MM-DD) for source discovery. |
| --dry-run | boolean | No | False | Preview discovered backfill sources without writing summary artifacts. |

### `vertex bootstrap`

**Usage:** `vertex bootstrap [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --dry-run | boolean | No | False | Preview bootstrap proposals without writing them. |
| --limit INTEGER RANGE | integer range | No | 500 | Maximum number of proposals to seed. |

### `vertex bridge-status`

**Usage:** `vertex bridge-status [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition id, e.g. myprogram_weekly. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |
| --graduate | boolean | No | False | Mark the bridge as graduated when exit criteria are met. |
| --yes | boolean | No | False | Skip the graduation confirmation prompt. |
| --export-metrics | boolean | No | False | Write current bridge metrics to publications/<edition>/bridge_metrics.json. |

### `vertex brief`

**Usage:** `vertex brief [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --today | boolean | No | False | Use the current session-scoped local state surfaces. |
| --approve TEXT | text | No |  | Approve a staged intervention id from the brief output. |
| --dismiss TEXT | text | No |  | Dismiss a staged intervention id from the brief output. |
| --dry-run | boolean | No | False | Render to stdout without writing the brief artifact. |

### `vertex catchup`

**Usage:** `vertex catchup [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --since TEXT | text | No |  | Numeric hours for L0 catchup, or ISO date/time for L1 reality catch-up. |
| --no-scan | boolean | No | False | Show the cached last catchup result without scanning ADO again. |
| --source TEXT | text | No | ado | Signal source to scan. Repeat or use comma-separated values: ado, workiq, kusto, analytics, sprints, icm. |
| --interactive | boolean | No | False | For ISO-date L1 catch-up, prompt once to acknowledge resolved staleness items. |
| --notify | boolean | No | False | Emit a terminal bell after successful catchup output. |
| --reason TEXT | text | No |  | Optional note recorded with L1 catch-up audit events. |

### `vertex capture-lt-deck`

**Usage:** `vertex capture-lt-deck [OPTIONS]`

Validate and write an LT deck snapshot (γ-Read Phase 3).

The snapshot is used by QG-ED-LT to check LT deck freshness.
Run vertex doctor --context to see the LT deck freshness status.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name. |
| --file PATH | path | Yes |  | Path to the lt_deck_snapshot.yaml to validate and write. |
| --dry-run | boolean | No | False | Validate only; do not write. |

### `vertex confirm`

**Usage:** `vertex confirm [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --issue INTEGER | integer | No |  | Issue number to confirm. Defaults to next issue after archive index. |
| --dry-run | boolean | No | False | Validate and show what would be confirmed without archive writes. |
| --force | boolean | No | False | Override forceable gates such as freshness while keeping hard blocks enforced. |
| --ack-forecast | boolean | No | False | Acknowledge an enabled forecast before confirming. |
| --ack-stale-approval | boolean | No | False | Acknowledge a stale approval after reviewing the updated ADO data. |
| --untrusted | boolean | No | False | Archive the issue without advancing the trusted continuity baseline. |
| --reason TEXT | text | No |  | Reason for confirming with --untrusted. |
| --legacy-regex-extractor | boolean | No | False | Force the legacy regex claim extractor instead of the AI claim extractor. |
| --post-weekly-summary-card | boolean | No | False | After a successful confirm, write and post the weekly summary Adaptive Card to Teams. |
| --skip-ncfl | boolean | No | False | Skip best-effort NCFL proposal extraction after confirm. |

### `vertex deck-companion`

**Usage:** `vertex deck-companion [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --issue INTEGER | integer | No |  | Issue number to render. Defaults to the active issue. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex diff`

**Usage:** `vertex diff [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition name. |
| --issue INTEGER | integer | No |  | Issue number. Defaults to the latest draft issue. |
| --since TEXT | text | No | last-draft | Comparison point: last-draft, last-confirmed, or issue-N. |
| --section TEXT | text | No |  | Optional section id or dimension name to diff. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex doctor`

**Usage:** `vertex doctor [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition name. Uses the only configured edition when omitted. |
| --fix | boolean | No | False | Auto-create missing overrides.yaml when possible. |
| --check-auth | boolean | No | False | Validate ADO auth reachability/token age, Graph send prerequisites, and Agency CLI availability. |
| --operator-gates | boolean | No | False | Summarize the remaining PM/operator gates with live evidence, next commands, and explicit operator-vs-LLM responsibilities. |
| --platform-readiness | boolean | No | False | Measure fleet-scoped P4/P5/V-11 readiness from provable repo signals and mark unrecorded proof criteria as UNPROVEN. |
| --kb | boolean | No | False | Validate knowledge, program, and edition referential integrity. |
| --kb-check-origins | boolean | No | False | With --kb, compare current origin files against stored knowledge vault hashes to detect stale ingested copies. |
| --context | boolean | No | False | Validate cross-file program context invariants (§5) and staleness policy (§8). |
| --ids | boolean | No | False | Validate scorecard, chapter, slice, registry, and workstream ID consistency. |
| --cadence | boolean | No | False | Validate communication-plan cadence against recent confirmation history. |
| --channels | boolean | No | False | Inspect gather channel completeness, active flags, and transcript coverage telemetry. |
| --privacy | boolean | No | False | Scan journal files for credential patterns and verify people_profiles.yaml encryption state. |
| --kusto | boolean | No | False | Validate applicable Kusto query definitions and probe live reachability. |
| --milestones | boolean | No | False | Validate milestones.yaml schema, workstream links, and owner aliases. |
| --dependencies | boolean | No | False | Validate dependencies.yaml schema, references, cycles, and legacy fallback state. |
| --actions | boolean | No | False | Validate actions.jsonl schema, references, and overdue actions. |
| --risks | boolean | No | False | Validate risk_register.yaml schema, references, and stale review dates. |
| --escalations | boolean | No | False | Validate escalation_rules.yaml schema and escalation_state.json cooldown state. |
| --decisions | boolean | No | False | Validate decisions.yaml schema, references, and stale proposed decisions. |
| --assumptions | boolean | No | False | Validate assumptions.yaml schema, references, and overdue validation dates. |
| --readiness | boolean | No | False | Validate readiness.yaml presence and readiness_snapshot.yaml freshness/integrity. |
| --semantic-index | boolean | No | False | Validate semantic index freshness, dirty state, and optimization health. |
| --personas | boolean | No | False | Validate personas.yaml schema, check hygiene, minimum density, staleness, and re2 availability. |
| --metric-bindings | boolean | No | False | Validate L1 metric-binding readiness, revalidate stale bindings, and flag validation drift. |
| --consistency | boolean | No | False | Validate trusted baseline, confirmed archive, and review-state issue alignment. |
| --checkpoints | boolean | No | False | Validate checkpoint inventory and whether the latest checkpoint covers the mutable program stores needed for rollback. |
| --storage | boolean | No | False | Validate journal retention posture, trajectory footprint, and SQLite storage health. |
| --flip-status | boolean | No | False | Report the current Fact Store source-of-record posture for the resolved edition (legacy, dual, or fact-store). |
| --flip-parity | boolean | No | False | Compare legacy mutable-state projections against Fact Store projections for one confirmed issue. |
| --fact-parity | boolean | No | False | Check whether enough dual-read parity cycles have been logged for the resolved program (reads fact_store.dual_read_cycles from platform_state.yaml, default 5). |
| --fact-bridge | boolean | No | False | Check the ledger->fact-store bridge posture: whether it is enabled for a REV-configured program, and whether a persistent bridge-failure backlog exists (fix-data-flow.md Track A / PS-2). |
| --fact-deserialization | boolean | No | False | Confirm existing persisted facts still deserialize against the current schema, not just newly-bridged ones (fix-data-flow.md Track L). |
| --confirm-readiness | boolean | No | False | Enumerate exact live blockers that would prevent a non-forced confirm. Returns 0 only when confirm would succeed. |
| --adapter-cert | boolean | No | False | Audit UIL adapter certification per WS-3: checks which channels are enabled/certified and probes WorkIQ verb availability. |
| --issue INTEGER | integer | No |  | Issue number required by --flip-parity. |
| --charts | boolean | No | False | Validate chart cache TTL vs edition cadence, attachment targets, exec-summary uniqueness, and renderer IDs. |
| --source-waivers | boolean | No | False | Audit programs/<id>/source_waivers.yaml against vertex/policies/source_waivers.schema.yaml (D-32). |
| --schedule-health | boolean | No | False | Check whether scheduled prefetch/cockpit-build artifacts are present and fresh (ADF-W5.10). |
| --watch-sources | boolean | No | False | Validate selected vertex watch signal sources without starting the polling loop. |
| --source TEXT | text | No |  | Watch signal source to validate with --watch-sources. Repeat or use comma-separated values: ado, workiq, kusto, analytics, sprints, icm. |
| --catchup-log | boolean | No | False | Show recent catchup failures or truncation events from _feedback/usage_log.jsonl. |
| --nudge | boolean | No | False | Run all NQ-1 through NQ-9 nudge health checks for the resolved program. |
| --circuit-breakers | boolean | No | False | Show current persisted circuit breaker state and optionally reset it. |
| --reset-circuit-breakers | boolean | No | False | Reset persisted circuit breaker state to CLOSED. Requires --circuit-breakers. |
| --ranked | boolean | No | False | Show ranked context gaps from _feedback/context_gaps.jsonl (§21.3). Requires --context. |
| --fix-hints | boolean | No | False | Show per-item remediation guidance for each violation. Requires --context. |
| --refactor-status | boolean | No | False | Show Phase 0 debt-remediation progress metrics. |
| --sharepoint | boolean | No | False | Validate SharePoint/LT deck integration health (QG-SP-1 through QG-SP-8). |
| --strict-lt-alignment | boolean | No | False | With --sharepoint, treat lt_deck_alignment divergence as a warning (QG-SP-5). |
| --rev-health | boolean | No | False | Summarize Program-Context Intelligence (REV) subsystem health: run-state + verification distributions, evidence-vault retention, and Prompt-Shields mode. |
| --rev-program TEXT | text | No |  | Program ID for --rev-health (defaults to the resolved edition's program). |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex edit`

**Usage:** `vertex edit [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition name. |
| --section TEXT | text | Yes |  | Section id, dimension name, or exec_summary. |
| --issue INTEGER | integer | No |  | Issue number. Defaults to the latest draft issue. |

### `vertex enrich`

**Usage:** `vertex enrich [OPTIONS]`

Extract structured evidence from emails and transcripts via WorkIQ.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name (e.g. acme_weekly) |
| --lane TEXT | text | No |  | Target a single lane ID. Omit for all lanes. |
| --since TEXT | text | No | 7d | Lookback window (e.g. 7d, 14d) |
| --dry-run | boolean | No | False |  |
| --accept | boolean | No | False | Also update workiq_latest in registry with AI summary. |
| --format TEXT | text | No | human | Output format: human \| json |
| --batch / --no-batch | boolean | No | True | P4-17: batch lanes sharing a meeting series into one WorkIQ call. |

### `vertex escalate`

**Usage:** `vertex escalate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition id, e.g. myprogram_weekly. |
| --decision-ask TEXT | text | No |  | Optional decision ask id to scope the preview to a single ask. |
| --dry-run | boolean | No | False | Preview escalation recipients and draft content without writing files. |
| --channel TEXT | text | No | eml | Delivery channel. 'eml' writes manual-send draft EMLs and 'email' sends live Graph mail when maturity_level >= 2. |
| --rules PATH | path | No |  | Optional path to escalation_rules.yaml. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex evidence`

**Usage:** `vertex evidence [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition name. |
| --issue TEXT | text | No | latest | Issue number or 'latest'. |
| --section TEXT | text | No |  | Section id or dimension name. |
| --claim TEXT | text | No |  | Claim key such as deployment-velocity.risk. |
| --ado INTEGER | integer | No |  | ADO work item id. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex freshness`

**Usage:** `vertex freshness [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition used for the freshness run. |
| --since TEXT | text | No |  | Relative lookback window, for example 14d. |
| --by TEXT | text | No | dri | Grouping mode for freshness findings. |
| --teams-format | boolean | No | False | Print the Teams/Markdown version to stdout. |
| --notify | boolean | No | False | Preview outbound DRI notifications without sending them. |
| --allow-stale | boolean | No | False | Allow stale-snapshot fallback when live ADO is unavailable. |
| --dry-run | boolean | No | False | Preview notification output without send confirmation. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex fleet`

**Usage:** `vertex fleet [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --format TEXT | text | No | human | Output format: human, json, csv, md, or html. |
| --programs TEXT | text | No |  | Optional comma-separated program ids. |

### `vertex gather`

**Usage:** `vertex gather [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --cadence TEXT | text | No |  | Optional source profile: daily or weekly. |
| --workiq | boolean | No | False | Fetch WorkIQ evidence and append pending-review signals. |
| --kusto | boolean | No | False | Execute golden Kusto queries and append signals. |
| --probe | boolean | No | False | Include unvalidated Kusto candidate queries during gather. |
| --analytics | boolean | No | False | Query ADO Analytics snapshots and append telemetry signals. |
| --sprints | boolean | No | False | Query current ADO iterations and append sprint summary signals. |
| --pipelines | boolean | No | False | Query configured ADO pipeline runs and open pull requests and append auto-approved telemetry signals. |
| --icm | boolean | No | False | Execute IcM incident queries and append auto-approved signals. |
| --engms | boolean | No | False | Scan ADO work item descriptions for referenced eng.ms pages and append change signals. |
| --sharepoint | boolean | No | False | Gather SharePoint ref docs from engms_pages.yaml via WorkIQ. |
| --lt-deck | boolean | No | False | Also extract latest LT deck from program.yaml m365.sharepoint.lt_deck config. |
| --force-refresh | boolean | No | False | Bypass SharePoint change detection; re-extract all docs. |
| --dependency-scout | boolean | No | False | Refresh dependency proposals from the current gather signals and trajectories. |
| --verbose | boolean | No | False | Write structured gather traces under publications/<program>/observability/. |
| --facts-only | boolean | No | False | Skip full gather; only mirror current program facts into the fact store (FR-SG-61). |
| --extract-evidence | boolean | No | False | Run ContentExtractionAgent on transcript signals to populate WorkstreamEvidence. Requires --workiq. Off by default until validated. |

### `vertex prefetch`

**Usage:** `vertex prefetch [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --edition TEXT | text | No |  | Edition id to resolve item context from (defaults to the program's first edition). |
| --channel TEXT | text | No | workiq | Prefetch channel (only 'workiq' is implemented). |
| --ttl-seconds INTEGER | integer | No | 3600 | Snapshot freshness window. |

### `vertex history`

**Usage:** `vertex history [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --last INTEGER | integer | No |  | Show only the most recent N archived issues. |
| --issue INTEGER | integer | No |  | Show the archived Markdown for a specific issue. |
| --diff <INTEGER INTEGER>... | <integer integer> | No |  | Show a Markdown diff for two archived issues. |
| --search TEXT | text | No |  | Search archived Markdown for a keyword. |
| --semantic TEXT | text | No |  | Search archived confirmed narratives and incident learnings using the local semantic index. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex investigate`

**Usage:** `vertex investigate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --icm TEXT | text | No |  | Investigate a specific IcM incident id through Geneva Monitoring Agent. |
| --account TEXT | text | No |  | Run a Geneva health check for the specified Geneva account. |
| --dry-run | boolean | No | False | Show the resolved command and output path without executing it. |

### `vertex ingest-update`

**Usage:** `vertex ingest-update [OPTIONS]`

Parse a compact-update EML and stage ContextUpdateProposals (γ-Read Phase 3).

Apply proposals with: vertex context apply --edition EDITION --proposal-id ID

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name. |
| --source PATH | path | Yes |  | Path to the compact-update EML file. |
| --issue INTEGER RANGE | integer range | Yes |  | Issue number to stage proposals for. |
| --dry-run | boolean | No | False | Preview proposals without writing. |

### `vertex manifest`

**Usage:** `vertex manifest [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --issue INTEGER | integer | No |  | Issue number. Defaults to the latest draft or confirmed issue. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex meeting-close`

**Usage:** `vertex meeting-close [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --transcript TEXT | text | Yes |  | Meeting transcript id or meeting id. |
| --title TEXT | text | No |  | Optional meeting title override. |
| --format TEXT | text | No | human | Output format: human or json. |
| --html | boolean | No | False | Also write an HTML review artifact and open it locally. |
| --teams | boolean | No | False | Also write a Teams-markdown follow-up draft artifact. |
| --promote-actions | boolean | No | False | Queue extracted actions into the local action register for review. |
| --apply-ado | boolean | No | False | Apply the generated meeting-close ADO proposal immediately after review-plan filtering. |
| --approve-action INTEGER | integer | No |  | 1-based action index to approve. Repeat as needed. |
| --dismiss-action INTEGER | integer | No |  | 1-based action index to dismiss. Repeat as needed. |
| --edit-action INTEGER | integer | No |  | 1-based action index to edit before writing artifacts. |
| --edit-text TEXT | text | No |  | Replacement action text for --edit-action. |
| --edit-owner TEXT | text | No |  | Replacement owner alias for --edit-action. |
| --edit-due TEXT | text | No |  | Replacement due date (YYYY-MM-DD) for --edit-action. |
| --dry-run | boolean | No | False | Render the closure packet but skip local packet/proposal writes. |

### `vertex maturity-check`

**Usage:** `vertex maturity-check [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition id, e.g. myprogram_weekly. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex migrate`

**Usage:** `vertex migrate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --to TEXT | text | No |  | Target storage backend. Currently only sqlite is supported. |
| --rebuild-analytics | boolean | No | False | Rebuild the per-program analytics projection database from archive and journal primaries. |
| --dry-run | boolean | No | False | Preview migrated counts without writing SQLite data or updating program.yaml. |

### `vertex notify`

**Usage:** `vertex notify [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition used for the notify run. |
| --issue INTEGER RANGE | integer range | Yes |  | Pending issue number that will receive notify previews. |
| --channel TEXT | text | No | eml | Notification channel. 'eml' writes manual-send email drafts, 'adaptive-card' posts Teams cards when a webhook is configured or writes manual-post card JSON otherwise, and 'email' uses Graph send. |
| --since TEXT | text | No |  | Relative lookback window, for example 14d. |
| --dry-run | boolean | No | False | Preview notification emails without attempting send. |

### `vertex nudge`

**Usage:** `vertex nudge [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program ID, e.g. nova or armada. |
| --dry-run | boolean | No | False | Write preview EML to drafts/ without mutating cooldown state. |
| --stale-override TEXT | text | No |  | Override staleness per section: section_id=days. |
| --audit-registry | boolean | No | False | Read-only registry audit. Prints to stdout; no state change. |
| --audit-registry-output PATH | path | No |  | Write registry audit to this path (requires --audit-registry). |
| --reset-cooldown | boolean | No | False | Preview or confirm cooldown reset. |
| --yes | boolean | No | False | Confirm --reset-cooldown mutation. |
| --approve-draft TEXT | text | No |  | Record operator approval for a draft EML by filename or run_id. |
| --mark-sent TEXT | text | No |  | Mark a draft EML as sent: copy to published_eml/ and record audit. Pass the draft filename or run_id. |
| --import-sent TEXT | text | No |  | Import an already-sent published EML into cooldown/publication tracking by filename or run_id. |
| --sent-at TEXT | text | No |  | Override the attested/imported send timestamp for --mark-sent or --import-sent (ISO-8601, e.g. 2026-06-22T09:00:00Z). Defaults to now. |
| --list-drafts | boolean | No | False | List available draft EML files in drafts/. |

### `vertex next`

**Usage:** `vertex next [OPTIONS]`

Print up to 3 ranked CLI suggestions for the next step on the given edition.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition name (e.g. myprogram_weekly). |
| --program TEXT | text | No |  | Program id when using --goal without an edition. |
| --goal TEXT | text | No |  | Optional static goal name from program.yaml. |

### `vertex onboard`

**Usage:** `vertex onboard [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | New edition id, for example fabrikam_weekly. |
| --update TEXT | text | No |  | Existing edition name to update. |
| --migrate-v3 | boolean | No | False | Scaffold V3 program-model files for an existing edition without running the interactive wizard. |
| --migrate-deps | boolean | No | False | Compatibility alias for --migrate-v3 when migrating legacy dependencies into dependencies.yaml. |
| --ai | boolean | No | False | Use AI-assisted suggestions during onboarding when available. |

### `vertex setup`

**Usage:** `vertex setup [OPTIONS]`

Conversational, AI-assisted setup for a new Vertex program.

Creates a working program configuration through a short guided conversation.
Type ? at any prompt for contextual help.

Quick start:
  vertex setup
  vertex setup --demo
  vertex setup --from-description "Platform reliability weekly for the Acme team"

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --from-description TEXT | text | No |  | One-line program description. Skips greeting; enters auto-discovery. |
| --demo | boolean | No | False | Show what setup produces for a fictional program. No files written. |
| --preview | boolean | No | False | Show config YAML to stdout without writing files. |
| --auto | boolean | No | False | Accept AI suggestions automatically; minimize interactive prompts. |
| --auto-confirm | boolean | No | False | Skip the review step and write files immediately. Only valid with --from-description. |
| --advanced | boolean | No | False | Expose all fields, including optional ones. |
| --manual | boolean | No | False | Skip ADO discovery entirely; collect all values interactively. |
| --resume | boolean | No | False | Resume from a saved .vertex/setup_session_*.json file. |
| --no-open | boolean | No | False | Suppress automatic browser opening after preview. |
| --update | boolean | No | False | Update an existing program/edition config. |
| --dry-run | boolean | No | False | Output generated YAML to stdout without writing files. |
| --output-dir PATH | path | No | . | Where to write preview and session files. |
| --ado-org TEXT | text | No |  | ADO organization (skips ADO org prompt). |
| --ado-project TEXT | text | No |  | ADO project (skips ADO project prompt). |

### `vertex owner-pack`

**Usage:** `vertex owner-pack [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --owner TEXT | text | Yes |  | Owner alias, for example priya. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex override`

**Usage:** `vertex override [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --dimension TEXT | text | No |  | Optional single dimension name to edit. |

### `vertex prep`

**Usage:** `vertex prep [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name (e.g. myprogram_lt_deck). |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex decision-brief`

**Usage:** `vertex decision-brief [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. acme_weekly. |
| --issue INTEGER | integer | No |  | Issue number. Defaults to the active issue. |
| --ai / --no-ai | boolean | No | False | Run LLM-as-judge to generate recommendations per item. |
| --open / --no-open | boolean | No | True | Open the decision brief in the browser. |

### `vertex propose`

**Usage:** `vertex propose [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition id, e.g. myprogram_weekly. |
| --ai / --no-ai | boolean | No | False | Enable AI text generation for proposals. |
| --dry-run | boolean | No | False | Preview proposal briefs without writing proposals.jsonl. |
| --offline / --no-offline | boolean | No | False | Use cached snapshot instead of live ADO fetch. |
| --steering TEXT | text | No |  | PM strategic theme to apply as narrative emphasis (recorded as a ProgramEvent for provenance). |

### `vertex published-baseline`

**Usage:** `vertex published-baseline [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition name. |
| --issue INTEGER | integer | Yes |  | Confirmed issue number that owns the published newsletter. |
| --eml FILE | file | No |  | Optional explicit published EML path. |
| --target-issue INTEGER | integer | No |  | Optional active issue number to update with the imported published narratives. |
| --write | boolean | No | False | Persist the imported published bundle and apply any safe target updates. |

### `vertex rollback`

**Usage:** `vertex rollback [OPTIONS]`

Restore a program's mutable stores to a named checkpoint.

Run without --to to list available checkpoints. Pass --to <checkpoint_name> to restore.
Use --drill to run the rollback in a sandbox and record proof s7a_rollback_drill.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition id, e.g. myprogram_weekly. |
| --to TEXT | text | No |  | Checkpoint directory name to restore (omit to list). |
| --dry-run | boolean | No | False | Preview restore without writing files. |
| --drill | boolean | No | False | Phase 6 §22 Step 10: run a rollback drill. Simulates the rollback in a temporary sandbox (live program is NOT modified), verifies the post-rollback state is queryable + the trusted baseline is re-derivable, and records the result as proof `s7a_rollback_drill` in `platform_proof_log.yaml`. Pass --to <checkpoint> to choose the checkpoint (defaults to the newest). |
| --archetype TEXT | text | No |  | Optional archetype label for the recorded proof, e.g. 'ADO + Kusto'. |
| --notes TEXT | text | No |  | Optional operator notes recorded with the proof. |

### `vertex review-proposals`

**Usage:** `vertex review-proposals [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --issue INTEGER | integer | No |  | Issue number to inspect. Defaults to the active issue. |
| --section TEXT | text | No |  | Render only the pending proposal for the specified section id. |
| --resolved-only | boolean | No | False | Render resolved proposal history instead of pending proposals. |
| --open / --no-open | boolean | No | True | Open the proposal review HTML in the browser after rendering. |

### `vertex publish-gate`

**Usage:** `vertex publish-gate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --issue INTEGER | integer | No |  | Issue number to validate. Defaults to the active issue. |
| --force | boolean | No | False | Override forceable publish-gate failures while keeping hard blocks enforced. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex draft`

**Usage:** `vertex draft [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, for example acme_weekly. |
| --issue INTEGER | integer | No |  | Issue number to render. Defaults to next issue. |
| --dry-run | boolean | No | False | Generate draft outputs without archive writes. Narrative seeding may still write draft narrative files for the current issue. |
| --reseed | boolean | No | False | Delete seedable draft narratives for the target issue before re-seeding from the trusted baseline. Dry-run only. |
| --no-seed | boolean | No | False | Skip trusted-baseline narrative seeding for this run and render from scaffold templates or existing narratives instead. |
| --offline | boolean | No | False | Render from the newest cached snapshot without live ADO or Kusto calls. |
| --diff | boolean | No | False | Compare the current draft against the last dry-run and print a diff summary. |
| --send-draft | boolean | No | False | Send the rendered draft to the author's mailbox for Outlook preview. |
| --ai-review | boolean | No | False | Run advisory draft review suggestions after rendering the dry-run draft. |
| --no-ai | boolean | No | False | Suppress optional AI-powered draft review helpers. |
| --as-of [%Y-%m-%d\|%Y-%m-%dT%H:%M:%S\|%Y-%m-%d %H:%M:%S] | datetime | No |  | Override ADO data timestamp in UTC. |
| --edition-type TEXT | text | No |  | Override edition type for rendering. |
| --range INTEGER RANGE | integer range | No |  | Number of confirmed issues to include for lookback editions. Defaults to the edition window when omitted. |
| --sections TEXT | text | No |  | Limit rendered detail sections to these section ids. Repeat or comma-separate values; ws:<id> is accepted. |
| --stdout | boolean | No | False | Emit compact JSON manifest to stdout. |
| --format TEXT | text | No | json | Stdout payload format: json, html, or md. |
| --verbose | boolean | No | False | Include evidence packet details in --stdout json output. |

### `vertex reconcile`

**Usage:** `vertex reconcile [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --refresh | boolean | No | False | Recompute contradiction state instead of reading the cached analytics state. |
| --dry-run | boolean | No | False | Compute contradictions but skip updating the cached analytics state. |

### `vertex report`

**Usage:** `vertex report [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, for example acme_weekly. |
| --issue INTEGER | integer | No |  | Issue number to render. Defaults to next issue. |
| --dry-run | boolean | No | False | Generate draft outputs without archive writes. Narrative seeding may still write draft narrative files for the current issue. |
| --reseed | boolean | No | False | Delete seedable draft narratives for the target issue before re-seeding from the trusted baseline. Dry-run only. |
| --no-seed | boolean | No | False | Skip trusted-baseline narrative seeding for this run and render from scaffold templates or existing narratives instead. |
| --offline | boolean | No | False | Render from the newest cached snapshot without live ADO or Kusto calls. |
| --diff | boolean | No | False | Compare the current draft against the last dry-run and print a diff summary. |
| --send-draft | boolean | No | False | Send the rendered draft to the author's mailbox for Outlook preview. |
| --ai-review | boolean | No | False | Run advisory draft review suggestions after rendering the dry-run draft. |
| --no-ai | boolean | No | False | Suppress optional AI-powered draft review helpers. |
| --as-of [%Y-%m-%d\|%Y-%m-%dT%H:%M:%S\|%Y-%m-%d %H:%M:%S] | datetime | No |  | Override ADO data timestamp in UTC. |
| --edition-type TEXT | text | No |  | Override edition type for rendering. |
| --range INTEGER RANGE | integer range | No |  | Number of confirmed issues to include for lookback editions. Defaults to the edition window when omitted. |
| --sections TEXT | text | No |  | Limit rendered detail sections to these section ids. Repeat or comma-separate values; ws:<id> is accepted. |
| --stdout | boolean | No | False | Emit compact JSON manifest to stdout. |
| --format TEXT | text | No | json | Stdout payload format: json, html, or md. |
| --verbose | boolean | No | False | Include evidence packet details in --stdout json output. |

### `vertex review-debrief`

**Usage:** `vertex review-debrief [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --edition TEXT | text | No |  | Edition id used to resolve the program when --program is omitted. |
| --issue INTEGER | integer | No |  | Optional issue number associated with the debrief. |
| --title TEXT | text | Yes |  | Decision title. |
| --context TEXT | text | Yes |  | Decision context or LT feedback framing. |
| --decision TEXT | text | Yes |  | Decision outcome recorded from the debrief. |
| --reviewer TEXT | text | No |  | Reviewer or decider alias. Defaults to the current OS user. |
| --review-by TEXT | text | No |  | Optional YYYY-MM-DD follow-up review date for the decision. |
| --workstream TEXT | text | No |  | Optional workstream id for the recorded decision and follow-up actions. |
| --entity-ref TEXT | text | No |  | Repeat to attach entity refs such as WI:7818186. |
| --alternative TEXT | text | No |  | Repeat to record alternatives considered. |
| --action TEXT | text | No |  | Repeat to add follow-up actions using 'owner_alias\|YYYY-MM-DD\|text' or 'owner_alias\|\|text'. |
| --dry-run | boolean | No | False | Preview the debrief write without mutating decisions.yaml or actions.jsonl. |

### `vertex review-full`

**Usage:** `vertex review-full [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --issue INTEGER | integer | No |  | Issue number to render. Defaults to the active issue. |
| --open / --no-open | boolean | No | True | Open the reviewer HTML in the browser after rendering. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |
| --post-adaptive-cards | boolean | No | False | Post generated section review adaptive cards to Teams when a webhook is configured. |

### `vertex probe-ado`

**Usage:** `vertex probe-ado [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --area TEXT | text | Yes |  | ADO area path to probe. |
| --since TEXT | text | No | 14d | Relative lookback window, for example 14d. |
| --edition TEXT | text | No |  | Edition used for organization, project, and work item type defaults. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex status`

**Usage:** `vertex status [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition id, e.g. myprogram_weekly. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex summarize`

**Usage:** `vertex summarize [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --reset | boolean | No | False | Regenerate summaries from raw approved signals instead of incrementally. |
| --workstream TEXT | text | No |  | Limit summary generation to a single workstream id. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex synthesize`

**Usage:** `vertex synthesize [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --workstream TEXT | text | Yes |  | Workstream id to synthesize. |
| --program TEXT | text | No |  | Program id override. |
| --edition TEXT | text | No |  | Edition id used to resolve the program. |
| --reviewer TEXT | text | No |  | Reviewer alias for supersession records. |
| --window-days INTEGER RANGE | integer range | No | 30 | Approved-signal lookback window. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex triage`

**Usage:** `vertex triage [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition id, e.g. myprogram_weekly. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex trust`

**Usage:** `vertex trust [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --window-issues INTEGER | integer | No |  | Rolling issue window used for editorial trust calibration. |
| --action TEXT | text | No |  | Optional action or task type filter, for example decision_ask_escalation. |
| --slice TEXT | text | No |  | Optional autonomy slice: workstream, dri, or time. |
| --window TEXT | text | No |  | Optional time-slice window in weeks, for example 8w. |
| --graduation-metrics | boolean | No | False | Emit bridge-graduation metrics over the latest confirmed issue window. |
| --format TEXT | text | No | human | Output format: human or json. |

### `vertex trust-bootstrap`

**Usage:** `vertex trust-bootstrap [OPTIONS]`

Apply cold-start trust grants from trust_policy.yaml to a program.

Writes trust.bootstrap_grant facts for each provenance class defined in
vertex/policies/trust_policy.yaml. Idempotent — skips existing grants.
Never synthesises reviews; human-reviewed flow is never blocked.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program ID. |
| --granted-by TEXT | text | Yes |  | Operator identity granting the bootstrap. |
| --dry-run | boolean | No | False | Show what would be applied without writing. |
| --format TEXT | text | No | human | Output format: human or json. |

### `vertex hints`

**Usage:** `vertex hints [OPTIONS]`

Generate and interactively manage narrative delta hints.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name. |
| --issue INTEGER | integer | No |  | Issue number. Defaults to the latest/next draft issue. |
| --interactive / --no-interactive | boolean | No | True | Interactively accept/reject/modify generated hints. |

### `vertex vitality`

**Usage:** `vertex vitality [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --owner TEXT | text | No |  | Filter to a single owner alias. |
| --workstream TEXT | text | No |  | Filter to a single workstream id. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex watch`

**Usage:** `vertex watch [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --interval INTEGER RANGE | integer range | No | 60 | Polling interval in seconds. |
| --cadence [intraday\|daily] | choice | No | WatchCadence.INTRADAY | Polling cadence: intraday or daily. |
| --source TEXT | text | No |  | Signal source to poll. Repeat or use comma-separated values: ado, workiq, kusto, analytics, sprints, icm. |

### `vertex list`

**Usage:** `vertex list [OPTIONS] COMMAND [ARGS]...`

List configured Vertex resources.

**Subcommands**

| Command | Description |
|---|---|
| `editions` |  |
| `workstreams` |  |
| `dris` |  |

#### `vertex list editions`

**Usage:** `vertex list editions [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex list workstreams`

**Usage:** `vertex list workstreams [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex list dris`

**Usage:** `vertex list dris [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex actions`

**Usage:** `vertex actions [OPTIONS] COMMAND [ARGS]...`

List and review extracted actions.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `review` |  |
| `resolve` |  |

#### `vertex actions list`

**Usage:** `vertex actions list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --status TEXT | text | No |  | Optional status filter. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex actions review`

**Usage:** `vertex actions review [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --reviewer TEXT | text | No |  | Reviewer alias. Defaults to current OS user. |
| --apply-ado | boolean | No | False | Apply fully approved meeting-close ADO proposal batches after review. |

#### `vertex actions resolve`

**Usage:** `vertex actions resolve [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Action id. |
| --status TEXT | text | No | done | Resolution status: done\|cancelled. |
| --note TEXT | text | No |  | Optional resolution note. |
| --resolver TEXT | text | No |  | Resolver alias. Defaults to current OS user. |

### `vertex actuate`

**Usage:** `vertex actuate [OPTIONS] COMMAND [ARGS]...`

Governed actuation — review proposals and execute approved ones.

**Subcommands**

| Command | Description |
|---|---|
| `review` | Show pending actuation proposals for a program. |
| `execute` | Execute an approved actuation proposal. |

#### `vertex actuate review`

**Usage:** `vertex actuate review [OPTIONS]`

Show pending actuation proposals for a program.

Proposals are derived from actuation_engine.derive_proposals() against the
current ProgramReality. Expired, executed, and terminally-failed proposals
are excluded.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to review proposals for. |
| --dry-run | boolean | No | False | Show what would be executed without writing. |

#### `vertex actuate execute`

**Usage:** `vertex actuate execute [OPTIONS]`

Execute an approved actuation proposal.

INV-12: This command will refuse to execute if the proposal is not marked
approved. Human approval (setting approved=True on the action.proposal fact)
is the only path to execution.

CP-7: Operators MUST review dry-run payloads of all enabled rules before
live execution. Run with --dry-run first.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID. |
| --proposal-id TEXT | text | Yes |  | Proposal ID to execute. |
| --dry-run | boolean | No | False | Validate and render; no live writes. |

### `vertex ado`

**Usage:** `vertex ado [OPTIONS] COMMAND [ARGS]...`

ADO diagnostics and update workflows.

**Subcommands**

| Command | Description |
|---|---|
| `status` |  |
| `propose` |  |
| `apply` |  |
| `reconcile` |  |
| `discover-repos` |  |
| `set-repos` |  |

#### `vertex ado status`

**Usage:** `vertex ado status [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex ado propose`

**Usage:** `vertex ado propose [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --type TEXT | text | Yes |  | Proposal type. Supports comment, field, vitality_nudge, and vitality_tag. |
| --edition TEXT | text | No |  | Edition id that owns the confirmed issue. |
| --issue INTEGER | integer | No |  | Confirmed issue number to cite in the proposal. |
| --dry-run | boolean | No | False | Preview the proposal without writing a manifest file. |

#### `vertex ado apply`

**Usage:** `vertex ado apply [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --proposal TEXT | text | Yes |  | Proposal id or manifest path. |
| --yes | boolean | No | False | Apply without interactive confirmation. |

#### `vertex ado reconcile`

**Usage:** `vertex ado reconcile [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex ado discover-repos`

**Usage:** `vertex ado discover-repos [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --workstream TEXT | text | No |  | Optional workstream id to scope repository discovery. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex ado set-repos`

**Usage:** `vertex ado set-repos [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --workstream TEXT | text | Yes |  | Workstream id to update. |
| --repository-id TEXT | text | No |  | Repository id to attach. Repeat for multiple repos. |
| --repository-name TEXT | text | No |  | Exact repository name to resolve and attach. Repeat for multiple repos. |
| --clear | boolean | No | False | Clear all configured ado_repository_ids for the target workstream. |

### `vertex ai-proposals`

**Usage:** `vertex ai-proposals [OPTIONS] COMMAND [ARGS]...`

Review AI-generated proposals: risk, meeting action, top-three, governance decision brief, dependency blast radius.

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `generate` |  |
| `accept` |  |
| `reject` |  |
| `review-batch` | ADF-W5.12 P4 (Section 8.15.2): sampled/batch review for a proposal |
| `flag-regression` | ADF-W5.12 P4 (Section 8.15.1's 'zero material downstream regressions' |
| `review` | Interactive one-by-one review of every staged proposal of --type: |

#### `vertex ai-proposals list`

**Usage:** `vertex ai-proposals list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --type TEXT | text | No |  | Optional filter, one of: risk, meeting_action, top_three, governance_decision_brief, dependency_blast_radius. |
| --status TEXT | text | No | staged | Filter by status: staged\|approved\|rejected\|all. |

#### `vertex ai-proposals generate`

**Usage:** `vertex ai-proposals generate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --type TEXT | text | Yes |  | One of: risk, meeting_action, top_three, governance_decision_brief, dependency_blast_radius. |
| --candidate-risk-id TEXT | text | No |  | [risk] Candidate risk id being escalated. |
| --title TEXT | text | No |  | [risk] Candidate risk title. |
| --description TEXT | text | No |  | [risk] Candidate risk description. |
| --evidence-text TEXT | text | No |  | [risk] Evidence text snippet. Repeat for multiple. |
| --evidence-ref TEXT | text | No |  | [risk] Evidence reference id (e.g. signal id). Repeat for multiple. |
| --meeting-ref TEXT | text | No |  | [meeting_action] Meeting reference id. |
| --transcript-file PATH | path | No |  | [meeting_action] Path to a plain-text transcript file. |
| --work-item-id INTEGER | integer | No |  | [meeting_action] Allowed work item id the extractor may link actions to. Repeat for multiple. |
| --candidates-file PATH | path | No |  | [top_three] JSON file: {"items": [{"category": str, "item_id": str, "summary": str, "severity": str\|null, "evidence_refs": [str, ...]}, ...]}. |
| --decision-ask-id TEXT | text | No |  | [governance_decision_brief] Decision ask id being resolved. |
| --decision-text TEXT | text | No |  | [governance_decision_brief] The open decision ask's text. |
| --dependency-id TEXT | text | No |  | [dependency_blast_radius] Dependency id being assessed. |
| --from-summary TEXT | text | No |  | [dependency_blast_radius] Upstream (from) side summary. |
| --to-summary TEXT | text | No |  | [dependency_blast_radius] Downstream (to) side summary. |
| --risk-if-broken TEXT | text | No |  | [dependency_blast_radius] Risk if this dependency breaks. |
| --current-status TEXT | text | No |  | [dependency_blast_radius] Current dependency status. |
| --deployment TEXT | text | No |  | Override Azure OpenAI deployment name; defaults to VERTEX_AI_DEPLOYMENT/VERTEX_EXEC_DEPLOYMENT/AZURE_OPENAI_DEPLOYMENT. |
| --dry-run | boolean | No | False | Run the AI generation but do not stage the result for review. |

#### `vertex ai-proposals accept`

**Usage:** `vertex ai-proposals accept [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --type TEXT | text | Yes |  | One of: risk, meeting_action, top_three, governance_decision_brief, dependency_blast_radius. |
| --id TEXT | text | Yes |  | Proposal id to accept. |
| --actor TEXT | text | No |  | Reviewer identity. Defaults to the current OS user. |
| --org TEXT | text | No |  | ADO organization (meeting_action routing only). |
| --project TEXT | text | No |  | ADO project (meeting_action routing only). |
| --area-path TEXT | text | No |  | Optional ADO area path (meeting_action routing only). |
| --iteration-path TEXT | text | No |  | Optional ADO iteration path (meeting_action routing only). |
| --edition TEXT | text | No |  | Edition to publish into top_3_now (top_three only). |
| --by-date TEXT | text | No |  | Optional YYYY-MM-DD due date for the published top_3_now entry (top_three only). |
| --ado-link TEXT | text | No |  | Optional ADO link for the published top_3_now entry (top_three only). |
| --anchor TEXT | text | No |  | Optional report anchor for the published top_3_now entry (top_three only). |
| --dry-run | boolean | No | False | Preview the accept without persisting any change. |

#### `vertex ai-proposals reject`

**Usage:** `vertex ai-proposals reject [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --type TEXT | text | Yes |  | One of: risk, meeting_action, top_three, governance_decision_brief, dependency_blast_radius. |
| --id TEXT | text | Yes |  | Proposal id to reject. |
| --reason TEXT | text | Yes |  | Why this proposal was rejected. |
| --dry-run | boolean | No | False | Preview the reject without persisting any change. |

#### `vertex ai-proposals review-batch`

**Usage:** `vertex ai-proposals review-batch [OPTIONS]`

ADF-W5.12 P4 (Section 8.15.2): sampled/batch review for a proposal
class at L3+ autonomy. A human individually reviews only a sample of
the currently-staged batch (random + materiality-weighted); the rest
are auto-approved by extension of that sample's trust. Requires L3+
(promote via `vertex cockpit autonomy-promote --to l3 --sample-rate ...`
first) -- below L3, use `list`/`accept`/`reject` for full review.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --type TEXT | text | Yes |  | One of: risk, meeting_action, top_three, governance_decision_brief, dependency_blast_radius (sampled review is piloted for these two types). |
| --sample-size INTEGER | integer | No |  | Override the computed sample size (operator control/testing). |
| --seed INTEGER | integer | No |  | Deterministic RNG seed for sample selection (mainly for testing). |
| --actor TEXT | text | No |  | Reviewer identity for the sampled subset. Defaults to the current OS user. |
| --org TEXT | text | No |  | ADO organization (meeting_action routing only). |
| --project TEXT | text | No |  | ADO project (meeting_action routing only). |
| --area-path TEXT | text | No |  | Optional ADO area path (meeting_action routing only). |
| --iteration-path TEXT | text | No |  | Optional ADO iteration path (meeting_action routing only). |
| --dry-run | boolean | No | False | Preview the sample/auto-approve split without deciding or applying anything. |

#### `vertex ai-proposals flag-regression`

**Usage:** `vertex ai-proposals flag-regression [OPTIONS]`

ADF-W5.12 P4 (Section 8.15.1's 'zero material downstream regressions'
L3/L4 floor): records that an already-approved proposal (whether
individually reviewed or auto-approved via `review-batch`'s sampled
trust extension) turned out to be a material downstream regression --
the human-facing entry point for the "material regression" signal the
autonomy ladder's L3/L4 evidence needs and that no code path could ever
detect on its own (a CLI cannot observe the downstream consequences of
its own past effects). Feeds `vertex cockpit autonomy-evaluate`'s next
run: any flagged regression at L3+ immediately demotes the class one
level (see `proposal_autonomy_ladder.evaluate_promotion`).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --type TEXT | text | Yes |  | One of: risk, meeting_action, top_three, governance_decision_brief, dependency_blast_radius. |
| --id TEXT | text | Yes |  | Proposal id to flag as a material regression. |
| --reason TEXT | text | Yes |  | Why this approved proposal's effect turned out to be a material downstream regression. |

#### `vertex ai-proposals review`

**Usage:** `vertex ai-proposals review [OPTIONS]`

Interactive one-by-one review of every staged proposal of --type:
preview, confirm accept/reject, prompt for a rejection reason. For
per-type follow-on options (ADO routing, edition publish), use
`ai-proposals accept --id <id> ...` directly after accepting here.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --type TEXT | text | Yes |  | One of: risk, meeting_action, top_three, governance_decision_brief, dependency_blast_radius. |
| --actor TEXT | text | No |  | Reviewer identity. Defaults to the current OS user. |

### `vertex admin`

**Usage:** `vertex admin [OPTIONS] COMMAND [ARGS]...`

Vertex operator and debug commands.

**Subcommands**

| Command | Description |
|---|---|
| `doctor` |  |
| `notifications` |  |
| `baseline` |  |
| `platform-proof` |  |
| `s7-position` |  |
| `reconcile` |  |
| `migrate-legacy-state` |  |
| `fact-store-flip` |  |
| `archive-signing` | Manage the HMAC key used to sign archive manifests. |
| `upgrade-state` |  |
| `metrics-rollup` | Computes and appends one ISO week's aggregate for one or all raw |
| `assertion` | Author telemetry assertions for L1 reality evaluation. |
| `auth` | Authentication setup commands. |
| `db` | Inspect and validate the L1 reality database. |
| `metric` | Metric binding operator commands. |

#### `vertex admin doctor`

**Usage:** `vertex admin doctor [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition name. Uses the only configured edition when omitted. |
| --fix | boolean | No | False | Auto-create missing overrides.yaml when possible. |
| --check-auth | boolean | No | False | Validate ADO auth reachability/token age, Graph send prerequisites, and Agency CLI availability. |
| --operator-gates | boolean | No | False | Summarize the remaining PM/operator gates with live evidence, next commands, and explicit operator-vs-LLM responsibilities. |
| --platform-readiness | boolean | No | False | Measure fleet-scoped P4/P5/V-11 readiness from provable repo signals and mark unrecorded proof criteria as UNPROVEN. |
| --kb | boolean | No | False | Validate knowledge, program, and edition referential integrity. |
| --kb-check-origins | boolean | No | False | With --kb, compare current origin files against stored knowledge vault hashes to detect stale ingested copies. |
| --context | boolean | No | False | Validate cross-file program context invariants (§5) and staleness policy (§8). |
| --ids | boolean | No | False | Validate scorecard, chapter, slice, registry, and workstream ID consistency. |
| --cadence | boolean | No | False | Validate communication-plan cadence against recent confirmation history. |
| --channels | boolean | No | False | Inspect gather channel completeness, active flags, and transcript coverage telemetry. |
| --privacy | boolean | No | False | Scan journal files for credential patterns and verify people_profiles.yaml encryption state. |
| --kusto | boolean | No | False | Validate applicable Kusto query definitions and probe live reachability. |
| --milestones | boolean | No | False | Validate milestones.yaml schema, workstream links, and owner aliases. |
| --dependencies | boolean | No | False | Validate dependencies.yaml schema, references, cycles, and legacy fallback state. |
| --actions | boolean | No | False | Validate actions.jsonl schema, references, and overdue actions. |
| --risks | boolean | No | False | Validate risk_register.yaml schema, references, and stale review dates. |
| --escalations | boolean | No | False | Validate escalation_rules.yaml schema and escalation_state.json cooldown state. |
| --decisions | boolean | No | False | Validate decisions.yaml schema, references, and stale proposed decisions. |
| --assumptions | boolean | No | False | Validate assumptions.yaml schema, references, and overdue validation dates. |
| --readiness | boolean | No | False | Validate readiness.yaml presence and readiness_snapshot.yaml freshness/integrity. |
| --semantic-index | boolean | No | False | Validate semantic index freshness, dirty state, and optimization health. |
| --personas | boolean | No | False | Validate personas.yaml schema, check hygiene, minimum density, staleness, and re2 availability. |
| --metric-bindings | boolean | No | False | Validate L1 metric-binding readiness, revalidate stale bindings, and flag validation drift. |
| --consistency | boolean | No | False | Validate trusted baseline, confirmed archive, and review-state issue alignment. |
| --checkpoints | boolean | No | False | Validate checkpoint inventory and whether the latest checkpoint covers the mutable program stores needed for rollback. |
| --storage | boolean | No | False | Validate journal retention posture, trajectory footprint, and SQLite storage health. |
| --flip-status | boolean | No | False | Report the current Fact Store source-of-record posture for the resolved edition (legacy, dual, or fact-store). |
| --flip-parity | boolean | No | False | Compare legacy mutable-state projections against Fact Store projections for one confirmed issue. |
| --fact-parity | boolean | No | False | Check whether enough dual-read parity cycles have been logged for the resolved program (reads fact_store.dual_read_cycles from platform_state.yaml, default 5). |
| --fact-bridge | boolean | No | False | Check the ledger->fact-store bridge posture: whether it is enabled for a REV-configured program, and whether a persistent bridge-failure backlog exists (fix-data-flow.md Track A / PS-2). |
| --fact-deserialization | boolean | No | False | Confirm existing persisted facts still deserialize against the current schema, not just newly-bridged ones (fix-data-flow.md Track L). |
| --confirm-readiness | boolean | No | False | Enumerate exact live blockers that would prevent a non-forced confirm. Returns 0 only when confirm would succeed. |
| --adapter-cert | boolean | No | False | Audit UIL adapter certification per WS-3: checks which channels are enabled/certified and probes WorkIQ verb availability. |
| --issue INTEGER | integer | No |  | Issue number required by --flip-parity. |
| --charts | boolean | No | False | Validate chart cache TTL vs edition cadence, attachment targets, exec-summary uniqueness, and renderer IDs. |
| --source-waivers | boolean | No | False | Audit programs/<id>/source_waivers.yaml against vertex/policies/source_waivers.schema.yaml (D-32). |
| --schedule-health | boolean | No | False | Check whether scheduled prefetch/cockpit-build artifacts are present and fresh (ADF-W5.10). |
| --watch-sources | boolean | No | False | Validate selected vertex watch signal sources without starting the polling loop. |
| --source TEXT | text | No |  | Watch signal source to validate with --watch-sources. Repeat or use comma-separated values: ado, workiq, kusto, analytics, sprints, icm. |
| --catchup-log | boolean | No | False | Show recent catchup failures or truncation events from _feedback/usage_log.jsonl. |
| --nudge | boolean | No | False | Run all NQ-1 through NQ-9 nudge health checks for the resolved program. |
| --circuit-breakers | boolean | No | False | Show current persisted circuit breaker state and optionally reset it. |
| --reset-circuit-breakers | boolean | No | False | Reset persisted circuit breaker state to CLOSED. Requires --circuit-breakers. |
| --ranked | boolean | No | False | Show ranked context gaps from _feedback/context_gaps.jsonl (§21.3). Requires --context. |
| --fix-hints | boolean | No | False | Show per-item remediation guidance for each violation. Requires --context. |
| --refactor-status | boolean | No | False | Show Phase 0 debt-remediation progress metrics. |
| --sharepoint | boolean | No | False | Validate SharePoint/LT deck integration health (QG-SP-1 through QG-SP-8). |
| --strict-lt-alignment | boolean | No | False | With --sharepoint, treat lt_deck_alignment divergence as a warning (QG-SP-5). |
| --rev-health | boolean | No | False | Summarize Program-Context Intelligence (REV) subsystem health: run-state + verification distributions, evidence-vault retention, and Prompt-Shields mode. |
| --rev-program TEXT | text | No |  | Program ID for --rev-health (defaults to the resolved edition's program). |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex admin notifications`

**Usage:** `vertex admin notifications [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program identifier. |
| --since TEXT | text | Yes |  | Include entries at or after this ISO date. |
| --format TEXT | text | No | text | Output format: text, json, or csv. |

#### `vertex admin baseline`

**Usage:** `vertex admin baseline [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition name (e.g. myprogram_weekly). |
| --correct | boolean | No | False | Apply a trusted-baseline correction. |
| --record-rollback-drill | boolean | No | False | Append a rollback-drill pass record to trusted_baseline.yaml history. |
| --issue INTEGER RANGE | integer range | No |  | Confirmed archive issue number to set as the trusted baseline. |
| --reason TEXT | text | No |  | Why the trusted baseline is being corrected. |
| --checkpoint-name TEXT | text | No |  | Checkpoint name used for the rollback drill. |
| --rollback-exit-code INTEGER RANGE | integer range | No |  | Exit code returned by the rollback command during the drill. |
| --consistency-exit-code INTEGER RANGE | integer range | No |  | Exit code returned by doctor --consistency after the rollback drill. |
| --lock INTEGER RANGE | integer range | No |  | Hardlock an issue: its confirmed snapshot + overrides can no longer be overwritten. |
| --unlock INTEGER RANGE | integer range | No |  | Remove the hardlock from an issue so it can be rebuilt/overwritten again. |

#### `vertex admin platform-proof`

**Usage:** `vertex admin platform-proof [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --proof-id TEXT | text | No |  | Proof identifier, for example p4a_clean_machine or p6b_ado_only. |
| --program TEXT | text | No |  | Program id that owns the proof log. |
| --edition TEXT | text | No |  | Edition name; used to infer the owning program and stored edition when omitted. |
| --status TEXT | text | No | passed | Proof outcome: passed or failed. |
| --notes TEXT | text | No |  | Optional operator notes describing the proof run. |
| --elapsed-minutes FLOAT RANGE | float range | No |  | Optional elapsed time for the proof run. |
| --no-code-changes / --code-changes | boolean | No |  | Whether the proof run completed without editing code. |
| --confirm-exit-code INTEGER RANGE | integer range | No |  | Optional confirm exit code recorded during the proof run. |
| --archetype TEXT | text | No |  | Optional archetype label, for example 'ADO-only' or 'ADO + M365'. |
| --plan | boolean | No | False | Show required platform proofs and repo coverage instead of recording a new proof. |

#### `vertex admin s7-position`

**Usage:** `vertex admin s7-position [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --position TEXT | text | Yes |  | S7 readiness position: complete or deferred. |
| --justification TEXT | text | No |  | Required when position is deferred. |

#### `vertex admin reconcile`

**Usage:** `vertex admin reconcile [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --tier TEXT | text | No | all | One of: hot, warm, cold, all. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex admin migrate-legacy-state`

**Usage:** `vertex admin migrate-legacy-state [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --dry-run | boolean | No | False | Preview imported fact counts without writing to the fact store. |

#### `vertex admin fact-store-flip`

**Usage:** `vertex admin fact-store-flip [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --to TEXT | text | Yes |  | Edition id to assess for fact-store flip readiness. |
| --execute | boolean | No | False | Create a pre-flip checkpoint and persist SoR mode to shadow. |
| --commit | boolean | No | False | Promote a previously executed shadow flip to primary SoR mode. |
| --family TEXT | text | No |  | Authority family to flip to primary mode (WI-5.3). |

#### `vertex admin archive-signing`

**Usage:** `vertex admin archive-signing [OPTIONS]`

Manage the HMAC key used to sign archive manifests.

Exactly one of --set-key, --clear, or --status is required. The key is
stored in the OS keyring under service "vertex-archive-signing"; on
Windows that is the Windows Credential Manager. The key is never
written to disk.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --set-key | boolean | No | False | Persist the HMAC signing key in the system keyring (reads from stdin or env VERTEX_ARCHIVE_SIGNING_KEY). |
| --clear | boolean | No | False | Remove the HMAC signing key from the system keyring. |
| --status | boolean | No | False | Report whether a signing key is currently configured. |
| --keyring-user TEXT | text | No | primary | Keyring username to scope the key under (default: 'primary'). |

#### `vertex admin upgrade-state`

**Usage:** `vertex admin upgrade-state [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --dry-run | boolean | No | False | Preview the evolution steps without writing program.yaml or migration_log.jsonl. |
| --apply | boolean | No | False | Apply the evolution: rewrite program.yaml and append migration_log.jsonl rows. Mutually exclusive with --dry-run. |
| --operator TEXT | text | No | vertex.admin | Operator identity recorded in the migration log. |

#### `vertex admin metrics-rollup`

**Usage:** `vertex admin metrics-rollup [OPTIONS]`

Computes and appends one ISO week's aggregate for one or all raw
measurement families (Section 9.7's 13-month weekly rollup). Intended
to run on a weekly schedule (see the scheduled-tasks runbook) alongside
`vertex prefetch`/`vertex cockpit build` -- rolling up the prior
complete week is the natural cadence, but any week may be named
explicitly (e.g. to backfill a missed run).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --family TEXT | text | No |  | Restrict to one family: tier_decisions, ai_telemetry, run_telemetry. Default: all. |
| --iso-week TEXT | text | No |  | ISO week to roll up, e.g. 2026-W28. Default: the current ISO week. |

#### `vertex admin assertion`

**Usage:** `vertex admin assertion [OPTIONS] COMMAND [ARGS]...`

Author telemetry assertions for L1 reality evaluation.

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `history` |  |
| `add` |  |
| `update` |  |
| `add-evidence-url` |  |
| `export` |  |
| `composite` | Author composite assertions over existing telemetry assertions. |

#### `vertex admin assertion list`

**Usage:** `vertex admin assertion list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format [text\|json] | choice | No | text | Output format. |

#### `vertex admin assertion history`

**Usage:** `vertex admin assertion history [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | No |  | Assertion id to anchor the history lookup. |
| --metric-id TEXT | text | No |  | Metric id to inspect across assertion versions. |
| --format [text\|json] | choice | No | text | Output format. |
| --include-evaluations / --no-include-evaluations | boolean | No | True | Include linked assertion evaluation rows. |

#### `vertex admin assertion add`

**Usage:** `vertex admin assertion add [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id / --assertion-id TEXT | text | No |  | Optional explicit assertion id. |
| --query-id TEXT | text | No |  | Optional KPI query id whose catalog-linked metric_id/assertion_ids should be reused. |
| --metric-id TEXT | text | No |  | Metric id bound to this assertion. |
| --operator TEXT | text | No |  | Comparison operator, for example >= or <=. |
| --threshold FLOAT | float | No |  | Threshold value to compare against. |
| --threshold-upper FLOAT | float | No |  | Upper threshold for between assertions. |
| --baseline-value FLOAT | float | No |  | Baseline value required for percent-change assertions. |
| --baseline-captured-at TEXT | text | No |  | Optional ISO timestamp for the baseline observation. |
| --window-days INTEGER RANGE | integer range | No | 7 | Trailing observation window in days. |
| --tolerance-rel FLOAT RANGE | float range | No | 0.1 | Relative tolerance for delta magnitude. |
| --tolerance-abs FLOAT | float | No |  | Optional absolute tolerance. |
| --sustain-min-observations INTEGER RANGE | integer range | No | 3 | Consecutive violations required before challenge emission. |
| --cooldown-hours INTEGER RANGE | integer range | No | 24 | Cooldown after dismissal or resolution. |
| --severity-override TEXT | text | No |  | Optional severity override: info, warn, alert. |
| --description TEXT | text | No |  | Optional human-readable description. |
| --created-by TEXT | text | No |  | Optional actor recorded on the assertion. |

#### `vertex admin assertion update`

**Usage:** `vertex admin assertion update [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Existing assertion id. |
| --operator TEXT | text | No |  | Updated comparison operator, for example >= or <=. |
| --threshold FLOAT | float | No |  | Updated threshold value. |
| --threshold-upper FLOAT | float | No |  | Updated upper threshold for between assertions. |
| --baseline-value FLOAT | float | No |  | Updated baseline value for percent-change assertions. |
| --baseline-captured-at TEXT | text | No |  | Updated ISO timestamp for the baseline observation. |
| --clear-baseline | boolean | No | False | Clear stored baseline fields before applying other updates. |
| --window-days INTEGER RANGE | integer range | No |  | Updated trailing observation window in days. |
| --tolerance-rel FLOAT RANGE | float range | No |  | Updated relative tolerance. |
| --tolerance-abs FLOAT | float | No |  | Updated absolute tolerance. |
| --sustain-min-observations INTEGER RANGE | integer range | No |  | Updated sustained-violation count. |
| --cooldown-hours INTEGER RANGE | integer range | No |  | Updated cooldown after dismissal or resolution. |
| --severity-override TEXT | text | No |  | Updated severity override: info, warn, alert. Pass an empty string to clear. |
| --description TEXT | text | No |  | Updated human-readable description. Pass an empty string to clear. |

#### `vertex admin assertion add-evidence-url`

**Usage:** `vertex admin assertion add-evidence-url [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --binding-id TEXT | text | Yes |  | Metric source binding id. |
| --template TEXT | text | Yes |  | Evidence URL template. |

#### `vertex admin assertion export`

**Usage:** `vertex admin assertion export [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --include-history | boolean | No | False | Include archived versions and linked evaluation history. |

#### `vertex admin assertion composite`

**Usage:** `vertex admin assertion composite [OPTIONS] COMMAND [ARGS]...`

Author composite assertions over existing telemetry assertions.

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `add` |  |

#### `vertex admin assertion composite list`

**Usage:** `vertex admin assertion composite list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format [text\|json] | choice | No | text | Output format. |

#### `vertex admin assertion composite add`

**Usage:** `vertex admin assertion composite add [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id / --composite-id TEXT | text | No |  | Optional explicit composite assertion id. |
| --operator TEXT | text | Yes |  | Composite operator: and or or. |
| --child-assertion-id TEXT | text | Yes |  | Repeatable child assertion id (2-4 required). |
| --description TEXT | text | No |  | Optional human-readable description. |
| --created-by TEXT | text | No |  | Optional actor recorded on the composite assertion. |

#### `vertex admin auth`

**Usage:** `vertex admin auth [OPTIONS] COMMAND [ARGS]...`

Authentication setup commands.

**Subcommands**

| Command | Description |
|---|---|
| `setup` |  |

#### `vertex admin auth setup`

**Usage:** `vertex admin auth setup [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --tenant-id TEXT | text | No |  | Optional Entra tenant id passed to Azure CLI sign-in. |
| --use-device-code | boolean | No | False | Use Azure CLI device-code login instead of the default browser flow. |

#### `vertex admin db`

**Usage:** `vertex admin db [OPTIONS] COMMAND [ARGS]...`

Inspect and validate the L1 reality database.

**Subcommands**

| Command | Description |
|---|---|
| `verify` |  |
| `backup` |  |
| `migrate` |  |
| `compact` |  |
| `relocate` |  |

#### `vertex admin db verify`

**Usage:** `vertex admin db verify [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex admin db backup`

**Usage:** `vertex admin db backup [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --dest PATH | path | No |  | Destination directory or sqlite file path for the backup. |
| --accept-unencrypted | boolean | No | False | Allow writing the backup even when the destination volume reports encryption disabled. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex admin db migrate`

**Usage:** `vertex admin db migrate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --dry-run | boolean | No | False | List pending schema-version records without writing them. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex admin db compact`

**Usage:** `vertex admin db compact [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --dry-run | boolean | No | False | Preview compaction without writing any database changes. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex admin db relocate`

**Usage:** `vertex admin db relocate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex admin metric`

**Usage:** `vertex admin metric [OPTIONS] COMMAND [ARGS]...`

Metric binding operator commands.

**Subcommands**

| Command | Description |
|---|---|
| `bind` |  |
| `provision` |  |
| `status` |  |
| `list` |  |
| `history` |  |
| `validate` |  |

#### `vertex admin metric bind`

**Usage:** `vertex admin metric bind [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program identifier. |
| --query-id TEXT | text | Yes |  | Existing KPI query id to bind. |
| --binding-id TEXT | text | No |  | Optional explicit binding id. |
| --owner-alias TEXT | text | No |  | Optional owner alias for the binding. |
| --db-root PATH | path | No |  | Override the SQLite root for tests or local runs. |

#### `vertex admin metric provision`

**Usage:** `vertex admin metric provision [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program identifier. |
| --query-id TEXT | text | No |  | Existing KPI query id to provision. |
| --all-eligible | boolean | No | False | Provision every KPI catalog entry with enough metadata to create assertion and binding records. |
| --binding-id TEXT | text | No |  | Optional explicit binding id. |
| --assertion-id TEXT | text | No |  | Optional explicit assertion id. |
| --owner-alias TEXT | text | No |  | Optional owner alias for the binding. |
| --created-by TEXT | text | No |  | Optional actor recorded on the assertion. |
| --window-days INTEGER RANGE | integer range | No | 7 | Trailing observation window in days. |
| --tolerance-rel FLOAT RANGE | float range | No | 0.1 | Relative tolerance for delta magnitude. |
| --tolerance-abs FLOAT | float | No |  | Optional absolute tolerance. |
| --sustain-min-observations INTEGER RANGE | integer range | No | 3 | Consecutive violations required before challenge emission. |
| --cooldown-hours INTEGER RANGE | integer range | No | 24 | Cooldown after dismissal or resolution. |
| --severity-override TEXT | text | No |  | Optional severity override: info, warn, alert. |
| --description TEXT | text | No |  | Optional human-readable description. |
| --db-root PATH | path | No |  | Override the SQLite root for tests or local runs. |

#### `vertex admin metric status`

**Usage:** `vertex admin metric status [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program identifier. |
| --query-id TEXT | text | No |  | Existing KPI query id to inspect. |
| --all-eligible | boolean | No | False | Inspect rollout readiness for every KPI query eligible for deterministic provisioning. |
| --format TEXT | text | No | text | Output format: text or json. |
| --db-root PATH | path | No |  | Override the SQLite root for tests or local runs. |

#### `vertex admin metric list`

**Usage:** `vertex admin metric list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --product TEXT | text | No |  | Filter to a product id or metric-id prefix. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex admin metric history`

**Usage:** `vertex admin metric history [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --metric TEXT | text | Yes |  | Metric identifier. |
| --program TEXT | text | Yes |  | Program identifier. |
| --format TEXT | text | No | text | Output format: text or json. |
| --db-root PATH | path | No |  | Override the SQLite root for tests or local runs. |

#### `vertex admin metric validate`

**Usage:** `vertex admin metric validate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program identifier. |
| --binding-id TEXT | text | No |  | Specific binding id to validate. |
| --all | boolean | No | False | Validate all active bindings for the program. |
| --db-root PATH | path | No |  | Override the SQLite root for tests or local runs. |

### `vertex assertion`

**Usage:** `vertex assertion [OPTIONS] COMMAND [ARGS]...`

Author telemetry assertions for L1 reality evaluation.

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `history` |  |
| `add` |  |
| `update` |  |
| `add-evidence-url` |  |
| `export` |  |
| `composite` | Author composite assertions over existing telemetry assertions. |

#### `vertex assertion list`

**Usage:** `vertex assertion list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format [text\|json] | choice | No | text | Output format. |

#### `vertex assertion history`

**Usage:** `vertex assertion history [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | No |  | Assertion id to anchor the history lookup. |
| --metric-id TEXT | text | No |  | Metric id to inspect across assertion versions. |
| --format [text\|json] | choice | No | text | Output format. |
| --include-evaluations / --no-include-evaluations | boolean | No | True | Include linked assertion evaluation rows. |

#### `vertex assertion add`

**Usage:** `vertex assertion add [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id / --assertion-id TEXT | text | No |  | Optional explicit assertion id. |
| --query-id TEXT | text | No |  | Optional KPI query id whose catalog-linked metric_id/assertion_ids should be reused. |
| --metric-id TEXT | text | No |  | Metric id bound to this assertion. |
| --operator TEXT | text | No |  | Comparison operator, for example >= or <=. |
| --threshold FLOAT | float | No |  | Threshold value to compare against. |
| --threshold-upper FLOAT | float | No |  | Upper threshold for between assertions. |
| --baseline-value FLOAT | float | No |  | Baseline value required for percent-change assertions. |
| --baseline-captured-at TEXT | text | No |  | Optional ISO timestamp for the baseline observation. |
| --window-days INTEGER RANGE | integer range | No | 7 | Trailing observation window in days. |
| --tolerance-rel FLOAT RANGE | float range | No | 0.1 | Relative tolerance for delta magnitude. |
| --tolerance-abs FLOAT | float | No |  | Optional absolute tolerance. |
| --sustain-min-observations INTEGER RANGE | integer range | No | 3 | Consecutive violations required before challenge emission. |
| --cooldown-hours INTEGER RANGE | integer range | No | 24 | Cooldown after dismissal or resolution. |
| --severity-override TEXT | text | No |  | Optional severity override: info, warn, alert. |
| --description TEXT | text | No |  | Optional human-readable description. |
| --created-by TEXT | text | No |  | Optional actor recorded on the assertion. |

#### `vertex assertion update`

**Usage:** `vertex assertion update [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Existing assertion id. |
| --operator TEXT | text | No |  | Updated comparison operator, for example >= or <=. |
| --threshold FLOAT | float | No |  | Updated threshold value. |
| --threshold-upper FLOAT | float | No |  | Updated upper threshold for between assertions. |
| --baseline-value FLOAT | float | No |  | Updated baseline value for percent-change assertions. |
| --baseline-captured-at TEXT | text | No |  | Updated ISO timestamp for the baseline observation. |
| --clear-baseline | boolean | No | False | Clear stored baseline fields before applying other updates. |
| --window-days INTEGER RANGE | integer range | No |  | Updated trailing observation window in days. |
| --tolerance-rel FLOAT RANGE | float range | No |  | Updated relative tolerance. |
| --tolerance-abs FLOAT | float | No |  | Updated absolute tolerance. |
| --sustain-min-observations INTEGER RANGE | integer range | No |  | Updated sustained-violation count. |
| --cooldown-hours INTEGER RANGE | integer range | No |  | Updated cooldown after dismissal or resolution. |
| --severity-override TEXT | text | No |  | Updated severity override: info, warn, alert. Pass an empty string to clear. |
| --description TEXT | text | No |  | Updated human-readable description. Pass an empty string to clear. |

#### `vertex assertion add-evidence-url`

**Usage:** `vertex assertion add-evidence-url [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --binding-id TEXT | text | Yes |  | Metric source binding id. |
| --template TEXT | text | Yes |  | Evidence URL template. |

#### `vertex assertion export`

**Usage:** `vertex assertion export [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --include-history | boolean | No | False | Include archived versions and linked evaluation history. |

#### `vertex assertion composite`

**Usage:** `vertex assertion composite [OPTIONS] COMMAND [ARGS]...`

Author composite assertions over existing telemetry assertions.

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `add` |  |

#### `vertex assertion composite list`

**Usage:** `vertex assertion composite list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format [text\|json] | choice | No | text | Output format. |

#### `vertex assertion composite add`

**Usage:** `vertex assertion composite add [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id / --composite-id TEXT | text | No |  | Optional explicit composite assertion id. |
| --operator TEXT | text | Yes |  | Composite operator: and or or. |
| --child-assertion-id TEXT | text | Yes |  | Repeatable child assertion id (2-4 required). |
| --description TEXT | text | No |  | Optional human-readable description. |
| --created-by TEXT | text | No |  | Optional actor recorded on the composite assertion. |

### `vertex assumptions`

**Usage:** `vertex assumptions [OPTIONS] COMMAND [ARGS]...`

Manage the program assumptions register.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `add` |  |
| `validate` |  |
| `invalidate` |  |

#### `vertex assumptions list`

**Usage:** `vertex assumptions list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --status TEXT | text | No |  | Optional status filter. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex assumptions add`

**Usage:** `vertex assumptions add [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --text TEXT | text | Yes |  | Assumption text. |
| --validation-method TEXT | text | No |  | Optional validation method. |
| --validation-due TEXT | text | No |  | Optional YYYY-MM-DD validation due date. |
| --milestone TEXT | text | No |  | Optional linked milestone id. |
| --owner TEXT | text | No |  | Owner alias. Defaults to current OS user when omitted. |
| --identified-date TEXT | text | No |  | Optional YYYY-MM-DD identified date. |
| --entity-ref TEXT | text | No |  | Repeat to add entity refs. |

#### `vertex assumptions validate`

**Usage:** `vertex assumptions validate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Assumption id. |

#### `vertex assumptions invalidate`

**Usage:** `vertex assumptions invalidate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Assumption id. |
| --no-prompt | boolean | No | False | Invalidate without prompting to create a linked risk. |
| --force | boolean | No | False | Alias for --no-prompt for headless execution. |

### `vertex claims`

**Usage:** `vertex claims [OPTIONS] COMMAND [ARGS]...`

List and resolve tracked claims and decision asks.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

**Subcommands**

| Command | Description |
|---|---|
| `resolve` |  |

#### `vertex claims resolve`

**Usage:** `vertex claims resolve [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --id TEXT | text | Yes |  | Claim or decision-ask id. |
| --status TEXT | text | Yes |  | Status to record. |
| --program TEXT | text | No |  | Optional program id override. |
| --note TEXT | text | No |  | Optional resolution note. |
| --reviewer TEXT | text | No |  | Reviewer alias. Defaults to the current OS user. |

### `vertex cockpit`

**Usage:** `vertex cockpit [OPTIONS] COMMAND [ARGS]...`

Program/platform/economics/value cockpit (read-only projection).

**Subcommands**

| Command | Description |
|---|---|
| `show` |  |
| `build` | Section 10.1: the local HTML dashboard. Never a live time-travel |
| `explain` | Section 10.4: full explainability for one finding. Renders every |
| `tui` | Section 10.3a: the optional interactive terminal cockpit. Read-only |
| `compare` | Section 9.1/10.1: diffs two retained cockpit history snapshots. |
| `measure` | ADF-W2.11/W3.8/W4.8 (ADR-0017): review-latency/proposal-volume report |
| `adoption-skip` | ADF-W5.14 (ADF-OM15): explicit non-adoption reason capture. A CLI |
| `adoption` | ADF-OM15 dashboard: adoption rate + non-adoption reason breakdown |
| `autonomy-evaluate` | ADF-W5.12 (Section 8.15.1): runs the automatic evidence-based L0/L1/L2 |
| `autonomy-promote` | The explicit, human-gated path -- required for L3/L4 (Section 8.15.1's |
| `autonomy-demote` | Manual one-level demotion for a material contradiction, duplicate |

#### `vertex cockpit show`

**Usage:** `vertex cockpit show [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --edition TEXT | text | No |  | Optional edition id to scope this snapshot to. |
| --format TEXT | text | No | human | Output format: human or json. |
| --no-persist | boolean | No | False | Build and print the snapshot without writing latest.json/history (read-only preview). |

#### `vertex cockpit build`

**Usage:** `vertex cockpit build [OPTIONS]`

Section 10.1: the local HTML dashboard. Never a live time-travel
reconstruction -- ``--as-of`` reads a retained history snapshot.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --edition TEXT | text | No |  | Optional edition id to scope this snapshot to. |
| --open | boolean | No | False | Open the rendered HTML in the default browser. |
| --as-of TEXT | text | No |  | ISO timestamp: render the nearest retained history snapshot at or before this time, instead of building a fresh one. |

#### `vertex cockpit explain`

**Usage:** `vertex cockpit explain [OPTIONS]`

Section 10.4: full explainability for one finding. Renders every
field ``CockpitFinding`` structurally carries; explicitly labels the
two Section 10.4 fields it has no dedicated data for yet (calculation/
rule, and what-Vertex-did-not-do) rather than fabricating content.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --edition TEXT | text | No |  | Optional edition id to scope this snapshot to. |
| --finding TEXT | text | Yes |  | finding_id to explain. |

#### `vertex cockpit tui`

**Usage:** `vertex cockpit tui [OPTIONS]`

Section 10.3a: the optional interactive terminal cockpit. Read-only
navigation this pass (findings list + explain detail + refresh) --
never binds a port, never writes to a store, never bypasses any
mutation path (there is none in this command).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --edition TEXT | text | No |  | Optional edition id to scope this snapshot to. |

#### `vertex cockpit compare`

**Usage:** `vertex cockpit compare [OPTIONS]`

Section 9.1/10.1: diffs two retained cockpit history snapshots.
Operates ONLY on retained history (never recomputed from current
mutable state) -- if a requested time has no retained snapshot at or
before it, the comparison is unavailable, not silently substituted.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --from TEXT | text | Yes |  | ISO timestamp for the earlier snapshot. |
| --to TEXT | text | Yes |  | ISO timestamp for the later snapshot. |

#### `vertex cockpit measure`

**Usage:** `vertex cockpit measure [OPTIONS]`

ADF-W2.11/W3.8/W4.8 (ADR-0017): review-latency/proposal-volume report
computed from the proposal_audit.jsonl trail. Empty/near-empty until the
approve_*/reject_* helpers are actually called with programs_root set by
a real review flow -- this command reports what has accrued, it does
not simulate or backfill data.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --since-days INTEGER | integer | No |  | Only include decisions from the last N days (default: all-time). |
| --format TEXT | text | No | human | Output format: human or json. |

#### `vertex cockpit adoption-skip`

**Usage:** `vertex cockpit adoption-skip [OPTIONS]`

ADF-W5.14 (ADF-OM15): explicit non-adoption reason capture. A CLI
cannot observe a workflow that never ran, so this command is the
deliberate log-the-skip entry point -- an operator (or the pilot TPM
on their behalf) records why a golden workflow was not run this cadence
period, rather than the platform inferring or fabricating a reason.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --workflow TEXT | text | Yes |  | Golden workflow that was skipped this cadence: cockpit_show, cockpit_build, weekly_report, meeting_to_action, risk_dependency_review. |
| --reason TEXT | text | Yes |  | Non-adoption reason: not_applicable_this_cadence, manual_process_preferred, tool_issue, unaware, other. |

#### `vertex cockpit adoption`

**Usage:** `vertex cockpit adoption [OPTIONS]`

ADF-OM15 dashboard: adoption rate + non-adoption reason breakdown
over a recent cadence window, from real recorded adoption/non-adoption
events -- never simulated or backfilled.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --workflow TEXT | text | No |  | Restrict to one golden workflow (default: all workflows combined). |
| --since-weeks INTEGER | integer | No | 13 | Cadence window to summarize, in weeks. |
| --format TEXT | text | No | human | Output format: human or json. |

#### `vertex cockpit autonomy-evaluate`

**Usage:** `vertex cockpit autonomy-evaluate [OPTIONS]`

ADF-W5.12 (Section 8.15.1): runs the automatic evidence-based L0/L1/L2
autonomy evaluator for one or all proposal classes and persists the
result. L3/L4 require ``autonomy-promote`` (human-gated -- see that
command's help).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --class TEXT | text | No |  | Restrict to one proposal class: risk, meeting_action, top_three, governance_decision_brief, dependency_blast_radius. Default: all. |

#### `vertex cockpit autonomy-promote`

**Usage:** `vertex cockpit autonomy-promote [OPTIONS]`

The explicit, human-gated path -- required for L3/L4 (Section 8.15.1's
independent-review/outbox-proven evidence cannot be computed
automatically) and always capped at the governance-configured ceiling.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --class TEXT | text | Yes |  | One of: risk, meeting_action, top_three, governance_decision_brief, dependency_blast_radius. |
| --to TEXT | text | Yes |  | Target level: l0..l4. |
| --reason TEXT | text | Yes |  | Evidence/justification for this promotion. |
| --sample-rate FLOAT | float | No |  | L3/L4 only (Section 8.15.2): fraction of a batch a human must still individually review via `ai-proposals review-batch`, e.g. 0.2 for 20%% reviewed/80%% auto-approved. Must be between 0.05 and 1.0. Omit to keep full review (1.0) at L3/L4. |

#### `vertex cockpit autonomy-demote`

**Usage:** `vertex cockpit autonomy-demote [OPTIONS]`

Manual one-level demotion for a material contradiction, duplicate
effect, or policy violation an operator observes but the automatic
evaluator cannot detect (Section 8.15.1).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --class TEXT | text | Yes |  | One of: risk, meeting_action, top_three, governance_decision_brief, dependency_blast_radius. |
| --reason TEXT | text | Yes |  | Why this class is being demoted. |

### `vertex calibration`

**Usage:** `vertex calibration [OPTIONS] COMMAND [ARGS]...`

Inspect historical claim calibration.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --since TEXT | text | No |  | Inclusive ISO week filter, for example 2025-W01. |
| --dry-run | boolean | No | False | Render the report but skip writing forecast_calibration.yaml. |

**Subcommands**

| Command | Description |
|---|---|
| `report` |  |
| `edit-distance-trend` | Show draft↔confirm edit distance trend per task type (WS-22 learning-loop efficacy). |

#### `vertex calibration report`

**Usage:** `vertex calibration report [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --since TEXT | text | No |  | Inclusive ISO week filter, for example 2025-W01. |
| --dry-run | boolean | No | False | Render the report but skip writing forecast_calibration.yaml. |

#### `vertex calibration edit-distance-trend`

**Usage:** `vertex calibration edit-distance-trend [OPTIONS]`

Show draft↔confirm edit distance trend per task type (WS-22 learning-loop efficacy).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --window INTEGER RANGE | integer range | No | 10 | Number of most-recent confirmed issues to consider. |
| --min-issues INTEGER RANGE | integer range | No | 4 | Minimum issues required to compute a trend (default 4). |

### `vertex commitment`

**Usage:** `vertex commitment [OPTIONS] COMMAND [ARGS]...`

Manage program commitments (inbound/outbound).

**Subcommands**

| Command | Description |
|---|---|
| `list` | List commitments for a program. |
| `add` | Add a new commitment. |
| `update` | Update a commitment (slip date or status change). |

#### `vertex commitment list`

**Usage:** `vertex commitment list [OPTIONS]`

List commitments for a program.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program ID. |
| --direction TEXT | text | No |  | Filter by direction: inbound or outbound. |
| --status TEXT | text | No |  | Filter by status, e.g. active. |
| --format TEXT | text | No | human | Output format: human or json. |

#### `vertex commitment add`

**Usage:** `vertex commitment add [OPTIONS]`

Add a new commitment.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program ID. |
| --title TEXT | text | Yes |  | Commitment title. |
| --dri TEXT | text | Yes |  | DRI (alias or email). |
| --due-date TEXT | text | Yes |  | Due date (YYYY-MM-DD). |
| --direction TEXT | text | No | outbound | Direction: inbound or outbound. |
| --description TEXT | text | No |  | Optional description. |
| --entity-ref TEXT | text | No |  | Optional entity reference. |
| --status TEXT | text | No | active | Status (default: active). |

#### `vertex commitment update`

**Usage:** `vertex commitment update [OPTIONS]`

Update a commitment (slip date or status change).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program ID. |
| --id TEXT | text | Yes |  | Commitment ID. |
| --slip-to TEXT | text | No |  | New due date (YYYY-MM-DD) for slip. |
| --reason TEXT | text | No |  | Reason for slip (ref to signal/fact ID). |
| --status TEXT | text | No |  | New status. |

### `vertex context`

**Usage:** `vertex context [OPTIONS] COMMAND [ARGS]...`

NCFL context proposal extraction and review.

**Subcommands**

| Command | Description |
|---|---|
| `extract` |  |
| `proposals` |  |
| `dismiss` |  |
| `apply` | Apply one accepted NCFL proposal to its Plane 1 target store. |
| `apply-batch` | Apply all accepted NCFL proposals for an issue (batch mode). |
| `synthesize` | Zone B (Phase 5): synthesize a knowledge-doc proposal from accepted proposals + narrative. |

#### `vertex context extract`

**Usage:** `vertex context extract [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. acme_weekly. |
| --issue INTEGER RANGE | integer range | Yes |  | Confirmed issue number to extract from. |
| --dry-run | boolean | No | False | Preview extracted proposals without writing. |

#### `vertex context proposals`

**Usage:** `vertex context proposals [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. acme_weekly. |
| --issue INTEGER RANGE | integer range | No |  | Optional issue number filter. |
| --status TEXT | text | No |  | Optional status filter. |
| --format TEXT | text | No | human | Output format: human or json. |

#### `vertex context dismiss`

**Usage:** `vertex context dismiss [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. acme_weekly. |
| --proposal-id TEXT | text | Yes |  | Proposal identifier to dismiss. |
| --reason TEXT | text | Yes |  | Why the proposal is being dismissed. |
| --actor TEXT | text | No | operator | Actor recorded in the decision history. |
| --issue INTEGER RANGE | integer range | No |  | Optional issue filter to narrow the lookup. |

#### `vertex context apply`

**Usage:** `vertex context apply [OPTIONS]`

Apply one accepted NCFL proposal to its Plane 1 target store.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. acme_weekly. |
| --issue INTEGER RANGE | integer range | Yes |  | Issue number for the accepted proposals. |
| --proposal-id TEXT | text | Yes |  | Proposal ID to apply. |
| --actor TEXT | text | No | operator | Who is applying the proposal. |
| --dry-run | boolean | No | False | Preview apply without writing. |

#### `vertex context apply-batch`

**Usage:** `vertex context apply-batch [OPTIONS]`

Apply all accepted NCFL proposals for an issue (batch mode).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. acme_weekly. |
| --issue INTEGER RANGE | integer range | Yes |  | Issue number for the accepted proposals. |
| --actor TEXT | text | No | operator | Who is applying the proposals. |
| --dry-run | boolean | No | False | Preview apply without writing. |

#### `vertex context synthesize`

**Usage:** `vertex context synthesize [OPTIONS]`

Zone B (Phase 5): synthesize a knowledge-doc proposal from accepted proposals + narrative.

Reads accepted NCFL proposals for the issue and the published narrative,
asks the LLM to draft a knowledge-doc patch, enforces the ban-list, and
stages the result as a ``knowledge_doc`` ``ContextUpdateProposal``. The
proposal is never auto-applied — review with ``vertex context proposals``
and apply with ``vertex context apply``.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. acme_weekly. |
| --issue INTEGER RANGE | integer range | Yes |  | Confirmed issue number to synthesize from. |
| --dry-run | boolean | No | False | Preview the proposal without staging it. |
| --knowledge-doc TEXT | text | No | nova_program_context.md | Knowledge-doc filename to target (under programs/<id>/knowledge/). |
| --actor TEXT | text | No | operator | Actor recorded as the synthesizer. |

### `vertex config`

**Usage:** `vertex config [OPTIONS] COMMAND [ARGS]...`

Inspect and update governed program configuration.

**Subcommands**

| Command | Description |
|---|---|
| `get` |  |
| `set` |  |
| `validate` |  |
| `migrate` |  |

#### `vertex config get`

**Usage:** `vertex config get [OPTIONS] KEY`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |

#### `vertex config set`

**Usage:** `vertex config set [OPTIONS] KEY VALUE`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --dry-run | boolean | No | False | Validate and preview without writing program.yaml. |

#### `vertex config validate`

**Usage:** `vertex config validate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition id, e.g. myprogram_weekly. |
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human or json. |

#### `vertex config migrate`

**Usage:** `vertex config migrate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | No |  | Edition id, e.g. myprogram_weekly. |
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --dry-run | boolean | No | False | Preview schema updates without writing files. |

### `vertex decision-brief-pilot`

**Usage:** `vertex decision-brief-pilot [OPTIONS] COMMAND [ARGS]...`

ADF-W2.9 P5: blind A/B comparison of decision-brief-advisor's ContextCompiler/AISchemaGateway-wired pilot path against the current baseline.

**Subcommands**

| Command | Description |
|---|---|
| `compare` | Blind-compare the current decision-brief-advisor against its |
| `summary` | Report the cumulative blind-comparison tally recorded so far for |

#### `vertex decision-brief-pilot compare`

**Usage:** `vertex decision-brief-pilot compare [OPTIONS]`

Blind-compare the current decision-brief-advisor against its
ContextCompiler/AISchemaGateway-wired pilot path for every pending item,
one comparison at a time. Never affects ``decision-brief``'s own
``--ai`` output.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. acme_weekly. |
| --issue INTEGER | integer | No |  | Issue number. Defaults to the active issue. |
| --seed INTEGER | integer | No |  | Deterministic RNG seed for the A/B label order (mainly for testing). |
| --deployment TEXT | text | No |  | Override the AI deployment (else resolved from env/program config). |

#### `vertex decision-brief-pilot summary`

**Usage:** `vertex decision-brief-pilot summary [OPTIONS]`

Report the cumulative blind-comparison tally recorded so far for
``decision_brief_advisor``'s context-gateway pilot.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |

### `vertex program-synthesizer-pilot`

**Usage:** `vertex program-synthesizer-pilot [OPTIONS] COMMAND [ARGS]...`

ADF-W2.9: blind A/B comparison of program_synthesizer's ContextCompiler/AISchemaGateway-wired pilot path against the current baseline.

**Subcommands**

| Command | Description |
|---|---|
| `compare` | Blind-compare program_synthesizer's ContextCompiler/AISchemaGateway- |
| `summary` | Report the cumulative blind-comparison tally recorded so far for |

#### `vertex program-synthesizer-pilot compare`

**Usage:** `vertex program-synthesizer-pilot compare [OPTIONS]`

Blind-compare program_synthesizer's ContextCompiler/AISchemaGateway-
wired pilot path against its current ad-hoc-context baseline for one
program. Never affects any production caller of ``generate_program_
synthesis``.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --seed INTEGER | integer | No |  | Deterministic RNG seed for the A/B label order (mainly for testing). |
| --deployment TEXT | text | No |  | Override the AI deployment (else resolved from env/program config). |

#### `vertex program-synthesizer-pilot summary`

**Usage:** `vertex program-synthesizer-pilot summary [OPTIONS]`

Report the cumulative blind-comparison tally recorded so far for
``program_synthesizer``'s context-gateway pilot.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |

### `vertex connectors`

**Usage:** `vertex connectors [OPTIONS] COMMAND [ARGS]...`

External connector management (FR-SG-48).

**Subcommands**

| Command | Description |
|---|---|
| `poll` | Poll all external connectors configured in programs/{program}/slice_contracts.yaml. |

#### `vertex connectors poll`

**Usage:** `vertex connectors poll [OPTIONS]`

Poll all external connectors configured in programs/{program}/slice_contracts.yaml.

Each connector entry must specify connector_type, dep_id, source_url, and team.
Results are persisted to programs/{program}/external_dependencies.jsonl unless --dry-run.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program ID to poll connectors for. |
| --dry-run | boolean | No | False | Poll but do not persist results. |

### `vertex audit`

**Usage:** `vertex audit [OPTIONS] COMMAND [ARGS]...`

Inspect audit history and autonomy governance state.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --from TEXT | text | No |  | Inclusive start date in YYYY-MM-DD. |
| --to TEXT | text | No |  | Inclusive end date in YYYY-MM-DD. |
| --prompt-learning-summary | boolean | No | False | Append rolling calibration, prompt-version performance, and joined model/deployment summaries. |
| --window-issues INTEGER RANGE | integer range | No | 10 | Rolling issue window used when building the prompt-learning summary. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

**Subcommands**

| Command | Description |
|---|---|
| `archive` |  |
| `pause` |  |
| `rollback` |  |
| `query` | Filter the autonomy-audit JSONL and return matching events + chain status. |
| `verify-chain` | Walk the autonomy-audit hash chain and report tampering or success. |
| `excise` | Redact PII in one autonomy-audit line; the chain validator forgives the line. |

#### `vertex audit archive`

**Usage:** `vertex audit archive [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --before TEXT | text | No |  | Archive autonomy audit rows before YYYY-MM-DD. |
| --retention | boolean | No | False | Archive autonomy audit rows older than the configured retention window in program.yaml. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex audit pause`

**Usage:** `vertex audit pause [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --action-type TEXT | text | Yes |  | Action type to pause batch approval for, for example vitality_nudge. |
| --updated-by TEXT | text | No |  | Author alias for the pause audit record. |
| --dry-run | boolean | No | False | Preview the pause without changing policy state or audit history. |

#### `vertex audit rollback`

**Usage:** `vertex audit rollback [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --action TEXT | text | No |  | Original autonomy action id to roll back. |
| --batch TEXT | text | No |  | Roll back all proposal-backed autonomy actions applied on YYYY-MM-DD. Requires --program. |
| --program TEXT | text | No |  | Program id when you want to avoid action-id lookup across programs. |
| --updated-by TEXT | text | No |  | Author alias for the rollback audit record. |
| --dry-run | boolean | No | False | Preview the rollback target without applying any external writes. |

#### `vertex audit query`

**Usage:** `vertex audit query [OPTIONS]`

Filter the autonomy-audit JSONL and return matching events + chain status.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --action-type TEXT | text | No |  | Substring filter on action_type. |
| --level TEXT | text | No |  | Exact-match filter on level. |
| --action-id TEXT | text | No |  | Exact-match filter on action_id. |
| --from TEXT | text | No |  | Inclusive lower bound (YYYY-MM-DD, UTC). |
| --to TEXT | text | No |  | Inclusive upper bound (YYYY-MM-DD, UTC). |
| --limit INTEGER | integer | No |  | Truncate to the first N events. |
| --format TEXT | text | No | human | Output format: human, json, csv. |

#### `vertex audit verify-chain`

**Usage:** `vertex audit verify-chain [OPTIONS]`

Walk the autonomy-audit hash chain and report tampering or success.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human or json. |

#### `vertex audit excise`

**Usage:** `vertex audit excise [OPTIONS]`

Redact PII in one autonomy-audit line; the chain validator forgives the line.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --line INTEGER | integer | Yes |  | 1-indexed line number in autonomy_audit.jsonl. |
| --excisor TEXT | text | Yes |  | Operator name responsible for the excision. |
| --reason TEXT | text | No |  | Why this line is being redacted. |
| --dry-run | boolean | No | False | Preview without rewriting the file. |
| --format TEXT | text | No | human | Output format: human or json. |

### `vertex decisions`

**Usage:** `vertex decisions [OPTIONS] COMMAND [ARGS]...`

Manage the program decision register.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `aging` |  |
| `nudge` |  |
| `add` |  |
| `resolve` |  |
| `supersede` |  |
| `link-outcome` | Link a decision to a testable assumption premise (§6.2.8). |
| `governance` | Show and edit governance state (DFD, escalation) in issue overrides. |

#### `vertex decisions list`

**Usage:** `vertex decisions list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --status TEXT | text | No |  | Optional status filter. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex decisions aging`

**Usage:** `vertex decisions aging [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --min-age-days INTEGER | integer | No | 14 | Minimum inactive age to include in the decision debt report. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |
| --apply | boolean | No | False | Write follow-up drafts for due decision asks in the aging report. |
| --dry-run | boolean | No | False | Preview the follow-up drafts that --apply would write without creating files. |

#### `vertex decisions nudge`

**Usage:** `vertex decisions nudge [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Decision ask id. |
| --dry-run | boolean | No | False | Preview the nudge draft without writing an EML file. |

#### `vertex decisions add`

**Usage:** `vertex decisions add [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --title TEXT | text | Yes |  | Decision title. |
| --context TEXT | text | Yes |  | Decision context or problem statement. |
| --decision TEXT | text | Yes |  | Decision outcome or proposed choice. |
| --status TEXT | text | No | decided | Status: proposed\|decided\|superseded\|reverted. |
| --rationale TEXT | text | No |  | Optional rationale. |
| --alternative TEXT | text | No |  | Repeat to add alternatives considered. |
| --decided-by TEXT | text | No |  | Decision owner alias. Defaults to current OS user. |
| --decision-date TEXT | text | No |  | Optional YYYY-MM-DD decision date. Defaults to today. |
| --review-by TEXT | text | No |  | Optional YYYY-MM-DD review date for this decision. |
| --linked-claim TEXT | text | No |  | Optional linked decision-ask id. |
| --linked-risk TEXT | text | No |  | Optional linked risk id. |
| --linked-action TEXT | text | No |  | Repeat to link action ids. |
| --workstream TEXT | text | No |  | Optional workstream id. |
| --entity-ref TEXT | text | No |  | Repeat to add entity refs. |

#### `vertex decisions resolve`

**Usage:** `vertex decisions resolve [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Decision id. |
| --decision TEXT | text | No |  | Optional updated decision text. |
| --rationale TEXT | text | No |  | Optional updated rationale. |
| --decided-by TEXT | text | No |  | Decision owner alias. Defaults to current OS user. |
| --decision-date TEXT | text | No |  | Optional YYYY-MM-DD decision date. Defaults to today. |
| --review-by TEXT | text | No |  | Optional YYYY-MM-DD review date for this decision. |

#### `vertex decisions supersede`

**Usage:** `vertex decisions supersede [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Decision id. |
| --superseded-by TEXT | text | Yes |  | Replacement decision id. |

#### `vertex decisions link-outcome`

**Usage:** `vertex decisions link-outcome [OPTIONS]`

Link a decision to a testable assumption premise (§6.2.8).

Sets `expected_outcome_refs` on the decision.entry fact so that
DECISION_OUTCOME_DRIFT attention fires when the assumption becomes
disputed or stale.

Note: numeric/metric assumptions are evaluated via the §6.2.3 metric
digest. Free-text assumptions drift only via human dispute in triage.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --decision-id TEXT | text | Yes |  | Natural key or fact_id of the decision.entry to link. |
| --assumption TEXT | text | Yes |  | Natural key of the assumption.entry stating the testable premise. |

#### `vertex decisions governance`

**Usage:** `vertex decisions governance [OPTIONS] COMMAND [ARGS]...`

Show and edit governance state (DFD, escalation) in issue overrides.

**Subcommands**

| Command | Description |
|---|---|
| `show` | Show governance state from an issue's overrides. |
| `edit` | Edit governance state in an issue's overrides YAML. |

#### `vertex decisions governance show`

**Usage:** `vertex decisions governance show [OPTIONS]`

Show governance state from an issue's overrides.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name. |
| --issue INTEGER | integer | No |  | Issue number. Defaults to current draft. |
| --reports-root PATH | path | No |  | Override reports root path. |

#### `vertex decisions governance edit`

**Usage:** `vertex decisions governance edit [OPTIONS]`

Edit governance state in an issue's overrides YAML.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name. |
| --issue INTEGER | integer | Yes |  | Issue number. |
| --dfd-date TEXT | text | No |  | Set DFD date (YYYY-MM-DD). |
| --escalation-active / --no-escalation-active | boolean | No |  | Set escalation active state. |
| --lt-commitment TEXT | text | No |  | Set LT commitment text. |
| --lt-commitment-date TEXT | text | No |  | Set LT commitment date (YYYY-MM-DD). |
| --reports-root PATH | path | No |  | Override reports root path. |

### `vertex dependencies`

**Usage:** `vertex dependencies [OPTIONS] COMMAND [ARGS]...`

Inspect and manage inferred dependency proposals.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --status TEXT | text | No | proposed | Status filter: proposed, accepted, dismissed, or all. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `scout` |  |
| `accept` |  |
| `dismiss` |  |

#### `vertex dependencies list`

**Usage:** `vertex dependencies list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --status TEXT | text | No | proposed | Status filter: proposed, accepted, dismissed, or all. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex dependencies scout`

**Usage:** `vertex dependencies scout [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --edition TEXT | text | No |  | Optional edition to use for latest confirmed snapshot context. |
| --lookback-days INTEGER | integer | No | 30 | Signal lookback window in days. |
| --min-occurrences INTEGER | integer | No | 3 | Minimum repeated co-mentions required for a proposal. |
| --dry-run | boolean | No | False | Render proposals without writing dependency_proposals.yaml. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex dependencies accept`

**Usage:** `vertex dependencies accept [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Dependency proposal id. |
| --type TEXT | text | No |  | Optional override: blocks, informs, or shares_resource. |
| --risk-if-broken TEXT | text | No |  | Optional override risk text persisted to dependencies.yaml. |
| --resolution-path TEXT | text | No |  | Optional resolution path classification, for example intra_storage or cross_org_compute_pf. |

#### `vertex dependencies dismiss`

**Usage:** `vertex dependencies dismiss [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Dependency proposal id. |

### `vertex discover`

**Usage:** `vertex discover [OPTIONS] COMMAND [ARGS]...`

Discovery pipeline orchestration commands.

**Subcommands**

| Command | Description |
|---|---|
| `candidates` |  |

#### `vertex discover candidates`

**Usage:** `vertex discover candidates [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program ID receiving the discovery run. |
| --source TEXT | text | No |  | Discovery source to run. Supported: backfill_import, lt_deck, newsletter, email, sharepoint_doc, sharepoint, workiq, teams, outlook, prose_extract. |
| --pipeline TEXT | text | No |  | Discovery pipeline name when not using --result-json. |
| --batch-id TEXT | text | No |  | Candidate batch id when not using --result-json. |
| --candidate-count INTEGER RANGE | integer range | No | 0 | Number of candidates staged by the discovery run. |
| --gap-json TEXT | text | No |  | JSON object describing one gap detail; may be repeated. |
| --heartbeat / --no-heartbeat | boolean | No | True | Whether the pipeline produced a heartbeat even if no gaps/candidates were found. |
| --result-json TEXT | text | No |  | Full DiscoveryRunResult JSON payload; cannot be combined with inline fields. |
| --input-jsonl PATH | path | No |  | JSONL import source used with --source backfill_import. |
| --source-dir PATH | path | No |  | Source directory used with source-backed discovery runs. |
| --from INTEGER | integer | No |  | Optional starting year used with source-backed discovery runs. |
| --dry-run | boolean | No | False | Preview the selected source pipeline without staging candidates or recording governance events. |
| --record | boolean | No | False | Persist the resulting governance events to the ledger instead of previewing only. |
| --actor TEXT | text | No | discover_candidates | Actor recorded on governance events when --record is used. |
| --recorded-at TEXT | text | No |  | Optional recorded-at override (ISO-8601). |
| --format TEXT | text | No | text | Output format: text or json. |
| --wave INTEGER | integer | No | 1 | Extraction wave for --source prose_extract (1=decision/risk/milestone/metric; 2=phase/scope/workstream; 3=commitment/assumption/dependency/incident; 4=knowledge/sku_generation/kpi). |

### `vertex editor`

**Usage:** `vertex editor [OPTIONS] COMMAND [ARGS]...`

Editorial evaluation commands.

**Subcommands**

| Command | Description |
|---|---|
| `report` | Run standalone editorial evaluation and produce per-persona pass/fail summary. |

#### `vertex editor report`

**Usage:** `vertex editor report [OPTIONS]`

Run standalone editorial evaluation and produce per-persona pass/fail summary.

Exit codes:
  0 — no findings at or above warn (clean; only info)
  2 — one or more warn-severity findings, no blocking failure
  3 — at least one block-severity gate failed (after its enforce_after date)

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name. |
| --issue INTEGER RANGE | integer range | No |  | Issue number (defaults to current). |
| --format TEXT | text | No | human | Output format: human or json. |

### `vertex entity-aliases`

**Usage:** `vertex entity-aliases [OPTIONS] COMMAND [ARGS]...`

Inspect unresolved entity aliases in a program's fact store.

**Subcommands**

| Command | Description |
|---|---|
| `pending` | List entity_refs in the fact snapshot that cannot be resolved by the entity registry. |

#### `vertex entity-aliases pending`

**Usage:** `vertex entity-aliases pending [OPTIONS]`

List entity_refs in the fact snapshot that cannot be resolved by the entity registry.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to inspect. |

### `vertex facts`

**Usage:** `vertex facts [OPTIONS] COMMAND [ARGS]...`

Manage program fact store (export, import, rebuild).

**Subcommands**

| Command | Description |
|---|---|
| `export` | Export the current fact snapshot to JSON (ProgramFactEnvelope). |
| `import` | Import facts from a ProgramFactEnvelope JSON file into the fact store. |
| `rebuild` | Rebuild the fact store from canonical program files. |
| `parity-check` | Compare current legacy projections against current fact-store projections. |
| `pin-snapshot` | Pin the current fact snapshot to a confirmed issue (spec §22, Step 8). |
| `detect-drift` | List fact revisions that drifted after a pin (spec §22, Step 8). |
| `dual-read-log` | Sustained dual-read shadow window per spec §22. |
| `backfill-observations` | Backfill signal.observation facts from archive/extractor data (WI-3.8). |
| `backfill-judgments` | GAP-34 (F4): backfill override risk choices into the fact store as ``judgment.dimension`` facts. |

#### `vertex facts export`

**Usage:** `vertex facts export [OPTIONS]`

Export the current fact snapshot to JSON (ProgramFactEnvelope).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --output PATH | path | No |  | Output JSON file path (default: stdout). |

#### `vertex facts import`

**Usage:** `vertex facts import [OPTIONS]`

Import facts from a ProgramFactEnvelope JSON file into the fact store.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --input PATH | path | Yes |  | JSON file previously created by 'facts export'. |
| --dry-run | boolean | No | False | Parse and validate without writing. |

#### `vertex facts rebuild`

**Usage:** `vertex facts rebuild [OPTIONS]`

Rebuild the fact store from canonical program files.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --dry-run | boolean | No | False | Show what would be rebuilt without writing. |

#### `vertex facts parity-check`

**Usage:** `vertex facts parity-check [OPTIONS]`

Compare current legacy projections against current fact-store projections.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |

#### `vertex facts pin-snapshot`

**Usage:** `vertex facts pin-snapshot [OPTIONS]`

Pin the current fact snapshot to a confirmed issue (spec §22, Step 8).

Creates a row in the ``fact_snapshot_pins`` table with the current
fact-snapshot ID and the issue number.  ``detect_drift(snapshot_id)`` then
reports any post-pin fact writes as material drift, so the operator can
prove that a confirmed issue's fact-state was not retroactively changed.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --issue-number INTEGER | integer | Yes |  | Confirmed issue number to pin the fact snapshot to. |

#### `vertex facts detect-drift`

**Usage:** `vertex facts detect-drift [OPTIONS]`

List fact revisions that drifted after a pin (spec §22, Step 8).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --snapshot-id TEXT | text | Yes |  | Pin ID returned by `pin-snapshot`. |

#### `vertex facts dual-read-log`

**Usage:** `vertex facts dual-read-log [OPTIONS]`

Sustained dual-read shadow window per spec §22.

Runs ``cycles`` parity-check passes (legacy + Fact Store) and appends one
JSONL record per cycle to ``programs/<prog>/fact_store_parity_log.jsonl``.
Mismatched family items (if --quarantine) are appended to a sibling
``fact_store_quarantine.jsonl`` for offline review.  Operator-run, not a
daemon: the spec calls for a *minimum* of 2 full confirmed cycles, so
operators run this command between confirm runs as the live proof artifact.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --cycles INTEGER RANGE | integer range | No | 2 | Number of parity-check cycles to run in this window. |
| --interval FLOAT | float | No | 0.0 | Sleep between cycles (seconds). |
| --quarantine / --no-quarantine | boolean | No | True | Write mismatched family facts to a quarantine JSONL file. |

#### `vertex facts backfill-observations`

**Usage:** `vertex facts backfill-observations [OPTIONS]`

Backfill signal.observation facts from archive/extractor data (WI-3.8).

Reads existing program facts and re-promotes them as signal.observation
records. All backfilled facts are:
- tagged `backfilled: true` in the payload
- truth-capped at SOURCE_VALIDATED (never CORROBORATED or above)
- written via append_fact (idempotent)

This command NEVER blocks Phase 4+ work — it is a convenience backfill
for populating the observation layer from existing tracked data.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --dry-run | boolean | No | False | Print what would be backfilled without writing. |
| --limit INTEGER | integer | No | 500 | Max number of facts to backfill in one run. |

#### `vertex facts backfill-judgments`

**Usage:** `vertex facts backfill-judgments [OPTIONS]`

GAP-34 (F4): backfill override risk choices into the fact store as ``judgment.dimension`` facts.

Scans ``programs/<id>/**/overrides/issue_*.yaml`` (including archived
per-edition overrides), extracts one ``Judgment`` per non-needs-input
dimension, and — unless ``--dry-run`` — appends each as a
``judgment.dimension`` fact via the canonical Program Fact Store append
path. Re-runs are idempotent: the fact natural key encodes
``program | issue | edition | dimension``.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --dry-run | boolean | No | False | Discover and print judgments without writing them to the fact store. |

### `vertex kb`

**Usage:** `vertex kb [OPTIONS] COMMAND [ARGS]...`

Knowledge base diagnostics and history.

**Subcommands**

| Command | Description |
|---|---|
| `changelog` |  |
| `update` |  |
| `profiles` | Protect or unwrap sensitive people profile files. |

#### `vertex kb changelog`

**Usage:** `vertex kb changelog [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --since TEXT | text | Yes |  | ISO week in YYYY-Www format. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex kb update`

**Usage:** `vertex kb update [OPTIONS] CORRECTION`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id. Inferred when only one program exists. |
| --apply | boolean | No | False | Write the validated KB change. |
| --ai / --no-ai | boolean | No | True | Allow AI planning when deterministic parsing is insufficient. |

#### `vertex kb profiles`

**Usage:** `vertex kb profiles [OPTIONS] COMMAND [ARGS]...`

Protect or unwrap sensitive people profile files.

**Subcommands**

| Command | Description |
|---|---|
| `encrypt` |  |
| `decrypt` |  |

#### `vertex kb profiles encrypt`

**Usage:** `vertex kb profiles encrypt [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --scope TEXT | text | No | active | Target active, shared, or program-scoped people_profiles.yaml. |

#### `vertex kb profiles decrypt`

**Usage:** `vertex kb profiles decrypt [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --scope TEXT | text | No | active | Target active, shared, or program-scoped people_profiles.yaml. |

### `vertex knowledge`

**Usage:** `vertex knowledge [OPTIONS] COMMAND [ARGS]...`

Knowledge plane authoring and inspection commands.

**Subcommands**

| Command | Description |
|---|---|
| `assert` |  |
| `supersede` |  |
| `redact` |  |
| `redact-vault` |  |
| `gc` |  |
| `ingest` |  |
| `extract` |  |
| `quarantine-batch` |  |
| `show` |  |
| `predicates` |  |
| `status` |  |
| `triage` | Review staged knowledge claim candidates. |

#### `vertex knowledge assert`

**Usage:** `vertex knowledge assert [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --scope TEXT | text | Yes |  | Knowledge scope, e.g. domain:storage-platform. |
| --subject TEXT | text | Yes |  | Subject entity id. |
| --predicate TEXT | text | Yes |  | Registered knowledge predicate. |
| --value TEXT | text | No |  | String value for the claim. |
| --value-json TEXT | text | No |  | JSON-encoded value. Use 'null' for tombstones. |
| --valid-from TEXT | text | No |  | Validity start (ISO date or datetime). Defaults to now. |
| --valid-until TEXT | text | No |  | Validity end (ISO date or datetime). |
| --actor TEXT | text | Yes |  | Operator alias writing the claim. |

#### `vertex knowledge supersede`

**Usage:** `vertex knowledge supersede [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --scope TEXT | text | No |  | Knowledge scope, e.g. domain:storage-platform. |
| --subject TEXT | text | No |  | Subject entity id. |
| --predicate TEXT | text | No |  | Registered knowledge predicate. |
| --claim-id TEXT | text | No |  | Existing claim revision ULID to supersede. |
| --value TEXT | text | No |  | String value for the replacement claim. |
| --value-json TEXT | text | No |  | JSON-encoded value. Use 'null' for tombstones. |
| --valid-from TEXT | text | No |  | Validity start (ISO date or datetime). Defaults to now. |
| --valid-until TEXT | text | No |  | Validity end (ISO date or datetime). |
| --reason TEXT | text | Yes |  | Operator reason for the supersession. |
| --actor TEXT | text | Yes |  | Operator alias writing the claim. |

#### `vertex knowledge redact`

**Usage:** `vertex knowledge redact [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --claim-id TEXT | text | Yes |  | Existing claim revision ULID to redact. |
| --reason TEXT | text | Yes |  | Compliance reason for redaction. |
| --actor TEXT | text | Yes |  | Operator performing the redaction. |
| --backup-root PATH | path | No |  | Optional root directory containing backup snapshots to inspect. |

#### `vertex knowledge redact-vault`

**Usage:** `vertex knowledge redact-vault [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --vault-hash TEXT | text | Yes |  | Knowledge vault hash to destroy and cascade-redact. |
| --reason TEXT | text | Yes |  | Compliance reason for vault redaction. |
| --actor TEXT | text | Yes |  | Operator performing the redaction. |
| --backup-root PATH | path | No |  | Optional root directory containing backup snapshots to inspect. |

#### `vertex knowledge gc`

**Usage:** `vertex knowledge gc [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --dry-run | boolean | No | False | Preview unreferenced vault entries without deleting them. |
| --older-than-days INTEGER | integer | No | 90 | Minimum age in days before an unreferenced vault entry is collectible. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex knowledge ingest`

**Usage:** `vertex knowledge ingest [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --source PATH | path | Yes |  | Local source document to ingest into the knowledge vault. |
| --scope TEXT | text | Yes |  | Knowledge scope to register the source under. |

#### `vertex knowledge extract`

**Usage:** `vertex knowledge extract [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --scope TEXT | text | Yes |  | Knowledge scope to extract from. |
| --source TEXT | text | No |  | Optional vault hash to extract from. |
| --dry-run | boolean | No | False | Preview candidates without writing pending.jsonl. |

#### `vertex knowledge quarantine-batch`

**Usage:** `vertex knowledge quarantine-batch [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --batch-id TEXT | text | Yes |  | Batch ID to quarantine. |
| --actor TEXT | text | Yes |  | Operator quarantining the batch. |
| --reason TEXT | text | Yes |  | Reason for quarantining the batch. |

#### `vertex knowledge show`

**Usage:** `vertex knowledge show [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --entity TEXT | text | Yes |  | Entity id to resolve. |
| --program TEXT | text | Yes |  | Program id used to resolve scope chain. |
| --as-of TEXT | text | No |  | Occurred-time cutoff (ISO date or datetime). |
| --knowledge-as-of TEXT | text | No |  | Knowledge-time cutoff (ISO date or datetime). |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex knowledge predicates`

**Usage:** `vertex knowledge predicates [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex knowledge status`

**Usage:** `vertex knowledge status [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex knowledge triage`

**Usage:** `vertex knowledge triage [OPTIONS] COMMAND [ARGS]...`

Review staged knowledge claim candidates.

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `approve` |  |
| `edit` |  |
| `batch-approve` |  |
| `batch-status` |  |
| `reject` |  |
| `skip` |  |
| `expire-skips` |  |

#### `vertex knowledge triage list`

**Usage:** `vertex knowledge triage list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --scope TEXT | text | No |  | Optional scope filter. |
| --batch-id TEXT | text | No |  | Optional batch filter. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex knowledge triage approve`

**Usage:** `vertex knowledge triage approve [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --candidate TEXT | text | Yes |  | Candidate ID to approve. |
| --actor TEXT | text | Yes |  | Operator approving the candidate. |

#### `vertex knowledge triage edit`

**Usage:** `vertex knowledge triage edit [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --candidate TEXT | text | Yes |  | Candidate ID to edit and approve. |
| --actor TEXT | text | Yes |  | Operator editing the candidate. |
| --subject TEXT | text | No |  | Replacement subject entity id. |
| --predicate TEXT | text | No |  | Replacement predicate. |
| --value TEXT | text | No |  | Replacement string value. |
| --value-json TEXT | text | No |  | Replacement JSON value. Use 'null' for tombstones. |
| --valid-from TEXT | text | No |  | Replacement validity start (ISO date or datetime). |
| --valid-until TEXT | text | No |  | Replacement validity end (ISO date or datetime). |
| --reason TEXT | text | No |  | Optional edit rationale. |

#### `vertex knowledge triage batch-approve`

**Usage:** `vertex knowledge triage batch-approve [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --batch-id TEXT | text | Yes |  | Batch ID to approve. |
| --actor TEXT | text | Yes |  | Operator approving the batch. |
| --min-confidence FLOAT | float | No | 0.9 | Minimum extraction confidence required for auto-approval. |

#### `vertex knowledge triage batch-status`

**Usage:** `vertex knowledge triage batch-status [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --batch-id TEXT | text | Yes |  | Batch ID to summarize. |
| --min-confidence FLOAT | float | No | 0.9 | Minimum extraction confidence required for auto-approval. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex knowledge triage reject`

**Usage:** `vertex knowledge triage reject [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --candidate TEXT | text | Yes |  | Candidate ID to reject. |
| --actor TEXT | text | Yes |  | Operator rejecting the candidate. |
| --reason TEXT | text | No |  | Optional rejection reason. |

#### `vertex knowledge triage skip`

**Usage:** `vertex knowledge triage skip [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --candidate TEXT | text | Yes |  | Candidate ID to skip for now. |
| --actor TEXT | text | Yes |  | Operator skipping the candidate. |
| --reason TEXT | text | No |  | Optional skip reason. |

#### `vertex knowledge triage expire-skips`

**Usage:** `vertex knowledge triage expire-skips [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --actor TEXT | text | No | vertex.knowledge.expire_skips | Actor materializing expired skips. |

### `vertex ledger`

**Usage:** `vertex ledger [OPTIONS] COMMAND [ARGS]...`

Manage append-only program ledger state.

**Subcommands**

| Command | Description |
|---|---|
| `write` |  |
| `correct` |  |
| `lock` |  |
| `unlock` |  |
| `status` |  |
| `quarantine-batch` |  |
| `history` |  |
| `gaps` |  |
| `replay` |  |
| `verify` |  |
| `diff` |  |
| `export` |  |
| `import` |  |
| `redact` | Redact a single ledger event payload in-place (§10.8 compliance redaction). |
| `redact-vault` | Destroy a ledger evidence vault entry and cascade-redact all referencing events (§10.8). |
| `backfill` |  |
| `triage` | Review staged ledger candidates. |

#### `vertex ledger write`

**Usage:** `vertex ledger write [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --event-type TEXT | text | Yes |  | Registered ledger event type to write. |
| --occurred-at TEXT | text | Yes |  | Occurred-at timestamp (ISO-8601). |
| --actor TEXT | text | Yes |  | Actor writing the event. |
| --payload-json TEXT | text | Yes |  | JSON object payload for the event. |
| --source-ref-json TEXT | text | Yes |  | JSON object representing the SourceRef. |
| --confidence TEXT | text | No | operator_confirmed | Confidence tier. |
| --temporal-confidence TEXT | text | No | exact | Temporal confidence tier. |
| --corroborating-refs-json TEXT | text | No |  | Optional JSON array of corroborating SourceRefs. |

#### `vertex ledger correct`

**Usage:** `vertex ledger correct [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --event-id TEXT | text | Yes |  | Event ID being corrected. |
| --actor TEXT | text | Yes |  | Operator applying the correction. |
| --reason TEXT | text | Yes |  | Correction reason. |
| --corrected-payload-json TEXT | text | Yes |  | JSON object payload or JSON null for tombstone. |

#### `vertex ledger lock`

**Usage:** `vertex ledger lock [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --entity-id TEXT | text | Yes |  | Entity to lock. |
| --field TEXT | text | Yes |  | Field name to lock. |
| --actor TEXT | text | Yes |  | Operator applying the lock. |
| --locked-value-json TEXT | text | No |  | Optional JSON value to pin. |
| --valid-until TEXT | text | No |  | Optional ISO-8601 expiry timestamp. |
| --reason TEXT | text | No |  | Optional lock reason. |
| --override-session-id TEXT | text | No |  | Optional override session ID. |

#### `vertex ledger unlock`

**Usage:** `vertex ledger unlock [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --entity-id TEXT | text | Yes |  | Entity to unlock. |
| --field TEXT | text | Yes |  | Field name to unlock. |
| --actor TEXT | text | Yes |  | Operator removing the lock. |
| --reason TEXT | text | No |  | Optional unlock reason. |
| --override-session-id TEXT | text | No |  | Optional override session ID. |

#### `vertex ledger status`

**Usage:** `vertex ledger status [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to inspect. |
| --batch-id TEXT | text | No |  | Filter active candidates to a single batch. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex ledger quarantine-batch`

**Usage:** `vertex ledger quarantine-batch [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --batch-id TEXT | text | Yes |  | Batch ID to quarantine. |
| --actor TEXT | text | Yes |  | Operator quarantining the batch. |
| --reason TEXT | text | Yes |  | Reason for quarantining the batch. |

#### `vertex ledger history`

**Usage:** `vertex ledger history [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to inspect. |
| --entity TEXT | text | Yes |  | Entity ID to show timeline for. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex ledger gaps`

**Usage:** `vertex ledger gaps [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to inspect. |
| --unacknowledged-only / --all | boolean | No | True | Show only unacknowledged gaps by default. |
| --ack TEXT | text | No |  | Acknowledge a specific gap event ID before listing. |
| --actor TEXT | text | No |  | Actor acknowledging the gap when --ack is used. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex ledger replay`

**Usage:** `vertex ledger replay [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to rebuild. |
| --as-of TEXT | text | No |  | Optional occurred_at cutoff (ISO-8601). |
| --knowledge-as-of TEXT | text | No |  | Optional recorded_at cutoff (ISO-8601). |
| --reindex | boolean | No | False | Rebuild the derived event index before projection replay. |
| --family TEXT | text | No |  | Selective bridge re-projection: re-run the fact-store bridge only for events whose fact_family matches. Repeatable (e.g. --family milestone --family risk). When omitted the full ledger projection is rebuilt. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex ledger verify`

**Usage:** `vertex ledger verify [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to inspect. |
| --deep | boolean | No | False | Also compare current projection to a fresh replay dump. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex ledger diff`

**Usage:** `vertex ledger diff [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to inspect. |
| --from TEXT | text | Yes |  | Earlier occurred_at cutoff (ISO-8601). |
| --to TEXT | text | Yes |  | Later occurred_at cutoff (ISO-8601). |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex ledger export`

**Usage:** `vertex ledger export [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to export. |
| --format TEXT | text | Yes |  | Export format: jsonl or sqlite. |
| --out PATH | path | Yes |  | Destination path. |

#### `vertex ledger import`

**Usage:** `vertex ledger import [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to stage into. |
| --source PATH | path | Yes |  | Source JSONL file to import into the candidate queue. |
| --dry-run | boolean | No | False | Preview staged candidates without writing pending.jsonl. |
| --sample-limit INTEGER RANGE | integer range | No | 3 | How many sample candidates to print in dry-run output. |

#### `vertex ledger redact`

**Usage:** `vertex ledger redact [OPTIONS]`

Redact a single ledger event payload in-place (§10.8 compliance redaction).

The event envelope is preserved; only the payload is replaced with {redacted: true}.
Hash-chain continuity is maintained via the .redactions.jsonl registry.
This is the ONLY physical mutation allowed in the ledger subsystem.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID. |
| --event-id TEXT | text | Yes |  | Event ID to redact. |
| --reason TEXT | text | Yes |  | Compliance reason for redaction. |
| --actor TEXT | text | Yes |  | Operator performing the redaction. |
| --scrub-field TEXT | text | No |  | Source field to blank in source_ref/corroborating_refs (repeatable). |

#### `vertex ledger redact-vault`

**Usage:** `vertex ledger redact-vault [OPTIONS]`

Destroy a ledger evidence vault entry and cascade-redact all referencing events (§10.8).

Deletes the vault content + metadata files, then redacts every ledger event
whose source_ref or corroborating_refs cite this vault_hash.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID. |
| --vault-hash TEXT | text | Yes |  | Evidence vault hash to destroy and cascade-redact. |
| --reason TEXT | text | Yes |  | Compliance reason for vault redaction. |
| --actor TEXT | text | Yes |  | Operator performing the redaction. |

#### `vertex ledger backfill`

**Usage:** `vertex ledger backfill [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to stage into. |
| --source-dir PATH | path | No |  | Root directory containing backfill source files. |
| --quarantine-batch TEXT | text | No |  | Quarantine a previously staged backfill batch instead of staging a new one. |
| --actor TEXT | text | No |  | Operator name for --quarantine-batch. |
| --reason TEXT | text | No |  | Reason for --quarantine-batch. |
| --from INTEGER | integer | No |  | Optional starting year for recursive Tier-A enumeration. |
| --dry-run | boolean | No | False | Preview staged candidates without writing pending.jsonl. |
| --sample-limit INTEGER RANGE | integer range | No | 3 | How many sample candidates to print in dry-run output. |

#### `vertex ledger triage`

**Usage:** `vertex ledger triage [OPTIONS] COMMAND [ARGS]...`

Review staged ledger candidates.

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `batch-status` |  |
| `batch-approve` |  |
| `batch-reject` | Reject every active candidate in a batch (activation.md §6.14.15 / O-21). |
| `approve` |  |
| `edit` |  |
| `reject` |  |
| `revoke` |  |
| `skip` |  |
| `expire-skips` |  |

#### `vertex ledger triage list`

**Usage:** `vertex ledger triage list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to inspect. |
| --batch-id TEXT | text | No |  | Optional batch filter. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex ledger triage batch-status`

**Usage:** `vertex ledger triage batch-status [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to inspect. |
| --batch-id TEXT | text | Yes |  | Batch ID to summarize. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex ledger triage batch-approve`

**Usage:** `vertex ledger triage batch-approve [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --batch-id TEXT | text | Yes |  | Batch ID to approve. |
| --actor TEXT | text | Yes |  | Operator approving the batch. |

#### `vertex ledger triage batch-reject`

**Usage:** `vertex ledger triage batch-reject [OPTIONS]`

Reject every active candidate in a batch (activation.md §6.14.15 / O-21).

Batch judgment is the operator's real surface for a returning-from-PTO
backlog: a filtered bulk-reject keeps the time-motion ROI budget intact
when ~20% of facts need rejection. Each rejection writes its own audit
event + triage decision (with telemetry); rejections are terminal, so no
outbox enqueue or projection is needed.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --batch-id TEXT | text | Yes |  | Batch ID to reject. |
| --actor TEXT | text | Yes |  | Operator rejecting the batch. |
| --reason TEXT | text | No |  | Optional rejection reason applied to every candidate. |

#### `vertex ledger triage approve`

**Usage:** `vertex ledger triage approve [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --candidate TEXT | text | Yes |  | Candidate ID to approve. |
| --actor TEXT | text | Yes |  | Operator approving the candidate. |
| --override-lock | boolean | No | False | Temporarily unlock and relock a single conflicting field for this approval. |

#### `vertex ledger triage edit`

**Usage:** `vertex ledger triage edit [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --candidate TEXT | text | Yes |  | Candidate ID to edit and approve. |
| --actor TEXT | text | Yes |  | Operator editing the candidate. |
| --payload-json TEXT | text | Yes |  | Replacement JSON object payload for the resulting event. |
| --occurred-at TEXT | text | No |  | Optional replacement occurred-at timestamp (ISO-8601). |
| --reason TEXT | text | No |  | Optional edit rationale. |
| --override-lock | boolean | No | False | Temporarily unlock and relock a single conflicting field for this approval. |

#### `vertex ledger triage reject`

**Usage:** `vertex ledger triage reject [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --candidate TEXT | text | Yes |  | Candidate ID to reject. |
| --actor TEXT | text | Yes |  | Operator rejecting the candidate. |
| --reason TEXT | text | No |  | Optional rejection reason. |

#### `vertex ledger triage revoke`

**Usage:** `vertex ledger triage revoke [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --candidate TEXT | text | Yes |  | Approved candidate ID to revoke. |
| --actor TEXT | text | Yes |  | Operator revoking the approved candidate. |
| --reason TEXT | text | Yes |  | Revocation reason. |

#### `vertex ledger triage skip`

**Usage:** `vertex ledger triage skip [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --candidate TEXT | text | Yes |  | Candidate ID to skip for now. |
| --actor TEXT | text | Yes |  | Operator skipping the candidate. |
| --reason TEXT | text | No |  | Optional skip reason. |

#### `vertex ledger triage expire-skips`

**Usage:** `vertex ledger triage expire-skips [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID to mutate. |
| --actor TEXT | text | No | vertex.ledger.expire_skips | Actor materializing expired skips. |

### `vertex inspect`

**Usage:** `vertex inspect [OPTIONS] COMMAND [ARGS]...`

Inspect runtime state for deterministic command surfaces.

**Subcommands**

| Command | Description |
|---|---|
| `kusto` |  |

#### `vertex inspect kusto`

**Usage:** `vertex inspect kusto [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --query TEXT | text | No |  | Optional Kusto query id to inspect. |
| --format TEXT | text | No | table | Output format: table or json. |
| --since TEXT | text | No |  | Optional success recency window, for example 7d. |

### `vertex integration`

**Usage:** `vertex integration [OPTIONS] COMMAND [ARGS]...`

Inspect and manage the unified integration registry.

**Subcommands**

| Command | Description |
|---|---|
| `show` |  |
| `candidates` |  |
| `seed-id` |  |
| `seed-plan` |  |
| `candidate-accept` |  |
| `candidate-reject` |  |
| `candidate-clear-rejection` |  |
| `candidate-reassign` |  |
| `intent-suppress` |  |
| `intent-retire` |  |
| `intent-clear-suppression` |  |
| `intent-reopen` |  |
| `explain-source` |  |
| `diff` |  |
| `retire` |  |
| `suppress` |  |
| `confirm` |  |
| `promote` |  |
| `signal-yield` |  |
| `reassign` | Reassign workstream attribution for a UIL channel registration. |
| `ref-id` | Migrate a UIL registration to a new ref_id (e.g. after a Teams thread rotation). |
| `discover` |  |
| `migrate` |  |
| `schema-migrate` | Handle non-additive schema migrations after a code upgrade. |
| `backup` |  |
| `restore` |  |
| `prune` | Delete RETIRED and SUPPRESSED registrations older than the retention window. |

#### `vertex integration show`

**Usage:** `vertex integration show [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --channel TEXT | text | No |  | Filter to one channel. |
| --provider-instance TEXT | text | No |  | Filter to one provider instance. |
| --reveal-titles | boolean | No | False | Show stored plaintext titles. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration candidates`

**Usage:** `vertex integration candidates [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --status TEXT | text | No |  | Filter by candidate status. |
| --workstream TEXT | text | No |  | Filter by workstream id. |
| --source-type TEXT | text | No |  | Filter by source type / ref kind. |
| --requires-decision | boolean | No | False | Show only candidates that still need a PM decision. |
| --json | boolean | No | False | Emit JSON instead of a human table. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration seed-id`

**Usage:** `vertex integration seed-id [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --intent-id TEXT | text | Yes |  | Target source intent id. |
| --ref-id TEXT | text | Yes |  | Durable source identifier to seed. |
| --pm-alias TEXT | text | Yes |  | PM/operator alias authorising the seed. |
| --reason TEXT | text | No |  | Optional rationale for the seeded binding. |
| --provider-instance TEXT | text | No | default | Provider instance id to bind. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration seed-plan`

**Usage:** `vertex integration seed-plan [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --json | boolean | No | False | Emit JSON instead of a human checklist. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration candidate-accept`

**Usage:** `vertex integration candidate-accept [OPTIONS] CANDIDATE_ID`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --pm-alias TEXT | text | Yes |  | PM/operator alias authorising the decision. |
| --intent-id TEXT | text | No |  | Explicit intent id when a candidate matches multiple intents. |
| --reason TEXT | text | No |  | Optional rationale for the accepted binding. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration candidate-reject`

**Usage:** `vertex integration candidate-reject [OPTIONS] CANDIDATE_ID`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --pm-alias TEXT | text | Yes |  | PM/operator alias authorising the decision. |
| --reason TEXT | text | Yes |  | Why this candidate should be rejected. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration candidate-clear-rejection`

**Usage:** `vertex integration candidate-clear-rejection [OPTIONS] CANDIDATE_ID`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --pm-alias TEXT | text | Yes |  | PM/operator alias authorising the decision. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration candidate-reassign`

**Usage:** `vertex integration candidate-reassign [OPTIONS] CANDIDATE_ID`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --workstream TEXT | text | Yes |  | Target workstream id. |
| --pm-alias TEXT | text | Yes |  | PM/operator alias authorising the reassignment. |
| --from-intent-id TEXT | text | No |  | Explicit current intent id when a candidate matches multiple intents. |
| --reason TEXT | text | No |  | Optional rationale for the reassignment. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration intent-suppress`

**Usage:** `vertex integration intent-suppress [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --workstream TEXT | text | Yes |  | Workstream id. |
| --kind TEXT | text | Yes |  | Source kind. |
| --name TEXT | text | Yes |  | Intent display name. |
| --pm-alias TEXT | text | Yes |  | PM/operator alias authorising the decision. |
| --reason TEXT | text | Yes |  | Why this source should be suppressed. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration intent-retire`

**Usage:** `vertex integration intent-retire [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --workstream TEXT | text | Yes |  | Workstream id. |
| --kind TEXT | text | Yes |  | Source kind. |
| --name TEXT | text | Yes |  | Intent display name. |
| --pm-alias TEXT | text | Yes |  | PM/operator alias authorising the decision. |
| --reason TEXT | text | Yes |  | Why this source should be retired. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration intent-clear-suppression`

**Usage:** `vertex integration intent-clear-suppression [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --workstream TEXT | text | Yes |  | Workstream id. |
| --kind TEXT | text | Yes |  | Source kind. |
| --name TEXT | text | Yes |  | Intent display name. |
| --pm-alias TEXT | text | Yes |  | PM/operator alias authorising the decision. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration intent-reopen`

**Usage:** `vertex integration intent-reopen [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --workstream TEXT | text | Yes |  | Workstream id. |
| --kind TEXT | text | Yes |  | Source kind. |
| --name TEXT | text | Yes |  | Intent display name. |
| --pm-alias TEXT | text | Yes |  | PM/operator alias authorising the decision. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration explain-source`

**Usage:** `vertex integration explain-source [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --intent-id TEXT | text | No |  | Explain one source intent. |
| --ref-id TEXT | text | No |  | Explain the candidate/source carrying this durable ref id. |
| --ref-kind TEXT | text | No |  | Ref kind used with --ref-id. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration diff`

**Usage:** `vertex integration diff [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --channel TEXT | text | Yes |  | Channel to inspect. |
| --provider-instance TEXT | text | No |  | Filter to one provider instance. |
| --history INTEGER RANGE | integer range | No | 1 | Number of deltas to show. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration retire`

**Usage:** `vertex integration retire [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --channel TEXT | text | Yes |  | Channel name. |
| --ref-id TEXT | text | Yes |  | External reference id. |
| --ref-kind TEXT | text | No | work_item | Reference kind. |
| --provider-instance TEXT | text | No |  | Target one provider instance. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration suppress`

**Usage:** `vertex integration suppress [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --channel TEXT | text | Yes |  | Channel name. |
| --ref-id TEXT | text | Yes |  | External reference id. |
| --ref-kind TEXT | text | No | work_item | Reference kind. |
| --provider-instance TEXT | text | No |  | Target one provider instance. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration confirm`

**Usage:** `vertex integration confirm [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --channel TEXT | text | Yes |  | Channel name. |
| --ref-id TEXT | text | Yes |  | External reference id. |
| --ref-kind TEXT | text | No | work_item | Reference kind. |
| --provider-instance TEXT | text | No |  | Target one provider instance. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration promote`

**Usage:** `vertex integration promote [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --channel TEXT | text | Yes |  | Channel name. |
| --ref-id TEXT | text | Yes |  | External reference id. |
| --ref-kind TEXT | text | No | work_item | Reference kind. |
| --provider-instance TEXT | text | No |  | Target one provider instance. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration signal-yield`

**Usage:** `vertex integration signal-yield [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --channel TEXT | text | Yes |  | Channel name. |
| --ref-id TEXT | text | Yes |  | External reference id. |
| --ref-kind TEXT | text | No | work_item | Reference kind. |
| --count INTEGER RANGE | integer range | Yes |  | Newest signal-yield count to record. |
| --provider-instance TEXT | text | No |  | Target one provider instance. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration reassign`

**Usage:** `vertex integration reassign [OPTIONS]`

Reassign workstream attribution for a UIL channel registration.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --channel TEXT | text | Yes |  | Channel name. |
| --ref-id TEXT | text | Yes |  | External reference id. |
| --ref-kind TEXT | text | No | work_item | Reference kind. |
| --workstream TEXT | text | Yes |  | New workstream id. |
| --old-workstream TEXT | text | No |  | Only reassign bindings from this workstream. |
| --provider-instance TEXT | text | No |  | Target one provider instance. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration ref-id`

**Usage:** `vertex integration ref-id [OPTIONS]`

Migrate a UIL registration to a new ref_id (e.g. after a Teams thread rotation).

This is the UIL equivalent of 'vertex registry set-id'.  Use when a channel artifact
changes its identity (e.g. a Teams meeting series moves to a new thread).  All bindings
and governance state are carried over to the new ref_id in a single atomic transaction.
Raises an error if the old ref_id does not exist or the new ref_id is already registered.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --channel TEXT | text | Yes |  | Channel name. |
| --old-ref-id TEXT | text | Yes |  | Current external reference id. |
| --new-ref-id TEXT | text | Yes |  | New external reference id (e.g. new Teams thread id). |
| --ref-kind TEXT | text | No | teams_message | Reference kind (default: teams_message). |
| --pm TEXT | text | Yes |  | PM alias authorising this change. |
| --reason TEXT | text | No |  | Optional reason for the ref-id change. |
| --provider-instance TEXT | text | No |  | Target one provider instance. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration discover`

**Usage:** `vertex integration discover [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --channel TEXT | text | No |  | Channel to discover. |
| --programs-root PATH | path | No | programs | Programs root. |
| --dry-run | boolean | No | False | Compute without writes. |
| --force | boolean | No | False | Run even when discovery is fresh. |
| --accept-shrinkage | boolean | No | False | Accept guarded shrinkage. |

#### `vertex integration migrate`

**Usage:** `vertex integration migrate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --dry-run | boolean | No | False | Report what would be migrated without writing. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration schema-migrate`

**Usage:** `vertex integration schema-migrate [OPTIONS]`

Handle non-additive schema migrations after a code upgrade.

For additive changes (new columns), schema is auto-migrated on connection.
This command is needed only when SchemaVersionError is raised (unknown schema
version), indicating a non-additive structural change.

Creates a timestamped backup before any destructive migration.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --force | boolean | No | False | Accept schema re-initialization (data-destructive). Creates a backup first. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration backup`

**Usage:** `vertex integration backup [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration restore`

**Usage:** `vertex integration restore [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --backup TEXT | text | No |  | Backup file name or timestamp. |
| --programs-root PATH | path | No | programs | Programs root. |

#### `vertex integration prune`

**Usage:** `vertex integration prune [OPTIONS]`

Delete RETIRED and SUPPRESSED registrations older than the retention window.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program id. |
| --channel / -c TEXT | text | Yes |  | Channel to prune (e.g. teams, ado). |
| --older-than-days INTEGER | integer | No | 90 | Delete RETIRED/SUPPRESSED registrations older than this many days. |
| --dry-run | boolean | No | False | Report how many rows would be pruned without deleting. |
| --programs-root PATH | path | No | programs | Programs root. |

### `vertex index`

**Usage:** `vertex index [OPTIONS] COMMAND [ARGS]...`

Manage the local semantic archive index.

**Subcommands**

| Command | Description |
|---|---|
| `rebuild` |  |
| `optimize` |  |

#### `vertex index rebuild`

**Usage:** `vertex index rebuild [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name to index, e.g. myprogram_weekly. |

#### `vertex index optimize`

**Usage:** `vertex index optimize [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name to optimize, e.g. myprogram_weekly. |
| --if-needed | boolean | No | False | Only optimize when more than 1000 new excerpts have been indexed since the last optimize. |

### `vertex milestones`

**Usage:** `vertex milestones [OPTIONS] COMMAND [ARGS]...`

Manage milestone health and authored milestone data.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `assess` |  |
| `update` |  |

#### `vertex milestones list`

**Usage:** `vertex milestones list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex milestones assess`

**Usage:** `vertex milestones assess [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |
| --as-of TEXT | text | No |  | Optional YYYY-MM-DD override for assessment time. |

#### `vertex milestones update`

**Usage:** `vertex milestones update [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Milestone id. |
| --status TEXT | text | No |  | Optional new status. |
| --target-date TEXT | text | No |  | Optional new YYYY-MM-DD target date. |
| --owner TEXT | text | No |  | Optional new owner alias. |
| --name TEXT | text | No |  | Optional new milestone name. |
| --notes TEXT | text | No |  | Optional new notes text. |
| --clear-notes | boolean | No | False | Clear milestone notes. |

### `vertex hypothesis`

**Usage:** `vertex hypothesis [OPTIONS] COMMAND [ARGS]...`

Manage L1 reality hypotheses.

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `show` |  |
| `propose` |  |
| `from-assumption` |  |
| `confirm` |  |
| `reject` |  |
| `challenge` |  |
| `update` |  |
| `invalidate` |  |
| `reinstate` |  |
| `export-confirmations` |  |
| `quickstart` |  |
| `annotate` | Attach annotation-only documents to hypotheses. |

#### `vertex hypothesis list`

**Usage:** `vertex hypothesis list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --include-terminal | boolean | No | False | Include rejected, invalidated, and superseded hypotheses. |
| --format TEXT | text | No | human | Output format: human or json. |

#### `vertex hypothesis show`

**Usage:** `vertex hypothesis show [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Hypothesis id or short id. |
| --format TEXT | text | No | human | Output format: human or json. |

#### `vertex hypothesis propose`

**Usage:** `vertex hypothesis propose [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --kind TEXT | text | Yes |  | Hypothesis kind: scalar_fact, trend, or delivery_date. |
| --statement TEXT | text | Yes |  | PM-readable hypothesis statement. |
| --assertion-id TEXT | text | No |  | Telemetry assertion id for scalar_fact or trend hypotheses. |
| --composite-assertion-id TEXT | text | No |  | Composite assertion id for scalar_fact or trend hypotheses. |
| --expected-value FLOAT | float | No |  | Expected numeric value for scalar_fact or trend hypotheses. |
| --expected-date TEXT | text | No |  | Expected ISO date for delivery_date hypotheses. |
| --linked-ado-item INTEGER | integer | No |  | ADO work item id for delivery_date hypotheses. |
| --depends-on TEXT | text | No |  | Repeatable upstream hypothesis id or short id dependency. |
| --review-due TEXT | text | No |  | Optional YYYY-MM-DD review date. |
| --workstream TEXT | text | No |  | Optional workstream id. |
| --proposed-by TEXT | text | No |  | Actor alias. Defaults to current OS user. |

#### `vertex hypothesis from-assumption`

**Usage:** `vertex hypothesis from-assumption [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Assumption id to promote. |
| --kind TEXT | text | Yes |  | Hypothesis kind: scalar_fact, trend, or delivery_date. |
| --assertion-id TEXT | text | No |  | Telemetry assertion id for scalar_fact or trend hypotheses. |
| --composite-assertion-id TEXT | text | No |  | Composite assertion id for scalar_fact or trend hypotheses. |
| --expected-value FLOAT | float | No |  | Expected numeric value for scalar_fact or trend hypotheses. |
| --expected-date TEXT | text | No |  | Expected ISO date for delivery_date hypotheses. |
| --linked-ado-item INTEGER | integer | No |  | ADO work item id for delivery_date hypotheses. |
| --depends-on TEXT | text | No |  | Repeatable upstream hypothesis id or short id dependency. |
| --review-due TEXT | text | No |  | Optional YYYY-MM-DD review date. Defaults to the assumption validation due date. |
| --workstream TEXT | text | No |  | Optional workstream id override. |
| --proposed-by TEXT | text | No |  | Actor alias. Defaults to current OS user. |

#### `vertex hypothesis confirm`

**Usage:** `vertex hypothesis confirm [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Hypothesis id or short id. |
| --confirmed-by TEXT | text | No |  | Actor alias. Defaults to current OS user. |

#### `vertex hypothesis reject`

**Usage:** `vertex hypothesis reject [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Hypothesis id or short id. |
| --reason TEXT | text | Yes |  | Why the proposed hypothesis is being rejected. |
| --rejected-by TEXT | text | No |  | Actor alias. Defaults to current OS user. |

#### `vertex hypothesis challenge`

**Usage:** `vertex hypothesis challenge [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Hypothesis id or short id. |
| --reason TEXT | text | Yes |  | Why the hypothesis is being manually challenged. |
| --challenged-by TEXT | text | No |  | Actor alias. Defaults to current OS user. |

#### `vertex hypothesis update`

**Usage:** `vertex hypothesis update [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Hypothesis id or short id. |
| --reason TEXT | text | Yes |  | Why this revision supersedes the prior hypothesis. |
| --statement TEXT | text | No |  | Updated statement. |
| --assertion-id TEXT | text | No |  | Updated telemetry assertion id. |
| --composite-assertion-id TEXT | text | No |  | Updated composite assertion id. |
| --expected-value FLOAT | float | No |  | Updated numeric expected value. |
| --expected-date TEXT | text | No |  | Updated ISO date for delivery_date hypotheses. |
| --linked-ado-item INTEGER | integer | No |  | Updated ADO work item id for delivery_date hypotheses. |
| --depends-on TEXT | text | No |  | Updated repeatable upstream hypothesis id or short id dependency list. |
| --clear-depends-on | boolean | No | False | Clear all dependency links from the replacement hypothesis. |
| --review-due TEXT | text | No |  | Updated YYYY-MM-DD review date. |
| --workstream TEXT | text | No |  | Updated workstream id. |
| --updated-by TEXT | text | No |  | Actor alias. Defaults to current OS user. |

#### `vertex hypothesis invalidate`

**Usage:** `vertex hypothesis invalidate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Hypothesis id or short id. |
| --reason TEXT | text | Yes |  | Why the hypothesis is being retired. |
| --invalidated-by TEXT | text | No |  | Actor alias. Defaults to current OS user. |

#### `vertex hypothesis reinstate`

**Usage:** `vertex hypothesis reinstate [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Hypothesis id or short id. |
| --reason TEXT | text | Yes |  | Why the challenge is considered resolved. |
| --reinstated-by TEXT | text | No |  | Actor alias. Defaults to current OS user. |

#### `vertex hypothesis export-confirmations`

**Usage:** `vertex hypothesis export-confirmations [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --output PATH | path | Yes |  | Destination JSONL path for the confirmation seed export. |

#### `vertex hypothesis quickstart`

**Usage:** `vertex hypothesis quickstart [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --metric-id TEXT | text | No |  | Metric id to monitor. May be omitted when --query-id refers to a KPI catalog entry that declares metric_id or exactly one active assertion_id. |
| --binding-id TEXT | text | No |  | Metric source binding id to reuse or create. Defaults to a stable id derived from the metric id. |
| --operator TEXT | text | No |  | Comparison operator, for example >= or <=. May be omitted when --query-id refers to a KPI catalog entry with exactly one active assertion_id. |
| --threshold FLOAT | float | No |  | Threshold value for the quickstart assertion. May be omitted when --query-id refers to a KPI catalog entry with exactly one active assertion_id. |
| --baseline-value FLOAT | float | No |  | Baseline value required for percent-change assertions when quickstart authors a new assertion. |
| --baseline-captured-at TEXT | text | No |  | Optional ISO timestamp for the percent-change baseline observation. |
| --cluster TEXT | text | No |  | Kusto cluster for a new binding. |
| --database TEXT | text | No |  | Kusto database for a new binding. |
| --kql-template TEXT | text | No |  | Kusto query template for a new binding. |
| --result-column TEXT | text | No |  | Result column for a new binding. |
| --query-id TEXT | text | No |  | Existing KPI query id whose binding inputs should be reused when creating a new binding. |
| --metric-title TEXT | text | No |  | Title for a new metric definition when the metric is missing. |
| --unit TEXT | text | No |  | Unit for a new metric definition when the metric is missing. |
| --aggregation TEXT | text | No | last | Aggregation for a new metric definition when the metric is missing. |
| --statement TEXT | text | No |  | Optional PM-readable hypothesis statement override. |
| --review-due TEXT | text | No |  | Optional YYYY-MM-DD review date. |
| --description TEXT | text | No |  | Optional assertion description override. |
| --proposed-by TEXT | text | No |  | Actor alias. Defaults to current OS user. |

#### `vertex hypothesis annotate`

**Usage:** `vertex hypothesis annotate [OPTIONS] COMMAND [ARGS]...`

Attach annotation-only documents to hypotheses.

**Subcommands**

| Command | Description |
|---|---|
| `add` |  |
| `list` |  |
| `archive` |  |

#### `vertex hypothesis annotate add`

**Usage:** `vertex hypothesis annotate add [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Hypothesis id or short id. |
| --kind TEXT | text | Yes |  | Annotation kind: pdf, markdown, url, or file. |
| --title TEXT | text | Yes |  | Operator-facing annotation title. |
| --locator TEXT | text | Yes |  | URL or file/path locator for the artifact. |
| --locator-kind TEXT | text | Yes |  | Locator kind: url, repo_path, or local_path. |
| --media-type TEXT | text | No |  | Optional MIME type for the artifact. |
| --sha256 TEXT | text | No |  | Optional content hash for local artifacts. |
| --note TEXT | text | No |  | Optional PM-authored context for the annotation. |
| --tag TEXT | text | No |  | Repeatable operator tag for the annotation. |
| --added-by TEXT | text | No |  | Actor alias. Defaults to current OS user. |

#### `vertex hypothesis annotate list`

**Usage:** `vertex hypothesis annotate list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Hypothesis id or short id. |
| --include-archived | boolean | No | False | Include archived annotations. |
| --format TEXT | text | No | human | Output format: human or json. |

#### `vertex hypothesis annotate archive`

**Usage:** `vertex hypothesis annotate archive [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --annotation-id TEXT | text | Yes |  | Annotation id to archive. |
| --reason TEXT | text | Yes |  | Why the annotation is being archived. |
| --archived-by TEXT | text | No |  | Actor alias. Defaults to current OS user. |

### `vertex observation`

**Usage:** `vertex observation [OPTIONS] COMMAND [ARGS]...`

Inject manual telemetry observations into L1 reality state.

**Subcommands**

| Command | Description |
|---|---|
| `inject` |  |
| `pin` |  |
| `unpin` |  |

#### `vertex observation inject`

**Usage:** `vertex observation inject [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --metric TEXT | text | Yes |  | Metric id to record. |
| --value FLOAT | float | Yes |  | Numeric observation value. |
| --measurement-period-start TEXT | text | Yes |  | Measurement window start in ISO-8601. |
| --measurement-period-end TEXT | text | Yes |  | Measurement window end in ISO-8601. |
| --observed-at TEXT | text | No |  | Observation timestamp in ISO-8601. Defaults to now. |
| --dimension TEXT | text | No |  | Repeat as key=value to capture dimensions. |
| --sample-count INTEGER RANGE | integer range | No | 1 | Optional sample count. |
| --force | boolean | No | False | Overwrite an existing manual observation for the same metric, dimensions, and period. |

#### `vertex observation pin`

**Usage:** `vertex observation pin [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --metric TEXT | text | Yes |  | Metric id to pin. |
| --measurement-period-end TEXT | text | Yes |  | Measurement window end in ISO-8601. |
| --dimension TEXT | text | No |  | Repeat as key=value to identify dimensions. |
| --reason TEXT | text | Yes |  | Why this manual observation should override telemetry. |

#### `vertex observation unpin`

**Usage:** `vertex observation unpin [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --metric TEXT | text | Yes |  | Metric id to unpin. |
| --measurement-period-end TEXT | text | Yes |  | Measurement window end in ISO-8601. |
| --dimension TEXT | text | No |  | Repeat as key=value to identify dimensions. |

### `vertex policy`

**Usage:** `vertex policy [OPTIONS] COMMAND [ARGS]...`

Promote governed policy proposals into active local rules.

**Subcommands**

| Command | Description |
|---|---|
| `promote` |  |

#### `vertex policy promote`

**Usage:** `vertex policy promote [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --rule TEXT | text | Yes |  | Signal approval rule id to promote, for example approval:decision_ask_escalation. |
| --updated-by TEXT | text | No |  | Author alias for the policy promotion audit record. |
| --dry-run | boolean | No | False | Preview the promotion without writing the rule or audit record. |

### `vertex privacy`

**Usage:** `vertex privacy [OPTIONS] COMMAND [ARGS]...`

Privacy & data governance matrix (WS-15).

**Subcommands**

| Command | Description |
|---|---|
| `show` | Print the privacy & data governance matrix. |
| `check` | Return the posture for a single channel (machine-friendly). |
| `purge` | WS-18/ADF-W5.9: run the unified retention purge (`src/core/privacy_purge.py`) |

#### `vertex privacy show`

**Usage:** `vertex privacy show [OPTIONS]`

Print the privacy & data governance matrix.

Sections:
  - channels: per-channel read/write classification, retention, RBAC model
  - sidecars: per-sidecar classification + retention
  - retention: retention-class → days mapping

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --section TEXT | text | No |  | Filter to one of: channels, sidecars, retention, all (default: all). |

#### `vertex privacy check`

**Usage:** `vertex privacy check [OPTIONS]`

Return the posture for a single channel (machine-friendly).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --channel TEXT | text | Yes |  | Channel name to inspect posture for (e.g. ado, kusto). |

#### `vertex privacy purge`

**Usage:** `vertex privacy purge [OPTIONS]`

WS-18/ADF-W5.9: run the unified retention purge (`src/core/privacy_purge.py`)
for one program against every registered `SIDECAR_RETENTION` rule.
Dry-run by default -- pass --apply to actually rewrite sidecars.
Rules with INDEFINITE retention are skipped (never auto-purged);
non-JSONL sidecars (SQLite, YAML config, immutable archive files) are
recorded as no-op (governed by their own rotation/migration paths).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. xpf. |
| --apply | boolean | No | False | Actually mutate sidecars. Default is dry-run (report only). |
| --format TEXT | text | No | human | Output format: human or json. |

### `vertex observability`

**Usage:** `vertex observability [OPTIONS] COMMAND [ARGS]...`

SRE-grade observability: failure diagnosis, per-channel perf, support bundle.

**Subcommands**

| Command | Description |
|---|---|
| `diagnose` | Explain the last gather failure for the program. |
| `perf` | Per-channel P50/P95 latency + SLO status. |
| `bundle` | Build a redacted support bundle (.tar.gz) for SRE triage. |

#### `vertex observability diagnose`

**Usage:** `vertex observability diagnose [OPTIONS]`

Explain the last gather failure for the program.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id to diagnose. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |
| --window INTEGER | integer | No | 1 | How many recent run_telemetry rows to scan (default 1 = last run). |

#### `vertex observability perf`

**Usage:** `vertex observability perf [OPTIONS]`

Per-channel P50/P95 latency + SLO status.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id to inspect. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |
| --window INTEGER | integer | No | 10 | How many recent run_telemetry rows to aggregate (default 10). |

#### `vertex observability bundle`

**Usage:** `vertex observability bundle [OPTIONS]`

Build a redacted support bundle (.tar.gz) for SRE triage.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id to bundle. |
| --to PATH | path | No |  | Output path. Defaults to programs/<id>/_alerts/support_bundle_<ts>.tar.gz |
| --archive-root PATH | path | No |  | Optional archive root (defaults to repo archive/). |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

### `vertex alerts`

**Usage:** `vertex alerts [OPTIONS] COMMAND [ARGS]...`

Between-runs alert management (WS-17).

**Subcommands**

| Command | Description |
|---|---|
| `show` | List alerts for a program (open by default). |
| `append` | Append a new alert (operator- or tool-curated). |
| `resolve` | Mark an alert resolved (append-only; no in-place rewrite). |
| `banner` | Print the next-run banner (or nothing if all clear). |

#### `vertex alerts show`

**Usage:** `vertex alerts show [OPTIONS]`

List alerts for a program (open by default).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --include-resolved | boolean | No | False | Include resolved alerts. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex alerts append`

**Usage:** `vertex alerts append [OPTIONS]`

Append a new alert (operator- or tool-curated).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --severity TEXT | text | No | warn | info \| warn \| error \| critical. |
| --category TEXT | text | No | unknown | Failure category (failure taxonomy). |
| --message TEXT | text | Yes |  | Alert message. |
| --next-command TEXT | text | No |  | Operator next command. |
| --format TEXT | text | No | human | Output format: human, json. |

#### `vertex alerts resolve`

**Usage:** `vertex alerts resolve [OPTIONS]`

Mark an alert resolved (append-only; no in-place rewrite).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --alert-id TEXT | text | Yes |  | Alert id to resolve. |
| --format TEXT | text | No | human | Output format: human, json. |

#### `vertex alerts banner`

**Usage:** `vertex alerts banner [OPTIONS]`

Print the next-run banner (or nothing if all clear).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |

### `vertex readiness`

**Usage:** `vertex readiness [OPTIONS] COMMAND [ARGS]...`

Manage launch readiness snapshots.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | table | Output format: table or json. |

**Subcommands**

| Command | Description |
|---|---|
| `fetch` |  |
| `show` |  |

#### `vertex readiness fetch`

**Usage:** `vertex readiness fetch [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | table | Output format: table or json. |

#### `vertex readiness show`

**Usage:** `vertex readiness show [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | table | Output format: table or json. |

### `vertex registry`

**Usage:** `vertex registry [OPTIONS] COMMAND [ARGS]...`

Inspect M365 registry state.

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `confirm` |  |
| `reject` |  |
| `reassign` |  |
| `set-id` |  |
| `discover-ids` |  |
| `promote` |  |
| `rename` |  |

#### `vertex registry list`

**Usage:** `vertex registry list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |
| --source TEXT | text | No | auto | Data source: auto (UIL when available, else yaml), yaml, or uil. |

#### `vertex registry confirm`

**Usage:** `vertex registry confirm [OPTIONS] ARTIFACT_ID`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --pm-alias TEXT | text | Yes |  | PM alias recording this action. |
| --workstream-id TEXT | text | No |  | Optional explicit workstream assignment. |
| --topics TEXT | text | No |  | Comma-separated topic tags to attach. |
| --reason TEXT | text | No |  | Optional rationale for the confirmation. |

#### `vertex registry reject`

**Usage:** `vertex registry reject [OPTIONS] ARTIFACT_ID`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --pm-alias TEXT | text | Yes |  | PM alias recording this action. |
| --reason TEXT | text | No |  | Optional rationale for the rejection. |

#### `vertex registry reassign`

**Usage:** `vertex registry reassign [OPTIONS] ARTIFACT_ID`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --workstream-id TEXT | text | Yes |  | Workstream id to assign. |
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --pm-alias TEXT | text | Yes |  | PM alias recording this action. |
| --topics TEXT | text | No |  | Comma-separated topic tags to attach. |
| --reason TEXT | text | No |  | Optional rationale for the reassignment. |

#### `vertex registry set-id`

**Usage:** `vertex registry set-id [OPTIONS] ARTIFACT_ID`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --pm-alias TEXT | text | Yes |  | PM alias recording this action. |
| --series-id TEXT | text | No |  | Meeting series id to attach. |
| --thread-id TEXT | text | No |  | Thread id to attach. |
| --reason TEXT | text | No |  | Optional rationale for the update. |

#### `vertex registry discover-ids`

**Usage:** `vertex registry discover-ids [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --apply | boolean | No | False | Apply only unique exact-match candidates directly to the registry. |
| --limit INTEGER RANGE | integer range | No | 10 | Max candidates to inspect per artifact. |
| --pm-alias TEXT | text | No | vertex | PM alias recorded when --apply writes discovered ids. |
| --format TEXT | text | No | human | Output format: human or json. |

#### `vertex registry promote`

**Usage:** `vertex registry promote [OPTIONS] ARTIFACT_ID`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --pm-alias TEXT | text | No |  | PM alias recording confidence-based promotion. |
| --reason TEXT | text | No |  | Optional rationale for confidence-based promotion. |

#### `vertex registry rename`

**Usage:** `vertex registry rename [OPTIONS] ARTIFACT_ID`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --display-name TEXT | text | Yes |  | Stable display name used to derive thread:named:<slug>. |
| --pm-alias TEXT | text | Yes |  | PM alias recording the rename. |
| --reason TEXT | text | No |  | Optional rationale for the rename. |

### `vertex reality`

**Usage:** `vertex reality [OPTIONS] COMMAND [ARGS]...`

Inspect and act on L1 reality state.

**Subcommands**

| Command | Description |
|---|---|
| `pending-review` |  |
| `digest` |  |
| `challenges` |  |
| `snooze` |  |
| `dismiss` |  |
| `reopen` |  |
| `status` | Show truth-level status for a program and run QG-27 gate check (WI-5.1 / WI-3.9). |
| `explain` | Explain why one fact is believed, disputed, or provisional. |
| `export` | Export program reality as a versioned JSON envelope. |
| `maintenance` | Author maintenance windows for reality suppression. |

#### `vertex reality pending-review`

**Usage:** `vertex reality pending-review [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --interactive | boolean | No | False | Prompt through proposed hypotheses one at a time. |
| --reviewer TEXT | text | No |  | Reviewer alias used for accept or reject actions. |
| --format TEXT | text | No | text | Output format when not interactive: text or json. |

#### `vertex reality digest`

**Usage:** `vertex reality digest [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --all-programs | boolean | No | False | Aggregate reality digests across all configured programs. |
| --refresh | boolean | No | False | Recompute the digest before rendering it. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex reality challenges`

**Usage:** `vertex reality challenges [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --severity TEXT | text | No |  | Optional severity filter: info, warn, alert. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex reality snooze`

**Usage:** `vertex reality snooze [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --challenge-id TEXT | text | Yes |  | Challenge id to snooze. |
| --until TEXT | text | Yes |  | Snooze-until date or timestamp in ISO-8601. |
| --reason TEXT | text | Yes |  | Why the challenge is being snoozed. |

#### `vertex reality dismiss`

**Usage:** `vertex reality dismiss [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --challenge-id TEXT | text | Yes |  | Challenge id to dismiss. |
| --reason TEXT | text | Yes |  | Why the challenge is being dismissed. |

#### `vertex reality reopen`

**Usage:** `vertex reality reopen [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --challenge-id TEXT | text | Yes |  | Challenge id to reopen. |

#### `vertex reality status`

**Usage:** `vertex reality status [OPTIONS]`

Show truth-level status for a program and run QG-27 gate check (WI-5.1 / WI-3.9).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --all-programs | boolean | No | False | Show status for all programs (fleet default, WI-5.1). |
| --format TEXT | text | No | text | Output format: text or json. |
| --force | boolean | No | False | Override advisory (QG-27, forceable) gates. |

#### `vertex reality explain`

**Usage:** `vertex reality explain [OPTIONS]`

Explain why one fact is believed, disputed, or provisional.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --fact-id TEXT | text | Yes |  | Fact id to explain. |
| --format TEXT | text | No | text | Output format: text or json. |

#### `vertex reality export`

**Usage:** `vertex reality export [OPTIONS]`

Export program reality as a versioned JSON envelope.

Without --timeseries: exports the current snapshot.
With --timeseries: exports an array of historical frames.

Every export appends to the edition-scoped audit log and writes a
cursor manifest at publications/<program>/reality_export_cursor.json.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --json | boolean | No | False | Output as JSON envelope. |
| --timeseries | boolean | No | False | Emit an array of historical frames using as_of replay. |
| --interval INTEGER | integer | No | 7 | Days between frames for --timeseries. |
| --since TEXT | text | No |  | Earliest date (ISO) for --timeseries. Defaults to 60 * interval days ago. |

#### `vertex reality maintenance`

**Usage:** `vertex reality maintenance [OPTIONS] COMMAND [ARGS]...`

Author maintenance windows for reality suppression.

**Subcommands**

| Command | Description |
|---|---|
| `schedule` |  |

#### `vertex reality maintenance schedule`

**Usage:** `vertex reality maintenance schedule [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --title TEXT | text | Yes |  | Maintenance window title. |
| --starts-at TEXT | text | Yes |  | Window start in ISO-8601. |
| --ends-at TEXT | text | Yes |  | Window end in ISO-8601. |
| --scope-kind TEXT | text | No | program | One of: program, metric, binding, workstream. |
| --scope-value TEXT | text | No | * | Scope value for non-program windows. |
| --reference TEXT | text | No |  | Optional change or incident reference. |

### `vertex review-sections`

**Usage:** `vertex review-sections [OPTIONS] COMMAND [ARGS]...`

Manage per-section review status for the active issue.

**Subcommands**

| Command | Description |
|---|---|
| `show` |  |
| `set` |  |
| `clear` |  |
| `export` |  |

#### `vertex review-sections show`

**Usage:** `vertex review-sections show [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --issue INTEGER | integer | No |  | Issue number to inspect. Defaults to the active issue. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex review-sections set`

**Usage:** `vertex review-sections set [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --issue INTEGER | integer | No |  | Issue number to update. Defaults to the active issue. |
| --section TEXT | text | Yes |  | Section id, for example exec_summary or ws:deployment. |
| --state TEXT | text | Yes |  | Review state: pending, sent, approved, changes_requested, rejected. |
| --note TEXT | text | No |  | Optional reviewer note. |
| --reviewer TEXT | text | No |  | Optional reviewer name override. |

#### `vertex review-sections clear`

**Usage:** `vertex review-sections clear [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --issue INTEGER | integer | No |  | Issue number to update. Defaults to the active issue. |
| --section TEXT | text | Yes |  | Section id to reset to pending. |

#### `vertex review-sections export`

**Usage:** `vertex review-sections export [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --edition TEXT | text | Yes |  | Edition name, e.g. myprogram_weekly. |
| --issue INTEGER | integer | No |  | Issue number to export. Defaults to the active issue. |
| --section TEXT | text | Yes |  | Workstream review section id, for example ws:deployment_readiness. |
| --open / --no-open | boolean | No | False | Open the exported section HTML in the browser after rendering. |

### `vertex rev`

**Usage:** `vertex rev [OPTIONS] COMMAND [ARGS]...`

Program-Context Intelligence (REV) retrieval + verification.

**Subcommands**

| Command | Description |
|---|---|
| `run` | Run one REV retrieval cycle and stage candidates for triage. |
| `init-inbox` | Scaffold the local-import inbox directory tree + write a local README (P1-5). |
| `rotate-processed` | Rotate stale/surplus files from ``processed/`` → ``processed/archive/`` (P2-14). |
| `export-corpus` | Export a PII-scrubbed REV corpus bundle (P2-5). |
| `label-corpus` | Import or bootstrap the REV labeled corpus for quality gating (S-9c). |

#### `vertex rev run`

**Usage:** `vertex rev run [OPTIONS]`

Run one REV retrieval cycle and stage candidates for triage.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID. |
| --mailbox TEXT | text | Yes |  | Principal mailbox (UPN) to retrieve from. |
| --tenant-id TEXT | text | No |  | Tenant ID for the mailbox (defaults to 'default'). |
| --container TEXT | text | No | inbox | Logical container label. |
| --subject TEXT | text | No |  | Subject search term (repeatable). |
| --body TEXT | text | No |  | Body/free-text search term (repeatable). |
| --sender TEXT | text | No |  | Sender filter (repeatable). |
| --limit INTEGER | integer | No | 25 | Max candidates to enumerate. |
| --profile TEXT | text | No |  | Override REV profile: legacy_nl \| search_hydrate \| rev_verified. |
| --mock-fixture PATH | path | No |  | JSON fixture of messages for the P1 walking skeleton (no live consent). |
| --eml-inbox PATH | path | No |  | Directory containing locally-exported .eml files (inbox/ dir for EmlEnumerator). |
| --ics-inbox PATH | path | No |  | Directory containing locally-exported .ics calendar files (inbox/ dir for IcsEnumerator). W6-1. |
| --docs-inbox PATH | path | No |  | Directory containing locally-downloaded .docx/.pdf files (inbox/ dir for LocalFileEnumerator). P3-5. |
| --extractor TEXT | text | No | deterministic | Extractor tier: deterministic \| llm. 'llm' requires VERTEX_AI_DEPLOYMENT. |

#### `vertex rev init-inbox`

**Usage:** `vertex rev init-inbox [OPTIONS]`

Scaffold the local-import inbox directory tree + write a local README (P1-5).

Creates ``rev_inbox/`` with the ``claimed/`` / ``processed/`` /
``quarantine/`` subdirectories used by the 3-directory atomicity model, plus
the program ``_rev/`` checkpoint dir, and writes an operator-facing
``README.md`` documenting the export-import workflow + OA-4 privacy policy.
Idempotent: re-running only refreshes the README.

The 3-directory atomicity mechanics are identical across the three
local-import enumerators (eml/ics/docs), so ``--eml-inbox``/``--ics-inbox``/
``--docs-inbox`` are interchangeable overrides of the same inbox root; all
three fall back to the same program default when none is given.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program / -p TEXT | text | Yes |  | Program ID. |
| --eml-inbox PATH | path | No |  | Inbox root (defaults to programs/<program>/rev_inbox). Must be on a LOCAL filesystem. |
| --ics-inbox PATH | path | No |  | Inbox root for locally-exported .ics calendar files (defaults to programs/<program>/rev_inbox). Must be on a LOCAL filesystem. |
| --docs-inbox PATH | path | No |  | Inbox root for locally-downloaded .docx/.pdf files (defaults to programs/<program>/rev_inbox). Must be on a LOCAL filesystem. |

#### `vertex rev rotate-processed`

**Usage:** `vertex rev rotate-processed [OPTIONS]`

Rotate stale/surplus files from ``processed/`` → ``processed/archive/`` (P2-14).

Runs the same OA-4 retention rotation that fires automatically at the end of
each ``vertex rev run`` cycle, but as a standalone housekeeping command so
an operator can purge the hot ``processed/`` path without running a full
cycle. Files older than ``--max-age-days`` (default 90) **or** a surplus
beyond ``--max-count`` (default 500, oldest first) are moved to
``processed/archive/``.

The rotation mechanics are format-agnostic (it moves whatever is in
``processed/``, regardless of which enumerator produced it), so
``--eml-inbox``/``--ics-inbox``/``--docs-inbox`` are interchangeable
overrides of the same inbox root, matching ``init-inbox``.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --eml-inbox PATH | path | No |  | Local-import inbox root (defaults to programs/<program>/rev_inbox). |
| --ics-inbox PATH | path | No |  | Local-import inbox root for .ics calendar imports (defaults to programs/<program>/rev_inbox). |
| --docs-inbox PATH | path | No |  | Local-import inbox root for .docx/.pdf imports (defaults to programs/<program>/rev_inbox). |
| --max-age-days INTEGER | integer | No | 90 | Rotate files older than this many days. |
| --max-count INTEGER | integer | No | 500 | Rotate oldest surplus files beyond this count. |
| --programs-root PATH | path | No | programs | Programs root directory. |

#### `vertex rev export-corpus`

**Usage:** `vertex rev export-corpus [OPTIONS]`

Export a PII-scrubbed REV corpus bundle (P2-5).

Writes ``candidates.jsonl`` + ``triage_decisions.jsonl`` + the labeled corpus
copy (if present) + optionally ``evidence_vault.jsonl`` + a ``manifest.json``
to ``--output``. Direct identifiers (sender SMTP, message-id, mailbox
principal, triage actor) are hash-redacted; content hashes are kept for
restore/dedup. Content fields (subject, payload, excerpt text) are kept —
the manifest records a warning that they may contain incidental PII, so the
export is for operator-controlled backup (self-containment directive), not
external sharing.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --output PATH | path | Yes |  | Output directory for the PII-scrubbed bundle. |
| --include-vault / --no-include-vault | boolean | No | False | Also export vaulted evidence excerpts (raw text — may contain incidental PII). |
| --programs-root PATH | path | No | programs | Programs root directory. |

#### `vertex rev label-corpus`

**Usage:** `vertex rev label-corpus [OPTIONS]`

Import or bootstrap the REV labeled corpus for quality gating (S-9c).

Manages ``programs/<id>/_quality/rev_labeled_corpus.jsonl``.
Run with ``--import <file>`` to bulk-load pre-annotated records; run with
``--bootstrap`` to scaffold skeleton records for all un-annotated pending
candidates.  Run ``vertex rev export-corpus`` to dump the full bundle.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id. |
| --import PATH | path | No |  | JSONL file of corpus annotations to import. Each line must have candidate_id, expected_event_type, and label (accept\|reject). Annotator and second_label are optional. Existing records with the same candidate_id are updated (upsert). |
| --bootstrap / --no-bootstrap | boolean | No | False | Write empty skeleton corpus records for all pending candidates that are not yet in the corpus. Expected_event_type is set to the candidate's proposed_event_type (operator should review and correct); label is left blank for manual completion. |
| --programs-root PATH | path | No | programs | Programs root directory. |

### `vertex risks`

**Usage:** `vertex risks [OPTIONS] COMMAND [ARGS]...`

Manage the program risk register.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --show-links | boolean | No | False | Show RAID causal links for each listed risk. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

**Subcommands**

| Command | Description |
|---|---|
| `list` |  |
| `add` |  |
| `update` |  |
| `review` |  |
| `link` |  |

#### `vertex risks list`

**Usage:** `vertex risks list [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --status TEXT | text | No |  | Optional status filter. |
| --show-links | boolean | No | False | Show RAID causal links for each listed risk. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

#### `vertex risks add`

**Usage:** `vertex risks add [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --title TEXT | text | Yes |  | Risk title. |
| --probability TEXT | text | Yes |  | Probability: very_likely\|likely\|possible\|unlikely. |
| --impact TEXT | text | Yes |  | Impact: critical\|high\|medium\|low. |
| --description TEXT | text | No |  | Optional longer description. Defaults to the title. |
| --category TEXT | text | No | technical | Category: technical\|schedule\|resource\|dependency\|external. |
| --owner TEXT | text | No |  | Owner alias. Defaults to current OS user. |
| --mitigation-plan TEXT | text | No |  | Optional mitigation plan. |
| --mitigation-due-date TEXT | text | No |  | Optional YYYY-MM-DD mitigation due date. |
| --workstream TEXT | text | No |  | Repeat to link workstream ids. |
| --work-item INTEGER | integer | No |  | Repeat to link ADO work item ids. |
| --milestone TEXT | text | No |  | Repeat to link milestone ids. |
| --claim TEXT | text | No |  | Repeat to link claim ids. |
| --action TEXT | text | No |  | Repeat to link action ids. |
| --entity-ref TEXT | text | No |  | Repeat to add entity refs. |
| --issue INTEGER | integer | No |  | Optional Vertex issue number that identified the risk. |
| --identified-date TEXT | text | No |  | Optional YYYY-MM-DD identified date. |

#### `vertex risks update`

**Usage:** `vertex risks update [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --id TEXT | text | Yes |  | Risk id. |
| --status TEXT | text | No |  | Optional new status. |
| --probability TEXT | text | No |  | Optional new probability. |
| --impact TEXT | text | No |  | Optional new impact. |
| --category TEXT | text | No |  | Optional new category. |
| --owner TEXT | text | No |  | Optional new owner alias. |
| --description TEXT | text | No |  | Optional new description. |
| --mitigation-plan TEXT | text | No |  | Optional new mitigation plan. |
| --mitigation-due-date TEXT | text | No |  | Optional new YYYY-MM-DD mitigation due date. |
| --reviewed-date TEXT | text | No |  | Optional YYYY-MM-DD review date. Defaults to today when any change is applied. |
| --note TEXT | text | No |  | Optional status-change note. |
| --reviewer TEXT | text | No |  | Reviewer alias for status changes. Defaults to current OS user. |

#### `vertex risks review`

**Usage:** `vertex risks review [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --reviewer TEXT | text | No |  | Reviewer alias. Defaults to current OS user. |
| --mark-reviewed | boolean | No | False | Mark all stale risks reviewed today without prompting. |

#### `vertex risks link`

**Usage:** `vertex risks link [OPTIONS] RISK_ID ACTION_ID`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --dry-run | boolean | No | False | Preview the link without writing the risk register. |

### `vertex salience`

**Usage:** `vertex salience [OPTIONS] COMMAND [ARGS]...`

Inspect author salience feedback state.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --no-refresh | boolean | No | False | Read the cached author_salience.yaml without recomputing it. |
| --dry-run | boolean | No | False | Compute the salience model but skip writing author_salience.yaml. |

**Subcommands**

| Command | Description |
|---|---|
| `show` |  |

#### `vertex salience show`

**Usage:** `vertex salience show [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --no-refresh | boolean | No | False | Read the cached author_salience.yaml without recomputing it. |
| --dry-run | boolean | No | False | Compute the salience model but skip writing author_salience.yaml. |

### `vertex signals`

**Usage:** `vertex signals [OPTIONS] COMMAND [ARGS]...`

List and review journal signals.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | No |  | Program id, e.g. myprogram. |
| --format TEXT | text | No | human | Output format: human, json, or csv. |

**Subcommands**

| Command | Description |
|---|---|
| `review` |  |
| `add` |  |
| `link` |  |

#### `vertex signals review`

**Usage:** `vertex signals review [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --reviewer TEXT | text | No |  | Reviewer alias. Defaults to the current OS user. |

#### `vertex signals add`

**Usage:** `vertex signals add [OPTIONS] TEXT`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id, e.g. myprogram. |
| --workstream TEXT | text | No |  | Optional workstream id for the signal. |
| --ref TEXT | text | No |  | Optional entity reference. Repeat for multiple refs. |

#### `vertex signals link`

**Usage:** `vertex signals link [OPTIONS]`

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --signal TEXT | text | Yes |  | Signal id to add to the thread. Repeat for multiple ids. |
| --thread TEXT | text | No |  | Optional thread name. Defaults to a deterministic generated id. |
| --program TEXT | text | No |  | Optional program id. If omitted, Vertex searches all programs. |

### `vertex storage`

**Usage:** `vertex storage [OPTIONS] COMMAND [ARGS]...`

Inspect and validate Vertex storage (read-only).

**Subcommands**

| Command | Description |
|---|---|
| `check` | Validate SQLite journal integrity. |
| `stats` | Print signal count, trajectory count, and DB file size. |

#### `vertex storage check`

**Usage:** `vertex storage check [OPTIONS]`

Validate SQLite journal integrity.

Exit codes: 0=healthy, 1=integrity failure, 2=DB missing/unreadable,
3=WAL not in force (advisory).

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id (e.g. myprogram). |

#### `vertex storage stats`

**Usage:** `vertex storage stats [OPTIONS]`

Print signal count, trajectory count, and DB file size.

Exit codes: 0=success, 2=DB missing/unreadable.

**Options**

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| --program TEXT | text | Yes |  | Program id (e.g. myprogram). |

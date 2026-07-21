# Vertex — UX Specification

**Version:** 1.0  
**Status:** Reflects implemented state as of 2026-07-08  
**Companion docs:** [vertex-prd.md](vertex-prd.md) (requirements), [vertex-tech-spec.md](vertex-tech-spec.md) (technical), `specs/backlog.md` (remaining real-data activation work)
**Scope:** All visual surfaces for Vertex's currently supported Microsoft TPM-program archetypes — newsletter HTML, condensed daily, deck Markdown, reviewer HTML, Teams Markdown, Adaptive Card JSON draft surfaces, freshness report, CLI terminal output, EML files. Binding for layout, color, typography, spacing, and rendering behavior. Broader TPM/EM/global/non-ADO surface expansion is roadmap, not current scope.

## Changelog

- Last updated: 2026-07-08 — Incorporated `specs/fix-data-flow.md` (v1.0→v1.13, archived to `.archive/specs/fix-data-flow.md`) findings and closure. **Correction to the 2026-06-10 entry below**: direct code investigation during that spec's work found no trust-badge rendering markup existed anywhere in the codebase (not for risk, not even for milestone) — the 2026-06-10 claim that §3.6b/§3.6c were "already implemented" was verified false. Trust badges are genuinely implemented for the first time as of 2026-07-08: `templates/partials/truth_badge.j2` renders the `[DISPUTED ⚠]` marker (§3.6b) and the 5-level truth vocabulary (§3.6c) for risk (`health_banner.j2`, `continuity_exec_summary.j2`), milestone (`milestone_rows.j2`), and assumption (`lookback.j2`) sections. §3.6b/§3.6c's rendering rule updated below to match the actual, verified implementation convention: inline `style="..."` attributes, not `<span class="...">` CSS classes — this codebase's HTML surfaces have no `<style>` blocks anywhere (Outlook-safe rendering constraint), so a literal CSS-class instruction was never implementable as written.

- Last updated: 2026-07-07 — Incorporated `specs/activation.md` (archived to `.archive/specs/activation.md`) UX-relevant surfaces into §12.10: the REV triage queue's `why:` EXPLAIN-min sub-line (extraction rationale, so an operator can verify a claim without opening the source EML) and the milestone section's degraded-to-legacy banner (no silent fallback when the `ProgramReality` read path is rolled back). Both surfaces already existed structurally (rationale field, rollback env flag); this is the first time they are documented as binding UX contract. No newsletter-body layout changes — both surfaces are triage-CLI/section-banner only.

- Last updated: 2026-06-29 — No new reader-visible surfaces. The WS-1 read-path migration (S-8c/S-8d overlays for commitment + workstream/ownership families) is internal; rendered newsletter/nudge output is unchanged because the overlays are inactive until a family's SoR mode is flipped (which itself awaits S-9e corpus certification). The remaining work (corpus κ-certification, Q7 extractor promotion, S-10a Azure CS provisioning) is operator/human-paced and has no UX surface today.

- Last updated: 2026-06-27 — Folded consolidated REV/NCFL operator-surface decisions into the canonical UX spec. No new reader-visible newsletter chrome is introduced. `vertex context apply` / `apply-batch` are now real governed operator actions with preview/fresh-hash/repair states, `doctor --rev-health` may report Teams as an accepted limitation, and deliverable/incident authority remains Phase 2 even when scaffolded internally.

- Last updated: 2026-06-25 — W5-3 EML pseudonymization: `vertex rev run` on EML sources now replaces person display names extracted from From/To/Cc/Bcc headers with stable `PERSON_N` tokens before the text reaches the extraction stage. The token→original mapping is stored in the hydrated content's route metadata for entity binding. No reader-visible change: newsletter output is not affected (pseudonymization applies to LLM-ingested canonical text only, not to rendered output). Privacy invariant strengthened: display names of senders/recipients no longer transmitted to the external model. `vertex doctor --rev-health` completeness vector (`RealityCompletenessVector` 3-area: context coverage / reality integrity / model calibration) now reported; no single published score until the corpus floor (G-floor) passes. `vertex ledger replay --family <family>` added for selective family replay during correction propagation.

- Last updated: 2026-06-24 — Consolidated REV (Program-Context Intelligence) UX contract into §12.6.5. `vertex rev run` adds no reader-visible newsletter chrome; staged candidates enter the existing PENDING/triage queue. `vertex doctor --rev-health` is a new diagnostic block (enumeration-completion distribution, verification-assertion distribution, Prompt-Shields mode, hydration fallback rate, pending-queue age). Privacy invariant: no raw mailbox addresses, real subjects, or participant identities in diagnostic output. Working spec archived to `.archive/specs/program-context-intelligence.md`.

- Last updated: 2026-06-22 — Folded `specs/nudge-gaps.md` into the canonical nudge UX contract and archived the working spec. The nudge surface now documents ProgramReality-driven subject urgency, audience-policy-governed recipient resolution, and lifecycle-v2 attestation semantics (`--mark-sent` / `--sent-at`) as the moment cooldown begins.

- Last updated: 2026-06-21 — Consolidated WorkIQ structured-retrieval UX into §12.6.4. FQ-01 adds no reader-visible newsletter chrome; it preserves the existing pending-review and source-health surfaces while keeping raw mailbox qualification evidence private. The working specification was archived locally under `.archive/specs/fix-workiq.md`.

- Last updated: 2026-06-19 — Consolidated the WorkIQ/M365 newsletter-enrichment UX contract into the core UX spec. AI-generated newsletter sections are now canonically defined to render section-level source footnotes when they incorporate approved M365 evidence, and approved prior-issue feedback/reference-doc context is treated as behind-the-scenes drafting input rather than reader-visible chrome. The working spec `specs/newsletter-workiq.md` is archived locally under `.archive/specs/`.

- Last updated: 2026-06-17 — Companion docs updated to remove stale `backlog.md` reference (archived to `.archive/specs/`). Gap register and backlog feature specs incorporated into core specs and archived. Status date advanced to 2026-06-17.

- Last updated: 2026-06-12 — Incorporated `specs/data-model.md` (event-sourced program ledger) into core specs. §12.10 (new): `vertex ledger status/triage/verify/backfill/redact` and `vertex discover candidates` output format specifications. All ledger CLI surfaces follow standard §12.1 philosophy (one line per stage, quiet by default, semantic exit codes). Data-model spec archived to `.archive/specs/data-model.md`.

- Last updated: 2026-06-10 — Spec consolidation: re-debt.md and remains.md archived to `.archive/specs/`; all `remains.md` forward-references updated to `backlog.md`. Reality substrate UX surfaces (truth-level badges §3.6c, exec-summary min-truth filter §3.7, disputed badge §3.6b) already implemented in WI-8.0/WI-3.9 (2026-06-15). `vertex triage --full` is the daily front door (all AttentionKind items, signal review queue, pending_actuations, conflicts). `vertex reality status` shows SoR mode, fact counts, truth-level distribution, conflict summary, and QG-27 gate status.
- Last updated: 2026-06-15 — §3.6b (new, WI-3.9 mini-delta): `[DISPUTED ⚠]` badge style and placement (color, bg, font, padding, surfaces); "⚠ includes unconfirmed sources" footer line for sections containing RAW_OBSERVED facts; dispute/confirmation invariants. Sufficient for Phase 4–7 implementers. WI-8.0 may refine style but cannot rename tokens or change meaning.
- Last updated: 2026-06-09 — Phase 4/5/6 debt-remediation wave closed. New CLI surfaces: `vertex doctor --source-waivers` (schema validation + expiry), `vertex rollback --drill [--archetype] [--notes]` (sandbox simulation with sandbox output summary and proof recording), `vertex facts dual-read-log/pin-snapshot/detect-drift` (fact-store sustained-shadow and drift-detection surfaces). §12.9 updated to reflect `rollback --drill` dry-run sandbox output and `facts` parity/pin/drift one-liner output format. Working docs archived to `.archive/specs/`; remaining work in [backlog.md](backlog.md).
- Last updated: 2026-06-02 — Folded autonomous M365 source-discovery CLI behavior from `discover.md` into the canonical UX spec: `doctor --operator-gates` now has a six-category discovery queue, `integration candidates` / `explain-source` are privacy-safe by default, and candidate review commands are treated as first-class operator surfaces.
- Last updated: 2026-06-02 — §12.9 rollback surface now reflects current fail-loud behavior: `vertex rollback` exits non-zero when no checkpoints exist and `--dry-run`/restore both enumerate the checkpointed paths being restored.
- Last updated: 2026-06-02 — §12.6.2 now specifies `vertex doctor --checkpoints`: checkpoint inventory/coverage output for rollback readiness, including the fail-loud warning when a program has confirmed history but no checkpoint yet.
- Last updated: 2026-06-02 — §12.6.2 now specifies `vertex doctor --storage`: retention warning once quarterly-history buildup makes `archive-journals` due, trajectory footprint summary, and SQLite/reality-DB WAL + integrity reporting.
- Last updated: 2026-06-02 — P1 scope decision applied: UX scope now explicitly follows the Microsoft-TPM-program archetype/exclusion boundary, with broader TPM/EM/global/non-ADO support treated as roadmap rather than current surface contract.
- Last updated: 2026-06-02 — P7 spec-drift closure: §12.6.2 clarified required vs optional `doctor --channels` source-health messaging; §12.9 now labels richer `facts`/`connectors`/`rollback` examples as planned command-run output rather than guaranteed help-surface behavior.
- Last updated: 2026-06-01 — §12.6.2 (new): doctor signal-fidelity diagnostics (source health in `--channels`, ConversionFidelity matrix, ETA credibility, recurring gate failures, override streaks, external dependencies); §12.9 (new): `facts`/`connectors`/`rollback` CLI output + pre-draft `--steering` prompt; §3.6a (new): ETA slip-history strikethrough rendering rule. CLI/rendering surfaces only. Reflects the implemented (coding-agent) portion of signals.md. Remaining `[OPERATOR]`/`[HUMAN GATE]` work from `signals.md` and `backlog.md` is consolidated into [backlog.md](backlog.md); both are archived to `.archive/specs/`.
- Last updated: 2026-05-30 — §10.3 (new): Teams chart summary format; §14.1: chart image rendering rules (608px frame, border, provenance footer, degraded banner); §15.3 (new): chart accessibility requirements; §17: chart items added to rendering test checklist. Reflects charts.md implementation (archived to `.archive/specs/`).
- Last updated: 2026-05-29 — §3.6: DFD governance annotation rendering note; §12.8 (new): `vertex hints` CLI output format spec. Reflects hands-off.md implementation (archived to `.archive/specs/`).
- Last updated: 2026-05-27 — §12.6: doctor --context context health diagnostics and --fix-hints guidance display; §18.3: fleet context health column layout rules. No new visual rendering surfaces; all changes are CLI terminal output only.
- Last updated: 2026-05-27 — No visual surface changes from UIL (Unified Integration Layer) work; all UIL changes are backend/CLI only.
- Last updated: 2026-05-23 — Refreshed operational status after backlog consolidation.

## Table of Contents

1. [Design Philosophy](#1-design-philosophy)
2. [Design Tokens](#2-design-tokens)
3. [Newsletter Output — Detailed Archetype](#3-newsletter-output--detailed-archetype)
4. [Newsletter Output — Continuity Layout](#4-newsletter-output--continuity-layout)
5. [Condensed Daily Archetype](#5-condensed-daily-archetype)
6. [Narrative Archetype](#6-narrative-archetype)
7. [Lookback Archetype](#7-lookback-archetype)
8. [Deck Archetype](#8-deck-archetype)
9. [Reviewer Pane](#9-reviewer-pane)
10. [Teams Markdown Output](#10-teams-markdown-output)
11. [Freshness Report](#11-freshness-report)
12. [CLI Experience](#12-cli-experience)
13. [EML File Output](#13-eml-file-output)
14. [Outlook HTML Constraints](#14-outlook-html-constraints)
15. [Accessibility](#15-accessibility)
16. [Mobile Responsiveness](#16-mobile-responsiveness)
17. [Rendering Test Checklist](#17-rendering-test-checklist)

---

## §1 Design Philosophy

### 1.1 Three-Speed Reading Model

Every newsletter surface is structured for three reading speeds:

| Speed | Time Budget | Content Served |
|-------|------------|---------------|
| **Glance** | ≤10 seconds | Health Banner + Navigation Bar — color-coded risk, no reading required |
| **Scan** | ≤60 seconds | Top 3 Now + What Changed + Scorecards — delta-first, 80% of value |
| **Deep dive** | 2–8 minutes | Executive Summary + Workstream sections — narrative with attribution |

If a visual element does not serve at least one speed, it is excluded.

**Current surface posture (2026-06-09):** Weekly Acme HTML, Teams/Markdown, reviewer pane, nudge EML, freshness HTML, CLI, proposal review, and daily dry-run surfaces are implemented. `acme_daily` renders with data/config warnings rather than code failures. `acme_nudge` dry-run generated an owner-ready consolidated EML. `acme_lt_deck` needs a rerun because the last online dry-run was interrupted mid-fetch; `acme_quarterly` is gated on enough confirmed weekly issues. The open UX work is therefore validation and operator adoption rather than new layout primitives, with accessibility, Outlook, and mobile checks still tracked in [backlog.md](backlog.md).

### 1.2 Color = Risk. Always. Only.

Color is never decorative. The only colors in any Vertex surface are the five risk-level colors (§2.1) and the monochrome palette (§2.4). Branding, headers, navigation, and borders are monochrome so that color always signals risk level.

### 1.3 Delta Over State

The default posture is "what changed since last issue?" not "what is the current state?"
- What Changed section precedes Scorecards in assembly order.
- Every scorecard cell includes a delta badge.
- Every workstream blurb leads with its delta.
- Date references are always human dates ("vs May 5"), never issue numbers ("vs Issue 76").

### 1.4 Show-by-Exception

Silence is information. Unchanged dimensions get a muted "— no change" badge with no narrative space consumed. Sections with no delta may be omitted entirely (via `hide_details: true`).

Focused-edition exception: when a section still carries authored continuity context and registry preview relevance indicates it remains report-worthy, the section may stay visible even without a fresh delta so the surface does not accidentally erase an important lane.

### 1.5 Attribution Earns Trust

Two mechanisms:
- **Inline:** `🔗` icon per table row → ADO work item URL
- **Section-level:** "View N items in ADO →" aggregate link at section footer

Tier 4 — Catalog provenance. For Kusto-sourced tiles, the footer appends a single line in 11px secondary text: `Source: Dashboard "<dashboard_name>" page "<page_name>" query <query_ref>.` Hidden when `catalog_source` is null. Dashboard name links to `https://dataexplorer.azure.com/dashboards/<dashboard_id>` when present.

Every published claim must cite a source. The reviewer pane (§9) provides full evidence for the author; the published newsletter shows Tier 1/2 attribution for narrative claims and Tier 4 provenance on Kusto tiles when catalog metadata is available.

When a workstream blurb is AI-seeded from approved M365 evidence, the published newsletter renders a compact section footnote of the form `Signal sources: ...` with deduplicated source labels and per-source dates. For SharePoint LT deck sources the footnote entry uses `[Per LT deck, <date>]`; for SharePoint reference docs: `[Per <doc title>, <date>]`. Drafting-time helpers such as approved feedback-thread context, ADO telemetry summaries, and reference-doc updates inform the copy but do not add new reader-visible UI beyond the standard attribution footnote.

### 1.6 Author = Editor, Not Assembler

The system surfaces evidence; the author applies judgment. The issue cycle is a multi-command editorial workflow:

```
vertex gather → vertex triage → vertex report --dry-run → vertex edit →
vertex override → vertex review-sections → vertex review-full → vertex confirm
```

---

## §2 Design Tokens

### 2.1 Risk Color Palette

The canonical source is `src/core/jinja_filters.py → RISK_COLORS`. These are the ONLY non-monochrome colors in any Vertex surface.

| Level | Background | Foreground | Border | Icon | Label |
|-------|-----------|-----------|--------|------|-------|
| **High** | `#E97132` | `#FFFFFF` | `#FFFFFF` | 🔴 | "High" |
| **Medium** | `#FFE699` | `#000000` | `#BF8F00` | 🟡 | "Medium" |
| **Low** | `#B4E5A2` | `#000000` | `#4EA72E` | 🟢 | "Low" |
| **Done** | `#4EA72E` | `#FFFFFF` | `#FFFFFF` | ✅ | "Done" |
| **Unknown / Blocked** | `#C00000` | `#FFFFFF` | `#FFFFFF` | ⚪ | "Needs Input" |

### 2.2 Delta Badge Tokens

The canonical source is `src/core/jinja_filters.py → DELTA_COLORS`.

| Kind | Foreground | Background | Text Template | Sort Order |
|------|-----------|-----------|--------------|-----------|
| `risk_up` | `#991B1B` | `#FEE2E2` | `▲ was {old}` | 1 |
| `new` | `#1E40AF` | `#DBEAFE` | `● NEW` | 2 |
| `eta_changed` | `#92400E` | `#FEF3C7` | `ETA: {old} → {new}` | 3 |
| `risk_down` | `#065F46` | `#D1FAE5` | `▼ was {old}` | 4 |
| `closed` | `#4B5563` | `#F3F4F6` | `✓ DONE` | 5 |
| `owner_changed` | `#4B5563` | `#F3F4F6` | `→ {new_owner}` | 6 |
| `unchanged` | `#9CA3AF` | `#F3F4F6` | `— no change` | 7 |

### 2.3 Top 3 Item Tokens

| Type | Icon | Border Color |
|------|------|-------------|
| `decision` / `ask` | 🔴 | `#991B1B` |
| `risk` / `watch` | 🟡 | `#92400E` |
| `improved` / `win` | 🟢 | `#065F46` |

### 2.4 Monochrome Palette

| Token | Hex | Usage |
|-------|-----|-------|
| `--text-primary` | `#111827` | Headings, dimension names, numbers, body text |
| `--text-secondary` | `#374151` | Body text, blurbs, chapter titles |
| `--text-tertiary` | `#4B5563` | Owner names, metadata, status text, sparklines |
| `--text-muted` | `#6B7280` | Table headers, nav bar, section eyebrows, citations, footer |
| `--text-faint` | `#9CA3AF` | Provenance footer, timestamps |
| `--bg-surface` | `#FFFFFF` | Main card background |
| `--bg-subtle` | `#F9FAFB` | Page background, table headers, nav bar, metric tiles |
| `--border` | `#E5E7EB` | All horizontal rules, table borders, card borders |
| `--border-light` | `#D1D5DB` | Nav separator, risk load bar border, dry-run dashed border |
| `--link` | `#2563EB` | All hyperlinks (ADO, nav, reference) |
| `--link-hover` | `#1D4ED8` | Hyperlink hover (browser only) |
| `--warning-bg` | `#FFFBEB` | Warning boxes (missing narrative, dependency cascade) |
| `--warning-border` | `#F59E0B` | Warning box border |
| `--warning-text` | `#92400E` | Warning text |
| `--degraded-text` | `#9A3412` | Degraded Kusto section message |

### 2.5 Typography Scale

| Element | Size | Weight | Color | Line Height |
|---------|------|--------|-------|------------|
| Newsletter title | 20px | 600 | `#111827` | 1.3 |
| Section header (H2) | 16px | 600 | `#111827` | 1.4 |
| Chapter title | 18px | 600 | `#111827` | 1.4 |
| Subsection header (H3) | 14px | 600 | `#374151` | 1.3 |
| Body text | 14px | 400 | `#374151` | 1.6 |
| Section eyebrow | 12px | 600/700 | `#6B7280` | 1.4 |
| Table header | 11px | 600 | `#6B7280` | 1.4 |
| Table cell | 13px | 400 | `#111827` | 1.5 |
| Caption / metadata | 11px | 400 | `#9CA3AF` | 1.3 |
| Navigation bar | 12px | — | `#2563EB` | 1.4 |
| Risk chip text | 12px | 600 | per risk color | 1.4 |
| Delta badge text | 12px | 600 | per delta color | 1.4 |
| Provenance | 11px | 400 | `#6B7280` | 1.5 |

All section eyebrows use `text-transform: uppercase` with `letter-spacing: 0.05em`.

### 2.6 Spacing Rhythm

Base unit: **8px**.

| Context | Spacing |
|---------|---------|
| Section gaps (between cards) | 16px (`margin: 0 0 16px 0` on outer tables) |
| Continuity chapter gaps | 18px |
| Card internal padding | 14px–20px horizontal, 8px–16px vertical |
| Table row cell padding | 8px vertical, 10px–12px horizontal |
| Paragraph gaps | 10px–12px |
| Tile spacing (scorecard bands) | `border-spacing: 0 4px` |
| Counter tile spacing | `border-spacing: 8px 0` |

### 2.7 Font Stack

```
Segoe UI, -apple-system, Roboto, Helvetica, Arial, sans-serif
```

Applied to `<body>` in `base.email.j2`. Monospace for manifest IDs: `Consolas, 'Courier New', monospace`. Sparklines use `'Segoe UI Symbol', 'Apple Symbols', 'Noto Sans Symbols', monospace`.

---

## §3 Newsletter Output — Detailed Archetype

The primary weekly newsletter uses `archetypes/detailed.j2` extending `base.email.j2`. Section dispatch is driven by `ordered_sections` with kind-based partial selection.

### 3.1 Outer Structure (`base.email.j2`)

Three nested `<table role="presentation">`:
1. **Page table:** Full-width centering, `background-color: #F9FAFB`
2. **Container table:** `width="680"`, `background-color: #FFFFFF`, `border: 1px solid #E5E7EB`
3. **Content table:** `width="640"` — all partials render within this 640px content area

**Header block:** Title (20px/600), optional `header_label` (eyebrow), subtitle, bottom border.

**Preheader:** Hidden `<div>` for email client preview text (`display: none; max-height: 0; overflow: hidden`). This is the one allowed non-layout `<div>` in the email surface; layout remains table-based.

**Footer block:** Conditionally shown via `show_footer`. Includes orientation footer and provenance footer.

### 3.2 Navigation Bar (`partials/nav_bar.j2`)

**Condition:** Rendered only for `detailed`, `focused`, `lookback` editions.

**Note:** The `focused` edition type uses the same templates and partials as `detailed`. The difference is in `template_contract.yaml` ordering and visibility rules — focused editions may show fewer sections or a different scorecard subset, but authored sections can remain visible when the registry-backed preview indicates they still matter for continuity.

| Property | Value |
|----------|-------|
| Background | `#F9FAFB` |
| Border | `1px solid #E5E7EB` |
| Link cells | `padding: 8px 10px; font-size: 12px; color: #2563EB` |
| Separator | `|` character in `color: #D1D5DB` |
| Margin | `0 0 16px 0` |

**Nav items:** Health → Decisions → Scorecards → Changes → Details

Jump links use `#anchor` references. They work in browser/OWA but fail in Outlook reading pane — the nav bar serves as a visual TOC regardless.

### 3.3 Health Banner (`partials/health_banner.j2`)

The banner answers "Is this program healthy?" in ≤10 seconds.

**Health state resolution:**

| State | Condition | Banner BG | Banner FG |
|-------|-----------|----------|----------|
| HEALTHY | high=0, medium=0, total>0 | `#D1FAE5` | `#047857` |
| CRITICAL | high≥3 | `#FEE2E2` | `#B91C1C` |
| AT RISK | high>0 | `#FEF3C7` | `#92400E` |
| ON TRACK | medium>0 | `#FEF3C7` | `#92400E` |
| NEEDS INPUT | else | `#F3F4F6` | `#4B5563` |

**Banner structure:**
- **State label:** 12px/700, uppercase with 0.05em letter-spacing
- **BLUF text:** 14px/400, `color: #111827` — one-line health summary from author override
- **Forecast:** 13px, `color: #111827` — optional ETA forecast summary
- **Leadership ask:** 14px/700, `color: #374151`
- **Read time:** 12px, `color: #4B5563`

**Counter tiles:** Four 72px-wide tiles for High/Medium/Low/Done counts.

| Tile | Background | Foreground |
|------|-----------|-----------|
| High | `#FEE2E2` | `#B91C1C` |
| Medium | `#FEF3C7` | `#92400E` |
| Low | `#D1FAE5` | `#047857` |
| Done | `#F3F4F6` | `#4B5563` |

Tile label: 11px/700. Tile number: 18px/700. Border: `1px solid #E5E7EB`.

**Risk Load bar:** 6px height. Fill color: `#991B1B` (high≥3), `#F59E0B` (high>0 or medium>0), `#047857` (all green). Empty segment: `#F3F4F6`.

### 3.4 Top 3 Now (`partials/top_3_now.j2`)

Author-curated — **never auto-generated**. Max 3 items, ≤25 words each. Source: `overrides.yaml → top_3_now[]`.

**Section header:** "DECISIONS & SIGNALS" — 12px/600 uppercase eyebrow.

**Item rendering:**
- `border-left: 3px solid {risk_color}` — solid for confirmed, dashed for `.suggested`
- Label: 11px/700, uppercase, colored per item type
- Text: 14px/400, `color: #111827`
- Meta (owner, date): 13px, `color: #4B5563`
- Links: `color: #2563EB; text-decoration: underline`
- Item padding: `12px 16px`
- Row separator: `border-top: 1px solid #E5E7EB`

**Auto-suggestions:** System-generated top items shown with dashed border. Author acknowledges or replaces.

**Forwarding context:** Optional manager note at top: label in 700-weight uppercase, quote in 14px/400 body.

**Fallback (no items):** Color-coded fallback based on health state.

### 3.5 What Changed (`partials/what_changed.j2`)

**Section header:** "WHAT CHANGED" — 12px/600 uppercase eyebrow.

**Max visible rows:** 5 (overflow: "and N more changes…" text).

**Delta row:**
- Kind label: 11px/600, uppercase, colored per delta badge tokens (§2.2)
- Title: 13px, `color: #111827`
- Detail: 12px, `color: #4B5563`
- ADO link: `color: #2563EB; text-decoration: underline`
- Row margin: `0 0 6px 0`
- Cell padding: `8px 12px`

**Sort order:** risk_up → new → ETA shifts → risk_down → closed.

**Date reference rule:** Always human dates ("vs May 5"), never issue numbers.

### 3.6 Scorecard (`partials/scorecard.j2`)

Two rendering modes based on dimension label length and `mobile_safe_scorecards` setting:

**Row-fallback mode** (labels >10 chars or explicitly set):

| Column | Width | Font | Color |
|--------|-------|------|-------|
| Dimension | 42% | 12px/600 | `#111827` |
| Risk | 24% | 12px | per risk color |
| Trend | 20% | 12px | `#4B5563` |
| Date | 14% | 12px | `#4B5563` |

Row: `border: 1px solid #E5E7EB; margin-bottom: 4px`.

**Tile mode** (5-column grid batches):

| Element | Style |
|---------|-------|
| Tile background | Per risk color (`risk_bg`) |
| Tile border | `1px solid #E5E7EB` |
| Tile padding | `8px 8px 6px 8px` |
| Label | 11px/700, `color: #111827`, uppercase |
| Risk label | 10px/700, per risk color (`risk_fg`) |
| Sparkline | 13px, `color: #4B5563`, symbol font |
| ETA | 10px, `color: #4B5563` |

Awaiting-data tile. For KPIs with `validated: false` and `refresh_on_gather: false` (catalog placeholders): label + `Awaiting data` in 14px secondary text + `Owner: <owner_alias>` in 11px secondary text. Background `#FAFAFA`; border `1px solid #E1E1E1`.

Awaiting-validation tile. For KPIs with `validated: false` and `refresh_on_gather: true` (the transitional state between the Wave A merge and Wave B live probe success): label + `Awaiting validation - gather pending` in 14px secondary text, no owner footer. Background `#FAFAFA`; border `1px dashed #E1E1E1`.

Aggregate-incidents tile. Schema: `{Sev0Count, Sev1Count, Sev2Count, OldestAgeHours, OldestIncidentId, OldestUrl}`. When the Sev0/1/2 sum is greater than zero, render three count badges (Sev0 red `#A4262C`, Sev1 amber `#BC7C00`, Sev2 slate `#605E5C`) plus a one-line footer `Oldest: <Age>h - <IncidentId>` hyperlinked to `OldestUrl`. When the sum is zero, render a single green `✓ No active Sev 0-2 (Acme)` / `✓ No active Sev 0-2 (DD)` tile with no footer.

**Cell rendering rules:**

| Risk | Background | Text | Left Border |
|------|-----------|------|------------|
| High | `#FEE2E2` | `#991B1B` | 2px `#991B1B` (row mode) |
| Medium | `#FEF3C7` | `#92400E` | — |
| Low | `#D1FAE5` | `#065F46` | — |
| Done | `#DBEAFE` | `#1E40AF` | — |
| ❓ Needs Input | `#F3F4F6` | `#4B5563` | — |

**DFD governance annotation:** When `GovernanceState.dfd_date` is set, append a governance badge to the scorecard header row: imminent (<14 days) → red `#A4262C` `DFD: <date>`; near (<30 days) → amber `#BC7C00`; far (≥30 days) → slate `#605E5C`. No badge when `dfd_date` is absent. Badge renders as a `<span>` inline after the edition header text, font-weight 600, 10px.

**Delta trend in scorecard:** Uses §2.2 delta badge tokens inline.

**Footer:** Unchanged count (`12px, #6B7280`), ADO view link (`13px, #2563EB`).

### 3.6a ETA Slip History (strikethrough)

When a dimension or work item has slipped its ETA one or more times, the ETA cell renders the slip chain with each superseded date struck through, oldest-to-newest, ending in the current ETA in normal weight (FR-SG-22, sourced from `trajectory_analyzer.build_slip_history_markdown()`):

```
~~06/15~~ ~~06/20~~ 06/27
```

Rendering rules:
- Struck-through dates use `<s>`/`text-decoration: line-through` at the cell's existing font size and `#6B7280` (faint); the current (final) date uses the cell's normal `#4B5563`.
- Render the chain only when ≥1 prior ETA exists in trajectory/chronicle history; a never-slipped item shows the single current date with no strikethrough.
- The chain is capped at the most recent 3 prior dates plus the current date to preserve cell width; older slips collapse into a leading `…` rendered in faint text.
- This is a presentation of trajectory data only; it never invents dates and degrades gracefully (single date) when no slip history is available.

### 3.6b Disputed Fact Marker and Unconfirmed-Source Footer *(WI-3.9 mini-delta)*

When a fact or dimension value is disputed (i.e., an unresolved `fact.conflict` exists in the snapshot for that entity), render the `[DISPUTED ⚠]` badge immediately after the value or dimension label. This is the minimum-viable visual contract for Phase 4–7 implementers.

**`[DISPUTED ⚠]` badge:**

| Property | Value |
|----------|-------|
| Text | `[DISPUTED ⚠]` |
| Font size | 10px |
| Font weight | 700 |
| Color | `#B91C1C` (same red as `High` risk text) |
| Background | `#FEE2E2` (same red tint as `High` risk bg) |
| Padding | `1px 4px` |
| Border-radius | `2px` |
| Display | inline, after value with `4px` left gap |
| Surfaces | newsletter HTML, reviewer HTML, triage CLI terminal output |

Badge renders as an inline `<span style="...">` element (not a CSS class — see the 2026-07-08 changelog entry; this codebase's HTML surfaces have no `<style>` blocks, an Outlook-safe rendering constraint) in HTML surfaces, matching the property values above verbatim; as literal text `[DISPUTED ⚠]` in CLI terminal output. Implemented in `templates/partials/truth_badge.j2`.

**"⚠ includes unconfirmed sources" footer line:**

When a rendered section contains one or more facts with `truth_level < SOURCE_VALIDATED` (i.e., RAW_OBSERVED), append a single-line advisory footer to that section:

| Property | Value |
|----------|-------|
| Text | `⚠ includes unconfirmed sources` |
| Font size | 11px |
| Color | `#92400E` (amber warning text) |
| Placement | below the section's last content row, above the section border/separator |
| Surfaces | newsletter HTML workstream sections, reviewer HTML, triage CLI terminal output |

In CLI terminal output, emit the line as plain text prefixed with a blank line: `⚠ includes unconfirmed sources`.

**Invariants (WI-8.0 may refine style but cannot change meaning):**
- The token `[DISPUTED ⚠]` is reserved for unresolved material conflicts only — never use it for minor conflicts or staleness warnings.
- The `⚠ includes unconfirmed sources` line is informational only; it does not block publishing unless QG-27 fires.
- Both markers are controlled by Zone A (`program_reality.py` flags) injected at render time; templates must not re-derive dispute/confirmation state independently.

### 3.6c Truth-Level Badge Vocabulary *(WI-8.0)*

Applies to: `vertex reality status`, triage CLI output, and any surface that renders `ProgramReality`-sourced facts. Each fact carries a `truth_level` derived by Zone A. The five levels map to the following visual tokens:

| Level | Label | Color | Background | Usage |
|-------|-------|-------|-----------|-------|
| `GOVERNANCE_LOCKED` | `🔒 LOCKED` | `#1E3A8A` | `#DBEAFE` | Management-locked facts (policy, DFD, signed-off commitments) |
| `HUMAN_CONFIRMED` | `✔ CONFIRMED` | `#15803D` | `#DCFCE7` | Human-reviewed and accepted via confirm workflow |
| `CORROBORATED` | `◆ CORROBORATED` | `#0D9488` | `#CCFBF1` | Multiple independent sources agree; no human review yet |
| `SOURCE_VALIDATED` | `● VALIDATED` | `#4B5563` | `#F3F4F6` | Passed structural validation; single authoritative source |
| `RAW_OBSERVED` | `○ UNCONFIRMED` | `#B45309` | `#FEF3C7` | Raw ingestion; unvalidated; cite with ⚠ footer |

**Rendering rules:**
- In HTML surfaces, render as an inline `<span style="...">LABEL</span>` element with the specified color/bg applied directly via the `style` attribute (not a CSS class — see the 2026-07-08 changelog entry). Implemented in `templates/partials/truth_badge.j2`.
- In CLI terminal output, render as the plain label text in brackets (e.g., `[CONFIRMED]`).
- Truth badges appear inline after the fact value, with `4px` left gap (same as `[DISPUTED ⚠]`).
- `RAW_OBSERVED` facts always carry the `⚠ includes unconfirmed sources` section footer (§3.6b).
- Truth level is computed by `derive_truth_level()` in `src/core/truth_model.py`; templates never recompute it.
- Live surfaces as of 2026-07-08: newsletter risk (`health_banner.j2`, `continuity_exec_summary.j2`), milestone (`milestone_rows.j2`), and lookback assumption (`lookback.j2`) sections. Dependency, action, decision, commitment, and workstream carry no badge because direct investigation (`specs/fix-data-flow.md`, archived) found no current main-newsletter per-fact section to attach one to for those families.

### 3.7 Executive Summary (`partials/exec_summary.j2`) *(exec-narrative min-truth filter, WI-8.0)*

**Section header:** "EXECUTIVE SUMMARY" — 12px/600 uppercase eyebrow.

**Body:** 14px, line-height 1.6, `color: #111827`. Detailed and focused editions currently cap the executive summary at 150 words via the edition-aware verbosity enforcer.

**Split on `<!-- state -->`:** Divides into "WHAT MOVED" (12px/700, `#374151`) and "WHERE WE ARE" (12px/700, `#4B5563`).

**Citations:** ≤3 shown as individual ADO links (12px, `#6B7280`). >3 → summary text.

**Min-truth filter (WI-8.0):** The executive summary must render only facts with `truth_level ≥ HUMAN_CONFIRMED`. Facts at `RAW_OBSERVED`, `SOURCE_VALIDATED`, or `CORROBORATED` must not appear in the executive summary narrative. This filter is applied by Zone A before the AI narrative draft is generated (it constrains the input corpus, not the AI output).

- If all facts for the exec summary section fall below the minimum truth threshold, render: `"No confirmed facts available for this summary period."` in 13px italic `#6B7280`.
- The filter applies to the detailed and narrative archetypes; the condensed daily archetype is exempt (it shows only health state, no narrative).
- This constraint is enforced by the Zone A `exec_summary_corpus_filter()` function which gates the input to the AI narrative stage; the AI layer receives only HUMAN_CONFIRMED+ facts.
- The min-truth filter does not apply to `[DISPUTED ⚠]` rendering — that is a factual marker placed by Zone A, not AI narrative content.

### 3.8 Workstream Deep Dive (`partials/workstream.j2`)

**Section header:** Title at 16px/600 `#111827` + risk chip + scope counts (13px, `#6B7280`).

**Narrative missing warning:**
```
padding: 8px 10px; background-color: #FFFBEB;
border: 1px solid #F59E0B; font-size: 13px; color: #92400E
```

**Dependency cascade warning:** Same warning box style with cascade label.

**Blurb:** 14px/400, `color: #374151`. Max 90 words, ≤4 sentences. First sentence must lead with delta.

**Edit locator (dry-run only):** `10px, #6B7280, border: 1px dashed #E5E7EB; padding: 3px 8px` — shows file path for direct editing.

**Item table:**

| Column | Width | Style |
|--------|-------|-------|
| Item | — | 13px, `#111827` |
| Owner | — | 13px, `#4B5563` |
| Status | — | 13px, `#4B5563` |
| ADO | — | `#2563EB`, underline |

Header: `11px/600 uppercase, #6B7280, bg: #F9FAFB, border-bottom: 1px solid #E5E7EB`. Max 5 items shown.

**ETA forecasts:** Rendered inline per item. Low-confidence items show annotation text from `forecast.annotation`.

**Sort:** High → Medium → Low → Done → Unknown; within risk: overdue → approaching (≤5 days) → future → alphabetical.

### 3.9 ADO Vitality Section (`partials/ado_vitality.j2`)

**Condition:** Rendered only when vitality data is available.

**Title bar:** `16px/700, #111827, bg: #F9FAFB, border-bottom: 1px solid #E5E7EB`.

**Body:** 14px/400, `color: #374151`.

**Metrics table:** Labels in 600-weight `#111827`, values right-aligned `#111827`.

Metrics shown: Items updated (count/total/percentage), freshness average days, leakage events, best documented item, trend summary.

### 3.10 Provenance Footer (`partials/provenance_footer.j2`)

**Style:** 11px/400, `color: #6B7280`, center-aligned. Manifest ID in `Consolas, 'Courier New', monospace`.

**Content:** Issue number, generation timestamp, data-as-of timestamp, manifest ID (first 8 chars), QG status (✅/⚠️/🔴), optional productivity dividend.

### 3.11 Orientation Footer (`partials/orientation_footer.j2`)

**Condition:** `edition.show_orientation` is true.

**Style:** `bg: #F9FAFB, border-top: 1px solid #E5E7EB`. All text: 11px, `color: #6B7280`. Title: 700-weight uppercase. Explains three-speed reading model, risk legend, sparkline key.

### 3.12 Assembly Order (Detailed)

1. Navigation Bar
2. Dispatch `ordered_sections` by kind:
   - `health` → Health Banner
   - `top_3` → Top 3 Now
   - `selected_changes` → What Changed
   - `scorecard` → Scorecard (one per scorecard)
   - `kusto` → Kusto Telemetry Section
   - `ado_vitality` → ADO Vitality Section
   - `exec_summary` → Executive Summary
   - `workstream` → Workstream Deep Dive (one per workstream)
3. Orientation Footer
4. Provenance Footer

Section order is driven by `template_contract.yaml → families.<name>.order[]` when present, otherwise by default order.

**Note on archetypes:** `digest.j2` is a legacy alias with identical structure to `condensed.j2` and is targeted for removal after 2026-12-31 once legacy references are cleaned up. The `focused` edition type routes to the same templates as `detailed` but may use a different `template_contract` family with different section ordering and visibility rules.

### 3.13 Kusto Telemetry Section (`partials/kusto_section.j2`)

Rendered within the detailed archetype when Kusto queries return data.

**Title:** 16px/600, `color: #111827`. **Metadata:** 13px, `color: #6B7280`.

**Rendering modes** (driven by `render_mode` field):
- **`metric_highlight`:** Three 33%-width metric tiles (`bg: #F9FAFB, border: 1px solid #E5E7EB`) with 11px uppercase label and 18px/600 value.
- **`chart_image`:** Base64 inline `<img>` with `max-width: 608px, border: 1px solid #E5E7EB`.
- **`table`:** Standard data table with 11px/600 uppercase headers, 13px cell text.

Phase-1 table notice. For KPIs with `render_as: table` and `refresh_on_gather: true`, the body may render the 13px regular notice `Data available - see CLI inspector (python cli.py inspect kusto --query <id>).` Wave H replaces this with the inline table renderer.

**Degraded state:** `color: #9A3412` message text when cluster is unreachable. Reference URL link as fallback.

**Caveats:** 12px, `color: #6B7280` — displayed below the section when present.

---

## §4 Newsletter Output — Continuity Layout

The continuity layout (`archetypes/continuity.j2`) is an alternative assembly for editions with `layout_mode: "continuity"`. It replaces the scorecard table + workstream section model with band scorecards + chapter narratives.

### 4.1 Assembly Order (Continuity)

1. Brand Header (optional — logo image, max 640px width)
2. Base header (title, header_label eyebrow, subtitle)
3. Edition Intro (12px italic, `color: #6B7280`)
4. Cadence Note (13px, `color: #374151`)
5. Scorecard Bands (one per band — colored tile rows)
6. ADO Vitality Section
7. Executive Summary (paragraph cards split on `<!-- state -->`)
8. Jump-to-Section Grid (3-column link blocks)
9. Chapters (one per chapter — narrative with detail rows)
10. Provenance Comment (dry-run only — dashed border)

### 4.2 Scorecard Band (`partials/continuity_scorecard_band.j2`)

Fixed-width table with `table-layout: fixed`. Each cell is colored per risk level:

| Element | Style |
|---------|-------|
| Cell background | Per risk color (`risk_bg`) |
| Cell border-top | `1px solid #E5E7EB` |
| Cell label | 10px/700, per risk `risk_fg`, uppercase with 0.02em spacing |
| Risk label | 12px/800, per risk `risk_fg` |
| ETA | 10px, per risk `risk_fg` |
| Band title | 12px/700, `color: #374151` |

Cells are clickable links to their chapter anchor.

### 4.3 Chapter (`partials/continuity_chapter.j2`)

**Title:** 18px/600, `color: #111827`.
**Note (narrative):** 14px, line-height 1.7, `color: #374151`.

**Detail table columns:**

| Column | Width | Content |
|--------|-------|---------|
| # | 32px | Row number |
| Workstream | 23% | Label (600-weight) + owner (`#6B7280`) |
| Status | flex | Issue text, approach text, state/ETA |
| Risk | 84px | Risk chip (pill format) |

Row heights: 8px–10px cell padding. Risk chip: `display: inline-block; padding: 4px 8px; border-radius: 999px`.

**Chapter owner:** 12px, `color: #6B7280`.

### 4.4 Jump-to-Section Grid (`partials/jump_to_section.j2`)

3-column `table-layout: fixed`. Each cell: `width: 33.33%`.

Link blocks: `padding: 8px 10px; border: 1px solid #E5E7EB; background-color: #F9FAFB; font-size: 12px; font-weight: 600; color: #374151`.

### 4.5 Continuity Executive Summary (`partials/continuity_exec_summary.j2`)

Split on `<!-- state -->` then `\n\n`. Each paragraph rendered as a card:

```
border: 1px solid #E5E7EB; background-color: #F9FAFB
```

Paragraph label: 10px/700, uppercase, `color: #6B7280`. Body: 14px, line-height 1.65, `color: #111827`.

---

## §5 Condensed Daily Archetype

The daily digest (`archetypes/condensed.j2`) targets ≤30 seconds read time and ≤400px email height.

### 5.1 Structure

Three inline card tables (no section dispatch):

1. **Daily Digest card** — Health icon + risk label + delta direction + metadata line
2. **Change Summary card** — New/closed/risk change/ETA change counts
3. **Top 3 Summary card** (conditional) — Item icons + text

**No scorecards, no deep dives, no executive summary.**

### 5.2 Styling

All cards: `border: 1px solid #E5E7EB; background-color: #FFFFFF; margin: 0 0 12px 0`. Section headers: 12px uppercase eyebrow.

### 5.3 Data Requirements

`health` (overall_risk, delta_direction), `edition` (issue_number, ado_data_as_of, manifest_id), `delta_counts` (new, closed, risk_up, risk_down, eta), `top_items[]`, `changes_url`.

### 5.4 Verbosity

Exec summary: max 75 words for condensed editions via the current edition-aware verbosity rules. No workstream blurbs.

---

## §6 Narrative Archetype

The narrative archetype (`archetypes/narrative.j2`) is used by Platform and similar programs without scorecards.

### 6.1 Assembly Order

1. Navigation Bar
2. Health Banner
3. Top 3 Now (conditional)
4. What Changed (conditional)
5. Kusto Sections (loop)
6. Workstream Deep Dives (loop — flat list, not section dispatch)
7. Executive Summary

### 6.2 Differences from Detailed

- **No `ordered_sections` dispatch** — uses flat loops
- **No scorecard partial** — workstreams render directly
- **Exec summary at end** — after workstreams, not before
- Verbosity: narrative editions currently target a 200-word executive summary and 150-word blurbs through the current edition-aware verbosity rules, with per-program overrides available via `editorial_rules.yaml`.

---

## §7 Lookback Archetype

The quarterly retrospective (`archetypes/lookback.j2`) aggregates confirmed archive data over a configurable window.

### 7.1 Assembly Order

1. Navigation Bar
2. **Retrospective Window card** — gray card (`bg: #F3F4F6, border: 1px solid #E5E7EB`) with window description
3. Section dispatch (same kind→partial mapping as detailed):
   - health, top_3, selected_changes, scorecard, kusto, ado_vitality, exec_summary, workstream
4. **Incident Learnings** (conditional) — bounded retrospective card group sourced from incident-journal entries that fall inside the lookback window
5. Orientation Footer + Provenance Footer

### 7.2 Retrospective Window Card

The only unique visual element: describes the lookback period. 12px eyebrow "Quarterly retrospective window", then preheader text.

### 7.3 Content

The lookback template uses the same section dispatch and partial templates as the detailed archetype. The distinction is in the data — items, scorecards, and metrics cover the retrospective window instead of the current week.

When retrospective incident data exists, render an `Incident Learnings` section after the main section dispatch and before the footer. Each learning card is compact and evidence-first: incident identifier, date, short summary, and the extracted learning/action text. This section is bounded to a small operator-readable set rather than a full incident dump.

---

## §8 Deck Archetype

The LT deck (`archetypes/deck.j2`) produces **plain Markdown** for PowerPoint paste.

### 8.1 Output Format

**Not HTML.** No outer table, no inline styles, no color. Emoji icons only.

### 8.2 Structure

```markdown
# Issue {N} — {date}

## Health
- {risk_icon} **{dimension}** — {risk_label}: {summary}

## Top Risks
- {risk_icon} **{text}** — {delta_text} (WI:{id})

## What Changed This Week
- {text}

## Data
- **{label}:** {value}

## Open Asks
- **{title}** — {detail}

## Closed Asks
- **{title}** — {detail}
```

### 8.3 Data

`DeckRenderContext`: `issue_number`, `issue_date_label`, `health_rows[]`, `top_risk_rows[]`, `change_rows[]`, `data_rows[]`, `open_ask_rows[]`, `closed_ask_rows[]`.

Each section has an empty-state fallback ("No scorecard dimensions are available…").

---

## §9 Reviewer Pane

The leadership review pane (`base.reviewer.j2`) is a **full web page** (not email-constrained). It uses CSS variables, grid layout, and interactive `<details>` elements. The same review payload also feeds per-section Adaptive Card drafts for Teams posting, but the browser pane remains the canonical rich review surface.

### 9.1 Layout

Two-pane grid: `grid-template-columns: minmax(0, 1.35fr) minmax(340px, 0.95fr)`.

**Left pane:** "Published View" — contains an `<iframe srcdoc="...">` embedding the newsletter HTML (min-height 760px).

**Right pane:** "Evidence" — scrollable evidence stack.

### 9.2 CSS Variables (`:root`)

`--page-bg`, `--panel-bg`, `--panel-border`, `--text-strong`, `--text-muted`, `--text-subtle`, `--chip-bg`, `--approved-bg/fg`, `--pending-bg/fg`, `--changes-bg/fg`, `--rejected-bg/fg`, `--link`, `--shadow`, `--radius: 18px`

### 9.3 Evidence Stack (Right Pane)

1. **Anticipated Questions** (`partials/reviewer_anticipated_questions.j2`) — AI-predicted leadership questions with suggested responses. CSS class-based styling (not inline). Each question card shows: reader, confidence badge, question text, suggested response, evidence lines.

2. **Owner Vitality Bars** (`partials/vitality_reviewer.j2`) — Per-owner vitality with horizontal bar chart. Bar fill uses inline `width` style only. Shows composite score, perfect-score badge, summary text.

3. **Coverage Gaps card** — Helicopter-altitude warning. Suppressed for satellite editions.

4. **Open Claims card** — Tracked commitments from prior confirmed issues.

5. **Milestone Timeline card** — Ordered milestone rows with target-date and completion-history context, plus schedule variance when present.

6. **Signal Threads card** — Approved signal conversations rendered as per-thread event timelines with metadata and timestamps.

7. **Open Asks card** — Decision requests pending resolution.

8. **Section reviews** — Each section as a `<details>` element (auto-open if not approved). Contains:
   - Summary: title + state icon + state label + reviewer + section_id
   - Published Text card
   - Why Drawer (nested `<details>`) — evidence packet with confidence, reviewer summary
   - What Changed card — delta rows
   - Evidence Packets card
   - Signals & Patterns card
   - Risk History card
   - Latest Note card
   - Update command: `vertex review-sections set --edition ... --section ... --state ...`

### 9.4 Status Bar

Grid of `status-chip` divs. States:

| State | Visual |
|-------|--------|
| `approved` | `--approved-bg` / `--approved-fg` |
| `pending` / `sent` | `--pending-bg` / `--pending-fg` |
| `changes_requested` | `--changes-bg` / `--changes-fg` |
| `skipped_no_delta` | `--chip-bg` / `--text-muted` |
| `rejected` | `--rejected-bg` / `--rejected-fg` |

### 9.5 Responsive

Collapses to single column at 1100px. Further adjustments at 720px.

The reviewer pane also ships with explicit `:focus-visible` states for interactive elements, `@media print` rules for printer-friendly export, and `prefers-color-scheme: dark` support because it is a browser surface rather than an Outlook-constrained email surface.

---

## §10 Teams Markdown Output

The Teams renderer (`base.teams.j2`) produces GitHub-Flavored Markdown compatible with Microsoft Teams.

### 10.1 Structure

```markdown
# {title}
**Issue {N}**
Data as of {timestamp}

## Program Health
{state_icon} **{state_label}**
{bluf}
...

## Decisions & Signals
...

## {scorecard_name}
...

## What Changed
...

## Executive Summary
...

## {workstream_title}
...

Manifest {id[:8]} · {qg_summary}
```

### 10.2 Section Dispatch

Uses the same `ordered_sections` kind-dispatch as detailed.j2 but renders each kind as Markdown:
- `health` → Risk counts with emoji, BLUF, forecast, leadership ask
- `top_3` → Forwarding context + item list with labels/owners
- `scorecard` → Dimension list with risk icons, sparklines, evidence stats
- `selected_changes` → First 5 delta rows
- `exec_summary` → Split on `<!-- state -->` into WHAT MOVED / WHERE WE ARE
- `kusto` → Metric highlights or table rendering
- `ado_vitality` → Items updated, freshness, leakage, best documented, trend
- `workstream` → Risk/counts, blurb, cascades, items[:5] with forecasts

### 10.3 Rendering Rules

- Tables >4 columns → bullet lists
- Colors → emoji icons + bold text
- Citations → footnote links `[¹](url)`
- No inline styles

---

### 10.3 Chart Section Format

For Kusto sections with `render_mode == "chart"` or `render_mode == "chart_image"`, Teams renders a text trend summary rather than an image:

```
{title}: {trend_description}. [View chart →]({reference_url})
```

If `trend_description` (mapped from `KustoSectionData.message`) is absent:

```
{title}. [View chart →]({reference_url})
```

When `reference_url` is absent, `#` is used as the fallback. Charts are never inlined as images in Teams output.


## §11 Freshness Report

Author-facing only — never published to readers. Generated by `vertex freshness`.

### 11.1 Severity Levels

| Severity | Icon | Condition |
|----------|------|-----------|
| 🛑 BLOCK | — | ETA 30+ days overdue AND risk High/Off Track |
| ⚠ WARN | — | ETA overdue OR stale 14+ days |
| ⏰ INFO | — | Approaching deadline (≤5 business days) OR status changed |
| ⚡ BAD FRESH | — | Updated recently but placeholder/copy-paste |

### 11.2 DRI Grouping

Items grouped by DRI (workstream owner). Per-DRI summary: open count, overdue count, stale count.

### 11.3 Teams Format

Per-DRI message with item list, ADO links including "Edit in ADO" deep links.

---

## §12 CLI Experience

### 12.1 Output Philosophy

- One line per stage during pipeline execution
- Quiet by default; `--verbose` / `--debug` for more detail
- Color in terminal only; stripped when piped
- Semantic exit codes (§12.3)

When catchup is enabled and unseen items exist, the CLI may emit a short session-start catchup banner before the requested command output. The banner is advisory, bounded, and summarizes the highest-salience unseen changes rather than replaying full history.

### 12.2 Dry-Run Behavior

`vertex report --dry-run` generates all output artifacts (HTML, EML, MD, manifest, snapshot) but:
- No archive writes
- No external sends
- Auto-opens HTML in browser (suppress with `--no-open`)
- DraftState written for `--diff` comparison on next run
- Edit locators shown in rendered HTML (file paths for `vertex edit`)

### 12.3 Exit Codes

| Code | Meaning | Typical Trigger |
|------|---------|-----------------|
| **0** | Clean | All gates passing, no warnings |
| **2** | Warnings | Non-blocking quality issues (forceable gates) |
| **3** | Blocks | Hard publish-blocks (❓ Needs Input, ban-list violations) |
| **4** | Corruption | Lock collision, snapshot corruption |

### 12.4 Diff Mode

`vertex report --diff` compares the current dry-run against the previous dry-run state:
- Scorecard override changes
- ADO data changes since last dry-run
- Exec summary text diff
- Section-level additions/removals

### 12.5 Interactive Override

`vertex override --edition <name>` walks through each dimension showing evidence. Options per dimension:
- [L]ow [M]edium [H]igh [D]one — set risk
- [C]lear — opt into derived risk
- [S]kip — leave unchanged
- [K]eep (Enter default) — keep current value

Optional summary ≤50 words. Backup to `.bak` before each write.

### 12.6 Doctor Diagnostics

`vertex doctor` validates: config files, template presence, ADO connectivity, archive index integrity, knowledge base referential integrity (`--kb`), override YAML validity, token freshness (<10 min → warn).

**`vertex doctor --context`** validates the 20 Plane 1 program YAML files and emits:

```
✅  Context health — L4 (healthy)  [or ❌ L0 / ⚠ L1–L3]
  Invariant violations:
    ❌ [WS-01] Workstream "ws_foo" missing description
    ⚠  [MS-02] Milestone "m1" target_date not set
  Staleness:
    ⚠  [Staleness] workstreams.yaml / ws_foo: roles.primary_owner.reviewed_at is 32 days stale (threshold: 30)
  Context gaps (ranked by impact):
    1. [gather / roles.primary_owner.email / ws_foo] — high impact (3 occurrences)
```

**`vertex doctor --context --fix-hints`** appends one `[Fix hint]` line per invariant violation and one per staleness flag with actionable remediation text. Fix hints are informational; exit code is unchanged.

Exit code for `--context`: 0 = L4, 2 = L2–L3 (warnings/gaps), 3 = L0–L1 (errors).

### 12.6.1 Confidence-Preserving CLI Rows

Human CLI surfaces that consume confidence-bearing Zone A records must preserve that confidence inline rather than flattening the row to source/status text only.

- `vertex signals --program <prog>` pending rows show: signal id, timestamp, source, workstream, review state, and confidence.
- `vertex signals review --program <prog>` review prompts show: signal id, timestamp, source, workstream, and confidence before the approval prompt.
- `vertex actions list|review --program <prog>` meeting-close summary blocks surface recurring cross-meeting patterns ahead of item-by-item review so operators can see repeated ownership and due-date threads before acting.

### 12.6.2 Signal-Fidelity Diagnostics

Doctor surfaces the signal-fidelity / fact-layer health checks as additional advisory blocks. All are informational unless tied to a hard gate; none change the exit code beyond the standard 0/2/3 mapping.

**`vertex doctor --channels`** appends a per-role `SourceHealth` summary derived from `slice_contracts.yaml` and gather telemetry:

```
Source health (by slice role):
  ✅ ado        healthy    (yield 42, fresh 1d)
  ⚠  telemetry  stale      (last fresh 9d, TTL 7d)        [waivable]
  ❌ decision   unbound    (no structured decision_sources) [structural — not waivable]
```

A required role that is `stale`, `zero_yield`, or `auth_failed` is forceable and may be waived in `source_waivers.yaml` with owner/date/reason; an `unbound` structural misconfiguration is non-waivable and blocks `confirm` via QG-SG-01. Optional roles use the same status vocabulary in the doctor output but remain warning-only and never escalate the sub-check to a blocking failure.

**`vertex doctor --storage`** emits checks for storage health and program directory layout:

- **`Storage Retention`** — `ok` until quarterly-history volume accumulates; once ≥8 confirmed issues exist with stale journal partitions, flips to `warn` with the exact `vertex archive-journals --program <prog> --before <week>` remediation.
- **`Trajectory Storage`, `Program SQLite`, `Reality DB`** — always print path, size, WAL size, `journal_mode`, and `integrity_check`; fact-store warns when DB is not under `~/.vertex/<prog>/`.
- **`DC-01 Root Cleanliness`** — `warn` when stale `.bak`/`.lock` backups remain at root or unrecognized files are present; `info` when `_spike/` exceeds 50 files. `ok` when the root matches the registered whitelist.
- **`DC-02 Runtime Layout`** — tracks transition of platform-owned files to `runtime/`. During the pre-migration transition window: `info` (pre_migration). After migration: `ok` (clean), `warn` (partial), or `fail` (split-brain — same file at root and `runtime/` simultaneously). Split-brain blocks `confirm` in strict mode. DC-02 also appears in the **default `vertex doctor` report** (post-Phase-1-B) as a non-blocking advisory so operators see the migration state without needing `--storage`.
- **`DC-03 Docs Directory`** — `warn` when a file in `docs/` matches a platform filename pattern (`*.jsonl`, `*_state.*`, `*_registry.*`); `ok` otherwise. The `docs/` directory is the designated home for one-time human documents (decision records, analysis memos, run logs).

**`vertex doctor --checkpoints`** emits `Checkpoint Inventory` and, when checkpoints exist, `Checkpoint Coverage`. `Checkpoint Inventory` warns when a program already has confirmed archive history but no checkpoint exists yet, with the concrete remediation that a non-dry-run `vertex confirm` will seed rollback checkpoints automatically. `Checkpoint Coverage` validates the newest checkpoint against the currently present mutable rollback stores (`risk_register.yaml`, `decisions.yaml`, `journal/actions.jsonl`, `chronicle.jsonl`, and `overrides/`) and fails if any live path is missing from that checkpoint, so rollback readiness is surfaced before `vertex rollback` is attempted.

**ConversionFidelity matrix** — one row per function, color-coded; `warn` (amber) when any function scores < 50%:

```
Conversion fidelity (required inputs arriving as automatic facts):
  newsletter  72%   nudge  64%   risk  48% ⚠   action  80%   review  55%
```

Additional advisory blocks, each a single labeled line with `✅`/`⚠` prefix:
- **ETA credibility** — warns when any tracked work item has credibility < 50%, naming the item and score.
- **Recurring gate failures** — surfaces a specific remediation when the same gate fails from the same root cause across recent issues (e.g., "QG-21 oversized 3× from query `acme-fleet-health` — add `| take 50`").
- **Override streaks** — flags a dimension overridden in the same direction ≥3 consecutive issues (candidate for a permanent Judgment).
- **External dependencies** — counts stale vs. fresh `ExternalDependency` entries from connector polling.

### 12.6.3 Source Discovery Review

Autonomous M365 source discovery exposes a dedicated operator loop rather than burying review state inside generic registry output.

**`vertex doctor --operator-gates`** shows one row per actionable discovery/configuration problem and assigns exactly one category:

```
auto-resolvable        wait for next gather or rerun discovery
pm-decision-required   review candidates and accept/reject/reassign
operator-seed-required attach a manual durable ID with seed-id
auth-admin-required    restore WorkIQ/Graph visibility or consent
source-absent          retire or rename the authored source intent
config-mismatch        authored config still declares a PM-retired/suppressed source
```

Each row includes the source label, last attempt time, best candidate confidence, and a copy-paste-ready next command.

**`vertex integration candidates`** is privacy-safe by default:
- Candidate rows show candidate ID, source type, workstream, confidence, and next action.
- Display names and short previews stay hidden unless the operator opts into `--reveal-titles` or `--reveal-preview`.
- `--requires-decision` filters the queue down to pending/ambiguous items only.

**`vertex integration explain-source`** is the single-source-of-truth surface for one source. It renders, in order, the declared intent, current derived state, candidate list, recent attempts, PM/operator decisions, and the next recommended command so the operator does not need to cross-reference multiple CLI outputs.

### 12.6.4 WorkIQ Structured Retrieval

Structured WorkIQ enumeration does not add a new end-user surface. Accepted records enter the existing pending signal-review queue and use the standard gather counts, source-health diagnostics, and privacy-safe source explanations. Malformed, unsafe, or out-of-window records are omitted rather than partially rendered; connector-level failure remains a degraded-source warning, not fabricated empty success.

Rich per-thread evidence remains opt-in and invisible to readers until its source-specific review signal is approved. Published attribution continues to use the standard `Signal sources:` footnote; unapproved or quarantined thread content must never appear in a draft, reviewer pane, source footnote, or generated artifact. Grounding state is provenance, not a confidence synonym: only `human_verified` and `source_verified` count as grounded.

Qualification tooling is operator-only. It may show private subjects and previews only in the local interactive session and writes captures solely to an ignored/restricted directory. Committed documentation, examples, diagnostics, and test fixtures must not reveal mailbox identity, real participants, private subjects, or internal permalinks.

### 12.6.5 REV — Program-Context Intelligence Pipeline

REV adds two operator-facing CLI surfaces:

**`vertex rev run --program <id> --mailbox <upn> [--eml-inbox <dir>] [--ics-inbox <dir>] [--mock-fixture <path>]`** — runs one retrieval cycle and stages verified candidates in the existing `vertex ledger triage` queue. Output: one line per stage (enumerate → hydrate → extract → vault → stage → verify; stop=complete). `--eml-inbox <dir>` processes locally-exported `.eml` files from the given inbox directory (3-dir atomicity: inbox → claimed → processed). `--ics-inbox <dir>` processes exported `.ics` calendar files. `--mock-fixture` exercises the full P1 value chain with a JSON fixture. Neither flag provided exits 2 with the ADR-008 pivot reference (live Graph API permanently blocked by IT). No reader-visible newsletter chrome: staged candidates enter PENDING and follow the standard triage → project → draft pipeline. **EML privacy (W1-4 + W5-3):** the EML hydrator routes all email body text through `normalizer.normalize()` (PII scrub: emails/phones/SSNs/cards replaced with `[…_REDACTED]` tokens) then replaces person display names from From/To/Cc/Bcc headers with stable `PERSON_N` tokens before extraction. Raw names never reach the external model. The `PERSON_N`→original mapping is available in the hydrated content's route metadata for entity binding.

**`vertex doctor --rev-health`** — REV subsystem diagnostic block appended to standard doctor output. Reports:
- Enumeration-completion distribution (complete / truncated_by_budget / provider_limited / failed / unsupported — **categorical**, never a percentage of a theoretical total).
- Run-state distribution across active candidates (stages by count).
- Verification-assertion state distribution per workstream, including `legacy_unverified` count.
- Evidence-vault retention state (unreferenced / pending / accepted).
- Prompt-Shields mode (`local_only_degrade` until Azure Prompt Shields wired).
- Hydration fallback rate (`metadata_only_flagged` or `drop` triggers).
- Pending-queue age p50 and max (seconds).

Exit code follows standard doctor semantics. REV diagnostic failures do not change the newsletter publish gate; they are advisory for operator awareness. Teams source availability may render as an accepted limitation when ADR/policy records it; the UI must say that explicitly instead of implying a green operational connector. Privacy invariant: no raw mailbox addresses, real subjects, or participant identities appear in diagnostic output or committed artifacts.

**Phase 3 multi-surface operator workflow (2026-06-24):**

*Calendar import:* Export `.ics` files from Outlook (calendar → Export) and drop them into the same inbox directory used for `.eml` files. `IcsEnumerator` scans the same inbox, using SEQUENCE-highest VEVENT as the canonical event. Cancelled events (METHOD:CANCEL or STATUS:CANCELLED) are ingested as `metadata_only`. Organizer display names are extracted from CN= only — raw `mailto:` addresses never appear in canonical text or diagnostic output (OA-9 privacy).

*Local file import:* Drop `.docx` or `.pdf` project briefs, meeting notes, or status reports into the inbox. `LocalFileEnumerator` claims them via the same 3-dir atomicity. Files containing VBA macros are quarantined before opening (`macro_denied`). Scanned-image PDFs with no embedded text layer are quarantined (`pdf_no_text`). `doctor --rev-health` reports `macro_denied_count` and `pdf_no_text_count` per cycle.

**`vertex rev rotate-processed --program <id>`** — moves stale files from `processed/` to `processed/archive/` (mtime >90 days or count >500, oldest first). Name collisions receive a timestamp suffix. Fires automatically (best-effort, never breaks the cycle) at the end of each `vertex rev run`; this command is for explicit operator housekeeping.

**`vertex rev export-corpus --program <id> --output <path> [--include-vault]`** — produces a portable backup bundle: `candidates.jsonl`, `triage_decisions.jsonl`, labeled corpus copy, and optional `evidence_vault.jsonl`. Direct identifiers (sender SMTP, message IDs, principal mailbox, tenant ID, triage actor) are hash-redacted; display names preserved. The manifest warns that content fields (subject, payload text, excerpts) may contain incidental PII and are operator-controlled.

### 12.6.6 NCFL Context Apply

`vertex context apply` and `vertex context apply-batch` are operator mutation surfaces, not automated background jobs. Output is compact and state-oriented:

- preview mode shows proposal id, target store/path, current hash, proposed value, conflict status, and whether the proposal is batch-eligible
- apply mode prints each recoverable state transition: `proposed -> write_started -> yaml_written -> changelog_written -> ledger_written -> applied`
- stale-hash, policy-denied, or cross-issue-conflict outcomes must stop before writing and show the next operator action
- crash recovery reports `needs_repair` with the journal path and repair command; it must not appear as success
- batch apply groups by target store and summarizes applied, skipped, conflicted, stale, and needs-repair counts

No context-apply status appears in published newsletters by default. It may surface in doctor/triage as operator debt only.

### 12.7 Adaptive Card Artifacts

When the rendered surface supports it, Vertex writes deterministic Adaptive Card JSON artifacts alongside the primary output:

- `vertex report --dry-run` writes weekly-summary cards under `publications/<edition>/adaptive_cards/`
- `vertex review-full` writes per-section review cards under `publications/<edition>/review/adaptive_cards/`
- `vertex notify`, `vertex nudge`, and `vertex confirm --post-weekly-summary-card` can reuse those JSON payloads for optional Teams-webhook delivery while preserving the local artifact as the preview/debug surface

### 12.8 Hints CLI Output (`vertex hints`)

`vertex hints --edition <e> --issue <n>` renders narrative delta hints for a given issue and enters an interactive accept/reject/modify flow:

**List view:** One hint per line, formatted as:
```
[<n>] <KIND>  <workstream_id>  Stale <staleness_days>d  <severity>
     <suggested_text_preview truncated to 80 chars>
```
Color coding: `metric_stale` → amber `#BC7C00`; `hint_stale` → slate `#605E5C`; `decision_stale` → red `#A4262C`; `workstream_lead_missing` → red `#A4262C`. Severity prefix: `[CRITICAL]` red bold, `[WARN]` amber, `[INFO]` slate.

**Prompt flow:**
```
Accept [a], Reject [r], Modify [m], Skip [s], Quit [q]: 
```
- Accept: appends `{status: accepted}` to `hints.jsonl`; prints `✓ Accepted`
- Reject: prompts for optional reason; appends `{status: rejected, reason: ...}` to `hints.jsonl`
- Modify: opens `$EDITOR` with suggested text pre-filled; saves modified text to `hints.jsonl` with `{status: modified, text: ...}`
- Skip: leaves hint in `pending` state
- Quit: exits immediately, pending hints remain

**Terminal color:** applied only when stdout is a TTY; stripped when piped.

### 12.9 Fact-Layer & Steering CLI Output

**`vertex facts export/import/rebuild`** — *(planned richer command-run output; help text remains minimal)* quiet, one-line-per-stage output. `export` prints the destination path and revision count; `import` prints the ingested revision count; `rebuild` prints per-fact-type counts re-persisted from authored state. All three respect `--program`; none open a browser.

**`vertex facts parity-check`** — one header line then one row per tracked family: `✅ <family> (N/N)` or `⚠ <family> (M/N — <delta> gap)`. Exits 0 on clean; exits 1 if any zero-tolerance family has a parity gap (blocking); exits 2 if any pending-zero-tolerance family warns. Final summary: `Parity check: PASS` / `WARN` / `FAIL`.

**`vertex facts dual-read-log`** — one status line per cycle: `[cycle N/N] ✅ PASS` or `[cycle N/N] ❌ FAIL: <family>`. Quarantined families written to `fact_store_quarantine.jsonl` without TTY output. Final line: `Dual-read window: PASS (N cycles)` or `FAIL (<K> of N cycles had gaps)`.

**`vertex facts pin-snapshot`** — single-line output: `✅ Snapshot pinned: pfs_<hex> (<N> revisions as of <timestamp>)`.

**`vertex facts detect-drift`** — quiet on no drift (exit 0); on drift prints one line per drifted revision: `⚠ Drift: <natural_key> — <fact_type> at <recorded_at>`. Final line: `State drift: CLEAN` or `State drift: <N> revision(s) since pin <pfs_<hex>>` (exit 2).

**`vertex connectors poll`** — *(planned richer command-run output; help text remains minimal)* one line per configured connector: `✅ <dep_id> (<connector_type>) — <status>` or `⚠ <dep_id> — skipped: <reason>`. Connector errors never fail the command (exit 0); they are reported as skipped lines. `--dry-run` prints what would be polled without writing `external_dependencies.jsonl`.

**`vertex rollback`** — *(planned richer command-run output; help text remains minimal)* lists available checkpoints newest-first when `--to` is omitted:

```
Checkpoints for <edition>:
  [1] 2026-06-01T09:14Z   (pre-confirm issue 079)
  [2] 2026-05-30T17:02Z   (pre-confirm issue 078)
Pass --to <checkpoint> to restore.
```

With `--to`, it prints each restored store and a final `✓ Restored from <checkpoint>` line. Restore is destructive to current working state; the command echoes the affected files before proceeding.

When no checkpoints exist yet for the resolved program, the command fails loud with exit code 1 and tells the operator to run a non-dry-run `vertex confirm` first so a rollback target exists. `--dry-run` prints the same checkpointed path list without mutating files.

**`vertex rollback --drill`** — sandbox simulation mode. Output:

```
Drill: copying workspace to .rollback_sandbox/<id>_<ts>/ ...
  ✅ <N> stores restored from checkpoint <name>
  ✅ Replayability verified (<M> facts / <K> actions reloaded)
  ✅ Sandbox cleaned up
Proof recorded: s7a_rollback_drill (proof_id: pf_<hex>)
Drill complete — rollback is viable for <edition>.
```

Exits 0 on success; exits 1 if any replay step fails. `--archetype <name>` tags the proof record; `--notes <text>` appends operator commentary. Does not write to live stores.

**`vertex doctor --source-waivers`** — one section per program with waivers:

```
[<program_id>] source_waivers.yaml — <N> waivers
  ✅ <source_id>  expires: <date>  valid
  ❌ <source_id>  expires: <date>  EXPIRED
  ❌ <source_id>  reason: missing required field 'rationale'
Tip: Re-run `vertex doctor --source-waivers` after editing programs/<id>/source_waivers.yaml …
```

Exits 0 if all waivers are valid and not expired. Exits 1 on any schema violation or expired waiver.

**Pre-draft steering prompt (`--steering`).** `vertex propose` accepts an optional `--steering "<one-line intent>"` (and an interactive prompt when the flag is absent and the session is a TTY). (Implemented on `propose`; not yet wired into `report` — FR-SG-47 named both surfaces.)

```
Steering (optional, ≤1 line — e.g. "emphasize XKulfi as the launch blocker"): 
```

The steering line is recorded as a `pm_steering` `ProgramEvent` for provenance and constrains AI synthesis (FR-SG-24) as a system-prompt anchor. It can re-weight emphasis but can never fabricate facts; an empty line is accepted and recorded as no steering.

### 12.10 Ledger & Discovery CLI Surfaces

All `vertex ledger` and `vertex discover` output follows the standard §12.1 philosophy: one line per stage during pipeline execution, quiet by default, semantic exit codes (§12.3). No interactive prompts except `ledger triage` (a/e/r/s per candidate).

**`vertex ledger status --program <prog>`** — operator dashboard:
```
Ledger: 1,247 events (847 source_authoritative, 312 ai_extracted, 88 operator_confirmed)
  Active candidates: 12 pending (oldest: 8d)  |  3 lock conflicts
  Triage queue: 5 acknowledged gaps  |  2 unacknowledged gaps (⚠ oldest 18d)
  Last verify: PASSED  (chain: OK, content: OK, redactions: 2)  2026-06-11 14:32 UTC
  Batch progress: 0 active  |  58 LT decks queued (Tier A)
```

**`vertex ledger triage list --program <prog>`** — candidate queue:
```
[1/12] PENDING  candidate:01JWCM3DQPVZ  type=milestone.date_revised.v1  entity=WI:98421
  from: lt_deck_extractor  source: LT-Deck-2025-W23.pptx
  payload: new_target_date=2025-12-31  confidence=ai_extracted
  (a)ccept  (e)dit  (r)eject  (s)kip  (q)uit
```

**EXPLAIN-min (REV candidates, added 2026-07-07 — activation §6.14.19):** when a candidate carries `extraction_rationale` (a 1-sentence quote/summary of the source text that produced it — every REV-extracted `CandidateEvent` does), the queue line shows a `why:` sub-line so the operator can verify the claim in seconds without opening the source EML:
```
- sha256:6477a618…  milestone.completed.v1  batch=rev:20260628081716  extraction_confidence=0.800
    why: rollout Completed
```
This is the minimum-viable trust surface for the "judgment, not discovery" thesis — full drill-down (source excerpt + counter-source context for `disputed` facts) remains a Vision-bar item (`GAP-36`/`GAP-37`, tracked in `specs/backlog.md`'s non-goals).

**Degraded-to-legacy banner (milestone render path, added 2026-07-07):** if the milestone section's `ProgramReality` read path fails while the family's source-of-record mode is non-legacy, it does **not** silently fall back — the operator must explicitly set `VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK=1`, and doing so renders a visible degraded banner on the affected section (and blocks the AG-1 activation-sentence gate for that render). This is the "no silent fallback" rule: a migrated render surface either reads real data or visibly says it didn't.

**`vertex ledger verify --program <prog>`** — chain integrity:
```
Verifying 1,247 events across 3 JSONL files...
  Chain: OK  (all prev_event_hash links valid)
  Content hashes: OK  (all 1,245 non-redacted events match)
  Redactions: 2 registered, 2 verified  (original_hash preserved)
  Result: PASSED  (exit 0)
```

**`vertex ledger backfill --program <prog> --source <path> --dry-run`** — batch staging preview:
```
Scanning /path/to/lt-decks/...  found 58 PPTX files (2020-01-15 → 2025-06-04)
  Dry run: 312 candidate events would be staged (est. entity_resolution_rate: 94%)
  QG-DM-9 check: entity_resolution ≥ 90% ✓  |  lock_conflicts: 0 ✓
  Run without --dry-run to stage batch 'batch:01JW...'
```

**`vertex discover candidates --program <prog>`** — discovery pipeline:
```
[workiq] Scanning WorkIQ corpus... 6 candidates staged
[teams]  Teams connector: 0 candidates (series_id unbound — see QG-SG-01)
[ai/lt_deck] Processing 3 unprocessed LT decks... 18 candidates staged
Discovery run complete: 24 candidates staged  |  batch: batch:01JW...
Run: vertex ledger triage list --program <prog>
```

**`vertex ledger redact --program <prog> --event-id <id> --reason "<text>" --actor <alias>`**:
```
Redacted event 01JWCM3DQPVZ; original_hash=sha256:abc123...def456.
```

### 12.11 People Registry CLI (implemented, `.archive/specs/people.md`, Accepted, complete as of 2026-07-21)

The `vertex kb registry ...` and `vertex kb people ...` command family lives under the existing `vertex kb` surface (never `vertex knowledge`, which stays the claim/vault plane) — full taxonomy in `.archive/specs/people.md` §8.1. It now spans registry bootstrap/config, denormalized read queries (`find`, `overlaps`, `programs`, `search`, `stale`, `conflicts`), governed corrections (`merge`, `split`, `bind`), field-level pin/unpin/attest governance, explicit delegation lifecycle (`delegate create/revoke/list`), provider refresh, and steward-authorized lifecycle-status transitions (`lifecycle-set`). The binding UX contracts hold across all of it: mutation commands preview by default (`--apply` is explicit and, once applied, the command reports the resulting change-journal transaction/generation IDs); human output answers the question directly with relationship type/program/workstream/freshness/source; JSON output uses a versioned envelope with stable IDs and bounded pagination (`--limit`/`--cursor`) where the result set can be large; default output shows stable ID/alias and counts only — display name/email require an explicit `--reveal-pii` that is itself audited. Phase 0c's original read-only slice (`vertex kb people overlaps`/`vertex kb people programs`) still renders its visible `WARNING: alias-based legacy result; identity not verified` caveat and exposes aliases/source paths only when no verified identity binding exists for the entity in question.

**`vertex kb people lifecycle-set --person <ref> --status <active|inactive|departed|unknown> --reason "<text>"`** (preview; no steward credentials required, nothing written):
```
Preview: would apply lifecycle transition for person:alice: active -> departed.
Re-run with --apply to commit the canonical staged registry transaction.
```

**`vertex kb people lifecycle-set --person <ref> --status departed --reason "<text>" --apply --format json`** (applied, as an authenticated directory steward):
```json
{
  "entity_id": "person:alice",
  "from_status": "active",
  "generation_id": "...",
  "to_status": "departed",
  "transaction_id": "..."
}
```

Full taxonomy, per-command flag reference, and rationale for the remaining `vertex kb registry`/`vertex kb people` surface (merge/split/bind, delegate, refresh, doctor-facing checks) is `tests/contracts/cli_reference_snapshot.md`, generated from the live CLI rather than hand-maintained here — this section captures the binding UX *contracts*, not a duplicate of that generated reference. The CLI surface above is complete through Phase 6; the only remaining item is a real operator-run onboarding pilot with a live DSAR/rollback proof against that pilot's own data (PPL-W6.4) — not a CLI gap, an operator-paced evidence gate. See `governance/runbooks/ppl-w64-onboarding-pilot-runbook.md` for the documented real onboarding sequence and every tooling caveat a synthetic dry run surfaced.

---

## §13 EML File Output

### 13.1 Format

RFC 2822 `.eml` file produced by `src/core/eml_writer.py`.

### 13.2 Draft Marking

`X-Unsent: 1` header causes Outlook to open the file as an unsent draft. The author previews, edits recipients if needed, and sends manually.

### 13.3 Content

Multipart MIME: HTML body (the full newsletter) + plain text body (Teams Markdown). Subject line from `_build_email_subject()`.

---

## §14 Outlook HTML Constraints

These constraints apply to ALL HTML email output (detailed, continuity, narrative, lookback archetypes).

| Constraint | Value |
|-----------|-------|
| Max outer width | **680px** |
| Max content width | **640px** |
| CSS | Inline `style=""` only — no `<style>` blocks |
| Layout | `<table role="presentation">` only, `cellpadding="0" cellspacing="0"` — no `<div>` layout |
| Colors | Hex only — no `rgb()`, `hsl()`, CSS variables |
| Font | `Segoe UI, -apple-system, Roboto, Helvetica, Arial, sans-serif` |
| Images | CID-attached or base64 ≤100KB; prefer Unicode emoji |
| JavaScript | None |
| Media queries | Not supported (Outlook ignores) |
| Jump links | Work in browser/OWA only; fail in Outlook reading pane |
| Dark mode | No generated dark-mode CSS; design for resilience (risk chips include text + color) |
| `<div>` elements | Unpredictable in Outlook — use `<table>` for all layout |
| `role` attribute | `role="presentation"` on all layout tables for accessibility |

---

### 14.2 Chart Image Rendering Rules

Charts in email HTML are rendered as PNG images within a constrained frame:

| Property | Value |
|----------|-------|
| Max width | `608px` (matches content area) |
| Border | `1px solid #E5E7EB` |
| Alt text | `{title} chart` (required; must be insight-bearing when `message` is set) |
| Provenance footer | Query ID + source label + capture timestamp at 11px secondary text |
| Degraded banner | Shown when `is_degraded=True` and `cache_captured_at` is set: `⚠️ Using cached data from {date}` |
| Placeholder (zero rows) | 608×200px card, `#F3F4F6` background, `#6B7280` text, centered message |

No MSO conditional branches for charts. No client-side script. No dark-mode CSS overrides.


## §15 Accessibility

### 15.1 Contrast Ratios (WCAG 2.1 AA)

| Combination | Ratio | Compliance |
|-------------|-------|-----------|
| `#111827` on `#FFFFFF` | 16.6:1 | AAA |
| `#374151` on `#FFFFFF` | 10.6:1 | AAA |
| `#991B1B` on `#FEE2E2` | 7.8:1 | AAA |
| `#92400E` on `#FEF3C7` | 6.2:1 | AA |
| `#065F46` on `#D1FAE5` | 5.4:1 | AA |
| `#1E40AF` on `#DBEAFE` | 5.1:1 | AA |
| `#4B5563` on `#F3F4F6` | 5.9:1 | AA |
| `#6B7280` on `#FFFFFF` | 4.6:1 | AA |
| `#9CA3AF` on `#FFFFFF` | 3.0:1 | **AA-Large only** ⚠️ |

The `#9CA3AF` faint color is used only for provenance/timestamps at ≥11px (meets AA-Large 3:1 threshold).

**Cockpit HTML target (reconciled from `.archive/specs/arch-data-fix.md` §10.3):** the standalone `vertex cockpit` HTML output (§18.5) targets **WCAG 2.2 AA**, an intentional upgrade over this section's platform-wide 2.1 AA baseline — cockpit is a newer, self-contained surface (no external JS/CSS, skip-link + semantic landmarks already implemented) where the tighter bar was adopted from the start rather than retrofitted. No independent automated WCAG audit has been run yet (no accessibility-testing tool is wired into CI); this is tracked in `specs/backlog.md`, not a blocker for cockpit's current advisory-mode use.

### 15.2 Screen Reader Support

- `<th scope="col">` on all table header cells
- `aria-label` on risk chips: `aria-label="{risk_level} risk"`
- `aria-label` on nav jump links
- `role="banner"` on health banner
- `role="presentation"` on all layout tables

### 15.3 Dark Mode Resilience

No generated dark-mode CSS (Outlook Desktop ignores it). Design relies on:
- Risk chips include both color AND text label
- Background is never the sole information carrier
- All risk states have emoji icon fallbacks

---

### 15.3 Chart Accessibility

| Requirement | Rule |
|-------------|------|
| Alt text | Every `<img>` chart must have a non-empty `alt` attribute using the pattern `{title} chart` |
| Insight-bearing alt | When `KustoSectionData.message` is set, alt text must incorporate it |
| Color independence | Chart renderers must use shape/pattern differentiation in addition to color for data series |
| Contrast | Chart foreground elements must meet AA contrast (4.5:1) against the chart background |
| Provenance visible | Source and capture time must be readable at normal zoom for editorial review |


## §16 Mobile Responsiveness

### 16.1 Breakpoint

≤480px triggers mobile adaptations. Note: Outlook ignores media queries, so mobile optimization is best-effort for web/OWA clients.

### 16.2 Health Banner

Title + metadata stack vertically. Read time / edition type on separate line.

### 16.3 Scorecard

**Hidden columns:** Trend (merged into Risk cell as 2nd line), Items count, ADO link column.
**Shown only:** Dimension name + Risk level + Delta.
**Dimension name truncation:** Ellipsis at 18 characters.
**Per-row ADO links replaced by section-level link.**

### 16.4 Workstream Tables → Card Stack

- Cards: `1px border, 8px padding, 8px gap`
- Each card: title (bold) → owner + risk icon + ETA on one line → ADO link

### 16.5 Navigation Bar

Horizontal scroll with `-webkit-overflow-scrolling: touch`.

---

## §17 Rendering Test Checklist

### 17.0 Chart Pipeline Checklist

| Test | Spec ref |
|------|---------|
| Chart PNG renders at 608px max-width with correct border | §14.2 |
| Provenance footer shows query ID, source, and capture time | §14.2 |
| Degraded banner appears when `is_degraded=True` | §14.2 |
| Placeholder card renders for zero-row result | §14.2 |
| Teams chart uses trend-summary text format `{title}: {msg}. [View →]` | §10.3 |
| Teams chart without message uses `{title}. [View →]` | §10.3 |
| Charts suppressed in condensed daily edition | §5 |
| Alt text uses `{title} chart` pattern | §15.3 |

### 17.1 Email Client Tests

| # | Test | Client | Pass Criteria |
|---|------|--------|--------------|
| C-1 | Health Banner renders | Outlook Desktop (Win) | Risk color visible, text readable, no overflow |
| C-2 | Scorecard table aligns | Outlook Desktop (Win) | All columns visible, no dimension name wrapping |
| C-3 | Delta badges render | Outlook Desktop (Win) | Colored text ▲ ▼ ● visible, no missing chars |
| C-4 | Risk chips render | Outlook Desktop (Win) | BG color + text + icon all visible |
| C-5 | ADO links work | Outlook Desktop (Win) | Click opens correct ADO URL |
| C-6 | Jump links in nav | Outlook on Web | Clicking "Health" scrolls to banner |
| C-7 | Nav bar visual TOC | Outlook reading pane | Labels visible despite dead links |
| C-8 | Full newsletter ≤680px | All clients | No horizontal scrollbar |
| C-9 | Emoji renders | Outlook Mobile (iOS) | 🔴🟡🟢✅⚪ all display |
| C-10 | Provenance footer | All clients | Manifest ID monospace, centered |

### 17.2 Content Quality Tests

| # | Test | Tool | Pass Criteria |
|---|------|------|--------------|
| C-11 | Exec summary ≤150 words | `verbosity_enforcer.py` | Word count ≤150 |
| C-12 | Blurbs ≤90 words | `verbosity_enforcer.py` | Each ≤90 words, ≤4 sentences |
| C-13 | No banned phrases | `ban_list_validator.py` | Zero matches vs editorial_rules.yaml |
| C-14 | Every claim has ADO citation | `attribution_engine.py` | No Tier-1 claim without 🔗 |
| C-15 | No ❓ in confirmed | `confirm` command | Exit code 3 if any ❓ |
| C-16 | Delta dates human-readable | Visual inspection | "vs May 5" never "vs Issue 76" |
| C-17 | All ADO links resolve | `curl --head` | HTTP 200 or 302 |

### 17.3 Accessibility Tests

| # | Test | Tool | Pass Criteria |
|---|------|------|--------------|
| C-18 | Body text contrast ≥4.5:1 | Manual | All body text passes AA |
| C-19 | Table headers use `<th>` | HTML inspection | `scope="col"` on every `<th>` |
| C-20 | Risk chips have aria-label | HTML inspection | Present on every risk chip |
| C-21 | Mobile readability | 375px viewport | Key content readable without zoom |
| C-22 | Card stack layout | 375px viewport | Workstream items stack to cards |

### 17.4 Golden File Tests

| # | Test | Tool | Pass Criteria |
|---|------|------|--------------|
| C-23 | Deterministic output | `tests/golden/` | Byte-identical on re-render with same input |
| C-24 | Snapshot round-trip | Serialize → deserialize | Identity preserved |
| C-25 | Override three-way merge | Unit test | Preserved + new + removed counts match |

### 17.5 Archetype Tests

| # | Test | Pass Criteria |
|---|------|--------------|
| C-26 | Condensed daily ≤400px | Email height fits reading pane |
| C-27 | Deck output is valid Markdown | No HTML tags in output |
| C-28 | Continuity bands render | All risk colors visible in tile cells |
| C-29 | Lookback window card | Retrospective window description visible |
| C-30 | Narrative has no scorecards | No scorecard partial in rendered output |

---

## §18 Surface-Specific Layout Rules

These rules close the remaining surface-specific gaps left by the global newsletter constraints above. They are binding for new rendering work and for regression review on existing surfaces.

### 18.1 Nudge EML

- Layout width matches newsletter email rules: `680px` outer container, `640px` content width, table-based layout only.
- Heat indicators use canonical risk colors from `src/core/jinja_filters.py -> RISK_COLORS`; no alternate alert palette is introduced for nudge-only surfaces.
- Header and footer stay structurally simple: one banner row, one body stack, one footer block. No nested navigation, no scorecard grid, no hidden diagnostic chrome.
- Gap-resolution hints may include monospace command text, but they must stay inside the body column and not create horizontal scrolling.
- The comment hygiene signals (`has_recent_comment`, `comment_has_status_keyword`, `is_ready`) are tri-state: `True`/`False` render as a heat indicator; `None` ("not evaluated" — comment-fetch budget overflow or API failure) renders as a neutral "unknown" glyph and is never a hygiene failure. A run that produced any `None` or `query_error` still writes the EML but exits with code 3 (degraded); the EML header surfaces the degraded state.
- The nudge EML is a generated **draft** (`X-Unsent: 1`) written to `programs/{program_id}/nudge/drafts/{run_id}.eml`; Vertex never sends it. A human reviews and forwards it via Outlook, can record any required audience approval with `--approve-draft <draft-ref>`, and then uses `--mark-sent <draft-ref> [--sent-at <iso>]` to attest the send. Historical published EMLs can be backfilled into lifecycle tracking with `--import-sent <published-ref> [--sent-at <iso>]`. Recipient domains, opt-outs, and delivery mode are enforced before the draft is written.
- When configured, the subject may prepend `[Action DUE …]` or `[OVERDUE …]` derived from the resolved action-due date; milestone-linked urgency is sourced from `ProgramReality`, not from hardcoded calendar strings.
- Cooldown does not begin when the draft is generated. It begins only when the operator attests the send via `--mark-sent`, and the publication record stores content-hash + audience metadata alongside the published EML.
- Acceptance mapping: §17 C-1, C-4, C-8, C-10.
Pass condition: the generated nudge EML renders within the standard email width, preserves risk-color meaning, keeps footer/banner content readable in Outlook without overflow, and renders "not evaluated" signals as neutral rather than as failures.

### 18.2 Brief

- Brief sections are plain-text-first artifacts even when previewed in richer shells; no rich HTML formatting is required or assumed for delivery.
- Maximum section budget: `1,200` characters per major section (`Now`, `Next`, `Later`, intervention lists), with intervention lines staying single-paragraph and command-oriented.
- Condensation rule: inline attribution chains are collapsed into short evidence phrases rather than repeated source lists.
- Teams delivery compatibility is mandatory: if a brief is reposted into Teams or chat surfaces, it must remain legible as wrapped plain text with no dependency on HTML tables, color, or hover affordances.
- **Program Narrative section (added `.archive/specs/arch-data-fix.md` v1.77):** a new, always-last section surfaces the latest QG-29-released `ProgramSynthesis` through-line plus its long-pole lines, when one exists for the program — same plain-text-first rendering as every other section, appended after `Now`/`Watch`/`Staged`/`Reference Docs`. Degrades to fully omitted (not an empty header) when no synthesis has been released yet, preserving today's exact output for every program that hasn't wired the synthesis pipeline in.
- Acceptance mapping: §17 C-11, C-12, C-16, C-27.
Pass condition: the brief stays within section budgets, reads cleanly as wrapped plaintext, does not rely on HTML-only formatting for operator comprehension, and the Program Narrative section (when present) reads as one more plain-text section, not a formatting exception.

### 18.3 Fleet

- Fleet surfaces use one row or tile band per program: program name, health color, issue/count summary, and optional freshness badge.
- Per-program health color comes from the canonical shared status palette only; no program-specific branding colors are allowed in the fleet rollup.
- Nested tables are not allowed inside a fleet program row; secondary details must stack vertically within the same tile or row container.
- Cross-program comparison must remain scannable in one viewport pass, so summary density takes precedence over deep per-program prose.
- **Context health columns** (added 2026-05-27): each program row includes a context health summary line: `Context L{level} / {errors} errors / {stale} stale`. `level` maps to a semantic label: L4 = ✅ healthy, L3 = ⚠ warnings, L2 = ⚠ gaps, L1 = ❌ errors, L0 = ❌ critical. The context health line stacks below the lifecycle/phase info within the same program row — no additional column or nested table.
- Acceptance mapping: §17 C-8, C-18, C-21, C-22.
Pass condition: each program renders as a single scannable unit with canonical health color semantics, context health line visible without horizontal scroll, and no nested-table layout complexity.

### 18.4 Adaptive Card

- Adaptive Card payloads are constrained to `<=12` body elements per card to avoid unreadable long-scroll Teams cards.
- Action buttons are capped at `3` per card; overflow actions must collapse into a follow-up command or linked artifact rather than more buttons.
- Risk colors map from Vertex `RISK_COLORS` into the closest supported Adaptive Card emphasis semantics; the payload must not depend on unsupported arbitrary hex styling.
- Card copy should mirror the primary local artifact summary, but the card is a transport preview surface, not the full-fidelity review pane.
- Acceptance mapping: §17 C-9, C-21, C-30.
Pass condition: generated cards stay within body/action limits, preserve risk emphasis without custom HTML/CSS assumptions, and remain readable in Teams-native rendering.

### 18.5 Cockpit (added from `.archive/specs/arch-data-fix.md`, ADF-F01/ADF-W5.5)

- Terminal (`vertex cockpit show`) and standalone HTML (`vertex cockpit show --format html` / `build`) are the two supported renders; the HTML output is one self-contained document (inline `<style>` only, no external JS/CSS, no network requests — safe to open via `file://`).
- **System-health labels are distinct from program-risk colors**: system/source-health state (`[OK]` / `[Degraded]`) renders as text labels or neutral icons, never the canonical risk-color palette (`RISK_COLORS`) — program risk continues to be the only concept color is reserved for, per §1.2's platform-wide "Color = Risk. Always. Only." rule.
- Accessibility: skip-link + semantic landmarks (`<header>`/`<main>`/`<section aria-labelledby>`) for keyboard navigation; targets WCAG 2.2 AA (§15.1's reconciled note) rather than the platform-wide 2.1 AA baseline.
- Every evidence-derived string is escaped; an evidence ref renders as a real clickable link only when it parses as an allowlisted `http`/`https` URL — anything else (`javascript:`, `file:`, a bare id) renders as plain escaped text, never a clickable link.
- `explain <finding_id>` renders every explainability field a `CockpitFinding` actually carries (why, detail, owner, source-age, evidence, next-command) and honestly labels the fields it doesn't yet have data for (calculation/rule, confidence, what-Vertex-did-not-do) rather than fabricating content.
- `compare <earlier> <later>` operates only on retained history snapshots — never a live recompute — so a comparison is always reproducible from the same two points in time.
- Value/time-savings figures always render their confidence tier (`measured`/`calibrated`/`proxy`/`unavailable`) alongside the number — a figure is never presented as a bare percentage without its evidence tier visible.
- No formal §17 acceptance-mapping ID exists for this surface yet; an independent WCAG audit and golden-snapshot goldens are tracked in `specs/backlog.md`, not yet attempted.
Pass condition: the HTML render opens safely from a local file with no external requests, risk color is never reused for system-health state, every finding's explanation is either real data or an honest "not yet available" label, and no evidence-derived string can inject markup.

---

## Appendix A: Jinja2 Partial Registry

| Partial | File | Used By |
|---------|------|---------|
| Navigation Bar | `partials/nav_bar.j2` | detailed, narrative, lookback |
| Health Banner | `partials/health_banner.j2` | detailed, narrative, lookback, teams |
| Top 3 Now | `partials/top_3_now.j2` | detailed, narrative, lookback, teams |
| What Changed | `partials/what_changed.j2` | detailed, narrative, lookback, teams |
| Scorecard | `partials/scorecard.j2` | detailed, lookback, teams |
| Executive Summary | `partials/exec_summary.j2` | detailed, lookback, teams |
| Workstream | `partials/workstream.j2` | detailed, narrative, lookback, teams |
| Risk Chip | `partials/risk_chip.j2` | scorecard, workstream, chapter |
| Delta Badge | `partials/delta_badge.j2` | what_changed, scorecard |
| Verify Chip | `partials/verify_chip.j2` | workstream |
| ADO Vitality | `partials/ado_vitality.j2` | detailed, continuity, teams |
| Provenance Footer | `partials/provenance_footer.j2` | all HTML archetypes |
| Orientation Footer | `partials/orientation_footer.j2` | detailed, lookback |
| Kusto Section | `partials/kusto_section.j2` | detailed, narrative, teams |
| Brand Header | `partials/brand_header.j2` | continuity |
| Cadence Note | `partials/cadence_note.j2` | continuity |
| Edition Intro | `partials/edition_intro.j2` | continuity |
| Jump to Section | `partials/jump_to_section.j2` | continuity |
| Continuity Scorecard Band | `partials/continuity_scorecard_band.j2` | continuity |
| Continuity Chapter | `partials/continuity_chapter.j2` | continuity |
| Continuity Exec Summary | `partials/continuity_exec_summary.j2` | continuity |
| Continuity Provenance | `partials/continuity_provenance_comment.j2` | continuity |
| Vitality Reviewer | `partials/vitality_reviewer.j2` | reviewer pane |
| Anticipated Questions | `partials/reviewer_anticipated_questions.j2` | reviewer pane |

## Appendix B: Jinja2 Filter Registry

| Filter | Returns | Example |
|--------|---------|---------|
| `risk_bg(level)` | Background hex | `"high" → "#FEE2E2"` |
| `risk_fg(level)` | Foreground hex | `"high" → "#991B1B"` |
| `risk_icon(level)` | Emoji | `"high" → "🔴"` |
| `risk_label(level)` | Human label | `"high" → "High"` |
| `risk_short_label(level)` | Short label | `"high" → "H"` |
| `delta_bg(kind)` | Background hex | `"risk_up" → "#FEE2E2"` |
| `delta_fg(kind)` | Foreground hex | `"risk_up" → "#991B1B"` |
| `delta_label(kind, old, new)` | Human text | `"risk_up" → "▲ was High"` |
| `top_item_border(type)` | Border color hex | `"risk" → "#92400E"` |
| `top_item_icon(type)` | Emoji | `"risk" → "🟡"` |
| `build_anchor(text)` | URL-safe anchor slug | `"Deployment Velocity" → "deployment-velocity"` |
| `format_date(value)` | Formatted date | `"May 10, 2026"` |
| `format_datetime(value)` | Formatted datetime | `"May 10 2026, 09:00 UTC"` |
| `evidence_tooltip(packet)` | Tooltip string | `"12 items · 3 High · 2 stale"` |
| `qg_summary(results)` | Gate summary | `"QG: All gates passed"` |
| `scorecard_short_label(name)` | Abbreviated name | `"Deployment Velocity" → "Depl Vel"` |
| `risk_load_bar_width(load, px)` | Bar pixel width | `1.5 → 40` |
| `pluralize(count, s, p)` | Noun form | `(1, "item", "items") → "item"` |
| `ordinal(n)` | Ordinal string | `3 → "3rd"` |

## Appendix C: Canonical Label Registry

Prevents namespace collision when ADO risk labels and Vertex risk labels appear in the same table.

| Namespace | Labels | Visual Treatment |
|-----------|--------|-----------------|
| Program Health | Healthy · At Risk · Critical · On Track · Needs Input | Banner background color |
| Scorecard Risk | Low · Medium · High · Done · ❓ Needs Input | Colored risk chip (pill) |
| ADO State | On Track · At Risk · Off Track · Not Started · Closed | Italic text, no background |
| Freshness Severity | Block · Warn · Info · Bad Fresh | Severity icons (🛑⚠⏰⚡) |
| Review Status | Pending · Sent · Approved · Changes Requested · Rejected | Status emoji (⏳📤✅✏️❌) |

When ADO "At Risk" and Scorecard "High" appear in the same table: ADO state uses italic text (no background), Scorecard risk uses colored chip.

---

*End of UX spec.*

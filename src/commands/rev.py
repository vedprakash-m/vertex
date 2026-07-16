"""``vertex rev`` — Program-Context Intelligence (REV) CLI (Zone D).

specs/program-context-intelligence.md. Wires the Zone-C/B port implementations
into the Zone-A pipeline (``src.core.rev.pipeline.run_rev_cycle``). P1 ships a
``--mock-fixture`` mail walking skeleton (no live consent); the live Graph
mode is **P0 operator-gated** and prints the spike reference until consent +
the real ``RevGraphClient`` adapter are wired.

Commands:
* ``vertex rev run`` — run one REV retrieval cycle for a program/mailbox and
  stage candidates (mock fixture or local-export .eml import).
* ``vertex rev init-inbox`` — scaffold the local-import inbox directory tree +
  write a local README (operator convenience; P1-5).
* ``vertex rev rotate-processed`` — rotate stale/surplus files from ``processed/``
  → ``processed/archive/`` (OA-4 retention housekeeping; P2-14).
* ``vertex rev export-corpus`` — export a PII-scrubbed REV corpus bundle
  (candidates + triage + labeled corpus + optional vault; P2-5).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from src.ai.rev.extractor import DeterministicRevExtractor, LLMRevExtractor, LLMRevExtractorUnavailable
from src.ai.rev.verification import run_layered_verification
from src.core.config_loader import PROGRAMS_ROOT
from src.core.edition_resolver import load_program
from src.core.models_v2 import REV_PROFILE_LEGACY_NL, REV_PROFILE_REV_VERIFIED, REV_PROFILE_SEARCH_HYDRATE, RevRetrievalProfile
from src.core.rev.entity_types import EntityType
from src.core.rev.governor import BudgetLimits
from src.core.rev.pipeline import RevPipelineDeps, run_rev_cycle
from src.core.rev.prompt_shields import LocalOnlyPromptShields, PromptShields
from src.core.rev.query_planner import RetrievalIntent
from src.m365.rev import FakeRevGraphClient, GraphMessage
from src.core.rev.ports import CandidateEnumerator, ContentHydrator
from src.m365.rev.eml_enumerator import EmlEnumerator
from src.m365.rev.eml_hydrator import EmlHydrator
from src.m365.rev.local_file_enumerator import LocalFileEnumerator
from src.m365.rev.local_file_hydrator import LocalFileHydrator


def _resolve_shields() -> PromptShields:
    """Construct the Prompt Shields for a REV cycle (BL-A1).

    When Azure Content Safety is configured (``AZURE_CONTENT_SAFETY_ENDPOINT`` +
    ``AZURE_CONTENT_SAFETY_KEY``), use ``AzurePromptShields`` so cycles can run
    with ``shield_degrade=false`` and count toward the AG-3 authority ladder.
    Otherwise fall back to ``LocalOnlyPromptShields`` (visible degrade — never
    silent). This was hardcoded to local-only, which meant provisioning Azure CS
    had no effect on ``shield_degrade``; that is fixed here.
    """
    try:
        from src.m365.azure_prompt_shields import AzurePromptShields, load_azure_shield_config
        config = load_azure_shield_config()
        if config is not None:
            return AzurePromptShields(config=config)
    except Exception:
        pass
    return LocalOnlyPromptShields()
from src.m365.rev.enumerators import CollectionSearchEnumerator, MailboxContext
from src.m365.rev.hydrator import MailHydrator
from src.m365.rev.ics_enumerator import IcsEnumerator
from src.m365.rev.ics_hydrator import IcsHydrator

app = typer.Typer(add_completion=False, help="Program-Context Intelligence (REV) retrieval + verification.")

_PROFILE_CONSTANTS = {
    "legacy_nl": REV_PROFILE_LEGACY_NL,
    "search_hydrate": REV_PROFILE_SEARCH_HYDRATE,
    "rev_verified": REV_PROFILE_REV_VERIFIED,
}

P0_SPIKE_NOTE = (
    "Live Graph API REV retrieval is permanently unavailable (all delegated Graph scopes "
    "blocked by Microsoft IT policy — see docs/adrs/adr-008-graph-api-pivot.md). "
    "Supply --eml-inbox <dir> to process locally-exported .eml files, "
    "--ics-inbox <dir> to process locally-exported .ics calendar files, "
    "--docs-inbox <dir> to process locally-downloaded .docx/.pdf files, or "
    "--mock-fixture <path> for a JSON fixture walking skeleton."
)

# Local-import inbox README written by ``vertex rev init-inbox`` (P1-5).
# This file lives inside the gitignored program dir; it is operator-facing
# documentation, never committed.
_INBOX_README = """\
# REV local-import inbox — {program_id}

This directory is the **local-export import** inbox for the Vertex REV
(Program-Context Intelligence) pipeline. Microsoft Graph API delegated scopes
are permanently blocked by IT policy (ADR-008); the production ingestion path
is local desktop export — no API, no credentials, no expiry.

## Layout (3-directory atomicity)

    rev_inbox/            <- you drop .eml/.ics/.docx/.pdf files HERE (this directory)
      claimed/           <- in-flight (do not touch; replayed after a crash)
      processed/         <- completed (purged after 90 days — OA-4)
      quarantine/        <- failed parses / oversized / crash-loop poison files
      _crash_loop_counts.json   <- internal crash-loop counter (do not edit)

## How to use

1. Export items into this directory: Outlook messages via File → Save As →
   `.eml` (or drag messages out of Outlook), calendar occurrences as `.ics`,
   or locally-downloaded `.docx`/`.pdf` documents.
2. Run a cycle:

       vertex rev run --program {program_id} --mailbox <your-upn> {run_flag} "{inbox_abs}"

   Files are claimed (inbox -> claimed), hydrated, extracted, vaulted, and
   staged as PENDING candidates for `vertex ledger triage list`.

3. Re-running is safe and idempotent: processed files are not re-ingested;
   `claimed/` files left by a prior crash are replayed first.

## Privacy (OA-4 — required before first real intake)

- Restrict this directory's ACL to your own user account only.
- Raw file content is never written to logs or support bundles.
- `processed/` files are purged after 90 days (or once evidence excerpts are
  vaulted, whichever is later).
- Attachments of type `application/*` (Winmail.dat, PDFs, etc.) are denied and
  logged to `attachment_denied.jsonl` — only message text is extracted.

## Crash-loop guard

A file that fails on 3 consecutive startup recoveries (it survives 3 cycle
boundaries in `claimed/`) is presumed poison and moved to `quarantine/` with
`reason=crash_loop`. Inspect `quarantine/*.reason.txt` for details.
"""


def _default_inbox_root(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / "rev_inbox"


def _verifier(**kwargs: Any) -> str:
    return run_layered_verification(**kwargs).effective_state


def _load_mock_fixture(path: Path) -> tuple[GraphMessage, ...]:
    """Load a JSON list of message objects → GraphMessage tuple."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise typer.BadParameter(f"Mock fixture {path} must be a JSON list of message objects.")
    messages: list[GraphMessage] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        messages.append(
            GraphMessage(
                message_id=str(item["message_id"]),
                subject=str(item.get("subject", "")),
                sender=str(item.get("sender", "")),
                received_at=str(item.get("received_at", "")),
                unique_body=str(item.get("unique_body", "")),
                body=str(item.get("body", "")),
                body_content_type=str(item.get("body_content_type", "text")),
                unique_body_content_type=str(item.get("unique_body_content_type", "text")),
                conversation_id=str(item.get("conversation_id", "")),
                has_attachments=bool(item.get("has_attachments", False)),
                web_url=str(item.get("web_url", "")),
                etag=str(item.get("etag", "")),
                immutable_id=str(item.get("immutable_id", "")),
            )
        )
    return tuple(messages)


def _resolve_profile(program_id: str, override: str | None, *, programs_root: Path) -> RevRetrievalProfile:
    if override:
        const = _PROFILE_CONSTANTS.get(override)
        if const is None:
            raise typer.BadParameter(f"Unknown --profile '{override}'. One of: {sorted(_PROFILE_CONSTANTS)}")
        return RevRetrievalProfile(profile=const)
    program = load_program(program_id, programs_root=programs_root)
    if program is not None and program.m365 is not None and program.m365.rev is not None:
        return program.m365.rev
    # Default to the safest non-gated profile when the program has no REV config.
    return RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE)


@app.command("run")
def rev_run(
    program: str = typer.Option(..., "--program", "-p", help="Program ID."),
    mailbox: str = typer.Option(..., "--mailbox", help="Principal mailbox (UPN) to retrieve from."),
    tenant_id: str = typer.Option("", "--tenant-id", help="Tenant ID for the mailbox (defaults to 'default')."),
    container: str = typer.Option("inbox", "--container", help="Logical container label."),
    subject: list[str] = typer.Option([], "--subject", help="Subject search term (repeatable)."),
    body: list[str] = typer.Option([], "--body", help="Body/free-text search term (repeatable)."),
    senders: list[str] = typer.Option([], "--sender", help="Sender filter (repeatable)."),
    limit: int = typer.Option(25, "--limit", help="Max candidates to enumerate."),
    profile: str | None = typer.Option(None, "--profile", help="Override REV profile: legacy_nl | search_hydrate | rev_verified."),
    mock_fixture: Path | None = typer.Option(None, "--mock-fixture", help="JSON fixture of messages for the P1 walking skeleton (no live consent)."),
    eml_inbox: Path | None = typer.Option(None, "--eml-inbox", help="Directory containing locally-exported .eml files (inbox/ dir for EmlEnumerator)."),
    ics_inbox: Path | None = typer.Option(None, "--ics-inbox", help="Directory containing locally-exported .ics calendar files (inbox/ dir for IcsEnumerator). W6-1."),
    docs_inbox: Path | None = typer.Option(None, "--docs-inbox", help="Directory containing locally-downloaded .docx/.pdf files (inbox/ dir for LocalFileEnumerator). P3-5."),
    extractor: str = typer.Option("deterministic", "--extractor", help="Extractor tier: deterministic | llm. 'llm' requires VERTEX_AI_DEPLOYMENT."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    """Run one REV retrieval cycle and stage candidates for triage."""
    if mock_fixture is None and eml_inbox is None and ics_inbox is None and docs_inbox is None:
        typer.echo(P0_SPIKE_NOTE)
        raise typer.Exit(code=2)

    retrieval_profile = _resolve_profile(program, profile, programs_root=programs_root)

    # Sidecar paths for the local-import path (P1-9): dropped-claim grounding
    # misses + denied attachments, both under the inbox root with bounded rotation.
    grounding_missed_path: Path | None = None
    attachment_denied_path: Path | None = None
    if eml_inbox is not None:
        grounding_missed_path = eml_inbox / "grounding_missed.jsonl"
        attachment_denied_path = eml_inbox / "attachment_denied.jsonl"

    from src.ai.rev.extractor import RevExtractor as _RevExtractorProtocol
    _extractor: _RevExtractorProtocol
    if extractor == "llm":
        try:
            _extractor = LLMRevExtractor.from_env(
                grounding_missed_path=grounding_missed_path,
                program_id=program,
                programs_root=programs_root,
            )
        except LLMRevExtractorUnavailable as exc:
            typer.echo(f"Error: --extractor llm requires VERTEX_AI_DEPLOYMENT to be set. {exc}", err=True)
            raise typer.Exit(code=2)
    elif extractor == "deterministic":
        _extractor = DeterministicRevExtractor()
    else:
        typer.echo(f"Error: unknown --extractor '{extractor}'. Choose: deterministic | llm", err=True)
        raise typer.Exit(code=2)

    intent = RetrievalIntent(
        entity_type=EntityType.MESSAGE,
        subject_terms=tuple(subject),
        body_terms=tuple(body),
        senders=tuple(senders),
        limit=limit,
    )

    if eml_inbox is not None:
        _enumerator: CandidateEnumerator = EmlEnumerator(
            inbox_root=eml_inbox,
            mailbox_tenant_id=tenant_id or "default",
            principal_mailbox=mailbox,
            container=container,
            limit=limit,
        )
        _hydrator: ContentHydrator = EmlHydrator(
            mailbox_tenant_id=tenant_id or "default",
            principal_mailbox=mailbox,
            container=container,
            attachment_denied_path=attachment_denied_path,
        )
        deps = RevPipelineDeps(
            enumerator=_enumerator,
            hydrator=_hydrator,
            shields=_resolve_shields(),
            extractor=_extractor,
            verifier=_verifier,
        )
        mailbox_ctx = MailboxContext(tenant_id=tenant_id or "default", principal_mailbox=mailbox, container=container)
    elif ics_inbox is not None:
        _enumerator = IcsEnumerator(
            inbox_root=ics_inbox,
            mailbox_tenant_id=tenant_id or "default",
            principal_mailbox=mailbox,
            container=container,
            limit=limit,
        )
        _hydrator = IcsHydrator(
            mailbox_tenant_id=tenant_id or "default",
            principal_mailbox=mailbox,
            container=container,
        )
        deps = RevPipelineDeps(
            enumerator=_enumerator,
            hydrator=_hydrator,
            shields=_resolve_shields(),
            extractor=_extractor,
            verifier=_verifier,
        )
        mailbox_ctx = MailboxContext(tenant_id=tenant_id or "default", principal_mailbox=mailbox, container=container)
    elif docs_inbox is not None:
        _enumerator = LocalFileEnumerator(
            inbox_root=docs_inbox,
            mailbox_tenant_id=tenant_id or "default",
            principal_mailbox=mailbox,
            container=container,
            limit=limit,
        )
        _hydrator = LocalFileHydrator(
            mailbox_tenant_id=tenant_id or "default",
            principal_mailbox=mailbox,
            container=container,
        )
        deps = RevPipelineDeps(
            enumerator=_enumerator,
            hydrator=_hydrator,
            shields=_resolve_shields(),
            extractor=_extractor,
            verifier=_verifier,
        )
        mailbox_ctx = MailboxContext(tenant_id=tenant_id or "default", principal_mailbox=mailbox, container=container)
    else:
        assert mock_fixture is not None
        messages = _load_mock_fixture(mock_fixture)
        graph = FakeRevGraphClient(messages)
        mailbox_ctx = MailboxContext(tenant_id=tenant_id or "default", principal_mailbox=mailbox, container=container)
        deps = RevPipelineDeps(
            enumerator=CollectionSearchEnumerator(graph, mailbox_ctx),
            hydrator=MailHydrator(graph, mailbox_ctx),
            shields=_resolve_shields(),
            extractor=_extractor,
            verifier=_verifier,
        )
    correlation_id = f"cli:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    report = run_rev_cycle(
        program_id=program,
        intent=intent,
        deps=deps,
        profile=retrieval_profile,
        mailbox_tenant_id=mailbox_ctx.tenant_id,
        mailbox_principal=mailbox_ctx.principal_mailbox,
        mailbox_container=mailbox_ctx.container,
        correlation_id=correlation_id,
        programs_root=programs_root,
        budget_limits=BudgetLimits.from_rev_budgets(getattr(retrieval_profile, "budgets", None))
        if getattr(retrieval_profile, "budgets", None) is not None else BudgetLimits(),
    )
    typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    typer.echo(
        f"REV cycle {report.correlation_id}: staged={report.candidates_staged} "
        f"hydrated={report.hydrated} metadata_only={report.metadata_only} "
        f"quarantined={report.quarantined} stop={report.stop_category}"
        + (" (shield degrade: Prompt Shields unavailable — see logs)" if report.shield_degrade else ""),
        err=True,
    )
    raise typer.Exit(code=0 if report.stop_category == "complete" else 1)


@app.command("init-inbox")
def rev_init_inbox(
    program: str = typer.Option(..., "--program", "-p", help="Program ID."),
    eml_inbox: Path | None = typer.Option(
        None, "--eml-inbox",
        help="Inbox root (defaults to programs/<program>/rev_inbox). Must be on a LOCAL filesystem.",
    ),
    ics_inbox: Path | None = typer.Option(
        None, "--ics-inbox",
        help="Inbox root for locally-exported .ics calendar files (defaults to programs/<program>/rev_inbox). Must be on a LOCAL filesystem.",
    ),
    docs_inbox: Path | None = typer.Option(
        None, "--docs-inbox",
        help="Inbox root for locally-downloaded .docx/.pdf files (defaults to programs/<program>/rev_inbox). Must be on a LOCAL filesystem.",
    ),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    """Scaffold the local-import inbox directory tree + write a local README (P1-5).

    Creates ``rev_inbox/`` with the ``claimed/`` / ``processed/`` /
    ``quarantine/`` subdirectories used by the 3-directory atomicity model, plus
    the program ``_rev/`` checkpoint dir, and writes an operator-facing
    ``README.md`` documenting the export-import workflow + OA-4 privacy policy.
    Idempotent: re-running only refreshes the README.

    The 3-directory atomicity mechanics are identical across the three
    local-import enumerators (eml/ics/docs), so ``--eml-inbox``/``--ics-inbox``/
    ``--docs-inbox`` are interchangeable overrides of the same inbox root; all
    three fall back to the same program default when none is given.
    """
    explicit_inbox = eml_inbox or ics_inbox or docs_inbox
    inbox_root = explicit_inbox if explicit_inbox is not None else _default_inbox_root(program, programs_root)
    run_flag = _resolve_inbox_run_flag(eml_inbox, ics_inbox, docs_inbox)
    # The inbox MUST be on a local filesystem (network drives break atomic rename).
    if explicit_inbox is not None:
        try:
            import os as _os
            drive, _ = _os.path.splitdrive(str(inbox_root.resolve()))
            # Heuristic: UNC paths (\\server\share) and mapped-drive roots that
            # resolve off-box are the known-bad case. We cannot reliably detect
            # mapped drives from Python, so we warn rather than block.
            if str(inbox_root.resolve()).startswith("\\\\"):
                typer.echo(
                    f"WARN: {run_flag} resolves to a UNC network path. Atomic rename "
                    "(inbox -> claimed) will fail with WinError 17; use a LOCAL path. "
                    "See specs/gaps.md REV-G2 (network drive).",
                    err=True,
                )
        except OSError:
            pass

    created: list[str] = []
    for sub in ("claimed", "processed", "quarantine"):
        sub_path = inbox_root / sub
        sub_path.mkdir(parents=True, exist_ok=True)
        created.append(str(sub_path))
    # Program checkpoint dir (last_cycle.json / cycle_history.jsonl live here).
    rev_dir = programs_root / program / "_rev"
    rev_dir.mkdir(parents=True, exist_ok=True)
    created.append(str(rev_dir))

    readme_path = inbox_root / "README.md"
    readme_path.write_text(
        _INBOX_README.format(
            program_id=program,
            inbox_abs=str(inbox_root.resolve()),
            run_flag=run_flag,
        ),
        encoding="utf-8",
    )
    typer.echo(f"REV inbox scaffolded for program '{program}' at {inbox_root}")
    typer.echo(f"  README: {readme_path}")
    typer.echo("Drop your locally-exported .eml/.ics/.docx/.pdf files directly in the inbox root, then run:")
    typer.echo(
        f"  vertex rev run --program {program} --mailbox <upn> {run_flag} \"{inbox_root}\""
    )
    typer.echo(
        "Reminder (OA-4): restrict this directory's ACL to your own account before "
        "dropping real email. See README.md.",
        err=True,
    )
    raise typer.Exit(code=0)


def _resolve_inbox_run_flag(
    eml_inbox: Path | None, ics_inbox: Path | None, docs_inbox: Path | None,
) -> str:
    """Picks the ``vertex rev run`` inbox flag matching whichever explicit
    override (if any) was supplied, mirroring ``rev_run``'s own
    eml -> ics -> docs precedence. Defaults to ``--eml-inbox`` when none was
    given (matching legacy default-path behavior)."""
    if eml_inbox is not None:
        return "--eml-inbox"
    if ics_inbox is not None:
        return "--ics-inbox"
    if docs_inbox is not None:
        return "--docs-inbox"
    return "--eml-inbox"


@app.command("rotate-processed")
def rev_rotate_processed(
    program: str = typer.Option(..., "--program", help="Program id."),
    eml_inbox: Path | None = typer.Option(
        None, "--eml-inbox",
        help="Local-import inbox root (defaults to programs/<program>/rev_inbox).",
    ),
    ics_inbox: Path | None = typer.Option(
        None, "--ics-inbox",
        help="Local-import inbox root for .ics calendar imports (defaults to programs/<program>/rev_inbox).",
    ),
    docs_inbox: Path | None = typer.Option(
        None, "--docs-inbox",
        help="Local-import inbox root for .docx/.pdf imports (defaults to programs/<program>/rev_inbox).",
    ),
    max_age_days: int = typer.Option(
        90, "--max-age-days", help="Rotate files older than this many days.",
    ),
    max_count: int = typer.Option(
        500, "--max-count", help="Rotate oldest surplus files beyond this count.",
    ),
    programs_root: Path = typer.Option(
        PROGRAMS_ROOT, "--programs-root", help="Programs root directory.",
    ),
) -> None:
    """Rotate stale/surplus files from ``processed/`` → ``processed/archive/`` (P2-14).

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
    """
    explicit_inbox = eml_inbox or ics_inbox or docs_inbox
    inbox_root = explicit_inbox if explicit_inbox is not None else _default_inbox_root(program, programs_root)
    processed_dir = inbox_root / "processed"
    from src.core.rev.inbox_rotation import rotate_processed_dir

    moved = rotate_processed_dir(processed_dir, max_age_days=max_age_days, max_count=max_count)
    typer.echo(f"Rotated {moved} file(s) from {processed_dir} to {processed_dir / 'archive'}")
    raise typer.Exit(code=0)


@app.command("export-corpus")
def rev_export_corpus(
    program: str = typer.Option(..., "--program", help="Program id."),
    output: Path = typer.Option(..., "--output", help="Output directory for the PII-scrubbed bundle."),
    include_vault: bool = typer.Option(
        False, "--include-vault/--no-include-vault",
        help="Also export vaulted evidence excerpts (raw text — may contain incidental PII).",
    ),
    programs_root: Path = typer.Option(
        PROGRAMS_ROOT, "--programs-root", help="Programs root directory.",
    ),
) -> None:
    """Export a PII-scrubbed REV corpus bundle (P2-5).

    Writes ``candidates.jsonl`` + ``triage_decisions.jsonl`` + the labeled corpus
    copy (if present) + optionally ``evidence_vault.jsonl`` + a ``manifest.json``
    to ``--output``. Direct identifiers (sender SMTP, message-id, mailbox
    principal, triage actor) are hash-redacted; content hashes are kept for
    restore/dedup. Content fields (subject, payload, excerpt text) are kept —
    the manifest records a warning that they may contain incidental PII, so the
    export is for operator-controlled backup (self-containment directive), not
    external sharing.
    """
    from src.core.rev.corpus_export import export_corpus

    manifest = export_corpus(
        program_id=program,
        output_dir=output,
        programs_root=programs_root,
        include_vault=include_vault,
    )
    typer.echo(f"Exported PII-scrubbed corpus bundle for {program} → {output}")
    typer.echo(f"  candidates: {manifest['counts'].get('candidates', 0)}")
    typer.echo(f"  triage_decisions: {manifest['counts'].get('triage_decisions', 0)}")
    typer.echo(f"  labeled_corpus_records: {manifest['counts'].get('labeled_corpus_records', 0)}")
    if include_vault:
        typer.echo(f"  evidence_excerpts: {manifest['counts'].get('evidence_excerpts', 0)}")
        typer.echo(
            "  WARNING: evidence_vault.jsonl contains raw excerpt text — "
            "operator-controlled backup only; do not share externally.",
            err=True,
        )
    if manifest.get("warnings"):
        for w in manifest["warnings"]:
            typer.echo(f"  note: {w}", err=True)
    raise typer.Exit(code=0)


@app.command("label-corpus")
def rev_label_corpus(
    program: str = typer.Option(..., "--program", help="Program id."),
    import_file: Path = typer.Option(
        None, "--import",
        help=(
            "JSONL file of corpus annotations to import. Each line must have "
            "candidate_id, expected_event_type, and label (accept|reject). "
            "Annotator and second_label are optional. "
            "Existing records with the same candidate_id are updated (upsert)."
        ),
    ),
    bootstrap: bool = typer.Option(
        False, "--bootstrap/--no-bootstrap",
        help=(
            "Write empty skeleton corpus records for all pending candidates "
            "that are not yet in the corpus. Expected_event_type is set to the "
            "candidate's proposed_event_type (operator should review and correct); "
            "label is left blank for manual completion."
        ),
    ),
    programs_root: Path = typer.Option(
        PROGRAMS_ROOT, "--programs-root", help="Programs root directory.",
    ),
) -> None:
    """Import or bootstrap the REV labeled corpus for quality gating (S-9c).

    Manages ``programs/<id>/_quality/rev_labeled_corpus.jsonl``.
    Run with ``--import <file>`` to bulk-load pre-annotated records; run with
    ``--bootstrap`` to scaffold skeleton records for all un-annotated pending
    candidates.  Run ``vertex rev export-corpus`` to dump the full bundle.
    """
    import json

    from src.core.config_loader import PROGRAMS_ROOT as _PR
    from src.core.ledger.candidate_store import load_pending_candidates
    from src.core.jsonl_utils import read_jsonl_records

    prog_root = Path(programs_root)
    corpus_path = prog_root / program / "_quality" / "rev_labeled_corpus.jsonl"
    corpus_path.parent.mkdir(parents=True, exist_ok=True)

    if not import_file and not bootstrap:
        typer.echo(
            "Specify --import <file> to import annotations or --bootstrap to scaffold "
            "skeleton records for un-annotated candidates.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Load existing corpus (keyed by candidate_id for upsert).
    existing: dict[str, dict] = {}
    if corpus_path.exists():
        for row in read_jsonl_records(corpus_path):
            cid = str(row.get("candidate_id", "")).strip()
            if cid:
                existing[cid] = row

    imported = updated = bootstrapped = 0

    if import_file:
        if not import_file.exists():
            typer.echo(f"Import file not found: {import_file}", err=True)
            raise typer.Exit(code=1)
        raw_rows = read_jsonl_records(import_file)
        for row in raw_rows:
            cid = str(row.get("candidate_id", "")).strip()
            expected = str(row.get("expected_event_type", "")).strip()
            label = str(row.get("label", "")).strip().lower()
            if not cid or not expected or label not in ("accept", "reject", ""):
                typer.echo(f"  skipping malformed row: {row!r}", err=True)
                continue
            if cid in existing:
                existing[cid] = {**existing[cid], **row, "candidate_id": cid}
                updated += 1
            else:
                existing[cid] = row
                imported += 1
        typer.echo(f"Imported {imported} new + {updated} updated corpus records from {import_file}.")

    if bootstrap:
        candidates = load_pending_candidates(program, programs_root=prog_root)
        for cand in candidates:
            cid = str(cand.candidate_id)
            if cid not in existing:
                existing[cid] = {
                    "candidate_id": cid,
                    "expected_event_type": getattr(cand, "proposed_event_type", ""),
                    "label": "",          # operator must fill in
                    "annotator": "",
                    "second_label": "",
                    "notes": "bootstrapped — review and set label to accept/reject",
                }
                bootstrapped += 1
        typer.echo(f"Bootstrapped {bootstrapped} skeleton record(s) for un-annotated candidates.")

    # Write corpus back.
    with corpus_path.open("w", encoding="utf-8") as fh:
        for row in existing.values():
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    typer.echo(f"Corpus written: {corpus_path} ({len(existing)} total records).")
    raise typer.Exit(code=0)


__all__ = ["app", "rev_run", "rev_init_inbox", "rev_rotate_processed", "rev_export_corpus", "rev_label_corpus"]
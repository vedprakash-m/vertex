"""P2-5 ``vertex rev export-corpus`` — PII-scrubbed corpus export tests.

Exercises ``export_corpus`` (Zone-A pure) + the ``vertex rev export-corpus`` CLI
against a real staged candidate + vaulted evidence produced by ``run_rev_cycle``,
then asserts the redaction policy: direct identifiers hash-redacted, content
hashes kept, content fields kept, manifest documents the policy + warnings.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.commands.rev import app as rev_app
from src.core.ledger.candidate_store import (
    append_triage_decision,
    load_pending_candidates,
)
from src.core.ledger.source_refs import EmailRef
from src.core.ledger.rev_evidence import read_excerpt_text
from src.core.rev.corpus_export import export_corpus, redact_email
from src.core.ledger.candidate_store import CandidateDecisionRecord

runner = CliRunner()
NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


def _run_one_cycle(program_id: str, programs_root: Path) -> None:
    """Stage one real candidate (deployment.completed) with vaulted evidence."""
    from src.ai.rev.extractor import DeterministicRevExtractor
    from src.ai.rev.verification import run_layered_verification
    from src.core.models_v2 import REV_PROFILE_SEARCH_HYDRATE, RevRetrievalProfile
    from src.core.rev.entity_types import EntityType
    from src.core.rev.governor import BudgetLimits
    from src.core.rev.pipeline import RevPipelineDeps, run_rev_cycle
    from src.core.rev.prompt_shields import LocalOnlyPromptShields
    from src.core.rev.query_planner import RetrievalIntent
    from src.m365.rev import FakeRevGraphClient, GraphMessage
    from src.m365.rev.enumerators import CollectionSearchEnumerator, MailboxContext
    from src.m365.rev.hydrator import MailHydrator

    msg = GraphMessage(
        message_id="msg-export-1",
        subject="Deployment complete",
        sender="owner@example.com",
        received_at="2026-06-23T10:00:00Z",
        unique_body="The rollout deployment completed on 2026-06-23 without issues.",
        body="The rollout deployment completed on 2026-06-23 without issues.",
        conversation_id="conv-export-1",
        etag="etag-export-1",
        immutable_id="imm-export-1",
    )
    graph = FakeRevGraphClient((msg,))
    mailbox = MailboxContext(tenant_id="tenant-export", principal_mailbox="ved@ms.com", container="inbox")
    deps = RevPipelineDeps(
        enumerator=CollectionSearchEnumerator(graph, mailbox),
        hydrator=MailHydrator(graph, mailbox),
        shields=LocalOnlyPromptShields(),
        extractor=DeterministicRevExtractor(),
        verifier=lambda **kw: run_layered_verification(**kw).effective_state,
    )
    intent = RetrievalIntent(entity_type=EntityType.MESSAGE, limit=25)
    run_rev_cycle(
        program_id=program_id,
        intent=intent,
        deps=deps,
        profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
        mailbox_tenant_id="tenant-export",
        mailbox_principal="ved@ms.com",
        mailbox_container="inbox",
        correlation_id="export-test",
        programs_root=programs_root,
        budget_limits=BudgetLimits(),
        set_at=NOW,
    )


class TestRedactEmail:
    def test_bare_address_hashed(self) -> None:
        out = redact_email("owner@example.com")
        assert out.startswith("redacted:")
        assert "owner@example.com" not in out

    def test_display_name_preserved(self) -> None:
        out = redact_email("Owner Name <owner@example.com>")
        assert out.startswith("Owner Name <redacted:")
        assert "owner@example.com" not in out

    def test_none_passes_through(self) -> None:
        assert redact_email(None) is None
        assert redact_email("") == ""


class TestExportCorpus:
    def test_export_redacts_identifiers_keeps_hashes(self, tmp_path: Path) -> None:
        program_id = "p-export-1"
        _run_one_cycle(program_id, tmp_path)
        candidate = load_pending_candidates(program_id, programs_root=tmp_path)[0]
        # Seed a triage decision with a real actor identity.
        append_triage_decision(
            CandidateDecisionRecord(
                candidate_id=candidate.candidate_id,
                kind="approved",
                decided_at=NOW,
                triage_actor="ved@ms.com",
                reason="looks good",
            ),
            program_id=program_id,
            programs_root=tmp_path,
        )
        # Seed a labeled corpus record.
        qdir = tmp_path / program_id / "_quality"
        qdir.mkdir(parents=True)
        (qdir / "rev_labeled_corpus.jsonl").write_text(
            json.dumps({"candidate_id": candidate.candidate_id, "label": "accept"}) + "\n",
            encoding="utf-8",
        )

        out = tmp_path / "export"
        manifest = export_corpus(
            program_id=program_id,
            output_dir=out,
            programs_root=tmp_path,
            include_vault=True,
            exported_at=NOW,
        )

        # Candidates: identifiers redacted, content hashes kept.
        cand_lines = (out / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(cand_lines) == 1
        cand = json.loads(cand_lines[0])
        src = cand["source_ref"]
        assert "owner@example.com" not in json.dumps(src)
        assert src["sender"].startswith("redacted:")
        assert src["message_id"].startswith("redacted:")
        # Content hashes kept (needed for restore/dedup).
        assert cand["candidate_id"] == candidate.candidate_id
        assert cand["dedupe_core_hash"] == candidate.dedupe_core_hash
        assert cand["evidence_refs"][0]["vault_hash"] == candidate.evidence_refs[0].vault_hash

        # Triage: actor redacted, candidate_id kept.
        dec = json.loads((out / "triage_decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert dec["triage_actor"].startswith("redacted:")
        assert "ved@ms.com" not in dec["triage_actor"]
        assert dec["candidate_id"] == candidate.candidate_id
        assert dec["kind"] == "approved"

        # Labeled corpus copied through.
        corpus = (out / "rev_labeled_corpus.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(corpus) == 1
        assert json.loads(corpus[0])["label"] == "accept"

        # Evidence vault: excerpt text present, metadata identifiers redacted.
        vault_lines = (out / "evidence_vault.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(vault_lines) == 1
        vrec = json.loads(vault_lines[0])
        assert vrec["excerpt_text"]  # raw content kept (corpus value)
        assert vrec["vault_hash"].startswith("sha256:")
        meta = vrec["metadata"]
        assert "ved@ms.com" not in json.dumps(meta)
        assert "tenant-export" not in json.dumps(meta)

        # Manifest.
        assert manifest["schema_version"] == "rev_corpus_export.v1"
        assert manifest["counts"]["candidates"] == 1
        assert manifest["counts"]["triage_decisions"] == 1
        assert manifest["counts"]["labeled_corpus_records"] == 1
        assert manifest["counts"]["evidence_excerpts"] == 1
        assert manifest["includes_vault"] is True
        assert any("incidental PII" in w for w in manifest["warnings"])

    def test_export_without_vault_omits_vault_file(self, tmp_path: Path) -> None:
        program_id = "p-export-2"
        _run_one_cycle(program_id, tmp_path)
        out = tmp_path / "export"
        manifest = export_corpus(
            program_id=program_id, output_dir=out, programs_root=tmp_path,
            include_vault=False, exported_at=NOW,
        )
        assert not (out / "evidence_vault.jsonl").exists()
        assert manifest["counts"]["evidence_excerpts"] == 0
        assert manifest["includes_vault"] is False
        # Candidates still exported.
        assert manifest["counts"]["candidates"] == 1

    def test_export_warns_when_corpus_absent(self, tmp_path: Path) -> None:
        program_id = "p-export-3"
        _run_one_cycle(program_id, tmp_path)
        out = tmp_path / "export"
        manifest = export_corpus(
            program_id=program_id, output_dir=out, programs_root=tmp_path, exported_at=NOW,
        )
        assert not (out / "rev_labeled_corpus.jsonl").exists()
        assert manifest["counts"]["labeled_corpus_records"] == 0
        assert any("rev_labeled_corpus.jsonl absent" in w for w in manifest["warnings"])


class TestExportCorpusCli:
    def test_cli_exports_bundle(self, tmp_path: Path) -> None:
        program_id = "p-cli-export"
        _run_one_cycle(program_id, tmp_path)
        out = tmp_path / "bundle"
        result = runner.invoke(
            rev_app,
            ["export-corpus", "--program", program_id, "--output", str(out),
             "--programs-root", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "Exported PII-scrubbed corpus bundle" in result.output
        assert (out / "candidates.jsonl").is_file()
        assert (out / "manifest.json").is_file()

    def test_cli_include_vault_warns(self, tmp_path: Path) -> None:
        program_id = "p-cli-vault"
        _run_one_cycle(program_id, tmp_path)
        out = tmp_path / "bundle"
        result = runner.invoke(
            rev_app,
            ["export-corpus", "--program", program_id, "--output", str(out),
             "--include-vault", "--programs-root", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert (out / "evidence_vault.jsonl").is_file()
        assert "raw excerpt text" in result.output
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import pytest

from src.core import knowledge_candidate_store, knowledge_claim_store, knowledge_index, program_reality
from src.core.knowledge import vault
from src.core.fact_sor_state import save_fact_sor_state
from src.core.knowledge.vault import delete_vault_entry, ingest_knowledge_source, write_shared_vault_verify_status
from src.core.knowledge_candidate_store import KnowledgeCandidateDecisionRecord, append_candidate, append_triage_decision, build_candidate
from src.core.knowledge_claim_store import append_claim_revision, redact_claim_revision
from src.core.knowledge_index import rebuild_knowledge_index
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.ledger.event_log import ConfidenceTier
from src.core.ledger.source_refs import OperatorAssertionRef
from src.core.program_reality import ProgramReality
from src.core.workspace_lease import cooperative_capture_fence


NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def _claim(root: Path, value: str = "2026-H1"):
    return append_claim_revision(
        scope="org", subject="sku_generation:sample", predicate="first_deployment",
        value=value, valid_from=NOW, valid_until=None,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=OperatorAssertionRef(asserted_by="synthetic", asserted_at=NOW),
        knowledge_root=root, recorded_at=NOW,
    )


def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    programs = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    save_fact_sor_state("sample", mode="primary", recorded_at=NOW, programs_root=programs)
    config = programs / "sample" / "program.yaml"
    config.write_text("id: sample\nname: Synthetic\nknowledge_scopes: [org]\n", encoding="utf-8")
    root = get_shared_knowledge_root(programs)
    claim = _claim(root)
    candidate = build_candidate(
        candidate_id="synthetic-candidate", scope="org", subject="sku_generation:sample",
        predicate="first_deployment", value="2026-H2", valid_from=NOW, valid_until=None,
        proposed_confidence=ConfidenceTier.AI_EXTRACTED,
        source_ref=OperatorAssertionRef(asserted_by="synthetic", asserted_at=NOW),
        pipeline="synthetic", extraction_confidence=0.8, entity_resolution=(),
        corroborating_refs=(), batch_id="synthetic-batch", created_at=NOW,
    )
    append_candidate(candidate, programs_root=programs)
    source = tmp_path / "source.txt"
    source.write_text("Synthetic knowledge", encoding="utf-8")
    entry = ingest_knowledge_source(source, scope="org", programs_root=programs, ingested_at=NOW)
    return programs, root, claim, candidate, source, entry


def _mutate(writer: str, workspace):
    programs, root, claim, candidate, source, entry = workspace
    if writer == "claim":
        return _claim(root, "2026-H2")
    if writer == "redact":
        return redact_claim_revision(claim.claim_id, knowledge_root=root, actor="synthetic", reason="synthetic")
    if writer == "candidate":
        return append_candidate(candidate, programs_root=programs)
    if writer == "decision":
        return append_triage_decision(
            KnowledgeCandidateDecisionRecord(candidate.candidate_id, "rejected", NOW, "synthetic"),
            programs_root=programs,
        )
    if writer == "index":
        return rebuild_knowledge_index(knowledge_root=root, programs_root=programs)
    if writer == "ingest":
        return ingest_knowledge_source(source, scope="org", programs_root=programs, ingested_at=NOW)
    if writer == "delete":
        return delete_vault_entry(entry.vault_hash, programs_root=programs)
    assert writer == "verify"
    return write_shared_vault_verify_status(verified_at=NOW, ok=True, issue_records=[], programs_root=programs)


@pytest.mark.parametrize("writer", ["claim", "redact", "candidate", "decision", "index", "ingest", "delete", "verify"])
def test_actual_assessment_excludes_shared_knowledge_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str,
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    programs = workspace[0]
    captured, release, attempted, committed = Event(), Event(), Event(), Event()
    original = program_reality.load_scoped_claim_revisions

    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(program_reality, "load_scoped_claim_revisions", pause)

    def mutate():
        attempted.set()
        _mutate(writer, workspace)
        committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(ProgramReality.load, "sample", programs_root=programs, assessment_as_of=NOW)
        try:
            assert captured.wait(10)
            writing = pool.submit(mutate)
            assert attempted.wait(5)
            assert not committed.wait(0.2), "knowledge writer committed inside actual assessment"
        finally:
            release.set()
        result = reading.result(timeout=10)
        writing.result(timeout=10)
    assert committed.is_set()
    retained = result.knowledge_context(["sku_generation:sample"], as_of=NOW)
    assert retained.entry("sku_generation:sample").claims[0].value == "2026-H1"


@pytest.mark.parametrize(("writer", "module", "operation"), [
    ("claim", knowledge_index, "upsert_claim_vault_refs"),
    ("redact", knowledge_index, "remove_claim_vault_refs"),
    ("candidate", knowledge_index, "upsert_candidate_vault_refs"),
    ("decision", knowledge_index, "apply_candidate_decision_to_index"),
    ("index", knowledge_index, "_ensure_schema"),
    ("ingest", vault, "_record_scope_source"),
    ("delete", vault, "_remove_scope_sources"),
])
def test_actual_assessment_waits_for_complete_knowledge_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str, module, operation: str,
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    programs = workspace[0]
    appended, release, attempted, loaded, read_claims = Event(), Event(), Event(), Event(), Event()
    original = getattr(module, operation)
    load_claims = program_reality.load_scoped_claim_revisions

    def observe(*args, **kwargs):
        read_claims.set()
        return load_claims(*args, **kwargs)

    def pause(*args, **kwargs):
        appended.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, operation, pause)
    monkeypatch.setattr(program_reality, "load_scoped_claim_revisions", observe)

    def assess():
        attempted.set()
        result = ProgramReality.load("sample", programs_root=programs, assessment_as_of=NOW)
        loaded.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(_mutate, writer, workspace)
        try:
            assert appended.wait(10)
            reading = pool.submit(assess)
            assert attempted.wait(5)
            assert not read_claims.wait(1), "assessment read an unfinished claim/index publication"
            assert not loaded.is_set()
        finally:
            release.set()
        writing.result(timeout=10)
        result = reading.result(timeout=10)
    assert read_claims.is_set()
    assert loaded.is_set()
    if writer == "claim":
        assert result.knowledge_context(["sku_generation:sample"], as_of=NOW).entry(
            "sku_generation:sample",
        ).claims[0].value == "2026-H2"


def test_assessment_failure_releases_shared_knowledge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs, root, *_ = _workspace(tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic capture failure")

    monkeypatch.setattr(program_reality, "load_scoped_claim_revisions", fail)
    with pytest.raises(RuntimeError, match="synthetic capture failure"):
        ProgramReality.load("sample", programs_root=programs, assessment_as_of=NOW)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(_claim, root, "2026-H2").result(timeout=10).value == "2026-H2"


def test_shared_programs_exclude_writers_but_independent_workspace_progresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs, root, *_ = _workspace(tmp_path, monkeypatch)
    other_programs = tmp_path / "independent" / "programs"
    captured, release, attempted, committed = Event(), Event(), Event(), Event()
    original = program_reality.load_scoped_claim_revisions

    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(program_reality, "load_scoped_claim_revisions", pause)

    def write_other_program():
        attempted.set()
        result = ingest_knowledge_source(
            programs.parent / "source.txt", scope="program:another", programs_root=programs, ingested_at=NOW,
        )
        committed.set()
        return result

    with ThreadPoolExecutor(max_workers=3) as pool:
        reading = pool.submit(ProgramReality.load, "sample", programs_root=programs, assessment_as_of=NOW)
        try:
            assert captured.wait(10)
            writing = pool.submit(write_other_program)
            assert attempted.wait(5)
            assert not committed.wait(0.2)
            independent = pool.submit(_claim, get_shared_knowledge_root(other_programs)).result(timeout=5)
            assert independent.value == "2026-H1"
        finally:
            release.set()
        reading.result(timeout=10)
        writing.result(timeout=10)
    assert committed.is_set()


@pytest.mark.parametrize(("writer", "module", "operation"), [
    ("claim", knowledge_claim_store, "validate_predicate_value"),
    ("redact", knowledge_claim_store, "_locate_claim_revision_row"),
    ("candidate", knowledge_candidate_store, "append_jsonl_line"),
    ("decision", knowledge_candidate_store, "append_jsonl_line"),
    ("index", knowledge_index, "_ensure_schema"),
    ("ingest", vault, "_hash_file"),
    ("delete", vault, "load_vault_entry"),
    ("verify", Path, "write_text"),
])
def test_knowledge_writer_failure_propagates_and_releases_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str, module, operation: str,
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    root = workspace[1]

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic writer failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(module, operation, fail)
        with pytest.raises(RuntimeError, match="synthetic writer failure"):
            _mutate(writer, workspace)

    def reacquire():
        with cooperative_capture_fence(root):
            return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(reacquire).result(timeout=10)

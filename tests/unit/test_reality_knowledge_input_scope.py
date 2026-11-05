from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.fact_sor_state import save_fact_sor_state
from src.core.knowledge.vault import ingest_knowledge_source
from src.core.knowledge_claim_store import append_claim_revision
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.ledger.event_log import ConfidenceTier
from src.core.ledger.source_refs import OperatorAssertionRef
from src.core import edition_resolver
from src.core import program_reality
from src.core.program_reality import AttentionKind, ProgramReality


NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


@pytest.mark.parametrize("damaged", [False, True])
def test_actual_reality_uses_only_canonical_vault_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damaged: bool,
) -> None:
    programs = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    save_fact_sor_state("sample", mode="primary", recorded_at=NOW, programs_root=programs)
    source = tmp_path / "source.txt"
    source.write_text("Synthetic knowledge", encoding="utf-8")
    entry = ingest_knowledge_source(
        source, scope="program:sample", programs_root=programs, ingested_at=NOW,
    )
    decoy = programs / "knowledge" / "vault" / "decoy"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("Not canonical knowledge", encoding="utf-8")
    decoy.with_name(decoy.name + ".meta.json").write_text(
        entry.metadata_path.read_text(encoding="utf-8"), encoding="utf-8",
    )
    if damaged:
        entry.content_path.write_text("Changed content", encoding="utf-8")
    reality = ProgramReality.load("sample", programs_root=programs, assessment_as_of=NOW)
    findings = [item for item in reality.attention() if item.kind == AttentionKind.KNOWLEDGE_VAULT_INTEGRITY]
    assert len(findings) == int(damaged)
    if damaged:
        assert "1 file(s) with content hash mismatches" in findings[0].description


def test_actual_reality_preserves_canonical_knowledge_input_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    save_fact_sor_state("sample", mode="primary", recorded_at=NOW, programs_root=programs)
    source = tmp_path / "source.txt"
    source.write_text("Synthetic knowledge", encoding="utf-8")
    ingest_knowledge_source(
        source, scope="program:sample", programs_root=programs, ingested_at=NOW,
    )
    registry = tmp_path / "knowledge" / "programs" / "sample" / "sources.yaml"
    assert registry.is_file()
    registry.write_text("sources: [", encoding="utf-8")
    with pytest.raises(ConfigError):
        ProgramReality.load("sample", programs_root=programs, assessment_as_of=NOW)


@pytest.mark.parametrize("change_config", [False, True])
def test_actual_reality_reuses_its_selected_knowledge_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change_config: bool,
) -> None:
    programs = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    save_fact_sor_state("sample", mode="primary", recorded_at=NOW, programs_root=programs)
    config = programs / "sample" / "program.yaml"
    config.write_text("id: sample\nname: Synthetic\nknowledge_scopes: [org]\n", encoding="utf-8")
    claims = []
    for scope, value in (("org", "2026-H1"), ("domain:unselected", "2026-H2")):
        claims.append(append_claim_revision(
            scope=scope, subject="sku_generation:sample", predicate="first_deployment",
            value=value, valid_from=NOW, valid_until=None,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            source_ref=OperatorAssertionRef(asserted_by="synthetic", asserted_at=NOW),
            knowledge_root=get_shared_knowledge_root(programs), recorded_at=NOW,
        ))
    original = edition_resolver.load_program
    selected = []

    def select_program(*args, **kwargs):
        configured_program = original(*args, **kwargs)
        selected.append(configured_program)
        if change_config:
            config.write_text("id: sample\nname: Synthetic\nknowledge_scopes: []\n", encoding="utf-8")
        return configured_program

    monkeypatch.setattr(edition_resolver, "load_program", select_program)
    reality = ProgramReality.load("sample", programs_root=programs, assessment_as_of=NOW)
    retained = reality.knowledge_context(["sku_generation:sample"], as_of=NOW).entry("sku_generation:sample")
    assert len(selected) == 1
    assert selected[0].knowledge_scopes == ("org",)
    assert [claim.claim_id for claim in retained.claims] == [claims[0].claim_id]
    assert retained.claims[0].value == "2026-H1"


def test_actual_reality_preserves_selected_knowledge_scope_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    save_fact_sor_state("sample", mode="primary", recorded_at=NOW, programs_root=programs)

    def fail(*args, **kwargs):
        raise ConfigError("synthetic selected scope failure")

    monkeypatch.setattr(program_reality, "load_program_knowledge_scopes", fail)
    with pytest.raises(ConfigError, match="synthetic selected scope failure"):
        ProgramReality.load(
            "sample", programs_root=programs, assessment_as_of=NOW,
            require_committed_gather_run=False,
        )


def test_explicit_gather_override_keeps_scope_only_config_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    save_fact_sor_state("sample", mode="primary", recorded_at=NOW, programs_root=programs)
    config = programs / "sample" / "program.yaml"
    config.write_text(
        "id: sample\nname: Synthetic\nknowledge_scopes: [org]\ngather: {run_manifest_mode: invalid}\n",
        encoding="utf-8",
    )

    def unexpected_program_parse(*args, **kwargs):
        raise AssertionError("explicit gather override must not parse gather config")

    monkeypatch.setattr(edition_resolver, "load_program", unexpected_program_parse)
    reality = ProgramReality.load(
        "sample", programs_root=programs, assessment_as_of=NOW,
        require_committed_gather_run=False,
    )

    assert reality.knowledge_context([], as_of=NOW).scope_chain == ("program:sample", "org")

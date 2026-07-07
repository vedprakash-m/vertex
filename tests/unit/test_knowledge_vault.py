from __future__ import annotations

from datetime import datetime, timezone
import yaml
from pathlib import Path

from src.core.knowledge.vault import ingest_knowledge_source


def test_ingest_knowledge_source_writes_content_meta_and_scope_registry(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    source_path = tmp_path / "dd-acme-kb.md"
    source_path.write_text("# Title\n\nHello\n", encoding="utf-8")

    entry = ingest_knowledge_source(
        source_path,
        scope="domain:storage-platform",
        programs_root=programs_root,
        ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert entry.content_path.exists()
    assert entry.metadata_path.exists()
    assert entry.content_path.read_text(encoding="utf-8") == "# Title\n\nHello\n"
    metadata = yaml.safe_load(entry.metadata_path.read_text(encoding="utf-8"))
    assert metadata["vault_hash"] == entry.vault_hash
    assert metadata["original_filename"] == "dd-acme-kb.md"

    sources_path = tmp_path / "knowledge" / "domains" / "storage-platform" / "sources.yaml"
    registry = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    assert registry["sources"][0]["vault_hash"] == entry.vault_hash
    assert registry["sources"][0]["origin_path"] == str(source_path)
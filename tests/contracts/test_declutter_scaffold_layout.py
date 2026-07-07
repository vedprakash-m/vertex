"""Contract tests for the canonical program-directory scaffold layout
(specs/declutter.md §6 Phase 2-A).

A program onboarded mid-Phase-1-migration must be born canonical (runtime/,
docs/, summaries/ present; platform_proof_log.yaml at root) so it needs no
runtime migration (closes R-3). These tests lock that invariant against both
scaffold paths:

* the tracked template at ``programs/_templates/example_tpm/`` (the reference
  every copy-bootstrap inherits), and
* the ``vertex onboard`` create path's ``_write_documents`` mkdir set.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "programs" / "_templates" / "example_tpm"

# Canonical layout every new program inherits (declutter.md §6 2-A).
_CANONICAL_DIRS = ("runtime", "docs", "summaries")


def test_template_has_canonical_subdirs() -> None:
    """The tracked template must ship runtime/, docs/, summaries/ as placeholders."""
    for sub in _CANONICAL_DIRS:
        assert (TEMPLATE / sub).is_dir(), f"template missing canonical subdir: {sub}/"
        # Placeholder keepfile present so the empty dir survives git/VCS.
        assert any((TEMPLATE / sub).iterdir()), f"template {sub}/ has no keepfile"


def test_template_platform_proof_log_is_at_root_not_runtime() -> None:
    """platform_proof_log.yaml is T-4 (root durability), NOT a runtime/ artifact."""
    assert (TEMPLATE / "platform_proof_log.yaml").is_file(), "platform_proof_log.yaml must be at template root"
    assert not (TEMPLATE / "runtime" / "platform_proof_log.yaml").exists(), (
        "platform_proof_log.yaml must NOT be under runtime/ — it is T-4 root, purgeable runtime/ would risk it"
    )


def test_template_docs_readme_describes_one_time_docs_purpose() -> None:
    readme = TEMPLATE / "docs" / "README.md"
    assert readme.is_file(), "docs/README.md placeholder must exist in the template"
    text = readme.read_text(encoding="utf-8")
    # The placeholder must steer operators away from dropping platform artifacts in docs/.
    assert "docs/" in text and ("one-time" in text.lower() or "human" in text.lower())


def test_onboard_write_documents_scaffolds_canonical_dirs(tmp_path: Path) -> None:
    """The ``vertex onboard`` create path must mkdir runtime/, docs/, summaries/."""
    from src.commands.onboard import OnboardDocuments, OnboardPaths, _write_documents

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    paths = OnboardPaths(
        repo_root=tmp_path,
        reports_root=tmp_path / "reports",
        editions_root=program_dir / "editions",
        programs_root=programs_root,
        edition_path=program_dir / "editions" / "acme_weekly.yaml",
        program_dir=program_dir,
        knowledge_dir=program_dir / "knowledge",
    )
    documents = OnboardDocuments(
        edition={"schema_version": "2.0"},
        program={"schema_version": "3.0", "id": "acme"},
        workstreams={"schema_version": "1.0", "workstreams": []},
        scorecards={"scorecards": []},
        editorial_rules={},
        review={},
        people_directory={"people": []},
        teams={"teams": []},
        products={"products": []},
        golden_queries={"queries": []},
    )

    _write_documents(paths, "acme_weekly", documents, write_factual=True)

    for sub in _CANONICAL_DIRS:
        assert (program_dir / sub).is_dir(), f"onboard scaffold did not create {sub}/"
    # The pre-existing layout the scaffold always created must still be there.
    for sub in ("journal", "trajectories", "overrides", "narratives"):
        assert (program_dir / sub).is_dir(), f"onboard scaffold dropped existing {sub}/"
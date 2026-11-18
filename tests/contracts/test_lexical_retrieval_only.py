"""Retrieval stays lexical-only (FTS5/BM25 + rapidfuzz); embedding/vector stacks stay absent."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
_BANNED_MODULES = frozenset(
    {
        "sentence_transformers",
        "faiss",
        "sqlite_vec",
        "sqlite_vss",
        "chromadb",
        "lancedb",
        "qdrant_client",
        "pinecone",
        "weaviate",
        "hnswlib",
        "annoy",
        "torch",
        "transformers",
    }
)


def test_src_does_not_import_embedding_or_vector_libraries() -> None:
    violations: list[str] = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name.split(".")[0] in _BANNED_MODULES:
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} imports {name}")
    assert violations == []


def test_src_does_not_call_embedding_endpoints() -> None:
    pattern = re.compile(r"\.embeddings\.create\(|\bget_embedding\(|\bembed_documents\(|\bembed_query\(")
    hits = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted((REPO_ROOT / "src").rglob("*.py"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == []


def test_pyproject_declares_no_embedding_dependencies_and_pins_rapidfuzz() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements = list(project.get("dependencies", ()))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    names = {re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0].strip().lower().replace("-", "_") for requirement in requirements}
    assert names.isdisjoint(_BANNED_MODULES | {"sentence_transformers", "faiss_cpu", "faiss_gpu"})
    rapidfuzz_requirements = [requirement for requirement in requirements if requirement.lower().startswith("rapidfuzz")]
    assert rapidfuzz_requirements == ["rapidfuzz==3.14.6"]

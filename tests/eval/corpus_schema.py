"""ADF-W0.15 (specs/arch-data-fix.md Section 3.6.2a / Appendix C brief):
the evaluation corpus schema and loader. See governance/eval/corpus-schema.md
for the full field reference and lane design -- this module is the typed
implementation of that document, not a restatement of it.

Lives under tests/eval/ (not src/core/) because this is evaluation tooling,
not a production runtime feature -- matching the brief's own placement.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CORPUS_ROOT = Path(__file__).parent / "corpus"

SPLITS = frozenset({"train", "dev", "holdout"})
LABEL_SOURCES = frozenset({"human", "adjudicated", "heuristic", "llm_judge"})

#: Section 8.15.3: "LLM-as-judge may generate diagnostics but cannot be the
#: sole quality label or promotion authority." These are the only sources
#: admissible as holdout-lane ground truth.
INDEPENDENT_LABEL_SOURCES = frozenset({"human", "adjudicated"})


@dataclass(frozen=True, slots=True)
class CorpusItem:
    item_id: str
    family: str
    split: str
    input_excerpt: str
    label: Any
    label_source: str
    annotator: str | None = None
    annotated_at: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.split not in SPLITS:
            raise ValueError(f"CorpusItem.split={self.split!r} not in {sorted(SPLITS)}")
        if self.label_source not in LABEL_SOURCES:
            raise ValueError(f"CorpusItem.label_source={self.label_source!r} not in {sorted(LABEL_SOURCES)}")

    @property
    def is_independently_labeled(self) -> bool:
        return self.label_source in INDEPENDENT_LABEL_SOURCES


def _corpus_family_path(family: str, *, corpus_root: Path) -> Path:
    return corpus_root / f"{family}.jsonl"


def load_corpus_family(
    family: str,
    *,
    split: str | None = None,
    corpus_root: Path = _CORPUS_ROOT,
) -> tuple[CorpusItem, ...]:
    """Loads one family's corpus file, optionally filtered to one split.
    Returns an empty tuple (not an error) when the file doesn't exist --
    an unpopulated family is a real, expected state today (see
    governance/eval/corpus-schema.md's "Status" section)."""
    path = _corpus_family_path(family, corpus_root=corpus_root)
    if not path.exists():
        return ()
    items: list[CorpusItem] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        item = CorpusItem(
            item_id=str(raw["item_id"]),
            family=str(raw["family"]),
            split=str(raw["split"]),
            input_excerpt=str(raw["input_excerpt"]),
            label=raw["label"],
            label_source=str(raw["label_source"]),
            annotator=raw.get("annotator"),
            annotated_at=raw.get("annotated_at"),
            notes=raw.get("notes"),
        )
        if split is not None and item.split != split:
            continue
        items.append(item)
    return tuple(items)


__all__ = [
    "INDEPENDENT_LABEL_SOURCES",
    "LABEL_SOURCES",
    "SPLITS",
    "CorpusItem",
    "load_corpus_family",
]

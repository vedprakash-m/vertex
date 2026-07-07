from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class WorkIQPrecisionLabel(str, Enum):
    RELEVANT = "relevant"
    IRRELEVANT = "irrelevant"
    HALLUCINATED = "hallucinated"


@dataclass(frozen=True, slots=True)
class WorkIQPrecisionSample:
    sample_id: str
    lane_id: str
    query_name: str
    prompt_excerpt: str
    response_excerpt: str
    label: WorkIQPrecisionLabel
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class WorkIQPrecisionSummary:
    total_samples: int
    relevant_samples: int
    irrelevant_samples: int
    hallucinated_samples: int
    precision: float
    hallucination_rate: float
    irrelevant_rate: float


def load_workiq_precision_samples(path: Path) -> tuple[WorkIQPrecisionSample, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON list at {path}")

    samples: list[WorkIQPrecisionSample] = []
    for index, entry in enumerate(payload, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Sample #{index} in {path} must be an object")
        sample_id = str(entry.get("sample_id") or "").strip()
        lane_id = str(entry.get("lane_id") or "").strip()
        query_name = str(entry.get("query_name") or "").strip()
        prompt_excerpt = str(entry.get("prompt_excerpt") or "").strip()
        response_excerpt = str(entry.get("response_excerpt") or "").strip()
        raw_label = str(entry.get("label") or "").strip().lower()
        if not sample_id or not lane_id or not query_name or not prompt_excerpt or not response_excerpt:
            raise ValueError(f"Sample #{index} in {path} is missing required fields")
        try:
            label = WorkIQPrecisionLabel(raw_label)
        except ValueError as exc:
            raise ValueError(f"Unsupported label {raw_label!r} in {path} sample {sample_id}") from exc
        notes_raw = entry.get("notes")
        notes = str(notes_raw).strip() if isinstance(notes_raw, str) and notes_raw.strip() else None
        samples.append(
            WorkIQPrecisionSample(
                sample_id=sample_id,
                lane_id=lane_id,
                query_name=query_name,
                prompt_excerpt=prompt_excerpt,
                response_excerpt=response_excerpt,
                label=label,
                notes=notes,
            )
        )
    return tuple(samples)


def evaluate_workiq_precision(samples: tuple[WorkIQPrecisionSample, ...]) -> WorkIQPrecisionSummary:
    total = len(samples)
    if total <= 0:
        return WorkIQPrecisionSummary(
            total_samples=0,
            relevant_samples=0,
            irrelevant_samples=0,
            hallucinated_samples=0,
            precision=0.0,
            hallucination_rate=0.0,
            irrelevant_rate=0.0,
        )

    relevant = sum(1 for sample in samples if sample.label == WorkIQPrecisionLabel.RELEVANT)
    irrelevant = sum(1 for sample in samples if sample.label == WorkIQPrecisionLabel.IRRELEVANT)
    hallucinated = sum(1 for sample in samples if sample.label == WorkIQPrecisionLabel.HALLUCINATED)
    return WorkIQPrecisionSummary(
        total_samples=total,
        relevant_samples=relevant,
        irrelevant_samples=irrelevant,
        hallucinated_samples=hallucinated,
        precision=round(relevant / total, 4),
        hallucination_rate=round(hallucinated / total, 4),
        irrelevant_rate=round(irrelevant / total, 4),
    )


def passes_workiq_precision_gate(
    summary: WorkIQPrecisionSummary,
    *,
    min_precision: float,
    max_hallucination_rate: float,
) -> bool:
    if min_precision < 0.0 or max_hallucination_rate < 0.0:
        raise ValueError("Precision thresholds must be non-negative")
    return summary.precision >= min_precision and summary.hallucination_rate <= max_hallucination_rate

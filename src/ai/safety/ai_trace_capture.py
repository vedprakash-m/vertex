"""Opt-in, privacy-safe, TTL'd full AI I/O capture (arch-fix.md Phase 0).

`llm_trace.py` records metadata only (model, tokens, cost, latency) — it
never stores prompt/response text. That means the AF-1 semantic-validator
parity harness and the AF-4 local-router eval have no corpus to bake on
("without this, those gates are unrunnable" — arch-fix.md §A.0). This
module launches that corpus capture, sanitized through
``ai_trace_sanitizer.sanitize_ai_io`` and gated behind an explicit opt-in
flag mirroring ``llm_trace.py``'s ``VERTEX_LLM_TRACE`` gating style — off
by default, so no behavior changes for anyone who hasn't opted in.

The sidecar this writes (``ai/llm_trace_full_io.jsonl``) is registered in
``governance/data-classification.yaml`` and ``src/core/privacy_matrix.py``
(``SIDECAR_RETENTION``) with a short, dedicated retention class — it must
never accumulate indefinitely, and every persisted field is a sanitized
excerpt, never raw text.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ai.safety.ai_trace_sanitizer import sanitize_ai_io
from src.core.edition_resolver import PROGRAMS_ROOT, get_program_output_dir
from src.core.jsonl_utils import append_jsonl_line

_FULL_IO_CAPTURE_ENV = "VERTEX_AI_TRACE_FULL_IO"
# Matches the rev. 323 per-stem cap used by the other high-risk sidecars;
# rotate_jsonl_if_oversize's default retain=5 bounds on-disk footprint.
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024


def is_full_io_capture_enabled() -> bool:
    return os.environ.get(_FULL_IO_CAPTURE_ENV, "").strip().lower() in {"1", "true", "yes"}


def default_full_io_capture_path(edition: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_program_output_dir(edition, programs_root=programs_root) / "ai" / "llm_trace_full_io.jsonl"


def capture_ai_io(
    *,
    edition: str,
    run_id: str,
    caller: str,
    prompt_text: str,
    response_text: str,
    prompt_version: str | None = None,
    capture_file: Path | None = None,
) -> None:
    """No-op unless ``VERTEX_AI_TRACE_FULL_IO`` is set. Every persisted field
    is produced by ``sanitize_ai_io`` — never raw prompt/response text."""
    if not is_full_io_capture_enabled():
        return

    sanitized_prompt = sanitize_ai_io(prompt_text)
    sanitized_response = sanitize_ai_io(response_text)

    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "edition": edition,
        "run_id": run_id,
        "caller": caller,
        "prompt_version": prompt_version,
        "classification": "sanitized-excerpt",
        "prompt": {
            "excerpt": sanitized_prompt.text,
            "pii_detected": sanitized_prompt.pii_detected,
            "credential_detected": sanitized_prompt.credential_detected,
            "truncated": sanitized_prompt.truncated,
            "original_byte_length": sanitized_prompt.original_byte_length,
        },
        "response": {
            "excerpt": sanitized_response.text,
            "pii_detected": sanitized_response.pii_detected,
            "credential_detected": sanitized_response.credential_detected,
            "truncated": sanitized_response.truncated,
            "original_byte_length": sanitized_response.original_byte_length,
        },
    }

    target = capture_file or default_full_io_capture_path(edition)
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_line(target, line, max_bytes=_DEFAULT_MAX_BYTES)
    except Exception:
        # Corpus capture must never break a live AI call — same fail-open
        # posture as llm_trace.py's metadata trace.
        return

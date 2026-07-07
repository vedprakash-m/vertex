"""Voice validation helpers — thin re-exports from voice_validator.

These names are kept for backward compatibility with existing callers.
New code should import directly from src.core.voice_validator.
"""
from __future__ import annotations

from src.core.config_loader import EditorialRules
from src.core.voice_validator import VoiceViolation
from src.core.voice_validator import build_writing_contract_prompt_lines
from src.core.voice_validator import find_voice_violations
from src.core.voice_validator import has_decision_or_delta_lead
from src.core.voice_validator import starts_with_synthetic_delta_token
from src.core.voice_validator import uses_authentic_voice


# Re-export for callers that import from this module
VoiceViolation = VoiceViolation

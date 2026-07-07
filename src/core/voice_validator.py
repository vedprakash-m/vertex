from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from src.core.config_loader import EditorialRules


_DATE_PATTERN = re.compile(
    r"\b(?:[01]?\d/[0-3]?\d|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2})\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class VoiceViolation:
    location: str
    message: str


def uses_authentic_voice(
    editorial_rules: EditorialRules,
    program_context: Any | None,
    *,
    edition_name: str | None = None,
) -> bool:
    contract = editorial_rules.voice_contract
    if contract is None:
        return False
    if isinstance(edition_name, str) and edition_name in contract.applies_to_editions:
        return True
    if program_context is None:
        return False
    program_name = str(getattr(program_context, "program_name", "") or "").lower()
    current_phase = str(getattr(program_context, "current_phase", "") or "").lower()
    return any(token in program_name or token in current_phase for token in contract.program_tokens)


def build_writing_contract_prompt_lines(
    program_context: Any | None,
    *,
    editorial_rules: EditorialRules,
    workstream_name: str | None = None,
) -> tuple[str, ...]:
    if program_context is None:
        return ()

    lines: list[str] = []
    current_phase = getattr(program_context, "current_phase", None)
    if isinstance(current_phase, str) and current_phase.strip():
        lines.append(f"Current phase: {current_phase.strip()}")

    writing_style = getattr(program_context, "writing_style", None)
    if writing_style is not None:
        voice = getattr(writing_style, "voice", None)
        structure = getattr(writing_style, "structure", None)
        if isinstance(voice, str) and voice.strip():
            lines.append(f"Mandatory writing voice: {voice.strip()}")
        if isinstance(structure, str) and structure.strip():
            lines.append(f"Mandatory structure: {structure.strip()}")

        risk_framing = getattr(writing_style, "risk_framing", {}) or {}
        if isinstance(risk_framing, dict):
            for label in ("improving", "stuck", "escalation", "new_risk"):
                value = risk_framing.get(label)
                if isinstance(value, str) and value.strip():
                    pretty_label = label.replace("_", " ")
                    lines.append(f"Risk framing - {pretty_label}: {value.strip()}")

        preferred_patterns = tuple(getattr(writing_style, "preferred_patterns", ()) or ())
        for pattern in preferred_patterns:
            if isinstance(pattern, str) and pattern.strip():
                lines.append(f"Preferred pattern: {pattern.strip()}")

    dependency_lines = _dependency_lines(program_context, workstream_name=workstream_name)
    if dependency_lines:
        lines.append("Key dependency chain:")
        lines.extend(f"- {line}" for line in dependency_lines)

    sub_program_lines = _sub_program_lines(program_context, workstream_name=workstream_name)
    if sub_program_lines:
        lines.append("Program sub-program structure:")
        lines.extend(f"- {line}" for line in sub_program_lines)

    lines.extend(_workstream_lines(program_context, workstream_name=workstream_name))

    if uses_authentic_voice(editorial_rules, program_context):
        lines.append(
            "Do not use abstract portfolio phrasing. Name the blocking lane, concrete technical condition, owner or dependency chain, checkpoint, and consequence."
        )
        lines.append(
            "Do not start prose with synthetic delta tokens such as NEW, CLOSED, RISK_UP, RISK_DOWN, ETA, or OWNER."
        )

    lines.extend(_persona_prompt_lines(program_context))

    return tuple(lines)


def find_voice_violations(
    *,
    editorial_rules: EditorialRules,
    edition_name: str,
    exec_summary_text: str,
    workstream_blurbs: dict[str, str],
    program_context: Any | None,
    skip_persona_violations: bool = False,
) -> tuple[VoiceViolation, ...]:
    violations: list[VoiceViolation] = []

    # Contract-level violations — gated by the program's voice_contract
    if uses_authentic_voice(editorial_rules, program_context, edition_name=edition_name):
        concrete_terms = _concrete_terms(program_context, editorial_rules)
        violations.extend(
            _validate_text_block(
                location="exec_summary",
                text=exec_summary_text,
                concrete_terms=concrete_terms,
                editorial_rules=editorial_rules,
                require_decision_lead=True,
            )
        )
        for section_id, blurb in workstream_blurbs.items():
            violations.extend(
                _validate_text_block(
                    location=f"workstream:{section_id}",
                    text=blurb,
                    concrete_terms=concrete_terms,
                    editorial_rules=editorial_rules,
                    require_decision_lead=False,
                )
            )

    # Persona-level violations — gated by presence of personas with voice_rules
    if not skip_persona_violations:
        violations.extend(
            _find_persona_violations(
                program_context=program_context,
                exec_summary_text=exec_summary_text,
                workstream_blurbs=workstream_blurbs,
            )
        )

    return tuple(violations)


def _find_persona_violations(
    *,
    program_context: Any | None,
    exec_summary_text: str,
    workstream_blurbs: dict[str, str],
) -> tuple[VoiceViolation, ...]:
    """Check text blocks against machine-checkable persona voice rules.

    Runs independently of the global ``voice_contract`` — if a persona has
    ``voice_rules.banned_patterns``, those patterns are checked on every
    authored text block regardless of whether the edition-level contract fires.
    """
    personas = tuple(getattr(program_context, "leadership_personas", ()) or ())
    active = [p for p in personas if p.voice_rules is not None]
    if not active:
        return ()

    texts: list[tuple[str, str]] = [("exec_summary", exec_summary_text)]
    for section_id, blurb in workstream_blurbs.items():
        texts.append((f"workstream:{section_id}", blurb))

    violations: list[VoiceViolation] = []
    for persona in active:
        rules = persona.voice_rules
        assert rules is not None  # narrowed above
        bar_suffix = f" Communication bar: {persona.communication_bar}." if persona.communication_bar else ""
        for location, text in texts:
            if not isinstance(text, str) or not text.strip():
                continue
            lowered = text.lower()
            for pattern in rules.banned_patterns:
                if pattern in lowered:
                    violations.append(
                        VoiceViolation(
                            location=location,
                            message=(
                                f"[persona:{persona.persona_id}] Flagged pattern '{pattern}' "
                                f"(pet-peeve for {persona.role or persona.persona_id}).{bar_suffix}"
                            ),
                        )
                    )
            if rules.required_signals and not any(sig in lowered for sig in rules.required_signals):
                violations.append(
                    VoiceViolation(
                        location=location,
                        message=(
                            f"[persona:{persona.persona_id}] None of the required signals "
                            f"({', '.join(rules.required_signals)}) found.{bar_suffix}"
                        ),
                    )
                )
    return tuple(violations)


def starts_with_synthetic_delta_token(text: str, editorial_rules: EditorialRules) -> bool:
    contract = editorial_rules.voice_contract
    if contract is None:
        return False
    first_sentence = _first_sentence(text).lstrip()
    upper_sentence = first_sentence.upper()
    return any(
        upper_sentence.startswith(f"{prefix} ") or upper_sentence == prefix
        for prefix in contract.synthetic_delta_prefixes
    )


def has_decision_or_delta_lead(text: str, editorial_rules: EditorialRules) -> bool:
    contract = editorial_rules.voice_contract
    if contract is None:
        return False
    first_sentence = _lead_sentence(text, editorial_rules).lower()
    return _DATE_PATTERN.search(first_sentence) is not None or any(
        term in first_sentence for term in contract.decision_lead_terms
    )


def _persona_prompt_lines(program_context: Any | None) -> tuple[str, ...]:
    """Return prompt guidance lines derived from configured leadership personas.

    The editorial-quality gate persona is surfaced first (universal), followed
    by up to two additional personas.  Content is kept concise to avoid
    crowding the AI prompt with low-signal context.
    """
    personas = tuple(getattr(program_context, "leadership_personas", ()) or ())
    if not personas:
        return ()

    # editorial_quality is the canonical gate persona; surface it first
    ordered = sorted(personas, key=lambda p: (0 if p.persona_id == "editorial_quality" else 1, p.persona_id))

    lines: list[str] = []
    for persona in ordered[:3]:
        pid = persona.persona_id
        role_suffix = f" — {persona.role}" if persona.role else ""
        if persona.communication_bar:
            lines.append(f"Audience requirement ({pid}{role_suffix}): {persona.communication_bar}")
        if persona.cares_about:
            lines.append(f"Audience priority ({pid}{role_suffix}): {persona.cares_about}")
        for question in persona.typical_questions[:2]:
            lines.append(f"Audience will ask ({pid}): {question}")
    return tuple(lines)


def _validate_text_block(
    *,
    location: str,
    text: str,
    concrete_terms: tuple[str, ...],
    editorial_rules: EditorialRules,
    require_decision_lead: bool,
) -> tuple[VoiceViolation, ...]:
    contract = editorial_rules.voice_contract
    if contract is None or not isinstance(text, str) or not text.strip():
        return ()

    lowered = text.lower()
    violations: list[VoiceViolation] = []
    for phrase in contract.abstract_phrases:
        if re.search(rf"\b{re.escape(phrase)}\b", lowered, flags=re.IGNORECASE):
            violations.append(
                VoiceViolation(
                    location=location,
                    message=(
                        f"Configured authentic voice forbids abstract portfolio phrasing like '{phrase}'. "
                        "Use the concrete blocker, dependency chain, checkpoint, and consequence instead."
                    ),
                )
            )
    if starts_with_synthetic_delta_token(text, editorial_rules):
        violations.append(
            VoiceViolation(
                location=location,
                message=(
                    "Configured authentic voice does not lead with synthetic delta tokens. "
                    "Open with the blocking lane or the meaningful decision delta."
                ),
            )
        )
    if require_decision_lead and not has_decision_or_delta_lead(text, editorial_rules):
        violations.append(
            VoiceViolation(
                location=location,
                message=(
                    "Configured executive summary must open with the blocking lane or the key decision delta, not a generic status recap."
                ),
            )
        )
    if location == "exec_summary" and _has_bucketed_exec_summary_shape(text, editorial_rules):
        violations.append(
            VoiceViolation(
                location=location,
                message=(
                    "Configured executive summary must stay as one narrative block with inline lane updates. "
                    "Do not use bucket headings or bullet lists."
                ),
            )
        )
    if not _has_gate_signal(text, editorial_rules):
        violations.append(
            VoiceViolation(
                location=location,
                message=(
                    "Configured authentic voice requires a concrete checkpoint, ETA, gate, or dated decision signal in each authored summary block."
                ),
            )
        )
    if not _has_concrete_term(text, concrete_terms):
        violations.append(
            VoiceViolation(
                location=location,
                message="Configured authentic voice requires concrete lane and dependency terms, not generic status prose.",
            )
        )
    return tuple(violations)


def _has_bucketed_exec_summary_shape(text: str, editorial_rules: EditorialRules) -> bool:
    """Return True if the exec summary looks like a section-header bucket list.

    Bucket lists use top-level section labels as bullet prefixes (e.g. ``- DD-PF: blurb``
    or a bare ``DD-PF:`` heading ≤60 chars).  Detail-bullet lists (e.g.
    ``- __SCHIE Gaps__ (High): ...``) that enumerate workstream status inline are the
    *published standard* and must NOT be flagged.
    """
    contract = editorial_rules.voice_contract
    if contract is None:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return False
    prefixes = tuple(contract.exec_summary_bucket_prefixes)
    for line in lines:
        # Strip leading bullet marker to check the actual content
        bare = line.lstrip("-* ").lstrip()
        if bare.lower().startswith(prefixes):
            return True
        if line.endswith(":") and len(line) <= 60:
            return True
    return False


def _sub_program_lines(
    program_context: Any | None,
    *,
    workstream_name: str | None,
) -> tuple[str, ...]:
    sub_programs = tuple(getattr(program_context, "sub_programs", ()) or ())
    if not sub_programs:
        return ()

    matched_lines: list[str] = []
    if isinstance(workstream_name, str) and workstream_name.strip():
        normalized_workstream = workstream_name.strip().lower()
        for sub_program in sub_programs:
            name = str(getattr(sub_program, "name", "") or "").strip()
            aliases = tuple(str(alias).strip() for alias in getattr(sub_program, "aliases", ()) or ())
            tokens = {name.lower(), *(alias.lower() for alias in aliases if alias)}
            if normalized_workstream not in tokens:
                continue
            summary = str(getattr(sub_program, "summary", "") or "").strip()
            why_distinct = str(getattr(sub_program, "why_distinct", "") or "").strip()
            if summary:
                matched_lines.append(f"{name}: {summary}")
            if why_distinct:
                matched_lines.append(f"{name} boundary: {why_distinct}")
            return tuple(matched_lines)

    return tuple(
        f"{str(getattr(sub_program, 'name', '') or '').strip()}: {str(getattr(sub_program, 'summary', '') or '').strip()}"
        for sub_program in sub_programs
        if str(getattr(sub_program, "name", "") or "").strip()
        and str(getattr(sub_program, "summary", "") or "").strip()
    )


def _has_gate_signal(text: str, editorial_rules: EditorialRules) -> bool:
    contract = editorial_rules.voice_contract
    if contract is None:
        return False
    lowered = text.lower()
    return _DATE_PATTERN.search(lowered) is not None or any(term in lowered for term in contract.decision_lead_terms)


def _has_concrete_term(text: str, concrete_terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in concrete_terms)


def _dependency_lines(program_context: Any, *, workstream_name: str | None) -> tuple[str, ...]:
    dependencies = tuple(getattr(program_context, "key_dependency_chain", ()) or ())
    if not dependencies:
        return ()

    workstream_key = _normalize(workstream_name)
    matched_lines: list[str] = []
    fallback_lines: list[str] = []
    for dependency in dependencies:
        source = str(getattr(dependency, "source", "") or "").strip()
        target = str(getattr(dependency, "target", "") or "").strip()
        impact = str(getattr(dependency, "impact", "") or "").strip()
        if not (source or target or impact):
            continue
        line = " -> ".join(part for part in (source, target) if part)
        if impact:
            line = f"{line}: {impact}" if line else impact
        fallback_lines.append(line)
        if workstream_key and workstream_key in _normalize(line):
            matched_lines.append(line)
    selected = matched_lines or fallback_lines
    return tuple(selected[:3])


def _workstream_lines(program_context: Any, *, workstream_name: str | None) -> tuple[str, ...]:
    if not isinstance(workstream_name, str) or not workstream_name.strip():
        return ()

    lines: list[str] = []
    matched_workstream = _match_workstream(program_context, workstream_name)
    if matched_workstream is not None:
        why_it_matters = getattr(matched_workstream, "why_it_matters", None)
        current_blocker = getattr(matched_workstream, "current_blocker", None)
        if isinstance(why_it_matters, str) and why_it_matters.strip():
            lines.append(f"Why this lane matters: {why_it_matters.strip()}")
        if isinstance(current_blocker, str) and current_blocker.strip():
            lines.append(f"Current blocker: {current_blocker.strip()}")

    for owner in tuple(getattr(program_context, "workstream_owners", ()) or ()):
        areas = tuple(getattr(owner, "areas", ()) or ())
        if not any(_labels_match(workstream_name, area) for area in areas):
            continue
        style_note = getattr(owner, "style_note", None)
        if isinstance(style_note, str) and style_note.strip():
            lines.append(f"Owner style note: {style_note.strip()}")
    return tuple(lines)


def _match_workstream(program_context: Any, workstream_name: str) -> Any | None:
    normalized_name = _normalize(workstream_name)
    for workstream in tuple(getattr(program_context, "workstreams", ()) or ()):
        labels = (getattr(workstream, "name", None), *(tuple(getattr(workstream, "aliases", ()) or ())))
        if any(
            _normalize(label) == normalized_name
            or normalized_name in _normalize(label)
            or _normalize(label) in normalized_name
            for label in labels
            if isinstance(label, str)
        ):
            return workstream
    return None


def _concrete_terms(program_context: Any | None, editorial_rules: EditorialRules) -> tuple[str, ...]:
    contract = editorial_rules.voice_contract
    terms = set(contract.static_concrete_terms if contract is not None else ())
    if program_context is None:
        return tuple(sorted(terms, key=len, reverse=True))

    glossary = getattr(program_context, "glossary", {}) or {}
    if isinstance(glossary, dict):
        for key in glossary:
            normalized = str(key or "").strip().lower()
            if len(normalized) >= 3:
                terms.add(normalized)

    for workstream in tuple(getattr(program_context, "workstreams", ()) or ()):
        name = getattr(workstream, "name", None)
        if isinstance(name, str) and len(name.strip()) >= 3:
            terms.add(name.strip().lower())
        for alias in tuple(getattr(workstream, "aliases", ()) or ()):
            if isinstance(alias, str) and len(alias.strip()) >= 3:
                terms.add(alias.strip().lower())

    for theme in tuple(getattr(program_context, "recurring_themes", ()) or ()):
        if isinstance(theme, str) and len(theme.strip()) >= 3:
            terms.add(theme.strip().lower())

    for dependency in tuple(getattr(program_context, "key_dependency_chain", ()) or ()):
        for value in (getattr(dependency, "source", None), getattr(dependency, "target", None)):
            if isinstance(value, str) and len(value.strip()) >= 3:
                terms.add(value.strip().lower())

    for owner in tuple(getattr(program_context, "workstream_owners", ()) or ()):
        for area in tuple(getattr(owner, "areas", ()) or ()):
            if isinstance(area, str) and len(area.strip()) >= 3:
                terms.add(area.strip().lower())

    return tuple(sorted(terms, key=len, reverse=True))


def _labels_match(left: str, right: str) -> bool:
    normalized_left = _normalize(left)
    normalized_right = _normalize(right)
    return bool(normalized_left and normalized_right) and (
        normalized_left == normalized_right
        or normalized_left in normalized_right
        or normalized_right in normalized_left
    )


def _normalize(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _first_sentence(text: str) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        return ""
    match = re.search(r"[.!?]", normalized)
    if match is None:
        return normalized
    return normalized[: match.end()]


def _lead_sentence(text: str, editorial_rules: EditorialRules) -> str:
    contract = editorial_rules.voice_contract
    remaining = " ".join(text.split())
    while remaining:
        sentence = _first_sentence(remaining)
        lowered = sentence.lower()
        if contract is not None and any(lowered.startswith(prefix) for prefix in contract.objective_preamble_prefixes):
            remaining = remaining[len(sentence) :].lstrip()
            continue
        return sentence
    return ""

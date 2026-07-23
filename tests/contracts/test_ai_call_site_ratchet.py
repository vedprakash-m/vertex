"""Contract: every direct AI-provider call site is a known, inventoried one
(specs/backlog.md WO-4/BL-C2 step 5).

WO-4's inventory (`governance/ai-call-inventory.md`) enumerated every
`.structured(`/`.chat(` call site under `src/` and reconciled it against
`ai_policy.yaml`'s 26 features. This is the ratchet BL-C2 itself calls for:
"fails CI on any new or existing unregistered direct provider call outside
the approved inventory." A new call site in a file not on this list means
either a new AI feature was added without updating the inventory, or an
existing feature grew a second, un-reviewed call site -- either way, a
human should consciously extend `_KNOWN_CALL_SITE_FILES` (and update
`governance/ai-call-inventory.md` in the same change) rather than have it
happen silently.

File-level, not line-level: line numbers drift with routine edits and would
make this test flaky for no safety benefit. The invariant this protects is
"no new file starts talking to a provider unreviewed," not "no line in a
reviewed file ever moves."
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"

# Every file inventoried by WO-4 (governance/ai-call-inventory.md, 2026-07-22)
# as containing a real `.structured(`/`.chat(` provider call site, plus the
# shared infrastructure both routes through.
_KNOWN_CALL_SITE_FILES = frozenset({
    "src/ai/action_extractor.py",
    "src/ai/activation_judge.py",
    "src/ai/anticipation_engine.py",
    "src/ai/backfill_extractor.py",
    "src/ai/blurb_generator.py",
    "src/ai/claim_extractor.py",
    "src/ai/context_synthesizer.py",
    "src/ai/decision_brief_advisor.py",
    "src/ai/dependency_blast_radius_generator.py",
    "src/ai/deployment_fallback.py",  # shared FallbackAIClient/FallbackStructuredClient passthrough
    "src/ai/discovery/prose_event_extractor.py",
    "src/ai/exec_summary_drafter.py",
    "src/ai/governance_decision_brief_generator.py",
    "src/ai/intent_router.py",
    "src/ai/learning_distiller.py",
    "src/ai/m365_topic_router.py",
    "src/ai/meeting_action_extractor.py",
    "src/ai/onboard_assistant.py",
    "src/ai/program_synthesizer.py",
    "src/ai/rev/extractor.py",
    "src/ai/rev/judge.py",
    "src/ai/risk_proposal_generator.py",
    "src/ai/setup_assistant.py",
    "src/ai/summary_generator.py",
    "src/ai/synthesizer.py",
    "src/ai/top_three_candidate_generator.py",
    "src/commands/kb.py",
    "src/commands/report_lookback.py",
})

# Files containing the literal substring only inside a docstring/comment
# example, never a real call.
_DOCSTRING_ONLY_FILES = frozenset({
    "src/ai/tiered_router.py",
})


def _files_with_provider_calls() -> set[str]:
    found: set[str] = set()
    for py_file in _SRC_ROOT.rglob("*.py"):
        relative = py_file.relative_to(_REPO_ROOT).as_posix()
        if relative in _DOCSTRING_ONLY_FILES:
            continue
        text = py_file.read_text(encoding="utf-8")
        if ".structured(" in text or ".chat(" in text:
            found.add(relative)
    return found


def test_no_new_unreviewed_provider_call_site() -> None:
    actual = _files_with_provider_calls()
    unreviewed = actual - _KNOWN_CALL_SITE_FILES
    assert not unreviewed, (
        "New direct AI-provider call site(s) found outside the reviewed "
        f"inventory: {sorted(unreviewed)}. Add to governance/ai-call-inventory.md "
        "(classification, registry-managed prompt?, gateway?, audit?) and to "
        "this test's _KNOWN_CALL_SITE_FILES in the same change (specs/backlog.md BL-C2)."
    )


def test_known_call_site_list_has_no_stale_entries() -> None:
    """The reverse direction: a file removed/renamed since the inventory was
    written should be pruned here too, or this list silently stops meaning
    anything."""
    actual = _files_with_provider_calls()
    stale = _KNOWN_CALL_SITE_FILES - actual
    assert not stale, (
        f"These files no longer contain a provider call site: {sorted(stale)}. "
        "Remove them from _KNOWN_CALL_SITE_FILES (and governance/ai-call-inventory.md "
        "if the feature itself was removed)."
    )

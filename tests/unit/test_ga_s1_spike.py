"""Unit tests for the GA-S1 spike runbook's pure logic (scripts/ga_s1_spike.py).

The script itself cannot run end-to-end in CI (it drives the live WorkIQ CLI
against an authenticated mailbox). These tests cover the deterministic core that
the decision report rests on: stability math, JSON parsing (including the WorkIQ
CLI hard-wrap), environment-error classification, and the diagnosis mapping.

Canonical contract: specs/vertex-tech-spec.md (WorkIQ retrieval).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# The script lives outside the src/ package (it's a standalone operator tool),
# so load it by path rather than importing as a module.
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ga_s1_spike.py"
_spec = importlib.util.spec_from_file_location("ga_s1_spike", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
ga = importlib.util.module_from_spec(_spec)
sys.modules["ga_s1_spike"] = ga
_spec.loader.exec_module(ga)


# --- Stability math ---------------------------------------------------------

def test_jaccard_identical_sets_is_one() -> None:
    assert ga.jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_disjoint_sets_is_zero() -> None:
    assert ga.jaccard({"a"}, {"b"}) == 0.0


def test_jaccard_two_empty_sets_is_one_trivially_identical() -> None:
    # Spec §4.3: two empty result sets are trivially identical (no disagreement).
    assert ga.jaccard(set(), set()) == 1.0


def test_jaccard_partial_overlap() -> None:
    # {a,b,c} vs {a,b,d}: intersection 2, union 4 -> 0.5
    assert ga.jaccard({"a", "b", "c"}, {"a", "b", "d"}) == 0.5


def test_pairwise_jaccard_three_sets_yields_three_pairs() -> None:
    sets = [{"a"}, {"a", "b"}, {"b"}]
    scores = ga.pairwise_jaccard([set(s) for s in sets])
    assert len(scores) == 3  # C(3,2)


def test_parse_lane_requires_complete_privacy_safe_cli_declaration() -> None:
    assert ga.parse_lane("alpha|Alpha lane|term one; term two") == {
        "id": "alpha", "name": "Alpha lane", "terms": "term one; term two",
    }
    with pytest.raises(Exception):
        ga.parse_lane("alpha|missing terms")


def test_normalize_identity_prefers_conversation_id() -> None:
    record = {"conversationId": "conv-1", "threadId": "thread-1", "id": "msg-1"}
    assert ga.normalize_identity(record) == "conv-1"


def test_normalize_identity_falls_back_through_thread_and_id() -> None:
    assert ga.normalize_identity({"threadId": "thread-1", "id": "msg-1"}) == "thread-1"
    assert ga.normalize_identity({"id": "msg-1"}) == "msg-1"
    assert ga.normalize_identity({}) is None
    assert ga.normalize_identity({"conversationId": "  "}) is None


def test_normalize_identity_rejects_transient_workiq_ids() -> None:
    # WorkIQ returns per-call transient IDs like "turn1search1" that change every
    # invocation. They must NOT be used as the Jaccard identity, or every rep looks
    # disjoint even for the same recurring email.
    assert ga.normalize_identity({"id": "turn1search1"}) is None
    assert ga.normalize_identity({"conversationId": "turn3search42"}) is None


def test_normalize_identity_falls_back_to_subject_plus_datetime_for_transient_ids() -> None:
    # When only a transient ID is present, fall back to a stable subject+datetime
    # fingerprint so the same recurring email is recognized across reps.
    record = {"id": "turn1search1", "subject": "Re: XStore ramp", "receivedDateTime": "2026-06-10T08:00:00Z"}
    identity = ga.normalize_identity(record)
    assert identity is not None
    assert identity.startswith("subj:")
    assert "xstore ramp" in identity
    assert "2026-06-10T08:00:00Z" in identity


def test_normalize_identity_subject_fallback_requires_datetime() -> None:
    # Subject alone is ambiguous (replies share prefixes) — don't fingerprint
    # without the timestamp binding.
    assert ga.normalize_identity({"id": "turn1search1", "subject": "Re: XStore"}) is None


# --- Enumeration JSON parsing (incl. WorkIQ CLI hard-wrap) -------------------

def test_extract_emails_json_valid_envelope() -> None:
    raw = '{"emails":[{"id":"m1","conversationId":"c1","subject":"S","bodyPreview":"P","receivedDateTime":"2026-06-10T08:00:00Z"}]}'
    emails, outcome = ga.extract_emails_json(raw)
    assert outcome == "ok"
    assert len(emails) == 1
    assert emails[0]["conversationId"] == "c1"


def test_extract_emails_json_empty_set() -> None:
    emails, outcome = ga.extract_emails_json('{"emails":[]}')
    assert outcome == "empty"
    assert emails == []


def test_extract_emails_json_parse_failed_on_prose() -> None:
    emails, outcome = ga.extract_emails_json("I could not find any emails about that topic.")
    assert outcome == "parse_failed"
    assert emails == []


def test_extract_emails_json_recovers_cli_hard_wrap() -> None:
    # The WorkIQ CLI redirects stdout through a wrapper that hard-wraps long
    # lines, splitting JSON string values across newlines. The parser collapses
    # newlines on retry. This is the real-world failure mode the spec §4.3
    # "methodological notes" call out.
    raw = (
        '{"emails":[{"id":"m1","conversationId":"c1","subject":"A very long subject that '
        'got\nhard-wrapped by the\nCLI wrapper","bodyPreview":"p","receivedDateTime":"2026-06-10T08:00:00Z"}]}'
    )
    emails, outcome = ga.extract_emails_json(raw)
    assert outcome == "ok"
    assert len(emails) == 1


def test_one_hop_json_ok_with_decisions_key() -> None:
    raw = '{"decisions":["decide X"],"owners":["alice"],"etas":[],"raw_excerpts":["q"]}'
    assert ga.parse_one_hop_json(raw) == "ok"


def test_one_hop_json_ok_with_only_excerpts_key() -> None:
    raw = '{"raw_excerpts":["q"]}'
    assert ga.parse_one_hop_json(raw) == "ok"


def test_one_hop_json_parse_failed_on_prose() -> None:
    assert ga.parse_one_hop_json("Here is what the thread said: ...") == "parse_failed"


# --- Environment-error classification ---------------------------------------

def test_looks_like_error_clean_returncode_zero_is_none() -> None:
    result = {"returncode": 0, "stdout": '{"emails":[]}', "stderr": ""}
    assert ga.looks_like_error(result) is None


def test_looks_like_error_eula() -> None:
    result = {"returncode": 1, "stdout": "", "stderr": "EULA must be accepted before use."}
    assert ga.looks_like_error(result) == "eula_not_accepted"


def test_looks_like_error_auth() -> None:
    result = {"returncode": 1, "stdout": "", "stderr": "Authentication failed (401)."}
    assert ga.looks_like_error(result) == "auth"


def test_looks_like_error_throttled() -> None:
    result = {"returncode": 1, "stdout": "", "stderr": "Request was throttled (429)."}
    assert ga.looks_like_error(result) == "throttled"


def test_looks_like_error_generic_nonzero_exit() -> None:
    result = {"returncode": 2, "stdout": "", "stderr": "something else"}
    assert ga.looks_like_error(result) == "nonzero_exit_2"


# --- Diagnosis mapping (spec §4.4 orthogonal axes) --------------------------

def _probe(probe: str, outcome: str = "ok", emails=None, error_category=None, rep: int = 0) -> ga.ProbeResult:
    return ga.ProbeResult(
        probe=probe, rep=rep, prompt="p", prompt_hash="h", started_at="t",
        elapsed_seconds=1.0, returncode=0, error_category=error_category,
        outcome=outcome, emails=emails or [], relevance_labels={},
    )


def _lane(lane_id: str, enum_results, one_hop_results=None) -> ga.LaneCapture:
    results = list(enum_results)
    if one_hop_results:
        results.extend(one_hop_results)
    return ga.LaneCapture(
        lane_id=lane_id, lane_name=lane_id, lane_terms="terms", reps=len(enum_results),
        window_start="2026-06-06", window_end="2026-06-20",
        tool_version="v", ring="r", results=results,
    )


def test_decide_environment_when_positive_control_fails() -> None:
    pos = _probe("positive_control", outcome="empty", error_category="auth")
    captures = []
    decision = ga.decide(captures, pos)
    assert decision["diagnosis"] == "E_environment"
    assert decision["positive_control_ok"] is False


def test_decide_environment_when_any_lane_errors() -> None:
    pos = _probe("positive_control", outcome="ok", emails=[{"id": "1"}])
    lane = _lane("nova", [_probe("enumeration", error_category="auth")])
    decision = ga.decide([lane], pos)
    assert decision["diagnosis"] == "E_environment"
    assert "auth" in decision["environment_error_categories"]


def test_decide_retrieval_works_when_stable_and_sufficient_yield() -> None:
    pos = _probe("positive_control", outcome="ok", emails=[{"id": "1"}])
    # 5 reps, each returning the same 5 emails -> Jaccard 1.0, relevant yield 5.
    enum = [_probe("enumeration", emails=[{"conversationId": f"c{i}"} for i in range(5)]) for _ in range(5)]
    lane = _lane("nova", enum)
    decision = ga.decide([lane], pos)
    assert decision["diagnosis"] == "R_retrieval_works"


def test_decide_quiet_corpus_when_stable_but_low_yield() -> None:
    pos = _probe("positive_control", outcome="ok", emails=[{"id": "1"}])
    # Stable (identical sets) but only 1 email/relevant -> below AID_RELEVANT_YIELD.
    enum = [_probe("enumeration", emails=[{"conversationId": "c1"}]) for _ in range(5)]
    lane = _lane("nova", enum)
    decision = ga.decide([lane], pos)
    assert decision["diagnosis"] == "R_quiet_corpus"


def test_decide_unstable_when_jaccard_below_floor() -> None:
    pos = _probe("positive_control", outcome="ok", emails=[{"id": "1"}])
    # Disjoint sets across reps -> Jaccard 0.0, well below the unstable aid.
    enum = [
        _probe("enumeration", emails=[{"conversationId": "c1"}]),
        _probe("enumeration", emails=[{"conversationId": "c2"}]),
        _probe("enumeration", emails=[{"conversationId": "c3"}]),
    ]
    lane = _lane("nova", enum)
    decision = ga.decide([lane], pos)
    assert decision["diagnosis"] == "R_unstable"


def test_decide_one_hop_viability_aggregated_across_lanes() -> None:
    pos = _probe("positive_control", outcome="ok", emails=[{"id": "1"}])
    enum = [_probe("enumeration", emails=[{"conversationId": f"c{i}"} for i in range(5)]) for _ in range(5)]
    # 2 of 2 one-hop probes parse -> 100% >= 70% aid -> viable.
    one_hop = [_probe("one_hop", outcome="ok"), _probe("one_hop", outcome="ok")]
    lane = _lane("nova", enum, one_hop)
    decision = ga.decide([lane], pos)
    assert decision["one_hop_viable_provisional"] is True


def test_decide_one_hop_not_viable_when_below_aid() -> None:
    pos = _probe("positive_control", outcome="ok", emails=[{"id": "1"}])
    enum = [_probe("enumeration", emails=[{"conversationId": f"c{i}"} for i in range(5)]) for _ in range(5)]
    one_hop = [_probe("one_hop", outcome="parse_failed"), _probe("one_hop", outcome="parse_failed")]
    lane = _lane("nova", enum, one_hop)
    decision = ga.decide([lane], pos)
    assert decision["one_hop_viable_provisional"] is False


def test_decision_aids_marked_as_provisional_not_hard_gates() -> None:
    pos = _probe("positive_control", outcome="ok", emails=[{"id": "1"}])
    decision = ga.decide([_lane("nova", [_probe("enumeration", emails=[{"conversationId": "c1"}]) for _ in range(5)])], pos)
    assert "decision_aids" in decision
    assert "NOT hard gates" in decision["decision_aids"]["note"]

#!/usr/bin/env python3
"""GA-S1 standalone WorkIQ retrieval qualification runbook.

This is an *operator-run experiment*, not production code. It drives the WorkIQ
CLI directly (``workiq ask -q "<prompt>" -v``) to resolve the load-bearing
question that gates every downstream WorkIQ change: is the WorkIQ arm's
near-zero signal yield caused by **retrieval (R)** prompts or **environment (E)**
(auth/EULA/ring/config)?

Canonical contract: ``specs/vertex-tech-spec.md`` (WorkIQ retrieval).
  * Three probes per lane: (1) JSON enumeration, (2) per-thread prose extraction,
    (3) one-hop JSON hypothesis. Plus a positive control.
  * 5 repetitions per lane, byte-identical prompts, fixed absolute ISO window.
  * Full capture: prompt hash, tool version, ring, timestamps, latencies, parse
    outcome, normalized identity set by conversationId, and a human relevance
    label collected interactively.
  * Decision report on four orthogonal axes (yield / stability / relevance /
    errors), with provisional thresholds clearly marked as decision aids.

What this script is NOT:
  * It does not import src.commands / src.ai / src.m365 machinery — it is a
    standalone operator tool so it can run even if the gather pipeline is broken.
  * It does not write to any program journal or evidence store.
  * It does not enable FQ-01; FQ-01 enabling remains a separate operator
    decision gated on this script's results.

Usage (from repo root):
    python scripts/ga_s1_spike.py --help
    python scripts/ga_s1_spike.py run --operator <email> \
      --lane "lane-id|Lane name|term one; term two" [--reps 5]
    python scripts/ga_s1_spike.py report --capture <path>

Capture privacy (spec §4.5): raw subjects/permalinks/quotes are sensitive even
when "redacted". The default capture path is ``programs/_spike/`` which MUST be
gitignored. Only synthetic/irreversibly-sanitized fixtures belong in tests/data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --- Defaults (all overridable via flags) -----------------------------------

DEFAULT_REPS = 5
DEFAULT_WINDOW_DAYS = 14
DEFAULT_CAPTURE_DIR = Path("programs") / "_spike"
# Probe-1/2 result limit (matches build_structured_discovery_question default).
ENUMERATION_LIMIT = 8
# Number of threads to deep-extract per lane (Probe 2/3 sample size).
EXTRACTION_TOP_K = 3
# Provisional decision aids (spec §4.4: "not hard gates — GA-S1 produces the
# distribution; the real thresholds come from it"). Shown as aids only.
AID_JACCARD_PROCEED = 0.6
AID_JACCARD_UNSTABLE = 0.4
AID_ONEHOP_VIABLE = 0.7
AID_RELEVANT_YIELD = 3


# --- WorkIQ executable discovery (mirrors AgencyBridge._resolve_workiq_executable) ---

def resolve_workiq_executable() -> str:
    direct = shutil.which("workiq")
    if direct:
        return direct
    install_root = Path.home() / ".agency" / "WorkIQ.Cli.win-x64"
    candidates = sorted(install_root.glob("*/tools/workiq.exe"), reverse=True)
    if candidates:
        return str(candidates[0])
    raise FileNotFoundError(
        "Unable to locate workiq.exe. Ensure the WorkIQ CLI is installed "
        "(agency mcp workiq) and EULA-accepted."
    )


def workiq_version(executable: str) -> str:
    try:
        out = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=20
        )
        return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else "unknown"
    except Exception as exc:  # noqa: BLE001 — diagnostics only
        return f"unknown ({exc})"


def agency_ring() -> str:
    try:
        out = subprocess.run(
            ["agency", "ring", "--list"], capture_output=True, text=True, timeout=20
        )
        text = (out.stdout or out.stderr).strip()
        return text or "unknown"
    except Exception as exc:  # noqa: BLE001 — diagnostics only
        return f"unknown ({exc})"


# --- Prompt builders (inline prototypes of §5 functions; byte-identical across reps) ---

def build_enumeration_prompt(*, lane_terms: str, lane_name: str, window_start: str, window_end: str) -> str:
    return (
        "Use my Microsoft 365 mailbox to answer. "
        f"Which of my emails received between {window_start} and {window_end} "
        f"are about, or closely related to, '{lane_terms}' for workstream '{lane_name}'? "
        "Return JSON only, no markdown, using this schema: "
        '{"emails":[{"id":"","conversationId":"","threadId":"","subject":"","from":"",'
        '"receivedDateTime":"","webUrl":"","bodyPreview":""}]}. '
        f"Return up to {ENUMERATION_LIMIT} results. "
        'If there are no related emails, return {"emails":[]}.'
    )


def build_two_hop_prompt(*, conversation_id: str, subject: str, timestamp: str, sender: str,
                         lane_name: str, window_start: str, window_end: str) -> str:
    return (
        f"For the email thread (conversation '{conversation_id}') titled '{subject}', "
        f"latest message {timestamp} from {sender}, received between {window_start} and {window_end}, "
        f"relevant to workstream '{lane_name}': identify and quote VERBATIM from the message bodies: "
        "(1) decisions/direction set, (2) owners named, (3) dates/ETAs/deadlines, "
        "(4) blockers/risks (as ADO:NNNNN, IcM:NNNNN, PR:NNNN, or PIPELINE:NNNN). "
        "Be concise. If none, say so explicitly."
    )


def build_one_hop_prompt(*, subject: str, conversation_id: str, lane_name: str,
                         window_start: str, window_end: str) -> str:
    return (
        f"For the email thread titled '{subject}' (conversation '{conversation_id}') "
        f"received between {window_start} and {window_end}, relevant to workstream '{lane_name}': "
        "extract and return JSON only: "
        '{"decisions":["verbatim quote"],"owners":["name or alias"],'
        '"etas":[{"label":"","eta_date":"YYYY-MM-DD","owner":"","status":"open|closed|missed","ado_id":""}],'
        '"blocking_items":["ADO:NNNNN or IcM:NNNNN or PR:NNNN or PIPELINE:NNNN"],'
        '"raw_excerpts":["verbatim quote"],"confidence":0.0}. '
        "Ensure all quotes are JSON-escaped (internal quotes and newlines). "
        'If none, return {"decisions":[],...}.'
    )


# --- WorkIQ CLI invocation + response capture -------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _clean_cli_text(text: str) -> str:
    return _ANSI_RE.sub("", text)


def ask_workiq(executable: str, question: str, *, timeout_seconds: int = 180) -> dict[str, Any]:
    """Run ``workiq ask -q <question> -v`` and capture raw output + timing."""
    started = time.monotonic()
    proc = subprocess.run(
        [executable, "ask", "-q", question, "-v"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout_seconds,
    )
    elapsed = time.monotonic() - started
    return {
        "returncode": proc.returncode,
        "stdout": _clean_cli_text(proc.stdout or ""),
        "stderr": _clean_cli_text(proc.stderr or ""),
        "elapsed_seconds": round(elapsed, 2),
    }


def looks_like_error(result: dict[str, Any]) -> str | None:
    """Return an error category if the call clearly failed at the environment level."""
    if result["returncode"] != 0:
        stderr = (result["stderr"] or "").lower()
        if "eula" in stderr or "accept" in stderr:
            return "eula_not_accepted"
        if "auth" in stderr or "login" in stderr or "consent" in stderr or "401" in stderr:
            return "auth"
        if "throttl" in stderr or "429" in stderr:
            return "throttled"
        return f"nonzero_exit_{result['returncode']}"
    out = (result["stdout"] or "").lower()
    # A CLI-level error often surfaces on stdout/stderr even with rc 0.
    if "error:" in out and ("authenticate" in out or "token" in out):
        return "auth"
    return None


def extract_emails_json(raw_stdout: str) -> tuple[list[dict[str, Any]], str]:
    """Parse the Probe-1 JSON envelope. Returns (emails, outcome).

    outcome is one of: ok, empty, parse_failed. Handles the WorkIQ CLI hard-wrap
    (newlines inside JSON string values) by collapsing whitespace on retry.
    """
    decoder = json.JSONDecoder()

    def _try_parse(text: str) -> Any:
        for i, ch in enumerate(text):
            if ch == "{":
                try:
                    value, _ = decoder.raw_decode(text[i:])
                    return value
                except json.JSONDecodeError:
                    continue
        return None

    parsed = _try_parse(raw_stdout)
    if parsed is None:
        # CLI hard-wrap fallback: collapse newlines that split string values.
        collapsed = raw_stdout.replace("\r", "").replace("\n", "")
        parsed = _try_parse(collapsed)
    if not isinstance(parsed, dict) or "emails" not in parsed:
        # Maybe the response was an explicit empty-set object without the key.
        if isinstance(parsed, dict) and not parsed:
            return [], "empty"
        return [], "parse_failed"
    emails = parsed["emails"]
    if not isinstance(emails, list):
        return [], "parse_failed"
    if not emails:
        return [], "empty"
    return [e for e in emails if isinstance(e, dict)], "ok"


def parse_one_hop_json(raw_stdout: str) -> str:
    """Classify Probe-3 outcome: ok | parse_failed."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(raw_stdout):
        if ch == "{":
            try:
                value, _ = decoder.raw_decode(raw_stdout[i:])
                if isinstance(value, dict) and ("decisions" in value or "raw_excerpts" in value):
                    return "ok"
            except json.JSONDecodeError:
                continue
    collapsed = raw_stdout.replace("\r", "").replace("\n", "")
    for i, ch in enumerate(collapsed):
        if ch == "{":
            try:
                value, _ = decoder.raw_decode(collapsed[i:])
                if isinstance(value, dict) and ("decisions" in value or "raw_excerpts" in value):
                    return "ok"
            except json.JSONDecodeError:
                continue
    return "parse_failed"


# --- Stability math ---------------------------------------------------------

# WorkIQ returns transient per-call IDs like "turn1search1" that change every
# invocation (they're turn/search counters, not durable conversation IDs).
# Using them as the Jaccard identity makes every rep look disjoint even when the
# same email recurs — a measurement artifact, not real instability. The
# production validator (validate_structured_discovery_payload) rejects these via
# _TRANSIENT_WORKIQ_ID_RE; GA-S1 must do the same, falling back to a stable
# signal (subject + receivedDateTime) when no durable ID is present.
_TRANSIENT_WORKIQ_ID_RE = re.compile(r"^turn\d+(?:search|result)\d+$", re.IGNORECASE)


def normalize_identity(record: dict[str, Any]) -> str | None:
    """Canonical identity per spec: durable conversationId/threadId first; reject
    transient per-call IDs (turn1searchN); fall back to subject+datetime so the
    same recurring email is recognized across reps even without a durable ID."""
    for key in ("conversationId", "threadId", "id"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            candidate = value.strip()
            if not _TRANSIENT_WORKIQ_ID_RE.fullmatch(candidate):
                return candidate
    # Fallback: stable subject + receivedDateTime fingerprint. Subject alone is
    # ambiguous (replies share prefixes), so bind to the timestamp.
    subject = record.get("subject")
    received = record.get("receivedDateTime")
    if isinstance(subject, str) and isinstance(received, str):
        return f"subj:{subject.strip().lower()}|{received}"
    return None


def jaccard(set_a: set[str], set_b: set[str]) -> float:
    union = set_a | set_b
    if not union:
        return 1.0  # two empty sets are trivially identical
    return len(set_a & set_b) / len(union)


def pairwise_jaccard(identity_sets: list[set[str]]) -> list[float]:
    scores: list[float] = []
    for i in range(len(identity_sets)):
        for j in range(i + 1, len(identity_sets)):
            scores.append(jaccard(identity_sets[i], identity_sets[j]))
    return scores


# --- Capture records --------------------------------------------------------

@dataclass
class ProbeResult:
    probe: str
    rep: int
    prompt: str
    prompt_hash: str
    started_at: str
    elapsed_seconds: float
    returncode: int
    error_category: str | None
    outcome: str
    emails: list[dict[str, Any]] = field(default_factory=list)
    relevance_labels: dict[str, str] = field(default_factory=dict)  # identity -> on/off-workstream


@dataclass
class LaneCapture:
    lane_id: str
    lane_name: str
    lane_terms: str
    reps: int
    window_start: str
    window_end: str
    tool_version: str
    ring: str
    results: list[ProbeResult] = field(default_factory=list)


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def collect_relevance(emails: list[dict[str, Any]]) -> dict[str, str]:
    """Interactive relevance labeling (spec §4.3). Operator labels each email."""
    labels: dict[str, str] = {}
    if not emails:
        return labels
    print("\n  Relevance labeling (on/off-workstream). Press Enter for 'off'.")
    for idx, email in enumerate(emails, 1):
        identity = normalize_identity(email) or f"noid-{idx}"
        subject = (email.get("subject") or "").strip()[:80]
        preview = (email.get("bodyPreview") or "").strip()[:100]
        raw = input(f"    [{idx}] subject='{subject}' preview='{preview}' on/off? ").strip().lower()
        labels[identity] = "on" if raw.startswith("on") else "off"
    return labels


def auto_label_relevance(emails: list[dict[str, Any]], *, lane_terms: str) -> dict[str, str]:
    """Non-interactive relevance proxy for unattended runs.

    Heuristic: an email is 'on' if any lane term token appears in the subject or
    preview (case-insensitive), else 'off'. This is a deliberately coarse proxy
    for the human label the spec calls for — it overestimates 'off' for synonyms
    and underestimates 'on' for off-keyword-but-on-thread mail. Labels are tagged
    ``auto`` in the capture so a human reviewer knows to re-check before using the
    precision figures for a go decision.
    """
    tokens = set()
    for raw in re.split(r"[;,]", lane_terms):
        for word in raw.split():
            w = re.sub(r"[^a-z0-9]", "", word.lower())
            if len(w) >= 3:
                tokens.add(w)
    labels: dict[str, str] = {}
    for idx, email in enumerate(emails, 1):
        identity = normalize_identity(email) or f"noid-{idx}"
        haystack = f"{email.get('subject','')} {email.get('bodyPreview','')}".lower()
        flat = re.sub(r"[^a-z0-9 ]", " ", haystack)
        labels[identity] = "on" if any(t in flat for t in tokens) else "off"
    return labels


# --- The run ---------------------------------------------------------------

def run_lane(
    *,
    executable: str,
    lane: dict[str, str],
    reps: int,
    window_start: str,
    window_end: str,
    tool_version: str,
    ring: str,
    no_label: bool = False,
    skip_extraction: bool = False,
) -> LaneCapture:
    lane_id = lane["id"]
    lane_name = lane["name"]
    lane_terms = lane["terms"]
    print(f"\n=== Lane: {lane_id} ({lane_name}) ===")
    print(f"    terms: {lane_terms}")
    print(f"    window: {window_start} .. {window_end}, reps: {reps}")

    capture = LaneCapture(
        lane_id=lane_id, lane_name=lane_name, lane_terms=lane_terms,
        reps=reps, window_start=window_start, window_end=window_end,
        tool_version=tool_version, ring=ring,
    )

    enum_prompt = build_enumeration_prompt(
        lane_terms=lane_terms, lane_name=lane_name,
        window_start=window_start, window_end=window_end,
    )

    # Probe 1: enumeration, 5 reps, relevance labeled on the first rep only.
    top_threads: list[dict[str, Any]] = []
    for rep in range(reps):
        print(f"  [Probe 1 / rep {rep + 1}/{reps}] enumeration ...")
        result = ask_workiq(executable, enum_prompt)
        emails, outcome = extract_emails_json(result["stdout"])
        err = looks_like_error(result)
        labels: dict[str, str] = {}
        if outcome == "ok" and rep == 0:
            labels = (
                auto_label_relevance(emails, lane_terms=lane_terms)
                if no_label else collect_relevance(emails)
            )
            top_threads = emails[:EXTRACTION_TOP_K]
        pr = ProbeResult(
            probe="enumeration", rep=rep, prompt=enum_prompt, prompt_hash=prompt_hash(enum_prompt),
            started_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=result["elapsed_seconds"],
            returncode=result["returncode"], error_category=err, outcome=outcome,
            emails=emails, relevance_labels=labels,
        )
        capture.results.append(pr)
        print(f"      outcome={outcome} emails={len(emails)} err={err} latency={result['elapsed_seconds']}s")
        if err:
            print(f"      ⚠ environment error category '{err}' — continuing remaining reps for the record.")

    # Probes 2 & 3: deep extraction on the top-K threads from rep 0.
    # Skippable for fast stability-only runs (--skip-extraction); the one-hop
    # viability signal is then deferred to a later full run.
    threads_to_extract = () if skip_extraction else tuple(top_threads)
    if skip_extraction:
        print("  [Probes 2 & 3] skipped (--skip-extraction)")
    for thread in threads_to_extract:
        conversation_id = (thread.get("conversationId") or thread.get("threadId") or "unknown")
        subject = (thread.get("subject") or "(no subject)").replace("'", "")
        timestamp = (thread.get("receivedDateTime") or "unknown")
        sender = str(thread.get("from") or thread.get("sender") or "unknown")
        if isinstance(thread.get("from"), dict):
            ea = thread["from"].get("emailAddress") or {}
            sender = str(ea.get("address") or ea.get("name") or sender)

        two_hop_prompt = build_two_hop_prompt(
            conversation_id=conversation_id, subject=subject, timestamp=timestamp, sender=sender,
            lane_name=lane_name, window_start=window_start, window_end=window_end,
        )
        one_hop_prompt = build_one_hop_prompt(
            subject=subject, conversation_id=conversation_id, lane_name=lane_name,
            window_start=window_start, window_end=window_end,
        )
        print(f"  [Probe 2] two-hop extraction: '{subject[:50]}' ...")
        r2 = ask_workiq(executable, two_hop_prompt)
        capture.results.append(ProbeResult(
            probe="two_hop", rep=0, prompt=two_hop_prompt, prompt_hash=prompt_hash(two_hop_prompt),
            started_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=r2["elapsed_seconds"],
            returncode=r2["returncode"], error_category=looks_like_error(r2),
            outcome="ok" if r2["returncode"] == 0 else "error",
        ))
        print(f"      latency={r2['elapsed_seconds']}s")
        print(f"  [Probe 3] one-hop JSON: '{subject[:50]}' ...")
        r3 = ask_workiq(executable, one_hop_prompt)
        one_hop_outcome = parse_one_hop_json(r3["stdout"])
        capture.results.append(ProbeResult(
            probe="one_hop", rep=0, prompt=one_hop_prompt, prompt_hash=prompt_hash(one_hop_prompt),
            started_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=r3["elapsed_seconds"],
            returncode=r3["returncode"], error_category=looks_like_error(r3),
            outcome=one_hop_outcome,
        ))
        print(f"      one-hop outcome={one_hop_outcome} latency={r3['elapsed_seconds']}s")

    return capture


def run_positive_control(*, executable: str, operator: str, window_start: str, window_end: str) -> ProbeResult:
    """Spec §4.4 positive control: a known-high-volume query to confirm environment health.

    A positive control must be a query that *any* healthy mailbox will answer with
    results, independent of the program lanes being tested. The original draft used
    "emails sent by <operator>", but the enumeration schema has no sender-match
    guarantee, so WorkIQ correctly returned nothing — a false (E) signal. A
    near-universal content term ("meeting", "sync", "update") is the right control:
    every active corporate mailbox contains meeting-related mail, so an empty result
    genuinely indicates an environment problem rather than a prompt-shape problem.
    """
    print("\n=== Positive control (environment health) ===")
    prompt = build_enumeration_prompt(
        lane_terms="meeting, sync, or status update", lane_name="positive-control",
        window_start=window_start, window_end=window_end,
    )
    result = ask_workiq(executable, prompt)
    emails, outcome = extract_emails_json(result["stdout"])
    err = looks_like_error(result)
    print(f"    outcome={outcome} emails={len(emails)} err={err}")
    return ProbeResult(
        probe="positive_control", rep=0, prompt=prompt, prompt_hash=prompt_hash(prompt),
        started_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=result["elapsed_seconds"],
        returncode=result["returncode"], error_category=err, outcome=outcome,
        emails=emails,
    )


# --- Decision report (spec §4.4 axes) ---------------------------------------

def decide(captures: list[LaneCapture], positive_control: ProbeResult) -> dict[str, Any]:
    env_errors = [r.error_category for cap in captures for r in cap.results if r.error_category]
    pos_ok = positive_control.outcome == "ok" and len(positive_control.emails) > 0 and not positive_control.error_category

    # Per-lane axes.
    per_lane: list[dict[str, Any]] = []
    for cap in captures:
        enum_results = [r for r in cap.results if r.probe == "enumeration"]
        identity_sets = []
        relevant_counts = []
        for r in enum_results:
            ids = {nid for e in r.emails if (nid := normalize_identity(e))}
            identity_sets.append(ids)
            if r.relevance_labels:
                relevant_counts.append(sum(1 for v in r.relevance_labels.values() if v == "on"))
            else:
                relevant_counts.append(len(r.emails))
        jaccards = pairwise_jaccard(identity_sets) if len(identity_sets) >= 2 else []
        avg_jaccard = round(sum(jaccards) / len(jaccards), 3) if jaccards else None
        avg_relevant = round(sum(relevant_counts) / len(relevant_counts), 2) if relevant_counts else 0
        one_hop_results = [r for r in cap.results if r.probe == "one_hop"]
        one_hop_ok_rate = (
            round(sum(1 for r in one_hop_results if r.outcome == "ok") / len(one_hop_results), 2)
            if one_hop_results else None
        )
        per_lane.append({
            "lane_id": cap.lane_id,
            "avg_relevant_yield": avg_relevant,
            "pairwise_jaccard": avg_jaccard,
            "one_hop_ok_rate": one_hop_ok_rate,
            "env_error_count": sum(1 for r in enum_results if r.error_category),
        })

    # Provisional decision aids (NOT hard gates — spec §4.4).
    # The four axes (errors / stability / yield / precision) are orthogonal. Check
    # them in priority order: environment failure dominates everything; then
    # instability (a primary diagnosis regardless of yield — an unstable lane with
    # low yield is still primarily unstable); then yield (sufficient vs quiet);
    # precision (R_broken) is the residual when results arrive but quality is off.
    diagnosis = "undetermined"
    if env_errors or not pos_ok:
        diagnosis = "E_environment"
    else:
        # Note: pairwise_jaccard can legitimately be 0.0 (disjoint result sets),
        # which is falsy in Python. Guard with "is not None", never "or".
        scored = [pl["pairwise_jaccard"] for pl in per_lane if pl["pairwise_jaccard"] is not None]
        all_stable = bool(scored) and all(s >= AID_JACCARD_PROCEED for s in scored)
        any_unstable = any(s < AID_JACCARD_UNSTABLE for s in scored)
        any_low_yield = any(pl["avg_relevant_yield"] < AID_RELEVANT_YIELD for pl in per_lane)
        if any_unstable:
            # Stability is a primary axis: an unstable lane is unstable regardless
            # of whether its yield is also low. FQ-01 would proceed with union_runs.
            diagnosis = "R_unstable"
        elif all_stable and not any_low_yield:
            diagnosis = "R_retrieval_works"
        elif all_stable and any_low_yield:
            diagnosis = "R_quiet_corpus"
        else:
            diagnosis = "R_broken_precision"  # yield present but stability/precision unclear

    one_hop_viable = None
    ok_rates = [pl["one_hop_ok_rate"] for pl in per_lane if pl["one_hop_ok_rate"] is not None]
    if ok_rates:
        one_hop_viable = (sum(ok_rates) / len(ok_rates)) >= AID_ONEHOP_VIABLE

    return {
        "diagnosis": diagnosis,
        "positive_control_ok": pos_ok,
        "environment_error_categories": sorted(set(env_errors)),
        "per_lane": per_lane,
        "one_hop_viable_provisional": one_hop_viable,
        "decision_aids": {
            "jaccard_proceed_aid": AID_JACCARD_PROCEED,
            "jaccard_unstable_aid": AID_JACCARD_UNSTABLE,
            "relevant_yield_aid": AID_RELEVANT_YIELD,
            "one_hop_viable_aid": AID_ONEHOP_VIABLE,
            "note": "Provisional aids per spec §4.4, NOT hard gates. Derive real thresholds from this distribution.",
        },
    }


# --- CLI entry points -------------------------------------------------------

def parse_lane(value: str) -> dict[str, str]:
    """Parse a privacy-safe CLI lane declaration: ``id|name|terms``."""
    parts = [part.strip() for part in value.split("|", 2)]
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError("lane must be 'id|name|term one; term two'")
    return {"id": parts[0], "name": parts[1], "terms": parts[2]}


def cmd_run(args: argparse.Namespace) -> int:
    executable = resolve_workiq_executable()
    version = workiq_version(executable)
    ring = agency_ring()
    window_end = (datetime.now(timezone.utc)).date()
    window_start = window_end - timedelta(days=args.window_days)
    ws, we = window_start.isoformat(), window_end.isoformat()

    capture_dir = Path(args.capture)
    capture_dir.mkdir(parents=True, exist_ok=True)
    gitignore = Path(".gitignore")
    # Warn loudly if the capture dir isn't ignored. The repo's .gitignore uses
    # ``programs/*`` + an allowlist (``!programs/_templates/``), so programs/_spike
    # is covered by the blanket ignore; detect either the blanket pattern or an
    # explicit entry so operators don't get a false alarm on the safe config.
    ignored = False
    if gitignore.exists():
        lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
        ignored = any(
            pattern in lines
            for pattern in ("programs/*", "programs/_spike", "_spike/")
        )
    print(f"WorkIQ CLI : {executable}")
    print(f"version    : {version}")
    print(f"ring       : {ring}")
    print(f"window     : {ws} .. {we}")
    print(f"reps       : {args.reps}")
    print(f"capture    : {capture_dir.resolve()}")
    if not ignored:
        print(
            "⚠ CAPTURE PRIVACY (spec §4.5): capture dir is NOT covered by .gitignore. "
            "Raw subjects/permalinks/quotes are sensitive. Add 'programs/*' (with an "
            "allowlist for _templates) or an explicit 'programs/_spike/' entry before "
            "committing anything from this run."
        )

    # Positive control first (spec §4.4).
    pos = run_positive_control(executable=executable, operator=args.operator, window_start=ws, window_end=we)

    captures: list[LaneCapture] = []
    for lane in args.lanes:
        captures.append(run_lane(
            executable=executable, lane=lane, reps=args.reps,
            window_start=ws, window_end=we, tool_version=version, ring=ring,
            no_label=args.no_label,
            skip_extraction=args.skip_extraction,
        ))

    decision = decide(captures, pos)
    report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "operator": args.operator,
        "tool_version": version,
        "ring": ring,
        "window_start": ws,
        "window_end": we,
        "reps": args.reps,
        "relevance_labeling": "auto" if args.no_label else "human",
        "positive_control": asdict(pos),
        "lanes": [asdict(c) for c in captures],
        "decision": decision,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = capture_dir / f"ga_s1_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n=== DECISION ===")
    print(json.dumps(decision, indent=2))
    print(f"\nCapture written to {out_path}")
    print("Review the decision; if diagnosis != E_environment, proceed to §5 (FQ-01) per spec.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.capture).read_text(encoding="utf-8"))
    print(json.dumps(data["decision"], indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GA-S1 standalone WorkIQ retrieval qualification runbook.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run the GA-S1 spike (interactive — labels relevance per email).")
    p_run.add_argument("--operator", required=True, help="Mailbox identity for the positive control (never persisted outside the ignored capture).")
    p_run.add_argument(
        "--lane", dest="lanes", action="append", type=parse_lane, required=True,
        help="Lane declaration 'id|name|term one; term two'; repeat for each lane.",
    )
    p_run.add_argument("--reps", type=int, default=DEFAULT_REPS, help=f"Repetitions per lane (default {DEFAULT_REPS}).")
    p_run.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS, help=f"Lookback days (default {DEFAULT_WINDOW_DAYS}).")
    p_run.add_argument("--capture", default=str(DEFAULT_CAPTURE_DIR), help="Capture output directory (MUST be gitignored).")
    p_run.add_argument("--no-label", action="store_true", help="Skip interactive relevance labeling; auto-derive on/off from lane-term tokens (coarse proxy — re-check before go decisions).")
    p_run.add_argument("--skip-extraction", action="store_true", help="Skip Probe 2/3 (per-thread extraction); run only enumeration + positive control. Faster stability-only run.")
    p_run.set_defaults(func=cmd_run)

    p_rep = sub.add_parser("report", help="Print the decision block from a prior capture.")
    p_rep.add_argument("--capture", required=True, help="Path to a ga_s1_*.json capture file.")
    p_rep.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

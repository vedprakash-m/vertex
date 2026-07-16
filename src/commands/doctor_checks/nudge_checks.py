"""Nudge health checks.

Existing checks: NQ-1..NQ-10 (reserved IDs from fix-nudge.md)
New governance framework: NQD-1..NQD-12 (specs/nudge-gaps.md §10)

NQD execution classes:
  doctor_local   — static analysis, no ADO calls
  ci_contract    — import-boundary / architecture taint (CI only)
  runtime_*      — run only during actual nudge run (not bare doctor)
  doctor_online  — network checks, only under `--online`

`vertex doctor --nudge` runs: doctor_local + ci_contract only.
It must NOT make 250 ADO calls (runtime_* checks are excluded).

NQD-1   doctor_local   DRI coverage (Phase 2)
NQD-2   runtime_preflight  Required section non-empty (Phase 2)
NQD-3   doctor_local   Subject/preheader present (Phase 1)
NQD-4   doctor_local   Deadline health (Phase 2)
NQD-5   doctor_local   Hardcoded-deadline drift vs milestone (Phase 2/3)
NQD-6   doctor_local   Waiver governance (Phase 2)
NQD-7   runtime_postflight  Comment budget (Phase 4)
NQD-8   doctor_local   Audience policy (Phase 2)
NQD-9   doctor_local/runtime  Send reconciliation (Phase 3)
NQD-10  ci_contract    Fact single-seam (Phase 0)
NQD-11  doctor_online  Milestone source (Phase 2)
NQD-12  ci_contract    No evidence in nudge path (Phase 1)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.commands.doctor_checks.models import DoctorCheck
from src.core.edition_resolver import PROGRAMS_ROOT, get_legacy_nudge_output, get_nudge_paths
from src.core.exceptions import ConfigError
from src.core.nudge_models import NUDGE_AUDIT_MAX_BYTES, NUDGE_STATE_SCHEMA_VERSION
from src.core.yaml_utils import load_yaml_mapping


TEMPLATES_ROOT = Path(__file__).resolve().parents[4] / "templates"


def run_nudge_doctor(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    templates_root: Path | None = None,
) -> tuple[DoctorCheck, ...]:
    checks: list[DoctorCheck] = []
    tpl_root = templates_root or TEMPLATES_ROOT

    edition_path = programs_root / program_id / "editions" / f"{program_id}_nudge.yaml"
    raw: dict[str, Any] = {}
    edition_ok = False

    # NQ-1: Edition file present and parseable
    if not edition_path.exists():
        checks.append(DoctorCheck(
            label="NQ-1 nudge edition file",
            status="fail",
            detail=f"Missing: {edition_path}. Create editions/{program_id}_nudge.yaml.",
        ))
    else:
        try:
            raw = load_yaml_mapping(edition_path, required=True)
            edition_ok = True
            checks.append(DoctorCheck(
                label="NQ-1 nudge edition file",
                status="ok",
                detail=f"Found: {edition_path.name}",
            ))
        except (ConfigError, Exception) as exc:
            checks.append(DoctorCheck(
                label="NQ-1 nudge edition file",
                status="fail",
                detail=f"Parse error: {exc}",
            ))

    if not edition_ok:
        # Skip all further edition-dependent checks
        _stub_checks(checks, range(2, 11))
        return tuple(checks)

    fh = raw.get("full_hygiene") or {}
    if not isinstance(fh, dict):
        fh = {}
    hygiene = raw.get("hygiene") or {}
    if not isinstance(hygiene, dict):
        hygiene = {}

    # NQ-2: New sections: format
    has_new_sections = isinstance(fh.get("sections"), list) and len(fh["sections"]) > 0
    has_legacy = any(k in fh for k in ("ramp_p1_tag", "post_ramp_tag", "section_a_tag", "area_paths"))
    if has_new_sections:
        checks.append(DoctorCheck(
            label="NQ-2 sections format",
            status="ok",
            detail=f"New sections: format with {len(fh['sections'])} section(s).",
        ))
    elif has_legacy:
        checks.append(DoctorCheck(
            label="NQ-2 sections format",
            status="warn",
            detail=(
                "Legacy full_hygiene keys detected (ramp_p1_tag / post_ramp_tag). "
                "Migrate to sections: format. Legacy support will be removed in the next release."
            ),
        ))
    else:
        checks.append(DoctorCheck(
            label="NQ-2 sections format",
            status="fail",
            detail="full_hygiene must contain either sections: or legacy keys.",
        ))

    # NQ-3: Recipient resolves in people directory
    recipient_alias = str(fh.get("recipient") or "").strip()
    if not recipient_alias:
        checks.append(DoctorCheck(
            label="NQ-3 recipient resolution",
            status="fail",
            detail="full_hygiene.recipient is empty or missing.",
        ))
    else:
        person_ok = _check_recipient_in_directory(recipient_alias, programs_root=programs_root)
        if person_ok:
            checks.append(DoctorCheck(
                label="NQ-3 recipient resolution",
                status="ok",
                detail=f"Recipient alias {recipient_alias!r} found in people directory.",
            ))
        else:
            checks.append(DoctorCheck(
                label="NQ-3 recipient resolution",
                status="warn",
                detail=(
                    f"Recipient alias {recipient_alias!r} not found in people directory. "
                    "Nudge will fail at runtime without a valid email."
                ),
            ))

    # NQ-4: Template file exists
    template_name = str(fh.get("template") or "partials/nudge_full_hygiene.j2")
    template_path = tpl_root / template_name
    if template_path.exists():
        checks.append(DoctorCheck(
            label="NQ-4 template file",
            status="ok",
            detail=f"Template found: {template_name}",
        ))
    else:
        checks.append(DoctorCheck(
            label="NQ-4 template file",
            status="fail",
            detail=f"Template missing: templates/{template_name}. Create or fix the template path.",
        ))

    # NQ-5: State file validity (if it exists) — check new then legacy path
    _np = get_nudge_paths(program_id, programs_root=programs_root)
    _legacy_state = programs_root / program_id / "nudge_state.json"
    state_path = _np.state_path if _np.state_path.exists() else _legacy_state
    if not state_path.exists():
        checks.append(DoctorCheck(
            label="NQ-5 state file",
            status="ok",
            detail="No state file yet — will be created on first run.",
        ))
    else:
        try:
            raw_state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(raw_state, dict):
                raise ValueError("Expected a JSON object (dict)")
            invalid_ts_count = 0
            for k, v in raw_state.items():
                if k == "schema_version":
                    continue
                # D-5: schema 1.2 dict shape {triggered_at, origin, run_id};
                # schema 1.1 bare ISO string. Extract the timestamp accordingly
                # to match the runtime read path in nudge_state_store.py.
                if isinstance(v, dict):
                    ts = v.get("triggered_at")
                elif isinstance(v, str):
                    ts = v
                else:
                    invalid_ts_count += 1
                    continue
                if not isinstance(ts, str):
                    invalid_ts_count += 1
                    continue
                try:
                    datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    invalid_ts_count += 1
            if invalid_ts_count > 0:
                checks.append(DoctorCheck(
                    label="NQ-5 state file",
                    status="warn",
                    detail=f"State file has {invalid_ts_count} invalid timestamp(s). Run `vertex nudge --reset-cooldown --yes` to clear.",
                ))
            else:
                item_count = sum(1 for k in raw_state if k != "schema_version")
                checks.append(DoctorCheck(
                    label="NQ-5 state file",
                    status="ok",
                    detail=f"State file valid: {item_count} cooldown record(s).",
                ))
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            checks.append(DoctorCheck(
                label="NQ-5 state file",
                status="fail",
                detail=f"State file parse error: {exc}. Delete and regenerate.",
            ))

    # NQ-6: State schema version
    if state_path.exists():
        try:
            raw_state = json.loads(state_path.read_text(encoding="utf-8"))
            sv = str(raw_state.get("schema_version") or "")
            if sv == NUDGE_STATE_SCHEMA_VERSION:
                checks.append(DoctorCheck(
                    label="NQ-6 state schema version",
                    status="ok",
                    detail=f"schema_version={sv}",
                ))
            elif sv.startswith("1."):
                checks.append(DoctorCheck(
                    label="NQ-6 state schema version",
                    status="warn",
                    detail=(
                        f"State schema_version={sv!r}; expected {NUDGE_STATE_SCHEMA_VERSION!r}. "
                        "Will auto-migrate on next run."
                    ),
                ))
            else:
                checks.append(DoctorCheck(
                    label="NQ-6 state schema version",
                    status="fail",
                    detail=f"Unknown schema_version={sv!r}; expected {NUDGE_STATE_SCHEMA_VERSION!r}.",
                ))
        except (json.JSONDecodeError, OSError):
            checks.append(DoctorCheck(
                label="NQ-6 state schema version",
                status="fail",
                detail="Cannot read state file for schema version check.",
            ))
    else:
        checks.append(DoctorCheck(
            label="NQ-6 state schema version",
            status="ok",
            detail="No state file yet — will be written at current version on first run.",
        ))

    # NQ-7: Section IDs unique and non-empty (new format only)
    if has_new_sections:
        sec_ids: list[str] = []
        bad_ids: list[str] = []
        for sec_raw in fh["sections"]:
            if not isinstance(sec_raw, dict):
                bad_ids.append("<non-dict entry>")
                continue
            sid = str(sec_raw.get("id") or "").strip()
            if not sid:
                bad_ids.append("<empty>")
            else:
                sec_ids.append(sid)
        duplicates = {sid for sid in sec_ids if sec_ids.count(sid) > 1}
        if bad_ids:
            checks.append(DoctorCheck(
                label="NQ-7 section IDs",
                status="fail",
                detail=f"Section(s) with missing/empty id: {bad_ids}.",
            ))
        elif duplicates:
            checks.append(DoctorCheck(
                label="NQ-7 section IDs",
                status="fail",
                detail=f"Duplicate section id(s): {sorted(duplicates)}.",
            ))
        else:
            checks.append(DoctorCheck(
                label="NQ-7 section IDs",
                status="ok",
                detail=f"{len(sec_ids)} unique section ID(s): {sec_ids}.",
            ))
    else:
        checks.append(DoctorCheck(
            label="NQ-7 section IDs",
            status="ok",
            detail="Legacy format — section ID check not applicable.",
        ))

    # NQ-8: No @example.com or empty recipients
    all_recipient_aliases = _collect_all_recipient_refs(fh)
    example_com_refs = [r for r in all_recipient_aliases if "example.com" in r.lower()]
    if example_com_refs:
        checks.append(DoctorCheck(
            label="NQ-8 no example.com recipients",
            status="fail",
            detail=f"Config contains @example.com references: {example_com_refs}.",
        ))
    else:
        checks.append(DoctorCheck(
            label="NQ-8 no example.com recipients",
            status="ok",
            detail="No @example.com placeholders found in nudge config.",
        ))

    # NQ-9: Audit JSONL under size cap — check new then legacy path
    _legacy_output = get_legacy_nudge_output(program_id, programs_root=programs_root)
    audit_path = _np.audit_path if _np.audit_path.exists() else _legacy_output / "nudge_audit.jsonl"
    if not audit_path.exists():
        checks.append(DoctorCheck(
            label="NQ-9 audit JSONL size",
            status="ok",
            detail="No audit log yet — will be created on first run.",
        ))
    else:
        size = audit_path.stat().st_size
        cap_pct = size / NUDGE_AUDIT_MAX_BYTES * 100
        if size >= NUDGE_AUDIT_MAX_BYTES:
            checks.append(DoctorCheck(
                label="NQ-9 audit JSONL size",
                status="fail",
                detail=(
                    f"Audit log at or over {NUDGE_AUDIT_MAX_BYTES // (1024 * 1024)}MB cap "
                    f"({size // 1024}KB). Rotation may not be working."
                ),
            ))
        elif cap_pct >= 80:
            checks.append(DoctorCheck(
                label="NQ-9 audit JSONL size",
                status="warn",
                detail=(
                    f"Audit log at {cap_pct:.0f}% of {NUDGE_AUDIT_MAX_BYTES // (1024 * 1024)}MB cap "
                    f"({size // 1024}KB). Will auto-rotate soon."
                ),
            ))
        else:
            checks.append(DoctorCheck(
                label="NQ-9 audit JSONL size",
                status="ok",
                detail=f"Audit log {size // 1024}KB ({cap_pct:.0f}% of cap).",
            ))

    # NQ-10: Legacy nudge output paths should not exist (warn after migration)
    _legacy_output_dir = get_legacy_nudge_output(program_id, programs_root=programs_root)
    _legacy_state_path = programs_root / program_id / "nudge_state.json"
    legacy_paths_present = [
        p for p in (_legacy_output_dir, _legacy_state_path) if p.exists()
    ]
    if legacy_paths_present:
        checks.append(DoctorCheck(
            label="NQ-10 legacy nudge paths",
            status="warn",
            detail=(
                f"Legacy nudge path(s) still exist: {[str(p) for p in legacy_paths_present]}. "
                "Run `python scripts/migrate_nudge_layout.py --program {program_id}` to migrate, "
                "then remove the old paths."
            ),
        ))
    else:
        checks.append(DoctorCheck(
            label="NQ-10 legacy nudge paths",
            status="ok",
            detail="No legacy nudge paths detected.",
        ))

    return tuple(checks)


def _stub_checks(checks: list[DoctorCheck], indices: range) -> None:
    labels = {
        2: "NQ-2 sections format",
        3: "NQ-3 recipient resolution",
        4: "NQ-4 template file",
        5: "NQ-5 state file",
        6: "NQ-6 state schema version",
        7: "NQ-7 section IDs",
        8: "NQ-8 no example.com recipients",
        9: "NQ-9 audit JSONL size",
        10: "NQ-10 legacy nudge paths",
    }
    for i in indices:
        checks.append(DoctorCheck(
            label=labels.get(i, f"NQ-{i}"),
            status="warn",
            detail="Skipped — edition file not available.",
        ))


def _check_recipient_in_directory(alias: str, *, programs_root: Path) -> bool:
    from src.core.knowledge_store import load_program_knowledge  # noqa: PLC0415
    # Try to load from shared knowledge first
    shared_root = programs_root.parent / "knowledge"
    try:
        if shared_root.exists():
            from src.core.knowledge_store import load_knowledge  # noqa: PLC0415
            knowledge = load_knowledge(knowledge_root=shared_root)
        else:
            return False
        alias_lower = alias.strip().lower()
        return any(
            (getattr(p, "alias", None) or "").strip().lower() == alias_lower
            for p in knowledge.people_directory
        )
    except Exception:
        return False


def _collect_all_recipient_refs(fh: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    r = str(fh.get("recipient") or "").strip()
    if r:
        refs.append(r)
    for sec in (fh.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        sec_r = str(sec.get("recipient") or "").strip()
        if sec_r:
            refs.append(sec_r)
    return refs


# ---------------------------------------------------------------------------
# NQD-1..NQD-12 — governance check framework (specs/nudge-gaps.md §10)
# Execution class: doctor_local + ci_contract run via run_nudge_nqd_checks().
# runtime_* and doctor_online are excluded from bare doctor.
# ---------------------------------------------------------------------------


def run_nudge_nqd_checks(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    templates_root: Path | None = None,
) -> tuple[DoctorCheck, ...]:
    """Run doctor_local + ci_contract NQD checks (Phase 1/2, no ADO calls)."""
    checks: list[DoctorCheck] = []
    tpl_root = templates_root or TEMPLATES_ROOT

    # Load config (reuse NQ-1 pattern)
    edition_path = programs_root / program_id / "editions" / f"{program_id}_nudge.yaml"
    if not edition_path.exists():
        checks.append(DoctorCheck(
            label="NQD prerequisite",
            status="warn",
            detail=f"Edition file not found: {edition_path}. Run `vertex doctor --nudge` for NQ-1 details.",
        ))
        return tuple(checks)

    try:
        from src.core.yaml_utils import load_yaml_mapping as _lym  # noqa: PLC0415
        raw = _lym(edition_path, required=True)
        fh: dict[str, Any] = raw.get("full_hygiene") or {}
        if not isinstance(fh, dict):
            fh = {}
    except Exception as exc:
        checks.append(DoctorCheck(
            label="NQD prerequisite",
            status="fail",
            detail=f"Edition parse error: {exc}",
        ))
        return tuple(checks)

    # NQD-3 (doctor_local, Phase 1): Subject/preheader not missing or defaulted
    _nqd3_checks(checks, fh, program_id)

    # NQD-4 (doctor_local, Phase 2): Deadline health — requires_milestone ref missing/stale
    _nqd4_checks(checks, fh, program_id)

    # NQD-5 (doctor_local, Phase 2/3): Hardcoded-deadline drift vs live milestone
    _nqd5_checks(checks, fh, program_id)

    # NQD-6 (doctor_local, Phase 2): Waiver governance — expired or missing expires
    _nqd6_checks(checks, fh, program_id)

    # NQD-8 (doctor_local, Phase 2): Audience policy — domain, opt-out, unresolved_owner
    _nqd8_checks(checks, fh, program_id)

    # NQD-10 (ci_contract, Phase 0): Fact single-seam — nudge facts via append_nudge_event only
    _nqd10_checks(checks)

    # NQD-12 (ci_contract, Phase 1): No evidence in nudge path
    _nqd12_checks(checks)

    return tuple(checks)


def _nqd3_checks(checks: list[DoctorCheck], fh: dict[str, Any], program_id: str) -> None:
    """NQD-3: Subject/preheader should be explicit (not defaulted)."""
    _DEFAULT_SUBJECT = "ADO Hygiene Report"
    subject = str(fh.get("email_subject_label") or "").strip()
    preheader = str(fh.get("preheader") or "").strip()
    issues: list[str] = []
    if not subject or subject == _DEFAULT_SUBJECT:
        issues.append(
            "email_subject_label missing or equals default 'ADO Hygiene Report'. "
            "Set a program-specific subject in full_hygiene."
        )
    if not preheader:
        issues.append(
            "preheader is missing. Set a descriptive preheader in full_hygiene."
        )
    if issues:
        checks.append(DoctorCheck(
            label="NQD-3 subject/preheader",
            status="warn",
            detail="; ".join(issues),
        ))
    else:
        checks.append(DoctorCheck(
            label="NQD-3 subject/preheader",
            status="ok",
            detail=f"subject={subject!r} and preheader set.",
        ))


def _nqd4_checks(checks: list[DoctorCheck], fh: dict[str, Any], program_id: str) -> None:
    """NQD-4: Deadline health — requires_milestone sections must have a milestone ref."""
    sections = fh.get("sections") or []
    issues: list[str] = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        if not sec.get("requires_milestone"):
            continue
        mid = str(sec.get("deadline_milestone_id") or "").strip()
        if not mid:
            sid = str(sec.get("id") or "?")
            issues.append(
                f"section '{sid}' has requires_milestone=true but no deadline_milestone_id set"
            )
    if issues:
        checks.append(DoctorCheck(
            label="NQD-4 deadline health",
            status="fail" if any("requires_milestone" in i for i in issues) else "warn",
            detail="; ".join(issues),
        ))
    else:
        checks.append(DoctorCheck(
            label="NQD-4 deadline health",
            status="ok",
            detail="All requires_milestone sections have milestone refs.",
        ))


def _nqd5_checks(checks: list[DoctorCheck], fh: dict[str, Any], program_id: str) -> None:
    """NQD-5: Hardcoded explicit deadline that could use a milestone_id instead."""
    import datetime as _dt  # noqa: PLC0415
    sections = fh.get("sections") or []
    stale: list[str] = []
    today = _dt.date.today()
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        dl_raw = sec.get("deadline")
        if not dl_raw:
            continue
        mid = str(sec.get("deadline_milestone_id") or "").strip()
        if mid:
            continue  # has milestone_id too — OK
        try:
            dl = _dt.date.fromisoformat(str(dl_raw).strip())
        except ValueError:
            continue
        # Flag hardcoded dates in the past (likely stale)
        if dl < today:
            sid = str(sec.get("id") or "?")
            stale.append(f"section '{sid}' deadline {dl_raw!r} is past with no deadline_milestone_id")
    if stale:
        checks.append(DoctorCheck(
            label="NQD-5 hardcoded-deadline drift",
            status="warn",
            detail="; ".join(stale) + ". Add deadline_milestone_id or update the date.",
        ))
    else:
        checks.append(DoctorCheck(
            label="NQD-5 hardcoded-deadline drift",
            status="ok",
            detail="No stale hardcoded deadlines detected.",
        ))


def _nqd6_checks(checks: list[DoctorCheck], fh: dict[str, Any], program_id: str) -> None:
    """NQD-6: Waiver governance — expired or missing expires."""
    import datetime as _dt  # noqa: PLC0415
    waivers = fh.get("nudge_waivers") or []
    issues: list[str] = []
    today = _dt.date.today()
    for w in waivers:
        if not isinstance(w, dict):
            continue
        wid = w.get("work_item_id", "?")
        if not w.get("expires"):
            issues.append(f"waiver for item {wid} is missing 'expires'")
            continue
        try:
            exp = _dt.date.fromisoformat(str(w["expires"]).strip())
        except ValueError:
            issues.append(f"waiver for item {wid} has invalid expires date")
            continue
        if exp < today:
            issues.append(
                f"waiver for item {wid} expired {w['expires']} — remove or renew"
            )
    if issues:
        checks.append(DoctorCheck(
            label="NQD-6 waiver governance",
            status="fail",
            detail="; ".join(issues),
        ))
    else:
        if waivers:
            checks.append(DoctorCheck(
                label="NQD-6 waiver governance",
                status="ok",
                detail=f"{len(waivers)} active waiver(s), all with valid expires.",
            ))
        else:
            checks.append(DoctorCheck(
                label="NQD-6 waiver governance",
                status="ok",
                detail="No active waivers.",
            ))


def _nqd8_checks(checks: list[DoctorCheck], fh: dict[str, Any], program_id: str) -> None:
    """NQD-8: Audience policy validation."""
    ap_raw = fh.get("audience_policy")
    if ap_raw is None:
        checks.append(DoctorCheck(
            label="NQD-8 audience policy",
            status="ok",
            detail="No audience_policy block (using defaults — microsoft.com, drop mode).",
        ))
        return
    if not isinstance(ap_raw, dict):
        checks.append(DoctorCheck(
            label="NQD-8 audience policy",
            status="fail",
            detail="audience_policy must be a mapping.",
        ))
        return
    issues: list[str] = []
    domains = ap_raw.get("allowed_domains") or []
    if not domains:
        issues.append("allowed_domains is empty — all recipient domains will be blocked")
    delivery = str(ap_raw.get("delivery_mode") or "to").strip()
    if delivery not in ("to", "bcc"):
        issues.append(f"delivery_mode must be 'to' or 'bcc', got {delivery!r}")
    unresolved = str(ap_raw.get("unresolved_owner") or "drop").strip()
    if unresolved not in ("drop", "fail"):
        issues.append(f"unresolved_owner must be 'drop' or 'fail', got {unresolved!r}")
    opt_out_fallback = str(ap_raw.get("opt_out_fallback") or "escalate").strip()
    if opt_out_fallback not in ("escalate", "gap"):
        issues.append(f"opt_out_fallback must be 'escalate' or 'gap', got {opt_out_fallback!r}")
    if issues:
        checks.append(DoctorCheck(
            label="NQD-8 audience policy",
            status="fail",
            detail="; ".join(issues),
        ))
    else:
        checks.append(DoctorCheck(
            label="NQD-8 audience policy",
            status="ok",
            detail=f"audience_policy valid: domains={domains}, delivery={delivery}.",
        ))


def _nqd10_checks(checks: list[DoctorCheck]) -> None:
    """NQD-10 (ci_contract): All nudge facts must flow through append_nudge_event."""
    import ast as _ast  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415
    repo_root = _Path(__file__).resolve().parents[4]
    nudge_py = repo_root / "src" / "commands" / "nudge.py"
    violations: list[str] = []
    if nudge_py.exists():
        try:
            tree = _ast.parse(nudge_py.read_text(encoding="utf-8"))
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Call):
                    func = node.func
                    name = ""
                    if isinstance(func, _ast.Attribute):
                        name = func.attr
                    elif isinstance(func, _ast.Name):
                        name = func.id
                    if name == "append_program_event":
                        violations.append(f"nudge.py:{node.lineno}: direct append_program_event call")
        except SyntaxError:
            pass
    if violations:
        checks.append(DoctorCheck(
            label="NQD-10 fact single-seam",
            status="fail",
            detail=(
                f"nudge.py calls append_program_event directly ({len(violations)} site(s)): "
                f"{violations[:3]}. Use append_nudge_event() instead."
            ),
        ))
    else:
        checks.append(DoctorCheck(
            label="NQD-10 fact single-seam",
            status="ok",
            detail="No direct append_program_event calls in nudge.py.",
        ))


def _nqd12_checks(checks: list[DoctorCheck]) -> None:
    """NQD-12 (ci_contract): No M365/evidence imports in nudge path — architecture taint."""
    import ast as _ast  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415
    repo_root = _Path(__file__).resolve().parents[4]
    nudge_py = repo_root / "src" / "commands" / "nudge.py"
    nudge_resolution = repo_root / "src" / "core" / "nudge_resolution.py"
    taint_modules = {"src.m365", "src.adapters.m365", "sharepoint", "evidence_store"}
    violations: list[str] = []
    for check_path in (nudge_py, nudge_resolution):
        if not check_path.exists():
            continue
        try:
            tree = _ast.parse(check_path.read_text(encoding="utf-8"))
            for node in _ast.walk(tree):
                if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                    module = ""
                    if isinstance(node, _ast.Import):
                        for alias in node.names:
                            module = alias.name or ""
                    else:
                        module = node.module or ""
                    for taint in taint_modules:
                        if taint in module:
                            violations.append(
                                f"{check_path.name}:{node.lineno}: imports {module!r}"
                            )
        except SyntaxError:
            pass
    if violations:
        checks.append(DoctorCheck(
            label="NQD-12 no evidence in nudge path",
            status="fail",
            detail=(
                f"Nudge path imports evidence/M365 modules ({len(violations)} violation(s)): "
                f"{violations[:3]}. Evidence must not flow into nudge templates."
            ),
        ))
    else:
        checks.append(DoctorCheck(
            label="NQD-12 no evidence in nudge path",
            status="ok",
            detail="No M365/evidence imports detected in nudge path.",
        ))

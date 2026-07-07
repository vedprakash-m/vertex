from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

import typer
import yaml

from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.edition_resolver import EDITIONS_ROOT, PROGRAMS_ROOT, resolve_edition_paths
from src.core.manifest_writer import get_manifest_path
from src.core.overrides_store import REPORTS_ROOT, get_overrides_path, load_overrides
from src.core.snapshot_store import ARCHIVE_ROOT

_STALE_THRESHOLD_HOURS = 48
_QG5_SAFE_SENTENCE_LIMIT = 2  # ≤2 sentences guarantees ≤3 after _apply_scorecard_trend_annotation prepend


def _count_verbosity_violations(overrides_path: Path) -> int:
    """Count scorecard dimension summaries that exceed the safe sentence limit for QG-5."""
    if not overrides_path.exists():
        return 0
    try:
        raw = yaml.safe_load(overrides_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return 0
    scorecards = raw.get("scorecards", {})
    if not isinstance(scorecards, dict):
        return 0
    violations = 0
    for _sc_name, dims in scorecards.items():
        if not isinstance(dims, dict):
            continue
        for _dim_name, dim_data in dims.items():
            if not isinstance(dim_data, dict):
                continue
            summary = (dim_data.get("summary") or "").strip()
            if not summary:
                continue
            sentences = [s for s in re.split(r"(?<=[.!?])\s+", summary) if s]
            if len(sentences) > _QG5_SAFE_SENTENCE_LIMIT:
                violations += 1
    return violations


@dataclass(frozen=True, slots=True)
class NextSuggestion:
    priority: int
    sort_key: str
    command: str
    rationale: str


@dataclass(frozen=True, slots=True)
class GoalStep:
    command: str
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GoalDefinition:
    name: str
    description: str
    steps: tuple[GoalStep, ...]
    success_when: str | None


def suggest_next_steps(
    edition: str,
    *,
    archive_root: Path = ARCHIVE_ROOT,
    reports_root: Path = REPORTS_ROOT,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[NextSuggestion, ...]:
    """Return up to 3 ranked CLI suggestions for the given edition's active issue."""
    resolved_paths = resolve_edition_paths(
        edition,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    program_id: str | None = resolved_paths.program_id if resolved_paths is not None else None
    archive_index = read_archive_index(edition, archive_root=archive_root)
    latest = find_latest_confirmed_entry(archive_index)
    active_issue = (latest.issue_number + 1) if latest is not None else 1

    suggestions: list[NextSuggestion] = []

    manifest_path = get_manifest_path(edition, active_issue, programs_root=programs_root)
    if not manifest_path.exists():
        suggestions.append(NextSuggestion(
            priority=0,
            sort_key=f"{active_issue:06d}",
            command=f"vertex report --edition {edition}",
            rationale=f"No draft generated for issue {active_issue:03d}; run report to produce a draft.",
        ))
    else:
        manifest_raw: dict = json.loads(manifest_path.read_text(encoding="utf-8"))
        ended_at_raw: str | None = manifest_raw.get("ended_at")
        qg_results: dict[str, bool] = manifest_raw.get("qg_results", {})
        freshness: dict[str, int] = manifest_raw.get("freshness_summary", {})

        # Priority 1: stale snapshot (draft older than threshold)
        if ended_at_raw:
            ended_at = datetime.fromisoformat(ended_at_raw)
            if ended_at.tzinfo is None:
                ended_at = ended_at.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - ended_at).total_seconds() / 3600
            if age_hours > _STALE_THRESHOLD_HOURS:
                gather_cmd = (
                    f"vertex gather --program {program_id}"
                    if program_id is not None
                    else f"vertex report --edition {edition}"
                )
                suggestions.append(NextSuggestion(
                    priority=1,
                    sort_key=f"{active_issue:06d}",
                    command=gather_cmd,
                    rationale=(
                        f"Draft for issue {active_issue:03d} is {int(age_hours)}h old "
                        f"(>{_STALE_THRESHOLD_HOURS}h threshold); re-gather to refresh ADO data."
                    ),
                ))

        # Priority 2: Needs-Input gate (QG-8 failing = missing risk levels)
        if not qg_results.get("QG-8", True):
            suggestions.append(NextSuggestion(
                priority=2,
                sort_key=f"{active_issue:06d}",
                command=f"vertex override --edition {edition}",
                rationale=(
                    f"QG-8 failed for issue {active_issue:03d}: "
                    "scorecard dimensions are missing confirmed risk levels."
                ),
            ))

        # Priority 2 (also): QG-5 verbosity — live check bypasses stale manifest QG-5 result
        # (manifest QG-5 may reflect a prior run before overrides were populated with verbose summaries)
        overrides_path_qg5 = get_overrides_path(edition, reports_root, issue_number=active_issue)
        violation_count = _count_verbosity_violations(overrides_path_qg5)
        if violation_count > 0:
            suggestions.append(NextSuggestion(
                priority=2,
                sort_key=f"{active_issue:06d}_qg5",
                command=f"vertex report --edition {edition} --dry-run --offline",
                rationale=(
                    f"QG-5 verbosity violations in overrides for issue {active_issue:03d}: "
                    f"{violation_count} scorecard dimension summary(ies) exceed the "
                    f"{_QG5_SAFE_SENTENCE_LIMIT}-sentence safe limit "
                    f"(QG-5 enforces <=3 sentences; summaries need <={_QG5_SAFE_SENTENCE_LIMIT} to account for "
                    "_apply_scorecard_trend_annotation which may prepend a risk-change sentence). "
                    "QG-5 is never forceable -- blocks confirm even with --force. "
                    f"Shorten scorecard summary: fields in programs/*/overrides/issue_{active_issue:03d}.yaml "
                    f"to <={_QG5_SAFE_SENTENCE_LIMIT} sentences, then re-run this command to verify QG-5 passes."
                ),
            ))

        # Priority 3: missing chapter narratives — highest-value action (+20% score)
        draft_readiness = manifest_raw.get("metadata", {}).get("draft_readiness", {})
        missing_narrative_count = int(draft_readiness.get("missing_narrative_count") or 0)
        total_narrative_count = int(draft_readiness.get("total_narrative_count") or 0)
        if missing_narrative_count > 0:
            score = int(draft_readiness.get("score") or 0)
            gain_pct = round((missing_narrative_count / max(1, total_narrative_count)) * 20)
            suggestions.append(NextSuggestion(
                priority=3,
                sort_key=f"{active_issue:06d}",
                command=f"vertex report --edition {edition} --dry-run --offline",
                rationale=(
                    f"{missing_narrative_count} of {total_narrative_count} chapter narrative(s) are empty stubs "
                    f"for issue {active_issue:03d} (current score: {score}%, worth +{gain_pct}% when written). "
                    f"Write narrative content in programs/*/narratives/issue_{active_issue:03d}/*.md, "
                    "then re-run this command to verify score improvement."
                ),
            ))

        # Priority 4: section reviews pending (QG-3 failing)
        if not qg_results.get("QG-3", True):
            suggestions.append(NextSuggestion(
                priority=4,
                sort_key=f"{active_issue:06d}",
                command=f"vertex review-sections show --edition {edition}",
                rationale=(
                    f"QG-3 failed for issue {active_issue:03d}: sections pending PM approval. "
                    "Run 'vertex review-sections show' to see pending sections, then "
                    "'vertex review-sections set --section <id> --state approved --reviewer <alias>' for each."
                ),
            ))

        # Priority 5: freshness blocks (high-risk items without fresh evidence)
        blocks = freshness.get("blocks", 0)
        if blocks > 0:
            suggestions.append(NextSuggestion(
                priority=5,
                sort_key=f"{active_issue:06d}",
                command=f"vertex freshness --edition {edition}",
                rationale=(
                    f"{blocks} freshness block(s) for issue {active_issue:03d}; "
                    "review work items with stale evidence."
                ),
            ))

    # Priority 6: missing top_3_now in overrides
    overrides = load_overrides(edition, reports_root, issue_number=active_issue)
    if overrides is None or not overrides.top_3_now:
        suggestions.append(NextSuggestion(
            priority=6,
            sort_key=f"{active_issue:06d}",
            command=f"vertex override --edition {edition}",
            rationale=(
                f"top_3_now is empty for issue {active_issue:03d}; "
                "add at least one priority entry to the overrides file."
            ),
        ))

    suggestions.sort(key=lambda s: (s.priority, s.sort_key))

    # Deduplicate by command (keep first occurrence)
    seen: set[str] = set()
    deduped: list[NextSuggestion] = []
    for s in suggestions:
        if s.command not in seen:
            seen.add(s.command)
            deduped.append(s)

    return tuple(deduped[:3])


def next_command(
    edition: str | None = typer.Option(None, "--edition", help="Edition name (e.g. myprogram_weekly)."),
    program: str | None = typer.Option(None, "--program", help="Program id when using --goal without an edition."),
    goal: str | None = typer.Option(None, "--goal", help="Optional static goal name from program.yaml."),
) -> None:
    """Print up to 3 ranked CLI suggestions for the next step on the given edition."""
    if goal is not None:
        goal_definition = load_goal_definition(
            goal,
            edition=edition,
            program_id=program,
            editions_root=EDITIONS_ROOT,
            programs_root=PROGRAMS_ROOT,
        )
        typer.echo(render_goal_definition(goal_definition))
        raise typer.Exit(code=0)

    if edition is None or not edition.strip():
        raise typer.BadParameter("--edition is required unless --goal is provided with --program or --edition.")

    suggestions = suggest_next_steps(
        edition,
        archive_root=ARCHIVE_ROOT,
        reports_root=REPORTS_ROOT,
        editions_root=EDITIONS_ROOT,
        programs_root=PROGRAMS_ROOT,
    )
    if not suggestions:
        typer.echo(
            f"No blocking issues found for {edition}. "
            f"Run: vertex confirm --edition {edition}"
        )
        raise typer.Exit(code=0)
    for i, s in enumerate(suggestions, start=1):
        typer.echo(f"{i}. {s.command}")
        typer.echo(f"   # {s.rationale}")
    raise typer.Exit(code=0)


def load_goal_definition(
    goal_name: str,
    *,
    edition: str | None,
    program_id: str | None,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> GoalDefinition:
    resolved_program_id = _resolve_goal_program_id(
        edition=edition,
        program_id=program_id,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    raw_program = _load_program_document(resolved_program_id, programs_root=programs_root)
    goals = raw_program.get("goals")
    if not isinstance(goals, dict):
        raise typer.BadParameter(
            f"Program '{resolved_program_id}' does not define a goals stanza in program.yaml."
        )

    normalized_goal_name = goal_name.strip()
    raw_goal = goals.get(normalized_goal_name)
    if not isinstance(raw_goal, dict):
        available = ", ".join(sorted(str(name) for name in goals)) or "<none>"
        raise typer.BadParameter(
            f"Goal '{normalized_goal_name}' is not defined for program '{resolved_program_id}'. Available goals: {available}"
        )

    description = str(raw_goal.get("description") or "").strip()
    success_when = str(raw_goal.get("success_when")).strip() if raw_goal.get("success_when") is not None else None
    raw_steps = raw_goal.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise typer.BadParameter(
            f"Goal '{normalized_goal_name}' in program '{resolved_program_id}' must define a non-empty steps list."
        )

    steps: list[GoalStep] = []
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict):
            raise typer.BadParameter(
                f"Goal '{normalized_goal_name}' in program '{resolved_program_id}' contains an invalid step entry."
            )
        command = str(raw_step.get("command") or "").strip()
        if not command:
            raise typer.BadParameter(
                f"Goal '{normalized_goal_name}' in program '{resolved_program_id}' has a step with an empty command."
            )
        raw_args = raw_step.get("args", [])
        if raw_args is None:
            raw_args = []
        if not isinstance(raw_args, list) or any(not isinstance(arg, str) or not arg.strip() for arg in raw_args):
            raise typer.BadParameter(
                f"Goal '{normalized_goal_name}' in program '{resolved_program_id}' step '{command}' must define args as a list of strings."
            )
        steps.append(GoalStep(command=command, args=tuple(raw_args)))

    return GoalDefinition(
        name=normalized_goal_name,
        description=description,
        steps=tuple(steps),
        success_when=success_when,
    )


def render_goal_definition(goal: GoalDefinition) -> str:
    lines = [f"Goal: {goal.name}"]
    if goal.description:
        lines.append(goal.description)
    if goal.success_when:
        lines.extend(("", f"Success when: {goal.success_when}"))
    lines.extend(("", "Steps:"))
    for index, step in enumerate(goal.steps, start=1):
        suffix = " " + " ".join(step.args) if step.args else ""
        lines.append(f"{index}. vertex {step.command}{suffix}")
    return "\n".join(lines)


def _resolve_goal_program_id(
    *,
    edition: str | None,
    program_id: str | None,
    editions_root: Path,
    programs_root: Path,
) -> str:
    normalized_program_id = (program_id or "").strip()
    if normalized_program_id:
        return normalized_program_id
    normalized_edition = (edition or "").strip()
    if normalized_edition:
        resolved = resolve_edition_paths(
            normalized_edition,
            editions_root=editions_root,
            programs_root=programs_root,
        )
        if resolved is None:
            raise typer.BadParameter(f"Edition '{normalized_edition}' was not found.")
        return resolved.program_id
    raise typer.BadParameter("--goal requires --program or --edition.")


def _load_program_document(program_id: str, *, programs_root: Path) -> dict:
    path = programs_root / program_id / "program.yaml"
    if not path.exists():
        raise typer.BadParameter(f"Program '{program_id}' is missing program.yaml.")
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise typer.BadParameter(f"Program '{program_id}' has an invalid program.yaml document.")
    return document

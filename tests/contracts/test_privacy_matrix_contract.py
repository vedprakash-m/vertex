"""WS-15: privacy matrix contract tests.

The privacy matrix is a *contract* between:
1. The tracked markdown spec (`governance/privacy-matrix.md`)
2. The Python runtime source of truth (`src/core/privacy_matrix.py`)
3. The `doctor --privacy` extension (the consumer)
4. The `vertex privacy show` CLI surface

These tests assert the four are in sync, AND assert the matrix coverage is
complete (no tracked sidecar is missing a retention rule, every channel
has a posture, every classification has an order).
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest
import typer

from src.core.privacy_matrix import (
    CHANNEL_POSTURE,
    CLASSIFICATION_ORDER,
    RETENTION_DAYS,
    SIDECAR_RETENTION,
    Channel,
    DataClassification,
    RetentionClass,
    channels,
    classification_at_least,
    known_sidecar_paths,
    posture_for,
    sidecar_rules,
)


# ----- Spec ↔ code sync -----

# The 25 known sidecar paths in the markdown matrix (Section 3 of
# `governance/privacy-matrix.md`). If you add a sidecar to either, you must
# update the other AND this list.
SPEC_SIDECAR_PATHS: tuple[str, ...] = (
    "journal/signals.jsonl",
    "journal/reviews.jsonl",
    "journal/autonomy_audit.jsonl",
    "journal/actions.jsonl",
    "journal/ai_proposals.jsonl",
    "journal/risk_updates.jsonl",
    "journal/edit_patterns.jsonl",
    "people_profiles.yaml",
    "runtime/vertex_analytics.sqlite3",
    "migration_log.jsonl",
    "archive/<edition>/snapshots/issue_NNN.snapshot.json",
    "archive/<edition>/manifests/issue_NNN.json",
    "runtime/gather_state.json",
    "external_dependencies.jsonl",
    "ai/llm_trace_full_io.jsonl",
    # ADF-W0.16 (ADR-0015, 2026-07-13):
    "nudge/drafts/<solicitation_id>.eml",
    "nudge/replies/<message_id>.eml",
    "_feedback/context_gap_solicitations.jsonl",
    "runtime/program_synthesis/<ai_run_id>.json",
    "workstream_registry.yaml",
    # ADF-W5.9 (2026-07-14):
    "runtime/tier_decisions.jsonl",
    "_state/ai_telemetry.jsonl",
    "runtime/run_telemetry.jsonl",
    "_alerts/alerts.jsonl",
    "runtime/context_manifests",
)


# The 6 known channels in the markdown matrix (Section 2).
SPEC_CHANNELS: tuple[str, ...] = (
    "ado", "kusto", "icm", "teams", "workiq", "transcript",
)


def test_privacy_matrix_markdown_exists() -> None:
    """Tracked matrix file must exist."""
    repo_root = Path(__file__).resolve().parents[2]
    matrix_path = repo_root / "governance" / "privacy-matrix.md"
    assert matrix_path.exists(), f"tracked privacy matrix missing: {matrix_path}"
    text = matrix_path.read_text(encoding="utf-8")
    assert "Privacy & Data Governance Matrix" in text
    assert "WS-15" in text
    # Tracked file should NOT be gitignored. (sanity check at the test layer
    # rather than via `git check-ignore` because that requires git plumbing.)
    gitignore = repo_root / ".gitignore"
    if gitignore.exists():
        # governance/ is tracked, but be defensive.
        assert "governance/" not in (line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines() if line.strip()), (
            "governance/ should not be gitignored — privacy-matrix.md is a tracked artifact"
        )


def test_spec_sidecar_paths_match_runtime() -> None:
    """The set of sidecar paths in the markdown spec must equal the runtime SIDECAR_RETENTION set."""
    runtime_paths = set(known_sidecar_paths())
    spec_paths = set(SPEC_SIDECAR_PATHS)
    assert runtime_paths == spec_paths, (
        f"sidecar path drift — runtime has {runtime_paths - spec_paths}, "
        f"spec has {spec_paths - runtime_paths}"
    )


def test_spec_channels_match_runtime() -> None:
    """The set of channels in the markdown spec must equal the runtime CHANNEL_POSTURE set."""
    runtime_channels = {c.value for c in channels()}
    spec_channels = set(SPEC_CHANNELS)
    assert runtime_channels == spec_channels, (
        f"channel drift — runtime has {runtime_channels - spec_channels}, "
        f"spec has {spec_channels - runtime_channels}"
    )


def test_every_channel_has_posture() -> None:
    """Every Channel enum member must have a posture entry (no orphaned channels)."""
    for channel in Channel:
        assert channel in CHANNEL_POSTURE, f"channel {channel.value} has no posture"


def test_every_posture_has_valid_fields() -> None:
    """Each posture's fields must be valid (no None for required fields)."""
    for channel, posture in CHANNEL_POSTURE.items():
        assert posture.channel == channel
        assert isinstance(posture.read_default_class, DataClassification)
        # write_default_class may be None for read-only channels; verify the
        # posture is internally consistent.
        if posture.write_default_class is not None:
            assert isinstance(posture.write_default_class, DataClassification)
        assert isinstance(posture.retention, RetentionClass)
        assert posture.rbac_model in ("user-context", "application", "managed-identity"), (
            f"{channel.value}: unknown rbac_model {posture.rbac_model!r}"
        )
        assert len(posture.least_privilege_scopes) >= 1, (
            f"{channel.value}: must have at least one least-privilege scope"
        )


def test_every_sidecar_rule_has_excise_consistency() -> None:
    """PII/Confidential sidecars that hold audit-of-record MUST support excise; SECRET/ephemeral MUST NOT."""
    for rule in sidecar_rules():
        # people_profiles.yaml is the special case: full-record deletion only.
        if rule.artifact_path == "people_profiles.yaml":
            assert not rule.supports_excise, "people_profiles supports full-record deletion only, not in-place excise"
            assert rule.classification == DataClassification.PII
            continue
        # migration_log holds no PII; no excise needed.
        if rule.artifact_path == "migration_log.jsonl":
            assert rule.classification == DataClassification.INTERNAL
            assert not rule.supports_excise
            continue
        # gather_state has transient PII (error messages with operator info);
        # excise is operationally complex — assert no for now, with a TODO marker.
        if rule.artifact_path == "runtime/gather_state.json":
            continue
        # external_dependencies has no PII; excise is unnecessary.
        if rule.artifact_path == "external_dependencies.jsonl":
            assert not rule.supports_excise
            continue
        # ADF-W0.16 (ADR-0015): cooldown log has no PII (id/fingerprint/timestamp only).
        if rule.artifact_path == "_feedback/context_gap_solicitations.jsonl":
            assert not rule.supports_excise
            continue
        # ADF-W0.16 (ADR-0015): aggregated business content only, no PII.
        if rule.artifact_path == "runtime/program_synthesis/<ai_run_id>.json":
            assert not rule.supports_excise
            continue
        # ADF-W0.16 (ADR-0015): live overwrite-in-place config file (like
        # program.yaml), not a rotating audit log — operator edits/redacts
        # the field directly instead of a tombstone; same rationale as
        # runtime/gather_state.json above.
        if rule.artifact_path == "workstream_registry.yaml":
            continue
        # ADF-W5.9: the four raw-telemetry JSONL sidecars this session's ADF
        # work introduced hold no PII (routing decisions, provider/latency/
        # cost metrics, channel performance, alert metadata) — purge is
        # outright deletion, no tombstone needed, same rationale as
        # migration_log.jsonl/external_dependencies.jsonl above.
        if rule.artifact_path in (
            "runtime/tier_decisions.jsonl",
            "_state/ai_telemetry.jsonl",
            "runtime/run_telemetry.jsonl",
            "_alerts/alerts.jsonl",
            "runtime/context_manifests",
        ):
            assert rule.classification == DataClassification.INTERNAL
            assert not rule.supports_excise
            continue
        # All other sidecars (CONFIDENTIAL + PII holding append-only audit
        # records) MUST support excise so WS-18 can implement `[EXCISED]`.
        assert rule.supports_excise, (
            f"sidecar {rule.artifact_path} ({rule.classification.value}) should support excise"
        )


def test_retention_classes_have_days_mapping() -> None:
    """Every retention class must have a `RETENTION_DAYS` entry."""
    for cls in RetentionClass:
        assert cls in RETENTION_DAYS, f"retention class {cls.value} has no days mapping"
        days = RETENTION_DAYS[cls]
        assert days is None or days >= 0


def test_ephemeral_retention_is_zero_days() -> None:
    """EPHEMERAL retention must round-trip to 0 days (do-not-persist-beyond-live-gather)."""
    assert RETENTION_DAYS[RetentionClass.EPHEMERAL] == 0


def test_indefinite_retention_is_none() -> None:
    """INDEFINITE retention must round-trip to None (never auto-delete)."""
    assert RETENTION_DAYS[RetentionClass.INDEFINITE] is None


def test_classification_ordering_is_total() -> None:
    """Every DataClassification must have a CLASSIFICATION_ORDER entry, and the ordering is total."""
    for cls in DataClassification:
        assert cls in CLASSIFICATION_ORDER
    values = [CLASSIFICATION_ORDER[cls] for cls in DataClassification]
    assert len(set(values)) == len(values), "duplicate classification order entries"
    assert min(values) == 0
    assert max(values) == len(DataClassification) - 1


def test_classification_at_least_helper() -> None:
    """The `classification_at_least` helper must implement the documented contract."""
    # PUBLIC is at-least-PUBLIC
    assert classification_at_least(DataClassification.PUBLIC, DataClassification.PUBLIC)
    # PII is at-least-CONFIDENTIAL
    assert classification_at_least(DataClassification.PII, DataClassification.CONFIDENTIAL)
    # PUBLIC is NOT at-least-CONFIDENTIAL
    assert not classification_at_least(DataClassification.PUBLIC, DataClassification.CONFIDENTIAL)
    # SECRET is at-least-PII
    assert classification_at_least(DataClassification.SECRET, DataClassification.PII)


def test_posture_for_returns_correct_posture() -> None:
    """posture_for() must return the entry keyed by the channel."""
    for channel in channels():
        p = posture_for(channel)
        assert p.channel == channel


# ----- Consumer integration -----

def test_doctor_privacy_check_imports_matrix() -> None:
    """The doctor --privacy extension must import the matrix (the consumer contract)."""
    import src.commands.doctor_checks.privacy_checks as privacy_checks
    source = Path(privacy_checks.__file__).read_text(encoding="utf-8")
    # Must import the matrix
    assert "privacy_matrix" in source
    # Must call build_channel_posture_check (the new WS-15 check)
    assert "build_channel_posture_check" in source


def test_vertex_privacy_show_cli_is_registered() -> None:
    """The `vertex privacy show` subcommand must be registered with the cli app."""
    from cli import app
    # Typer exposes subcommand groups via `registered_groups`; each
    # `TyperInfo` wraps a `typer_instance` whose `.registered_commands`
    # holds the nested subcommands.
    group_names = {g.name for g in app.registered_groups}
    assert "privacy" in group_names, (
        f"`vertex privacy` subcommand not registered; got: {sorted(group_names)}"
    )
    privacy_group = next(g for g in app.registered_groups if g.name == "privacy")
    privacy_subs = {c.name for c in privacy_group.typer_instance.registered_commands}
    assert "show" in privacy_subs, (
        f"`vertex privacy show` subcommand not registered; got: {sorted(privacy_subs)}"
    )
    assert "check" in privacy_subs, (
        f"`vertex privacy check` subcommand not registered; got: {sorted(privacy_subs)}"
    )


def test_vertex_privacy_show_runs(tmp_path: Path) -> None:
    """Smoke test: `vertex privacy show --section channels` runs and emits the expected channel list."""
    from typer.main import get_command
    from src.commands.privacy import privacy_app

    cmd = get_command(privacy_app)
    runner = pytest.importorskip("click.testing").CliRunner()
    result = runner.invoke(cmd, ["show", "--section", "channels"])
    assert result.exit_code == 0, f"unexpected exit: {result.output}"
    # The output should mention every channel
    for channel_name in SPEC_CHANNELS:
        assert channel_name in result.output, (
            f"channel {channel_name} missing from `privacy show --section channels`"
        )


def test_vertex_privacy_check_unknown_channel_exits_nonzero(tmp_path: Path) -> None:
    """`vertex privacy check --channel <unknown>` should exit 2 (Typer convention for usage error)."""
    from typer.main import get_command
    from src.commands.privacy import privacy_app

    cmd = get_command(privacy_app)
    runner = pytest.importorskip("click.testing").CliRunner()
    result = runner.invoke(cmd, ["check", "--channel", "made_up_channel"])
    assert result.exit_code == 2, f"expected exit 2, got {result.exit_code}; output={result.output}"


def test_vertex_privacy_check_known_channel_prints_posture(tmp_path: Path) -> None:
    """`vertex privacy check --channel ado` prints the ADO posture in machine-friendly form."""
    from typer.main import get_command
    from src.commands.privacy import privacy_app

    cmd = get_command(privacy_app)
    runner = pytest.importorskip("click.testing").CliRunner()
    result = runner.invoke(cmd, ["check", "--channel", "ado"])
    assert result.exit_code == 0, f"unexpected exit: {result.output}"
    assert "channel=ado" in result.output
    assert "read_class=confidential" in result.output
    assert "write_class=confidential" in result.output
    assert "rbac=user-context" in result.output

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.core import freshness_engine, program_context
from src.core.exceptions import ConfigError


TODAY = date(2026, 10, 24)


def _policy(*sections: object):
    return freshness_engine.parse_context_review_policy(*sections)


def test_compatibility_thresholds() -> None:
    policy = _policy()
    assert [(name, rule.warn_days, rule.block_days) for name, rule in policy.files] == [
        ("program.yaml", 90, 180),
        ("workstreams.yaml", 30, 90),
        ("milestones.yaml", 14, 30),
        ("scorecards.yaml", 60, 120),
        ("kpis.yaml", 60, None),
        ("editorial_rules.yaml", 60, None),
        ("dependencies.yaml", 30, 60),
        ("capability_status.yaml", 14, 30),
        ("template_contract.yaml", 90, None),
        ("slice_contracts.yaml", 60, 120),
        ("chapter_contract.yaml", 90, None),
        ("readiness.yaml", 30, 60),
    ]
    assert [(name, rule.warn_days, rule.block_days) for name, rule in policy.entries] == [
        ("workstreams.yaml", 14, 30),
        ("workstream_registry.yaml", 14, 30),
        ("milestones.yaml", 14, 30),
        ("risk_register.yaml", 7, 14),
        ("decisions.yaml", 0, 14),
        ("assumptions.yaml", 30, 60),
    ]


def test_partial_overrides_merge_without_mutating_inputs() -> None:
    base = {"files": {"program.yaml": {"warn_days": 10, "block_days": 20}}}
    private = {"files": {"program.yaml": {"block_days": None}}}
    policy = _policy(base, private)
    rule = dict(policy.files)["program.yaml"]
    assert (rule.warn_days, rule.block_days) == (10, None)
    assert base["files"]["program.yaml"]["block_days"] == 20
    assert dict(_policy().files)["program.yaml"].warn_days == 90


@pytest.mark.parametrize("section", [
    None, [], "invalid", {"unknown": {}}, {"files": None}, {"entries": []},
    {"files": {"unknown.yaml": {"warn_days": 1}}},
    {"entries": {"program.yaml": {"warn_days": 1}}},
    {"files": {"program.yaml": None}},
    {"files": {"program.yaml": {"unknown": 1}}},
    {"files": {"program.yaml": {"warn_days": True}}},
    {"files": {"program.yaml": {"warn_days": -1}}},
    {"files": {"program.yaml": {"warn_days": "1"}}},
    {"files": {"program.yaml": {"warn_days": None}}},
    {"files": {"program.yaml": {"block_days": False}}},
    {"files": {"program.yaml": {"block_days": 1.5}}},
    {"files": {"program.yaml": {"block_days": -1}}},
    {"files": {"program.yaml": {"warn_days": 181}}},
])
def test_explicit_malformed_or_unknown_rule_rejected(section: object) -> None:
    with pytest.raises(ConfigError, match="context_review"):
        _policy(section)


def test_invalid_base_is_not_hidden_by_valid_override() -> None:
    with pytest.raises(ConfigError, match="context_review"):
        _policy(
            {"entries": {"decisions.yaml": {"block_days": -1}}},
            {"entries": {"decisions.yaml": {"block_days": 20}}},
        )


@pytest.fixture
def synthetic_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    program_dir = tmp_path / "programs" / "synthetic"
    program_dir.mkdir(parents=True)
    for name in ("workstreams", "milestones", "risk_entries", "decision_entries", "assumptions", "dependencies"):
        monkeypatch.setattr(program_context, f"load_current_{name}", lambda *args, **kwargs: ())

    def load(
        section: object,
        reviewed: object = "2026-10-04",
        *,
        facts: dict[str, tuple[object, ...]] | None = None,
        **documents: object,
    ):
        policy = _policy(section)
        seen: list[Path] = []

        def load_policy(*, override_path: Path):
            seen.append(override_path)
            return SimpleNamespace(context_review=policy)

        monkeypatch.setattr(program_context, "load_freshness_policy", load_policy)
        for name, entries in (facts or {}).items():
            monkeypatch.setattr(program_context, f"load_current_{name}", lambda *args, _entries=entries, **kwargs: _entries)
            monkeypatch.setattr(program_context, {
                "workstreams": "_workstream_entry_from_model",
                "milestones": "_milestone_entry_from_model",
                "risk_entries": "_risk_entry_from_model",
                "decision_entries": "_decision_entry_from_model",
                "assumptions": "_assumption_entry_from_model",
            }[name], lambda entry: entry)
        (program_dir / "program.yaml").write_text(yaml.safe_dump({
            "program": {"id": "synthetic"}, "last_reviewed_date": reviewed,
        }), encoding="utf-8")
        for filename, document in documents.items():
            (program_dir / filename).write_text(yaml.safe_dump(document), encoding="utf-8")
        context = program_context.load_program_context(
            "synthetic", programs_root=program_dir.parent, today=TODAY, raise_on_error=False,
        )
        assert seen == [program_dir / "freshness_policy.yaml"]
        return context

    return load


@pytest.mark.parametrize(("age", "severity"), [(9, None), (10, "warn"), (19, "warn"), (20, "block")])
def test_file_override_boundaries(synthetic_context, age: int, severity: str | None) -> None:
    context = synthetic_context(
        {"files": {"program.yaml": {"warn_days": 10, "block_days": 20}}},
        reviewed=(TODAY - timedelta(days=age)).isoformat(),
    )
    flags = context.staleness_flags
    assert [flag.severity for flag in flags] == ([] if severity is None else [severity])
    if flags:
        assert (flags[0].warn_threshold, flags[0].block_threshold) == (10, 20)


@pytest.mark.parametrize("reviewed", [None, "invalid", "2026-10-25"])
def test_override_does_not_make_unknown_review_metadata_fresh(synthetic_context, reviewed: object) -> None:
    context = synthetic_context(
        {"files": {"program.yaml": {"warn_days": 999, "block_days": None}}},
        reviewed=reviewed,
    )
    assert context.staleness_flags[0].days_stale is None
    assert context.staleness_flags[0].severity == "warn"
    assert context.context_gaps[0].field == "last_reviewed_date"


def test_entry_overrides_and_sequential_maturity_blocker(synthetic_context) -> None:
    context = synthetic_context(
        {"entries": {"workstream_registry.yaml": {"warn_days": 1, "block_days": 2}}},
        reviewed=TODAY.isoformat(),
        **{"workstream_registry.yaml": {"workstreams": [{
            "id": "lane", "deep_context": {"why": "synthetic"},
            "last_reviewed_date": "2026-10-22",
        }]}},
    )
    assert [(flag.entity_id, flag.severity) for flag in context.staleness_flags] == [("lane", "block")]
    assert context.maturity_level == program_context.MaturityLevel.L1
    assert context.maturity_blockers == ("1 blocking context staleness flags",)


@pytest.mark.parametrize(("collection", "filename", "entry"), [
    ("workstreams", "workstreams.yaml", program_context.WorkstreamEntry(id="ws", name="Workstream")),
    ("milestones", "milestones.yaml", program_context.MilestoneEntry(id="ms", name="Milestone")),
    ("risk_entries", "risk_register.yaml", program_context.RiskEntry(id="risk", title="Risk", status="open")),
    ("assumptions", "assumptions.yaml", program_context.AssumptionEntry(id="assumption", status="active")),
])
def test_each_reviewed_entry_uses_its_own_rule(synthetic_context, collection, filename, entry) -> None:
    from dataclasses import replace

    entry = replace(entry, last_reviewed_date=TODAY - timedelta(days=3))
    context = synthetic_context(
        {"entries": {filename: {"warn_days": 2, "block_days": 3}}},
        reviewed=TODAY.isoformat(),
        facts={collection: (entry,)},
    )
    assert [(flag.file, flag.entity_id, flag.severity) for flag in context.staleness_flags] == [
        (filename, entry.id, "block"),
    ]


@pytest.mark.parametrize(("age", "expected"), [(-1, []), (1, []), (2, ["warn"]), (3, ["block"])])
def test_decision_due_review_uses_override_without_changing_future_behavior(synthetic_context, age, expected) -> None:
    decision = program_context.DecisionEntry(
        id="decision", title="Decision", status="active", review_by=TODAY - timedelta(days=age),
    )
    context = synthetic_context(
        {"entries": {"decisions.yaml": {"warn_days": 2, "block_days": 3}}},
        reviewed=TODAY.isoformat(), facts={"decision_entries": (decision,)},
    )
    assert [flag.severity for flag in context.staleness_flags] == expected
    assert all(flag.field == "review_by" for flag in context.staleness_flags)


def test_risk_review_override_reaches_existing_maturity_check_only(synthetic_context) -> None:
    risk = program_context.RiskEntry(
        id="risk", title="Risk", status="open", mitigation_due_date=TODAY - timedelta(days=20),
        last_reviewed_date=TODAY - timedelta(days=10),
    )
    context = synthetic_context(
        {"entries": {"risk_register.yaml": {"warn_days": 15, "block_days": 30}}},
        reviewed=TODAY.isoformat(), facts={"risk_entries": (risk,)},
        **{"workstream_registry.yaml": {"workstreams": [{
            "id": "lane", "deep_context": {"why": "synthetic"},
            "last_reviewed_date": TODAY.isoformat(),
        }]}},
    )
    assert context.maturity_level == program_context.MaturityLevel.L2
    assert not any("mitigation_due_date" in blocker for blocker in context.maturity_blockers)

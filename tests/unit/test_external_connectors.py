"""Tests for FR-SG-48: External connector framework."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.core.connector_config import ExternalConnectorConfig
from src.core.external_connector import (
    ExternalConnector,
    make_connector,
)
from src.core.external_dependency import ExternalDependency, load_external_dependencies
from src.core.connectors import CONNECTOR_REGISTRY
from src.core.connectors.github_issues import GitHubIssuesConnector, _parse_issue_ref
from src.core.connectors.sharepoint_lists import SharePointListsConnector
from src.core.connector_polling import poll_and_save_external_connectors
from src.core.slice_contract_loader import (
    load_external_connector_configs,
    _parse_external_connector_config,
)
from src.core.exceptions import ConfigError


# ---------------------------------------------------------------------------
# ExternalConnectorConfig
# ---------------------------------------------------------------------------


def _make_config(**kwargs: Any) -> ExternalConnectorConfig:
    defaults = {
        "dep_id": "test-dep",
        "connector_type": "github_issues",
        "source_url": "https://github.com/owner/repo/issues/42",
        "team": "Test Team",
    }
    defaults.update(kwargs)
    return ExternalConnectorConfig(**defaults)


def test_connector_config_defaults():
    cfg = _make_config()
    assert cfg.dep_id == "test-dep"
    assert cfg.gates == ()
    assert cfg.auth_token is None


def test_connector_config_frozen():
    cfg = _make_config()
    with pytest.raises((AttributeError, TypeError)):
        cfg.dep_id = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CONNECTOR_REGISTRY and make_connector
# ---------------------------------------------------------------------------


def test_registry_contains_expected_types():
    assert "github_issues" in CONNECTOR_REGISTRY
    assert "sharepoint_lists" in CONNECTOR_REGISTRY


def test_make_connector_github():
    cfg = _make_config(connector_type="github_issues")
    conn = make_connector(cfg)
    assert isinstance(conn, GitHubIssuesConnector)


def test_make_connector_sharepoint():
    cfg = _make_config(connector_type="sharepoint_lists")
    conn = make_connector(cfg)
    assert isinstance(conn, SharePointListsConnector)


def test_make_connector_unknown_type():
    cfg = _make_config(connector_type="jira")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unknown connector type"):
        make_connector(cfg)


# ---------------------------------------------------------------------------
# _parse_issue_ref
# ---------------------------------------------------------------------------


def test_parse_issue_ref_standard():
    owner, repo, number = _parse_issue_ref("https://github.com/microsoft/vscode/issues/12345")
    assert owner == "microsoft"
    assert repo == "vscode"
    assert number == 12345


def test_parse_issue_ref_trailing_slash():
    owner, repo, number = _parse_issue_ref("https://github.com/acme/widget/issues/7/")
    assert owner == "acme"
    assert repo == "widget"
    assert number == 7


def test_parse_issue_ref_invalid_url():
    with pytest.raises(ValueError, match="Cannot parse GitHub issue URL"):
        _parse_issue_ref("https://github.com/owner/repo/pulls/3")


# ---------------------------------------------------------------------------
# GitHubIssuesConnector.poll (mocked)
# ---------------------------------------------------------------------------


_MOCK_ISSUE = {
    "number": 42,
    "title": "Fix platform dep",
    "state": "open",
    "state_reason": "reopened",
}


def _mock_request(self: Any, url: str) -> dict:
    return _MOCK_ISSUE


def test_github_connector_poll_returns_external_dependency():
    cfg = _make_config(
        dep_id="gh-42",
        team="Platform",
        gates=("platform.delivery",),
    )
    conn = GitHubIssuesConnector(cfg)
    with patch.object(GitHubIssuesConnector, "_request", _mock_request):
        dep = conn.poll()

    assert dep.dep_id == "gh-42"
    assert dep.team == "Platform"
    assert dep.tracked_items == (42,)
    # WS-2 PB-8: connector now records approval_type="github" and surfaces
    # the upstream state (open here).
    assert dep.approval_type == "github"
    assert dep.state == "open"
    assert dep.is_fulfilled is False
    assert dep.source_ref == "owner/repo#42"
    assert dep.gates == ("platform.delivery",)
    assert dep.last_seen is not None
    assert dep.last_seen.tzinfo is not None  # UTC-aware


def test_github_connector_health_check_true():
    cfg = _make_config()
    conn = GitHubIssuesConnector(cfg)
    with patch.object(GitHubIssuesConnector, "_request", _mock_request):
        assert conn.health_check() is True


def test_github_connector_health_check_false():
    import urllib.error

    cfg = _make_config()
    conn = GitHubIssuesConnector(cfg)

    def _fail(*args: Any, **kwargs: Any) -> dict:
        raise urllib.error.URLError("timeout")

    with patch.object(GitHubIssuesConnector, "_request", _fail):
        assert conn.health_check() is False


# ---------------------------------------------------------------------------
# SharePointListsConnector — raises NotImplementedError
# ---------------------------------------------------------------------------


def test_sharepoint_connector_poll_raises_when_unconfigured():
    """WS-2 PB-7: an unconfigured SharePoint connector (no auth_token)
    raises NotImplementedError on poll(). The configured path is exercised
    via a patched `_fetch_list_items()` in the cassette-style test below.
    """
    cfg = _make_config(connector_type="sharepoint_lists")
    conn = SharePointListsConnector(cfg)
    with pytest.raises(NotImplementedError, match="SharePoint Lists connector requires operator"):
        conn.poll()


def test_sharepoint_connector_health_check_false_when_unconfigured():
    """WS-2 PB-7: the unconfigured health check returns False (it does not
    raise) so callers can treat it as a soft-fail rather than a crash.
    """
    cfg = _make_config(connector_type="sharepoint_lists")
    conn = SharePointListsConnector(cfg)
    assert conn.health_check() is False


def test_sharepoint_connector_poll_with_cassette(tmp_path: Path) -> None:
    """WS-2 PB-7: with a configured connector and a patched
    `_fetch_list_items` returning a recorded list, `poll()` returns a
    typed `state="fulfilled"` dependency with `approval_type="sharepoint"`.

    The cassette surface is the `_fetch_list_items` patch — there is no
    real network call. The test pins the dep shape that the QG-26 gate
    will read.
    """
    cfg = _make_config(
        connector_type="sharepoint_lists",
        dep_id="sp-cassette-1",
        team="Cassette Team",
        auth_token='{"tenant_id": "t", "site_id": "s", "list_id": "l"}',
    )
    conn = SharePointListsConnector(cfg)
    cassette = [
        {"Id": 7, "Status": "Completed", "Title": "Build dep"},
    ]

    def _patched_fetch(self):  # type: ignore[no-untyped-def]
        return cassette

    with patch.object(SharePointListsConnector, "_fetch_list_items", _patched_fetch):
        dep = conn.poll()

    assert dep.dep_id == "sp-cassette-1"
    assert dep.approval_type == "sharepoint"
    assert dep.state == "fulfilled"
    assert dep.is_fulfilled is True
    assert dep.tracked_items == (7,)
    assert dep.source_ref and "sharepoint" in dep.source_ref


def test_sharepoint_connector_poll_open_state() -> None:
    """WS-2 PB-7: an open list item maps to `state="open"` so a critical
    dep blocks confirm (QG-26 surface).
    """
    cfg = _make_config(
        connector_type="sharepoint_lists",
        dep_id="sp-open-1",
        team="Open Team",
        auth_token='{"tenant_id": "t", "site_id": "s", "list_id": "l"}',
    )
    conn = SharePointListsConnector(cfg)
    cassette = [{"Id": 1, "Status": "Active"}]

    def _patched_fetch(self):  # type: ignore[no-untyped-def]
        return cassette

    with patch.object(SharePointListsConnector, "_fetch_list_items", _patched_fetch):
        dep = conn.poll()

    assert dep.state == "open"
    assert dep.is_fulfilled is False


# ---------------------------------------------------------------------------
# poll_and_save_external_connectors
# ---------------------------------------------------------------------------


def test_poll_and_save_writes_dependency(tmp_path: Path):
    cfg = _make_config(dep_id="test-write", team="Team A", gates=("gate.a",))
    dep = ExternalDependency(
        dep_id="test-write",
        team="Team A",
        tracked_items=(42,),
        approval_type="manual",
        gates=("gate.a",),
        canonical_owner_program=None,
        last_seen=datetime.now(timezone.utc),
    )

    class _FakeConnector(ExternalConnector):
        def poll(self) -> ExternalDependency:
            return dep

        def health_check(self) -> bool:
            return True

    with patch("src.core.connector_polling.make_connector", return_value=_FakeConnector(cfg)):
        results = poll_and_save_external_connectors("prog1", (cfg,), programs_root=tmp_path)

    assert len(results) == 1
    saved = load_external_dependencies("prog1", programs_root=tmp_path)
    assert len(saved) == 1
    assert saved[0].dep_id == "test-write"


def test_poll_and_save_skips_not_implemented(tmp_path: Path):
    cfg = _make_config(connector_type="sharepoint_lists")
    results = poll_and_save_external_connectors("prog1", (cfg,), programs_root=tmp_path)
    assert results == []


def test_load_external_dependencies_rejects_numeric_string_tracked_items(tmp_path: Path) -> None:
    path = tmp_path / "prog1" / "external_dependencies.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"dep_id":"test-dep","team":"Team A","tracked_items":["42"],"approval_type":"manual","gates":["gate.a"],"canonical_owner_program":null,"last_seen":"2026-06-07T12:00:00+00:00"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="tracked_items must contain integers only"):
        load_external_dependencies("prog1", programs_root=tmp_path)


def test_load_external_dependencies_rejects_non_list_tracked_items(tmp_path: Path) -> None:
    path = tmp_path / "prog1" / "external_dependencies.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"dep_id":"test-dep","team":"Team A","tracked_items":"42","approval_type":"manual","gates":["gate.a"],"canonical_owner_program":null,"last_seen":"2026-06-07T12:00:00+00:00"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="tracked_items must be a list of integers"):
        load_external_dependencies("prog1", programs_root=tmp_path)


def test_load_external_dependencies_rejects_non_string_dep_id(tmp_path: Path) -> None:
    path = tmp_path / "prog1" / "external_dependencies.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"dep_id":42,"team":"Team A","tracked_items":[42],"approval_type":"manual","gates":["gate.a"],"canonical_owner_program":null,"last_seen":"2026-06-07T12:00:00+00:00"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="dep_id must be a string"):
        load_external_dependencies("prog1", programs_root=tmp_path)


def test_load_external_dependencies_rejects_non_string_last_seen(tmp_path: Path) -> None:
    path = tmp_path / "prog1" / "external_dependencies.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"dep_id":"test-dep","team":"Team A","tracked_items":[42],"approval_type":"manual","gates":["gate.a"],"canonical_owner_program":null,"last_seen":123}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="last_seen must be a string"):
        load_external_dependencies("prog1", programs_root=tmp_path)


def test_load_external_dependencies_rejects_non_string_canonical_owner_program(tmp_path: Path) -> None:
    path = tmp_path / "prog1" / "external_dependencies.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"dep_id":"test-dep","team":"Team A","tracked_items":[42],"approval_type":"manual","gates":["gate.a"],"canonical_owner_program":99,"last_seen":"2026-06-07T12:00:00+00:00"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="canonical_owner_program must be a string"):
        load_external_dependencies("prog1", programs_root=tmp_path)


def test_load_external_dependencies_rejects_non_object_rows(tmp_path: Path) -> None:
    path = tmp_path / "prog1" / "external_dependencies.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('["not","an","object"]\n', encoding="utf-8")

    with pytest.raises(TypeError, match="external dependency rows must be JSON objects"):
        load_external_dependencies("prog1", programs_root=tmp_path)


def test_load_external_dependencies_rejects_unknown_approval_type(tmp_path: Path) -> None:
    path = tmp_path / "prog1" / "external_dependencies.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"dep_id":"test-dep","team":"Team A","tracked_items":[42],"approval_type":"email","gates":["gate.a"],"canonical_owner_program":null,"last_seen":"2026-06-07T12:00:00+00:00"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported approval_type 'email'"):
        load_external_dependencies("prog1", programs_root=tmp_path)


def test_load_external_dependencies_rejects_non_string_gates(tmp_path: Path) -> None:
    path = tmp_path / "prog1" / "external_dependencies.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"dep_id":"test-dep","team":"Team A","tracked_items":[42],"approval_type":"manual","gates":[1],"canonical_owner_program":null,"last_seen":"2026-06-07T12:00:00+00:00"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="gates must contain strings only"):
        load_external_dependencies("prog1", programs_root=tmp_path)


def test_poll_and_save_logs_and_continues_on_error(tmp_path: Path):
    cfg = _make_config()

    class _ErrorConnector(ExternalConnector):
        def poll(self) -> ExternalDependency:
            raise RuntimeError("boom")

        def health_check(self) -> bool:
            return False

    with patch("src.core.connector_polling.make_connector", return_value=_ErrorConnector(cfg)):
        results = poll_and_save_external_connectors("prog1", (cfg,), programs_root=tmp_path)

    assert results == []


# ---------------------------------------------------------------------------
# load_external_connector_configs
# ---------------------------------------------------------------------------


def _write_contracts(path: Path, connectors: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import yaml  # type: ignore[import]

    doc = {"schema_version": "1.0", "slices": [], "external_connectors": connectors}
    path.write_text(yaml.dump(doc), encoding="utf-8")


def test_load_external_connector_configs_empty(tmp_path: Path):
    p = tmp_path / "slice_contracts.yaml"
    _write_contracts(p, [])
    assert load_external_connector_configs(p) == ()


def test_load_external_connector_configs_nonexistent_file(tmp_path: Path):
    p = tmp_path / "slice_contracts.yaml"
    assert load_external_connector_configs(p) == ()


def test_load_external_connector_configs_parses_github(tmp_path: Path):
    p = tmp_path / "slice_contracts.yaml"
    _write_contracts(
        p,
        [
            {
                "dep_id": "gh-ext-1",
                "connector_type": "github_issues",
                "source_url": "https://github.com/acme/core/issues/99",
                "team": "Acme Platform",
                "gates": ["platform.launch"],
            }
        ],
    )
    configs = load_external_connector_configs(p)
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg.dep_id == "gh-ext-1"
    assert cfg.connector_type == "github_issues"
    assert cfg.team == "Acme Platform"
    assert cfg.gates == ("platform.launch",)
    assert cfg.auth_token is None


def test_load_external_connector_configs_missing_dep_id(tmp_path: Path):
    p = tmp_path / "slice_contracts.yaml"
    _write_contracts(
        p,
        [{"connector_type": "github_issues", "source_url": "x", "team": "t"}],
    )
    with pytest.raises(ConfigError):
        load_external_connector_configs(p)


def test_load_external_connector_configs_invalid_list(tmp_path: Path):
    p = tmp_path / "slice_contracts.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    import yaml  # type: ignore[import]

    doc = {"schema_version": "1.0", "slices": [], "external_connectors": "not-a-list"}
    p.write_text(yaml.dump(doc), encoding="utf-8")
    with pytest.raises(ConfigError, match="external_connectors must be a list"):
        load_external_connector_configs(p)


# ---------------------------------------------------------------------------
# WS-2 PB-33: ExternalDependency schema evolution + backward compatibility.
# ---------------------------------------------------------------------------


def test_external_dependency_legacy_record_loads_with_defaults(tmp_path: Path) -> None:
    """WS-2: a JSONL record written under the OLD schema (no `state`/
    `is_fulfilled`/`criticality`/`resolved_at`/`source_ref`) must deserialize
    cleanly with the new fields defaulted. The persisted format on disk is
    the old shape; the in-memory object must reflect the current schema.
    """
    from src.core.config_loader import PROGRAMS_ROOT
    from src.core.external_dependency import save_external_dependency

    program_root = tmp_path / "programs" / "acme"
    program_root.mkdir(parents=True)
    legacy = {
        "dep_id": "legacy-dep-1",
        "team": "team-x",
        "tracked_items": [101, 102],
        "approval_type": "ado",
        "gates": ["QG-1"],
        "canonical_owner_program": None,
        "last_seen": None,
        # No state/is_fulfilled/criticality/resolved_at/source_ref — these
        # were added in WS-2.
    }
    jsonl = program_root / "external_dependencies.jsonl"
    jsonl.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    loaded = load_external_dependencies("acme", programs_root=tmp_path / "programs")
    assert len(loaded) == 1
    dep = loaded[0]
    assert dep.dep_id == "legacy-dep-1"
    assert dep.state == "unknown"
    assert dep.is_fulfilled is False
    assert dep.criticality == "normal"
    assert dep.resolved_at is None
    assert dep.source_ref is None
    assert dep.approval_type == "ado"


def test_external_dependency_widened_approval_type_github_sharepoint(tmp_path: Path) -> None:
    """WS-2: approval_type is widened to include github and sharepoint
    so the GitHub and SharePoint connectors can persist their state
    through the same store.
    """
    from src.core.external_dependency import save_external_dependency

    program_root = tmp_path / "programs" / "acme"
    program_root.mkdir(parents=True)
    save_external_dependency(
        "acme",
        ExternalDependency(
            dep_id="gh-1",
            team="gh-team",
            tracked_items=(),
            approval_type="github",
            gates=(),
            canonical_owner_program=None,
            last_seen=None,
            state="open",
            criticality="high",
            source_ref="owner/repo#42",
        ),
        programs_root=tmp_path / "programs",
    )
    save_external_dependency(
        "acme",
        ExternalDependency(
            dep_id="sp-1",
            team="sp-team",
            tracked_items=(),
            approval_type="sharepoint",
            gates=(),
            canonical_owner_program=None,
            last_seen=None,
            state="fulfilled",
            is_fulfilled=True,
            resolved_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
            source_ref="site/list#row-7",
        ),
        programs_root=tmp_path / "programs",
    )

    loaded = load_external_dependencies("acme", programs_root=tmp_path / "programs")
    by_id = {d.dep_id: d for d in loaded}
    assert by_id["gh-1"].approval_type == "github"
    assert by_id["gh-1"].state == "open"
    assert by_id["gh-1"].criticality == "high"
    assert by_id["sp-1"].approval_type == "sharepoint"
    assert by_id["sp-1"].state == "fulfilled"
    assert by_id["sp-1"].is_fulfilled is True
    assert by_id["sp-1"].resolved_at is not None


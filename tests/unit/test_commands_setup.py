from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from src.ai.ai_mode import AIMode, set_ai_mode
from src.core.setup_state import (
    ConversationStateMachine,
    SetupDraft,
    generate_edition_slug,
    load_session,
    save_session,
)


def test_setup_manual_mode_populates_draft_without_ado_or_ai() -> None:
    """T1: manual wizard path produces a complete SetupDraft with no external deps."""
    import src.commands.setup as setup_mod

    inputs = iter([
        "Test Program",
        "",
        "Test Author",
        "test@example.com",
        "",
        "",
        "Infra",
        "",
        "",
        "",
        "",
        "",
    ])

    draft = SetupDraft()
    sm = ConversationStateMachine(draft)

    with mock.patch("src.commands.setup.input", side_effect=inputs),          mock.patch("src.commands.setup._ai_suggest_workstreams", return_value=[]):

        setup_mod._collect_identity(draft)
        sm.transition("identity")
        workstreams = setup_mod._collect_workstreams(draft)
        sm.transition("ado_probe")
        ws_area_paths = setup_mod._collect_ado(draft, workstreams)
        sm.transition("structure_propose")
        setup_mod._build_structure(draft, workstreams, ws_area_paths)
        sm.transition("review")

    from src.commands.onboard import IdentityStage, ADOStage
    assert isinstance(draft.identity, IdentityStage)
    assert draft.identity.program_name == "Test Program"
    assert draft.identity.author_email == "test@example.com"
    assert draft.identity.cadence == "weekly"
    assert workstreams == [("Infra", "")]
    assert isinstance(draft.ado, ADOStage)
    assert draft.structure is not None
    assert draft.people is not None
    assert draft.style is not None
    assert sm.current == "review"

    fields = draft.to_onboard_draft()
    assert len(fields) == 5
    assert fields[0] is draft.identity
    assert fields[1] is draft.ado


def test_session_roundtrip_is_lossless(tmp_path: Path) -> None:
    """T3: draft serialises and deserialises through JSON with no data loss."""
    draft = SetupDraft(
        field_confidence={
            "identity.program_name": "user_confirmed",
            "identity.author_email": "user_confirmed",
            "ado.organization": "default",
        }
    )
    sm = ConversationStateMachine(draft)
    sm.current = "structure_propose"

    save_session(draft, sm, tmp_path, edition_slug="acme_weekly")
    result = load_session(tmp_path, edition_slug="acme_weekly")

    assert result is not None
    loaded_draft, loaded_sm, raw = result
    assert loaded_sm.current == "structure_propose"
    assert loaded_draft.field_confidence == {
        "identity.program_name": "user_confirmed",
        "identity.author_email": "user_confirmed",
        "ado.organization": "default",
    }
    assert raw["edition_slug"] == "acme_weekly"


def test_ai_suggest_workstreams_returns_empty_when_ai_disabled() -> None:
    import src.commands.setup as setup_mod

    set_ai_mode(AIMode.DISABLED)
    try:
        suggestions = setup_mod._ai_suggest_workstreams("Program description")
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert suggestions == []


def test_ai_suggest_workstreams_does_not_require_unused_onboard_assistant(monkeypatch) -> None:
    import src.commands.setup as setup_mod

    class _FakeFallbackStructuredClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
            del system, user, max_tokens, prompt_version
            return parser(
                {
                    "workstreams": [
                        {
                            "name": "Reliability",
                            "description": "Track platform health.",
                        }
                    ]
                }
            )

    monkeypatch.setattr(
        "src.ai.onboard_assistant.OnboardAssistant.from_environment",
        classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("unused onboard assistant should not be constructed"))),
    )
    monkeypatch.setattr("src.ai.deployment_fallback.resolve_ai_deployments", lambda **_kwargs: ("primary",))
    monkeypatch.setattr("src.ai.deployment_fallback.FallbackStructuredClient", _FakeFallbackStructuredClient)

    suggestions = setup_mod._ai_suggest_workstreams("Program description")

    assert suggestions == [("Reliability", "Track platform health.")]


def test_parse_ws_suggestions_runs_ai_text_through_safety_pipeline() -> None:
    import src.commands.setup as setup_mod

    suggestions = setup_mod._parse_ws_suggestions(
        {
            "workstreams": [
                {
                    "name": "Reliability",
                    "description": "Track platform health with foo@gmail.com.",
                }
            ]
        }
    )

    assert suggestions == [("Reliability", "Track platform health with [PII-FILTERED-EMAIL].")]


def test_session_load_returns_none_when_no_files(tmp_path: Path) -> None:
    """T3 edge: load_session returns None when no session exists."""
    assert load_session(tmp_path) is None
    assert load_session(tmp_path, edition_slug="nonexistent_weekly") is None


def test_session_load_returns_none_for_corrupt_json(tmp_path: Path) -> None:
    """T3 edge: corrupt session file returns None instead of raising."""
    session_dir = tmp_path / ".vertex"
    session_dir.mkdir()
    bad_file = session_dir / "setup_session_broken.json"
    bad_file.write_text("not json {{{", encoding="utf-8")
    result = load_session(tmp_path, edition_slug="broken")
    assert result is None


def test_session_save_uses_atomic_write(tmp_path: Path, monkeypatch) -> None:
    """T6: save_session writes to .tmp staging file then atomically replaces."""
    replacements: list[tuple[str, str]] = []
    real_replace = __import__("os").replace

    def _tracking_replace(src: str, dst: str) -> None:
        replacements.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr("os.replace", _tracking_replace)

    draft = SetupDraft(field_confidence={"identity.program_name": "user_confirmed"})
    sm = ConversationStateMachine(draft)
    save_session(draft, sm, tmp_path, edition_slug="test_weekly")

    assert len(replacements) == 1
    src, dst = replacements[0]
    assert src.endswith(".tmp")
    assert dst.endswith(".json")
    assert not Path(src).exists()
    assert Path(dst).exists()


def test_to_onboard_draft_returns_all_five_stage_fields() -> None:
    """T7: to_onboard_draft() returns (identity, ado, structure, people, style)."""
    identity = SimpleNamespace(program_name="Test", author_email="t@t.com")
    ado = SimpleNamespace(organization="msazure", project="One")
    structure = SimpleNamespace(scorecards=())
    people = SimpleNamespace(workstreams=())
    style = SimpleNamespace(voice="direct")

    draft = SetupDraft(
        identity=identity, ado=ado, structure=structure, people=people, style=style,
    )
    fields = draft.to_onboard_draft()
    assert len(fields) == 5
    assert fields[0] is identity
    assert fields[1] is ado
    assert fields[2] is structure
    assert fields[3] is people
    assert fields[4] is style


def test_to_onboard_draft_returns_none_for_unset_fields() -> None:
    """T7 edge: unset fields return as None (partial draft in early wizard state)."""
    draft = SetupDraft()
    fields = draft.to_onboard_draft()
    assert all(f is None for f in fields)


import pytest

@pytest.mark.parametrize("name,expected", [
    ("Acme Platform Reliability", "acme_platform_reliability_weekly"),
    ("Storage Compliance", "storage_compliance_weekly"),
    ("  My   Program  ", "my_program_weekly"),
    ("C++ & Infra!", "c_infra_weekly"),
    ("", "new_program_weekly"),
])
def test_generate_edition_slug_follows_spec_rules(name: str, expected: str) -> None:
    """Slug generation: lowercase, special chars to underscores, appends _weekly."""
    assert generate_edition_slug(name) == expected


def test_state_machine_rejects_invalid_transition() -> None:
    """State machine raises ValueError on invalid transition."""
    draft = SetupDraft()
    sm = ConversationStateMachine(draft)
    assert sm.current == "greeting"
    with pytest.raises(ValueError, match="Invalid transition"):
        sm.transition("write")


def test_state_machine_allows_simplified_wizard_path() -> None:
    """Simplified wizard path (no style/people steps) reaches done via review."""
    draft = SetupDraft()
    sm = ConversationStateMachine(draft)
    sm.transition("identity")
    sm.transition("ado_probe")
    sm.transition("structure_propose")
    sm.transition("review")
    sm.transition("write")
    sm.transition("done")
    assert sm.current == "done"


# ---------------------------------------------------------------------------
# Phase B — ADO Discovery tests
# ---------------------------------------------------------------------------


def test_list_area_paths_deduplicates_and_sorts() -> None:
    """ADOClient.list_area_paths() extracts unique sorted area paths from work item expand."""
    from unittest.mock import patch
    from src.core.ado_client import ADOClient

    raw_items = [
        {"WorkItemId": 1, "Area": {"AreaPath": "One\\Storage\\Compliance"}},
        {"WorkItemId": 2, "Area": {"AreaPath": "One\\Storage\\Networking"}},
        {"WorkItemId": 3, "Area": {"AreaPath": "One\\Storage\\Compliance"}},  # duplicate
        {"WorkItemId": 4, "Area": {}},  # no AreaPath — skipped
    ]

    with patch.object(ADOClient, "_init_auth", return_value=None), \
         patch.object(ADOClient, "query_work_items", return_value=raw_items):
        client = ADOClient.__new__(ADOClient)
        client.organization = "test-org"
        client.project = "test-project"
        client.timeout = 30
        client.auth_method = "pat"

        result = client.list_area_paths(days=90, top=200)

    assert result == ("One\\Storage\\Compliance", "One\\Storage\\Networking")


def test_get_recent_work_items_summary_builds_correct_filter() -> None:
    """ADOClient.get_recent_work_items_summary() builds an area+date+state filter."""
    from unittest.mock import patch
    from src.core.ado_client import ADOClient

    captured: list[str] = []

    def _fake_query(filter_expression: str, *, select_fields: tuple, top: int) -> list:
        captured.append(filter_expression)
        return []

    with patch.object(ADOClient, "_init_auth", return_value=None), \
         patch.object(ADOClient, "query_work_items", side_effect=_fake_query):
        client = ADOClient.__new__(ADOClient)
        client.organization = "test-org"
        client.project = "test-project"
        client.timeout = 30
        client.auth_method = "pat"

        client.get_recent_work_items_summary("One\\Storage\\Compliance", days=30, top=200)

    assert len(captured) == 1
    expr = captured[0]
    assert "One\\Storage\\Compliance" in expr
    assert "startswith(AreaPath" in expr
    assert "State ne 'Closed'" in expr
    assert "State ne 'Resolved'" in expr
    assert "ChangedDate ge" in expr


def test_suggest_workstreams_from_samples_clusters_by_prefix() -> None:
    """suggest_workstreams_from_samples() groups WorkItemSamples by 3-component prefix."""
    from src.core.ado_discovery import WorkItemSample, suggest_workstreams_from_samples

    def _sample(area_path: str) -> WorkItemSample:
        return WorkItemSample(
            id=1, title="Test", work_item_type="Feature",
            area_path=area_path, assigned_to=None, state="Active", target_date=None,
        )

    samples = (
        _sample("One\\Storage\\Compliance\\Cert"),
        _sample("One\\Storage\\Compliance\\Audit"),
        _sample("One\\Storage\\Compliance\\Audit"),
        _sample("One\\Storage\\Networking\\Infra"),
        _sample("One\\Storage\\Networking\\Infra"),
        _sample("One\\Storage\\Networking\\Infra"),
    )

    result = suggest_workstreams_from_samples(samples)

    assert len(result) == 2
    names = {r.name for r in result}
    assert names == {"Networking", "Compliance"}
    networking = next(r for r in result if r.name == "Networking")
    assert networking.area_paths == ("One\\Storage\\Networking",)
    assert networking.item_count == 3
    compliance = next(r for r in result if r.name == "Compliance")
    assert compliance.area_paths == ("One\\Storage\\Compliance",)
    assert compliance.item_count == 3


def test_list_projects_returns_sorted_names() -> None:
    """list_projects() returns sorted project names as a tuple."""
    from unittest.mock import patch
    from src.core.ado_client import ADOClient
    from src.core.ado_discovery import list_projects

    fake_response = {
        "value": [
            {"name": "Zephyr"},
            {"name": "Alpha"},
            {"name": "Middleware"},
        ]
    }

    with patch.object(ADOClient, "_init_auth", return_value=None), \
         patch.object(ADOClient, "_request_json", return_value=fake_response):
        client = ADOClient.__new__(ADOClient)
        client.organization = "test-org"
        client.project = "test-project"
        client.timeout = 30
        client.auth_method = "pat"

        result = list_projects(client)

    assert result == ("Alpha", "Middleware", "Zephyr")


def test_collect_ado_uses_live_discovery_when_available(monkeypatch) -> None:
    """_collect_ado() shows discovered area paths and records ado._discovery_used."""
    import src.commands.setup as setup_mod
    from src.commands.onboard import ADOStage, IdentityStage

    discovered = ("One\\MyOrg\\Networking", "One\\MyOrg\\Reliability")
    monkeypatch.setattr(setup_mod, "_try_live_ado_area_discovery", lambda o, p: discovered)

    draft = SetupDraft()
    draft.identity = IdentityStage(
        program_name="My Program",
        program_id="my_program",
        objective="Ship GA",
        mission="Ship GA",
        newsletter_title="My Program Weekly",
        author_display_name="Test User",
        author_email="user@example.com",
        cadence="weekly",
        send_day="monday",
        send_time_local=None,
        timezone=None,
    )

    inputs = iter([
        "my-org",      # org
        "my-project",  # project
        "",            # accept suggested default for "Infra" workstream
    ])

    with mock.patch("src.commands.setup.input", side_effect=inputs):
        ws_area_paths = setup_mod._collect_ado(draft, [("Infra", "")], manual=False)

    assert isinstance(draft.ado, ADOStage)
    assert draft.ado.organization == "my-org"
    assert draft.ado_discovery_used is True
    assert len(ws_area_paths) == 1


# ---------------------------------------------------------------------------
# T4: Preview HTML contains required sections
# ---------------------------------------------------------------------------

def test_preview_html_contains_required_sections() -> None:
    """T4: preview HTML renders health banner, scorecard, workstream, footer."""
    from src.commands.setup_preview import (
        PreviewDimension,
        PreviewItem,
        PreviewScorecard,
        PreviewWorkstream,
        render_preview_html,
    )

    workstreams = [
        PreviewWorkstream(
            name="Infra Health",
            items=(
                PreviewItem(
                    title="Fix latency spike",
                    risk="yellow",
                    risk_label="At Risk",
                    owner="Alex Chen",
                    eta="2026-07-01",
                ),
            ),
        )
    ]
    scorecards = [
        PreviewScorecard(
            name="Reliability",
            dimensions=(
                PreviewDimension(name="SLA Health", score="On Track", detail="3 items"),
            ),
        )
    ]

    html = render_preview_html(
        "Acme Platform",
        "acme_platform_weekly",
        workstreams,
        scorecards,
    )

    assert "Preview — sample data only" in html
    assert "Health:" in html  # health banner
    assert "Reliability" in html  # scorecard
    assert "Infra Health" in html  # workstream
    assert "provenance" in html.lower() or "Provenance" in html  # footer
    assert "SLA Health" in html
    assert "At Risk" in html  # text label (not color alone)
    assert 'scope="col"' in html  # WCAG table headers


def test_preview_html_demo_mode_sets_data_attribute() -> None:
    """T4 edge: demo=True adds data-demo attribute to html element."""
    from src.commands.setup_preview import render_preview_html

    html = render_preview_html("Demo Prog", "demo_prog_weekly", [], [], demo=True)
    assert 'data-demo="true"' in html


def test_preview_generate_data_is_deterministic() -> None:
    """T4: generate_preview_data produces identical output for the same seed."""
    from src.commands.setup_preview import generate_preview_data

    ws_names = ["Infra", "Features", "Compliance"]
    sc_names = [("Core", ["Availability", "Latency"])]

    ws1, sc1 = generate_preview_data(ws_names, sc_names, seed=42)
    ws2, sc2 = generate_preview_data(ws_names, sc_names, seed=42)

    assert len(ws1) == len(ws2)
    for a, b in zip(ws1, ws2):
        assert a.name == b.name
        assert len(a.items) == len(b.items)
        for ia, ib in zip(a.items, b.items):
            assert ia.risk == ib.risk
            assert ia.title == ib.title


def test_preview_write_creates_file(tmp_path: Path) -> None:
    """T4 edge: write_preview creates output/setup_preview.html."""
    from src.commands.setup_preview import write_preview

    preview_path = write_preview("<html>test</html>", tmp_path)
    assert preview_path.exists()
    assert preview_path.read_text() == "<html>test</html>"
    assert preview_path.name == "setup_preview.html"


# ---------------------------------------------------------------------------
# ADO discovery deterministic clustering (Phase B)
# ---------------------------------------------------------------------------

def test_suggest_workstreams_from_samples_clusters_by_area_path() -> None:
    """Phase B: deterministic workstream clustering by area path prefix."""
    from src.core.ado_discovery import WorkItemSample, suggest_workstreams_from_samples

    def _s(area_path: str, title: str) -> WorkItemSample:
        return WorkItemSample(
            id=1, title=title, work_item_type="Feature",
            area_path=area_path, assigned_to=None, state="Active", target_date=None,
        )

    samples = (
        _s("One\\Storage\\Reliability", "Fix latency"),
        _s("One\\Storage\\Reliability", "Add retry"),
        _s("One\\Storage\\Compliance", "Run audit"),
        _s("One\\Storage\\Features", "Ship v2"),
    )

    suggestions = suggest_workstreams_from_samples(samples)

    assert len(suggestions) == 3
    names = {s.name for s in suggestions}
    assert "Reliability" in names
    assert "Compliance" in names
    assert "Features" in names

    reliability = next(s for s in suggestions if s.name == "Reliability")
    assert reliability.item_count == 2


def test_suggest_workstreams_empty_input() -> None:
    """Edge: empty samples returns empty tuple."""
    from src.core.ado_discovery import suggest_workstreams_from_samples

    assert suggest_workstreams_from_samples(()) == ()


# ---------------------------------------------------------------------------
# SetupAssistant: concept explanations and fallback
# ---------------------------------------------------------------------------

def test_setup_assistant_explains_known_concepts() -> None:
    """SetupAssistant.explain_concept returns hard-coded defaults for known terms."""
    from src.ai.setup_assistant import SetupAssistant

    assistant = SetupAssistant()
    explanation = assistant.explain_concept("workstream")
    assert "workstream" in explanation.lower()
    assert len(explanation) > 10


def test_setup_assistant_unknown_concept_returns_message() -> None:
    """SetupAssistant.explain_concept handles unknown concepts gracefully."""
    from src.ai.setup_assistant import SetupAssistant

    assistant = SetupAssistant()
    result = assistant.explain_concept("unobtainium")
    assert "unobtainium" in result.lower()


def test_setup_assistant_suggest_workstreams_empty_without_ai() -> None:
    """SetupAssistant returns empty list when no AI client available."""
    from src.ai.setup_assistant import SetupAssistant

    assistant = SetupAssistant()  # no client
    result = assistant.suggest_workstreams_from_description("Platform reliability newsletter")
    assert isinstance(result, list)


def test_setup_assistant_from_environment_does_not_build_ai_helper_when_invocation_ai_disabled(monkeypatch) -> None:
    from src.ai.setup_assistant import SetupAssistant

    calls = {"count": 0}

    def _unexpected_from_environment(_cls):
        calls["count"] += 1
        raise AssertionError("OnboardAssistant.from_environment should not be called")

    set_ai_mode(AIMode.DISABLED)
    try:
        monkeypatch.setattr(
            "src.ai.onboard_assistant.OnboardAssistant.from_environment",
            classmethod(_unexpected_from_environment),
        )

        assistant = SetupAssistant.from_environment()
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert calls["count"] == 0
    assert isinstance(assistant, SetupAssistant)
    assert assistant.suggest_workstreams_from_description("Platform reliability newsletter") == []


def test_setup_assistant_suggest_workstreams_from_description_does_not_call_ai_when_invocation_ai_disabled(monkeypatch) -> None:
    from src.ai.setup_assistant import SetupAssistant

    calls = {"count": 0}

    def _unexpected_ai_suggest(_description: str):
        calls["count"] += 1
        raise AssertionError("_ai_suggest_workstreams should not be called")

    assistant = SetupAssistant(client=object())
    monkeypatch.setattr(assistant, "_ai_suggest_workstreams", _unexpected_ai_suggest)

    set_ai_mode(AIMode.DISABLED)
    try:
        result = assistant.suggest_workstreams_from_description("Platform reliability newsletter")
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert calls["count"] == 0
    assert result == []


def test_setup_assistant_suggest_workstreams_from_description_uses_injected_client(monkeypatch) -> None:
    from src.ai.setup_assistant import SetupAssistant

    class _FakeAIClient:
        def __init__(self) -> None:
            self.last_prompt_version: str | None = None
            self.last_user: str | None = None

        def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
            del system, max_tokens
            self.last_prompt_version = prompt_version
            self.last_user = user
            return parser(
                {
                    "workstreams": [
                        {
                            "name": "Reliability",
                            "description": "Track platform health.",
                            "rationale": "Reliability work is central and foo@gmail.com owns the escalation path.",
                        }
                    ]
                }
            )

    calls = {"count": 0}

    def _unexpected_resolve_ai_deployments(*args, **kwargs):
        del args, kwargs
        calls["count"] += 1
        raise AssertionError("resolve_ai_deployments should not be called when a client is injected")

    monkeypatch.setattr("src.ai.deployment_fallback.resolve_ai_deployments", _unexpected_resolve_ai_deployments)

    client = _FakeAIClient()
    assistant = SetupAssistant(client=client)
    result = assistant.suggest_workstreams_from_description("Storage platform reliability and compliance")

    assert calls["count"] == 0
    assert len(result) == 1
    assert result[0].name == "Reliability"
    assert result[0].description == "Track platform health."
    assert "foo@gmail.com" not in result[0].rationale
    assert "[PII-FILTERED-EMAIL]" in result[0].rationale
    assert client.last_prompt_version == "setup_ws_suggest.v1"
    assert client.last_user == "Program description: Storage platform reliability and compliance"


# ---------------------------------------------------------------------------
# Phase D: _run_preview wiring
# ---------------------------------------------------------------------------

def test_run_preview_writes_html_and_skips_browser_when_no_open(tmp_path: Path) -> None:
    """_run_preview writes setup_preview.html to output_dir; no_open=True skips webbrowser."""
    import src.commands.setup as setup_mod
    from src.commands.onboard import ADOStage, IdentityStage

    draft = SetupDraft()
    draft.identity = IdentityStage(
        program_name="Acme Platform",
        program_id="acme_platform",
        objective="Ship GA",
        mission="Ship GA",
        newsletter_title="Acme Platform Weekly",
        author_display_name="Test User",
        author_email="user@example.com",
        cadence="weekly",
        send_day="monday",
        send_time_local=None,
        timezone=None,
    )

    workstreams = [("Infra Health", "Track infra"), ("Feature Delivery", "Track features")]

    setup_mod._run_preview(draft, workstreams, output_dir=tmp_path, no_open=True)

    preview_file = tmp_path / "setup_preview.html"
    assert preview_file.exists(), "setup_preview.html was not written"
    html = preview_file.read_text(encoding="utf-8")
    assert "Acme Platform" in html
    assert "Preview" in html  # watermark
    assert "Infra Health" in html
    assert "Feature Delivery" in html

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import cli
import src.ai.intent_router as intent_router
from cli import app
from src.ai.intent_router import IntentRouter, IntentRouterError, RoutedInvocation
from src.core.ledger.event_log import read_events


pytestmark = pytest.mark.usefixtures("isolated_cli_startup")


runner = CliRunner()


@pytest.fixture(autouse=True)
def configured_editions(monkeypatch, tmp_path: Path) -> None:
    for program in ("acme", "fabrikam"):
        edition_dir = tmp_path / program / "editions"
        edition_dir.mkdir(parents=True)
        (edition_dir / f"{program}_weekly.yaml").write_text(
            f"id: {program}_weekly\nprogram_id: {program}\n", encoding="utf-8"
        )
    monkeypatch.setattr(intent_router, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)


def test_ask_command_renders_deterministic_route_from_golden(monkeypatch, repo_root: Path) -> None:
    monkeypatch.setattr("src.commands.ask.IntentRouter.from_environment", classmethod(lambda cls: (_ for _ in ()).throw(IntentRouterError("VERTEX_AI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT not set. Configure Azure OpenAI or use only supported deterministic intent routes."))))

    result = runner.invoke(app, ["ask", "--edition", "acme_weekly", "redo issue 12 with fabrikam only"])

    expected = (repo_root / "tests" / "golden" / "ask_output.txt").read_text(encoding="utf-8")
    assert result.exit_code == 0
    assert result.stdout == expected


def test_ask_command_uses_ai_fallback_when_router_is_available(monkeypatch, tmp_path: Path) -> None:
    route = IntentRouter.route
    monkeypatch.setattr(
        IntentRouter,
        "route",
        lambda self, request, *, default_edition="": route(
            self, request, default_edition=default_edition, programs_root=tmp_path
        ),
    )
    monkeypatch.setattr(
        "src.commands.ask._build_router",
        lambda: IntentRouter(
            client=type(
                "_FakeClient",
                (),
                {
                    "structured": staticmethod(
                        lambda system, user, *, parser, max_tokens=500, prompt_version=None: parser(
                            {
                                "command": "doctor",
                                "args": ["--edition", "acme_weekly"],
                                "warnings": ["Mapped to the nearest supported health-check command."],
                            }
                        )
                    )
                },
            )()
        ),
    )

    result = runner.invoke(app, ["ask", "--edition", "acme_weekly", "help me sanity-check the edition before I do anything else"])

    assert result.exit_code == 0
    assert "vertex doctor --edition acme_weekly" in result.stdout
    assert "Routing: ai (intent_router.v1)" in result.stdout
    events = read_events("acme", programs_root=tmp_path, repair_corruption=False)
    assert [event.payload["terminal"] for event in events if event.event_type == "ai.release_decision.v1"] == [
        "released",
    ]


def test_ask_command_surfaces_routing_failure(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.ask._build_router", lambda: IntentRouter())

    result = runner.invoke(app, ["ask", "--edition", "acme_weekly", "do something entirely unsupported"])

    assert result.exit_code == 1
    assert "Intent routing failed:" in result.stdout


def test_route_request_returns_rendered_output(monkeypatch) -> None:
    from src.commands.ask import route_request

    monkeypatch.setattr("src.commands.ask._build_router", lambda: IntentRouter())

    rendered = route_request("what should i do next", edition="acme_weekly")

    assert "vertex next --edition acme_weekly" in rendered
    assert "Routing: deterministic" in rendered


def test_route_request_raises_bad_parameter_on_failure(monkeypatch) -> None:
    from src.commands.ask import route_request

    monkeypatch.setattr("src.commands.ask._build_router", lambda: IntentRouter())

    with pytest.raises(typer.BadParameter, match="Intent routing failed:"):
        route_request("do something entirely unsupported", edition="acme_weekly")


def test_root_cli_routes_default_nl_request(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.ask._build_router", lambda: IntentRouter())

    result = runner.invoke(app, ["--edition", "acme_weekly", "what should i do next"])

    assert result.exit_code == 0
    assert "vertex next --edition acme_weekly" in result.stdout
    assert "Routing: deterministic" in result.stdout


def test_root_cli_routes_default_nl_request_with_root_edition_override(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.ask._build_router", lambda: IntentRouter())

    result = runner.invoke(app, ["--edition", "fabrikam_weekly", "what should i do next"])

    assert result.exit_code == 0
    assert "vertex next --edition fabrikam_weekly" in result.stdout


def test_root_cli_preserves_help_without_request() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Vertex hybrid journal automation CLI." in result.stdout


def test_render_ask_output_formats_warning_list() -> None:
    from src.commands.ask import _render_ask_output

    rendered = _render_ask_output(
        RoutedInvocation(
            command="report",
            args=("--edition", "acme_weekly", "--issue", "12", "--dry-run"),
            warnings=("Example warning.",),
            prompt_version=None,
        )
    )

    assert rendered.splitlines() == [
        "Suggested command:",
        "vertex report --edition acme_weekly --issue 12 --dry-run",
        "Routing: deterministic",
        "Warnings:",
        "- Example warning.",
    ]


@pytest.mark.parametrize(
    ("nl_request", "expected_command"),
    [
        ("give me the morning brief", "vertex brief --program acme --today"),
        ("what should i do next", "vertex next --edition acme_weekly"),
        ("catch me up", "vertex catchup --program acme"),
        ("show current manifest", "vertex manifest --edition acme_weekly"),
        ("show contradictions", "vertex reconcile --program acme --dry-run"),
        ("list editions", "vertex list editions"),
        ("list workstreams", "vertex list workstreams --edition acme_weekly"),
        ("list dris", "vertex list dris --edition acme_weekly"),
        ("list milestones", "vertex milestones list --program acme"),
        ("show milestone health", "vertex milestones assess --program acme"),
        ("show review sections", "vertex review-sections show --edition acme_weekly"),
        ("show dependency proposals", "vertex dependencies list --program acme"),
        ("generate prep brief", "vertex prep --edition acme_weekly"),
        ("generate deck companion", "vertex deck-companion --edition acme_weekly"),
        ("review proposals", "vertex review-proposals --edition acme_weekly --no-open"),
        ("show fleet health", "vertex fleet"),
        ("show trust calibration", "vertex trust --program acme"),
        ("show audit timeline", "vertex audit --program acme"),
        ("show prompt learning summary", "vertex audit --program acme --prompt-learning-summary"),
        ("show decision debt", "vertex decisions aging --program acme"),
        ("show author salience", "vertex salience show --program acme --no-refresh"),
        ("show evidence for WI 1001", "vertex evidence --edition acme_weekly --issue latest --ado 1001"),
        ("search semantic history for ud chunking latency regression", "vertex history --edition acme_weekly --semantic ud chunking latency regression"),
        ("close meeting transcript mtg-12345", "vertex meeting-close --program acme --transcript mtg-12345 --dry-run"),
        ("show launch readiness", "vertex readiness --program acme"),
        ("list open risks", "vertex risks list --program acme"),
        ("show open claims", "vertex claims --program acme"),
        ("show claim accuracy", "vertex calibration report --program acme"),
        ("show unreviewed signals", "vertex signals --program acme"),
        ("show open actions", "vertex actions --program acme"),
        ("show program vitality", "vertex vitality --program acme"),
        ("show ado status", "vertex ado status --program acme"),
        ("reconcile ado drift", "vertex ado reconcile --program acme"),
        ("show storage stats", "vertex storage stats --program acme"),
        ("check storage health", "vertex storage check --program acme"),
    ],
)
def test_ask_command_routes_additional_deterministic_read_only_intents(monkeypatch, nl_request: str, expected_command: str) -> None:
    monkeypatch.setattr("src.commands.ask._build_router", lambda: IntentRouter())

    result = runner.invoke(app, ["ask", "--edition", "acme_weekly", nl_request])

    assert result.exit_code == 0
    assert expected_command in result.stdout
    assert "Routing: deterministic" in result.stdout
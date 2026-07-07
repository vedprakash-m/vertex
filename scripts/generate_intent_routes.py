from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, cast

import click
import yaml
from typer.main import get_command
from typer.core import TyperCommand, TyperGroup, TyperOption

import cli


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "vertex" / "intent_routes.yaml"
SCHEMA_VERSION = "1"

# Blocklist for commands that should never be AI-routable (admin, internal,
# destructive, or operational-only).  All other commands are automatically
# discovered and emitted.  Adding a new user-facing CLI command no longer
# requires editing this file.
_NON_ROUTABLE_COMMANDS: frozenset[str] = frozenset({
    "admin",
    "archive-journals",
    "backup",
    "bootstrap",
    "bridge-status",
    "config",
    "connectors",
    "context-diff",
    "diff",
    "edit",
    "escalate",
    "hints",
    "hypothesis",
    "index",
    "integration",
    "migrate",
    "notify",
    "nudge",
    "observation",
    "onboard",
    "override",
    "policy",
    "probe-ado",
    "published-baseline",
    "reality",
    "rollback",
    "setup",
    "summarize",
    "synthesize",
    "watch",
})


def _click_root() -> click.Command:
    root = get_command(cli.app)
    if not hasattr(root, "commands"):
        raise RuntimeError("Vertex CLI root is not a command group.")
    return root


def _iter_command_entries(command: click.Command) -> dict[str, click.Command]:
    commands = getattr(command, "commands", None)
    if isinstance(commands, dict):
        return {
            name: entry
            for name, entry in commands.items()
            if isinstance(entry, (click.Command, TyperCommand, TyperGroup))
        }
    return {}


def _extract_option_tokens(command: click.Command) -> dict[str, list[str]]:
    flags: list[str] = []
    value_options: list[str] = []
    for parameter in command.params:
        if not isinstance(parameter, (click.Option, TyperOption)):
            continue
        if getattr(parameter, "hidden", False):
            continue
        long_opts = [option for option in (*parameter.opts, *parameter.secondary_opts) if option.startswith("--")]
        for option in long_opts:
            if parameter.is_flag:
                flags.append(option)
            else:
                value_options.append(option)
    return {
        "flags": sorted(dict.fromkeys(flags)),
        "value_options": sorted(dict.fromkeys(value_options)),
    }


def build_intent_route_catalog() -> dict[str, object]:
    root = _click_root()
    catalog: dict[str, object] = {"schema_version": SCHEMA_VERSION, "commands": {}}
    commands = cast(dict[str, object], catalog["commands"])
    for command_name, command in sorted(_iter_command_entries(root).items()):
        if command_name in _NON_ROUTABLE_COMMANDS:
            continue

        command_spec: dict[str, object] = {}
        subcommands = _iter_command_entries(command)
        callback_spec = _extract_option_tokens(command)
        if callback_spec["flags"] or callback_spec["value_options"]:
            command_spec["callback"] = callback_spec
        elif not subcommands:
            command_spec["callback"] = {"flags": [], "value_options": []}

        if subcommands:
            subcommand_specs: dict[str, object] = {}
            for subcommand_name, subcommand in sorted(subcommands.items()):
                if subcommand_name.startswith("_"):
                    continue
                subcommand_specs[subcommand_name] = _extract_option_tokens(subcommand)
            if subcommand_specs:
                command_spec["subcommands"] = subcommand_specs

        if command_spec:
            commands[command_name] = command_spec
    return catalog


def render_intent_route_catalog(catalog: dict[str, object]) -> str:
    return yaml.safe_dump(catalog, sort_keys=False, allow_unicode=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the Vertex intent route catalog.")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH, help="Target YAML path.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify that the existing catalog is up to date without rewriting it.",
    )
    args = parser.parse_args(argv)

    rendered = render_intent_route_catalog(build_intent_route_catalog())
    if args.check:
        existing = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if existing != rendered:
            print(f"Intent route catalog is out of date: {args.output}", file=sys.stderr)
            return 1
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"Wrote intent route catalog to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from pathlib import Path
import tempfile
import sys
from typing import Iterable

import click
from typer.main import get_command


REPO_ROOT = Path(__file__).resolve().parents[1]
# Generated CLI reference snapshot lives in tests/contracts/ (tracked). Do not edit manually.
OUTPUT_PATH = REPO_ROOT / "tests" / "contracts" / "cli_reference_snapshot.md"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cli import app


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help=f"Where to write the generated reference (default: {OUTPUT_PATH.relative_to(REPO_ROOT)}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the existing file at --output differs from the regeneration. Used in CI.",
    )
    args = parser.parse_args()
    target = args.output.resolve()
    if not target.is_absolute():
        target = (REPO_ROOT / target).resolve()
    root_command = get_command(app)
    lines: list[str] = [
        "# Vertex CLI Reference",
        "",
        "Generated from the live Typer command tree. Do not edit manually.",
        "Regenerate with `./.venv/Scripts/python.exe scripts/generate_cli_reference.py`.",
        "",
        "Installed entry points: `vertex`, `vx`.",
        "",
    ]
    lines.extend(_render_command_tree(root_command, path=()))
    rendered = "\n".join(lines).rstrip() + "\n"
    rendered = rendered.replace("\r\n", "\n")
    if args.check:
        if not target.exists():
            print(f"FAIL: {target.relative_to(REPO_ROOT)} does not exist; run without --check to seed it.", file=sys.stderr)
            raise SystemExit(2)
        existing = target.read_text(encoding="utf-8")
        if existing != rendered:
            print(f"FAIL: {target.relative_to(REPO_ROOT)} drifted from live command tree. Regenerate and commit.", file=sys.stderr)
            raise SystemExit(1)
        print(f"OK: {target.relative_to(REPO_ROOT)} matches live command tree.")
        raise SystemExit(0)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(target, rendered)
    print(f"Wrote {target.relative_to(REPO_ROOT)}")


def _render_command_tree(command: click.Command, *, path: tuple[str, ...]) -> list[str]:
    lines = _render_command(command, path=path)
    if _is_group(command):
        ctx = _build_context(path)
        for name in command.list_commands(ctx):
            subcommand = command.get_command(ctx, name)
            if subcommand is None or subcommand.hidden:
                continue
            lines.extend(_render_command_tree(subcommand, path=(*path, name)))
    return lines


def _render_command(command: click.Command, *, path: tuple[str, ...]) -> list[str]:
    ctx = _build_context(path)
    invocation = _command_invocation(path)
    heading_level = min(2 + len(path), 4)
    lines = [
        f"{'#' * heading_level} `{invocation}`",
        "",
        f"**Usage:** `{_usage_line(ctx)}`",
        "",
    ]

    help_text = (command.help or command.short_help or "").strip()
    if help_text:
        lines.extend([help_text, ""])

    options = _visible_options(command)
    if options:
        lines.extend(["**Options**", "", "| Option | Type | Required | Default | Description |", "|---|---|---|---|---|"])
        for option in options:
            lines.append(
                "| {names} | {type_name} | {required} | {default} | {description} |".format(
                    names=_escape_table_text(_format_option_names(option, ctx)),
                    type_name=_escape_table_text(_option_type_name(option)),
                    required="Yes" if option.required else "No",
                    default=_escape_table_text(_format_default(option.default)),
                    description=_escape_table_text(option.help or ""),
                )
            )
        lines.append("")

    if _is_group(command):
        subcommands = list(_visible_subcommands(command, ctx))
        if subcommands:
            lines.extend(["**Subcommands**", "", "| Command | Description |", "|---|---|"])
            for name, subcommand in subcommands:
                description = _summary_line(subcommand.short_help or subcommand.help or "")
                lines.append(f"| `{name}` | {_escape_table_text(description)} |")
            lines.append("")

    return lines


def _build_context(path: tuple[str, ...]) -> click.Context:
    root_command = get_command(app)
    ctx = click.Context(root_command, info_name="vertex")
    command: click.Command = root_command
    for name in path:
        if not _is_group(command):
            raise click.ClickException(f"Command path is not a group: {' '.join(path)}")
        subcommand = command.get_command(ctx, name)
        if subcommand is None:
            raise click.ClickException(f"Unknown command path: {' '.join(path)}")
        ctx = click.Context(subcommand, info_name=name, parent=ctx)
        command = subcommand
    return ctx


def _visible_options(command: click.Command) -> list[click.Option]:
    return [
        param
        for param in command.params
        if isinstance(param, click.Option) and not param.hidden
    ]


def _visible_subcommands(command: click.Group, ctx: click.Context) -> Iterable[tuple[str, click.Command]]:
    for name in command.list_commands(ctx):
        subcommand = command.get_command(ctx, name)
        if subcommand is None or subcommand.hidden:
            continue
        yield name, subcommand


def _is_group(command: click.Command) -> bool:
    return isinstance(command, click.Group)


def _command_invocation(path: tuple[str, ...]) -> str:
    return "vertex" if not path else f"vertex {' '.join(path)}"


def _usage_line(ctx: click.Context) -> str:
    usage = ctx.command.get_usage(ctx).strip()
    if usage.lower().startswith("usage:"):
        usage = usage[6:].strip()
    return usage


def _format_option_names(option: click.Option, ctx: click.Context) -> str:
    names = list(option.opts)
    if option.secondary_opts:
        names.extend(option.secondary_opts)
    formatted = " / ".join(names)
    if option.is_flag:
        return formatted
    metavar = option.make_metavar(ctx)
    return f"{formatted} {metavar}" if metavar else formatted


def _option_type_name(option: click.Option) -> str:
    type_name = getattr(option.type, "name", option.type.__class__.__name__)
    return str(type_name or "text")


def _format_default(value: object) -> str:
    if value in (None, (), [], {}):
        return ""
    if isinstance(value, (tuple, list, set)):
        return ", ".join(_format_default_scalar(item) for item in value)
    return _format_default_scalar(value)


def _format_default_scalar(value: object) -> str:
    rendered = str(value)
    path_like = value if isinstance(value, Path) else Path(rendered)
    if path_like.is_absolute():
        return _sanitize_absolute_path(path_like)
    return rendered


def _sanitize_absolute_path(path_value: Path) -> str:
    resolved_repo = REPO_ROOT.resolve()
    resolved_path = path_value.resolve()
    try:
        return resolved_path.relative_to(resolved_repo).as_posix()
    except ValueError:
        if resolved_path.name == "programs":
            return "programs"
        return "<absolute-path>"


def _write_text_atomic(target: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    temp_path.replace(target)


def _escape_table_text(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _summary_line(value: str) -> str:
    lines = value.strip().splitlines()
    return lines[0].strip() if lines else ""


if __name__ == "__main__":
    main()

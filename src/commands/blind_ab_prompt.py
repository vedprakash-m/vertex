"""Shared input validation for blind pilot comparisons."""
from __future__ import annotations

from collections.abc import Callable

from src.core.blind_ab_comparison import ComparisonChoice


def prompt_comparison_judgment(
    prompt_fn: Callable[[str], str],
    echo_fn: Callable[[str], None],
) -> tuple[ComparisonChoice, bool]:
    choices: dict[str, ComparisonChoice] = {
        "a": "a", "b": "b", "tie": "tie", "neither": "neither",
    }
    while True:
        choice = choices.get(prompt_fn("Which is better? [a/b/tie/neither]").strip().lower())
        if choice is not None:
            break
        echo_fn("Enter a, b, tie, or neither.")

    while True:
        critical = prompt_fn(
            "Did either option contain a critical error (hallucinated/unsupported claim)? [y/n]"
        ).strip().lower()
        if critical in ("y", "yes"):
            return choice, True
        if critical in ("", "n", "no"):
            return choice, False
        echo_fn("Enter y or n.")

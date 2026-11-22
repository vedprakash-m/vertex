"""Prepare or verify explicit RELO-W6 qualification evidence packets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.relo_w6 import (  # noqa: E402
    W6_CARD_IDS,
    build_learning_disposition_inventory,
    build_w6_evidence_template,
    terminal_state_from_evidence,
    verify_w6_packet,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--template", choices=W6_CARD_IDS)
    action.add_argument("--input", type=Path)
    action.add_argument("--learning-dispositions", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--terminal-state", action="store_true")
    args = parser.parse_args(argv)

    if args.template:
        print(json.dumps(build_w6_evidence_template(args.template), indent=2, sort_keys=True))
        return 0
    if args.learning_dispositions:
        print(json.dumps(
            build_learning_disposition_inventory(args.repo_root.resolve()),
            indent=2,
            sort_keys=True,
        ))
        return 0

    assert args.input is not None
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print(json.dumps({
            "classification": "not_qualified",
            "invalid_fields": ["input"],
        }, indent=2, sort_keys=True))
        return 1
    if args.terminal_state:
        if not isinstance(payload, dict):
            print(json.dumps({"terminal_state": None, "assessment": "not_assessed"}))
            return 1
        output = terminal_state_from_evidence(payload)
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if output["terminal_state"] is not None else 1
    result = verify_w6_packet(payload, repo_root=args.repo_root.resolve())
    print(json.dumps(result.to_payload(), indent=2, sort_keys=True))
    return 0 if result.classification == "owner_review" else 1


if __name__ == "__main__":
    raise SystemExit(main())

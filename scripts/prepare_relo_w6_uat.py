"""Create a privacy-preserving RELO-W6 UAT evidence checklist and manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


DEFAULT_REQUIRED_PROFILES = (
    "outlook_desktop",
    "outlook_web",
    "outlook_ios",
    "projector_1920x1080",
)
_REQUIRED_CHECKS = (
    "Artifact opens in the named client/profile.",
    "Document width is 680px or less with a 640px content area.",
    "Links, bullets, tables, and scorecard text remain readable.",
    "Dark/light client behavior has been observed and recorded.",
    "Projector text is at least 16px and contrast is recorded.",
    "Blind output review records reviewer identity only in the private packet.",
    "No material factual, traceability, risk, dependency, action, or consistency regression is found.",
    "Private recipient, mailbox, source-path, token, and program metadata are not copied into this manifest.",
)


def prepare_manifest(
    artifacts: list[tuple[str, Path]],
    *,
    edition: str,
    issue_number: int,
) -> dict[str, Any]:
    entries = []
    labels: set[str] = set()
    for label, path in artifacts:
        if not label.strip() or label in labels:
            raise ValueError("UAT artifact labels must be unique and non-empty.")
        labels.add(label)
        payload = path.read_bytes()
        entries.append({
            "profile": label,
            "artifact_sha256": hashlib.sha256(payload).hexdigest(),
            "byte_length": len(payload),
            "file_name": path.name,
        })
    profiles = sorted(labels)
    missing_profiles = sorted(set(DEFAULT_REQUIRED_PROFILES) - labels)
    return {
        "schema_version": "1",
        "edition": edition,
        "issue_number": issue_number,
        "artifacts": entries,
        "profiles": profiles,
        "missing_profiles": missing_profiles,
        "required_checks": list(_REQUIRED_CHECKS),
        "owner_review": {
            "status": "pending",
            "approved_by": None,
            "approved_at": None,
            "approval_ref": None,
        },
        "limitations": [
            "Artifact hashing does not establish client rendering, human review, or publication approval.",
            "Live Outlook/projector evidence and the blind reviewer decisions remain external owner evidence.",
        ],
    }


def render_checklist(manifest: dict[str, Any]) -> str:
    issue_number = manifest["issue_number"]
    missing_profiles = manifest["missing_profiles"]
    artifacts = manifest["artifacts"]
    required_checks = manifest["required_checks"]
    assert isinstance(issue_number, int)
    assert isinstance(missing_profiles, list)
    assert isinstance(artifacts, list)
    assert isinstance(required_checks, list)
    lines = [
        "# RELO-W6 UAT enrollment",
        "",
        f"- Edition: `{manifest['edition']}`",
        f"- Issue: `{issue_number:03d}`",
        f"- Missing profiles: `{', '.join(str(value) for value in missing_profiles) or 'none'}`",
        "",
        "## Artifacts",
    ]
    for artifact in artifacts:
        assert isinstance(artifact, dict)
        lines.append(
            f"- [ ] `{artifact['profile']}` — `{artifact['artifact_sha256']}` "
            f"({artifact['byte_length']} bytes, `{artifact['file_name']}`)"
        )
    lines.extend(["", "## Required checks"])
    lines.extend(f"- [ ] {check}" for check in required_checks)
    lines.extend([
        "",
        "## Owner disposition",
        "- [ ] Owner reviewed each required profile.",
        "- [ ] Owner supplied a private approval reference.",
        "- [ ] Blind review findings were attached privately without copying PII into this checklist.",
    ])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edition", required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--artifact", action="append", nargs=2, metavar=("PROFILE", "PATH"), default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.issue <= 0:
        parser.error("--issue must be positive")
    try:
        manifest = prepare_manifest(
            [(label, Path(path)) for label, path in args.artifact],
            edition=args.edition,
            issue_number=args.issue,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "w6-uat-manifest.json"
    checklist_path = args.output_dir / "w6-uat-checklist.md"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checklist_path.write_text(render_checklist(manifest), encoding="utf-8")
    print(json.dumps({
        "manifest": str(manifest_path),
        "checklist": str(checklist_path),
        "missing_profiles": manifest["missing_profiles"],
    }, indent=2, sort_keys=True))
    return 0 if not manifest["missing_profiles"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    path = REPO_ROOT / "scripts" / "check_publication_content.py"
    spec = importlib.util.spec_from_file_location("check_publication_content", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_scan_rejects_high_confidence_secret_and_private_host() -> None:
    module = _load_module()
    findings = module.scan_lines(
        "src/example.py",
        [
            "token = 'ghp_abcdefghijklmnopqrstuvwxyz123456'",
            "index = 'https://packagefeedproxy.microsoft.io/pypi/simple/'",
        ],
    )
    assert any("secret" in finding for finding in findings)
    assert any("private/PII" in finding for finding in findings)


def test_scan_allows_synthetic_test_controls() -> None:
    module = _load_module()
    assert module.scan_lines(
        "tests/unit/example.py",
        [
            'owner = "synthetic@microsoft.com"',
            'connection = "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=abcdEFGH1234567890+/="',
            'sample = "contact owner@example.net or call (206) 555-0101"',
            'canary = "123-45-6789"',
        ],
    ) == []

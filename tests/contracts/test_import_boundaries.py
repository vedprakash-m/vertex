from __future__ import annotations

import ast
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "src" / "core"
FORBIDDEN_PREFIXES = ("src.ai", "src.m365", "src.commands")
FORBIDDEN_FEEDBACK_PREFIX = "src.core.feedback"
FORBIDDEN_PROVIDER_SDK_PREFIXES = (
    "atlassian",
    "azure",
    "google",
    "googleapiclient",
    "jira",
    "kusto",
    "microsoftgraph",
    "msal",
    "msgraph",
    "office365",
    "openai",
    "slack_sdk",
)


class ImportBoundaryTests(unittest.TestCase):
    def test_core_has_no_ai_m365_or_commands_imports(self) -> None:
        violations: list[str] = []
        for file_path in CORE_ROOT.rglob("*.py"):
            module = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            for node in ast.walk(module):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(FORBIDDEN_PREFIXES):
                            violations.append(f"{file_path}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith(FORBIDDEN_PREFIXES):
                        violations.append(f"{file_path}: from {node.module} import ...")
        self.assertEqual(violations, [])

    def test_core_outside_feedback_does_not_import_feedback_modules(self) -> None:
        violations: list[str] = []
        for file_path in CORE_ROOT.rglob("*.py"):
            relative = file_path.relative_to(CORE_ROOT)
            if relative.parts and relative.parts[0] == "feedback":
                continue
            module = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            for node in ast.walk(module):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(FORBIDDEN_FEEDBACK_PREFIX):
                            violations.append(f"{file_path}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith(FORBIDDEN_FEEDBACK_PREFIX):
                        violations.append(f"{file_path}: from {node.module} import ...")
        self.assertEqual(violations, [])

    def test_core_has_no_direct_provider_sdk_imports(self) -> None:
        violations: list[str] = []
        for file_path in CORE_ROOT.rglob("*.py"):
            module = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            for node in ast.walk(module):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(FORBIDDEN_PROVIDER_SDK_PREFIXES):
                            violations.append(f"{file_path}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith(FORBIDDEN_PROVIDER_SDK_PREFIXES):
                        violations.append(f"{file_path}: from {node.module} import ...")
        self.assertEqual(violations, [])


    def test_chart_cache_store_zone_boundary(self) -> None:
        """chart_cache_store.py must not import from src.ai or src.m365 — spec §7 / §11 contract."""
        chart_cache_path = CORE_ROOT / "chart_cache_store.py"
        if not chart_cache_path.exists():
            self.skipTest("chart_cache_store.py not found")
        module = ast.parse(chart_cache_path.read_text(encoding="utf-8"), filename=str(chart_cache_path))
        violations: list[str] = []
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(("src.ai", "src.m365")):
                        violations.append(f"chart_cache_store.py: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(("src.ai", "src.m365")):
                    violations.append(f"chart_cache_store.py: from {node.module} import ...")
        self.assertEqual(violations, [], "chart_cache_store.py must stay in Zone A (no AI/M365 imports)")

    def test_chart_renderers_zone_boundary(self) -> None:
        """All files under src/core/charts/ must not import from src.ai or src.m365 — spec §7 / §11 contract."""
        charts_root = CORE_ROOT / "charts"
        if not charts_root.exists():
            self.skipTest("src/core/charts/ directory not found")
        violations: list[str] = []
        for file_path in charts_root.rglob("*.py"):
            module = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            for node in ast.walk(module):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(("src.ai", "src.m365")):
                            violations.append(f"{file_path.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith(("src.ai", "src.m365")):
                        violations.append(f"{file_path.name}: from {node.module} import ...")
        self.assertEqual(violations, [], "src/core/charts/ files must stay in Zone A (no AI/M365 imports)")

    def test_chart_renderers_no_provider_sdks(self) -> None:
        """src/core/charts/ must not import provider SDKs directly — spec §7 contract."""
        charts_root = CORE_ROOT / "charts"
        if not charts_root.exists():
            self.skipTest("src/core/charts/ directory not found")
        violations: list[str] = []
        for file_path in charts_root.rglob("*.py"):
            module = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            for node in ast.walk(module):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(FORBIDDEN_PROVIDER_SDK_PREFIXES):
                            violations.append(f"{file_path.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith(FORBIDDEN_PROVIDER_SDK_PREFIXES):
                        violations.append(f"{file_path.name}: from {node.module} import ...")
        self.assertEqual(violations, [], "src/core/charts/ must not import provider SDKs")


    def test_inv_dm_6_zone_b_c_discovery_never_calls_event_write_api(self) -> None:
        """INV-DM-6: Zone B (src/ai/discovery/) and Zone C (src/m365/discovery/) must
        never import or call the event write API from src.core.ledger.event_log.
        Only Zone A (discovery_run_recorder) may write ledger events; Zones B/C
        append to candidate_store only and return DiscoveryRunResult objects (FR-12).
        """
        _EVENT_WRITE_SYMBOLS = frozenset({
            "write_event",
            "write_events_atomic",
            "index_event",
            "index_events",
            "append_jsonl_line",
            "append_jsonl_lines",
        })
        discovery_roots = [
            REPO_ROOT / "src" / "ai" / "discovery",
            REPO_ROOT / "src" / "m365" / "discovery",
        ]
        violations: list[str] = []
        for discovery_root in discovery_roots:
            if not discovery_root.exists():
                continue
            for file_path in discovery_root.rglob("*.py"):
                if file_path.name.startswith("_") and file_path.name != "__init__.py":
                    continue
                source = file_path.read_text(encoding="utf-8")
                module = ast.parse(source, filename=str(file_path))
                for node in ast.walk(module):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        if "event_log" in node.module:
                            imported_names = {alias.name for alias in node.names}
                            forbidden = imported_names & _EVENT_WRITE_SYMBOLS
                            if forbidden:
                                violations.append(
                                    f"{file_path.relative_to(REPO_ROOT)}: "
                                    f"imports event write symbol(s) {sorted(forbidden)} from {node.module}"
                                )
        self.assertEqual(
            violations,
            [],
            "Zone B/C discovery files must not import event write API (INV-DM-6); "
            "use candidate_store.append_candidate() instead.",
        )


    def test_workiq_retrieval_config_stays_in_zone_a(self) -> None:
        """WorkIQ retrieval config (vertex-tech-spec §13.1.1) must stay in
        Zone A (src/core/models_v2.py) with no src.m365 / src.ai / src.commands imports.

        The retrieval config is parsed by Zone A (edition_resolver._parse_m365) and read
        by Zone D (gather). If models_v2 imported the Zone-C prompt/validation helpers
        (build_structured_discovery_question / validate_structured_discovery_payload,
        which live in src/m365/workiq_ask_support.py), it would drag probabilistic
        WorkIQ machinery into the deterministic core — a contract violation that the
        general core test would catch, but this explicit test documents the deliberate
        Zone split and fails with a clear message if it regresses.
        """
        models_path = CORE_ROOT / "models_v2.py"
        self.assertTrue(models_path.exists(), "src/core/models_v2.py not found")
        module = ast.parse(models_path.read_text(encoding="utf-8"), filename=str(models_path))

        # The config types must be defined in Zone A.
        defined_names: set[str] = set()
        for node in ast.walk(module):
            if isinstance(node, ast.ClassDef) and node.name == "WorkIQRetrievalConfig":
                defined_names.add(node.name)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "WORKIQ_DISCOVERY_MODES":
                        defined_names.add(target.id)
        self.assertIn(
            "WorkIQRetrievalConfig",
            defined_names,
            "WorkIQRetrievalConfig must be defined in src/core/models_v2.py (Zone A)",
        )
        self.assertIn(
            "WORKIQ_DISCOVERY_MODES",
            defined_names,
            "WORKIQ_DISCOVERY_MODES must be defined in src/core/models_v2.py (Zone A)",
        )

        # And it must not reach into Zone C/B/D — the general core test covers this,
        # but assert workiq_ask_support specifically is not imported here.
        violations: list[str] = []
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "workiq_ask_support" in alias.name:
                        violations.append(f"models_v2.py: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if "workiq_ask_support" in node.module:
                    violations.append(f"models_v2.py: from {node.module} import ...")
        self.assertEqual(
            violations,
            [],
            "models_v2.py must not import src.m365.workiq_ask_support — "
            "retrieval config types stay pure in Zone A; prompts/validation live in Zone C.",
        )

    def test_workiq_structured_discovery_lives_in_zone_c(self) -> None:
        """Structured WorkIQ prompt, validation, and retrieval live in Zone C.

        A deliberate deviation from the original plan (which proposed a Zone-A
        validation module): validation is colocated with the JSON toolkit it shares
        helpers with, and imports only normalize_thread_id from src.core.m365_identifiers
        (Zone C -> Zone A is permitted). This test locks the location so a future
        refactor cannot silently relocate probabilistic/WorkIQ-coupled code into the
        deterministic core, and documents that the placement is intentional.
        """
        zone_c_path = REPO_ROOT / "src" / "m365" / "workiq_ask_support.py"
        self.assertTrue(zone_c_path.exists(), "src/m365/workiq_ask_support.py not found")
        source = zone_c_path.read_text(encoding="utf-8")
        module = ast.parse(source, filename=str(zone_c_path))

        defined_funcs: set[str] = set()
        for node in ast.walk(module):
            if isinstance(node, ast.FunctionDef):
                defined_funcs.add(node.name)
        self.assertIn(
            "build_structured_discovery_question",
            defined_funcs,
            "build_structured_discovery_question must live in src/m365/workiq_ask_support.py (Zone C)",
        )
        self.assertIn(
            "validate_structured_discovery_payload",
            defined_funcs,
            "validate_structured_discovery_payload must live in src/m365/workiq_ask_support.py (Zone C)",
        )

        # Zone C may import from Zone A (src.core.*) but must not import Zone B/D
        # (src.ai.*, src.commands.*) — those would invert the layering.
        violations: list[str] = []
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(("src.ai", "src.commands")):
                        violations.append(f"workiq_ask_support.py: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(("src.ai", "src.commands")):
                    violations.append(f"workiq_ask_support.py: from {node.module} import ...")
        self.assertEqual(
            violations,
            [],
            "src/m365/workiq_ask_support.py (Zone C) must not import src.ai or src.commands",
        )

        retriever_path = REPO_ROOT / "src" / "m365" / "workiq_retriever.py"
        retriever = ast.parse(retriever_path.read_text(encoding="utf-8"), filename=str(retriever_path))
        retriever_violations = []
        for node in ast.walk(retriever):
            module_name = node.module if isinstance(node, ast.ImportFrom) else None
            if module_name and module_name.startswith(("src.ai", "src.commands")):
                retriever_violations.append(module_name)
        self.assertEqual(retriever_violations, [], "Zone-C retriever must not import Zone B/D")


if __name__ == "__main__":
    unittest.main()

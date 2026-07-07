from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent, indent
import unittest
from unittest.mock import patch

import yaml

from src.core.config_loader import discover_report_editions, load_bundle_with_mode, load_editorial_rules, load_program_context, load_report_bundle, load_report_config
from src.core.config_loader import load_review_config
from src.core.edition_resolver import resolve_edition


def _write_demo_v2_layout(
    root: Path,
    *,
    edition_extra: str = "",
    m365_yaml: str = "  enabled: false\n  prefer_agency: true",
) -> tuple[Path, Path]:
    editions_root = root / "editions"
    programs_root = root / "programs"
    program_dir = programs_root / "demo"
    knowledge_dir = program_dir / "knowledge"
    editions_root.mkdir(parents=True)
    knowledge_dir.mkdir(parents=True)

    edition_extra_block = f"\n{edition_extra.strip()}" if edition_extra.strip() else ""
    m365_block = indent(dedent(m365_yaml).strip(), "  ")
    (editions_root / "demo_weekly.yaml").write_text(
        f"""
schema_version: "2.0"
id: demo_weekly
program_id: demo
name: "Demo Issue {{issue_number}}"
type: detailed
altitude: helicopter
cadence: weekly
layout_mode: dashboard
scorecard_sort: risk_desc
scorecard_plain_text_only: true
author:
  display_name: "Demo Author"
  email: "demo@example.com"
distribution:
  to: ["demo@example.com"]
  cc: []
  channels: ["email"]{edition_extra_block}
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "program.yaml").write_text(
        f"""
schema_version: "2.0"
id: demo
name: "Demo Program"
objective: "Ship safely"
mission: "Keep the program predictable."
current_phase: "validation"
pillars: ["Reliability"]
ado:
  organization: your-org
  project: One
  area_paths: []
  work_item_types: ["Feature"]
  excluded_states: ["Removed"]
  date_window_days: 14
  api_timeout_seconds: 30
ai:
  enabled: false
  budget_usd_per_run: 0.5
kusto:
  enabled: true
m365:
{m365_block}
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "workstreams.yaml").write_text(
        """
schema_version: "2.0"
workstreams:
  - id: ws_demo
    name: Demo WS
    area_paths: ['One\\Demo']
    dri_email: demo@example.com
    alternate_owner: backup@example.com
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "scorecards.yaml").write_text(
        """
schema_version: "2.0"
scorecards:
  - name: Demo Scorecard
    dimensions:
      - name: Demo Dimension
        workstream_id: ws_demo
        ado_filter: "area_path contains 'Demo'"
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "editorial_rules.yaml").write_text(
        """
schema_version: "1.0"
stale_warn_days: 14
stale_block_days: 30
banned_phrases: []
banned_openings: []
verbosity: {}
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "review.yaml").write_text(
        """
required: false
reviewers:
  - name: Reviewer
    sections: [exec_summary]
""".strip(),
        encoding="utf-8",
    )
    (knowledge_dir / "people_directory.yaml").write_text(
        """
schema_version: "1.0"
people:
  - alias: demo
    email: demo@example.com
    display_name: Demo Author
""".strip(),
        encoding="utf-8",
    )
    (knowledge_dir / "teams.yaml").write_text(
        'schema_version: "1.0"\nteams: []\n',
        encoding="utf-8",
    )
    (knowledge_dir / "products.yaml").write_text(
        'schema_version: "1.0"\nproducts: []\n',
        encoding="utf-8",
    )
    (knowledge_dir / "golden_queries.yaml").write_text(
        """
schema_version: "1.0"
queries:
  - id: velocity-p50
    cluster: https://adventure.kusto.windows.net
    database: xdataanalytics
    kql: Demo | take 1
    section: Demo Telemetry
    render_as: table
    confidence: high
    reference_url: https://adventure.kusto.windows.net
    program_ids: [demo]
    validated: true
""".strip(),
        encoding="utf-8",
    )
    return editions_root, programs_root


class ConfigLoaderTests(unittest.TestCase):
    def test_load_editorial_rules_parses_edition_specific_verbosity_limits(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "editorial_rules.yaml"
            path.write_text(
                dedent(
                    """
                    schema_version: "1.0"
                    stale_warn_days: 14
                    stale_block_days: 30
                    banned_phrases: []
                    banned_openings: []
                    voice_contract:
                      applies_to_editions: [acme_weekly]
                      program_tokens: [acme, northwind]
                      abstract_phrases: [materially narrower]
                      synthetic_delta_prefixes: [NEW, ETA]
                      decision_lead_terms: [blocking, checkpoint]
                      static_concrete_terms: [schie, azure core]
                      exec_summary_bucket_prefixes: ['acme:']
                      objective_preamble_prefixes: [the objective of the acme program is]
                    verbosity:
                      workstream_blurb_max_sentences: 4
                      workstream_blurb_max_words:
                        default: 90
                        narrative: 150
                      exec_summary_max_words:
                        default: 150
                        condensed: 75
                        deck: 100
                      exec_bullet_max_words: 25
                      exec_max_bullets: 3
                      scorecard_summary_max_sentences: 3
                    """
                ).strip(),
                encoding="utf-8",
            )

            rules = load_editorial_rules(path)

            self.assertEqual(rules.verbosity.workstream_blurb_max_words, 90)
            self.assertEqual(rules.verbosity.workstream_blurb_max_words_by_edition.narrative, 150)
            self.assertEqual(rules.verbosity.exec_summary_max_words, 150)
            self.assertEqual(rules.verbosity.exec_summary_max_words_by_edition.condensed, 75)
            self.assertEqual(rules.verbosity.exec_summary_max_words_by_edition.deck, 100)
            self.assertEqual(rules.voice_contract.applies_to_editions, ("acme_weekly",))
            self.assertEqual(rules.voice_contract.program_tokens, ("acme", "northwind"))
            self.assertEqual(rules.voice_contract.synthetic_delta_prefixes, ("NEW", "ETA"))

    def test_resolve_edition_parses_dependency_ado_queries(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            editions_root, programs_root = _write_demo_v2_layout(root)
            (programs_root / "demo" / "workstreams.yaml").write_text(
                """
schema_version: "2.0"
workstreams:
  - id: ws_demo
    name: Demo WS
    area_paths: ['One\\Demo']
    dri_email: demo@example.com
    signal_sources:
      dependency_ado_queries:
        - label: OneDeploy stager
          area_path: One\\Azure Compute\\OneDeploy\\Stager
          resolution_path: cross_org_onedeploy
        - label: SCHIE gap owners
          work_item_ids: [1001, 1002]
          resolution_path: cross_org_compute_pf
""".strip(),
                encoding="utf-8",
            )

            resolved = resolve_edition(
                "demo_weekly",
                editions_root=editions_root,
                programs_root=programs_root,
            )

            self.assertIsNotNone(resolved)
            assert resolved is not None
            signal_sources = resolved.workstreams[0].signal_sources
            self.assertIsNotNone(signal_sources)
            assert signal_sources is not None
            self.assertEqual(len(signal_sources.dependency_ado_queries), 2)
            self.assertEqual(signal_sources.dependency_ado_queries[0].label, "OneDeploy stager")
            self.assertEqual(
                signal_sources.dependency_ado_queries[0].area_path,
                "One\\Azure Compute\\OneDeploy\\Stager",
            )
            self.assertEqual(
                signal_sources.dependency_ado_queries[1].work_item_ids,
                (1001, 1002),
            )

    def test_resolve_edition_parses_m365_signal_source_work_item_ids(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            editions_root, programs_root = _write_demo_v2_layout(root)
            (programs_root / "demo" / "workstreams.yaml").write_text(
                """
schema_version: "2.0"
workstreams:
  - id: ws_demo
    name: Demo WS
    area_paths: ['One\\Demo']
    dri_email: demo@example.com
    signal_sources:
      teams_meeting_series:
        - display_name: Weekly sync
          series_id: series-1
          work_item_ids: [1001, 1002]
      teams_chats:
        - display_name: Chat
          thread_id: thread-1
          work_item_ids: [2001]
      email_threads:
        - display_name: Thread
          thread_id: email-1
          work_item_ids: [3001, 3002]
""".strip(),
                encoding="utf-8",
            )

            resolved = resolve_edition(
                "demo_weekly",
                editions_root=editions_root,
                programs_root=programs_root,
            )

            assert resolved is not None
            signal_sources = resolved.workstreams[0].signal_sources
            assert signal_sources is not None
            self.assertEqual(signal_sources.teams_meeting_series[0].work_item_ids, (1001, 1002))
            self.assertEqual(signal_sources.teams_chats[0].work_item_ids, (2001,))
            self.assertEqual(signal_sources.email_threads[0].work_item_ids, (3001, 3002))

    def test_resolve_edition_parses_workstream_ado_repository_ids(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            editions_root, programs_root = _write_demo_v2_layout(root)
            (programs_root / "demo" / "workstreams.yaml").write_text(
                """
schema_version: "2.0"
workstreams:
  - id: ws_demo
    name: Demo WS
    area_paths: ['One\\Demo']
    ado_repository_ids: [repo-alpha, repo-beta]
    dri_email: demo@example.com
""".strip(),
                encoding="utf-8",
            )

            resolved = resolve_edition(
                "demo_weekly",
                editions_root=editions_root,
                programs_root=programs_root,
            )

            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.workstreams[0].ado_repository_ids, ("repo-alpha", "repo-beta"))

    def test_resolve_edition_reads_workstreams_via_fact_store_and_preserves_yaml_order(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            editions_root, programs_root = _write_demo_v2_layout(root)
            (programs_root / "demo" / "workstreams.yaml").write_text(
                """
schema_version: "2.0"
workstreams:
  - id: ws_beta
    name: Beta WS
    area_paths: ['One\\Beta']
    dri_email: beta@example.com
  - id: ws_alpha
    name: Alpha WS
    area_paths: ['One\\Alpha']
    dri_email: alpha@example.com
""".strip(),
                encoding="utf-8",
            )

            baseline = resolve_edition(
                "demo_weekly",
                editions_root=editions_root,
                programs_root=programs_root,
            )

            self.assertIsNotNone(baseline)
            assert baseline is not None
            reversed_workstreams = tuple(reversed(baseline.workstreams))

            with patch(
                "src.core.program_fact_store.load_current_workstreams",
                return_value=reversed_workstreams,
            ) as load_current_workstreams_mock:
                resolved = resolve_edition(
                    "demo_weekly",
                    editions_root=editions_root,
                    programs_root=programs_root,
                )

            self.assertIsNotNone(resolved)
            assert resolved is not None
            load_current_workstreams_mock.assert_called_once_with("demo", programs_root=programs_root)
            self.assertEqual(tuple(workstream.id for workstream in resolved.workstreams), ("ws_beta", "ws_alpha"))
            self.assertEqual(
                tuple(entry["id"] for entry in resolved.raw_workstreams["workstreams"]),
                ("ws_beta", "ws_alpha"),
            )

    def test_resolve_edition_parses_program_golden_query_activation_ids(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            editions_root, programs_root = _write_demo_v2_layout(root)
            program_path = programs_root / "demo" / "program.yaml"
            program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
            program_doc["golden_queries"] = ["fabrikam-xhealth-m0"]
            program_path.write_text(
                yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
            )

            resolved = resolve_edition(
                "demo_weekly",
                editions_root=editions_root,
                programs_root=programs_root,
            )

            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.program.golden_queries, ("fabrikam-xhealth-m0",))

    def test_load_report_config_resolves_linked_scorecard_dimension_filters(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            editions_root, programs_root = _write_demo_v2_layout(root)
            (programs_root / "demo" / "scorecards.yaml").write_text(
                """
schema_version: "2.0"
scorecards:
  - name: Adventure Readiness
    dimensions:
      - name: Deployment Velocity
        workstream_id: ws_demo
        description: Shared deployment evidence
        ado_filter: "tag contains 'Deployment'"
  - name: Contoso Pilot Readiness
    dimensions:
      - name: Deployment
        workstream_id: ws_demo
        linked_scorecard: Adventure Readiness
        linked_dimension: Deployment Velocity
""".strip(),
                encoding="utf-8",
            )

            resolved = resolve_edition(
                "demo_weekly",
                editions_root=editions_root,
                programs_root=programs_root,
            )

            self.assertIsNotNone(resolved)
            assert resolved is not None
            linked_dimension = resolved.scorecards[1].dimensions[0]
            self.assertEqual(linked_dimension.linked_scorecard_name, "Adventure Readiness")
            self.assertEqual(linked_dimension.linked_dimension_name, "Deployment Velocity")

            config = load_report_config(
              "demo_weekly",
              editions_root=editions_root,
              programs_root=programs_root,
            )

            linked_settings = config.scorecards[1].dimensions[0]
            self.assertEqual(linked_settings.linked_scorecard_name, "Adventure Readiness")
            self.assertEqual(linked_settings.linked_dimension_name, "Deployment Velocity")
            self.assertEqual(linked_settings.description, "Shared deployment evidence")
            self.assertEqual(linked_settings.ado_filter, "tag contains 'Deployment'")

    def test_load_bundle_with_mode_includes_canonical_ddpf_diag_dimension_in_live_nova_config(self) -> None:
      from src.core.edition_resolver import EDITIONS_ROOT
      if not (EDITIONS_ROOT / "acme_weekly.yaml").exists():
          self.skipTest("Requires local edition data")
      bundle = load_report_bundle("acme_weekly")

      ddpf_scorecard = next(
        scorecard for scorecard in bundle.config.scorecards if scorecard.name == "Contoso Pilot Readiness"
      )
      dimension_names = tuple(dimension.name for dimension in ddpf_scorecard.dimensions)

      self.assertIn("Deploy. Velocity", dimension_names)
      self.assertIn("Repairs & Safety", dimension_names)
      self.assertEqual(len(dimension_names), 9)

    def test_discover_report_editions_returns_nova(self) -> None:
        from src.core.edition_resolver import EDITIONS_ROOT
        if not (EDITIONS_ROOT / "acme_weekly.yaml").exists():
            self.skipTest("Requires local edition data")
        self.assertEqual(
            discover_report_editions(),
            ("armada_nudge", "fabrikam_weekly", "nova_daily", "nova_lt_deck", "nova_nudge", "nova_quarterly", "acme_weekly"),
        )

    def test_load_bundle_with_mode_reads_new_v2_nova_editions(self) -> None:
        from src.core.edition_resolver import EDITIONS_ROOT
        if not (EDITIONS_ROOT / "nova_daily.yaml").exists():
            self.skipTest("Requires local edition data")
        daily = load_bundle_with_mode("nova_daily")
        deck = load_bundle_with_mode("nova_lt_deck")
        quarterly = load_bundle_with_mode("nova_quarterly")
        fabrikam = load_bundle_with_mode("fabrikam_weekly")

        self.assertEqual(daily.mode, "v2")
        self.assertEqual(daily.bundle.config.edition.type, "condensed")
        self.assertEqual(deck.mode, "v2")
        self.assertEqual(deck.bundle.config.edition.type, "deck")
        self.assertEqual(quarterly.mode, "v2")
        self.assertEqual(quarterly.bundle.config.edition.type, "lookback")
        self.assertEqual(quarterly.bundle.config.edition.cadence, "quarterly")
        self.assertEqual(quarterly.bundle.config.ado.date_window_days, 91)
        self.assertEqual(fabrikam.mode, "v2")
        self.assertEqual(fabrikam.bundle.config.edition.type, "narrative")
        self.assertEqual(fabrikam.bundle.config.layout_mode, "dashboard")
        self.assertEqual(fabrikam.bundle.program_context.program_name, "Fabrikam")
        self.assertEqual(fabrikam.bundle.config.scorecards[0].name, "Fabrikam Weekly Update")

    def test_load_report_config_returns_expected_shape(self) -> None:
        from src.core.edition_resolver import EDITIONS_ROOT
        if not (EDITIONS_ROOT / "acme_weekly.yaml").exists():
            self.skipTest("Requires local edition data")
        config = load_report_config("acme_weekly")
        self.assertEqual(config.edition.name, "acme_weekly")
        self.assertEqual(config.edition.type, "detailed")
        self.assertEqual(config.layout_mode, "dashboard")
        self.assertEqual(config.ado.organization, "msazure")
        self.assertEqual(config.ado.api_timeout_seconds, 60)
        self.assertEqual(config.ado_fetch_timeout_seconds, 60)
        self.assertTrue(config.forecast_enabled)
        self.assertIsNone(config.mobile_safe_scorecards)
        self.assertEqual(config.scorecard_sort, "risk_desc")
        self.assertTrue(config.scorecard_plain_text_only)
        self.assertEqual(config.brand_name, "Program Hygiene")
        self.assertIsNone(config.brand_header_url)
        self.assertIsNone(config.cadence_note)
        self.assertEqual(len(config.scorecards), 2)
        self.assertIn(
          "Deployment Velocity",
          [dimension.name for dimension in config.scorecards[0].dimensions],
        )
        self.assertTrue(config.kusto.enabled)
        self.assertEqual(len(config.kusto.queries), 8)
        self.assertEqual(config.kusto.queries[0].id, "fleet-health")
        self.assertEqual(config.kusto.queries[0].render_as, "table")
        self.assertFalse(config.kusto.queries[0].kusto_section_validates_slice)
        self.assertTrue(config.m365.enabled)
        self.assertTrue(config.m365.prefer_agency)
        self.assertIn("newsletter_search", dir(config.m365.workiq))
        self.assertTrue(config.m365.workiq.newsletter_search)
        self.assertEqual(config.m365.bluebird.teams_channels, ("xInfraSWPM: Acme Weekly",))
        self.assertEqual(config.m365.bluebird.lookback_days, 7)
        self.assertEqual(config.m365.offline.transcript_dir, "backfill/transcripts/")

    def test_load_report_config_reads_m365_settings(self) -> None:
        with TemporaryDirectory() as temp_dir:
            editions_root, programs_root = _write_demo_v2_layout(
                Path(temp_dir),
                m365_yaml="""
  enabled: true
  prefer_agency: false
  workiq:
    newsletter_search: "demo newsletters"
    feedback_search: "demo feedback"
    teams_search: "demo teams"
  bluebird:
    teams_channels:
      - "demo: channel"
      - "demo: leadership"
    lookback_days: 21
  offline:
    newsletter_dir: "offline/emails"
    transcript_dir: "offline/transcripts"
""",
            )
            config = load_report_config(
                "demo_weekly",
                editions_root=editions_root,
                programs_root=programs_root,
            )

        self.assertTrue(config.m365.enabled)
        self.assertFalse(config.m365.prefer_agency)
        self.assertEqual(config.m365.workiq.newsletter_search, "demo newsletters")
        self.assertEqual(config.m365.workiq.feedback_search, "demo feedback")
        self.assertEqual(config.m365.workiq.teams_search, "demo teams")
        self.assertEqual(config.m365.bluebird.teams_channels, ("demo: channel", "demo: leadership"))
        self.assertEqual(config.m365.bluebird.lookback_days, 21)
        self.assertEqual(config.m365.offline.newsletter_dir, "offline/emails")
        self.assertEqual(config.m365.offline.transcript_dir, "offline/transcripts")

    def test_resolve_edition_includes_live_ddpf_saved_query_ids(self) -> None:
      from src.core.edition_resolver import EDITIONS_ROOT
      if not (EDITIONS_ROOT / "acme_weekly.yaml").exists():
          self.skipTest("Requires local edition data")
      resolved = resolve_edition("acme_weekly")

      assert resolved is not None
      ddpf_workstream = next(
        workstream for workstream in resolved.workstreams if workstream.id == "dd_on_pf"
        )

      self.assertGreaterEqual(len(ddpf_workstream.ado_saved_query_ids), 4)
      self.assertIn("439c9110-f94f-4801-874f-1ed70791f9c8", ddpf_workstream.ado_saved_query_ids)
      self.assertIn("9de9b658-5bef-45db-9984-b1fa9d0fc0c5", ddpf_workstream.ado_saved_query_ids)

    def test_load_report_config_reads_timeout_alias_and_ux_flags(self) -> None:
        with TemporaryDirectory() as temp_dir:
            editions_root, programs_root = _write_demo_v2_layout(
                Path(temp_dir),
                edition_extra="""
ado_fetch_timeout_seconds: 45
forecast_enabled: true
mobile_safe_scorecards: row
""",
            )
            config = load_report_config(
                "demo_weekly",
                editions_root=editions_root,
                programs_root=programs_root,
            )

        self.assertEqual(config.ado_fetch_timeout_seconds, 45)
        self.assertEqual(config.ado.api_timeout_seconds, 45)
        self.assertTrue(config.forecast_enabled)
        self.assertEqual(config.mobile_safe_scorecards, "row")

    def test_resolve_edition_applies_ado_fetch_timeout_override_to_program_ado_config(self) -> None:
        with TemporaryDirectory() as temp_dir:
            editions_root, programs_root = _write_demo_v2_layout(
                Path(temp_dir),
                edition_extra="""
ado_fetch_timeout_seconds: 45
""",
            )
            resolved = resolve_edition(
                "demo_weekly",
                editions_root=editions_root,
                programs_root=programs_root,
            )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertIsNotNone(resolved.program.ado)
        assert resolved.program.ado is not None
        self.assertEqual(resolved.edition.ado_fetch_timeout_seconds, 45)
        self.assertEqual(resolved.program.ado.api_timeout_seconds, 45)

    def test_load_report_config_reads_ado_proposal_ttl_hours(self) -> None:
        with TemporaryDirectory() as temp_dir:
            editions_root, programs_root = _write_demo_v2_layout(Path(temp_dir))
            program_path = programs_root / "demo" / "program.yaml"
            program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
            assert isinstance(program_doc, dict)
            ado_block = program_doc.setdefault("ado", {})
            assert isinstance(ado_block, dict)
            ado_block["proposal_ttl_hours"] = 96
            program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

            config = load_report_config(
                "demo_weekly",
                editions_root=editions_root,
                programs_root=programs_root,
            )

        self.assertEqual(config.ado.proposal_ttl_hours, 96)

    def test_load_report_config_substitutes_present_env_values(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "VERTEX_AI_DEPLOYMENT": "blurb-model",
                "VERTEX_EXEC_DEPLOYMENT": "summary-model",
            },
            clear=False,
        ):
            from src.core.edition_resolver import EDITIONS_ROOT
            if not (EDITIONS_ROOT / "acme_weekly.yaml").exists():
                self.skipTest("Requires local edition data")
            config = load_report_config("acme_weekly")
        self.assertEqual(config.ai.blurb_deployment, "blurb-model")
        self.assertEqual(config.ai.exec_summary_deployment, "summary-model")

    def test_load_report_config_reads_backup_deployments(self) -> None:
        with TemporaryDirectory() as temp_dir:
            editions_root, programs_root = _write_demo_v2_layout(Path(temp_dir))
            program_path = programs_root / "demo" / "program.yaml"
            program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
            assert isinstance(program_doc, dict)
            ai_block = program_doc.setdefault("ai", {})
            assert isinstance(ai_block, dict)
            ai_block["enabled"] = True
            ai_block["blurb_deployment"] = "primary-blurb"
            ai_block["blurb_backup_deployment"] = "backup-blurb"
            ai_block["exec_summary_deployment"] = "primary-exec"
            ai_block["exec_summary_backup_deployment"] = "backup-exec"
            program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

            config = load_report_config(
                "demo_weekly",
                editions_root=editions_root,
                programs_root=programs_root,
            )

        self.assertEqual(config.ai.blurb_deployment, "primary-blurb")
        self.assertEqual(config.ai.blurb_backup_deployment, "backup-blurb")
        self.assertEqual(config.ai.exec_summary_deployment, "primary-exec")
        self.assertEqual(config.ai.exec_summary_backup_deployment, "backup-exec")

    def test_load_report_bundle_includes_program_context_and_rules(self) -> None:
        from src.core.edition_resolver import EDITIONS_ROOT
        if not (EDITIONS_ROOT / "acme_weekly.yaml").exists():
            self.skipTest("Requires local edition data")
        bundle = load_report_bundle("acme_weekly")
        self.assertIsNotNone(bundle.program_context)
        self.assertIsNotNone(bundle.template_contract)
        self.assertIsNotNone(bundle.slice_contracts)
        self.assertIsNotNone(bundle.chapter_contract)
        assert bundle.program_context is not None
        assert bundle.template_contract is not None
        assert bundle.slice_contracts is not None
        assert bundle.chapter_contract is not None
        self.assertEqual(bundle.program_context.program_name, "Acme Platform Migration")
        self.assertEqual(bundle.editorial_rules.stale_warn_days, 14)
        self.assertEqual(bundle.editorial_rules.stale_block_days, 30)
        self.assertIn("due to", bundle.editorial_rules.banned_phrases)
        self.assertEqual(bundle.config.kusto.queries[2].id, "icm-mttr")
        self.assertFalse(bundle.config.kusto.queries[2].kusto_section_validates_slice)
        self.assertTrue(bundle.review.required)
        self.assertEqual(bundle.review.reviewers[0].name, "Lead PM")
        focused = bundle.template_contract.family_for("focused")
        condensed = bundle.template_contract.family_for("condensed")
        deck = bundle.template_contract.family_for("deck")
        lookback = bundle.template_contract.family_for("lookback")
        nudge = bundle.template_contract.family_for("nudge")
        self.assertIsNotNone(focused)
        self.assertIsNotNone(condensed)
        self.assertIsNotNone(deck)
        self.assertIsNotNone(lookback)
        self.assertIsNotNone(nudge)
        assert focused is not None
        self.assertEqual(focused.order[0], "health")
        self.assertEqual(focused.rules["selected_changes"].render_only_if, "baseline_available")
        self.assertEqual(
            len(bundle.slice_contracts),
            sum(len(scorecard.dimensions) for scorecard in bundle.config.scorecards),
        )
        contracts = {(contract.scorecard_name, contract.title): contract for contract in bundle.slice_contracts}
        self.assertIn(("Contoso Pilot Readiness", "XSSE Ops"), contracts)
        # XSSE Ops is a linked dimension (inherits filter from Acme); blank filter is not an error
        self.assertFalse(contracts[("Contoso Pilot Readiness", "XSSE Ops")].degradation.blank_filter_is_error)
        self.assertEqual(
            contracts[("Acme Adventure/XIO 100% Ramp Readiness", "Deployment Velocity")].source_of_truth,
            "hybrid",
        )
        telemetry = contracts[("Acme Adventure/XIO 100% Ramp Readiness", "Deployment Velocity")].source_contract.telemetry
        self.assertIsNotNone(telemetry)
        assert telemetry is not None
        self.assertEqual(telemetry.query_id, "velocity-p50")
        self.assertEqual(telemetry.expected_grain, "daily")
        chapters = bundle.chapter_contract.chapters_for("focused")
        self.assertEqual(chapters[0].id, "schie_map_day_gaps")
        self.assertEqual(chapters[1].id, "dd_data_control_plane")
        self.assertEqual(bundle.chapter_contract.chapters_for("deck")[0].id, "schie_map_day_gaps")
        self.assertEqual(
            bundle.chapter_contract.resolve_dimension("acme.schie_gaps"),
            ("Acme Adventure/XIO 100% Ramp Readiness", "SCHIE Gaps"),
        )
        self.assertIn("acme.xsse_ops", bundle.chapter_contract.unmapped_dimensions)
        self.assertIn("acme.repairs_safety", bundle.chapter_contract.unmapped_dimensions)

    def test_load_bundle_with_mode_rejects_missing_edition(self) -> None:
      self.assertRaises(FileNotFoundError, load_bundle_with_mode, "removed_edition")

    def test_load_bundle_with_mode_synthesizes_v2_layout(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reports_root = root / "reports"
            editions_root, programs_root = _write_demo_v2_layout(root)
            result = load_bundle_with_mode(
                "demo_weekly",
                reports_root=reports_root,
                editions_root=editions_root,
                programs_root=programs_root,
            )

        self.assertEqual(result.mode, "v2")
        self.assertEqual(result.bundle.config.edition.name, "demo_weekly")
        self.assertEqual(result.bundle.config.ado.area_paths, ("One\\Demo",))
        self.assertEqual(len(result.bundle.config.kusto.queries), 1)
        self.assertEqual(result.bundle.config.kusto.queries[0].id, "velocity-p50")
        self.assertIsNotNone(result.bundle.program_context)
        assert result.bundle.program_context is not None
        self.assertEqual(result.bundle.program_context.program_name, "Demo Program")
        self.assertEqual(result.bundle.program_context.workstreams[0].alternate_owner, "backup@example.com")

    def test_load_program_context_reads_alternate_owner(self) -> None:
        with TemporaryDirectory() as temp_dir:
            program_context_path = Path(temp_dir) / "program_context.yaml"
            program_context_path.write_text(
                """
schema_version: "1.0"
program_name: "Example Program"
objective: "Keep routing metadata together."
workstreams:
  - name: "Acme"
    area_paths:
      - One\\Adventure\\Acme
    dri_email: "primary@example.com"
    alternate_owner: "backup@example.com"
people: []
""".strip(),
                encoding="utf-8",
            )

            program_context = load_program_context(program_context_path)

        self.assertEqual(program_context.workstreams[0].alternate_owner, "backup@example.com")

    def test_load_program_context_reads_leadership_and_style_fields(self) -> None:
        with TemporaryDirectory() as temp_dir:
            program_context_path = Path(temp_dir) / "program_context.yaml"
            program_context_path.write_text(
                """
schema_version: "1.0"
program_name: "Example Program"
objective: "Keep leadership context together."
workstreams: []
people: []
leadership_readers:
  - name: "Executive Reader"
    role: "PM Lead"
    cares_about:
      - "accuracy"
      - "exec summary quality"
    prefers: "Lead with wins + deltas."
    pet_peeves:
      - "verbosity"
recurring_themes:
  - "SCHIE Gaps"
  - "Deployment Velocity"
writing_style:
  voice: "Confident but honest."
  structure: "Wins first, then risks."
tone_calibration:
  overall: "concern"
  per_theme_override:
    Deployment Velocity: "strong"
""".strip(),
                encoding="utf-8",
            )

            program_context = load_program_context(program_context_path)

        self.assertEqual(program_context.leadership_readers[0].name, "Executive Reader")
        self.assertEqual(program_context.leadership_readers[0].cares_about, ("accuracy", "exec summary quality"))
        self.assertIsNotNone(program_context.writing_style)
        assert program_context.writing_style is not None
        self.assertEqual(program_context.writing_style.voice, "Confident but honest.")
        self.assertEqual(program_context.recurring_themes, ("SCHIE Gaps", "Deployment Velocity"))
        self.assertIsNotNone(program_context.tone_calibration)
        assert program_context.tone_calibration is not None
        self.assertEqual(program_context.tone_calibration.overall, "concern")
        self.assertEqual(program_context.tone_calibration.per_theme_override["Deployment Velocity"], "strong")

    def test_load_program_context_reads_deeper_program_knowledge_fields(self) -> None:
        with TemporaryDirectory() as temp_dir:
            program_context_path = Path(temp_dir) / "program_context.yaml"
            program_context_path.write_text(
                """
schema_version: "1.0"
program:
  name: "Acme"
  objective: "Track ramp readiness."
  why_it_matters: "The ramp depends on clean dependency closure."
  current_phase: "100% Ramp readiness gating"
  key_dependency_chain:
    - from: "SCHIE commitments"
      to: "Acme Ramp"
      impact: "Ramp cannot proceed until SCHIE gaps are resolved"
workstreams:
  - name: "SCHIE Gaps"
    aliases: ["schie"]
    area_paths: ['One\\Adventure\\SCHIE']
    dri_email: "owner@example.com"
    why_it_matters: "Blocks the ramp decision."
    history_summary: "High risk for 3 consecutive issues."
    leadership_sensitivity: "critical"
    current_blocker: "Awaiting LT sign-off"
people:
  people: []
  leadership_readers:
    - name: "Jordan Lee"
      role: "Director"
      cares_about: ["ramp timeline"]
  workstream_owners:
    - name: "Isaiah Gregory"
      areas: ["Deployment", "OS"]
      style_note: "Needs editing for exec audience"
      timezone: "America/Los_Angeles"
      alternate: "Sebastian Rios"
writing_style:
  voice: "Confident but honest."
  structure: "Wins first, then risks."
  risk_framing:
    stuck: "Acknowledge directly. State blocker. Name next action + ETA."
    escalation: "Clear ask + deadline + who decides."
  preferred_patterns:
    - "Blocked on {team}; mitigation: {action} by {date}"
""".strip(),
                encoding="utf-8",
            )

            program_context = load_program_context(program_context_path)

        self.assertEqual(program_context.program_name, "Acme")
        self.assertEqual(program_context.current_phase, "100% Ramp readiness gating")
        self.assertEqual(program_context.key_dependency_chain[0].source, "SCHIE commitments")
        self.assertEqual(program_context.key_dependency_chain[0].target, "Acme Ramp")
        self.assertEqual(program_context.workstreams[0].why_it_matters, "Blocks the ramp decision.")
        self.assertEqual(program_context.workstreams[0].history_summary, "High risk for 3 consecutive issues.")
        self.assertEqual(program_context.workstreams[0].leadership_sensitivity, "critical")
        self.assertEqual(program_context.workstreams[0].current_blocker, "Awaiting LT sign-off")
        self.assertEqual(program_context.workstream_owners[0].name, "Isaiah Gregory")
        self.assertEqual(program_context.workstream_owners[0].areas, ("Deployment", "OS"))
        self.assertIsNotNone(program_context.writing_style)
        assert program_context.writing_style is not None
        self.assertEqual(
            program_context.writing_style.risk_framing["stuck"],
            "Acknowledge directly. State blocker. Name next action + ETA.",
        )
        self.assertEqual(
            program_context.writing_style.preferred_patterns,
            ("Blocked on {team}; mitigation: {action} by {date}",),
        )

    def test_load_review_config_reads_reviewer_assignments(self) -> None:
        with TemporaryDirectory() as temp_dir:
            review_path = Path(temp_dir) / "review.yaml"
            review_path.write_text(
                """
reviewers:
  - name: "Lead PM"
    sections:
      - exec_summary
      - ws:deployment
required: true
""".strip(),
                encoding="utf-8",
            )

            review = load_review_config(review_path)

        self.assertTrue(review.required)
        self.assertEqual(review.reviewers[0].sections, ("exec_summary", "ws:deployment"))

    def test_discover_report_editions_excludes_tracked_example_templates(self) -> None:
        from src.core.edition_resolver import EDITIONS_ROOT

        if not (EDITIONS_ROOT / "example_tpm_weekly.yaml").exists():
            self.skipTest("Requires tracked example edition")

        discovered = discover_report_editions()

        self.assertNotIn("example_tpm_weekly", discovered)

    def test_load_bundle_with_mode_loads_tracked_example_template_bundle(self) -> None:
        from src.core.edition_resolver import PROGRAMS_ROOT, find_edition_yaml

        edition_path = find_edition_yaml("example_tpm_weekly", programs_root=PROGRAMS_ROOT)
        if not edition_path.exists():
            self.skipTest("Requires tracked example edition")

        result = load_bundle_with_mode(
            "example_tpm_weekly",
            programs_root=PROGRAMS_ROOT,
        )

        self.assertEqual(result.mode, "v2")
        self.assertEqual(result.bundle.program_id, "example_tpm")
        self.assertEqual(result.bundle.config.edition.name, "example_tpm_weekly")
        self.assertEqual(result.bundle.config.ado.organization, "your-org")
        self.assertEqual(result.bundle.config.ado.area_paths, ("One\\Example\\Delivery",))
        self.assertFalse(result.bundle.config.kusto.enabled)
        self.assertIsNotNone(result.bundle.program_context)
        assert result.bundle.program_context is not None
        self.assertEqual(result.bundle.program_context.program_name, "Example TPM Program")
        self.assertEqual(result.bundle.program_context.workstreams[0].name, "Delivery Readiness")


if __name__ == "__main__":
    unittest.main()

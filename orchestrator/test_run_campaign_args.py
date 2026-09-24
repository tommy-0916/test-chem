from __future__ import annotations

import unittest

import run_campaign


class RunCampaignArgsTest(unittest.TestCase):
    def test_device_repair_resume_flags_do_not_require_query_at_parse_time(self) -> None:
        args = run_campaign.build_parser().parse_args(
            [
                "--resume-device-repair",
                "device_repair_request.json",
                "--device-plan-override",
                "device_plan_override.json",
            ]
        )

        self.assertIsNone(args.query)
        self.assertEqual(
            args.resume_device_repair,
            "device_repair_request.json",
        )
        self.assertEqual(
            args.device_plan_override,
            "device_plan_override.json",
        )

    def test_safe_connected_planning_defaults_are_forwarded(self) -> None:
        args = run_campaign.build_parser().parse_args(["--query", "test query"])

        research_args, device_args = run_campaign.build_step_args(args)

        self.assertEqual(args.max_iterations, 12)
        self.assertIn("--include-device-context", research_args)
        self.assertIn("--online-literature", research_args)
        self.assertIn("--web-search", research_args)
        self.assertIn("--full-workstations", device_args)
        self.assertEqual(
            research_args[research_args.index("--contract-version") + 1],
            "v2",
        )
        self.assertEqual(
            device_args[device_args.index("--contract-version") + 1],
            "v2",
        )

    def test_explicit_v1_is_forwarded_to_both_agents(self) -> None:
        args = run_campaign.build_parser().parse_args(
            ["--query", "test query", "--contract-version", "v1"]
        )

        research_args, device_args = run_campaign.build_step_args(args)

        self.assertEqual(
            research_args[research_args.index("--contract-version") + 1],
            "v1",
        )
        self.assertEqual(
            device_args[device_args.index("--contract-version") + 1],
            "v1",
        )

    def test_new_campaign_rejects_explicit_network_opt_out(self) -> None:
        args = run_campaign.build_parser().parse_args(
            ["--query", "test query", "--no-online-literature", "--no-web-search"]
        )

        with self.assertRaisesRegex(ValueError, "online scholarly retrieval"):
            run_campaign.build_step_args(args)

    def test_new_campaign_accepts_explicit_local_knowledge_mode(self) -> None:
        args = run_campaign.build_parser().parse_args(
            [
                "--query",
                "test query",
                "--knowledge-base-dir",
                "reaserch_agent/chem_kb",
                "--no-online-literature",
                "--no-web-search",
            ]
        )

        research_args, _ = run_campaign.build_step_args(args)

        self.assertIn("--knowledge-base-dir", research_args)
        self.assertIn("--no-online-literature", research_args)
        self.assertIn("--no-web-search", research_args)
        self.assertNotIn("--online-literature", research_args)
        self.assertNotIn("--web-search", research_args)

    def test_programmatic_false_flags_cannot_bypass_retrieval_invariant(self) -> None:
        args = run_campaign.build_parser().parse_args(["--query", "test query"])
        args.online_literature = False

        with self.assertRaisesRegex(ValueError, "online scholarly retrieval"):
            run_campaign.build_step_args(args)

        args = run_campaign.build_parser().parse_args(["--query", "test query"])
        args.web_search = False
        with self.assertRaisesRegex(ValueError, "open-web retrieval"):
            run_campaign.build_step_args(args)

    def test_device_repair_resume_does_not_require_research_retrieval(self) -> None:
        args = run_campaign.build_parser().parse_args(
            [
                "--resume-device-repair",
                "device_repair_request.json",
                "--device-plan-override",
                "device_plan_override.json",
                "--no-online-literature",
                "--no-web-search",
            ]
        )

        research_args, _ = run_campaign.build_step_args(args)

        self.assertIn("--no-online-literature", research_args)
        self.assertIn("--no-web-search", research_args)

    def test_llm_timeout_is_forwarded_to_both_agents(self) -> None:
        args = run_campaign.build_parser().parse_args(
            [
                "--query",
                "test query",
                "--wire-api",
                "codex_responses",
                "--llm-timeout-seconds",
                "900",
            ]
        )

        research_args, device_args = run_campaign.build_step_args(args)

        self.assertIn("--llm-timeout-seconds", research_args)
        self.assertEqual(
            research_args[research_args.index("--llm-timeout-seconds") + 1],
            "900",
        )
        self.assertIn("--timeout-seconds", device_args)
        self.assertEqual(
            device_args[device_args.index("--timeout-seconds") + 1],
            "900",
        )

    def test_default_keeps_agent_specific_timeouts(self) -> None:
        args = run_campaign.build_parser().parse_args(["--query", "test query"])

        research_args, device_args = run_campaign.build_step_args(args)

        self.assertNotIn("--llm-timeout-seconds", research_args)
        self.assertNotIn("--timeout-seconds", device_args)


if __name__ == "__main__":
    unittest.main()

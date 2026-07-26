from __future__ import annotations

import unittest

import run_campaign


class RunCampaignArgsTest(unittest.TestCase):
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

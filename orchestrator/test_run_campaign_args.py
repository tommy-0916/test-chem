from __future__ import annotations

import json
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_campaign
from orchestrator.execution_adapters import MockExecutionAdapter
from orchestrator.runner import CampaignConfig, CampaignRunner


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

    def test_route_trust_config_is_forwarded_only_to_research(self) -> None:
        args = run_campaign.build_parser().parse_args(
            [
                "--query", "test query",
                "--knowledge-base-dir", "kb",
                "--route-trust-config", "reviewed-route-trust.json",
            ]
        )

        research_args, device_args = run_campaign.build_step_args(args)

        self.assertEqual(
            research_args[research_args.index("--route-trust-config") + 1],
            "reviewed-route-trust.json",
        )
        self.assertNotIn("--route-trust-config", device_args)

    def test_route_trust_config_is_not_added_by_default(self) -> None:
        args = run_campaign.build_parser().parse_args(["--query", "test query"])

        research_args, _ = run_campaign.build_step_args(args)

        self.assertNotIn("--route-trust-config", research_args)

    def test_bootstrap_constraints_are_not_added_to_shared_research_args(self) -> None:
        args = run_campaign.build_parser().parse_args(
            [
                "--query", "test query",
                "--bootstrap-constraints-json", '{"route_decision_goal_v1":{"goal_id":"G1"}}',
            ]
        )

        research_args, device_args = run_campaign.build_step_args(args)

        self.assertNotIn("--constraints-json", research_args)
        self.assertNotIn("--constraints-json", device_args)
        self.assertEqual(
            json.loads(args.bootstrap_constraints_json)["route_decision_goal_v1"],
            {"goal_id": "G1"},
        )

    def test_bootstrap_constraints_reject_invalid_or_non_object_json(self) -> None:
        for value in ("{", "[]"):
            with self.subTest(value=value):
                with patch("sys.argv", [
                    "run_campaign.py", "--query", "test query",
                    "--bootstrap-constraints-json", value,
                ]), patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as failure:
                        run_campaign.main()
                self.assertEqual(failure.exception.code, 2)

    def test_bootstrap_constraints_reach_only_bootstrap_research_subprocess(self) -> None:
        constraints = {
            "route_decision_goal_v1": {"goal_id": "G1"},
            "route_action_intent_v1": {"route_id": "R1"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            iteration_dir = root / "iteration_00"
            iteration_dir.mkdir()
            runner = CampaignRunner(
                CampaignConfig(
                    query="test query", campaign_id="cmp_bootstrap_constraints",
                    campaigns_root=root, bootstrap_constraints=constraints,
                    research_args=["--route-trust-config", "reviewed-route-trust.json"],
                ),
                MockExecutionAdapter(),
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> SimpleNamespace:
                calls.append(command)
                output = Path(command[command.index("--save-state") + 1])
                output.write_text('{"status":"completed"}', encoding="utf-8")
                return SimpleNamespace(stdout="", stderr="", returncode=0)

            with patch("orchestrator.runner.subprocess.run", side_effect=fake_run):
                runner._research_step_subprocess(
                    "bootstrap", query="test query", previous_state_path=None,
                    payload=None, iteration_dir=iteration_dir, references=[],
                )
                runner._research_step_subprocess(
                    "new_observation", query="", previous_state_path=iteration_dir / "research_state.json",
                    payload={"observation": {"summary": "done"}},
                    iteration_dir=iteration_dir, references=[],
                )

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            json.loads(calls[0][calls[0].index("--constraints-json") + 1]),
            constraints,
        )
        self.assertNotIn("--constraints-json", calls[1])
        self.assertIn("--route-trust-config", calls[0])
        self.assertIn("--route-trust-config", calls[1])

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

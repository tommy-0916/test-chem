"""Route trust is deployment input, separate from Research event constraints."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reaserch_agent import run_research_agent as cli


class RouteTrustCliTest(unittest.TestCase):
    def test_absent_configuration_stays_conservative(self):
        self.assertEqual(cli.load_route_trust_config(None, None), {})
        args = cli.build_parser().parse_args(["--query", "test"])
        self.assertIsNone(args.route_trust_config)

    def test_loader_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trust.json"
            kb = Path(directory) / "kb"
            kb.mkdir()
            path.write_text('{"schema_version":"route-trust-config/v1",'
                            '"schema_version":"route-trust-config/v1"}', encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "duplicate JSON key"):
                cli.load_route_trust_config(str(path), str(kb))

    def test_loader_rejects_config_inside_knowledge_base(self):
        with tempfile.TemporaryDirectory() as directory:
            kb = Path(directory) / "kb"
            kb.mkdir()
            path = kb / "trust.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "outside the knowledge base"):
                cli.load_route_trust_config(str(path), str(kb))

    def test_loader_rejects_missing_signed_event_key_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trust.json"
            kb = Path(directory) / "kb"
            kb.mkdir()
            path.write_text(json.dumps({
                "schema_version": "route-trust-config/v1",
                "signed_route_source_events": [],
                "trusted_route_public_keys": {},
                "signed_route_signature_reviews": [],
                "trusted_route_signature_public_keys": {},
                "trusted_route_capabilities_by_group": [],
                "trusted_route_group_roles_by_group": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "requires signed source events"):
                cli.load_route_trust_config(str(path), str(kb))

    def test_main_passes_only_independently_loaded_trust_to_agent(self):
        deployment_trust = {
            "signed_route_source_events": (object(),),
            "trusted_route_public_keys": {"source-key": object()},
            "signed_route_signature_reviews": (object(),),
            "trusted_route_signature_public_keys": {"review-key": object()},
            "trusted_route_capabilities_by_group": {("p", "g", "d"): ("mix",)},
            "trusted_route_group_roles_by_group": {("p", "g", "d"): "synthesis"},
        }
        fake_state = SimpleNamespace(status="completed", campaign_id="", plan_revisions=[])
        args = [
            "run_research_agent.py", "--query", "test", "--disable-llm",
            "--no-online-literature", "--no-web-search", "--no-ledger",
            "--device-context-json", "{}", "--route-trust-config", "deployment.json",
            "--constraints-json", json.dumps({"route_decision_goal_v1": {"goal_id": "G"}}),
        ]
        with patch.object(sys, "argv", args), patch.object(
            cli, "load_route_trust_config", return_value=deployment_trust,
        ) as load, patch.object(cli, "ResearchAgent") as agent_type, patch.object(
            cli, "print_summary"
        ), patch.object(cli, "write_debug_log", return_value=Path("debug.json")):
            agent_type.return_value.run.return_value = fake_state
            self.assertEqual(cli.main(), 0)

        load.assert_called_once_with("deployment.json", None)
        kwargs = agent_type.call_args.kwargs
        for name, expected in deployment_trust.items():
            self.assertIs(kwargs[name], expected)
        self.assertNotIn("route_trust_config", agent_type.return_value.run.call_args.kwargs["constraints"])

    def test_v1_cannot_silently_ignore_route_trust_config(self):
        args = [
            "run_research_agent.py", "--query", "test", "--contract-version", "v1",
            "--route-trust-config", "deployment.json", "--device-context-json", "{}",
        ]
        with patch.object(sys, "argv", args):
            with self.assertRaisesRegex(SystemExit, "requires --contract-version v2"):
                cli.main()


if __name__ == "__main__":
    unittest.main()

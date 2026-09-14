from __future__ import annotations

import unittest
from unittest.mock import patch

from reaserch_agent.run_research_agent import build_parser
from reaserch_agent.state import ResearchEvent
from reaserch_agent.tools.device_context import ensure_device_context
from reaserch_agent.workflow import ResearchAgent


class DefaultRuntimePolicyTest(unittest.TestCase):
    def test_cli_defaults_enable_device_context_and_both_retrieval_lines(self) -> None:
        args = build_parser().parse_args(["--query", "test query"])
        self.assertTrue(args.include_device_context)
        self.assertTrue(args.online_literature)
        self.assertTrue(args.web_search)

    def test_direct_research_agent_defaults_enable_both_retrieval_lines(self) -> None:
        agent = ResearchAgent(model=None, use_llm=False)
        self.assertIs(agent._online_literature, True)
        self.assertIs(agent._web_search_enabled, True)

    def test_default_context_comes_from_capability_index(self) -> None:
        constraints = ensure_device_context({})
        context = constraints["device_context"]
        self.assertIn(
            "workstation_capability_index.json",
            context["capability_index"],
        )
        self.assertGreaterEqual(len(context["workstations"]), 40)
        self.assertIn("physically connected", context["planning_policy"])

    def test_legacy_false_flag_cannot_disable_bootstrap_device_context(self) -> None:
        constraints = ensure_device_context(
            {"include_default_device_context": False}
        )
        self.assertEqual(
            constraints["device_context"]["source_kind"],
            "lab-design-all capability index",
        )

    def test_direct_run_event_also_loads_device_context(self) -> None:
        agent = ResearchAgent(model=None, use_llm=False)
        event = ResearchEvent(
            event_type="bootstrap",
            query="test query",
            constraints={"include_default_device_context": False},
        )

        # This policy test must not contact the network.  Stop immediately
        # after run_event has enforced and installed the bootstrap context.
        with patch.object(agent, "_run_b0", side_effect=lambda value: value):
            state = agent.run_event(event)

        self.assertIn("device_context", state.event.constraints)
        self.assertEqual(
            state.event.constraints["device_context"]["source_kind"],
            "lab-design-all capability index",
        )


if __name__ == "__main__":
    unittest.main()

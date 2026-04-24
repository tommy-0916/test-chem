"""Unit tests for the partially implemented research agent."""

from __future__ import annotations

import unittest
from pathlib import Path

from reaserch_agent.workflow import ResearchAgent


class ResearchAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = ResearchAgent(model=None, use_llm=False)
        self.structured_outputs_dir = (
            Path(__file__).resolve().parents[1] / "structured_outputs"
        )

    def test_b0_returns_not_implemented_for_non_bootstrap_event(self) -> None:
        state = self.agent.run(
            event_type="new observation",
            query="测试一个尚未实现的分支",
        )

        self.assertEqual(state.status, "not_implemented")
        self.assertEqual(state.current_branch, "B0")
        self.assertIsNone(state.next_branch)
        self.assertIn("仅实现 B0/B1", state.route_message)

    def test_b1_bootstrap_generates_initial_outputs(self) -> None:
        query = "设计一种 NiCo-PBA 核壳结构双功能水分解催化剂，并规划从合成到阳极活化的第一轮实验"
        state = self.agent.run(event_type="bootstrap", query=query)

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.last_completed_branch, "B1")
        self.assertEqual(state.current_branch, "B0")
        self.assertEqual(state.next_branch, "B0")
        self.assertTrue(state.stage_route)
        self.assertTrue(state.current_stage)
        self.assertTrue(state.current_stage_plan)
        self.assertTrue(state.macro_plan)
        self.assertTrue(state.knowledge_hits)
        self.assertEqual(state.device_adaptation_handoff["query"], query)
        self.assertEqual(
            state.device_adaptation_handoff["待执行 macro plan"],
            state.macro_plan,
        )
        self.assertTrue(
            any("NiCo" in str(step) or "PBA" in str(step) for step in state.macro_plan)
        )

    def test_custom_knowledge_base_dir_is_supported(self) -> None:
        agent = ResearchAgent(
            model=None,
            use_llm=False,
            knowledge_base_dir=str(self.structured_outputs_dir),
            memory_dir=str(self.structured_outputs_dir),
        )

        state = agent.run(
            event_type="bootstrap",
            query="设计一种普鲁士蓝类似物合成路线",
        )

        self.assertEqual(state.status, "completed")
        self.assertTrue(state.knowledge_hits)
        self.assertTrue(state.memory_hits)


if __name__ == "__main__":
    unittest.main()

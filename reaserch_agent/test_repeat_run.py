"""Issue 3 tests: repeat-run stability and change attribution.

The deterministic (heuristic + fixed KB) pipeline must reproduce identical
plans for identical queries, and when two runs differ the comparison tool must
say whether the change came from papers/network, the plan, or agent fill.
"""

from __future__ import annotations

import copy
import unittest
from unittest import mock

from reaserch_agent.tools.run_comparison import compare_research_states
from reaserch_agent.workflow import ResearchAgent

QUERY = "合成 NiFe 普鲁士蓝类似物并进行 XRD 表征"


class RepeatRunStabilityTest(unittest.TestCase):
    def setUp(self):
        offline = mock.patch.dict("os.environ", {
            "RESEARCH_ONLINE_LITERATURE": "0", "RESEARCH_WEB_SEARCH": "0",
        })
        offline.start()
        self.addCleanup(offline.stop)

    def test_same_query_twice_produces_identical_plan(self) -> None:
        """Deterministic acceptance: same query + same KB + heuristic mode
        → identical survey queries, stage route, and macro plan."""
        state_a = ResearchAgent(model=None, use_llm=False).run(
            event_type="bootstrap", query=QUERY
        )
        state_b = ResearchAgent(model=None, use_llm=False).run(
            event_type="bootstrap", query=QUERY
        )

        self.assertEqual(state_a.survey_queries, state_b.survey_queries)
        self.assertEqual(state_a.stage_route, state_b.stage_route)
        self.assertEqual(state_a.current_stage, state_b.current_stage)
        core_a = [
            (s.get("操作"), s.get("试剂/对象"), s.get("参数")) for s in state_a.macro_plan
        ]
        core_b = [
            (s.get("操作"), s.get("试剂/对象"), s.get("参数")) for s in state_b.macro_plan
        ]
        self.assertEqual(core_a, core_b)

        report = compare_research_states(state_a.to_dict(), state_b.to_dict())
        self.assertTrue(report["identical"], report["summary"])

    def test_comparison_attributes_paper_changes_to_network(self) -> None:
        state = ResearchAgent(model=None, use_llm=False).run(
            event_type="bootstrap", query=QUERY
        )
        run_a = state.to_dict()
        run_b = copy.deepcopy(run_a)
        run_b["knowledge_hits"] = list(run_b.get("knowledge_hits", []))[:0]

        report = compare_research_states(run_a, run_b)
        self.assertFalse(report["identical"])
        self.assertTrue(report["papers_changed"])
        self.assertTrue(
            any(c["source"] == "papers_or_network" for c in report["changes"])
        )

    def test_comparison_attributes_agent_fill_changes(self) -> None:
        state = ResearchAgent(model=None, use_llm=False).run(
            event_type="bootstrap", query=QUERY
        )
        run_a = state.to_dict()
        run_b = copy.deepcopy(run_a)
        # mutate one agent-filled step's parameters (simulates fill randomness)
        for step in run_b.get("macro_plan", []):
            if "agent补全" in str(step.get("来源", "")):
                step["参数"] = str(step.get("参数", "")) + "；改为 600 rpm"
                break

        report = compare_research_states(run_a, run_b)
        self.assertFalse(report["identical"])
        self.assertTrue(
            any(c["source"] == "agent_generated" for c in report["changes"]),
            report["summary"],
        )


if __name__ == "__main__":
    unittest.main()

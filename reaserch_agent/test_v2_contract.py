from __future__ import annotations

import unittest

from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.workflow import ResearchAgent


class _OnlineService:
    def __init__(self):
        self.calls = []
        self.responses = [
            {"status": "success", "retrieval_status": "success", "results": [{"title": "paper A"}]},
            {"status": "success", "retrieval_status": "empty", "results": []},
        ]

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses[len(self.calls) - 1]


class ResearchV2ContractTest(unittest.TestCase):
    def test_each_action_gets_isolated_online_evidence(self):
        service = _OnlineService()
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        agent._online_research_service = lambda state: service
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="synthesize catalyst")
        )
        state.current_stage = "stage A"

        agent._refresh_action_evidence(
            state, planning_mode="bootstrap", observation_point="XRD"
        )
        first_id = state.current_evidence_bundle["bundle_id"]
        agent._refresh_action_evidence(
            state, planning_mode="post_observation", observation_point="activity"
        )

        self.assertEqual(len(service.calls), 2)
        self.assertTrue(all(call["references"] == [] for call in service.calls))
        self.assertNotEqual(first_id, state.current_evidence_bundle["bundle_id"])
        self.assertEqual(state.current_evidence_bundle["results"], [])
        self.assertTrue(state.current_evidence_bundle["current_invocation_only"])

    def test_v2_rejects_vague_or_unquantified_active_inputs(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        issues = agent._macro_plan_quality_issues(
            [
                {
                    "步骤序号": 1,
                    "操作": "加入前驱体",
                    "试剂/对象": "前驱体",
                    "参数": "加入适量前驱体并搅拌 10 min",
                    "material_inputs": [],
                    "material_outputs": [],
                    "container_requirements": [],
                    "intermediate_returns": [],
                }
            ],
            "prepare sample",
        )
        self.assertTrue(any("material_inputs 为空" in issue for issue in issues))
        self.assertTrue(any("provenance" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()

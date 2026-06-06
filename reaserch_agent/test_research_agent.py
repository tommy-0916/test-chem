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

    def test_b0_returns_not_implemented_for_unknown_event(self) -> None:
        state = self.agent.run(
            event_type="unknown event",
            query="测试一个尚未实现的分支",
        )

        self.assertEqual(state.status, "not_implemented")
        self.assertEqual(state.current_branch, "B0")
        self.assertIsNone(state.next_branch)
        self.assertIn("已实现 B0/B1/B2", state.route_message)

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
            any(
                "NiCo" in str(step) or "PBA" in str(step) or "普鲁士蓝" in str(step)
                for step in state.macro_plan
            )
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
        self.assertFalse(state.memory_hits)

    def test_memory_can_be_enabled_explicitly(self) -> None:
        agent = ResearchAgent(
            model=None,
            use_llm=False,
            knowledge_base_dir=str(self.structured_outputs_dir),
            memory_dir=str(self.structured_outputs_dir),
            enable_memory=True,
        )

        state = agent.run(
            event_type="bootstrap",
            query="设计一种普鲁士蓝类似物合成路线",
        )

        self.assertEqual(state.status, "completed")
        self.assertTrue(state.memory_hits)

    def test_b1_extracts_protocol_before_macro_plan(self) -> None:
        agent = ResearchAgent(
            model=None,
            use_llm=False,
            knowledge_base_dir=str(self.structured_outputs_dir),
        )

        state = agent.run(
            event_type="bootstrap",
            query="High-Entropy Prussian Blue Analogues as Sulfur Hosts for Lithium-Sulfur Batteries",
        )

        self.assertEqual(state.status, "completed")
        self.assertTrue(state.extracted_protocols)
        protocol_blob = str(state.extracted_protocols)
        macro_blob = str(state.macro_plan)
        self.assertIn("2 mmol metal nitrate", protocol_blob)
        self.assertIn("2 mmol metal nitrate", macro_blob)

    def test_b2_normal_observation_updates_progress(self) -> None:
        bootstrap_state = self.agent.run(
            event_type="bootstrap",
            query="合成普鲁士蓝样品并通过 XRD 确认目标物相",
        )

        state = self.agent.run(
            event_type="new observation",
            payload={
                "observation": {
                    "observation_type": "XRD",
                    "summary": "XRD characteristic peaks matched the target Prussian Blue phase; observation completed successfully.",
                    "metrics": {"phase_match": True},
                }
            },
            previous_state=bootstrap_state,
        )

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.last_completed_branch, "B2")
        self.assertIn(state.post_observation_repair_path, {"normal_progress"})
        self.assertTrue(state.latest_observation)
        self.assertTrue(state.observation_stage_fit["fits_current_stage"])
        self.assertTrue(state.stage_progress_status)
        self.assertIn(state.current_branch, {"B0"})

    def test_b2_abnormal_observation_repairs_macro_plan(self) -> None:
        bootstrap_state = self.agent.run(
            event_type="bootstrap",
            query="合成普鲁士蓝样品并通过 XRD 确认目标物相",
        )

        state = self.agent.run(
            event_type="new observation",
            payload={
                "observation": {
                    "observation_type": "XRD",
                    "summary": "XRD failed: strong impurity phase appeared and target Prussian Blue peaks were weak.",
                    "metrics": {"phase_match": False},
                }
            },
            previous_state=bootstrap_state,
        )

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.last_completed_branch, "B2")
        self.assertEqual(state.post_observation_repair_path, "stage_internal")
        self.assertFalse(state.observation_stage_fit["fits_current_stage"])
        self.assertTrue(state.macro_plan)
        self.assertTrue(
            any(
                "修复" in str(step) or "调整" in str(step) or "重新" in str(step)
                for step in state.macro_plan
            )
        )

    def test_b2_repairs_negated_pba_phase_match_from_text_plan(self) -> None:
        bootstrap_state = self.agent.run(
            event_type="bootstrap",
            query="针对水系 K 离子电池正极材料容量偏低，以亚铁氰化铁为正极并通过 XRD 确认 K2Fe[Fe(CN)6]·2H2O",
        )

        state = self.agent.run(
            event_type="new observation",
            payload={
                "previous_macro_plan": [
                    "Prepared solution A from K4Fe(CN)6·3H2O, sodium citrate, and water.",
                    "Prepared solution B from FeCl2·4H2O and water.",
                    "Added solution B dropwise into solution A.",
                    "Added ethylene glycol and heated the suspension in a Teflon-lined autoclave at 80 °C for 24 h.",
                    "Washed, dried, and measured PXRD.",
                ],
                "observation": {
                    "synthesis_process": [
                        "A precipitate formed immediately when solution B was added to solution A.",
                        "The mixture became turbid before solvothermal treatment.",
                        "The reaction medium was water-rich and approximately neutral rather than acidic.",
                        "The final product was a pale blue powder.",
                    ],
                    "PXRD": [
                        "The pattern shows broad PBA-like diffraction peaks.",
                        "The peak positions and relative intensities do not cleanly match the target K2Fe[Fe(CN)6]·2H2O reference.",
                        "Weak extra peaks and elevated background are observed.",
                    ],
                },
            },
            previous_state=bootstrap_state,
        )

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.post_observation_repair_path, "stage_internal")
        self.assertFalse(state.observation_stage_fit["fits_current_stage"])
        self.assertEqual(state.observation_stage_fit["status"], "abnormal")
        self.assertTrue(
            any("do not cleanly match" in signal for signal in state.observation_stage_fit[
                "observation_interpretation"
            ]["negative_signals"])
        )
        macro_blob = str(state.macro_plan)
        self.assertIn("FeCl2", macro_blob)
        self.assertIn("pH 2-3", macro_blob)
        self.assertIn("K2Fe[Fe(CN)6]·2H2O", macro_blob)

    def test_b2_device_feasibility_error_replans_for_supported_containers(self) -> None:
        bootstrap_state = self.agent.run(
            event_type="bootstrap",
            query="针对水系 K 离子电池正极材料容量偏低，以亚铁氰化铁为正极并通过 XRD 确认 K2Fe[Fe(CN)6]·2H2O",
        )
        original_stage = bootstrap_state.current_stage
        original_stage_route = list(bootstrap_state.stage_route)

        state = self.agent.run(
            event_type="new observation",
            payload={
                "feedback_type": "device_feasibility_error",
                "previous_macro_plan": [
                    {
                        "步骤序号": 1,
                        "操作": "配制单源前驱体溶液并调节 pH",
                        "试剂/对象": "K4Fe(CN)6·3H2O、去离子水、乙二醇、稀盐酸",
                        "参数": "1 mmol K4Fe(CN)6·3H2O in 25 mL water + 25 mL ethylene glycol; pH 3-4",
                    },
                    {
                        "步骤序号": 2,
                        "操作": "溶剂热反应",
                        "试剂/对象": "聚四氟乙烯内衬高压反应釜",
                        "参数": "80 C 24 h",
                    },
                ],
                "status": "feasibility_error",
                "error_package": {
                    "type": "physical_infeasible",
                    "blocking_constraints": [
                        "当前设备层不支持聚四氟乙烯内衬高压反应釜容器类型。",
                        "当前工作站资源中没有反应釜/高压釜工作站。",
                        "当前平台没有 XRD 工作站，XRD 只能作为离线 observation。",
                    ],
                    "message": "该 macro_plan 包含当前设备无法执行的反应釜溶剂热步骤。",
                },
                "device_capabilities": {
                    "supported_containers": ["进样瓶", "西林瓶", "50ml耐热瓶", "留样瓶"],
                    "supported_workstations": [
                        "物料站",
                        "液体进样站",
                        "磁力搅拌工作站",
                        "纯化工作站",
                        "烘干机",
                    ],
                },
                "request": "请在不使用反应釜的前提下重新规划设备可执行的 macro_plan。",
            },
            previous_state=bootstrap_state,
        )

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.latest_observation["feedback_type"], "device_feasibility_error")
        self.assertFalse(state.observation_stage_fit["fits_current_stage"])
        self.assertEqual(state.observation_stage_fit["status"], "abnormal")
        self.assertEqual(state.post_observation_repair_path, "device_adaptation")
        self.assertEqual(state.current_stage, original_stage)
        self.assertEqual(state.stage_route, original_stage_route)
        self.assertTrue(state.latest_observation["prior_paper_hits"])
        self.assertIn("current_stage", state.latest_observation["previous_stage_context"])
        self.assertTrue(state.latest_observation["previous_macro_action"])

        macro_blob = str(state.macro_plan)
        self.assertIn("进样瓶", macro_blob)
        self.assertIn("离线", macro_blob)
        self.assertIn("XRD", macro_blob)
        self.assertIn("K2Fe[Fe(CN)6]·2H2O", state.event.query)
        self.assertIn("K2Fe[Fe(CN)6]·2H2O", state.current_stage_plan)
        self.assertIn("XRD", state.current_stage_plan)
        self.assertNotIn("聚四氟", macro_blob)
        self.assertNotIn("高压釜", macro_blob)


if __name__ == "__main__":
    unittest.main()

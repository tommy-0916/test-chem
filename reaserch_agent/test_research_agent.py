"""Unit tests for the partially implemented research agent."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.workflow import ResearchAgent


class CapturingModel:
    def __init__(self) -> None:
        self.prompts = []

    def invoke(self, messages):
        prompt = "\n\n".join(
            getattr(message, "content", None) or message.get("content", "")
            for message in messages
        )
        self.prompts.append(prompt)

        if "survey query generate" in prompt:
            return self._json(
                {
                    "queries": [
                        "普鲁士蓝 水系钾离子电池",
                        "亚铁氰化铁 设备可执行 合成",
                        "Prussian blue aqueous potassium ion battery",
                    ],
                    "reason": "覆盖材料、方法和设备可执行路线。",
                }
            )
        if "survey expansion" in prompt:
            return self._json(
                {
                    "continue_research": False,
                    "new_queries": [],
                    "reason": "当前知识足够支撑设备约束下的首轮规划。",
                }
            )
        if "paper protocol extract" in prompt:
            return self._json(
                {
                    "protocols": [
                        {
                            "source_title": "测试 protocol",
                            "source_file": "structured_outputs/test.json",
                            "relevance": "用于测试设备上下文能进入 B1 prompt。",
                            "protocol_summary": "固定容器和固定时间条件下制备样品。",
                            "steps": [
                                {
                                    "步骤序号": 1,
                                    "操作": "配制前驱体溶液",
                                    "试剂/对象": "K4Fe(CN)6·3H2O、去离子水",
                                    "参数": "0.5 mmol in 10 mL water",
                                    "evidence": "测试证据",
                                }
                            ],
                        }
                    ],
                    "missing_parameters": [],
                }
            )
        if "survey report generate" in prompt:
            return self._json(
                {
                    "summary": "普鲁士蓝类材料可作为水系钾离子电池正极。",
                    "key_findings": ["设备上下文要求避开反应釜和在线 XRD。"],
                    "candidate_precedents": [],
                    "route_implications": ["优先选择当前设备支持的容器路线。"],
                    "open_questions": [],
                }
            )
        if "stage design" in prompt:
            return self._json(
                {
                    "stage_route": ["合成目标样品并完成离线 XRD handoff"],
                    "current_stage": "合成目标样品并完成离线 XRD handoff",
                    "stage_route_reason": "当前 observation point 为离线 XRD 数据回传。",
                    "current_stage_reason": "先确认目标相。",
                }
            )
        if "macro plan design" in prompt:
            return self._json(
                {
                    "current_stage_plan": "当前 stage 为合成目标样品并完成离线 XRD handoff；目标 observation point 是离线 XRD 数据回传；stage goal 是获得可送样粉末；planning logic 是使用设备支持容器和固定时间条件；key variables 为体积、时间、温度；expected observation 为 PBA 特征峰；stage completion condition 为离线 XRD 数据返回。",
                    "macro_plan": [
                        {
                            "步骤序号": 1,
                            "操作": "配制前驱体溶液",
                            "试剂/对象": "K4Fe(CN)6·3H2O、去离子水、50ml 耐热瓶",
                            "参数": "0.5 mmol K4Fe(CN)6·3H2O 溶于 10 mL 去离子水，室温混合 10 min",
                        },
                        {
                            "步骤序号": 2,
                            "操作": "固定条件加热反应",
                            "试剂/对象": "前驱体溶液、50ml 耐热瓶",
                            "参数": "80 C 加热 12 h，自然冷却至室温",
                        },
                        {
                            "步骤序号": 3,
                            "操作": "固定次数洗涤并干燥",
                            "试剂/对象": "沉淀、去离子水、乙醇",
                            "参数": "去离子水洗涤 3 次、乙醇洗涤 2 次，60 C 干燥 12 h",
                        },
                        {
                            "步骤序号": 4,
                            "操作": "离线 XRD handoff",
                            "试剂/对象": "干燥粉末",
                            "参数": "收集 30 mg 粉末送外部 XRD，等待数据回传",
                        },
                    ],
                    "macro_plan_summary": "使用设备支持路线推进到离线 XRD handoff。",
                }
            )
        raise AssertionError(f"Unexpected prompt: {prompt[:200]}")

    def _json(self, payload):
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


class DeviceAdaptationRetryModel:
    def __init__(self) -> None:
        self.prompts = []

    def invoke(self, messages):
        prompt = "\n\n".join(
            getattr(message, "content", None) or message.get("content", "")
            for message in messages
        )
        self.prompts.append(prompt)

        if "device-adaptation macro plan design" not in prompt:
            raise AssertionError(f"Unexpected prompt: {prompt[:200]}")

        if "本地质量检查反馈" not in prompt:
            return self._json(
                {
                    "current_stage_plan": "保持当前 XRD observation point，只缩小规模并保留目标物相。",
                    "macro_plan": [
                        {
                            "步骤序号": 1,
                            "操作": "小体积共沉淀制备目标样品",
                            "试剂/对象": "A 液、B 液",
                            "参数": "每批 15 mL A 液与 15 mL B 液混合，室温 aging 48 h",
                        },
                        {
                            "步骤序号": 2,
                            "操作": "分离和洗涤沉淀",
                            "试剂/对象": "目标沉淀、去离子水",
                            "参数": "固液分离后用去离子水洗涤至上清液基本澄清",
                        },
                        {
                            "步骤序号": 3,
                            "操作": "常压干燥并离线 XRD",
                            "试剂/对象": "洗涤后的沉淀",
                            "参数": "100 C 常压干燥 overnight，送离线 XRD observation",
                        },
                    ],
                    "macro_plan_summary": "第一次故意输出闭环终点以触发质量反馈。",
                }
            )

        return self._json(
            {
                "current_stage_plan": "保持当前 XRD observation point，只缩小规模并保留目标物相。",
                "macro_plan": [
                    {
                        "步骤序号": 1,
                        "操作": "小体积共沉淀制备目标样品",
                        "试剂/对象": "A 液、B 液",
                        "参数": "每批 15 mL A 液与 15 mL B 液混合，室温 aging 48 h",
                    },
                    {
                        "步骤序号": 2,
                        "操作": "固定次数分离和洗涤沉淀",
                        "试剂/对象": "目标沉淀、去离子水",
                        "参数": "固液分离后用去离子水洗涤 3 次，每次使用 5 mL 去离子水",
                    },
                    {
                        "步骤序号": 3,
                        "操作": "常压干燥并离线 XRD",
                        "试剂/对象": "洗涤后的沉淀",
                        "参数": "100 C 常压干燥 overnight，送离线 XRD observation",
                    },
                ],
                "macro_plan_summary": "第二次根据质量反馈改成固定洗涤次数。",
            }
        )

    def _json(self, payload):
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


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

    def test_b1_bootstrap_builds_observation_point_macro_action(self) -> None:
        """Issue 6: macro actions must be observation-point-driven units.

        The macro plan must carry a structured macro_action descriptor (id +
        observation point + completion condition), each macro step must be
        stamped with its macro_action_id / observation_point_id, and the
        additive handoff key must appear WITHOUT dropping the 4 contract keys.
        """
        query = "合成 NiFe 普鲁士蓝类似物并进行 XRD 表征"
        state = self.agent.run(event_type="bootstrap", query=query)

        macro_action = state.macro_action
        self.assertTrue(macro_action)
        self.assertTrue(macro_action.get("macro_action_id"))
        self.assertTrue(macro_action.get("observation_point_id"))
        self.assertTrue(macro_action.get("observation_point"))
        self.assertTrue(macro_action.get("completion_condition"))
        self.assertEqual(macro_action.get("stage"), state.current_stage)

        for step in state.macro_plan:
            self.assertEqual(step.get("macro_action_id"), macro_action["macro_action_id"])
            self.assertEqual(
                step.get("observation_point_id"), macro_action["observation_point_id"]
            )

        handoff = state.device_adaptation_handoff
        for contract_key in (
            "待执行 macro plan",
            "当前 stage",
            "当前 stage 的完整化学语义实验计划",
            "stage路线设计理由",
        ):
            self.assertIn(contract_key, handoff)
        self.assertIn("当前 macro action", handoff)
        self.assertTrue(state.macro_action_history)

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

    def test_b1_first_llm_prompt_includes_device_context(self) -> None:
        model = CapturingModel()
        agent = ResearchAgent(
            model=model,
            use_llm=True,
            knowledge_base_dir=str(self.structured_outputs_dir),
            max_survey_rounds=1,
        )

        state = agent.run(
            event_type="bootstrap",
            query="设计普鲁士蓝水系钾离子电池正极首轮实验",
            constraints={
                "device_context": {
                    "workstations": [
                        {
                            "station_name": "dryer-workstation",
                            "usage_summary": "支持固定温度和固定时间干燥",
                        }
                    ]
                }
            },
        )

        self.assertEqual(state.status, "completed")
        self.assertTrue(model.prompts)
        first_prompt = model.prompts[0]
        self.assertIn('"device_context"', first_prompt)
        self.assertIn("dryer-workstation", first_prompt)
        self.assertIn("survey query generate", first_prompt)

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

    def test_b2_records_completed_macro_action_outcome(self) -> None:
        """Issue 6: a normal observation marks the prior macro action completed
        in macro_action_history, not just the stage."""
        bootstrap_state = self.agent.run(
            event_type="bootstrap",
            query="合成 NiFe-LDH，先做 XRD 表征，再进行电化学 OER 活化测试",
        )
        first_id = bootstrap_state.macro_action["macro_action_id"]

        state = self.agent.run(
            event_type="new observation",
            payload={
                "observation": {
                    "observation_type": "XRD",
                    "summary": "XRD characteristic peaks matched the target phase; observation completed.",
                    "metrics": {"phase_match": True},
                }
            },
            previous_state=bootstrap_state,
        )

        history = {h["macro_action_id"]: h for h in state.macro_action_history}
        self.assertIn(first_id, history)
        self.assertEqual(history[first_id]["outcome"], "completed")
        # a distinct macro action now leads the next observation point
        self.assertNotEqual(state.macro_action.get("macro_action_id"), first_id)

    def test_b2_records_needs_repair_outcome_on_abnormal(self) -> None:
        """Issue 6: an abnormal observation marks the macro action needs_repair."""
        bootstrap_state = self.agent.run(
            event_type="bootstrap",
            query="合成普鲁士蓝样品并通过 XRD 确认目标物相",
        )
        first_id = bootstrap_state.macro_action["macro_action_id"]

        state = self.agent.run(
            event_type="new observation",
            payload={
                "observation": {
                    "observation_type": "XRD",
                    "summary": "XRD failed: strong impurity phase appeared and target peaks were weak.",
                    "metrics": {"phase_match": False},
                }
            },
            previous_state=bootstrap_state,
        )

        history = {h["macro_action_id"]: h for h in state.macro_action_history}
        self.assertIn(first_id, history)
        self.assertEqual(history[first_id]["outcome"], "needs_repair")

    def test_b2_records_device_rejected_outcome(self) -> None:
        """Issue 6: a device feasibility error marks the macro action
        device_rejected (not a whole-stage failure)."""
        bootstrap_state = self.agent.run(
            event_type="bootstrap",
            query="合成普鲁士蓝样品并通过 XRD 确认目标物相",
        )
        first_id = bootstrap_state.macro_action["macro_action_id"]

        state = self.agent.run(
            event_type="new observation",
            payload={
                "feedback_type": "device_feasibility_error",
                "status": "feasibility_error",
                "error_package": {
                    "type": "physical_infeasible",
                    "blocking_constraints": ["缺少高压反应釜"],
                },
            },
            previous_state=bootstrap_state,
        )

        history = {h["macro_action_id"]: h for h in state.macro_action_history}
        self.assertIn(first_id, history)
        self.assertEqual(history[first_id]["outcome"], "device_rejected")

    def test_b2_translation_failed_routes_to_device_adaptation(self) -> None:
        """Issue #4: an exhausted-translation failure (error_package.type
        workflow_translation_failed) must ride the device-adaptation path —
        research re-plans against the structured device feedback instead of
        treating it as a generic observation."""
        bootstrap_state = self.agent.run(
            event_type="bootstrap",
            query="合成普鲁士蓝样品并通过 XRD 确认目标物相",
        )

        state = self.agent.run(
            event_type="new observation",
            payload={
                "feedback_type": "device_feasibility_error",
                "status": "failed",
                "error_package": {
                    "type": "workflow_translation_failed",
                    "blocking_constraints": [
                        "第 3 步：参数 `加样方案` 应为数组（type_mismatch）"
                    ],
                    "structured_errors": [
                        {"error_code": "type_mismatch", "step_number": 3,
                         "parameter_path": "加样方案"}
                    ],
                    "failed_plan_signature": "plan_deadbeef",
                },
            },
            previous_state=bootstrap_state,
        )

        self.assertEqual(state.post_observation_repair_path, "device_adaptation")
        self.assertTrue(state.macro_plan)  # re-planned, not cleared
        # the translation blockers joined the cumulative constraint memory
        self.assertTrue(
            any("加样方案" in item for item in state.cumulative_device_constraints)
        )

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

    def test_b2_device_feasibility_error_keeps_macro_actions_device_neutral(self) -> None:
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
        self.assertIn("离线", macro_blob)
        self.assertIn("XRD", macro_blob)
        self.assertIn("K2Fe[Fe(CN)6]·2H2O", state.event.query)
        self.assertIn("K2Fe[Fe(CN)6]·2H2O", state.current_stage_plan)
        self.assertIn("XRD", state.current_stage_plan)
        self.assertIn("device agent", state.current_stage_plan)
        self.assertNotIn("聚四氟", macro_blob)
        self.assertNotIn("高压釜", macro_blob)
        self.assertNotIn("液体进样站", macro_blob)
        self.assertNotIn("容器编号", macro_blob)

    def test_b2_accumulates_device_constraints_across_rounds(self) -> None:
        """Issue 4: every device blocking constraint is remembered across
        re-planning rounds and the rejected plan is fingerprinted."""
        bootstrap_state = self.agent.run(
            event_type="bootstrap",
            query="以亚铁氰化铁为正极并通过 XRD 确认 K2Fe[Fe(CN)6]·2H2O",
        )

        def device_error(constraint: str) -> dict:
            return {
                "feedback_type": "device_feasibility_error",
                "status": "feasibility_error",
                "device_snapshot_id": "ws_snapshot_A",
                "previous_macro_plan": [
                    {
                        "步骤序号": 1,
                        "操作": "溶剂热反应",
                        "试剂/对象": "聚四氟乙烯内衬高压反应釜",
                        "参数": "80 C 24 h",
                    }
                ],
                "error_package": {
                    "type": "physical_infeasible",
                    "blocking_constraints": [constraint],
                },
            }

        state1 = self.agent.run(
            event_type="new observation",
            payload=device_error("容器不兼容：进样瓶无法进入马弗炉"),
            previous_state=bootstrap_state,
        )
        state2 = self.agent.run(
            event_type="new observation",
            payload=device_error("单容器体积超过离心上限"),
            previous_state=state1,
        )

        self.assertIn("容器不兼容：进样瓶无法进入马弗炉", state2.cumulative_device_constraints)
        self.assertIn("单容器体积超过离心上限", state2.cumulative_device_constraints)
        self.assertEqual(state2.device_snapshot_id, "ws_snapshot_A")
        self.assertTrue(state2.failed_plan_signatures)
        plan_ids = [s["plan_id"] for s in state2.failed_plan_signatures]
        self.assertEqual(plan_ids, sorted(set(plan_ids)))
        handoff = state2.device_adaptation_handoff
        self.assertIn("累计设备阻塞约束", handoff)
        self.assertIn("单容器体积超过离心上限", handoff["累计设备阻塞约束"])

    def test_plan_signature_detects_repeated_route(self) -> None:
        """Issue 4: a regenerated plan with the same operations + key numbers
        matches the failed signature even when wording differs."""
        agent = self.agent
        plan_a = [
            {"步骤序号": 1, "操作": "磁力搅拌熟化", "试剂/对象": "混合液", "参数": "室温 700 rpm 搅拌 120 min"},
        ]
        plan_b = [
            {"步骤序号": 1, "操作": "磁力搅拌熟化", "试剂/对象": "混合液", "参数": "在 700 rpm 下持续搅拌 120 min，室温"},
        ]
        plan_c = [
            {"步骤序号": 1, "操作": "磁力搅拌熟化", "试剂/对象": "混合液", "参数": "室温 500 rpm 搅拌 60 min"},
        ]
        self.assertEqual(agent._plan_signature(plan_a), agent._plan_signature(plan_b))
        self.assertNotEqual(agent._plan_signature(plan_a), agent._plan_signature(plan_c))

    def test_repeated_failed_route_raises_warning(self) -> None:
        """Issue 4: when the regenerated plan repeats an already-failed route,
        a repetition warning is recorded into cumulative constraints."""
        bootstrap_state = self.agent.run(
            event_type="bootstrap",
            query="以亚铁氰化铁为正极并通过 XRD 确认 K2Fe[Fe(CN)6]·2H2O",
        )

        def device_error() -> dict:
            return {
                "feedback_type": "device_feasibility_error",
                "status": "feasibility_error",
                "previous_macro_plan": [
                    {
                        "步骤序号": 1,
                        "操作": "溶剂热反应",
                        "试剂/对象": "聚四氟乙烯内衬高压反应釜",
                        "参数": "80 C 24 h",
                    }
                ],
                "error_package": {
                    "type": "physical_infeasible",
                    "blocking_constraints": ["当前设备层不支持聚四氟乙烯内衬高压反应釜容器类型。"],
                },
            }

        state1 = self.agent.run(
            event_type="new observation",
            payload=device_error(),
            previous_state=bootstrap_state,
        )
        # probe: learn what the next regeneration will deterministically
        # produce, then mark exactly that plan as already failed.
        probe = self.agent.run(
            event_type="new observation",
            payload=device_error(),
            previous_state=state1,
        )
        self.agent._record_failed_plan_signature(
            state1, probe.macro_plan, ["模拟：该修复方案也被设备拒绝"]
        )
        state2 = self.agent.run(
            event_type="new observation",
            payload=device_error(),
            previous_state=state1,
        )

        # determinism: the regeneration reproduced the probe plan…
        self.assertEqual(
            self.agent._plan_signature(state2.macro_plan),
            self.agent._plan_signature(probe.macro_plan),
        )
        # …and the repetition warning fired into cumulative constraints.
        self.assertTrue(
            any("已失败方案" in c for c in state2.cumulative_device_constraints),
            state2.cumulative_device_constraints,
        )

    def test_device_boundary_doubt_marks_steps_instead_of_clearing_plan(self) -> None:
        """Issue 5 (C02): a device-boundary doubt on some steps must demote to
        per-step markers and keep the plan, not clear it to manual_required."""
        agent = ResearchAgent(model=None, use_llm=False)
        plan = [
            {"步骤序号": 1, "操作": "称取固体配制前驱体", "试剂/对象": "NiCl2 固体", "参数": "称取 1 mmol 溶于水"},
            {"步骤序号": 2, "操作": "磁力搅拌", "试剂/对象": "混合液", "参数": "搅拌至混合均匀，必要时延长"},
            {"步骤序号": 3, "操作": "离线 XRD", "试剂/对象": "粉末", "参数": "送样测试"},
        ]

        class _State:
            class _Event:
                constraints = {"device_context": {"workstations": [{"station_name": "x"}]}}

            event = _Event()

        markers = agent._device_context_macro_step_markers(_State(), plan)
        self.assertTrue(markers)
        kept = agent._apply_device_validation_markers(plan, markers)

        # the whole plan survives — no step is dropped
        self.assertEqual(len(kept), 3)
        # solid weighing → adaptation_required; vague stirring → needs_device_validation
        self.assertEqual(kept[0]["device_validation"], "adaptation_required")
        self.assertEqual(kept[1]["device_validation"], "needs_device_validation")
        # a clean step carries no marker
        self.assertNotIn("device_validation", kept[2])

    def test_b1_keeps_plan_with_device_markers_under_device_context(self) -> None:
        """Issue 5 end to end (heuristic): a bootstrap with device_context that
        trips a device-boundary rule still returns a non-empty macro plan."""
        agent = ResearchAgent(model=None, use_llm=False)
        state = agent.run(
            event_type="bootstrap",
            query="合成普鲁士蓝类似物并通过 XRD 确认目标物相",
            constraints={
                "device_context": {
                    "workstations": [
                        {"station_name": "液体进样站", "usage_summary": "支持已装载原液加液"},
                        {"station_name": "磁力搅拌工作站", "usage_summary": "固定转速搅拌"},
                    ]
                }
            },
        )

        # plan is NOT cleared to manual_required over device-boundary doubts
        self.assertEqual(state.status, "completed")
        self.assertTrue(state.macro_plan)

    def test_protocol_extract_empty_result_degrades_instead_of_aborting(self) -> None:
        """Regression (eval run #1: 8/8 manual_required): when the LLM returns
        no usable protocols, paper_protocol_extract must degrade to the
        heuristic and continue — never raise and collapse the whole B1 to
        manual_required. Protocol extraction is a reference step, not a gate."""

        class EmptyProtocolModel:
            def invoke(self, messages):
                return SimpleNamespace(content=json.dumps({"protocols": []}, ensure_ascii=False))

        agent = ResearchAgent(
            model=EmptyProtocolModel(),
            use_llm=True,
            knowledge_base_dir=str(self.structured_outputs_dir),
        )
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="probe"),
        )
        # No knowledge hits + empty LLM protocols → the step must return a list
        # and NOT raise (the old code called _raise_llm_step_failure here).
        result = agent._step_paper_protocol_extract(state)
        self.assertIsInstance(result, list)
        # the degradation is logged, not raised
        self.assertTrue(
            any("degrad" in entry.lower() or "no usable protocols" in entry.lower()
                for entry in state.logs)
        )

    def test_failure_category_classification(self) -> None:
        """Issue 5: an empty macro plan carries an explicit failure category so
        the UI can tell generation vs. quality vs. network vs. device apart."""
        agent = ResearchAgent(model=None, use_llm=False)
        self.assertEqual(
            agent._classify_failure("macro plan quality check failed: 第 1 步"),
            "macro_quality_error",
        )
        self.assertEqual(
            agent._classify_failure("survey LLM failed: connection timed out"),
            "network_or_retrieval_error",
        )
        self.assertEqual(
            agent._classify_failure("could not parse JSON from response"),
            "macro_generation_error",
        )

    def test_b2_device_adaptation_retries_after_quality_gate_feedback(self) -> None:
        model = DeviceAdaptationRetryModel()
        agent = ResearchAgent(
            model=model,
            use_llm=True,
            knowledge_base_dir=str(self.structured_outputs_dir),
        )
        bootstrap_state = self.agent.run(
            event_type="bootstrap",
            query="合成普鲁士蓝样品并通过 XRD 确认目标物相",
        )

        state = agent.run(
            event_type="new observation",
            query=bootstrap_state.event.query,
            payload={
                "feedback_type": "device_feasibility_error",
                "status": "feasibility_error",
                "previous_macro_plan": bootstrap_state.macro_plan,
                "error_package": {
                    "type": "physical_infeasible",
                    "blocking_constraints": [
                        "250 mL + 250 mL 大体积共沉淀体系不支持。",
                    ],
                    "message": "请改为小体积等比例共沉淀。",
                },
                "device_capabilities": {
                    "supported_containers": ["进样瓶", "西林瓶", "50ml耐热瓶"],
                    "supported_workstations": ["液体进样站", "纯化工作站", "烘干机"],
                },
            },
            previous_state=bootstrap_state,
        )

        self.assertEqual(state.status, "completed")
        self.assertIn("device_adaptation_macro_plan_design", state.raw_llm_outputs)
        self.assertIn("device_adaptation_macro_plan_design_retry_1", state.raw_llm_outputs)
        self.assertEqual(len(model.prompts), 2)
        self.assertIn("本地质量检查反馈", model.prompts[1])
        self.assertIn("洗涤 3 次", str(state.macro_plan))
        self.assertNotIn("洗涤至", str(state.macro_plan))
        self.assertTrue(
            any("passed after quality-feedback retry" in log for log in state.logs)
        )

    def test_temporal_addition_stirring_is_not_a_research_quality_block(self) -> None:
        agent = ResearchAgent(model=None, use_llm=False)
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="合成 PBA",
                constraints={
                    "device_context": {
                        "workstations": [
                            {
                                "station_name": "Liquid_Handling_Station_1ml_V2",
                                "usage_summary": "支持加液",
                            },
                            {
                                "station_name": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
                                "usage_summary": "支持搅拌",
                            },
                        ]
                    }
                },
            )
        )
        macro_plan = [
            {
                "步骤序号": 1,
                "操作": "共沉淀",
                "试剂/对象": "A 液和 B 液",
                "参数": "边滴入边搅拌 10 mL，30 min",
            },
            {
                "步骤序号": 2,
                "操作": "固定时间老化",
                "试剂/对象": "反应悬浊液",
                "参数": "室温静置老化 12 h",
            },
        ]

        issues = agent._device_context_macro_quality_issues(state, macro_plan)

        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()

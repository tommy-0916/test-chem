from __future__ import annotations

import json
import unittest

from agent_skills.capabilities import (
    DEFAULT_INDEX,
    load_capability_tier_skill,
    load_current_capability_index,
)
from reaserch_agent.state import ResearchAgentState, ResearchEvent, SearchHit
from reaserch_agent.tools.device_context import apply_device_status
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

    def test_v2_local_only_action_evidence_never_calls_online_service(self):
        class _LocalQuery:
            def search(self, queries):
                self.queries = list(queries)
                return [
                    SearchHit(
                        title="Local HE-PBA paper",
                        file_path="/kb/he-pba.pdf",
                        score=9.5,
                        problem="irreversible phase transition",
                        synthesis_summary="coprecipitation",
                    )
                ]

        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        agent._online_literature = False
        agent._web_search_enabled = False
        agent._knowledge_query = _LocalQuery()
        agent._online_research_service = lambda state: self.fail(
            "online service must not be constructed in local-only mode"
        )
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="HE-PBA sodium storage")
        )
        state.current_stage = "synthesis"
        state.survey_queries = ["high entropy PBA coprecipitation"]

        agent._refresh_action_evidence(
            state, planning_mode="bootstrap", observation_point="XRD"
        )

        self.assertEqual(
            state.current_evidence_bundle["retrieval_mode"], "local_knowledge"
        )
        self.assertEqual(
            state.current_evidence_bundle["results"][0]["title"],
            "Local HE-PBA paper",
        )
        self.assertEqual(
            state.tool_invocations[-1]["tool"],
            "local_knowledge_search",
        )
        self.assertIn(
            "high entropy PBA coprecipitation",
            agent._knowledge_query.queries,
        )

    def test_v2_macro_step_context_selects_relevant_operation_contracts(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="prepare HE-PBA and observe XRD",
                constraints={"device_context": load_current_capability_index()},
            )
        )
        state.pending_macro_action = {
            "planned_operations": [
                "配制五金属前驱液并受控加液",
                "常温搅拌共沉淀",
                "离心洗涤后烘干",
                "乙醇分散并进行 XRD 检测",
            ]
        }

        projected = agent._projected_constraints(state, "step")["device_context"]
        names = {item["name"] for item in projected["operation_contracts"]}

        self.assertEqual(projected["tier"], "macro_step")
        self.assertNotIn("selection_miss", projected)
        self.assertIn("加液_物料绑定", names)
        self.assertIn("开始搅拌", names)
        self.assertIn("烘干主流程", names)
        self.assertIn("XRD滴液检测全流程", names)
        self.assertLess(len(projected["operation_contracts"]), 61)

    def test_v2_planning_evidence_is_bounded_and_deduplicated(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        records = [
            {
                "paper_id": f"p{index}",
                "title": "duplicate" if index in {0, 1} else f"paper {index}",
                "source": "local",
                "problem": "p" * 10000,
                "synthesis_summary": "s" * 10000,
                "experiment_details": "e" * 10000,
                "steps": [
                    {
                        "operation": "mix solution",
                        "parameters": f"add {step + 1} mL and stir 10 min "
                        + "x" * 5000,
                    }
                    for step in range(10)
                ],
            }
            for index in range(5)
        ]

        compact = agent._compact_action_evidence_for_planning(records)

        self.assertEqual(len(compact), 2)
        self.assertEqual(len({item["title"] for item in compact}), 2)
        self.assertLess(len(json.dumps(compact, ensure_ascii=False)), 5000)
        self.assertTrue(all(len(item["steps"]) == 3 for item in compact))

    def test_macro_step_prompt_projection_keeps_contract_without_audit_noise(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="prepare HE-PBA and observe XRD",
                constraints={"device_context": load_current_capability_index()},
            )
        )
        state.pending_macro_action = {
            "planned_operations": [
                "受控加液并搅拌共沉淀",
                "离心洗涤后烘干",
                "进行 XRD 检测",
            ]
        }

        full = agent._projected_constraints(state, "step")["device_context"]
        compact = agent._compact_macro_step_device_context_for_prompt(full)

        self.assertEqual(
            compact["capability_snapshot_id"], full["capability_snapshot_id"]
        )
        self.assertEqual(
            compact["dependency_station_codes"], full["dependency_station_codes"]
        )
        for key in (
            "instructions",
            "planning_policy",
            "selection_policy",
            "selected_operations",
        ):
            self.assertEqual(compact[key], full[key])
        self.assertTrue(compact["operation_contracts"])
        for operation in compact["operation_contracts"]:
            self.assertIn("availability", operation)
            self.assertIn("currently_usable", operation)
            self.assertIn("input", operation)
            self.assertIn("output", operation)
            self.assertIn("container_contract", operation)
            self.assertIn("scientific_controls", operation)
            self.assertIn("quantity_semantics", operation)
            self.assertIn("feedback_contract", operation)
            self.assertNotIn("io_evidence", operation)
            self.assertNotIn("source", operation)
        self.assertTrue(compact["workstations"])
        self.assertTrue(compact["global_constraints"])
        self.assertNotIn(
            '"sources"', json.dumps(compact, ensure_ascii=False)
        )
        self.assertNotIn(
            '"skill_source_path"', json.dumps(compact, ensure_ascii=False)
        )
        self.assertLess(
            len(json.dumps(compact, ensure_ascii=False)),
            len(json.dumps(full, ensure_ascii=False)) * 0.7,
        )

    def test_macro_step_prompt_projection_preserves_user_planning_policy(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        device_context = load_current_capability_index()
        device_context.update(
            {
                "planning_policy": "必须先制备空白样并限制加热温度。",
                "restrictions": {"maximum_temperature": "50 °C"},
                "excluded_capabilities": ["ultrasonic-treatment"],
                "allowed_capabilities": ["xrd"],
                "allowed_operations": ["XRD滴液检测全流程"],
            }
        )
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="observe a prepared sample by XRD",
                constraints={"device_context": device_context},
            )
        )
        state.pending_macro_action = {
            "planned_operations": ["XRD检测：对悬浊液进行物相表征"]
        }

        full = agent._projected_constraints(state, "step")["device_context"]
        compact = agent._compact_macro_step_device_context_for_prompt(full)

        for key in (
            "instructions",
            "planning_policy",
            "additional_planning_policy",
            "selection_policy",
            "restrictions",
            "excluded_capabilities",
            "allowed_capabilities",
            "allowed_operations",
        ):
            self.assertEqual(compact[key], full[key])

    def test_macro_step_prompt_projection_preserves_offline_status(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        device_context = apply_device_status(
            load_current_capability_index(), {"XRD_V1": "offline"}
        )
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="observe a prepared sample by XRD",
                constraints={"device_context": device_context},
            )
        )
        state.pending_macro_action = {
            "planned_operations": ["XRD检测：对悬浊液进行物相表征"]
        }

        full = agent._projected_constraints(state, "step")["device_context"]
        compact = agent._compact_macro_step_device_context_for_prompt(full)
        operation = next(
            item
            for item in compact["operation_contracts"]
            if item["station_code"] == "XRD_V1"
        )
        workstation = next(
            item
            for item in compact["workstations"]
            if item["station_code"] == "XRD_V1"
        )

        self.assertEqual(operation["availability"], "offline")
        self.assertIs(operation["currently_usable"], False)
        self.assertEqual(workstation["availability"], "offline")
        self.assertIs(workstation["currently_usable"], False)
        self.assertIn("当前不可用", workstation["availability_note"])

    def test_macro_step_prompt_projection_keeps_xrd_four_ml_contract(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="observe a prepared sample by XRD",
                constraints={"device_context": load_current_capability_index()},
            )
        )
        state.pending_macro_action = {
            "planned_operations": ["XRD检测：对悬浊液进行物相表征"]
        }

        full = agent._projected_constraints(state, "step")["device_context"]
        compact = agent._compact_macro_step_device_context_for_prompt(full)
        workstation = next(
            item
            for item in compact["workstations"]
            if item["station_code"] == "XRD_V1"
        )
        constraints = json.dumps(
            workstation["planning_constraints"], ensure_ascii=False
        )
        dependencies = workstation["dependencies"]
        xrd_operation = next(
            item
            for item in compact["operation_contracts"]
            if item["station_code"] == "XRD_V1"
        )

        self.assertIn("≥ 4.0ml", constraints)
        self.assertIn("4.0 mL 无水乙醇", constraints)
        self.assertIn("进样瓶", xrd_operation["input"]["containers"])
        self.assertIn("50ml耐热瓶", xrd_operation["input"]["containers"])
        dry_control = next(
            item
            for item in xrd_operation["scientific_controls"]
            if item["name"] == "静置晾干时间"
        )
        self.assertEqual(dry_control["note"], "1200")
        self.assertTrue(
            any(
                "Spectroscopy_Magnetic_Stirrer_Workstation_V1"
                in dependency.get("station_codes", [])
                for dependency in dependencies
            )
        )
        workstation_json = json.dumps(workstation, ensure_ascii=False)
        self.assertNotIn('"sources"', workstation_json)
        self.assertNotIn('"document"', workstation_json)
        self.assertNotIn('"line"', workstation_json)
        dependency_station = next(
            item
            for item in compact["workstations"]
            if item["station_code"]
            == "Spectroscopy_Magnetic_Stirrer_Workstation_V1"
        )
        self.assertTrue(dependency_station["capability_description"])

    def test_macro_step_prompt_keeps_raw_container_and_template_requirements(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        full = load_capability_tier_skill(
            load_current_capability_index(),
            "macro_step",
            selected_operations=["电化学检测"],
        )

        compact = agent._compact_macro_step_device_context_for_prompt(full)
        operation = next(
            item
            for item in compact["operation_contracts"]
            if item["name"] == "电化学检测"
        )

        self.assertIn("必选", operation["output"]["container_type_raw"][0])
        self.assertEqual(
            operation["output"]["template_requirements"],
            ["该参数必须已填写"],
        )

    def test_colon_operation_label_still_selects_semantic_contract(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="observe a prepared sample by XRD",
                constraints={"device_context": load_current_capability_index()},
            )
        )

        for separator in ("：", ":"):
            with self.subTest(separator=separator):
                state.pending_macro_action = {
                    "planned_operations": [
                        f"XRD检测{separator}对悬浊液进行物相表征"
                    ]
                }
                projected = agent._projected_constraints(state, "step")[
                    "device_context"
                ]
                names = {
                    item["name"] for item in projected["operation_contracts"]
                }

                self.assertEqual(names, {"XRD滴液检测全流程"})
                self.assertNotIn("selection_miss", projected)

    def test_v2_quality_retry_previous_output_is_valid_and_bounded(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        previous = {
            "current_stage_plan": "stage " + "x" * 10000,
            "macro_plan": [
                {
                    "macro_step_id": f"ms-{index}",
                    "步骤序号": index + 1,
                    "操作": "配制并混合",
                    "参数": "p" * 5000,
                    "material_inputs": [
                        {"material": f"salt-{item}", "amount": item + 1, "unit": "g"}
                        for item in range(20)
                    ],
                }
                for index in range(20)
            ],
            "unrelated_trace": "secret" * 10000,
        }

        encoded = agent._macro_plan_retry_result_json(previous)
        decoded = json.loads(encoded)

        self.assertLessEqual(len(encoded), 6000)
        self.assertEqual(decoded["macro_plan"][0]["macro_step_id"], "ms-0")
        self.assertNotIn("unrelated_trace", decoded)

    def test_plain_washing_does_not_pull_ultrasonic_contracts(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="wash and dry a PBA precipitate",
                constraints={"device_context": load_current_capability_index()},
            )
        )

        state.pending_macro_action = {"planned_operations": ["离心洗涤后烘干"]}
        plain_names = {
            item["name"]
            for item in agent._projected_constraints(state, "step")[
                "device_context"
            ]["operation_contracts"]
        }
        self.assertNotIn("超声清洗", plain_names)
        self.assertIn("纯化离心", plain_names)

        state.pending_macro_action = {"planned_operations": ["超声清洗样品"]}
        ultrasonic_names = {
            item["name"]
            for item in agent._projected_constraints(state, "step")[
                "device_context"
            ]["operation_contracts"]
        }
        self.assertIn("超声清洗", ultrasonic_names)

    def test_labeled_action_operations_do_not_match_explanatory_negations(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="prepare a PBA suspension",
                constraints={"device_context": load_current_capability_index()},
            )
        )
        state.pending_macro_action = {
            "planned_operations": [
                "开始搅拌：继续搅拌熟化并避免强制静置换瓶",
                "批量加液流程：加入已装载的前驱体原液",
            ]
        }

        projected = agent._projected_constraints(state, "step")["device_context"]
        names = {item["name"] for item in projected["operation_contracts"]}

        self.assertEqual(names, {"开始搅拌", "批量加液流程"})
        self.assertNotIn("静置", names)
        self.assertNotIn("超声加液流程", names)

    def test_labeled_operations_refine_a_persisted_identity_only_context(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        device_context = load_current_capability_index()
        device_context["capability_index"] = str(DEFAULT_INDEX)
        device_context["workstations"] = [
            {
                "station_name": station.get("station_code", ""),
                "display_name": station.get("display_name", ""),
            }
            for station in device_context["workstations"]
        ]
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="prepare and characterize PBA",
                constraints={"device_context": device_context},
            )
        )
        state.pending_macro_action = {
            "planned_operations": [
                "开始搅拌：继续熟化",
                "纯化离心：洗涤沉淀",
                "XRD滴液检测全流程：记录物相",
            ]
        }

        projected = agent._projected_constraints(state, "step")["device_context"]
        names = {item["name"] for item in projected["operation_contracts"]}

        self.assertEqual(
            names,
            {"开始搅拌", "纯化离心", "XRD滴液检测全流程"},
        )
        self.assertNotIn("离心-复位机制", names)
        self.assertNotIn("selection_miss", projected)

    def test_partial_operation_selection_exposes_the_unknown_label(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="prepare a PBA suspension",
                constraints={"device_context": load_current_capability_index()},
            )
        )
        state.pending_macro_action = {
            "planned_operations": [
                "开始搅拌：继续熟化",
                "不存在的自动化操作：继续处理",
            ]
        }

        projected = agent._projected_constraints(state, "step")["device_context"]
        compact = agent._compact_macro_step_device_context_for_prompt(projected)

        self.assertIs(projected["selection_miss"], True)
        self.assertEqual(
            projected["unmatched_operations"],
            ["不存在的自动化操作"],
        )
        self.assertEqual(
            compact["unmatched_operations"],
            ["不存在的自动化操作"],
        )

    def test_exact_label_normalization_avoids_semantic_overexpansion(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="centrifuge a sample",
                constraints={"device_context": load_current_capability_index()},
            )
        )
        state.pending_macro_action = {
            "planned_operations": ["离心复位机制：完成配平离心"]
        }

        projected = agent._projected_constraints(state, "step")["device_context"]
        names = {item["name"] for item in projected["operation_contracts"]}

        self.assertEqual(names, {"离心-复位机制"})
        self.assertNotIn("selection_miss", projected)


if __name__ == "__main__":
    unittest.main()

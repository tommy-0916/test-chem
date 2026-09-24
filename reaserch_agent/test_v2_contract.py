from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_skills.capabilities import (
    DEFAULT_INDEX,
    load_capability_tier_skill,
    load_current_capability_index,
)
from chem_agent_contracts.v2 import canonical_digest
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
    def test_v2_generated_user_provenance_is_stamped_before_quality_gate(self):
        query = "Prepare a catalyst with 4.0 mL ethanol"
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query=query)
        )
        state.current_stage = "sample preparation"
        state.current_evidence_bundle = {"query": query, "results": []}
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        agent._use_llm = True
        agent._step_macro_action_design = lambda _state, _mode: None
        agent._device_context_macro_step_markers = lambda _state, _plan: []
        raw_step = {
            "步骤序号": 1,
            "操作": "加入乙醇",
            "试剂/对象": "ethanol",
            "参数": "4.0 mL ethanol",
            "provenance": {
                "kind": "user",
                "reference": "current_query",
                "source_path": "evidence_bundle.query",
                "excerpt": "4.0 mL ethanol",
            },
        }
        agent._invoke_state_json = lambda *_args, **_kwargs: {
            "current_stage_plan": "Prepare the sample for observation",
            "macro_plan": [raw_step],
        }
        observed_digests = []
        original_quality_gate = agent._macro_plan_quality_issues

        def quality_gate(macro_plan, _query, state=None):
            # Keep the real provenance stamping/validation path while this
            # focused generator test ignores unrelated material fields.
            original_quality_gate(macro_plan, _query, state=state)
            observed_digests.append(macro_plan[0]["provenance"].get("source_digest"))
            return []

        agent._macro_plan_quality_issues = quality_gate

        result = agent._step_macro_plan_design(state)

        expected = canonical_digest(query)
        self.assertEqual(observed_digests, [expected])
        self.assertEqual(result["macro_plan"][0]["provenance"]["source_digest"], expected)
        self.assertNotIn("source_digest", raw_step["provenance"])

    def test_v2_query_stamp_never_repairs_wrong_excerpt_or_forged_digest(self):
        query = "Prepare a catalyst with 4.0 mL ethanol"
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query=query)
        )
        state.current_evidence_bundle = {"query": query, "results": []}
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        candidates = [
            {
                "provenance": {
                    "kind": "user",
                    "reference": "current_query",
                    "source_path": "evidence_bundle.query",
                    "excerpt": "9.0 mL methanol",
                }
            },
            {
                "provenance": {
                    "kind": "user",
                    "reference": "current_query",
                    "source_path": "evidence_bundle.query",
                    "excerpt": "4.0 mL ethanol",
                    "source_digest": canonical_digest("a different query"),
                }
            },
        ]

        agent._stamp_current_evidence_provenance(state, candidates)

        self.assertNotIn("source_digest", candidates[0]["provenance"])
        self.assertEqual(
            candidates[1]["provenance"]["source_digest"],
            canonical_digest("a different query"),
        )
        for candidate in candidates:
            with self.subTest(provenance=candidate["provenance"]):
                issues = agent._v2_material_provenance_issues(1, candidate, state)
                self.assertTrue(
                    any("未绑定当前用户任务输入" in issue for issue in issues),
                    issues,
                )

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
        query = "prepare sample"
        issues = agent._macro_plan_quality_issues(
            [
                {
                    "步骤序号": 1,
                    "操作": "加入前驱体",
                    "试剂/对象": "前驱体",
                    "参数": "加入适量前驱体并搅拌 10 min",
                    "material_inputs": [],
                    "material_intermediates": [],
                    "material_outputs": [],
                    "material_relations": [],
                    "container_requirements": [],
                    "intermediate_returns": [],
                    "material_contract_status": {
                        "material_inputs": "declared",
                        "material_intermediates": "unresolved",
                        "material_outputs": "unresolved",
                        "logical_containers": "unresolved",
                        "material_relations": "unresolved",
                    },
                    "operation_segments": [
                        {
                            "segment_id": "add_precursor",
                            "material_effect": "consume_material",
                            "source_operation_ref": "加入前驱体",
                            "provenance": {
                                "kind": "user",
                                "reference": "current_query",
                                "source_path": "evidence_bundle.query",
                                "excerpt": query,
                                "source_digest": canonical_digest(query),
                            },
                        }
                    ],
                }
            ],
            query,
        )
        self.assertFalse(any("material_contract_status 无效" in issue for issue in issues))
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

    def test_v2_local_evidence_is_attached_as_structured_paper_provenance(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        temporary_source = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_source.cleanup)
        source_file = Path(temporary_source.name) / "protocol.json"
        excerpt = "配制前驱体溶液：Ni salt and water, 1 mmol in 10 mL, stir 10 min"
        source_file.write_text(
            json.dumps({"protocol_excerpt": excerpt}, ensure_ascii=False),
            encoding="utf-8",
        )
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare catalyst")
        )
        state.current_evidence_bundle = {
            "results": [
                {
                    "paper_id": "local_protocol_1",
                    "title": "Local protocol",
                    "source": "local_knowledge_base",
                    "verification_status": "local_file",
                    "full_text_status": "local_parsed",
                    "corpus_files": [str(source_file)],
                    "evidence_excerpt": excerpt,
                    "steps": [
                        {
                            "操作": "配制前驱体溶液",
                            "试剂/对象": "Ni salt and water",
                            "参数": "1 mmol in 10 mL, stir 10 min",
                        }
                    ],
                }
            ]
        }
        state.macro_plan = [
            {
                "步骤序号": 1,
                "操作": "配制前驱体溶液",
                "试剂/对象": "Ni salt and water",
                "参数": "1 mmol in 10 mL, stir 10 min",
            }
        ]
        # Production validates the normalized V2 macro shape.  This test is
        # about provenance attachment, so exercise the same boundary instead
        # of passing a deliberately pre-normalization record to the gate.
        state.macro_plan = agent._normalize_macro_plan(state.macro_plan)

        agent._annotate_macro_plan_sources(state)

        step = state.macro_plan[0]
        self.assertEqual(step["provenance"]["kind"], "paper")
        self.assertEqual(step["provenance"]["reference"], "local_protocol_1")
        self.assertEqual(
            step["provenance"]["source_path"],
            "evidence_bundle.items[0].excerpt",
        )
        self.assertEqual(step["provenance"]["excerpt"], excerpt)
        self.assertEqual(step["provenance"]["source_digest"], canonical_digest(excerpt))
        self.assertIn(excerpt, source_file.read_text(encoding="utf-8"))
        self.assertEqual(step["来源"], step["provenance"]["reference"])
        self.assertTrue(agent._valid_v2_provenance(step["provenance"]))

    def test_v2_local_file_excerpt_is_bound_to_its_exact_source(self):
        verified_summary = (
            "Ni salt and water are mixed as 1 mmol in 10 mL, stir 10 min "
            "to prepare a precursor solution."
        )
        protocol_step = {
            "操作": "配制前驱体溶液",
            "试剂/对象": "Ni salt and water",
            "参数": "1 mmol in 10 mL, stir 10 min",
        }
        payload = {
            "文献题目": "Local protocol",
            "2. 具体的合成步骤": {
                "描述性总结": verified_summary,
                "参数列表": [protocol_step],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "protocol.json"
            source_text = json.dumps(payload, ensure_ascii=False)
            source.write_text(source_text, encoding="utf-8")
            hit = SearchHit(
                title="Local protocol",
                file_path=str(source),
                score=9.5,
                problem="",
                synthesis_summary=verified_summary,
                steps=[protocol_step],
            )

            class _LocalQuery:
                def search(self, _queries):
                    return [hit]

            agent = ResearchAgent.__new__(ResearchAgent)
            agent._contract_version = "v2"
            agent._online_literature = False
            agent._web_search_enabled = False
            agent._knowledge_base_dir = directory
            agent._knowledge_query = _LocalQuery()
            state = ResearchAgentState(
                event=ResearchEvent(event_type="bootstrap", query="prepare catalyst")
            )
            state.current_stage = "synthesis"
            agent._refresh_action_evidence(
                state, planning_mode="bootstrap", observation_point="XRD"
            )

            evidence = state.current_evidence_bundle["results"][0]
            excerpt = evidence.get("evidence_excerpt", "")
            self.assertTrue(excerpt)
            self.assertIn(excerpt, source_text)
            self.assertIn("1 mmol in 10 mL, stir 10 min", excerpt)
            state.macro_plan = [{"步骤序号": 1, **protocol_step}]
            agent._annotate_macro_plan_sources(state)
            provenance = state.macro_plan[0]["provenance"]
            self.assertEqual(provenance["kind"], "paper")
            self.assertEqual(provenance["reference"], evidence["paper_id"])
            self.assertEqual(
                provenance["source_path"], "evidence_bundle.items[0].excerpt"
            )
            self.assertEqual(provenance["source_digest"], canonical_digest(excerpt))
            compact = agent._compact_action_evidence_for_planning(
                state.current_evidence_bundle["results"]
            )
            self.assertEqual(
                compact[0]["evidence_source_path"], provenance["source_path"]
            )
            self.assertEqual(
                agent._v2_material_provenance_issues(1, state.macro_plan[0], state),
                [],
            )
            with tempfile.TemporaryDirectory() as outside_directory:
                outside_source = Path(outside_directory) / "protocol.json"
                outside_source.write_text(source_text, encoding="utf-8")
                outside_hit = SearchHit(
                    title=hit.title,
                    file_path=str(outside_source),
                    score=hit.score,
                    problem=hit.problem,
                    synthesis_summary=hit.synthesis_summary,
                    steps=hit.steps,
                )
                self.assertEqual(agent._verified_local_evidence_fields(outside_hit), {})

    def test_v2_missing_local_source_cannot_create_paper_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_source = Path(directory) / "missing.json"
            protocol_step = {
                "操作": "配制前驱体溶液",
                "试剂/对象": "Ni salt and water",
                "参数": "1 mmol in 10 mL, stir 10 min",
            }
            hit = SearchHit(
                title="Unverifiable local protocol",
                file_path=str(missing_source),
                score=9.5,
                problem="",
                synthesis_summary="Ni salt and water are mixed",
                steps=[protocol_step],
            )

            class _LocalQuery:
                def search(self, _queries):
                    return [hit]

            agent = ResearchAgent.__new__(ResearchAgent)
            agent._contract_version = "v2"
            agent._online_literature = False
            agent._web_search_enabled = False
            agent._knowledge_base_dir = directory
            agent._knowledge_query = _LocalQuery()
            state = ResearchAgentState(
                event=ResearchEvent(event_type="bootstrap", query="prepare catalyst")
            )
            state.current_stage = "synthesis"
            agent._refresh_action_evidence(
                state, planning_mode="bootstrap", observation_point="XRD"
            )

            evidence = state.current_evidence_bundle["results"][0]
            self.assertFalse(evidence.get("evidence_excerpt"))
            state.macro_plan = [{"步骤序号": 1, **protocol_step}]
            agent._annotate_macro_plan_sources(state)
            provenance = state.macro_plan[0]["provenance"]
            self.assertEqual(provenance["kind"], "agent_inferred")
            self.assertEqual(
                agent._v2_material_provenance_issues(1, state.macro_plan[0], state),
                [],
            )

    def test_v2_unmatched_local_step_is_explicitly_agent_inferred(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare catalyst")
        )
        state.current_evidence_bundle = {
            "results": [
                {
                    "paper_id": "web_only",
                    "title": "Unverified web record",
                    "source": "local_knowledge_base",
                    "verification_status": "web_unverified",
                    "full_text_status": "local_parsed",
                    "steps": [
                        {
                            "操作": "配制前驱体溶液",
                            "试剂/对象": "Ni salt and water",
                            "参数": "1 mmol in 10 mL",
                        }
                    ],
                }
            ]
        }
        state.macro_plan = [
            {
                "步骤序号": 1,
                "操作": "执行本地模板操作",
                "试剂/对象": "sample",
                "参数": "25 C for 10 min",
            }
        ]

        agent._annotate_macro_plan_sources(state)

        provenance = state.macro_plan[0]["provenance"]
        self.assertEqual(provenance["kind"], "agent_inferred")
        self.assertTrue(provenance["rationale"])
        self.assertEqual(provenance["reference"], "local_heuristic_planner")
        self.assertNotIn("web_only", provenance["reference"])

    def test_v2_existing_structured_provenance_survives_annotation(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare catalyst")
        )
        state.current_evidence_bundle = {"results": []}
        state.macro_plan = [
            {
                "步骤序号": 1,
                "操作": "观察样品",
                "试剂/对象": "sample",
                "参数": "25 C for 10 min",
                "provenance": {
                    "kind": "agent_inferred",
                    "reference": "existing_rule_v1",
                    "rationale": "existing structured provenance",
                },
            }
        ]

        agent._annotate_macro_plan_sources(state)

        self.assertEqual(
            state.macro_plan[0]["provenance"]["reference"], "existing_rule_v1"
        )
        self.assertEqual(state.macro_plan[0]["来源"], "existing_rule_v1")

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
        records[2]["evidence_excerpt"] = "Verbatim local source excerpt"
        records[2]["score"] = 100

        compact = agent._compact_action_evidence_for_planning(records)

        self.assertEqual(len(compact), 2)
        self.assertEqual(compact[0]["paper_id"], "p2")
        self.assertEqual(
            compact[0]["evidence_source_path"], "evidence_bundle.items[2].excerpt"
        )
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

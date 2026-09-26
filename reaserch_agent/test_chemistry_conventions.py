"""Scientific semantics Phase 1+2 tests: chemistry-convention expansion,
bare agent_inferred gating, and the boundary-B dual-graph checklist.

Deterministic unittest: no LLM, no network (sockets are blocked).
"""

from __future__ import annotations

import copy
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reaserch_agent.workflow import ResearchAgent
from chem_agent_contracts.v2 import (
    MacroStepV2,
    MaterialPortV2,
    ProvenanceV2,
    LineageRelationV2,
    StateTransitionV2,
    normalize_material_state,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONVENTIONS_PATH = (
    REPO_ROOT / "chem_resources" / "chemistry_conventions" / "conventions.json"
)


def centrifuge_plan(intent: str) -> list:
    return [
        {
            "步骤序号": 1,
            "macro_step_id": "MS_001",
            "操作": "离心分离",
            "试剂/对象": intent,
            "参数": "8000 rpm 10 min",
            "provenance": {"kind": "paper", "reference": "evidence_x"},
            "material_inputs": [
                {
                    "material_id": "product",
                    "material_instance_id": "product_susp_01",
                    "name": "反应悬浊液",
                    "state": "悬浊液",
                    "material_origin": "external_inventory",
                    "parent_output_refs": [],
                    "quantity": {
                        "mode": "exact",
                        "semantic": "planned_target",
                        "value": 10,
                        "unit": "mL",
                    },
                    "provenance": {
                        "kind": "paper",
                        "reference": "evidence_x",
                        "source_path": "evidence_bundle.items[0].excerpt",
                        "excerpt": "将悬浊液离心",
                    },
                }
            ],
            "material_outputs": [
                {
                    "material_id": "product",
                    "material_instance_id": "product_retained_01",
                    "name": "离心保留相",
                    "state": "unknown",
                    "provenance": {"kind": "agent_inferred", "rationale": "惯例保留相"},
                },
                {
                    "material_id": "product",
                    "material_instance_id": "product_discard_01",
                    "name": "离心弃去相",
                    "state": "unknown",
                    "provenance": {"kind": "agent_inferred", "rationale": "惯例弃去相"},
                }
            ],
        }
    ]


class ConventionResourceTest(unittest.TestCase):
    def test_resource_shape_and_numeric_guard(self):
        payload = json.loads(CONVENTIONS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("schema"), "chemistry-conventions/v1")
        rules = payload["rules"]
        self.assertGreaterEqual(len(rules), 9)
        operations = {rule["operation"] for rule in rules}
        self.assertEqual(
            operations,
            {
                "centrifugation",
                "washing",
                "drying",
                "filtration",
                "aliquot",
                "transfer",
                "split",
                "merge",
            },
        )
        for rule in rules:
            for key in (
                "rule_id",
                "version",
                "operation",
                "preconditions",
                "allowed_input_states",
                "output_states",
                "retained_output",
                "discard_outputs",
                "lineage_effect",
                "numeric_generation_allowed",
            ):
                self.assertIn(key, rule, rule["rule_id"])
            self.assertIs(rule["numeric_generation_allowed"], False, rule["rule_id"])
            self.assertIn("intent_patterns", rule["preconditions"])
            self.assertIn("operation_patterns", rule["preconditions"])

    def test_precipitate_and_supernatant_are_distinct_rules(self):
        payload = json.loads(CONVENTIONS_PATH.read_text(encoding="utf-8"))
        by_id = {rule["rule_id"]: rule for rule in payload["rules"]}
        precipitate = by_id["CENTRIFUGE_COLLECT_PRECIPITATE_V1"]
        supernatant = by_id["CENTRIFUGE_COLLECT_SUPERNATANT_V1"]
        self.assertEqual(precipitate["retained_output"], "retained_wet_solid")
        self.assertEqual(supernatant["retained_output"], "supernatant")
        self.assertIn("supernatant", precipitate["discard_outputs"])
        self.assertIn("retained_wet_solid", supernatant["discard_outputs"])


class ExpansionTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, "connect", side_effect=AssertionError("offline")).start()
        patch.object(socket.socket, "connect_ex", side_effect=AssertionError("offline")).start()
        patch.object(socket, "create_connection", side_effect=AssertionError("offline")).start()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.agent = ResearchAgent(
            model=object(),
            use_llm=True,
            knowledge_base_dir=directory.name,
            memory_dir=directory.name,
            enable_memory=False,
            enable_online_literature=False,
            enable_web_search=False,
            contract_version="v2",
        )

    def test_precipitate_rule_expands_states_and_lineage_only(self):
        plan = centrifuge_plan("离心收集沉淀，保留沉淀")
        before = copy.deepcopy(plan)
        records = self.agent._expand_chemistry_conventions(plan)
        self.assertEqual([r["rule_id"] for r in records], ["CENTRIFUGE_COLLECT_PRECIPITATE_V1"])
        step = plan[0]
        self.assertEqual(
            step["state_transition"],
            {
                "before_state": "suspension",
                "after_state": "retained_wet_solid",
                "confidence": "convention",
            },
        )
        self.assertEqual(step["lineage_relation"]["relation_type"], "state_change_of")
        self.assertEqual(
            step["lineage_relation"]["parent_material_instance_ids"],
            ["product_susp_01"],
        )
        # provenance keeps kind=agent_inferred and gains convention evidence
        output_provenance = step["material_outputs"][0]["provenance"]
        self.assertEqual(output_provenance["kind"], "agent_inferred")
        self.assertEqual(output_provenance["evidence_class"], "chemistry_convention")
        self.assertEqual(
            output_provenance["inference_rule"], "CENTRIFUGE_COLLECT_PRECIPITATE_V1"
        )
        # paper-bound inputs are untouched
        self.assertNotIn("evidence_class", step["material_inputs"][0]["provenance"])
        # numeric guard: quantities and parameters are never written
        self.assertEqual(
            plan[0]["material_inputs"][0]["quantity"],
            before[0]["material_inputs"][0]["quantity"],
        )
        self.assertEqual(plan[0]["参数"], before[0]["参数"])
        self.assertFalse(
            any(
                "quantity" in material and material["quantity"] != before_material.get("quantity")
                for material, before_material in zip(
                    plan[0]["material_outputs"], before[0]["material_outputs"]
                )
            )
        )

    def test_supernatant_rule_retains_supernatant(self):
        plan = centrifuge_plan("离心后保留上清液")
        records = self.agent._expand_chemistry_conventions(plan)
        self.assertEqual([r["rule_id"] for r in records], ["CENTRIFUGE_COLLECT_SUPERNATANT_V1"])
        self.assertEqual(plan[0]["state_transition"]["after_state"], "supernatant")

    def test_no_match_no_expansion(self):
        plan = centrifuge_plan("静置老化")
        records = self.agent._expand_chemistry_conventions(plan)
        self.assertEqual(records, [])
        self.assertNotIn("state_transition", plan[0])

    def test_drying_rule_never_generates_mass(self):
        plan = [
            {
                "步骤序号": 1,
                "macro_step_id": "MS_001",
                "操作": "真空干燥",
                "试剂/对象": "洗涤后湿固体",
                "参数": "60 C overnight",
                "provenance": {"kind": "paper", "reference": "evidence_x"},
                "material_inputs": [
                    {
                        "material_id": "product",
                        "material_instance_id": "wet_01",
                        "name": "湿固体",
                        "state": "washed_wet_solid",
                        "material_origin": "external_inventory",
                        "parent_output_refs": [],
                    }
                ],
                "material_outputs": [
                    {
                        "material_id": "product",
                        "material_instance_id": "dry_01",
                        "name": "干燥产物",
                        "state": "unknown",
                        "provenance": {"kind": "agent_inferred", "rationale": "惯例"},
                    }
                ],
            }
        ]
        records = self.agent._expand_chemistry_conventions(plan)
        self.assertEqual([r["rule_id"] for r in records], ["DRYING_V1"])
        step = plan[0]
        self.assertEqual(step["state_transition"]["after_state"], "dry_solid")
        # DRYING_V1: mass stays runtime_pending -- no numeric value invented.
        output = step["material_outputs"][0]
        self.assertNotIn("quantity", output)
        self.assertNotIn("value", output)

    def test_bare_agent_inferred_numeric_fact_is_blocked(self):
        plan = centrifuge_plan("离心收集沉淀，保留沉淀")
        plan[0]["material_outputs"][0]["quantity"] = {
            "mode": "exact",
            "semantic": "planning_estimate",
            "value": 8.0,
            "unit": "mg",
        }
        # convention expansion covers it with a rule_id -> publishable
        self.agent._expand_chemistry_conventions(plan)
        self.assertEqual(self.agent._v2_bare_agent_inferred_issues(plan), [])
        # strip the rule_id -> bare agent_inferred numeric fact blocked
        plan[0]["material_outputs"][0]["provenance"].pop("inference_rule")
        plan[0]["material_outputs"][0]["provenance"].pop("evidence_class")
        issues = self.agent._v2_bare_agent_inferred_issues(plan)
        self.assertEqual(len(issues), 1)
        self.assertIn("裸 agent_inferred", issues[0])


class GraphChecklistTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, "connect", side_effect=AssertionError("offline")).start()
        patch.object(socket.socket, "connect_ex", side_effect=AssertionError("offline")).start()
        patch.object(socket, "create_connection", side_effect=AssertionError("offline")).start()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.agent = ResearchAgent(
            model=object(),
            use_llm=True,
            knowledge_base_dir=directory.name,
            memory_dir=directory.name,
            enable_memory=False,
            enable_online_literature=False,
            enable_web_search=False,
            contract_version="v2",
        )

    @staticmethod
    def _material(instance_id, origin="upstream_output", refs=None, state="suspension"):
        material = {
            "material_id": "product",
            "material_instance_id": instance_id,
            "name": instance_id,
            "state": state,
            "material_origin": origin,
            "parent_output_refs": refs or [],
        }
        return material

    def _relation(self, inputs, outputs):
        return {
            "relation_id": "MR_1",
            "event_kind": "state_change",
            "input_material_instance_ids": inputs,
            "output_material_instance_ids": outputs,
            "logical_container_ids": [],
            "quantity_basis": "whole_batch",
            "source_operation_ref": "MS_001/OP_01",
            "provenance": {"kind": "paper", "reference": "evidence_x"},
        }

    def test_traceable_plan_passes(self):
        plan = [
            {
                "步骤序号": 1,
                "操作": "共沉淀",
                "material_inputs": [
                    self._material("root_01", origin="external_inventory")
                ],
                "material_outputs": [self._material("mid_01")],
                "material_relations": [self._relation(["root_01"], ["mid_01"])],
            },
            {
                "步骤序号": 2,
                "操作": "离心收集沉淀",
                "material_inputs": [
                    self._material("mid_01", refs=[{"macro_step_id": "MS_001", "material_instance_id": "mid_01"}])
                ],
                "material_outputs": [self._material("final_01", state="retained_wet_solid")],
                "material_relations": [self._relation(["mid_01"], ["final_01"])],
            },
        ]
        self.assertEqual(self.agent._v2_material_graph_issues(plan, []), [])

    def test_unknown_input_instance_flagged(self):
        plan = [
            {
                "步骤序号": 1,
                "操作": "离心",
                "material_inputs": [self._material("ghost_01")],
                "material_outputs": [self._material("out_01")],
            }
        ]
        issues = self.agent._v2_material_graph_issues(plan, [])
        self.assertTrue(any("先前不存在" in issue for issue in issues))

    def test_orphan_output_flagged(self):
        plan = [
            {
                "步骤序号": 1,
                "操作": "共沉淀",
                "material_inputs": [
                    self._material("root_01", origin="external_inventory")
                ],
                "material_outputs": [self._material("mid_01")],
                "material_relations": [self._relation(["root_01"], ["mid_01"])],
            },
            {
                "步骤序号": 2,
                "操作": "分取",
                "material_inputs": [
                    self._material("mid_01", refs=[{"macro_step_id": "MS_001", "material_instance_id": "mid_01"}])
                ],
                "material_outputs": [self._material("final_01"), self._material("orphan_01")],
                "material_relations": [
                    self._relation(["mid_01"], ["final_01"]),
                    self._relation(["mid_01"], ["orphan_01"]),
                ],
            },
            {
                "步骤序号": 3,
                "操作": "表征",
                "material_inputs": [
                    self._material("final_01", refs=[{"macro_step_id": "MS_002", "material_instance_id": "final_01"}])
                ],
                "material_outputs": [self._material("observed_01", state="dry_solid")],
                "material_relations": [self._relation(["final_01"], ["observed_01"])],
            },
        ]
        issues = self.agent._v2_material_graph_issues(plan, [])
        self.assertTrue(any("orphan output" in issue for issue in issues))

    def test_merge_missing_child_flagged(self):
        plan = [
            {
                "步骤序号": 1,
                "操作": "合并",
                "material_inputs": [
                    self._material("a_01", origin="external_inventory"),
                    self._material("b_01", origin="external_inventory"),
                ],
                "material_outputs": [self._material("merged_01")],
                "lineage_relation": {
                    "relation_type": "merge_from_children",
                    "parent_material_instance_ids": ["a_01", "b_01", "ghost_child"],
                    "child_material_instance_ids": ["merged_01"],
                },
            }
        ]
        issues = self.agent._v2_material_graph_issues(plan, [])
        self.assertTrue(any("merge 输出未追到全部 children" in issue for issue in issues))

    def test_discard_phase_cannot_be_declared_as_retained_output(self):
        plan = centrifuge_plan("离心收集沉淀，保留沉淀")
        self.agent._expand_chemistry_conventions(plan)
        # corrupted plan: declares the discarded supernatant as an output state
        plan[0]["material_outputs"][1]["state"] = "supernatant"
        records = [
            {
                "step_index": 0,
                "rule_id": "CENTRIFUGE_COLLECT_PRECIPITATE_V1",
                "discard_outputs": ["supernatant"],
                "retained_output": "retained_wet_solid",
            }
        ]
        issues = self.agent._v2_material_graph_issues(plan, records)
        self.assertTrue(any("discard 相" in issue for issue in issues))


class StateVocabularyTest(unittest.TestCase):
    def test_normalization_converges(self):
        self.assertEqual(normalize_material_state("悬浊液"), "suspension")
        self.assertEqual(normalize_material_state("Washed_Wet_Solid".lower()), "washed_wet_solid")
        self.assertEqual(normalize_material_state("something-else"), "unknown")
        self.assertEqual(normalize_material_state(""), "unknown")

    def test_port_state_converges_in_model(self):
        port = MaterialPortV2(
            material_id="m",
            name="n",
            state="上清液",
            provenance={"kind": "user"},
        )
        self.assertEqual(port.state, "supernatant")
        port_unknown = MaterialPortV2(
            material_id="m",
            name="n",
            state="不认识的态",
            provenance={"kind": "user"},
        )
        self.assertEqual(port_unknown.state, "unknown")

    def test_evidence_class_kind_mapping(self):
        with self.assertRaises(ValueError):
            ProvenanceV2(kind="paper", reference="r", evidence_class="device_sop")
        with self.assertRaises(ValueError):
            ProvenanceV2(
                kind="agent_inferred",
                rationale="r",
                evidence_class="chemistry_convention",
            )
        ok = ProvenanceV2(
            kind="agent_inferred",
            rationale="r",
            evidence_class="chemistry_convention",
            inference_rule="WASHING_V1",
        )
        self.assertEqual(ok.inference_rule, "WASHING_V1")
        paper = ProvenanceV2(kind="paper", reference="r", evidence_class="paper_explicit")
        self.assertEqual(paper.evidence_class, "paper_explicit")

    def test_dual_graph_models(self):
        transition = StateTransitionV2(
            before_state="湿固体", after_state="干粉", confidence="convention"
        )
        self.assertEqual(transition.before_state, "retained_wet_solid")
        self.assertEqual(transition.after_state, "dry_solid")
        lineage = LineageRelationV2(
            relation_type="split_from_parent",
            parent_material_instance_ids=["p1"],
            child_material_instance_ids=["c1", "c2"],
        )
        self.assertEqual(lineage.relation_type, "split_from_parent")
        with self.assertRaises(ValueError):
            LineageRelationV2(
                relation_type="split_from_parent",
                parent_material_instance_ids=["p1", "p2"],
                child_material_instance_ids=["c1"],
            )

    def test_macro_step_dual_graph_roundtrip(self):
        step = MacroStepV2(
            macro_step_id="MS_1",
            macro_action_id="MA_1",
            sequence=1,
            operation="离心收集沉淀",
            sample_id="S1",
            state_transition={
                "before_state": "悬浊液",
                "after_state": "湿固体",
                "confidence": "convention",
            },
            lineage_relation={
                "relation_type": "state_change_of",
                "parent_material_instance_ids": ["p1"],
                "child_material_instance_ids": ["c1"],
            },
            container_lineage={
                "before_container_id": "reactor_01",
                "after_container_id": "tube_01",
            },
            provenance={"kind": "paper", "reference": "r"},
        )
        self.assertEqual(step.state_transition.before_state, "suspension")
        self.assertEqual(step.lineage_relation.relation_type, "state_change_of")
        self.assertEqual(step.container_lineage.after_container_id, "tube_01")


if __name__ == "__main__":
    unittest.main()

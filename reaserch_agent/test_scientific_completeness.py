"""Scientific semantics Phase 3+4 tests: completeness audit gates, evidence
ladder, extraction locator schema, and the frozen A01 six-tuple regression.

The A01 fixture is a read-only copy of the frozen campaign package
(campaigns/A01-device-k3-fresh-20260920-140057/device_state.json); campaigns/
itself is never touched.
"""

from __future__ import annotations

import copy
import json
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reaserch_agent.workflow import ResearchAgent
from reaserch_agent.state import ResearchAgentState, ResearchEvent
from chem_agent_contracts.v2 import (
    ResearchActionPackageV2,
    ScientificCompletenessV2,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "a01_frozen_package.json"


def make_agent() -> ResearchAgent:
    directory = tempfile.TemporaryDirectory()
    agent = ResearchAgent(
        model=object(),
        use_llm=True,
        knowledge_base_dir=directory.name,
        memory_dir=directory.name,
        enable_memory=False,
        enable_online_literature=False,
        enable_web_search=False,
        contract_version="v2",
    )
    agent._tmpdir = directory
    return agent


def base_step(number: int, operation: str = "搅拌") -> dict:
    return {
        "步骤序号": number,
        "macro_step_id": f"MS_{number:03d}",
        "操作": operation,
        "试剂/对象": "样品",
        "参数": "室温 10 min",
        "provenance": {"kind": "paper", "reference": "evidence_x"},
        "material_inputs": [],
        "material_outputs": [],
        "material_relations": [],
    }


def runtime_plan() -> list[dict]:
    """One batch is produced, weighed whole, then used with a mass dependency."""
    material = {
        "material_id": "product", "material_instance_id": "dry_01",
        "name": "干燥产物", "state": "dry_solid",
        "provenance": {"kind": "paper", "reference": "evidence_x"},
    }
    produce = base_step(1, "干燥")
    produce["material_outputs"] = [{**material, "quantity": {
        "mode": "runtime_measured", "semantic": "runtime_measurement_required",
        "unit": "mg",
    }}]
    measure = base_step(2, "定量称量干燥产物")
    measure["material_inputs"] = [{**material, "quantity": {
        "mode": "all_available", "semantic": "whole_batch_unspecified",
    }}]
    measure["intermediate_returns"] = [{
        "name": "样品质量", "material_instance_id": "dry_01", "unit": "mg",
        "required_for_next_step": True, "availability": "declared",
        "delivery_mode": "automatic", "feedback_kind": "returned_data",
        "source": {"station_code": "BALANCE", "operation": "称量"},
    }]
    consume = base_step(3, "下游配料")
    consume["material_inputs"] = [material.copy()]
    return [produce, measure, consume]


def runtime_state() -> ResearchAgentState:
    return ResearchAgentState(event=ResearchEvent("bootstrap", "test", {
        "device_context": {"workstations": [{
            "station_code": "BALANCE", "availability": "online",
            "operations": [{"name": "称量", "feedback_contract": {
                "returned_data": {"status": "supported", "fields": ["样品质量"],
                                  "field_units": {"样品质量": "mg"}},
            }}],
        }]},
    }))


class CompletenessAuditTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, "connect", side_effect=AssertionError("offline")).start()
        patch.object(socket.socket, "connect_ex", side_effect=AssertionError("offline")).start()
        patch.object(socket, "create_connection", side_effect=AssertionError("offline")).start()
        self.agent = make_agent()
        self.addCleanup(self.agent._tmpdir.cleanup)

    def test_clean_plan_audit_counts(self):
        plan = [base_step(1), base_step(2, "XRD表征")]
        audit = self.agent._scientific_completeness_audit(plan, [])
        self.assertEqual(audit["counts"]["required_unsupported"], 0)
        self.assertEqual(audit["counts"]["unresolved_runtime_dependency"], 0)
        self.assertEqual(audit["counts"]["scientific_fidelity_risk_requiring_review"], 0)
        self.assertEqual(audit["broken_material_lineage"], 0)
        self.assertEqual(audit["evidence_class_counts"]["paper_explicit"], 2)

    def test_unsupported_required_false_does_not_block(self):
        step = base_step(1)
        step["material_outputs"] = [
            {
                "material_id": "byproduct",
                "material_instance_id": "bp_01",
                "name": "副产物",
                "state": "solution",
                "provenance": {"kind": "bogus"},
            }
        ]
        audit = self.agent._scientific_completeness_audit([step, base_step(2)], [])
        self.assertEqual(len(audit["unsupported"]), 1)
        self.assertFalse(audit["unsupported"][0]["required"])
        self.assertEqual(audit["counts"]["required_unsupported"], 0)
        self.assertIn("unsupported", audit["unsupported"][0]["ladder_exhausted"])

    def test_unsupported_required_true_blocks(self):
        step1 = base_step(1)
        step1["material_outputs"] = [
            {
                "material_id": "product",
                "material_instance_id": "p_01",
                "name": "产物",
                "state": "unknown",
            }
        ]
        step2 = base_step(2)
        step2["material_inputs"] = [
            {
                "material_id": "product",
                "material_instance_id": "p_01",
                "name": "产物",
                "state": "unknown",
                "material_origin": "upstream_output",
                "parent_output_refs": [
                    {"macro_step_id": "MS_001", "material_instance_id": "p_01"}
                ],
            }
        ]
        audit = self.agent._scientific_completeness_audit([step1, step2], [])
        self.assertEqual(audit["counts"]["required_unsupported"], 1)
        self.assertTrue(audit["unsupported"][0]["required"])

    def test_runtime_pending_with_and_without_resolution_path(self):
        plan = runtime_plan()
        unresolved = self.agent._scientific_completeness_audit(
            [plan[0], plan[2]], [], state=runtime_state(),
        )
        self.assertEqual(unresolved["counts"]["unresolved_runtime_dependency"], 1)
        self.assertEqual(
            unresolved["unresolved_runtime_dependencies"][0]["missing_resolution"],
            "no_measurement_scheduled",
        )
        resolved = self.agent._scientific_completeness_audit(
            plan, [], state=runtime_state(),
        )
        self.assertEqual(resolved["counts"]["unresolved_runtime_dependency"], 0)

    def test_runtime_resolution_rejects_unbound_or_wrong_quantity_returns(self):
        variants = {
            "other_batch": {"material_instance_id": "dry_02"},
            "temperature": {"unit": "C"},
            "no_unit": {"unit": ""},
            "optional_return": {"required_for_next_step": False},
            "not_a_declared_return": {"availability": "undeclared"},
            "setpoint_instead_of_reading": {"name": "设定质量"},
            "unknown_operation": {"source": {"station_code": "BALANCE", "operation": "guess"}},
        }
        for label, update in variants.items():
            with self.subTest(label=label):
                plan = runtime_plan()
                plan[1]["intermediate_returns"][0].update(update)
                result = self.agent._scientific_completeness_audit(
                    plan, [], state=runtime_state(),
                )
                self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)

    def test_runtime_resolution_requires_available_source_contract(self):
        for state in (None, ResearchAgentState(event=ResearchEvent("bootstrap", "test", {}))):
            result = self.agent._scientific_completeness_audit(runtime_plan(), [], state=state)
            self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)
        state = runtime_state()
        state.event.constraints["device_context"]["workstations"][0]["availability"] = "offline"
        result = self.agent._scientific_completeness_audit(runtime_plan(), [], state=state)
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)
        state.event.constraints["device_context"]["workstations"][0].pop("availability")
        result = self.agent._scientific_completeness_audit(runtime_plan(), [], state=state)
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)
        for availability in ("unknown", "not_declared", "made-up-status"):
            with self.subTest(availability=availability):
                state.event.constraints["device_context"]["workstations"][0]["availability"] = availability
                result = self.agent._scientific_completeness_audit(runtime_plan(), [], state=state)
                self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)

    def test_external_pending_quantity_is_checked_at_its_first_input(self):
        plan = runtime_plan()
        plan[0]["material_inputs"] = plan[0].pop("material_outputs")
        result = self.agent._scientific_completeness_audit([plan[0]], [], state=runtime_state())
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)

    def test_distinct_quantity_dependencies_of_same_instance_are_not_deduplicated(self):
        plan = runtime_plan()
        plan[2]["quantity_requirements"] = [{
            "kind": "runtime_measured_inventory", "material_id": "product", "unit": "mL",
        }]
        result = self.agent._scientific_completeness_audit(plan, [], state=runtime_state())
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)

    def test_each_quantity_uses_its_own_first_numeric_consumer(self):
        plan = runtime_plan()
        plan[2]["material_inputs"][0]["quantity"] = {"value": 2, "unit": "mg"}
        volume_measure = copy.deepcopy(plan[1])
        volume_measure["intermediate_returns"][0].update(name="样品体积", unit="mL")
        volume_consume = copy.deepcopy(plan[2])
        volume_consume["material_inputs"][0]["quantity"] = {
            "mode": "runtime_measured", "semantic": "runtime_measurement_required", "unit": "mL",
        }
        volume_consume["quantity_requirements"] = [{
            "kind": "runtime_measured_inventory", "material_id": "product", "unit": "mL",
        }]
        state = runtime_state()
        declaration = state.event.constraints["device_context"]["workstations"][0]["operations"][0]["feedback_contract"]["returned_data"]
        declaration["fields"].append("样品体积")
        declaration["field_units"]["样品体积"] = "mL"
        result = self.agent._scientific_completeness_audit(
            [*plan, volume_measure, volume_consume], [], state=state,
        )
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 0)
        result = self.agent._scientific_completeness_audit(
            [*plan, volume_consume, volume_measure], [], state=state,
        )
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)

    def test_measurement_of_one_instance_does_not_resolve_another(self):
        plan = runtime_plan()
        second = copy.deepcopy(plan[0]["material_outputs"][0])
        second.update(material_id="other_product", material_instance_id="dry_02")
        plan[0]["material_outputs"].append(second)
        plan[2]["material_inputs"].append(copy.deepcopy(second))
        result = self.agent._scientific_completeness_audit(plan, [], state=runtime_state())
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)

    def test_copied_scalar_feedback_cannot_measure_two_instances(self):
        plan = runtime_plan()
        second = copy.deepcopy(plan[0]["material_outputs"][0])
        second.update(material_id="other_product", material_instance_id="dry_02")
        plan[0]["material_outputs"].append(second)
        second_input = copy.deepcopy(second)
        second_input["quantity"] = {"mode": "all_available", "semantic": "whole_batch_unspecified"}
        plan[1]["material_inputs"].append(second_input)
        second_return = copy.deepcopy(plan[1]["intermediate_returns"][0])
        second_return["material_instance_id"] = "dry_02"
        plan[1]["intermediate_returns"].append(second_return)
        plan[2]["material_inputs"].append(copy.deepcopy(second))
        result = self.agent._scientific_completeness_audit(plan, [], state=runtime_state())
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 2)

        # Independent operations may return the same declared scalar field.
        first_measure = copy.deepcopy(plan[1])
        first_measure["material_inputs"] = first_measure["material_inputs"][:1]
        first_measure["intermediate_returns"] = first_measure["intermediate_returns"][:1]
        second_measure = copy.deepcopy(plan[1])
        second_measure["material_inputs"] = second_measure["material_inputs"][1:]
        second_measure["intermediate_returns"] = second_measure["intermediate_returns"][1:]
        result = self.agent._scientific_completeness_audit(
            [plan[0], first_measure, second_measure, plan[2]], [], state=runtime_state(),
        )
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 0)

        # Distinct declared fields in one operation also represent independent readings.
        plan[1]["intermediate_returns"][1]["name"] = "第二样品质量"
        state = runtime_state()
        declaration = state.event.constraints["device_context"]["workstations"][0]["operations"][0]["feedback_contract"]["returned_data"]
        declaration["fields"].append("第二样品质量")
        declaration["field_units"]["第二样品质量"] = "mg"
        result = self.agent._scientific_completeness_audit(plan, [], state=state)
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 0)

    def test_runtime_resolution_unit_must_be_declared_by_source_not_proposal(self):
        for field_units in ({}, {"样品质量": "C"}):
            state = runtime_state()
            declaration = state.event.constraints["device_context"]["workstations"][0]["operations"][0]["feedback_contract"]["returned_data"]
            declaration["field_units"] = field_units
            result = self.agent._scientific_completeness_audit(runtime_plan(), [], state=state)
            self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)

    def test_runtime_resolution_must_follow_production_and_precede_use(self):
        for ordering in ((1, 0, 2), (0, 2, 1)):
            plan = runtime_plan()
            result = self.agent._scientific_completeness_audit(
                [plan[i] for i in ordering], [], state=runtime_state(),
            )
            self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)

    def test_unrelated_measurement_text_and_pending_requirement_never_resolve(self):
        plan = runtime_plan()
        plan[1] = base_step(2, "measure unrelated reagent B temperature")
        plan[1]["operation_segments"] = [{"role": "weigh"}]
        plan[1]["quantity_requirements"] = [{"kind": "runtime_measured_inventory", "material_id": "B"}]
        result = self.agent._scientific_completeness_audit(plan, [], state=runtime_state())
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)

    def test_manual_runtime_resolution_requires_bound_wait(self):
        plan = runtime_plan()
        feedback = plan[1]["intermediate_returns"][0]
        feedback.update(availability="undeclared", delivery_mode="manual_handoff", wait_for="mass observation for dry_01")
        result = self.agent._scientific_completeness_audit(plan, [])
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 0)
        feedback.pop("wait_for")
        result = self.agent._scientific_completeness_audit(plan, [])
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)

    def test_runtime_inventory_requirement_is_a_dependency_not_a_resolution(self):
        plan = runtime_plan()
        plan[0]["material_outputs"][0].pop("quantity")
        plan[2]["quantity_requirements"] = [{
            "kind": "runtime_measured_inventory", "material_id": "product", "unit": "mg",
        }]
        result = self.agent._scientific_completeness_audit(plan, [], state=runtime_state())
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 0)
        plan[1]["intermediate_returns"] = []
        result = self.agent._scientific_completeness_audit(plan, [], state=runtime_state())
        self.assertEqual(result["counts"]["unresolved_runtime_dependency"], 1)

    def test_fidelity_risk_requires_equivalence_evidence(self):
        risky = base_step(1, "共沉淀")
        risky["参数"] = "边滴加边搅拌;分10批加入NaOH,每批加料后搅拌"
        audit = self.agent._scientific_completeness_audit([risky], [])
        self.assertEqual(
            audit["counts"]["scientific_fidelity_risk_requiring_review"], 1
        )
        covered = copy.deepcopy(risky)
        covered["fidelity_equivalence_evidence"] = "operando Raman 证明循环加料与连续加料产物一致"
        audit2 = self.agent._scientific_completeness_audit([covered], [])
        self.assertEqual(
            audit2["counts"]["scientific_fidelity_risk_requiring_review"], 0
        )
        self.assertEqual(len(audit2["scientific_fidelity_risks"]), 1)
        self.assertTrue(audit2["scientific_fidelity_risks"][0]["equivalence_evidence"])

    def test_publish_gate_blocks_each_category_with_counts(self):
        state = ResearchAgentState(
            event=ResearchEvent(
                "bootstrap", "test query", {"device_context": {}}
            ),
            contract_version="v2",
            stage_route=["S"],
            current_stage="S",
            branch_history=["B1"],
        )
        # fidelity risk case exercises the full publish path raise
        state.macro_plan = [base_step(1, "共沉淀")]
        state.macro_plan[0]["参数"] = "while stirring 分5批滴加"
        with patch.object(
            self.agent, "_macro_plan_quality_issues", return_value=[]
        ), patch.object(
            self.agent, "_v2_material_graph_issues", return_value=[]
        ), patch.object(
            self.agent, "_v2_bare_agent_inferred_issues", return_value=[]
        ):
            with self.assertRaises(ValueError) as ctx:
                self.agent._publish_v2_contract(state)
        message = str(ctx.exception)
        self.assertIn("scientific completeness gate failed", message)
        self.assertIn("scientific_fidelity_risk_requiring_review=1", message)
        self.assertIn("required_unsupported=0", message)
        self.assertIn("unresolved_runtime_dependency=0", message)
        self.assertIn("broken_material_lineage=0", message)

    def test_audit_persists_into_package(self):
        completeness = ScientificCompletenessV2(
            evidence_class_counts={"paper_explicit": 2},
            unsupported=[],
            unresolved_runtime_dependencies=[],
            scientific_fidelity_risks=[],
            broken_material_lineage=0,
            counts={},
        )
        self.assertEqual(completeness.counts["required_unsupported"], 0)
        self.assertEqual(
            completeness.counts["scientific_fidelity_risk_requiring_review"], 0
        )


class EvidenceLadderTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, "connect", side_effect=AssertionError("offline")).start()
        patch.object(socket.socket, "connect_ex", side_effect=AssertionError("offline")).start()
        patch.object(socket, "create_connection", side_effect=AssertionError("offline")).start()
        self.agent = make_agent()
        self.addCleanup(self.agent._tmpdir.cleanup)
        self.state = ResearchAgentState(
            event=ResearchEvent("bootstrap", "q", {"device_context": {}}),
            contract_version="v2",
        )

    def test_known_gaps_carry_ladder_position(self):
        self.state.extracted_protocols = [
            {
                "source_title": "Web hit",
                "verification_status": "web_unverified",
                "full_text_status": "metadata_only",
                "missing_parameters": ["干燥温度"],
            },
            {
                "source_title": "Parsed paper",
                "verification_status": "verified_doi",
                "full_text_status": "parsed",
                "missing_parameters": ["超声功率"],
            },
        ]
        packet = self.agent._evidence_packet(self.state)
        structured = packet["known_gaps_structured"]
        self.assertEqual(len(structured), 4)
        for item in structured:
            self.assertIn("ladder_position", item)
            self.assertIn(item["ladder_position"], packet["ladder"])
        self.assertEqual(packet["ladder"][0], "main_text")
        self.assertEqual(packet["ladder"][-1], "unsupported")
        # legacy string form retained
        self.assertEqual(len(packet["known_gaps"]), 4)

    def test_extraction_locator_schema_is_reserved_null(self):
        from reaserch_agent.prompts.task_prompts import PAPER_PROTOCOL_EXTRACT_PROMPT

        self.assertIn('"locator": null', PAPER_PROTOCOL_EXTRACT_PROMPT)
        self.assertIn('"excerpt_hash": null', PAPER_PROTOCOL_EXTRACT_PROMPT)
        self.assertIn("source_document", PAPER_PROTOCOL_EXTRACT_PROMPT)


# Frozen A01 six-tuple pin.  Source assumption (recorded per task spec):
# fixture copied from campaigns/A01-device-k3-fresh-20260920-140057 frozen
# device_state.json; the frozen package predates evidence_class discipline, so
# its step provenance kind=agent_inferred maps to the audit verdict
# "unsupported" under the current ladder -- pinned deliberately: any silent
# re-binding (value moved, material re-associated, provenance reclassified)
# flips this test red and must be a deliberate fixture update.
A01_PARAMETERS = [
    {
        "name": "Ni(NO3)2 stock per repeat (Ni-0 route)",
        "pattern": r"5\.00 mL Ni\(NO3\)2",
        "unit": "mL",
        "steps": {2},
        "material": "Ni(NO3)2",
    },
    {
        "name": "Ni(NO3)2 stock per repeat (NiFe-CP route)",
        "pattern": r"4\.00 mL Ni\(NO3\)2",
        "unit": "mL",
        "steps": {2, 4},
        "material": "Ni(NO3)2",
    },
    {
        "name": "NaOH batch dosing",
        "pattern": r"分\d+批加入\d+\.\d+ mL NaOH|分\d+批加入0\.\d+ mL NaOH",
        "unit": "mL",
        "steps": {2, 3, 4, 5},
        "material": "NaOH",
    },
    {
        "name": "stirring speed",
        "pattern": r"600 rpm",
        "unit": "rpm",
        "steps": {2, 3, 4, 5, 6},
        "material": None,
    },
    {
        "name": "aging temperature",
        "pattern": r"60 ℃",
        "unit": "℃",
        "steps": {2, 3, 4, 5, 6},
        "material": None,
    },
    {
        "name": "aging time",
        "pattern": r"360 min",
        "unit": "min",
        "steps": {2, 3, 4},
        "material": None,
    },
    {
        "name": "centrifugation",
        "pattern": r"8000 rpm离心10 min|8000 rpm 离心10 min",
        "unit": "rpm",
        "steps": {2, 3, 4, 5},
        "material": None,
    },
    {
        "name": "water washing",
        "pattern": r"8\.0 mL去离子水洗涤3次|去离子水洗涤3次",
        "unit": "mL",
        "steps": {2, 3, 4, 5},
        "material": "去离子水",
    },
    {
        "name": "ethanol washing",
        "pattern": r"8\.0 mL乙醇洗涤2次|乙醇洗涤2次",
        "unit": "mL",
        "steps": {2, 3, 4, 5},
        "material": "乙醇",
    },
    {
        "name": "drying",
        "pattern": r"60 ℃干燥12 h|60 ℃真空干燥",
        "unit": "h",
        "steps": {2, 3, 4, 5, 6},
        "material": None,
    },
]


class A01SixTupleRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.package = fixture["research_action_package_v2"]
        cls.model = ResearchActionPackageV2.model_validate(cls.package)

    @staticmethod
    def _step_text(step) -> str:
        get_value = (
            (lambda p: str(p.get("value", "")))
            if isinstance(step, dict)
            else (lambda p: str(getattr(p, "value", "")))
        )
        parameters = (
            step.get("parameters", [])
            if isinstance(step, dict)
            else step.parameters
        )
        return " ".join(get_value(p) for p in parameters)

    @staticmethod
    def _prov_value(provenance, key):
        if isinstance(provenance, dict):
            return provenance.get(key)
        return getattr(provenance, key, None)

    def _evidence_class(self, step) -> str:
        provenance = (
            step.get("provenance") if isinstance(step, dict) else step.provenance
        ) or {}
        kind = str(self._prov_value(provenance, "kind") or "")
        if kind == "agent_inferred" and not (
            self._prov_value(provenance, "inference_rule")
            or self._prov_value(provenance, "derivation")
        ):
            return "unsupported"
        mapping = {
            "paper": "paper_explicit",
            "device_skill": "device_sop",
            "runtime": "runtime_measurement",
        }
        return mapping.get(kind, "unsupported")

    def test_frozen_six_tuples_are_stable(self):
        steps = {s.sequence: s for s in self.model.macro_steps}
        bundle_refs = {
            str(item.evidence_id or "") for item in self.model.evidence_bundle.items
        }
        for spec in A01_PARAMETERS:
            hits = {
                seq
                for seq, step in steps.items()
                if re.search(spec["pattern"], self._step_text(step))
            }
            # macro-step association: value present in exactly the frozen steps
            self.assertEqual(
                hits,
                spec["steps"],
                f"{spec['name']}: bound to steps {sorted(hits)}, "
                f"expected {sorted(spec['steps'])}",
            )
            for seq in spec["steps"]:
                step = steps[seq]
                text = self._step_text(step)
                match = re.search(spec["pattern"], text)
                # value + unit co-located in the same parameter text
                self.assertIsNotNone(match, f"{spec['name']} missing in step {seq}")
                self.assertIn(spec["unit"], text)
                # evidence_class pinned from the frozen provenance
                self.assertEqual(
                    self._evidence_class(step),
                    "unsupported",
                    f"{spec['name']} step {seq} evidence_class drifted",
                )
                # source reference pinned (frozen package used agent补全)
                reference = str(self._prov_value(step.provenance, "reference") or "")
                self.assertEqual(reference, "agent补全")
                self.assertFalse(bundle_refs)  # no evidence items: pinned gap
                # material association
                if spec["material"]:
                    ports = (
                        list(step.material_inputs)
                        + list(step.material_outputs)
                        + list(step.material_intermediates)
                    )
                    blob = json.dumps(
                        [port.model_dump(mode="json") for port in ports],
                        ensure_ascii=False,
                    )
                    # The frozen A01 package has no structured material ports;
                    # materials bind at operation/parameter text level there.
                    # (Structured-port binding is pinned by the synthetic
                    # minimal package test below.)
                    association_text = (
                        blob
                        + str(getattr(step, "operation", "") or "")
                        + self._step_text(step)
                    )
                    self.assertIn(
                        spec["material"],
                        association_text,
                        f"{spec['name']}: {spec['material']} not associated in step {seq}",
                    )

    def test_rebinding_to_wrong_step_is_detected(self):
        """Negative case: move a pinned parameter to a wrong step; the
        macro-step association assertion must catch it."""
        mutated = copy.deepcopy(self.package)
        mutated.pop("research_contract_hash", None)
        for step in mutated["macro_steps"]:
            if step["sequence"] == 1:
                step["parameters"].append(
                    {
                        "name": "research_parameter_text",
                        "value": "600 rpm 搅拌",
                        "provenance": {
                            "kind": "agent_inferred",
                            "rationale": "negative-case rebinding probe",
                        },
                    }
                )
        model = ResearchActionPackageV2.model_validate(mutated)
        steps = {s.sequence: s for s in model.macro_steps}
        hits = {
            seq
            for seq, step in steps.items()
            if re.search(r"600 rpm", self._step_text(step))
        }
        self.assertIn(1, hits)
        self.assertNotEqual(hits, next(s["steps"] for s in A01_PARAMETERS if s["name"] == "stirring speed"))

    def test_desired_tuple_shape_on_minimal_synthetic_package(self):
        """The corrected shape the pipeline should produce: one paper-bound
        six-tuple (NaOH dosing) with explicit evidence_class and source."""
        step = self.model.macro_steps[1]
        synthetic_provenance = {
            "kind": "paper",
            "reference": "jacs.8b05294",
            "source_path": "evidence_bundle.items[0].excerpt",
            "excerpt": "在 25 ℃、600 rpm 搅拌下于 10 min 内分 10 批加入 2.50 mL NaOH 原液",
            "source_digest": "sha256_" + "a" * 64,
            "evidence_class": "paper_explicit",
        }
        six_tuple = {
            "name": "NaOH batch dosing",
            "value": 2.50,
            "unit": "mL",
            "evidence_class": synthetic_provenance["evidence_class"],
            "source_reference": synthetic_provenance["reference"],
            "material_association": "NaOH",
            "macro_step": step.macro_step_id,
        }
        self.assertEqual(six_tuple["evidence_class"], "paper_explicit")
        self.assertEqual(six_tuple["source_reference"], "jacs.8b05294")
        self.assertIn("NaOH", six_tuple["material_association"])
        validated = ProvenanceRoundtrip(synthetic_provenance)
        self.assertEqual(validated["evidence_class"], "paper_explicit")


def ProvenanceRoundtrip(provenance: dict) -> dict:
    from chem_agent_contracts.v2 import ProvenanceV2

    return ProvenanceV2(**provenance).model_dump(mode="json", exclude_none=True)


if __name__ == "__main__":
    unittest.main()

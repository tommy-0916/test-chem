"""Selected-route typed package ↔ persisted state structural equivalence."""

from __future__ import annotations

import copy
import json
import unittest

from chem_agent_contracts.adapters import research_state_to_v2
from chem_agent_contracts.route_action_intent import (
    RouteActionIntentV1,
    build_route_action_binding_draft_v1,
)
from chem_agent_contracts.route_saved_state import (
    RouteSavedStateMismatch,
    build_selected_route_saved_state_v2,
    validate_selected_route_saved_state_v2,
)
from chem_agent_contracts.test_route_action_intent import _candidate, _paper_provenance
from chem_agent_contracts.test_route_research_package_draft import _reviewed_fixture
from chem_agent_contracts.v2 import (
    LogicalContainerV2,
    MaterialContractMigrationV2,
    MaterialContractStatusV2,
    MaterialOperationSegmentV2,
    MaterialPortV2,
    MaterialRelationV2,
    QuantityV2,
    ResearchActionPackageV2,
    StateTransitionV2,
    canonical_raw_observations_digest,
    canonical_raw_step_digest,
)


def _rich_selected_route(*, explicit_material_metadata: bool = False):
    candidate = _candidate()
    provenance = _paper_provenance(0)
    first = candidate.material_graph[0]
    quantity = QuantityV2(
        mode="exact", semantic="planned_target", value=2, unit="mmol"
    )
    first.material_inputs[0].quantity = quantity
    first.material_inputs[0].material_origin = "external_inventory"
    first.material_inputs[0].logical_container_id = "LC1"
    first.material_intermediates = [MaterialPortV2(
        material_id="nickel_salt", material_instance_id="mid-A",
        name="nickel salt", state="solution", quantity=quantity,
        material_origin="same_step_relation", logical_container_id="LC1",
        provenance=provenance,
    )]
    first.material_relations = [MaterialRelationV2(
        relation_id="REL1", event_kind="state_change",
        input_material_instance_ids=["in-A"],
        output_material_instance_ids=["mid-A"],
        logical_container_ids=["LC1"], quantity_basis="whole_batch",
        source_operation_ref="SEG1", provenance=provenance,
    )]
    first.operation_segments = [MaterialOperationSegmentV2(
        segment_id="SEG1", material_effect="transform_material",
        source_operation_ref="mix", provenance=provenance,
    )]
    first.logical_containers = [LogicalContainerV2(
        logical_container_id="LC1", container_type="vial", count=1,
        capacity_ml=20, lid_state="open",
    )]
    first.state_transition = StateTransitionV2(
        before_state="solution", after_state="solution", confidence="explicit"
    )
    first.quantity_requirements = [{
        "kind": "scientific_input_setpoint", "material_id": "nickel_salt",
        "material": "nickel salt", "source": "literature",
        "value": 2, "unit": "mmol",
        "provenance": provenance.model_dump(mode="json"),
    }]
    if explicit_material_metadata:
        first.material_contract_status = MaterialContractStatusV2(
            material_inputs="unresolved",
            material_intermediates="unresolved",
            material_outputs="unresolved",
            logical_containers="unresolved",
            material_relations="unresolved",
        )
        first.material_contract_migration = MaterialContractMigrationV2(
            source_contract_version="v2", migration_mode="native_v2",
            source_path="selected_route.material_graph[0]",
            diagnostics=["supplied by trusted route compiler"],
        )
    return _reviewed_fixture(candidate)


class RouteSavedStateTest(unittest.TestCase):
    def test_json_save_reload_reconstructs_typed_route_facts_and_transport(self) -> None:
        draft, decision, bundle = _rich_selected_route()
        observations = [{"observation_id": "O1", "result": "awaiting measurement"}]
        saved = build_selected_route_saved_state_v2(
            draft,
            decision=decision,
            current_evidence_bundle=bundle,
            campaign_id="campaign-generic",
            observations=observations,
        )
        restored = json.loads(json.dumps(saved, ensure_ascii=False))
        canonical = validate_selected_route_saved_state_v2(restored)
        rebuilt = research_state_to_v2(restored)
        embedded = ResearchActionPackageV2.model_validate(
            restored["research_action_package_v2"], strict=True
        )

        self.assertEqual(canonical.model_dump(mode="json"), rebuilt.model_dump(mode="json"))
        self.assertEqual(canonical.model_dump(mode="json"), embedded.model_dump(mode="json"))
        self.assertEqual(canonical.macro_steps[0].parameters, draft.macro_steps[0].parameters)
        self.assertEqual(canonical.macro_steps[0].material_inputs,
                         draft.macro_steps[0].material_inputs)
        self.assertEqual(canonical.macro_steps[0].material_intermediates,
                         draft.macro_steps[0].material_intermediates)
        self.assertEqual(canonical.macro_steps[0].material_relations,
                         draft.macro_steps[0].material_relations)
        self.assertEqual(canonical.macro_steps[0].state_transition,
                         draft.macro_steps[0].state_transition)
        self.assertEqual(canonical.macro_steps[0].material_contract_status, None)
        self.assertEqual(canonical.macro_steps[0].material_contract_migration, None)
        self.assertEqual(canonical.macro_steps[0].material_inputs[0].quantity.value, 2)
        self.assertEqual(canonical.macro_steps[0].material_inputs[0].quantity.unit, "mmol")
        self.assertEqual(
            canonical.macro_steps[0].material_inputs[0].provenance.evidence_class,
            "paper_explicit",
        )
        self.assertEqual(
            canonical.macro_steps[0].material_inputs[0].provenance.source_path,
            "evidence_bundle.items[0].excerpt",
        )
        self.assertEqual(canonical.stage.capability_requirements, ["mixing"])
        self.assertEqual(canonical.route_binding.route_id, draft.route_id)
        self.assertEqual(canonical.route_binding.experimental_group_id, "group-A")
        self.assertEqual(canonical.route_binding.source_locator, "Methods, page 3")
        self.assertEqual(canonical.route_binding.required_capabilities, ["mixing"])
        self.assertEqual(
            canonical.raw_observations_sha256,
            canonical_raw_observations_digest(observations),
        )
        for raw, step in zip(restored["macro_plan"], canonical.macro_steps):
            self.assertEqual(step.raw_step_sha256, canonical_raw_step_digest(raw))
        self.assertEqual(restored["macro_plan"][0]["操作"], "mix")
        self.assertEqual(restored["macro_plan"][0]["试剂/对象"],
                         "nickel salt and base")
        self.assertIn("2 mmol", restored["macro_plan"][0]["参数"])
        self.assertEqual(restored["persistent_outputs"],
                         restored["device_adaptation_handoff"])
        self.assertEqual(restored["route_binding_status_v1"], "selected_unbound")
        self.assertEqual(
            restored["device_adaptation_handoff"]["route_binding_status_v1"],
            "selected_unbound",
        )

        observations[0]["result"] = "changed later"
        draft.macro_steps[0].parameters[0].value = 999
        self.assertEqual(restored["observations"][0]["result"], "awaiting measurement")
        self.assertEqual(canonical.macro_steps[0].parameters[0].value, 2)

    def test_rejects_raw_changes_after_save_even_when_mirrors_agree(self) -> None:
        draft, decision, bundle = _rich_selected_route()
        saved = build_selected_route_saved_state_v2(
            draft, decision=decision, current_evidence_bundle=bundle,
            campaign_id="campaign-generic", observations=[],
        )
        for path, replacement in (
            (("material_inputs", 0, "quantity", "value"), 3),
            (("material_relations", 0, "provenance", "source_path"), "elsewhere"),
            (("state_transition", "confidence"), "unknown"),
        ):
            with self.subTest(path=path):
                changed = copy.deepcopy(saved)
                target = changed["macro_plan"][0]
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = replacement
                for mirror_name in ("device_adaptation_handoff", "persistent_outputs"):
                    changed[mirror_name]["待执行 macro plan"] = copy.deepcopy(
                        changed["macro_plan"]
                    )
                with self.assertRaises(ValueError):
                    validate_selected_route_saved_state_v2(changed)

        changed = copy.deepcopy(saved)
        changed["route_binding"]["route_id"] = "other-route"
        with self.assertRaisesRegex(RouteSavedStateMismatch, "route_binding differs"):
            validate_selected_route_saved_state_v2(changed)

    def test_keeps_independent_stage_and_action_completion_conditions(self) -> None:
        draft, decision, bundle = _rich_selected_route()
        stage = draft.stage.model_copy(deep=True)
        stage.completion_condition = "stage characterization complete"
        intent = RouteActionIntentV1(
            route_id=draft.route_id,
            candidate_digest=draft.candidate_digest,
            decision_id=draft.decision_id,
            evidence_bundle_id=draft.evidence_bundle_id,
            evidence_bundle_digest=draft.evidence_bundle_digest,
            stage=stage,
            macro_action=draft.macro_action,
        )
        rebound = build_route_action_binding_draft_v1(
            intent, decision=decision, current_evidence_bundle=bundle
        )
        saved = build_selected_route_saved_state_v2(
            rebound, decision=decision, current_evidence_bundle=bundle,
            campaign_id="campaign-generic", observations=[],
        )
        canonical = validate_selected_route_saved_state_v2(saved)
        self.assertEqual(canonical.stage.completion_condition,
                         "stage characterization complete")
        self.assertEqual(canonical.macro_action.completion_condition,
                         "solid isolated")
        self.assertEqual(saved["macro_action"]["stage_completion_condition"],
                         "stage characterization complete")

    def test_explicit_material_contract_metadata_is_not_regenerated(self) -> None:
        draft, decision, bundle = _rich_selected_route(explicit_material_metadata=True)
        saved = build_selected_route_saved_state_v2(
            draft, decision=decision, current_evidence_bundle=bundle,
            campaign_id="campaign-generic", observations=[],
        )
        reloaded = validate_selected_route_saved_state_v2(
            json.loads(json.dumps(saved, ensure_ascii=False))
        )
        self.assertEqual(
            reloaded.macro_steps[0].material_contract_status,
            draft.macro_steps[0].material_contract_status,
        )
        self.assertEqual(
            reloaded.macro_steps[0].material_contract_migration,
            draft.macro_steps[0].material_contract_migration,
        )

    def test_requires_explicit_json_observations_and_current_typed_decision(self) -> None:
        draft, decision, bundle = _rich_selected_route()
        args = {
            "decision": decision,
            "current_evidence_bundle": bundle,
            "campaign_id": "campaign-generic",
        }
        with self.assertRaisesRegex(TypeError, "observations"):
            build_selected_route_saved_state_v2(draft, observations=None, **args)
        with self.assertRaisesRegex(ValueError, "observations must be finite JSON"):
            build_selected_route_saved_state_v2(
                draft, observations=[{"bad": float("nan")}], **args
            )
        with self.assertRaisesRegex(TypeError, "current typed RouteDecisionV1"):
            build_selected_route_saved_state_v2(
                draft, observations=[], decision=decision.model_dump(mode="json"),
                current_evidence_bundle=bundle, campaign_id="campaign-generic",
            )


if __name__ == "__main__":
    unittest.main()

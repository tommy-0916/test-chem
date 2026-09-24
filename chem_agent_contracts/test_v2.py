from __future__ import annotations

import unittest

from chem_agent_contracts.adapters import (
    attach_device_v2_contract,
    build_observation_event_v2,
    device_result_to_v2,
    research_state_to_v2,
)
from chem_agent_contracts.identity import (
    IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
    IDENTITY_WIRE_PREFIX,
    decode_json_scalar_identity,
    encode_json_scalar_identity,
)
from chem_agent_contracts.v2 import (
    DeviceWorkflowPackageV2,
    ObservationEventV2,
    ResearchActionPackageV2,
    canonical_digest,
)


def research_fixture():
    return research_state_to_v2(
        {
            "campaign_id": "CMP_V2",
            "current_stage": "制备并观察晶相",
            "macro_action": {
                "macro_action_id": "MA_001",
                "objective": "制备单一样品",
                "planned_operations": ["加液", "搅拌"],
                "expected_observation": "XRD",
                "completion_condition": "获得有效 XRD",
                "experiment_group": {
                    "group_id": "G_001",
                    "sample_id": "S_001",
                    "role": "experimental",
                },
            },
            "macro_plan": [
                {
                    "步骤序号": 1,
                    "macro_step_id": "MS_001",
                    "操作": "加液",
                    "试剂/对象": "水",
                    "参数": "加入 10 mL 水",
                    "provenance": {
                        "kind": "agent_inferred",
                        "rationale": "按 10 mL 反应规模设置",
                    },
                    "material_inputs": [
                        {
                            "name": "水",
                            "state": "liquid",
                            "quantity": {"value": 10, "unit": "mL"},
                            "provenance": {
                                "kind": "agent_inferred",
                                "rationale": "按 10 mL 反应规模设置",
                            },
                        }
                    ],
                    "material_outputs": [
                        {
                            "name": "混合液",
                            "state": "solution",
                            "quantity": {"mode": "all_available"},
                        }
                    ],
                },
                {
                    "步骤序号": 2,
                    "macro_step_id": "MS_002",
                    "操作": "搅拌",
                    "试剂/对象": "混合液",
                    "参数": "500 rpm 搅拌 10 min",
                    "provenance": {
                        "kind": "agent_inferred",
                        "rationale": "保证小体积混合均匀",
                    },
                },
            ],
            "current_evidence_bundle": {
                "bundle_id": "EV_CURRENT",
                "query": "test",
                "retrieval_status": "empty",
                "results": [],
            },
        }
    )


def research_with_ids(identifiers):
    return research_state_to_v2(
        {
            "campaign_id": "CMP_TYPED",
            "current_stage": "typed identity audit",
            "macro_action": {
                "macro_action_id": f"{IDENTITY_WIRE_PREFIX}action",
                "objective": "preserve typed identities",
                "planned_operations": ["audit"],
                "expected_observation": "typed mappings",
                "completion_condition": "all mappings are exact",
                "experiment_group": {
                    "group_id": "G_TYPED",
                    "sample_id": "S_TYPED",
                    "role": "experimental",
                },
            },
            "macro_plan": [
                {
                    "macro_step_id": identity,
                    "步骤序号": sequence,
                    "操作": f"operation {sequence}",
                    "试剂/对象": "sample",
                    "参数": "室温 1 min",
                    "provenance": {
                        "kind": "agent_inferred",
                        "rationale": "identity contract regression",
                    },
                }
                for sequence, identity in enumerate(identifiers, start=1)
            ],
        }
    )


class V2ContractTest(unittest.TestCase):
    def test_research_contract_is_hashed_and_one_group(self):
        research = research_fixture()
        self.assertTrue(research.research_contract_hash.startswith("research_v2_"))
        self.assertEqual({step.sample_id for step in research.macro_steps}, {"S_001"})
        self.assertEqual(research.macro_steps[0].material_inputs[0].quantity.value, 10)

    def test_legacy_unmarked_research_hash_remains_valid(self):
        payload = research_fixture().model_dump(mode="json")
        payload.pop("research_contract_hash", None)
        payload.pop("identity_encoding", None)
        legacy_hash = canonical_digest(payload, prefix="research_v2")
        payload["research_contract_hash"] = legacy_hash

        restored = ResearchActionPackageV2.model_validate(payload)

        self.assertIsNone(restored.identity_encoding)
        self.assertEqual(restored.research_contract_hash, legacy_hash)

    def test_conflicting_typed_research_identity_mirrors_fail_closed(self):
        state = {
            "macro_action": {
                "macro_action_id": 1,
                "objective": "audit",
                "planned_operations": ["audit"],
                "expected_observation": "audit",
                "completion_condition": "audit",
                "experiment_group": {"group_id": "G", "sample_id": "S"},
            },
            "macro_plan": [
                {
                    "macro_action_id": "1",
                    "macro_step_id": "A",
                    "logical_step_id": "B",
                    "操作": "audit",
                    "试剂/对象": "sample",
                    "参数": "室温 1 min",
                    "provenance": {
                        "kind": "agent_inferred",
                        "rationale": "audit",
                    },
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "conflicting typed macro_action_id"):
            research_state_to_v2(state)

        state["macro_plan"][0]["macro_action_id"] = 1
        with self.assertRaisesRegex(ValueError, "macro_step_id/logical_step_id"):
            research_state_to_v2(state)

    def test_research_macro_plan_rejects_non_object_entries_without_filtering(self):
        state = {
            "macro_action": {
                "macro_action_id": "MA",
                "objective": "audit",
                "planned_operations": ["audit"],
                "expected_observation": "audit",
                "completion_condition": "audit",
                "experiment_group": {"group_id": "G", "sample_id": "S"},
            },
            "macro_plan": [
                {
                    "macro_step_id": "A",
                    "操作": "audit",
                    "试剂/对象": "sample",
                    "参数": "室温 1 min",
                    "provenance": {
                        "kind": "agent_inferred",
                        "rationale": "audit",
                    },
                },
                "MALFORMED",
            ],
        }
        with self.assertRaisesRegex(ValueError, r"macro_plan\[1\].*cannot be filtered"):
            research_state_to_v2(state)

    def test_mapping_preserves_macro_ids_and_exact_binding(self):
        research = research_fixture()
        result = {
            "status": "success",
            "device_snapshot_id": "SNAP_1",
            "workflow_json": {
                "steps": [
                    {
                        "device_step_id": "DS_1",
                        "source_macro_step_id": "MS_001",
                        "station_code": "Liquid_Handling_Station_1ml_V2",
                        "station_version": "V2",
                        "platform_name": "移液平台1ml_V2",
                        "workstation": "移液平台1ml_V2",
                        "id": 36,
                        "operation": "加液_物料绑定",
                        "parameters": {"体积(mL)": 1.0},
                    }
                ]
            },
            "dispatch_payload": {"experiment_steps": {"steps": []}},
            "dispatch_validation": {"status": "passed", "errors": []},
        }
        package = device_result_to_v2(result, research)
        self.assertEqual(package.status, "ready_for_dispatch")
        step = package.workstation_mapping.device_steps[0]
        self.assertEqual(step.source_macro_step_id, "MS_001")
        self.assertEqual(step.station_version, "V2")
        self.assertEqual(step.station_id, 36)
        self.assertEqual(
            {item.macro_step_id for item in package.workstation_mapping.requirements},
            {"MS_001", "MS_002"},
        )

    def test_typed_macro_ids_are_reversible_and_keep_derived_tasks_distinct(self):
        reserved_literal = f"{IDENTITY_WIRE_PREFIX}i:1"
        identifiers = [0, 1, "1", "001", reserved_literal]
        research = research_with_ids(identifiers)

        self.assertEqual(
            research.identity_encoding,
            IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
        )
        self.assertEqual(
            [decode_json_scalar_identity(step.macro_step_id) for step in research.macro_steps],
            identifiers,
        )
        self.assertEqual(len({step.macro_step_id for step in research.macro_steps}), 5)

        result = {
            "status": "success",
            "workflow_json": {
                "steps": [
                    {
                        "device_step_id": f"DS_{index}",
                        "source_macro_step_id": identity,
                        "station_code": "Audit_Station_V1",
                        "platform_name": "audit",
                        "operation": "audit",
                    }
                    for index, identity in enumerate(identifiers, start=1)
                ]
            },
            "dispatch_validation": {"status": "passed", "errors": []},
        }
        device = device_result_to_v2(result, research)

        self.assertEqual(device.status, "ready_for_dispatch")
        self.assertEqual(device.identity_encoding, research.identity_encoding)
        self.assertEqual(
            [
                decode_json_scalar_identity(step.source_macro_step_id)
                for step in device.workstation_mapping.device_steps
            ],
            identifiers,
        )
        self.assertEqual(len(device.workstation_mapping.requirements), 5)
        self.assertEqual(
            len({item.workstation_task_id for item in device.workstation_mapping.requirements}),
            5,
        )

    def test_marked_wire_sources_decode_but_unmarked_prefix_strings_stay_literal(self):
        reserved_literal = f"{IDENTITY_WIRE_PREFIX}i:1"
        research = research_with_ids([1, reserved_literal])
        numeric_wire, literal_wire = [step.macro_step_id for step in research.macro_steps]

        marked = device_result_to_v2(
            {
                "status": "success",
                "identity_encoding": IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
                "workflow_json": {
                    "steps": [
                        {
                            "device_step_id": "DS_NUMERIC",
                            "source_macro_step_id": numeric_wire,
                            "station_code": "Audit_Station_V1",
                            "platform_name": "audit",
                            "operation": "audit",
                        },
                        {
                            "device_step_id": "DS_LITERAL",
                            "source_macro_step_id": literal_wire,
                            "station_code": "Audit_Station_V1",
                            "platform_name": "audit",
                            "operation": "audit",
                        },
                    ]
                },
                "dispatch_validation": {"status": "passed", "errors": []},
            },
            research,
        )
        self.assertEqual(
            [step.source_macro_step_id for step in marked.workstation_mapping.device_steps],
            [numeric_wire, literal_wire],
        )

        unmarked = device_result_to_v2(
            {
                "status": "success",
                "workflow_json": {
                    "steps": [
                        {
                            "device_step_id": "DS_LITERAL",
                            "source_macro_step_id": reserved_literal,
                            "station_code": "Audit_Station_V1",
                            "platform_name": "audit",
                            "operation": "audit",
                        }
                    ]
                },
                "dispatch_validation": {"status": "passed", "errors": []},
            },
            research,
        )
        self.assertEqual(unmarked.status, "ready_for_dispatch")
        self.assertEqual(
            unmarked.workstation_mapping.device_steps[0].source_macro_step_id,
            literal_wire,
        )

    def test_exact_typed_source_precedes_integer_sequence_and_string_never_is_sequence(self):
        research = research_with_ids(["A", 1, "1"])
        result = {
            "status": "success",
            "workflow_json": {
                "steps": [
                    {
                        "device_step_id": "DS_INT_EXACT",
                        "source_macro_step_id": 1,
                        "station_code": "Audit_Station_V1",
                        "platform_name": "audit",
                        "operation": "audit",
                    },
                    {
                        "device_step_id": "DS_STRING_EXACT",
                        "source_macro_step_id": "1",
                        "station_code": "Audit_Station_V1",
                        "platform_name": "audit",
                        "operation": "audit",
                    },
                ]
            },
            "dispatch_validation": {"status": "passed", "errors": []},
        }
        device = device_result_to_v2(result, research)
        self.assertEqual(
            [
                decode_json_scalar_identity(step.source_macro_step_id)
                for step in device.workstation_mapping.device_steps
            ],
            [1, "1"],
        )

        legacy_sequence = device_result_to_v2(
            {
                "status": "success",
                "workflow_json": {
                    "steps": [
                        {
                            "device_step_id": "DS_SEQUENCE",
                            "source_macro_step_id": 1,
                            "station_code": "Audit_Station_V1",
                            "platform_name": "audit",
                            "operation": "audit",
                        }
                    ]
                },
                "dispatch_validation": {"status": "passed", "errors": []},
            },
            research_with_ids(["A", "B"]),
        )
        self.assertEqual(
            legacy_sequence.workstation_mapping.device_steps[0].source_macro_step_id,
            "A",
        )

    def test_multisource_coverage_uses_typed_primary_and_requires_mirror_agreement(self):
        research = research_with_ids(["A", "B", "C", "D"])
        base_step = {
            "device_step_id": "DS_MULTI",
            "source_macro_steps": [1, 2, 3, 4],
            "station_code": "Audit_Station_V1",
            "platform_name": "audit",
            "operation": "combined audit",
        }
        device = device_result_to_v2(
            {
                "status": "success",
                "workflow_json": {"steps": [base_step]},
                "dispatch_validation": {"status": "passed", "errors": []},
            },
            research,
        )
        self.assertEqual(device.status, "ready_for_dispatch")
        self.assertEqual(
            device.workstation_mapping.device_steps[0].source_macro_step_id,
            "A",
        )
        self.assertEqual(
            device.workstation_mapping.device_steps[0].source_macro_step_ids,
            ["A", "B", "C", "D"],
        )
        self.assertTrue(
            all(
                requirement.candidate_station_codes == ["Audit_Station_V1"]
                for requirement in device.workstation_mapping.requirements
            )
        )
        self.assertEqual(device.workflow["steps"][0]["source_macro_steps"], [1, 2, 3, 4])

        conflicting = dict(base_step)
        conflicting["source_macro_step_id"] = 1
        conflicting["source_macro_step"] = "1"
        rejected = device_result_to_v2(
            {
                "status": "success",
                "workflow_json": {"steps": [conflicting]},
                "dispatch_validation": {"status": "passed", "errors": []},
            },
            research,
        )
        self.assertEqual(rejected.status, "human_review_required")
        self.assertEqual(rejected.workstation_mapping.device_steps, [])

    def test_multisource_tail_is_fully_validated_and_complete_mirrors_must_match(self):
        research = research_with_ids(["A", "B", "C", "D"])
        base = {
            "device_step_id": "DS_MULTI",
            "source_macro_step_id": "A",
            "source_macro_steps": ["A", "B", "C", "D"],
            "station_code": "Audit_Station_V1",
            "platform_name": "audit",
            "operation": "combined audit",
        }

        for mutation in (
            {"source_macro_steps": ["A", "B", "UNKNOWN", "D"]},
            {
                "identity_encoding": IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
                "source_macro_steps": ["A", "B", f"{IDENTITY_WIRE_PREFIX}i:01", "D"],
            },
            {
                "source_macro_step": ["A", "B", "C"],
                "source_macro_steps": ["A", "B", "D"],
            },
        ):
            with self.subTest(mutation=mutation):
                step = dict(base)
                result = {
                    "status": "success",
                    "workflow_json": {"steps": [step]},
                    "dispatch_validation": {"status": "passed", "errors": []},
                }
                if "identity_encoding" in mutation:
                    result["workflow_json"]["identity_encoding"] = mutation.pop(
                        "identity_encoding"
                    )
                step.update(mutation)
                rejected = device_result_to_v2(result, research)
                self.assertEqual(rejected.status, "human_review_required")
                self.assertEqual(rejected.workstation_mapping.device_steps, [])
                self.assertTrue(rejected.validation.issues)

    def test_missing_unknown_and_invalid_sources_require_human_review_without_position_guess(self):
        research = research_with_ids(["A", "B"])
        result = {
            "status": "success",
            "workflow_json": {
                "steps": [
                    {
                        "device_step_id": "DS_MISSING",
                        "station_code": "Audit_Station_V1",
                        "platform_name": "audit",
                        "operation": "audit",
                    },
                    {
                        "device_step_id": "DS_UNKNOWN",
                        "source_macro_step_id": "unknown",
                        "station_code": "Audit_Station_V1",
                        "platform_name": "audit",
                        "operation": "audit",
                    },
                    {
                        "device_step_id": "DS_INVALID",
                        "source_macro_step_id": False,
                        "station_code": "Audit_Station_V1",
                        "platform_name": "audit",
                        "operation": "audit",
                    },
                ]
            },
            "dispatch_validation": {"status": "passed", "errors": []},
        }
        device = device_result_to_v2(result, research)

        self.assertEqual(device.status, "human_review_required")
        self.assertEqual(device.workstation_mapping.device_steps, [])
        source_issues = [
            issue for issue in device.validation.issues
            if issue.validator == "typed_macro_source_binding_v1"
        ]
        self.assertEqual(len(source_issues), 3)
        self.assertEqual(
            {issue.device_step_id for issue in source_issues},
            {"DS_MISSING", "DS_UNKNOWN", "DS_INVALID"},
        )

    def test_explicit_invalid_source_mirror_owns_parsing_and_cannot_fallback(self):
        research = research_with_ids(["A"])
        for invalid in (None, "", False):
            with self.subTest(invalid=invalid):
                package = device_result_to_v2(
                    {
                        "status": "success",
                        "workflow_json": {
                            "steps": [
                                {
                                    "device_step_id": "DS_A",
                                    "source_macro_step_id": invalid,
                                    "source_macro_step": "A",
                                    "station_code": "Audit_Station_V1",
                                    "platform_name": "audit",
                                    "operation": "audit",
                                }
                            ]
                        },
                        "dispatch_validation": {"status": "passed", "errors": []},
                    },
                    research,
                )
                self.assertEqual(package.status, "human_review_required")
                self.assertEqual(package.workstation_mapping.device_steps, [])
                self.assertTrue(
                    any(
                        issue.field_path.endswith("source_macro_step_id")
                        for issue in package.validation.issues
                    )
                )

    def test_contract_step_collections_reject_malformed_entries(self):
        research = research_with_ids(["A"])
        valid_step = {
            "device_step_id": "DS_A",
            "source_macro_step_id": "A",
            "station_code": "Audit_Station_V1",
            "platform_name": "audit",
            "operation": "audit",
        }
        cases = (
            {
                "workflow_json": {"steps": [valid_step, "MALFORMED"]},
                "expected_path": "workflow_json.steps[1]",
            },
            {
                "workflow_json": {"steps": [valid_step]},
                "device_plan": [
                    {"source_macro_step_id": "A", "station_code": "Audit_Station_V1"},
                    "MALFORMED",
                ],
                "expected_path": "device_plan[1]",
            },
        )
        for case in cases:
            with self.subTest(expected_path=case["expected_path"]):
                result = {
                    "status": "success",
                    "workflow_json": case["workflow_json"],
                    "dispatch_validation": {"status": "passed", "errors": []},
                }
                if "device_plan" in case:
                    result["device_plan"] = case["device_plan"]
                package = device_result_to_v2(result, research)
                self.assertEqual(package.status, "human_review_required")
                collection_issues = [
                    issue
                    for issue in package.validation.issues
                    if issue.validator == "contract_object_collection_v1"
                ]
                self.assertEqual(len(collection_issues), 1)
                self.assertEqual(collection_issues[0].field_path, case["expected_path"])

    def test_device_step_ids_are_explicit_nonempty_strings_and_unique(self):
        research = research_with_ids(["A"])
        base = {
            "source_macro_step_id": "A",
            "station_code": "Audit_Station_V1",
            "platform_name": "audit",
            "operation": "audit",
        }
        for invalid_step in (
            {**base, "device_step_id": None, "step_id": "VALID_FALLBACK"},
            {**base, "device_step_id": 1},
            {**base, "device_step_id": "   "},
            {**base, "device_step_id": "A", "step_id": "B"},
        ):
            with self.subTest(invalid_step=invalid_step):
                package = device_result_to_v2(
                    {
                        "status": "success",
                        "workflow_json": {"steps": [invalid_step]},
                        "dispatch_validation": {"status": "passed", "errors": []},
                    },
                    research,
                )
                self.assertEqual(package.status, "human_review_required")
                self.assertEqual(package.workstation_mapping.device_steps, [])

        duplicate = device_result_to_v2(
            {
                "status": "success",
                "workflow_json": {
                    "steps": [
                        {**base, "device_step_id": "DUP"},
                        {**base, "device_step_id": "DUP"},
                    ]
                },
                "dispatch_validation": {"status": "passed", "errors": []},
            },
            research,
        )
        self.assertEqual(duplicate.status, "human_review_required")
        self.assertEqual(duplicate.workstation_mapping.device_steps, [])
        self.assertEqual(duplicate.validation.locked_step_hashes, {})

    def test_failed_dispatch_validation_never_becomes_ready(self):
        research = research_with_ids(["A"])
        workflow = {
            "steps": [
                {
                    "device_step_id": "DS_A",
                    "source_macro_step_id": "A",
                    "station_code": "Audit_Station_V1",
                    "platform_name": "audit",
                    "operation": "audit",
                }
            ]
        }
        explained = device_result_to_v2(
            {
                "status": "success",
                "workflow_json": workflow,
                "dispatch_validation": {
                    "status": "failed",
                    "errors": ["station contract rejected DS_A"],
                },
            },
            research,
        )
        self.assertEqual(explained.status, "human_review_required")
        self.assertEqual(explained.validation.status, "failed")
        self.assertTrue(explained.validation.issues)

        unexplained = device_result_to_v2(
            {
                "status": "success",
                "workflow_json": workflow,
                "dispatch_validation": {"status": "failed", "errors": []},
            },
            research,
        )
        self.assertEqual(unexplained.status, "human_review_required")
        self.assertEqual(unexplained.validation.status, "failed")
        synthesized = [
            issue for issue in unexplained.validation.issues
            if issue.issue_id == "VI_DISPATCH_STATUS_0001"
        ]
        self.assertEqual(len(synthesized), 1)
        self.assertEqual(synthesized[0].field_path, "/dispatch_validation/status")

        missing = device_result_to_v2(
            {"status": "success", "workflow_json": workflow},
            research,
        )
        self.assertEqual(missing.status, "human_review_required")
        self.assertEqual(missing.validation.status, "failed")
        self.assertEqual(
            [issue.issue_id for issue in missing.validation.issues],
            ["VI_DISPATCH_STATUS_0001"],
        )

    def test_identity_encoding_declarations_must_agree_and_be_supported(self):
        research = research_with_ids(["A"])
        step = {
            "device_step_id": "DS_A",
            "source_macro_step_id": "A",
            "station_code": "Audit_Station_V1",
            "platform_name": "audit",
            "operation": "audit",
        }
        cases = (
            {
                "identity_encoding": IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
                "workflow_json": {"identity_encoding": None, "steps": [step]},
            },
            {
                "identity_encoding": IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
                "workflow_json": {
                    "identity_encoding": IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
                    "steps": [step],
                },
                "error_package": {"identity_encoding": None},
            },
            {
                "workflow_json": {"identity_encoding": "typed-json-scalar-v9", "steps": [step]},
            },
        )
        for extra in cases:
            with self.subTest(extra=extra):
                result = {
                    "status": "success",
                    "dispatch_validation": {"status": "passed", "errors": []},
                    **extra,
                }
                package = device_result_to_v2(result, research)
                self.assertEqual(package.status, "human_review_required")
                self.assertEqual(package.workstation_mapping.device_steps, [])
                self.assertTrue(
                    any(
                        issue.validator == "typed_macro_source_binding_v1"
                        for issue in package.validation.issues
                    )
                )

    def test_terminal_requires_exact_macro_step_id(self):
        research = research_fixture()
        exact = device_result_to_v2(
            {
                "status": "feasibility_error",
                "error_package": {
                    "blocking_constraints": ["MS_002 has no compatible workstation"]
                },
            },
            research,
        )
        self.assertEqual(exact.status, "terminal_unmappable")
        self.assertEqual(exact.terminal_macro_step_ids, ["MS_002"])

        ambiguous = device_result_to_v2(
            {"status": "feasibility_error", "error_package": {"blocking_constraints": ["gap"]}},
            research,
        )
        self.assertEqual(ambiguous.status, "human_review_required")

    def test_terminal_numeric_like_ids_require_structured_typed_binding(self):
        research = research_with_ids(["A", "1"])
        structured = device_result_to_v2(
            {
                "status": "terminal_unmappable",
                "error_package": {"source_macro_step_id": "1"},
            },
            research,
        )
        self.assertEqual(structured.status, "terminal_unmappable")
        self.assertEqual(structured.terminal_macro_step_ids, ["1"])

        text_only = device_result_to_v2(
            {
                "status": "terminal_unmappable",
                "error_package": {"blocking_constraints": ["macro steps [1] failed"]},
            },
            research,
        )
        self.assertEqual(text_only.status, "human_review_required")
        self.assertEqual(text_only.terminal_macro_step_ids, [])

    def test_terminal_explicit_invalid_structured_id_blocks_text_fallback(self):
        research = research_with_ids(["M1"])
        for invalid in (None, "", False):
            with self.subTest(invalid=invalid):
                package = device_result_to_v2(
                    {
                        "status": "terminal_unmappable",
                        "error_package": {
                            "macro_step_id": invalid,
                            "message": "M1 has no compatible station",
                        },
                    },
                    research,
                )
                self.assertEqual(package.status, "human_review_required")
                self.assertEqual(package.terminal_macro_step_ids, [])

    def test_terminal_free_text_rejects_containment_and_reserved_prefix_ambiguity(self):
        for identifiers, message in (
            (["A/B", "B"], "A/B failed"),
            (["foo.bar", "bar"], "foo.bar failed"),
            (["A+B", "B"], "A+B failed"),
            (["a b", "b"], "a b failed"),
            ([f"{IDENTITY_WIRE_PREFIX}i:1"], f"{IDENTITY_WIRE_PREFIX}i:1 failed"),
        ):
            with self.subTest(identifiers=identifiers):
                package = device_result_to_v2(
                    {
                        "status": "terminal_unmappable",
                        "error_package": {"blocking_constraints": [message]},
                    },
                    research_with_ids(identifiers),
                )
                self.assertEqual(package.status, "human_review_required")
                self.assertEqual(package.terminal_macro_step_ids, [])

        no_error_evidence = device_result_to_v2(
            {
                "status": "terminal_unmappable",
                "workflow_json": {
                    "steps": [{"source_macro_step_id": "MS_002"}]
                },
                "error_package": {},
            },
            research_fixture(),
        )
        self.assertEqual(no_error_evidence.status, "human_review_required")
        self.assertEqual(no_error_evidence.terminal_macro_step_ids, [])

        key_name_is_not_evidence = device_result_to_v2(
            {
                "status": "terminal_unmappable",
                "error_package": {"reason": "no station"},
            },
            research_with_ids(["reason"]),
        )
        self.assertEqual(key_name_is_not_evidence.status, "human_review_required")

    def test_attach_terminal_rewrites_identity_fields_with_matching_marker(self):
        literal = f"{IDENTITY_WIRE_PREFIX}i:1"
        research = research_with_ids([literal])
        updated = attach_device_v2_contract(
            {
                "status": "terminal_unmappable",
                "error_package": {
                    "source_macro_step_id": literal,
                    "reason": "no station",
                },
            },
            research,
        )
        encoded_literal = research.macro_steps[0].macro_step_id
        self.assertEqual(updated["status"], "terminal_unmappable")
        self.assertEqual(updated["error_package"]["macro_step_ids"], [encoded_literal])
        self.assertEqual(
            updated["error_package"]["identity_encoding"],
            IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
        )
        self.assertNotIn("source_macro_step_id", updated["error_package"])

        reloaded = device_result_to_v2(updated, research)
        self.assertEqual(reloaded.status, "terminal_unmappable")
        self.assertEqual(reloaded.terminal_macro_step_ids, [encoded_literal])

    def test_attach_propagates_device_internal_error_to_outer_result(self):
        research = research_with_ids(["A"])
        for raw in (
            {"status": "success", "workflow_json": {"steps": []}},
            {
                "status": "mystery",
                "workflow_json": {
                    "steps": [
                        {
                            "device_step_id": "DS_A",
                            "source_macro_step_id": "A",
                            "station_code": "Audit_Station_V1",
                            "platform_name": "audit",
                            "operation": "audit",
                        }
                    ]
                },
                "dispatch_validation": {"status": "passed", "errors": []},
            },
        ):
            with self.subTest(raw=raw):
                updated = attach_device_v2_contract(raw, research)
                self.assertEqual(updated["status"], "device_internal_error")
                self.assertEqual(updated["feedback_type"], "device_internal_error")
                self.assertEqual(updated["feedback_route"], "device")
                self.assertEqual(updated["error_package"]["type"], "device_internal_error")

    def test_direct_device_and_observation_models_reject_invalid_wire_identities(self):
        research = research_with_ids([1])
        device = device_result_to_v2(
            {
                "status": "success",
                "identity_encoding": IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
                "workflow_json": {
                    "steps": [
                        {
                            "device_step_id": "DS_1",
                            "source_macro_step_id": research.macro_steps[0].macro_step_id,
                            "station_code": "Audit_Station_V1",
                            "platform_name": "audit",
                            "operation": "audit",
                        }
                    ]
                },
                "dispatch_validation": {"status": "passed", "errors": []},
            },
            research,
        )
        malformed = f"{IDENTITY_WIRE_PREFIX}i:01"
        payload = device.model_dump(mode="json")
        payload["workstation_mapping"]["device_steps"][0]["source_macro_step_id"] = malformed
        payload["workstation_mapping"]["device_steps"][0]["source_macro_step_ids"] = [malformed]
        with self.assertRaisesRegex(ValueError, "canonical integer"):
            DeviceWorkflowPackageV2.model_validate(payload)

        payload = device.model_dump(mode="json")
        payload["validation"]["status"] = "failed"
        with self.assertRaisesRegex(ValueError, "validation.status=passed"):
            DeviceWorkflowPackageV2.model_validate(payload)

        payload = device.model_dump(mode="json")
        payload["workstation_mapping"]["device_steps"] = []
        payload["workflow"] = {}
        with self.assertRaisesRegex(ValueError, "at least one device step"):
            DeviceWorkflowPackageV2.model_validate(payload)

        payload = device.model_dump(mode="json")
        payload["workstation_mapping"]["device_steps"].append(
            dict(payload["workstation_mapping"]["device_steps"][0])
        )
        with self.assertRaisesRegex(ValueError, "device_step_id values must be unique"):
            DeviceWorkflowPackageV2.model_validate(payload)

        event = build_observation_event_v2({}, research, device)
        event_payload = event.model_dump(mode="json")
        event_payload["macro_action_id"] = malformed
        with self.assertRaisesRegex(ValueError, "canonical integer"):
            ObservationEventV2.model_validate(event_payload)

        event_payload = event.model_dump(mode="json")
        event_payload["macro_parameter_summary"][0]["macro_step_id"] = malformed
        with self.assertRaisesRegex(ValueError, "canonical integer"):
            ObservationEventV2.model_validate(event_payload)

    def test_observation_has_planned_setpoint_actual_and_deviation(self):
        research = research_fixture()
        device = device_result_to_v2(
            {
                "status": "success",
                "workflow_json": {
                    "steps": [
                        {
                            "device_step_id": "DS_1",
                            "source_macro_step_id": "MS_001",
                            "station_code": "Liquid_Handling_Station_1ml_V2",
                            "platform_name": "移液平台1ml_V2",
                            "id": 36,
                            "operation": "加液_物料绑定",
                            "parameters": {"numeric_parameter_1": 9.8},
                        }
                    ]
                },
                "dispatch_validation": {"status": "passed", "errors": []},
            },
            research,
        )
        event = build_observation_event_v2(
            {
                "status": "completed",
                "device_parameter_trace": [
                    {
                        "device_step_id": "DS_1",
                        "name": "numeric_parameter_1",
                        "actual_value": 9.7,
                        "unit": "mL",
                    }
                ],
            },
            research,
            device,
        )
        trace = event.device_parameter_trace[0]
        self.assertEqual(trace.planned_value, 10)
        self.assertEqual(trace.device_setpoint, 9.8)
        self.assertEqual(trace.actual_value, 9.7)
        self.assertAlmostEqual(trace.deviation, -0.3)
        self.assertTrue(event.macro_parameter_summary)
        self.assertEqual(event.identity_encoding, research.identity_encoding)


if __name__ == "__main__":
    unittest.main()

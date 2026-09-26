"""Offline schema/adapter parity regressions for logical Research containers."""

from __future__ import annotations

from copy import deepcopy
import unittest

from chem_agent_contracts.adapters import research_state_to_v2
from chem_agent_contracts.container_requirements import (
    LogicalContainerContractError, parse_logical_container_requirements,
)
from chem_agent_contracts.identity import encode_json_scalar_identity
from chem_agent_contracts.v2 import LogicalContainerV2, LOGICAL_LID_STATE_PROMPT
from reaserch_agent.prompts.task_prompts import MACRO_STEP_CONTRACT_PROMPT


def container_step(lid_state="none"):
    return {
        "macro_step_id": "MS_MA_S01_R00_008", "操作": "XRD滴液检测全流程",
        "container_requirements": [
            {"logical_container_id": "vial", "container_type": "进样瓶", "lid_state": "open"},
            {"logical_container_id": "wash", "container_type": "进样瓶", "lid_state": "closed"},
            {"logical_container_id": "A02-XRD-CARRIER", "container_type": "XRD基底片",
             "count": 1, "capacity_ml": None, "lid_state": lid_state},
        ],
    }


class LogicalContainerRequirementsTests(unittest.TestCase):
    def test_prompt_and_schema_share_enum_and_semantics(self):
        allowed = LogicalContainerV2.model_json_schema()["properties"]["lid_state"]["enum"]
        self.assertEqual(allowed, ["open", "closed", "none", "unknown"])
        rendered = MACRO_STEP_CONTRACT_PROMPT.format(macro_action_json="{}", device_context_json="{}")
        self.assertIn(LOGICAL_LID_STATE_PROMPT, rendered)
        self.assertNotIn("__LOGICAL_LID_STATE_PROMPT__", rendered)
        for value in allowed:
            self.assertIn(f"{value}：", rendered)
        self.assertIn("none/unknown 都不能替代真实进样瓶", rendered)

    def test_all_legal_lid_states_pass_without_conversion(self):
        for value in ("open", "closed", "none", "unknown"):
            with self.subTest(value=value):
                self.assertEqual(parse_logical_container_requirements(
                    container_step(value), sequence=8)[2].lid_state, value)

    def test_illegal_lid_values_have_precise_structured_location(self):
        for value in ("not_applicable", "OPEN", "无盖", "", None, True, 0, 1, [], {}):
            with self.subTest(value=value), self.assertRaises(LogicalContainerContractError) as caught:
                parse_logical_container_requirements(container_step(value), sequence=8)
            issue = caught.exception.issues[0]
            self.assertEqual(issue.field_path, "/macro_plan/7/container_requirements/2/lid_state")
            self.assertEqual(issue.macro_step_id, "MS_MA_S01_R00_008")
            self.assertEqual(issue.actual, value)
            self.assertEqual(issue.expected, ["open", "closed", "none", "unknown"])
            self.assertIn("A02-XRD-CARRIER", issue.rule)
            self.assertEqual(issue.repair_scope, "macro_step")

    def test_container_issue_preserves_zero_and_typed_macro_identity_on_wire(self):
        for identity in (0, 1, "1", "001"):
            with self.subTest(identity=identity):
                step = container_step("not_applicable")
                step["macro_step_id"] = identity
                with self.assertRaises(LogicalContainerContractError) as caught:
                    parse_logical_container_requirements(step, sequence=8)
                self.assertEqual(
                    caught.exception.issues[0].macro_step_id,
                    encode_json_scalar_identity(identity),
                )

    def test_adapter_rejects_exact_same_invalid_field_with_context(self):
        state = {"macro_plan": [container_step("not_applicable")]}
        original = deepcopy(state)
        with self.assertRaises(LogicalContainerContractError) as caught:
            research_state_to_v2(state)
        self.assertEqual(caught.exception.issues[0].field_path,
                         "/macro_plan/0/container_requirements/2/lid_state")
        self.assertEqual(state, original)

    def test_legacy_omission_defaults_remain_compatible(self):
        for raw in ({}, {"count": None, "capacity_ml": None}):
            with self.subTest(raw=raw):
                parsed = parse_logical_container_requirements(
                    {"container_requirements": [raw]}, sequence=1, step_id="MS_REAL")[0]
                self.assertEqual(parsed.logical_container_id, "LC_MS_REAL_1")
                self.assertEqual(parsed.container_type, "unknown")
                self.assertEqual(parsed.count, 1)
                self.assertIsNone(parsed.capacity_ml)
                self.assertEqual(parsed.lid_state, "unknown")
        self.assertEqual(parse_logical_container_requirements({}, sequence=1), [])

    def test_valid_numeric_values_and_metadata_survive_without_mutation(self):
        step = container_step()
        step["container_requirements"][0].update(count=2, capacity_ml=50, rationale="logical only")
        original = deepcopy(step)
        parsed = parse_logical_container_requirements(step, sequence=8)
        self.assertEqual(parsed[0].count, 2)
        self.assertEqual(parsed[0].capacity_ml, 50)
        self.assertEqual(step, original)
        self.assertNotIn("rationale", parsed[0].model_dump())

    def test_invalid_counts_are_not_truncated_or_defaulted(self):
        for value in (True, False, 0, -1, 1.0, 1.5, "2", [], {}):
            with self.subTest(value=value):
                step = container_step()
                step["container_requirements"][2]["count"] = value
                with self.assertRaises(LogicalContainerContractError) as caught:
                    parse_logical_container_requirements(step, sequence=8)
                self.assertEqual(caught.exception.issues[0].field_path,
                                 "/macro_plan/7/container_requirements/2/count")

    def test_invalid_identity_types_do_not_turn_into_generated_defaults(self):
        for field in ("logical_container_id", "container_type"):
            for value in (False, True, 0, 1, [], {}):
                with self.subTest(field=field, value=value):
                    step = container_step()
                    step["container_requirements"][2][field] = value
                    with self.assertRaises(LogicalContainerContractError) as caught:
                        parse_logical_container_requirements(step, sequence=8)
                    self.assertEqual(caught.exception.issues[0].field_path,
                                     f"/macro_plan/7/container_requirements/2/{field}")

    def test_invalid_capacity_is_not_dropped_or_coerced(self):
        for value in (True, False, 0, -1, "50", float("inf"), float("nan"), [], {}):
            with self.subTest(value=value):
                step = container_step()
                step["container_requirements"][2]["capacity_ml"] = value
                with self.assertRaises(LogicalContainerContractError) as caught:
                    parse_logical_container_requirements(step, sequence=8)
                self.assertEqual(caught.exception.issues[0].field_path,
                                 "/macro_plan/7/container_requirements/2/capacity_ml")

    def test_malformed_requirement_array_and_items_fail_closed(self):
        for value in (None, {}, "vial", 1, False):
            with self.subTest(value=value), self.assertRaises(LogicalContainerContractError) as caught:
                parse_logical_container_requirements({"container_requirements": value}, sequence=8)
            self.assertEqual(caught.exception.issues[0].field_path, "/macro_plan/7/container_requirements")
        for item in (None, "vial", [], False, 1):
            with self.subTest(item=item), self.assertRaises(LogicalContainerContractError) as caught:
                parse_logical_container_requirements({"container_requirements": [item]}, sequence=8)
            self.assertEqual(caught.exception.issues[0].field_path, "/macro_plan/7/container_requirements/0")

    def test_multiple_bad_fields_keep_all_paths(self):
        step = container_step("not_applicable")
        step["container_requirements"][0]["count"] = -2
        step["container_requirements"][1]["capacity_ml"] = "large"
        with self.assertRaises(LogicalContainerContractError) as caught:
            parse_logical_container_requirements(step, sequence=8)
        self.assertEqual([issue.field_path for issue in caught.exception.issues], [
            "/macro_plan/7/container_requirements/0/count",
            "/macro_plan/7/container_requirements/1/capacity_ml",
            "/macro_plan/7/container_requirements/2/lid_state",
        ])

    def test_physical_binding_cannot_be_smuggled_through_adapter(self):
        for key in ("station_id", "workstation_id", "slot_id", "bottle_id", "容器编号", "瓶号", "槽位"):
            with self.subTest(key=key):
                step = container_step()
                step["container_requirements"][2][key] = "physical-1"
                with self.assertRaises(LogicalContainerContractError) as caught:
                    research_state_to_v2({"macro_plan": [step]})
                self.assertTrue(caught.exception.issues[0].field_path.endswith("/" + key))


if __name__ == "__main__":
    unittest.main()

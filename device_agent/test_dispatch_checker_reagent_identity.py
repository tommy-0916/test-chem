"""Shared reagent-slot identity integration for the read-only checker."""

from __future__ import annotations

import copy

from device_agent.dispatch_checker import check_dispatch
from device_agent.test_dispatch_checker_cross_source import (
    addition_workflow,
    package,
)
from device_agent.test_dispatch_checker_real_contract import CONTRACT_ROOT


STATION = "Liquid_Handling_Station_1ml_V1"


def slot(number: int, identity: str, name: str = "水") -> dict:
    return {
        "工作站": STATION,
        "原液编号": number,
        "material_identity_id": identity,
        "canonical_name": name,
    }


def root(identity: str, name: str = "水") -> dict:
    return {
        "is_root_batch": True,
        "material_identity_id": identity,
        "research_material_identity_id": identity,
        "material_id": name,
    }


def checked(value: dict) -> dict:
    original = copy.deepcopy(value)
    report = check_dispatch(
        value,
        workstation_root=CONTRACT_ROOT,
        require_payload=True,
    )
    assert value == original
    assert report["input_sha256"]
    assert "checker_mutated_input" not in {
        finding["code"] for finding in report["findings"]
    }
    return report


def test_authoritative_registry_passes_without_legacy_identity_limitation():
    value = package(addition_workflow([(1, "水", 0.5)]))
    value["reagent_slot_plan"] = [slot(1, "MAT_WATER")]
    value["batch_plan"] = [root("MAT_WATER")]

    report = checked(value)

    assert report["status"] == "passed", report["findings"]
    assert report["checks"]["reagent_slot_identity"] == "executed"
    assert any("material_identity_id" in item for item in report["limitations"])
    assert not any("同名跨源瓶" in item for item in report["limitations"])


def test_noncanonical_name_is_reported_without_first_label_conflict_heuristic():
    value = package(addition_workflow([(1, "水", 0.5), (1, "乙醇", 0.5)]))
    value["reagent_slot_plan"] = [slot(1, "MAT_WATER")]
    value["batch_plan"] = [root("MAT_WATER")]

    report = checked(value)
    codes = [finding["code"] for finding in report["findings"]]

    assert report["status"] == "failed"
    assert codes.count("noncanonical_reagent_name") == 1
    assert "source_bottle_identity_conflict" not in codes
    finding = next(
        item for item in report["findings"]
        if item["code"] == "noncanonical_reagent_name"
    )
    assert finding["step_number"] == 4
    assert finding["json_pointer"].startswith("/workflow_json/steps/3/")
    assert finding["rule_id"] == "reagent_slot_identity/v1"


def test_confirmed_identity_across_two_slots_uses_one_volume_ledger():
    entries = [(1, "水", 1.0), (1, "水", 1.0), (2, "水", 1.0), (2, "水", 1.0)]
    value = package(addition_workflow(entries))
    value["reagent_slot_plan"] = [
        slot(1, "MAT_WATER"),
        slot(2, "MAT_WATER"),
    ]
    value["batch_plan"] = [root("MAT_WATER")]

    report = checked(value)
    confirmed = [
        finding for finding in report["findings"]
        if finding["code"] == "total_reagent_volume"
        and finding.get("identity_basis") == "material_identity_id"
    ]

    assert report["status"] == "failed", report["findings"]
    assert len(confirmed) == 1
    assert confirmed[0]["material_identity_id"] == "MAT_WATER"
    assert confirmed[0]["actual"] == 4.0
    assert not any(
        finding["code"] == "cross_source_reagent_identity_unverified"
        for finding in report["findings"]
    )


def test_unanchored_distinct_ids_cannot_split_same_label_and_pass():
    entries = [(1, "水", 1.0), (1, "水", 1.0), (2, "水", 1.0), (2, "水", 1.0)]
    value = package(addition_workflow(entries))
    value["reagent_slot_plan"] = [
        slot(1, "MAT_WATER_A"),
        slot(2, "MAT_WATER_B"),
    ]

    report = checked(value)

    assert report["status"] == "not_verifiable", report["findings"]
    codes = {finding["code"] for finding in report["findings"]}
    assert "reagent_slot_identity_unanchored" in codes
    assert "cross_source_reagent_identity_unverified" in codes


def test_anchored_distinct_material_identities_remain_separate():
    entries = [(1, "水", 1.0), (1, "水", 1.0), (2, "水", 1.0), (2, "水", 1.0)]
    value = package(addition_workflow(entries))
    value["reagent_slot_plan"] = [
        slot(1, "MAT_WATER_A"),
        slot(2, "MAT_WATER_B"),
    ]
    value["batch_plan"] = [
        root("MAT_WATER_A", "source A"),
        root("MAT_WATER_B", "source B"),
    ]

    report = checked(value)

    assert report["status"] == "passed", report["findings"]
    assert not any(
        finding["code"] in {
            "total_reagent_volume",
            "cross_source_reagent_identity_unverified",
        }
        for finding in report["findings"]
    )


def test_ledger_membership_can_anchor_a_slot_identity():
    value = package(addition_workflow([(1, "水", 0.5)]))
    value["reagent_slot_plan"] = [slot(1, "MAT_WATER")]
    value["material_ledger"] = {
        "entries": [
            {"entry_id": "water", "material_identity_id": "MAT_WATER"}
        ]
    }

    report = checked(value)

    assert report["status"] == "passed", report["findings"]
    assert "reagent_slot_identity_unanchored" not in {
        finding["code"] for finding in report["findings"]
    }


def test_nested_registry_anchors_same_derived_identity_as_normalizer():
    value = package(addition_workflow([(1, "Derived X canonical", 0.5)]))
    value["reagent_slot_plan"] = [
        slot(1, "DERIVED_X", "Derived X canonical")
    ]
    value["research_handoff"] = {
        "semantic_analysis": {
            "material_identity_registry": [
                {
                    "identity_id": "DERIVED_X",
                    "canonical_name": "Derived X canonical",
                }
            ]
        }
    }

    report = checked(value)
    codes = {finding["code"] for finding in report["findings"]}

    assert report["status"] == "passed", report["findings"]
    assert "reagent_slot_identity_unanchored" not in codes
    assert "untrusted_reagent_slot_identity" not in codes


def test_partial_trusted_registry_rejects_unknown_slot_identity():
    value = package(addition_workflow([(1, "水", 0.5), (2, "乙醇", 0.5)]))
    value["reagent_slot_plan"] = [
        slot(1, "MAT_WATER", "水"),
        slot(2, "MAT_ETHANOL", "乙醇"),
    ]
    value["batch_plan"] = [root("MAT_WATER")]

    report = checked(value)

    assert report["status"] == "failed", report["findings"]
    finding = next(
        item for item in report["findings"]
        if item["code"] == "untrusted_reagent_slot_identity"
    )
    assert finding["actual"] == ["MAT_ETHANOL"]


def test_source_plan_authorization_is_enforced_by_shared_rule():
    workflow = addition_workflow([(1, "水", 0.5)])
    workflow["steps"][2]["source_plan_step"] = 7
    value = package(workflow)
    value["reagent_slot_plan"] = [slot(1, "MAT_WATER")]
    value["batch_plan"] = [root("MAT_WATER")]
    value["device_plan"] = [
        {"plan_step": 7, "source_material_identity_ids": ["MAT_OTHER"]}
    ]

    report = checked(value)

    finding = next(
        item for item in report["findings"]
        if item["code"] == "unauthorized_slot_identity"
    )
    assert report["status"] == "failed"
    assert finding["step_number"] == 3
    assert finding["material_identity_id"] == "MAT_WATER"
    assert finding["authorized_material_identity_ids"] == ["MAT_OTHER"]

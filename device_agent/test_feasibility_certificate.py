from __future__ import annotations

import copy

try:
    from .feasibility_certificate import (
        DEVICE_PLAN_CONTRACT_DEFAULTS,
        device_plan_contract_digest,
        device_plan_contract_view,
    )
except ImportError:
    from feasibility_certificate import (
        DEVICE_PLAN_CONTRACT_DEFAULTS,
        device_plan_contract_digest,
        device_plan_contract_view,
    )


def test_device_plan_contract_digest_covers_every_declared_contract_field():
    baseline = device_plan_contract_view({})
    baseline_digest = device_plan_contract_digest(baseline)

    for key, default in DEVICE_PLAN_CONTRACT_DEFAULTS.items():
        changed = copy.deepcopy(baseline)
        if isinstance(default, list):
            changed[key] = [{"changed": key}]
        elif isinstance(default, dict):
            changed[key] = {"changed": key}
        else:
            changed[key] = f"changed:{key}"
        assert device_plan_contract_digest(changed) != baseline_digest, key


def test_device_plan_contract_digest_ignores_runtime_derived_fields():
    plan = device_plan_contract_view({})
    with_runtime_state = copy.deepcopy(plan)
    with_runtime_state.update(
        {
            "status": "manual_required",
            "quantity_audit": {"status": "failed"},
            "workflow_json": {"steps": [{"device_step_id": "DS_1"}]},
            "dispatch_payload": {"run": "not-part-of-plan"},
            "feasibility_certificate": {"certificate_id": "recursive"},
        }
    )

    assert device_plan_contract_digest(with_runtime_state) == (
        device_plan_contract_digest(plan)
    )


def test_device_plan_contract_view_covers_review_barriers_with_safe_defaults():
    baseline = device_plan_contract_view({})

    assert baseline["requires_scientific_review"] is False
    assert baseline["quantity_contract_required"] is False
    assert baseline["pending_quantity_human_review"] == {}

    declared = {
        "requires_scientific_review": True,
        "quantity_contract_required": True,
        "pending_quantity_human_review": {
            "reason": "unresolved_quantity_requirement",
        },
    }
    view = device_plan_contract_view(declared)

    assert view["requires_scientific_review"] is True
    assert view["quantity_contract_required"] is True
    assert view["pending_quantity_human_review"] == declared[
        "pending_quantity_human_review"
    ]

    declared["pending_quantity_human_review"]["reason"] = "mutated"
    assert view["pending_quantity_human_review"]["reason"] == (
        "unresolved_quantity_requirement"
    )


def test_each_review_barrier_changes_the_device_plan_contract_digest():
    baseline_digest = device_plan_contract_digest({})
    changed_values = {
        "requires_scientific_review": True,
        "quantity_contract_required": True,
        "pending_quantity_human_review": {
            "reason": "unresolved_quantity_requirement",
        },
    }

    for key, value in changed_values.items():
        assert device_plan_contract_digest({key: value}) != baseline_digest, key

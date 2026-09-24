from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from device_agent.human_quantity_approval import (
    HumanQuantityApprovalError,
    approved_quantity_contract_digest,
    consume_validated_approval_capability,
    consume_validated_human_quantity_approval_bundle,
    validate_human_quantity_approvals,
)


REQUEST_DIGEST = "a" * 64


def _request() -> dict:
    return {
        "schema_version": "chem-device-repair-request/v1",
        "request_id": "device-repair-001",
        "frozen_route_signature": "route-001",
        "frozen_sample_matrix_signature": "matrix-001",
        "device_snapshot_signature": "snapshot-001",
        "structured_errors": [
            {"code": "unknown_yield", "transition_id": "transition-001"}
        ],
        "feasibility_certificate": {
            "accepted": True,
            "certificate_id": "certificate-001",
            "protected_digest": "certificate-digest-001",
            "research_plan_signature": "route-001",
            "sample_matrix_signature": "matrix-001",
            "device_snapshot_signature": "snapshot-001",
        },
    }


def _override(*, basis: str = "observed_quantity") -> dict:
    approval = {
        "approval_id": "approval-001",
        "repair_request_id": "device-repair-001",
        "repair_request_sha256": REQUEST_DIGEST,
        "feasibility_certificate_id": "certificate-001",
        "feasibility_certificate_digest": "certificate-digest-001",
        "research_plan_signature": "route-001",
        "sample_matrix_signature": "matrix-001",
        "device_snapshot_signature": "snapshot-001",
        "transition_id": "transition-001",
        "sample_id": "sample-001",
        "material_id": "dry-product-001",
        "batch_id": "dry-batch-001",
        "basis": basis,
        "approved_quantity": {"value": 90, "unit": "umol"},
        "approved_by": "reviewer@example.org",
        "approved_at": "2026-08-27T06:30:00+08:00",
        "acknowledges_scientific_review": True,
    }
    transition = {
        "transition_id": "transition-001",
        "transition_kind": "state_change",
        "quantity_basis": "measured_observation",
        "parent_batch_ids": ["wet-batch-001"],
        "child_batch_ids": ["dry-batch-001"],
        "input_allocations": [
            {
                "batch_id": "wet-batch-001",
                "quantity": {"value": 0.18, "unit": "mmol"},
            }
        ],
        "output_allocations": [
            {
                "batch_id": "dry-batch-001",
                "quantity": {"value": 0.09, "unit": "mmol"},
            }
        ],
    }
    if basis == "planning_yield_lower_bound":
        approval["yield_lower_bound"] = 0.5
        transition["quantity_basis"] = "planning_yield_lower_bound"
        transition["yield_lower_bound"] = 0.5
    return {
        "schema_version": "chem-device-plan-override/v1",
        "request_id": "device-repair-001",
        "device_plan": {
            "batch_plan": [
                {
                    "batch_id": "wet-batch-001",
                    "sample_id": "sample-001",
                    "material_id": "wet-precursor-001",
                    "total_quantity": {"value": 0.18, "unit": "mmol"},
                },
                {
                    "batch_id": "dry-batch-001",
                    "sample_id": "sample-001",
                    "material_id": "dry-product-001",
                    "total_quantity": {"value": 0.09, "unit": "mmol"},
                },
            ],
            "material_transitions": [transition],
            "device_plan": [{"plan_step": 1}],
        },
        "human_quantity_approvals": [approval],
    }


@pytest.mark.parametrize(
    ("basis", "attestation_kind"),
    [
        ("observed_quantity", "human_attested_measurement"),
        (
            "planning_yield_lower_bound",
            "human_approved_planning_bound",
        ),
    ],
)
def test_valid_approval_is_bound_and_promoted_to_trusted_envelope(
    basis: str,
    attestation_kind: str,
) -> None:
    sanitized, envelope, records, bundle = validate_human_quantity_approvals(
        _override(basis=basis),
        _request(),
        repair_request_sha256=REQUEST_DIGEST,
    )

    assert envelope["validated"] is True
    assert envelope["repair_request_id"] == "device-repair-001"
    assert envelope["repair_request_digest"] == REQUEST_DIGEST
    assert envelope["approval_count"] == 1
    assert records[0]["attestation_kind"] == attestation_kind
    assert records[0]["scientific_review"] is True
    assert bundle is None
    assert "human_quantity_approvals" not in sanitized
    assert "validated_human_quantity_approvals" not in sanitized


def test_empty_approval_list_does_not_create_a_trusted_envelope() -> None:
    override = _override()
    override["human_quantity_approvals"] = []

    sanitized, envelope, records, bundle = validate_human_quantity_approvals(
        override,
        _request(),
        repair_request_sha256=REQUEST_DIGEST,
    )

    assert envelope == {}
    assert records == []
    assert bundle is None
    assert "validated_human_quantity_approvals" not in sanitized


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repair_request_id", "wrong-request"),
        ("repair_request_sha256", "b" * 64),
        ("feasibility_certificate_digest", "wrong-certificate"),
        ("research_plan_signature", "wrong-route"),
        ("sample_matrix_signature", "wrong-matrix"),
        ("device_snapshot_signature", "wrong-snapshot"),
        ("transition_id", "wrong-transition"),
        ("sample_id", "wrong-sample"),
        ("material_id", "wrong-material"),
        ("batch_id", "wrong-batch"),
        ("approved_at", "2026-08-27T06:30:00"),
    ],
)
def test_wrong_request_certificate_or_target_binding_is_rejected(
    field: str,
    value: str,
) -> None:
    override = _override()
    override["human_quantity_approvals"][0][field] = value

    with pytest.raises(HumanQuantityApprovalError):
        validate_human_quantity_approvals(
            override,
            _request(),
            repair_request_sha256=REQUEST_DIGEST,
        )


def test_override_wrapper_request_id_mismatch_is_rejected() -> None:
    override = _override()
    override["request_id"] = "wrong-wrapper-request"

    with pytest.raises(HumanQuantityApprovalError):
        validate_human_quantity_approvals(
            override,
            _request(),
            repair_request_sha256=REQUEST_DIGEST,
        )


@pytest.mark.parametrize(
    "missing_field",
    [
        "approval_id",
        "transition_id",
        "sample_id",
        "material_id",
        "batch_id",
        "approved_by",
        "approved_at",
    ],
)
def test_missing_required_approval_field_is_rejected(missing_field: str) -> None:
    override = _override()
    del override["human_quantity_approvals"][0][missing_field]

    with pytest.raises(HumanQuantityApprovalError):
        validate_human_quantity_approvals(
            override,
            _request(),
            repair_request_sha256=REQUEST_DIGEST,
        )


def test_quantity_drift_concentration_and_missing_acknowledgement_are_rejected() -> None:
    mutations = []
    quantity_drift = _override()
    quantity_drift["human_quantity_approvals"][0]["approved_quantity"] = {
        "value": 999,
        "unit": "mmol",
    }
    mutations.append(quantity_drift)
    concentration = _override()
    concentration["human_quantity_approvals"][0]["approved_quantity"] = {
        "value": 0.1,
        "unit": "mol/L",
    }
    mutations.append(concentration)
    no_ack = _override()
    no_ack["human_quantity_approvals"][0][
        "acknowledges_scientific_review"
    ] = False
    mutations.append(no_ack)

    for override in mutations:
        with pytest.raises(HumanQuantityApprovalError):
            validate_human_quantity_approvals(
                override,
                _request(),
                repair_request_sha256=REQUEST_DIGEST,
            )


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    "reserved_field",
    [
        "human_quantity_approval_validation",
        "validated_human_quantity_approvals",
        "_trusted_human_quantity_approvals",
        "approval_capability_token",
    ],
)
def test_caller_cannot_forge_reserved_validated_fields(
    nested: bool,
    reserved_field: str,
) -> None:
    override = _override()
    target = override["device_plan"] if nested else override
    target[reserved_field] = {"validated": True}

    with pytest.raises(HumanQuantityApprovalError):
        validate_human_quantity_approvals(
            override,
            _request(),
            repair_request_sha256=REQUEST_DIGEST,
        )


def test_approval_cannot_authorize_a_transition_not_named_by_unknown_yield() -> None:
    request = _request()
    request["structured_errors"] = [
        {"code": "unknown_yield", "transition_id": "different-transition"}
    ]

    with pytest.raises(HumanQuantityApprovalError):
        validate_human_quantity_approvals(
            _override(),
            request,
            repair_request_sha256=REQUEST_DIGEST,
        )


@pytest.mark.parametrize(
    ("basis", "wrong_transition_basis"),
    [
        ("observed_quantity", "planning_yield_lower_bound"),
        ("planning_yield_lower_bound", "measured_observation"),
    ],
)
def test_approval_basis_must_match_state_change_transition_basis(
    basis: str,
    wrong_transition_basis: str,
) -> None:
    override = _override(basis=basis)
    override["device_plan"]["material_transitions"][0][
        "quantity_basis"
    ] = wrong_transition_basis

    with pytest.raises(HumanQuantityApprovalError):
        validate_human_quantity_approvals(
            override,
            _request(),
            repair_request_sha256=REQUEST_DIGEST,
        )


def test_approval_cannot_target_identity_preserving_transition() -> None:
    override = _override()
    override["device_plan"]["material_transitions"][0][
        "transition_kind"
    ] = "process_same_material"

    with pytest.raises(HumanQuantityApprovalError):
        validate_human_quantity_approvals(
            override,
            _request(),
            repair_request_sha256=REQUEST_DIGEST,
        )


def test_runtime_capability_is_one_time_and_bound_to_exact_bundle() -> None:
    _, envelope, records, bundle = validate_human_quantity_approvals(
        _override(),
        _request(),
        repair_request_sha256=REQUEST_DIGEST,
        create_runtime_bundle=True,
    )
    assert bundle is not None
    with pytest.raises(TypeError):
        json.dumps(bundle)
    consumed = consume_validated_human_quantity_approval_bundle(bundle)
    assert consumed == (envelope, records)
    assert consume_validated_human_quantity_approval_bundle(bundle) is None

    _, tamper_envelope, tamper_records, tamper_bundle = validate_human_quantity_approvals(
        _override(),
        _request(),
        repair_request_sha256=REQUEST_DIGEST,
        create_runtime_bundle=True,
    )
    assert tamper_bundle is not None
    tamper_token = tamper_bundle._capability_token
    tamper_records[0]["approved_quantity"] = {"value": 999, "unit": "mmol"}
    assert (
        consume_validated_approval_capability(
            tamper_token,
            tamper_envelope,
            tamper_records,
        )
        is False
    )


def test_bundle_digest_detects_post_validation_plan_mutation() -> None:
    sanitized, envelope, _, bundle = validate_human_quantity_approvals(
        _override(basis="planning_yield_lower_bound"),
        _request(),
        repair_request_sha256=REQUEST_DIGEST,
        create_runtime_bundle=True,
    )
    assert bundle is not None
    assert envelope["approved_quantity_contract_digest"] == (
        approved_quantity_contract_digest(sanitized)
    )

    sanitized["device_plan"]["material_transitions"][0].update(
        {
            "transition_kind": "process_same_material",
            "quantity_basis": "conserved_inventory",
        }
    )

    assert envelope["approved_quantity_contract_digest"] != (
        approved_quantity_contract_digest(sanitized)
    )


def test_bundle_digest_detects_post_validation_quantity_adjustment() -> None:
    sanitized, envelope, _, _ = validate_human_quantity_approvals(
        _override(basis="planning_yield_lower_bound"),
        _request(),
        repair_request_sha256=REQUEST_DIGEST,
    )
    sanitized["device_plan"]["quantity_adjustments"] = [
        {
            "kind": "amount_change",
            "before": {"value": 1, "unit": "mmol"},
            "after": {"value": 999, "unit": "mmol"},
            "requires_scientific_review": True,
        }
    ]

    assert envelope["approved_quantity_contract_digest"] != (
        approved_quantity_contract_digest(sanitized)
    )


def test_quantity_contract_digest_normalizes_top_level_sidecar_merge() -> None:
    nested = _override(basis="planning_yield_lower_bound")
    nested["device_plan"]["quantity_adjustments"] = [
        {
            "kind": "split_transfer",
            "before": {"value": 90, "unit": "umol"},
            "after": {"value": 0.09, "unit": "mmol"},
        }
    ]
    top_level = copy.deepcopy(nested)
    for field in (
        "quantity_adjustments",
        "batch_plan",
        "material_transitions",
        "material_ledger",
    ):
        if field in top_level["device_plan"]:
            top_level[field] = top_level["device_plan"].pop(field)

    assert approved_quantity_contract_digest(nested) == (
        approved_quantity_contract_digest(top_level)
    )


def test_package_and_bare_import_share_type_and_capability_registry() -> None:
    package_module = importlib.import_module(
        "device_agent.human_quantity_approval"
    )
    bare_module = importlib.import_module("human_quantity_approval")

    assert package_module is bare_module
    assert (
        package_module.ValidatedHumanQuantityApprovalBundle
        is bare_module.ValidatedHumanQuantityApprovalBundle
    )


def test_direct_script_import_mode_shares_capability_registry() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    device_dir = repo_root / "device_agent"
    script = """
import human_quantity_approval as bare
import device_agent.human_quantity_approval as package
assert bare is package
envelope = {"validated": True}
records = [{"approval_id": "A"}]
token = bare.mint_validated_approval_capability(envelope, records)
assert package.consume_validated_approval_capability(token, envelope, records)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), str(device_dir)]
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=device_dir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr

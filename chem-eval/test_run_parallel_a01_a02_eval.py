"""Regression tests for the A01/A02 acceptance envelope."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).with_name("run_parallel_a01_a02_eval.py")
SPEC = importlib.util.spec_from_file_location("run_parallel_a01_a02_eval", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_terminal_status_accepts_legacy_and_canonical_v2_ready_states() -> None:
    assert MODULE._terminal_status_is_ready(
        {"status": "success", "feedback_route": "none"}
    )
    assert MODULE._terminal_status_is_ready(
        {"status": "ready_for_dispatch", "feedback_route": "success"}
    )


def test_terminal_status_rejects_mixed_or_failed_states() -> None:
    assert not MODULE._terminal_status_is_ready(
        {"status": "success", "feedback_route": "success"}
    )
    assert not MODULE._terminal_status_is_ready(
        {"status": "ready_for_dispatch", "feedback_route": "none"}
    )
    assert not MODULE._terminal_status_is_ready(
        {"status": "failed", "feedback_route": "device"}
    )
    assert not MODULE._terminal_status_is_ready(
        {
            "status": "ready_for_dispatch",
            "feedback_route": "success",
            "feedback_type": "human_review_required",
        }
    )
    assert not MODULE._terminal_status_is_ready(
        {
            "status": "ready_for_dispatch",
            "feedback_route": "success",
            "failure_scope": "device_workflow",
        }
    )


def _self_consistent_certificate(
    package: dict,
    *,
    certificate_version: str | None,
    contract_version: str,
    acceptance_scope: str,
) -> dict:
    certificate = {
        "accepted": True,
        "contract_version": contract_version,
        "research_plan_signature": "route-frozen",
        "target_materials": [],
        "reaction_route": [],
        "reagent_identity_and_order": [],
        "observation_points": [],
        "sample_control_matrix": [],
        "accepted_device_sample_control_matrix": [],
        "device_sample_ids": [],
        "semantic_analysis": [],
        "accepted_device_plan_signature": MODULE._stable_digest(
            package.get("device_plan", []), prefix="device_plan"
        ),
        "accepted_device_plan_contract_sha256": (
            MODULE.device_plan_contract_digest(package)
        ),
        "acceptance_scope": acceptance_scope,
        "device_snapshot_id": "snapshot",
        "device_truth_sha256": "truth",
    }
    if certificate_version is not None:
        certificate["certificate_version"] = certificate_version
    protected = MODULE.feasibility_certificate_protected_payload(certificate)
    certificate.update(
        {
            "protected_digest": MODULE._stable_digest(protected),
            "route_signature": "route-frozen",
            "sample_matrix_signature": MODULE._stable_digest(
                [], prefix="sample_matrix"
            ),
            "device_snapshot_signature": "truth",
        }
    )
    certificate["certificate_id"] = MODULE.feasibility_certificate_id(
        protected_digest=certificate["protected_digest"],
        device_snapshot_id="snapshot",
        device_truth_sha256="truth",
    )
    return certificate


def test_v2_acceptance_rejects_self_consistent_legacy_certificate() -> None:
    package = {"contract_version": "v2", "device_plan": []}
    certificate = _self_consistent_certificate(
        package,
        certificate_version=None,
        contract_version="v1",
        acceptance_scope="accepted_device_plan",
    )

    errors = MODULE._certificate_integrity_errors(certificate, package, {})

    assert "unsupported_certificate_version" in errors
    assert "certificate_contract_version_mismatch" in errors


def test_v2_success_rejects_route_only_certificate_scope() -> None:
    package = {"contract_version": "v2", "device_plan": []}
    certificate = _self_consistent_certificate(
        package,
        certificate_version=MODULE.FEASIBILITY_CERTIFICATE_VERSION,
        contract_version="v2",
        acceptance_scope="route_only_pending_device_plan_repair",
    )

    errors = MODULE._certificate_integrity_errors(certificate, package, {})

    assert "certificate_acceptance_scope_invalid_for_success" in errors

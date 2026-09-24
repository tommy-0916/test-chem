"""Shared feasibility-certificate contract helpers.

The Device agent and campaign orchestrator both consume this module so that
certificate integrity, accepted-plan identity, and successor lineage cannot
silently drift between the producer and the resume boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict


FEASIBILITY_CERTIFICATE_VERSION = "2.4"


# This is the accepted Device Plan contract, rather than every piece of
# transient runtime state attached to a plan.  In particular, quantity_audit,
# workflow_json, and dispatch results are deterministic products checked after
# certification and therefore do not belong to the accepted planning input.
DEVICE_PLAN_CONTRACT_DEFAULTS: Dict[str, Any] = {
    "feasibility": {},
    "macro_plan_summary": "",
    "sample_control_matrix": [],
    "device_self_check": {},
    "reagent_slot_plan": [],
    "container_plan": [],
    "quantity_adjustments": [],
    "quantity_requirement_dispositions": [],
    "batch_plan": [],
    "material_transitions": [],
    "material_ledger": {},
    "device_plan": [],
    "temporal_adaptations": [],
    "offline_handoffs": [],
    # These flags and the pending-review description alter whether an accepted
    # plan may proceed automatically.  They therefore belong to the signed
    # planning contract even though later runtime checks may also consult them.
    "requires_scientific_review": False,
    "quantity_contract_required": False,
    "pending_quantity_human_review": {},
}


CERTIFICATE_PROTECTED_DEFAULTS: Dict[str, Any] = {
    "certificate_version": FEASIBILITY_CERTIFICATE_VERSION,
    "contract_version": "",
    "research_plan_signature": "",
    "target_materials": [],
    "reaction_route": [],
    "reagent_identity_and_order": [],
    "observation_points": [],
    "sample_control_matrix": [],
    "accepted_device_sample_control_matrix": [],
    "device_sample_ids": [],
    "semantic_analysis": [],
    "accepted_device_plan_signature": "",
    "accepted_device_plan_contract_sha256": "",
    "acceptance_scope": "",
}


LEGACY_V23_PROTECTED_DEFAULTS: Dict[str, Any] = {
    "research_plan_signature": "",
    "target_materials": [],
    "reaction_route": [],
    "reagent_identity_and_order": [],
    "observation_points": [],
    "sample_control_matrix": [],
    "accepted_device_sample_control_matrix": [],
    "device_sample_ids": [],
    "semantic_analysis": [],
}


OPTIONAL_PROTECTED_FIELDS = (
    "external_return_contracts",
    "plan_audit_record_digest",
    "plan_audit_implementation_sha256",
    "supersedes_certificate_id",
    "repair_request_id",
    "repair_authority",
    "authorized_change_scope",
)


def stable_digest(value: Any, *, prefix: str = "sha256") -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()}"


def device_plan_contract_view(plan_result: Any) -> Dict[str, Any]:
    source = plan_result if isinstance(plan_result, dict) else {}
    return {
        key: copy.deepcopy(source.get(key, default))
        for key, default in DEVICE_PLAN_CONTRACT_DEFAULTS.items()
    }


def device_plan_contract_digest(plan_result: Any) -> str:
    return stable_digest(
        device_plan_contract_view(plan_result),
        prefix="device_plan_contract",
    )


def feasibility_certificate_protected_payload(
    certificate: Any,
) -> Dict[str, Any]:
    source = certificate if isinstance(certificate, dict) else {}
    if source.get("certificate_version") != FEASIBILITY_CERTIFICATE_VERSION:
        protected = {
            key: copy.deepcopy(source.get(key, default))
            for key, default in LEGACY_V23_PROTECTED_DEFAULTS.items()
        }
        if "contract_version" in source:
            protected["contract_version"] = copy.deepcopy(
                source["contract_version"]
            )
        if "external_return_contracts" in source:
            protected["external_return_contracts"] = copy.deepcopy(
                source["external_return_contracts"]
            )
        return protected
    protected = {
        key: copy.deepcopy(source.get(key, default))
        for key, default in CERTIFICATE_PROTECTED_DEFAULTS.items()
    }
    for key in OPTIONAL_PROTECTED_FIELDS:
        if key in source:
            protected[key] = copy.deepcopy(source[key])
    return protected


def feasibility_certificate_id(
    *,
    protected_digest: str,
    device_snapshot_id: str,
    device_truth_sha256: str,
) -> str:
    return stable_digest(
        {
            "protected_digest": protected_digest,
            "device_snapshot_id": device_snapshot_id,
            "device_truth_sha256": device_truth_sha256,
        },
        prefix="feasibility",
    )

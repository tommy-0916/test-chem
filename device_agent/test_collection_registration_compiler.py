"""Focused regressions for collect_same_material registration semantics.

A collect_same_material relationship is a Device-visible NO-OP: the compiler
accepts the registration, preserves its endpoints as package metadata, and
never turns it into a Device binding, a workstation action, a runtime
measurement obligation, a material transition, or a consumption event --
even though its quantity_basis is runtime_measurement_required.

Because the registration branch bypasses the generic endpoint-resolution
checks, it validates its own endpoints fail-closed: the referenced logical
container must resolve to exactly one declared collected_set container whose
member set equals the registered members, and the quantity basis must be
runtime_measurement_required.  Dangling containers, plain vials, mismatched
member sets, and allocated quantity bases are all rejected.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
for _path in (str(_HERE), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest

from material_relationship_compiler import (
    ALLOWED_EVENT_KINDS,
    MANUAL_SUPPORT_SCHEMA,
    MANUAL_SUPPORT_SCHEMA_VERSION,
    CANDIDATE_BINDING_DIGEST_SCOPE,
    RUNTIME_MEASUREMENT_SCHEMA,
    RUNTIME_QUANTITY_MODE,
    _canonical_digest,
    _expected_manual_support_record,
    compile_material_relationships,
    seal_relationship_binding_authority,
    candidate_binding_sha256,
)

RELATIONSHIP_ID = "rel-collect-1"
MEMBER_IDS = ["member-1", "member-2"]
COLLECTED_SET_ID = "collected-set-1"
SOURCE_OPERATION_REF = "op-collect-1"


def _member_input_port():
    return {
        "material_id": "material-A",
        "material_instance_id": "member-1",
        "material_origin": "external_inventory",
        "state": "powder",
        "quantity": {"mode": "exact", "semantic": "planned_target", "value": 1.0, "unit": "mg"},
    }


def _collect_registration(**overrides):
    relation = {
        "relation_id": RELATIONSHIP_ID,
        "event_kind": "collect_same_material",
        "input_material_instance_ids": list(MEMBER_IDS),
        "output_material_instance_ids": list(MEMBER_IDS),
        "logical_container_ids": [COLLECTED_SET_ID],
        "quantity_basis": RUNTIME_QUANTITY_MODE,
        "source_operation_ref": SOURCE_OPERATION_REF,
        "provenance": {"source": "research-v2-fixture"},
    }
    relation.update(overrides)
    return relation


def _macro_step(relation):
    return {
        "macro_step_id": "macro-1",
        "sample_id": "sample-1",
        "material_contract_status": {
            "material_inputs": "declared",
            "material_intermediates": "not_applicable",
            "material_outputs": "not_applicable",
            "logical_containers": "declared",
            "material_relations": "declared",
        },
        "material_inputs": [_member_input_port()],
        "material_intermediates": [],
        "material_outputs": [],
        "logical_containers": [
            {
                "logical_container_id": COLLECTED_SET_ID,
                "container_type": "collected_set",
                "member_material_instance_ids": list(MEMBER_IDS),
                "count": 1,
            }
        ],
        "operation_segments": [
            {
                "segment_id": SOURCE_OPERATION_REF,
                "material_effect": "observe_without_material_change",
                "source_operation_ref": SOURCE_OPERATION_REF,
                "provenance": {"source": "research-v2-fixture"},
            }
        ],
        "material_relations": [relation],
    }


def _authority(relation):
    return {"macro_steps": [_macro_step(relation)]}


def _candidate(device_plan=None):
    return {"device_plan": list(device_plan or [])}


def _codes(issues):
    return [issue.get("code") for issue in issues]


def _compile(candidate, authority, **kwargs):
    return compile_material_relationships(candidate, authority, **kwargs)


def test_collect_same_material_is_an_allowed_event_kind():
    assert "collect_same_material" in ALLOWED_EVENT_KINDS


def test_collect_registration_compiles_without_invalid_event_kind_issue():
    candidate = _candidate()
    updated, issues, applied = _compile(candidate, _authority(_collect_registration()))

    assert issues == []
    assert "invalid_material_event_kind" not in _codes(issues)
    assert applied == ["replace:material_relationship_graph"]
    manifest = updated["material_relationship_compiler"]
    assert manifest["construction_rule"] == "material-relationship-compiler/v1"
    assert manifest["relationship_ids"] == [RELATIONSHIP_ID]


def test_collect_registration_recorded_with_members_and_containers():
    updated, issues, _ = _compile(_candidate(), _authority(_collect_registration()))

    assert issues == []
    manifest = updated["material_relationship_compiler"]
    assert manifest["collection_registrations"] == [
        {
            "relationship_id": RELATIONSHIP_ID,
            "member_material_instance_ids": list(MEMBER_IDS),
            "logical_container_ids": [COLLECTED_SET_ID],
            "quantity_basis": RUNTIME_QUANTITY_MODE,
            "source_operation_ref": SOURCE_OPERATION_REF,
            "provenance": {"source": "research-v2-fixture"},
        }
    ]


def test_collect_registration_generates_no_measurements_or_device_actions():
    updated, issues, _ = _compile(_candidate(), _authority(_collect_registration()))

    assert issues == []
    # The registration's quantity_basis is runtime_measurement_required, yet
    # it must never produce a runtime-measurement obligation entry.
    obligations = updated["material_runtime_measurement_obligations"]
    assert obligations == []
    assert all(
        obligation.get("schema") != RUNTIME_MEASUREMENT_SCHEMA
        for obligation in obligations
    )
    assert not any(
        obligation.get("research_material_relationship_id") == RELATIONSHIP_ID
        for obligation in obligations
    )
    # Zero material transitions, consumption events, batches, ledger entries.
    assert updated["material_transitions"] == []
    assert updated["material_consumption_events"] == []
    assert updated["batch_plan"] == []
    assert updated["material_ledger"]["entries"] == []
    assert updated["material_relation_graph"]["edges"] == []
    # No Device binding record and no workstation action annotation.
    manifest = updated["material_relationship_compiler"]
    assert manifest["relationship_transition_ids"] == {}
    assert manifest["managed_transition_ids"] == []
    assert manifest["managed_consumption_event_ids"] == []
    assert manifest["managed_batch_ids"] == []
    assert manifest["managed_ledger_entry_ids"] == []
    assert manifest["managed_runtime_measurement_obligation_ids"] == []
    assert manifest["runtime_measurement_obligation_ids"] == []
    assert manifest["pending_runtime_measurement"] is False
    assert all(
        "material_event_kind" not in step for step in updated["device_plan"]
    )


def test_collect_registration_replay_is_idempotent():
    authority = _authority(_collect_registration())
    updated, issues, _ = _compile(_candidate(), authority)
    assert issues == []

    replayed, replay_issues, replay_applied = _compile(updated, authority)
    assert replay_issues == []
    assert replay_applied == []
    assert replayed == updated


def test_mismatched_member_sets_fail_closed_returning_original_candidate():
    relation = _collect_registration(
        output_material_instance_ids=["member-1", "member-3"]
    )
    candidate = _candidate()
    updated, issues, applied = _compile(candidate, _authority(relation))

    assert "collect_registration_endpoint_mismatch" in _codes(issues)
    assert applied == []
    assert updated == candidate
    assert updated is not candidate


def test_empty_member_set_rejected():
    relation = _collect_registration(
        input_material_instance_ids=[], output_material_instance_ids=[]
    )
    updated, issues, _ = _compile(_candidate(), _authority(relation))

    assert "collect_registration_endpoint_mismatch" in _codes(issues)


def test_allocations_fail_closed_returning_original_candidate():
    relation = _collect_registration(
        input_allocations=[
            {
                "material_instance_id": "member-1",
                "quantity": {"mode": "exact", "semantic": "planned_target", "value": 1.0, "unit": "mg"},
            }
        ]
    )
    candidate = _candidate()
    updated, issues, applied = _compile(candidate, _authority(relation))

    assert "collect_registration_with_allocations" in _codes(issues)
    assert applied == []
    assert updated == candidate
    assert updated is not candidate


def test_missing_collected_set_container_rejected():
    relation = _collect_registration(logical_container_ids=[])
    updated, issues, _ = _compile(_candidate(), _authority(relation))

    assert "collect_registration_missing_logical_container" in _codes(issues)


def _authority_with_containers(relation, containers):
    authority = _authority(relation)
    authority["macro_steps"][0]["logical_containers"] = containers
    return authority


def test_dangling_collected_set_container_rejected():
    relation = _collect_registration(logical_container_ids=["set-dangling"])
    candidate = _candidate()
    updated, issues, applied = _compile(candidate, _authority(relation))

    assert "collect_registration_unresolved_logical_container" in _codes(issues)
    assert applied == []
    assert updated == candidate
    assert updated is not candidate


def test_plain_vial_container_rejected():
    authority = _authority_with_containers(
        _collect_registration(),
        [
            {
                "logical_container_id": COLLECTED_SET_ID,
                "container_type": "vial",
                "count": 1,
            }
        ],
    )
    candidate = _candidate()
    updated, issues, applied = _compile(candidate, authority)

    assert "collect_registration_container_type_mismatch" in _codes(issues)
    assert applied == []
    assert updated == candidate
    assert updated is not candidate


def test_container_member_mismatch_rejected():
    authority = _authority_with_containers(
        _collect_registration(),
        [
            {
                "logical_container_id": COLLECTED_SET_ID,
                "container_type": "collected_set",
                "member_material_instance_ids": ["member-1", "member-9"],
                "count": 1,
            }
        ],
    )
    candidate = _candidate()
    updated, issues, applied = _compile(candidate, authority)

    assert "collect_registration_member_mismatch" in _codes(issues)
    assert applied == []
    assert updated == candidate
    assert updated is not candidate


def test_multiple_logical_containers_rejected():
    other = {
        "logical_container_id": "collected-set-2",
        "container_type": "collected_set",
        "member_material_instance_ids": list(MEMBER_IDS),
        "count": 1,
    }
    relation = _collect_registration(
        logical_container_ids=[COLLECTED_SET_ID, "collected-set-2"]
    )
    authority = _authority(relation)
    authority["macro_steps"][0]["logical_containers"].append(other)
    candidate = _candidate()
    updated, issues, applied = _compile(candidate, authority)

    assert "collect_registration_logical_container_not_unique" in _codes(issues)
    assert applied == []
    assert updated == candidate
    assert updated is not candidate


def test_non_runtime_quantity_basis_rejected():
    relation = _collect_registration(quantity_basis="whole_batch")
    candidate = _candidate()
    updated, issues, applied = _compile(candidate, _authority(relation))

    assert "collect_registration_quantity_basis_unsupported" in _codes(issues)
    assert applied == []
    assert updated == candidate
    assert updated is not candidate


def test_device_step_may_not_claim_collect_registration():
    candidate = _candidate(
        [
            {
                "plan_step": 1,
                "workstation": "collection station",
                "source_macro_steps": ["macro-1"],
                "research_material_relation_ids": [RELATIONSHIP_ID],
            }
        ]
    )
    updated, issues, applied = _compile(candidate, _authority(_collect_registration()))

    assert "candidate_binding_for_collect_registration" in _codes(issues)
    assert applied == []
    assert updated == candidate
    assert updated is not candidate


def test_collect_registration_may_not_have_a_binding_record():
    authority = _authority(_collect_registration())
    candidate = _candidate()
    truth = "e" * 64
    document = {
        "operation_binding_review_schema": MANUAL_SUPPORT_SCHEMA,
        "operation_binding_review_schema_version": MANUAL_SUPPORT_SCHEMA_VERSION,
        "operation_binding_review_authoring_mode": "manual_evidence_bound",
        "operation_binding_review_automation_claim": False,
        "operation_binding_review_research_authority_sha256": _canonical_digest(authority),
        "operation_binding_review_candidate_digest_scope": CANDIDATE_BINDING_DIGEST_SCOPE,
        "operation_binding_review_candidate_sha256": candidate_binding_sha256(candidate),
        "operation_binding_review_workstation_truth_sha256": truth,
        "operation_binding_reviews": {},
    }
    with pytest.raises(ValueError, match="may not have a Device binding record"):
        seal_relationship_binding_authority(
            candidate,
            authority,
            {RELATIONSHIP_ID: {"plan_step": 1, "binding_status": "bound"}},
            authoring_mode="manual_evidence_bound",
            automation_claim=False,
            evidence_sources={
                "research_authority": "research.json",
                "device_candidate": "candidate.json",
                "workstation_truth": "truth.json",
                "manual_support": "review.json",
            },
            workstation_truth_digest=truth,
            resolve_workstation=lambda workstation: workstation,
            manual_support_document=document,
        )


def test_none_relationship_semantics_unchanged():
    with_endpoints = _collect_registration(
        relation_id="rel-none-1",
        event_kind="none",
        input_material_instance_ids=["member-1"],
        output_material_instance_ids=["member-1"],
        logical_container_ids=[COLLECTED_SET_ID],
    )
    updated, issues, _ = _compile(_candidate(), _authority(with_endpoints))
    assert "none_relationship_has_endpoints" in _codes(issues)
    assert "material_relationship_compiler" not in updated

    plain_none = _collect_registration(
        relation_id="rel-none-2",
        event_kind="none",
        input_material_instance_ids=[],
        output_material_instance_ids=[],
        logical_container_ids=[],
    )
    updated, issues, _ = _compile(_candidate(), _authority(plain_none))
    assert issues == []
    manifest = updated["material_relationship_compiler"]
    assert manifest["collection_registrations"] == []
    assert manifest["relationship_transition_ids"] == {}
    assert manifest["relationship_ids"] == ["rel-none-2"]


def test_whole_batch_process_relation_still_binds_and_compiles():
    # Mainline regression: an evidence-bound process_same_material relation
    # still produces transitions, batches, ledger entries and a Device-step
    # material_event_kind, and stays untouched by registration handling.
    macro = {
        "macro_step_id": "macro-1",
        "sample_id": "sample-1",
        "material_contract_status": {
            "material_inputs": "declared",
            "material_intermediates": "not_applicable",
            "material_outputs": "declared",
            "logical_containers": "declared",
            "material_relations": "declared",
        },
        "material_inputs": [
            {
                "material_id": "material-A",
                "material_instance_id": "in-1",
                "material_origin": "external_inventory",
                "state": "powder",
                "quantity": {"mode": "all_available", "semantic": "whole_batch_unspecified"},
                "logical_container_id": "vial-in",
            }
        ],
        "material_intermediates": [],
        "material_outputs": [
            {
                "material_id": "material-A",
                "material_instance_id": "out-1",
                "state": "solution",
                "quantity": {"mode": "all_available", "semantic": "whole_batch_unspecified"},
                "logical_container_id": "vial-out",
            }
        ],
        "logical_containers": [
            {"logical_container_id": "vial-in", "count": 1},
            {"logical_container_id": "vial-out", "count": 1},
        ],
        "operation_segments": [
            {
                "segment_id": "op-1",
                "material_effect": "transform_material",
                "source_operation_ref": "op-1",
                "provenance": {"source": "research-v2-fixture"},
            }
        ],
        "material_relations": [
            {
                "relation_id": "rel-1",
                "event_kind": "process_same_material",
                "input_material_instance_ids": ["in-1"],
                "output_material_instance_ids": ["out-1"],
                "logical_container_ids": ["vial-in", "vial-out"],
                "quantity_basis": "whole_batch",
                "source_operation_ref": "op-1",
                "provenance": {"source": "research-v2-fixture"},
            }
        ],
    }
    authority = {"macro_steps": [macro]}
    candidate = _candidate(
        [
            {
                "plan_step": 1,
                "workstation": "station-a",
                "operation_intent": "dissolve",
                "containers": {"容器编号": [11]},
                "source_macro_steps": ["macro-1"],
            }
        ]
    )
    truth = "e" * 64
    step = candidate["device_plan"][0]
    review = _expected_manual_support_record(
        relationship_id="rel-1",
        plan_step=1,
        source_operation_ref="op-1",
        operation_segment=macro["operation_segments"][0],
        step=step,
        resolved_workstation="station-a",
        workstation_truth_digest=truth,
    )
    document = {
        "operation_binding_review_schema": MANUAL_SUPPORT_SCHEMA,
        "operation_binding_review_schema_version": MANUAL_SUPPORT_SCHEMA_VERSION,
        "operation_binding_review_authoring_mode": "manual_evidence_bound",
        "operation_binding_review_automation_claim": False,
        "operation_binding_review_research_authority_sha256": _canonical_digest(authority),
        "operation_binding_review_candidate_digest_scope": CANDIDATE_BINDING_DIGEST_SCOPE,
        "operation_binding_review_candidate_sha256": candidate_binding_sha256(candidate),
        "operation_binding_review_workstation_truth_sha256": truth,
        "operation_binding_reviews": {"rel-1": review},
    }
    sealed = seal_relationship_binding_authority(
        candidate,
        authority,
        {
            "rel-1": {
                "plan_step": 1,
                "binding_status": "bound",
                "logical_container_bindings": {"vial-in": 11, "vial-out": 11},
                "operation_evidence": {
                    "support_evidence_ref": "review.json#manual-binding:rel-1"
                },
            }
        },
        authoring_mode="manual_evidence_bound",
        automation_claim=False,
        evidence_sources={
            "research_authority": "research.json",
            "device_candidate": "candidate.json",
            "workstation_truth": "truth.json",
            "manual_support": "review.json",
        },
        workstation_truth_digest=truth,
        resolve_workstation=lambda workstation: workstation,
        manual_support_document=document,
    )

    updated, issues, applied = compile_material_relationships(
        candidate,
        authority,
        relationship_bindings=sealed,
        resolve_workstation=lambda workstation: workstation,
        workstation_truth_digest=truth,
    )

    assert issues == []
    assert applied == ["replace:material_relationship_graph", "bind:relationship:rel-1"]
    assert len(updated["material_transitions"]) == 1
    assert len(updated["batch_plan"]) == 2
    assert len(updated["material_ledger"]["entries"]) == 2
    assert len(updated["material_consumption_events"]) == 1
    assert updated["device_plan"][0]["material_event_kind"] == "process_same_material"
    manifest = updated["material_relationship_compiler"]
    assert manifest["collection_registrations"] == []
    assert manifest["pending_runtime_measurement"] is False

    replayed, replay_issues, replay_applied = compile_material_relationships(
        updated,
        authority,
        relationship_bindings=sealed,
        resolve_workstation=lambda workstation: workstation,
        workstation_truth_digest=truth,
    )
    assert replay_issues == []
    assert replay_applied == []
    assert replayed == updated


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))

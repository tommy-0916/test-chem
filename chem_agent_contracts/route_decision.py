"""Deterministic, fail-closed policy for choosing a route *for planning*.

The input candidates are proposals.  A trusted caller must obtain each
validation receipt from the existing Research evidence/science checks and the
current workstation contracts.  This module never interprets an LLM's own
``scientific_status`` or ``device_status`` as authority.  Final Research
publication and Device dispatch gates still run after macro-plan generation.
"""

from __future__ import annotations

from collections import Counter
import re
from typing import Callable, Dict, List, Literal, Optional, Sequence

from pydantic import Field, field_validator, model_validator

from .route_candidate import (
    ExperimentalGroupScopeV1,
    RouteCandidateV1,
    RouteGoalV1,
    RouteFieldEvidenceV1,
    RouteSignatureV1,
)
from .route_field_basis import (
    classify_route_field_basis, is_material_port_state_path,
    output_state_parent_role_issue,
    state_source_locally_attributed, verify_controlled_state_mapping,
)
from .v2 import (
    ScientificCompletenessV2,
    StrictModel,
    canonical_digest,
    evidence_contains_exact_quantity,
)


SELECTION_POLICY_V1 = "route_decision_v1"
_GENERATED_MATERIAL_FIELD = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\."
    r"(material_inputs|material_intermediates|material_outputs)"
    r"\[(0|[1-9][0-9]*)\]\."
    r"(material_id|material_instance_id)\Z"
)
_QUANTITY_REQUIREMENT_MATERIAL_ID = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\."
    r"quantity_requirements\[(0|[1-9][0-9]*)\]\.material_id\Z"
)


class DevicePreflightV1(StrictModel):
    """Candidate-level coverage, never a final Device executable verdict."""

    status: Literal["preflight_supported", "blocked", "unknown"] = "unknown"
    missing_capabilities: List[str] = Field(default_factory=list)
    unresolved_capabilities: List[str] = Field(default_factory=list)
    checked_capabilities: List[str] = Field(default_factory=list)
    snapshot_id: str = ""
    reasons: List[str] = Field(default_factory=list)


class RouteValidationReceiptV1(StrictModel):
    """Result of trusted evaluators, bound to the exact candidate payload.

    These receipts must be produced inside Chem-Agent, never deserialized from
    model output.  ``source_scope_verified`` means an upstream verifier checked
    the experimental-group boundary and excerpts against source material.
    """

    route_id: str = Field(min_length=1)
    candidate_digest: str = Field(pattern=r"^sha256_[0-9a-f]{64}$")
    source_scope_verified: bool = False
    source_document_kind: Literal["", "primary_paper", "supporting_information"] = ""
    source_identity_doi: str = ""
    source_attestation_digest: str = Field(
        default="", pattern=r"^(?:|sha256_[0-9a-f]{64})$"
    )
    source_route_signature: Optional[RouteSignatureV1] = None
    source_route_signature_review_digest: str = Field(
        default="", pattern=r"^(?:|sha256_[0-9a-f]{64})$"
    )
    verified_evidence_ids: List[str] = Field(default_factory=list)
    verified_field_paths: List[str] = Field(default_factory=list)
    audited_field_paths: List[str] = Field(default_factory=list)
    verified_runtime_resolution_fields: List[str] = Field(default_factory=list)
    verified_convention_field_paths: List[str] = Field(default_factory=list)
    verified_graph_step_ids: List[str] = Field(default_factory=list)
    scientific_completeness: Optional[ScientificCompletenessV2] = None
    scientific_gate_issues: List[str] = Field(default_factory=list)
    device_preflight: DevicePreflightV1 = Field(default_factory=DevicePreflightV1)
    route_adaptation_count: int = Field(default=0, ge=0)
    native_device_support: bool = False
    derived_scaled_parameter_count: int = Field(default=0, ge=0)


class RouteCandidateDecisionV1(StrictModel):
    route_id: str = Field(min_length=1)
    candidate: RouteCandidateV1
    candidate_digest: str = Field(pattern=r"^sha256_[0-9a-f]{64}$")
    validation: Optional[RouteValidationReceiptV1] = None
    validation_receipt_digest: str = ""
    status: Literal[
        "selected_for_planning", "admissible", "rejected", "unresolved"
    ]
    scientific_status: Literal["admissible", "rejected", "unresolved"]
    device_status: Literal["preflight_supported", "blocked", "unknown"]
    reasons: List[str] = Field(default_factory=list)
    priority: List[int] = Field(default_factory=list)


class RouteDecisionV1(StrictModel):
    schema_version: Literal["route-decision/v1"] = "route-decision/v1"
    decision_id: str = Field(pattern=r"^sha256_[0-9a-f]{64}$")
    goal_id: str = Field(min_length=1)
    goal: RouteGoalV1
    candidates: List[RouteCandidateDecisionV1]
    selected_route_id: Optional[str] = None
    status: Literal["selected_for_planning", "unresolved", "needs_route_choice"]
    selection_policy: Literal["route_decision_v1"] = SELECTION_POLICY_V1
    evidence_snapshot_hash: str = Field(pattern=r"^sha256_[0-9a-f]{64}$")
    device_contract_snapshot_hash: str = ""
    decision_reasons: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_decision_integrity(self) -> "RouteDecisionV1":
        if self.goal_id != self.goal.goal_id:
            raise ValueError("goal_id must match the embedded goal")
        route_ids = [item.route_id for item in self.candidates]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("candidate route IDs must be unique")
        for item in self.candidates:
            if item.route_id != item.candidate.route_id:
                raise ValueError("candidate record route ID mismatch")
            if item.candidate_digest != canonical_digest(item.candidate):
                raise ValueError("candidate digest mismatch")
            if item.validation is not None and (
                item.validation.route_id != item.route_id
                or item.validation.candidate_digest != canonical_digest(item.candidate)
            ):
                raise ValueError("candidate validation receipt digest mismatch")
            if item.validation_receipt_digest != (
                canonical_digest(item.validation) if item.validation is not None else ""
            ):
                raise ValueError("validation receipt digest mismatch")
        selected = [
            item for item in self.candidates if item.status == "selected_for_planning"
        ]
        if self.status == "selected_for_planning":
            if len(selected) != 1 or self.selected_route_id != selected[0].route_id:
                raise ValueError("selected route must identify exactly one selected candidate")
        elif selected or self.selected_route_id is not None:
            raise ValueError("unresolved/choice-needed decision cannot contain a selected route")
        expected = canonical_digest(self.model_dump(mode="json", exclude={"decision_id"}))
        if self.decision_id != expected:
            raise ValueError("decision_id does not match decision contents")
        return self


class RouteSearchBudgetV1(StrictModel):
    max_search_rounds: int = Field(default=2, ge=0)
    max_candidate_papers: int = Field(default=20, ge=0)
    max_queries_per_missing_field: int = Field(default=2, ge=0)


class RouteSearchRoundV1(StrictModel):
    queries_by_field: Dict[str, int] = Field(default_factory=dict)
    candidate_papers_seen: int = Field(default=0, ge=0)
    new_verified_fact_ids: List[str] = Field(default_factory=list)

    @field_validator("queries_by_field")
    @classmethod
    def require_nonnegative_query_counts(cls, value: Dict[str, int]) -> Dict[str, int]:
        if any(not key.strip() or count < 0 for key, count in value.items()):
            raise ValueError("query counts require nonempty fields and nonnegative values")
        return value

    @field_validator("new_verified_fact_ids")
    @classmethod
    def require_unique_verified_fact_ids(cls, value: List[str]) -> List[str]:
        if any(not item.strip() for item in value) or len(value) != len(set(value)):
            raise ValueError("verified fact IDs must be unique and nonempty")
        return value


def targeted_search_status(
    budget: RouteSearchBudgetV1,
    completed_rounds: Sequence[RouteSearchRoundV1],
    missing_required_fields: Sequence[str],
) -> Literal[
    "continue", "no_missing_fields", "no_new_verified_facts", "round_budget_exhausted",
    "paper_budget_exhausted", "query_budget_exhausted"
]:
    """Bound a caller's route-specific evidence search without doing I/O."""

    if not missing_required_fields:
        return "no_missing_fields"
    if completed_rounds:
        previously_verified = {
            fact_id
            for round_ in completed_rounds[:-1]
            for fact_id in round_.new_verified_fact_ids
        }
        if not set(completed_rounds[-1].new_verified_fact_ids) - previously_verified:
            return "no_new_verified_facts"
    if len(completed_rounds) >= budget.max_search_rounds:
        return "round_budget_exhausted"
    if sum(item.candidate_papers_seen for item in completed_rounds) >= budget.max_candidate_papers:
        return "paper_budget_exhausted"
    query_counts: Counter[str] = Counter()
    for round_ in completed_rounds:
        query_counts.update(round_.queries_by_field)
    if all(
        query_counts[field] >= budget.max_queries_per_missing_field
        for field in set(missing_required_fields)
    ):
        return "query_budget_exhausted"
    return "continue"


def _field_issue(
    field: RouteFieldEvidenceV1,
    candidate: RouteCandidateV1,
    receipt: RouteValidationReceiptV1,
) -> Optional[str]:
    """Check the structural binding; source authenticity is receipt-owned."""

    if classify_route_field_basis(field.field_path) == "generated_id":
        return "generated_id_paper_fact_forbidden"

    if field.status == "unsupported":
        return "required_unsupported" if field.required else None
    if field.status == "unknown":
        return "field_evidence_unknown" if field.required else None
    if field.status == "runtime_pending":
        if field.required and not field.resolution_path.strip():
            return "runtime_resolution_path_missing"
        if field.required and field.field_path not in receipt.verified_runtime_resolution_fields:
            return "runtime_resolution_unverified"
        return None
    provenance = field.provenance
    if provenance is None:
        return "supported_field_provenance_missing"
    if field.value is None:
        return "supported_field_value_missing"
    if is_material_port_state_path(field.field_path):
        match = re.fullmatch(
            r"material_graph\[([0-9]+)\]\."
            r"(material_inputs|material_intermediates|material_outputs)"
            r"\[([0-9]+)\]\.state", field.field_path,
        )
        if match is None or int(match.group(1)) >= len(candidate.material_graph):
            return "semantic_binding_pending"
        ports = getattr(candidate.material_graph[int(match.group(1))], match.group(2))
        if int(match.group(3)) >= len(ports):
            return "semantic_binding_pending"
        record = field.controlled_mapping
        mapping_issue = verify_controlled_state_mapping(
            field.field_path, field.value, ports[int(match.group(3))].state,
            record.model_dump(mode="json") if record is not None else None,
        )
        if mapping_issue:
            return mapping_issue
        if (provenance.kind == "paper"
                and output_state_parent_role_issue(
                    field.field_path, candidate.material_graph,
                    field.value, provenance.excerpt,
                )):
            return "parent_state_not_child_evidence"
        if (provenance.kind == "paper"
                and not state_source_locally_attributed(
                    field.value, provenance.excerpt,
                    ports[int(match.group(3))].name,
                )):
            return "semantic_binding_pending"
    elif field.controlled_mapping is not None:
        return "semantic_binding_pending"
    if field.unit and (isinstance(field.value, bool) or not isinstance(field.value, (int, float))):
        return "quantity_value_not_numeric"
    if provenance.kind == "paper":
        if provenance.evidence_class != "paper_explicit":
            return "paper_evidence_class_mismatch"
        if not _same_experimental_group(field.source_scope, candidate.source_scope):
            return "experimental_group_mixing"
        if not field.source_scope or not field.source_scope.locator.strip():
            return "paper_field_locator_missing"
        if not re.fullmatch(r"sha256_[0-9a-f]{64}", field.source_scope.source_digest):
            return "paper_field_digest_missing"
        if field.field_path not in receipt.verified_field_paths:
            return "paper_field_source_unverified"
        if not field.evidence_id or field.evidence_id not in receipt.verified_evidence_ids:
            return "paper_evidence_unverified"
        item = next(
            (item for item in candidate.evidence_bundle if item.evidence_id == field.evidence_id),
            None,
        )
        if item is None or not provenance.excerpt.strip():
            return "paper_excerpt_missing"
        if provenance.reference != field.evidence_id:
            return "paper_reference_mismatch"
        if provenance.excerpt not in item.excerpt:
            return "paper_excerpt_bundle_mismatch"
        item_index = next(
            index for index, evidence in enumerate(candidate.evidence_bundle)
            if evidence.evidence_id == field.evidence_id
        )
        if provenance.source_path != f"evidence_bundle.items[{item_index}].excerpt":
            return "paper_source_path_mismatch"
        # The experimental-group scope hashes original source bytes; V2
        # provenance hashes the published excerpt field. These are distinct
        # identities and must not be compared with each other.
        if provenance.source_digest != canonical_digest(item.excerpt):
            return "paper_excerpt_digest_mismatch"
        if isinstance(field.value, (int, float)) and not isinstance(field.value, bool):
            if not evidence_contains_exact_quantity(provenance.excerpt, field.value, field.unit):
                return "paper_quantity_excerpt_mismatch"
        return None
    if provenance.kind == "agent_inferred":
        if provenance.evidence_class != "chemistry_convention" or not provenance.inference_rule:
            return "agent_inferred_not_convention_backed"
        if field.field_path not in receipt.verified_convention_field_paths:
            return "convention_rule_not_verified"
        if _contains_numeric_content(field.value):
            return "convention_numeric_generation_forbidden"
        return None
    if provenance.kind == "runtime":
        return "runtime_fact_not_pre_run_evidence"
    if provenance.kind == "device_skill":
        return "device_sop_not_synthesis_evidence"
    return "unsupported_provenance_kind"


def _contains_numeric_content(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return bool(re.search(r"\d", value))
    if isinstance(value, dict):
        return any(
            _contains_numeric_content(key) or _contains_numeric_content(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_numeric_content(item) for item in value)
    return False


def _same_experimental_group(
    left: Optional[ExperimentalGroupScopeV1],
    right: Optional[ExperimentalGroupScopeV1],
) -> bool:
    return bool(
        left is not None and right is not None
        and left.paper_id == right.paper_id
        and left.experimental_group_id == right.experimental_group_id
    )


def _graph_material_port(
    candidate: RouteCandidateV1, field_path: str,
):
    """Resolve a structural ID path to its step and port, if it exists."""
    match = _GENERATED_MATERIAL_FIELD.fullmatch(field_path)
    if match is None:
        return None
    step_index, collection, port_index, field_name = match.groups()
    if int(step_index) >= len(candidate.material_graph):
        return None
    step = candidate.material_graph[int(step_index)]
    ports = getattr(step, collection)
    if int(port_index) >= len(ports):
        return None
    return step, ports[int(port_index)], field_name


def _port_name_evidence_issue(
    candidate: RouteCandidateV1,
    receipt: Optional[RouteValidationReceiptV1],
    field_by_path: Dict[str, RouteFieldEvidenceV1],
    name_path: str,
    port,
) -> str:
    """Bind an internal port to its quoted material name and trusted receipt."""
    field = field_by_path.get(name_path)
    if field is None or not field.required:
        return "name_fact_missing"
    if field.status == "unsupported":
        return "required_unsupported"
    if field.status != "supported":
        return "name_fact_missing"
    if field.value != port.name or field.provenance != port.provenance:
        return "name_fact_graph_mismatch"
    provenance = field.provenance
    if provenance is None or provenance.kind != "paper":
        return "name_fact_paper_basis_missing"
    literal = re.sub(r"\s+", " ", port.name.strip())
    excerpt = re.sub(r"\s+", " ", provenance.excerpt)
    if not literal or re.search(
        rf"(?<!\w){re.escape(literal)}(?!\w)", excerpt,
    ) is None:
        return "semantic_binding_pending"
    if receipt is None or receipt.route_id != candidate.route_id or (
        receipt.candidate_digest != canonical_digest(candidate)
    ):
        return "name_fact_receipt_missing"
    if not receipt.source_scope_verified or (
        name_path not in receipt.verified_field_paths
        or name_path not in receipt.audited_field_paths
    ):
        return "name_fact_unverified"
    return _field_issue(field, candidate, receipt) or ""


def _locked_scope_matches(
    candidate: Optional[ExperimentalGroupScopeV1],
    locked: Optional[ExperimentalGroupScopeV1],
) -> bool:
    if not _same_experimental_group(candidate, locked):
        return False
    assert candidate is not None and locked is not None
    return all(
        not getattr(locked, field_name) or
        getattr(candidate, field_name) == getattr(locked, field_name)
        for field_name in ("section", "locator", "source_digest")
    )


def route_signature_mismatches(
    proposed: RouteSignatureV1,
    verified_source: RouteSignatureV1,
) -> List[str]:
    """Compare route structure exactly; family labels confer no equivalence."""

    mismatches: List[str] = []
    if proposed.route_family != verified_source.route_family:
        mismatches.append("route_family")
    if proposed.target_transformation != verified_source.target_transformation:
        mismatches.append("target_transformation")
    if Counter(proposed.precursor_roles) != Counter(verified_source.precursor_roles):
        mismatches.append("precursor_roles")
    if Counter(proposed.reagent_roles) != Counter(verified_source.reagent_roles):
        mismatches.append("reagent_roles")
    if proposed.operations != verified_source.operations:
        mismatches.append("operation_sequence")
    if Counter(proposed.control_modes) != Counter(verified_source.control_modes):
        mismatches.append("control_modes")
    if proposed.phase_transitions != verified_source.phase_transitions:
        mismatches.append("phase_transitions")
    if proposed.endpoint_state != verified_source.endpoint_state:
        mismatches.append("endpoint_state")
    return mismatches


def _candidate_assessment(
    goal: RouteGoalV1,
    candidate: RouteCandidateV1,
    receipt: Optional[RouteValidationReceiptV1],
) -> RouteCandidateDecisionV1:
    science_hard: set[str] = set()
    science_pending: set[str] = set()
    device_hard: set[str] = set()
    device_pending: set[str] = set()
    device_status: Literal["preflight_supported", "blocked", "unknown"] = "unknown"
    field_by_path = {field.field_path: field for field in candidate.evidence_matrix}

    if candidate.target.material.casefold() != goal.target.material.casefold():
        science_hard.add("target_mismatch")
    if candidate.target.desired_state != goal.target.desired_state:
        science_hard.add("target_state_mismatch")
    if candidate.route_signature.endpoint_state == "unknown":
        science_pending.add("endpoint_state_unknown")
    elif candidate.route_signature.endpoint_state != goal.target.desired_state:
        science_hard.add("endpoint_state_mismatch")
    if goal.constraint == "locked_family" and candidate.route_signature.route_family != goal.locked_family:
        science_hard.add("locked_route_family_mismatch")
    if goal.constraint == "locked_experimental_group" and not _locked_scope_matches(candidate.source_scope, goal.locked_scope):
        science_hard.add("locked_experimental_group_mismatch")
    if candidate.origin == "hypothesis":
        science_hard.add("hypothesis_without_primary_experimental_group")
    if candidate.source_scope is None:
        science_pending.add("experimental_group_source_missing")
    elif not candidate.source_scope.locator or not re.fullmatch(r"sha256_[0-9a-f]{64}", candidate.source_scope.source_digest):
        science_pending.add("source_locator_or_digest_missing")
    if not candidate.material_graph:
        science_pending.add("material_graph_not_evaluated")
    elif (
        not candidate.material_graph[0].material_inputs
        or not candidate.material_graph[-1].material_outputs
    ):
        science_pending.add("material_graph_root_or_endpoint_missing")
    else:
        final_index = len(candidate.material_graph) - 1
        endpoint_ports = [
            (port_index, output)
            for port_index, output in enumerate(
                candidate.material_graph[-1].material_outputs
            )
            if output.state == goal.target.desired_state
            and output.name.strip().casefold() == goal.target.material.strip().casefold()
        ]
        if not endpoint_ports:
            science_hard.add("material_graph_endpoint_mismatch")
        else:
            endpoint_issues = [
                _port_name_evidence_issue(
                    candidate, receipt, field_by_path,
                    f"material_graph[{final_index}].material_outputs[{port_index}].name",
                    output,
                )
                for port_index, output in endpoint_ports
            ]
            if all(endpoint_issues):
                if any(issue in {
                    "name_fact_graph_mismatch", "name_fact_paper_basis_missing",
                    "name_fact_literal_missing", "required_unsupported",
                } for issue in endpoint_issues):
                    science_hard.add("material_graph_endpoint_name_basis_invalid")
                else:
                    science_pending.add("material_graph_endpoint_name_unverified")
    if not candidate.evidence_matrix:
        science_pending.add("evidence_matrix_missing")
    if not goal.required_fields:
        science_pending.add("required_field_specification_missing")
    if not candidate.route_signature.operations:
        science_pending.add("route_operations_missing")
    if not candidate.required_capabilities:
        device_pending.add("capability_requirements_missing")

    for field_path in goal.required_fields:
        if classify_route_field_basis(field_path) == "generated_id":
            if not _GENERATED_MATERIAL_FIELD.fullmatch(field_path):
                quantity_match = _QUANTITY_REQUIREMENT_MATERIAL_ID.fullmatch(
                    field_path
                )
                if quantity_match is None:
                    science_hard.add(
                        "required_generated_id_structure_unsupported:" + field_path
                    )
                    continue
                step_index, requirement_index = map(int, quantity_match.groups())
                if step_index >= len(candidate.material_graph):
                    science_hard.add("required_generated_id_reference_invalid:" + field_path)
                    continue
                graph_step = candidate.material_graph[step_index]
                if requirement_index >= len(graph_step.quantity_requirements):
                    science_hard.add("required_generated_id_reference_invalid:" + field_path)
                    continue
                requirement = graph_step.quantity_requirements[requirement_index]
                if not isinstance(requirement, dict):
                    science_hard.add("required_generated_id_reference_invalid:" + field_path)
                    continue
                referenced = requirement.get("material_id")
                material_name = requirement.get("material")
                if not isinstance(referenced, str) or not referenced.strip() or (
                    not isinstance(material_name, str) or not material_name.strip()
                ):
                    science_hard.add("required_generated_id_reference_invalid:" + field_path)
                    continue
                matching_ports = [
                    (collection_name, port_index, port)
                    for collection_name in (
                        "material_inputs", "material_intermediates",
                        "material_outputs",
                    )
                    for port_index, port in enumerate(
                        getattr(graph_step, collection_name)
                    )
                    if port.material_id == referenced
                    and port.name.strip().casefold() == material_name.strip().casefold()
                ]
                if not matching_ports:
                    science_hard.add("required_generated_id_reference_invalid:" + field_path)
                    continue
                name_issues = [
                    _port_name_evidence_issue(
                        candidate, receipt, field_by_path,
                        f"material_graph[{step_index}].{collection_name}"
                        f"[{port_index}].name", port,
                    )
                    for collection_name, port_index, port in matching_ports
                ]
                if all(name_issues):
                    if any(issue in {
                        "name_fact_graph_mismatch", "name_fact_paper_basis_missing",
                        "name_fact_literal_missing", "required_unsupported",
                    } for issue in name_issues):
                        science_hard.add(
                            "required_generated_id_name_basis_invalid:" + field_path
                        )
                    else:
                        science_pending.add(
                            "required_generated_id_name_unverified:" + field_path
                        )
                continue
            binding = _graph_material_port(candidate, field_path)
            if binding is None:
                science_pending.add("required_generated_id_path_missing:" + field_path)
                continue
            step, port, field_name = binding
            identity = getattr(port, field_name)
            if not isinstance(identity, str) or not identity.strip():
                science_hard.add("required_generated_id_invalid:" + field_path)
                continue
            if field_name == "material_id":
                names = {
                    other.name.strip().casefold()
                    for collection in (
                        step.material_inputs,
                        step.material_intermediates,
                        step.material_outputs,
                    )
                    for other in collection
                    if other.material_id == identity
                }
                if len(names) != 1:
                    science_hard.add("required_generated_id_identity_conflict:" + field_path)
                for graph_step in candidate.material_graph:
                    declared = {
                        item.material_id for collection in (
                            graph_step.material_inputs,
                            graph_step.material_intermediates,
                            graph_step.material_outputs,
                        ) for item in collection
                    }
                    if any(
                        isinstance(requirement, dict)
                        and str(requirement.get("material_id") or "").strip()
                        not in declared
                        for requirement in graph_step.quantity_requirements
                    ):
                        science_hard.add("required_generated_id_reference_invalid:" + field_path)
            name_path = field_path.rsplit(".", 1)[0] + ".name"
            name_issue = _port_name_evidence_issue(
                candidate, receipt, field_by_path, name_path, port,
            )
            if name_issue:
                if name_issue in {
                    "name_fact_graph_mismatch", "name_fact_paper_basis_missing",
                    "name_fact_literal_missing", "required_unsupported",
                }:
                    science_hard.add("required_generated_id_name_basis_invalid:" + field_path)
                else:
                    science_pending.add("required_generated_id_name_unverified:" + field_path)
            continue
        field = field_by_path.get(field_path)
        if field is None or not field.required:
            science_pending.add("required_field_not_covered:" + field_path)
    if receipt is None:
        science_pending.add("validation_receipt_missing")
        device_pending.add("validation_receipt_missing")
    elif receipt.route_id != candidate.route_id or receipt.candidate_digest != canonical_digest(candidate):
        science_pending.add("validation_receipt_candidate_mismatch")
        device_pending.add("validation_receipt_candidate_mismatch")
    else:
        device_status = receipt.device_preflight.status
        if not receipt.source_scope_verified:
            science_pending.add("source_scope_not_verified")
        if receipt.source_route_signature is None:
            science_pending.add("route_signature_unverified")
        else:
            if (
                goal.constraint == "locked_family"
                and receipt.source_route_signature.route_family != goal.locked_family
            ):
                science_hard.add("locked_source_route_family_mismatch")
            for component in route_signature_mismatches(
                candidate.route_signature, receipt.source_route_signature
            ):
                science_hard.add("route_signature_mismatch:" + component)
        for step in candidate.material_graph:
            if step.macro_step_id not in receipt.verified_graph_step_ids:
                science_pending.add("material_graph_step_unverified:" + step.macro_step_id)
        for field_path in goal.required_fields:
            if classify_route_field_basis(field_path) == "generated_id":
                continue
            if field_path not in receipt.audited_field_paths:
                science_pending.add("required_field_not_audited:" + field_path)
        for field in candidate.evidence_matrix:
            issue = _field_issue(field, candidate, receipt)
            if issue:
                if issue in {
                    "required_unsupported", "experimental_group_mixing",
                    "convention_numeric_generation_forbidden", "device_sop_not_synthesis_evidence",
                    "agent_inferred_not_convention_backed", "unsupported_provenance_kind",
                    "generated_id_paper_fact_forbidden",
                }:
                    science_hard.add(issue + ":" + field.field_path)
                else:
                    science_pending.add(issue + ":" + field.field_path)
        for issue in receipt.scientific_gate_issues:
            science_hard.add("scientific_gate_issue:" + issue)
        audit = receipt.scientific_completeness
        if audit is None:
            science_pending.add("scientific_completeness_not_evaluated")
        else:
            if any(item.required for item in audit.unsupported):
                science_hard.add("required_unsupported_audit")
            if audit.unresolved_runtime_dependencies:
                science_hard.add("unresolved_runtime_dependency")
            if audit.broken_material_lineage:
                science_hard.add("broken_material_lineage")
            if any(not str(risk.equivalence_evidence or "").strip()
                   for risk in audit.scientific_fidelity_risks):
                science_hard.add("scientific_fidelity_risk_unresolved")
        if device_status == "blocked":
            device_hard.add("required_device_capability_missing")
        elif device_status != "preflight_supported":
            device_pending.add("device_preflight_unknown")
        if receipt.device_preflight.missing_capabilities:
            device_hard.add("required_device_capability_missing")
            device_status = "blocked"
        if receipt.device_preflight.unresolved_capabilities:
            device_pending.add("required_device_capability_unresolved")
        if not set(candidate.required_capabilities).issubset(
            receipt.device_preflight.checked_capabilities
        ):
            device_pending.add("required_capabilities_not_checked")
        if not receipt.device_preflight.snapshot_id:
            device_pending.add("device_contract_snapshot_missing")

    reasons = sorted(science_hard | science_pending | device_hard | device_pending)
    status: Literal["admissible", "rejected", "unresolved"]
    status = (
        "rejected" if science_hard or device_hard
        else "unresolved" if science_pending or device_pending
        else "admissible"
    )
    scientific_status: Literal["admissible", "rejected", "unresolved"] = (
        "rejected" if science_hard
        else "unresolved" if science_pending
        else "admissible"
    )
    priority: List[int] = []
    if status == "admissible" and receipt is not None:
        required_supported = sum(
            (
                True if classify_route_field_basis(field_path) == "generated_id"
                else field_by_path[field_path].status == "supported"
            )
            for field_path in goal.required_fields
        )
        primary_supported = sum(
            classify_route_field_basis(field_path) != "generated_id"
            and field_by_path[field_path].status == "supported"
            and field_by_path[field_path].provenance is not None
            and field_by_path[field_path].provenance.kind == "paper"
            for field_path in goal.required_fields
        )
        resolved_fidelity = sum(
            bool(str(risk.equivalence_evidence or "").strip())
            for risk in receipt.scientific_completeness.scientific_fidelity_risks
        ) if receipt.scientific_completeness is not None else 0
        priority = [
            primary_supported,
            required_supported,
            -receipt.route_adaptation_count,
            1 if receipt.native_device_support else 0,
            -resolved_fidelity,
            -receipt.derived_scaled_parameter_count,
        ]
    return RouteCandidateDecisionV1(
        route_id=candidate.route_id,
        candidate=candidate,
        candidate_digest=canonical_digest(candidate),
        validation=(
            receipt if receipt is not None
            and receipt.route_id == candidate.route_id
            and receipt.candidate_digest == canonical_digest(candidate)
            else None
        ),
        validation_receipt_digest=(
            canonical_digest(receipt) if receipt is not None
            and receipt.route_id == candidate.route_id
            and receipt.candidate_digest == canonical_digest(candidate)
            else ""
        ),
        status=status,
        scientific_status=scientific_status,
        device_status=device_status,
        reasons=reasons,
        priority=priority,
    )


def decide_routes(
    goal: RouteGoalV1,
    candidates: Sequence[RouteCandidateV1],
    validate: Callable[[RouteCandidateV1], RouteValidationReceiptV1],
) -> RouteDecisionV1:
    """Select by hard constraints then a stable lexicographic policy.

    ``validate`` is a trusted Chem-Agent adapter.  Failure, missing receipts,
    or unknown checks cause abstention.  A selection authorizes macro planning
    only; it does not certify execution or bypass the existing publish gates.
    """

    ids = [candidate.route_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("route_id values must be unique")
    candidates = sorted(candidates, key=lambda item: item.route_id)
    receipts: Dict[str, RouteValidationReceiptV1] = {}
    validation_errors: Dict[str, str] = {}
    for candidate in candidates:
        try:
            receipts[candidate.route_id] = RouteValidationReceiptV1.model_validate(
                validate(candidate)
            )
        except Exception as exc:
            # A failed verifier is not permission to plan from unverified data.
            validation_errors[candidate.route_id] = type(exc).__name__
    records = [
        _candidate_assessment(goal, candidate, receipts.get(candidate.route_id))
        for candidate in candidates
    ]
    for record in records:
        if record.route_id in validation_errors:
            record.reasons.append(
                "validation_error:" + validation_errors[record.route_id]
            )
    eligible = [record for record in records if record.status == "admissible"]
    chosen: Optional[RouteCandidateDecisionV1] = None
    reasons: List[str] = []
    result_status: Literal["selected_for_planning", "unresolved", "needs_route_choice"]
    if any(record.status == "unresolved" for record in records):
        result_status = "unresolved"
        reasons = ["candidate_validation_incomplete"]
    elif eligible:
        best_priority = max(record.priority for record in eligible)
        winners = [record for record in eligible if record.priority == best_priority]
        if len(winners) == 1:
            chosen = winners[0]
            chosen.status = "selected_for_planning"
            result_status = "selected_for_planning"
            reasons = ["unique_lexicographic_winner"]
        else:
            result_status = "needs_route_choice"
            reasons = ["multiple_equally_admissible_routes"]
    else:
        result_status = "unresolved"
        reasons = ["no_admissible_route"]

    evidence_hash = canonical_digest([
        {
            "route_id": candidate.route_id,
            "source_scope": candidate.source_scope.model_dump(mode="json") if candidate.source_scope else None,
            "evidence_bundle": [item.model_dump(mode="json") for item in candidate.evidence_bundle],
            "evidence_matrix": [item.model_dump(mode="json") for item in candidate.evidence_matrix],
            "verified_evidence_ids": (
                receipts[candidate.route_id].verified_evidence_ids
                if candidate.route_id in receipts else []
            ),
            "verified_field_paths": (
                receipts[candidate.route_id].verified_field_paths
                if candidate.route_id in receipts else []
            ),
            "source_route_signature": (
                receipts[candidate.route_id].source_route_signature
                if candidate.route_id in receipts else None
            ),
            "source_route_signature_review_digest": (
                receipts[candidate.route_id].source_route_signature_review_digest
                if candidate.route_id in receipts else ""
            ),
            "source_document_kind": (
                receipts[candidate.route_id].source_document_kind
                if candidate.route_id in receipts else ""
            ),
            "source_identity_doi": (
                receipts[candidate.route_id].source_identity_doi
                if candidate.route_id in receipts else ""
            ),
            "source_attestation_digest": (
                receipts[candidate.route_id].source_attestation_digest
                if candidate.route_id in receipts else ""
            ),
        }
        for candidate in candidates
    ])
    snapshots = {
        receipt.device_preflight.snapshot_id
        for receipt in receipts.values() if receipt.device_preflight.snapshot_id
    }
    device_hash = next(iter(snapshots)) if len(snapshots) == 1 else ""
    if len(snapshots) > 1 or (receipts and not device_hash):
        # A mixed/unknown platform snapshot makes an apparent winner stale.
        chosen = None
        result_status = "unresolved"
        reasons = ["device_contract_snapshot_inconsistent"]
        for record in records:
            if record.status == "selected_for_planning":
                record.status = "unresolved"
                record.reasons.append("device_contract_snapshot_inconsistent")
    payload = {
        "goal_id": goal.goal_id,
        "goal": goal,
        "candidates": records,
        "selected_route_id": chosen.route_id if chosen else None,
        "status": result_status,
        "selection_policy": SELECTION_POLICY_V1,
        "evidence_snapshot_hash": evidence_hash,
        "device_contract_snapshot_hash": device_hash,
        "decision_reasons": reasons,
    }
    serializable = {
        "schema_version": "route-decision/v1",
        **payload,
        "goal": goal.model_dump(mode="json"),
        "candidates": [record.model_dump(mode="json") for record in records],
    }
    return RouteDecisionV1(
        decision_id=canonical_digest(serializable),
        **payload,
    )

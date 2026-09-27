"""Strict, read-only binding preflight for an explicitly supplied route action.

This contract does not construct a macro plan or authorize V2 publication.
The caller must supply Stage/Action intent and a current trusted decision and
evidence bundle; Research and Device publication gates remain authoritative.
"""

from __future__ import annotations

import re
from typing import Any, Iterator, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .route_candidate import RouteCandidateV1
from .route_decision import RouteDecisionV1
from .v2 import (
    EvidenceBundleV2,
    MacroActionV2,
    ProvenanceV2,
    StageV2,
    StrictModel,
    canonical_digest,
)


_DIGEST_PATTERN = r"^sha256_[0-9a-f]{64}$"
_VERIFIED_EVIDENCE = {
    "verified_doi", "verified_arxiv", "verified_semantic_scholar", "local_file",
}
_PARSED_FULL_TEXT = {"parsed", "local_parsed"}
_T = TypeVar("_T", bound=BaseModel)


class RouteActionIntentV1(StrictModel):
    """Execution intent supplied independently of a selected route candidate."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["route-action-intent/v1"] = "route-action-intent/v1"
    route_id: str = Field(min_length=1)
    candidate_digest: str = Field(pattern=_DIGEST_PATTERN)
    decision_id: str = Field(pattern=_DIGEST_PATTERN)
    evidence_bundle_id: str = Field(min_length=1)
    evidence_bundle_digest: str = Field(pattern=_DIGEST_PATTERN)
    stage: StageV2
    macro_action: MacroActionV2


def _fresh(model_type: type[_T], value: _T | dict[str, Any]) -> _T:
    """Revalidate a snapshot so mutation of an existing model cannot pass."""

    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return model_type.model_validate(payload, strict=True)


def _require_text(value: str, path: str) -> None:
    if not value.strip():
        raise ValueError(f"{path} must be explicitly nonempty")


def _graph_provenances(value: Any, path: str) -> Iterator[tuple[str, ProvenanceV2]]:
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _graph_provenances(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            item_path = f"{path}.{key}"
            if key == "provenance":
                yield item_path, ProvenanceV2.model_validate(item, strict=True)
            else:
                yield from _graph_provenances(item, item_path)


def _package_scalar(
    source_path: str, candidate: RouteCandidateV1, bundle: EvidenceBundleV2,
) -> Any:
    if source_path == "evidence_bundle.query":
        return bundle.query
    match = re.fullmatch(r"evidence_bundle\.items\[(\d+)\]\.excerpt", source_path)
    if match:
        index = int(match.group(1))
        if index < len(bundle.items):
            return bundle.items[index].excerpt
    match = re.fullmatch(r"macro_steps\[(\d+)\]\.operation", source_path)
    if match:
        index = int(match.group(1))
        if index < len(candidate.material_graph):
            return candidate.material_graph[index].operation
    match = re.fullmatch(
        r"macro_steps\[(\d+)\]\.parameters\[(\d+)\]\.value", source_path,
    )
    if match:
        step_index, parameter_index = (int(part) for part in match.groups())
        if (step_index < len(candidate.material_graph)
                and parameter_index < len(candidate.material_graph[step_index].parameters)):
            return candidate.material_graph[step_index].parameters[parameter_index].value
    raise ValueError(f"unresolvable provenance source_path: {source_path}")


def _check_provenance(
    path: str,
    provenance: ProvenanceV2,
    candidate: RouteCandidateV1,
    bundle: EvidenceBundleV2,
    verified_ids: set[str],
    source_identity_doi: str,
) -> None:
    if provenance.kind == "paper":
        candidate_ids = [item.evidence_id for item in candidate.evidence_bundle]
        if provenance.reference not in candidate_ids or provenance.reference not in verified_ids:
            raise ValueError(f"{path} paper reference is not verified for this candidate")
        index = candidate_ids.index(provenance.reference)
        candidate_item = candidate.evidence_bundle[index]
        if (source_identity_doi and candidate_item.doi
                and candidate_item.doi.strip().lower()
                != source_identity_doi.strip().lower()):
            raise ValueError(f"{path} paper DOI differs from attested route source")
        expected_path = f"evidence_bundle.items[{index}].excerpt"
        if provenance.source_path != expected_path:
            raise ValueError(f"{path} paper source_path does not match evidence_id")
        item = bundle.items[index]
        if (item.verification_status not in _VERIFIED_EVIDENCE
                or item.full_text_status not in _PARSED_FULL_TEXT):
            raise ValueError(f"{path} paper evidence is not verified and parsed")
        source_value = item.excerpt
    elif provenance.kind == "user":
        if provenance.source_path != "evidence_bundle.query":
            raise ValueError(f"{path} user source_path must be evidence_bundle.query")
        source_value = bundle.query
    elif provenance.kind == "manual_revision":
        source_value = _package_scalar(provenance.source_path, candidate, bundle)
    else:
        # Other kinds have their own Research publication checks. This
        # preflight must not relabel inference or a Device SOP as paper evidence.
        return
    if not provenance.excerpt or provenance.excerpt not in str(source_value):
        raise ValueError(f"{path} excerpt does not match current source")
    if provenance.source_digest != canonical_digest(source_value):
        raise ValueError(f"{path} source digest does not match current source")


def validate_route_action_intent_v1(
    intent: RouteActionIntentV1 | dict[str, Any],
    *,
    decision: RouteDecisionV1 | dict[str, Any],
    current_evidence_bundle: EvidenceBundleV2 | dict[str, Any],
) -> RouteCandidateV1:
    """Validate exact identities and return a detached selected candidate.

    A valid result is only a binding preflight. It is neither an executable
    plan nor a substitute for the Research V2 and Device publication gates.
    ``decision`` and ``current_evidence_bundle`` must come from trusted current
    evaluators, not from model-authored or restored status claims.
    """

    bound = _fresh(RouteActionIntentV1, intent)
    selected_decision = _fresh(RouteDecisionV1, decision)
    bundle = _fresh(EvidenceBundleV2, current_evidence_bundle)
    if selected_decision.status != "selected_for_planning":
        raise ValueError("route decision has not selected a route")
    if bound.decision_id != selected_decision.decision_id:
        raise ValueError("route decision digest mismatch")
    if bound.route_id != selected_decision.selected_route_id:
        raise ValueError("selected route ID mismatch")
    selected = next(
        item for item in selected_decision.candidates
        if item.route_id == selected_decision.selected_route_id
    )
    candidate = selected.candidate
    if bound.candidate_digest != selected.candidate_digest:
        raise ValueError("selected candidate digest mismatch")
    if (selected.status != "selected_for_planning"
            or selected.scientific_status != "admissible"
            or selected.device_status != "preflight_supported"
            or selected.validation is None):
        raise ValueError("selected route lacks a trusted validation receipt")
    receipt = selected.validation
    if (receipt.device_preflight.status != "preflight_supported"
            or not receipt.device_preflight.snapshot_id
            or receipt.device_preflight.snapshot_id != selected_decision.device_contract_snapshot_hash):
        raise ValueError("selected route device preflight snapshot mismatch")
    if candidate.origin == "paper_experimental_group" and (
        not receipt.source_scope_verified
        or not receipt.source_identity_doi
        or not receipt.source_attestation_digest
        or receipt.source_document_kind not in {
            "primary_paper", "supporting_information"
        }
    ):
        raise ValueError("selected paper group has no attested source identity")

    if (bundle.scope != "macro_action" or not bundle.current_invocation_only
            or bound.evidence_bundle_id != bundle.bundle_id
            or bound.evidence_bundle_digest != canonical_digest(bundle)):
        raise ValueError("current macro-action evidence bundle identity mismatch")
    if len(bundle.items) != len(candidate.evidence_bundle):
        raise ValueError("current evidence bundle differs from selected route evidence")
    identity_fields = ("evidence_id", "title", "doi", "arxiv_id", "url", "excerpt")
    for index, candidate_item in enumerate(candidate.evidence_bundle):
        current_item = bundle.items[index]
        if any(getattr(candidate_item, field) != getattr(current_item, field)
               for field in identity_fields):
            raise ValueError(f"candidate evidence identity mismatch at index {index}")
    if receipt.source_identity_doi and any(
        item.doi.strip()
        and item.doi.strip().lower() != receipt.source_identity_doi.strip().lower()
        for item in candidate.evidence_bundle
    ):
        raise ValueError("candidate evidence DOI differs from attested route source")

    stage = bound.stage
    action = bound.macro_action
    for path, value in (
        ("stage.stage_id", stage.stage_id), ("stage.name", stage.name),
        ("stage.objective", stage.objective),
        ("stage.observation_point", stage.observation_point),
        ("stage.completion_condition", stage.completion_condition),
        ("macro_action.macro_action_id", action.macro_action_id),
        ("macro_action.observation_point_id", action.observation_point_id),
        ("macro_action.objective", action.objective),
        ("macro_action.expected_observation", action.expected_observation),
        ("macro_action.completion_condition", action.completion_condition),
        ("macro_action.experiment_group.group_id", action.experiment_group.group_id),
        ("macro_action.experiment_group.sample_id", action.experiment_group.sample_id),
    ):
        _require_text(value, path)
    if action.stage_id != stage.stage_id:
        raise ValueError("macro action stage ID mismatch")
    if not set(candidate.required_capabilities).issubset(stage.capability_requirements):
        raise ValueError("stage omits selected route capability requirements")
    graph = candidate.material_graph
    if not graph:
        raise ValueError("selected route has no material graph")
    step_ids = [step.macro_step_id for step in graph]
    if (any(not step_id.strip() for step_id in step_ids)
            or len(set(step_ids)) != len(step_ids)
            or step_ids != receipt.verified_graph_step_ids):
        raise ValueError("material graph step IDs do not match verified receipt")
    if [step.sequence for step in graph] != list(range(1, len(graph) + 1)):
        raise ValueError("material graph step sequence is not contiguous")
    if (not action.planned_operations
            or any(not operation.strip() for operation in action.planned_operations)
            or action.planned_operations != [step.operation for step in graph]):
        raise ValueError("macro action operation order differs from selected graph")
    if any(step.macro_action_id != action.macro_action_id for step in graph):
        raise ValueError("material graph macro action ID mismatch")
    if any(step.sample_id != action.experiment_group.sample_id for step in graph):
        raise ValueError("material graph execution sample ID mismatch")

    verified_ids = set(receipt.verified_evidence_ids)
    for index, step in enumerate(graph):
        for requirement_index, requirement in enumerate(step.quantity_requirements):
            if "provenance" not in requirement:
                raise ValueError(
                    f"material_graph[{index}].quantity_requirements[{requirement_index}] lacks provenance"
                )
        for path, provenance in _graph_provenances(
            step.model_dump(mode="json", exclude_none=True), f"material_graph[{index}]",
        ):
            _check_provenance(
                path, provenance, candidate, bundle, verified_ids,
                receipt.source_identity_doi,
            )
    for index, field in enumerate(candidate.evidence_matrix):
        if field.status == "supported" and field.provenance is None:
            raise ValueError(f"evidence_matrix[{index}] supported field lacks provenance")
        if field.provenance is not None:
            if (field.provenance.kind == "paper"
                    and field.evidence_id != field.provenance.reference):
                raise ValueError(f"evidence_matrix[{index}] evidence ID mismatch")
            _check_provenance(
                f"evidence_matrix[{index}].provenance", field.provenance,
                candidate, bundle, verified_ids, receipt.source_identity_doi,
            )
    return candidate.model_copy(deep=True)

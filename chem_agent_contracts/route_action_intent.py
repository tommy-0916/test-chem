"""Strict, read-only binding preflight for an explicitly supplied route action.

This contract does not construct a macro plan or authorize V2 publication.
The caller must supply Stage/Action intent and a current trusted decision and
evidence bundle; Research and Device publication gates remain authoritative.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Iterator, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .route_candidate import RouteCandidateV1
from .route_decision import RouteDecisionV1
from .v2 import (
    EvidenceBundleV2,
    MacroActionV2,
    MacroStepV2,
    ProvenanceV2,
    StageV2,
    StrictModel,
    canonical_digest,
    evidence_contains_exact_quantity,
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


class RouteActionBindingDraftV1(StrictModel):
    """Detached typed route/action binding for review, never a V2 publication."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["route-action-binding-draft/v1"] = (
        "route-action-binding-draft/v1"
    )
    route_id: str = Field(min_length=1)
    decision_id: str = Field(pattern=_DIGEST_PATTERN)
    candidate_digest: str = Field(pattern=_DIGEST_PATTERN)
    evidence_snapshot_hash: str = Field(pattern=_DIGEST_PATTERN)
    device_contract_snapshot_hash: str = Field(min_length=1)
    evidence_bundle_id: str = Field(min_length=1)
    evidence_bundle_digest: str = Field(pattern=_DIGEST_PATTERN)
    stage: StageV2
    macro_action: MacroActionV2
    macro_steps: list[MacroStepV2] = Field(min_length=1)
    evidence_bundle: EvidenceBundleV2

    @model_validator(mode="after")
    def validate_embedded_binding(self) -> "RouteActionBindingDraftV1":
        if (self.evidence_bundle_id != self.evidence_bundle.bundle_id
                or self.evidence_bundle_digest != canonical_digest(self.evidence_bundle)):
            raise ValueError("binding draft evidence bundle identity mismatch")
        if self.macro_action.stage_id != self.stage.stage_id:
            raise ValueError("binding draft stage ID mismatch")
        if self.macro_action.planned_operations != [
            step.operation for step in self.macro_steps
        ]:
            raise ValueError("binding draft operation order mismatch")
        if any(
            step.macro_action_id != self.macro_action.macro_action_id
            or step.sample_id != self.macro_action.experiment_group.sample_id
            for step in self.macro_steps
        ):
            raise ValueError("binding draft action or sample ID mismatch")
        return self


def _plain_snapshot(value: Any) -> Any:
    """Detach nested model instances too, so Pydantic revalidates their fields."""

    if isinstance(value, BaseModel):
        return _plain_snapshot(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {copy.deepcopy(key): _plain_snapshot(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain_snapshot(item) for item in value]
    return copy.deepcopy(value)


def _fresh(model_type: type[_T], value: _T | dict[str, Any]) -> _T:
    """Deep-copy and revalidate snapshots, including nested model instances."""

    return model_type.model_validate(_plain_snapshot(value), strict=True)


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


def _check_paper_parameter_value(path: str, value: Any, unit: str, excerpt: str) -> None:
    """Require each paper-backed parameter's actual value in its excerpt."""

    if isinstance(value, bool):
        raise ValueError(f"{path} paper parameter must not be a boolean")
    if isinstance(value, (int, float)):
        if not evidence_contains_exact_quantity(excerpt, value, unit):
            raise ValueError(f"{path} paper excerpt does not support the parameter quantity")
        return
    if isinstance(value, str) and value.strip() and not unit.strip():
        # ScientificParameterV2.value is Any. A numeric string must not avoid
        # the numeric evidence check, while an explicitly quoted qualitative
        # setting may remain textual when the paper states it verbatim.
        if not re.search(r"\d", value) and re.search(
            rf"(?<!\w){re.escape(value.strip())}(?!\w)", excerpt, re.IGNORECASE,
        ):
            return
    raise ValueError(f"{path} paper parameter is not bound to an exact quoted value")


def _validated_binding_inputs(
    intent: RouteActionIntentV1 | dict[str, Any],
    *,
    decision: RouteDecisionV1 | dict[str, Any],
    current_evidence_bundle: EvidenceBundleV2 | dict[str, Any],
) -> tuple[RouteActionIntentV1, RouteDecisionV1, EvidenceBundleV2, RouteCandidateV1]:
    """Revalidate the trusted inputs once for both preflight and draft binding."""

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
        for parameter_index, parameter in enumerate(step.parameters):
            if parameter.provenance.kind == "paper":
                _check_paper_parameter_value(
                    f"material_graph[{index}].parameters[{parameter_index}].provenance",
                    parameter.value, parameter.unit, parameter.provenance.excerpt,
                )
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
    return bound, selected_decision, bundle, candidate


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

    _, _, _, candidate = _validated_binding_inputs(
        intent, decision=decision,
        current_evidence_bundle=current_evidence_bundle,
    )
    return candidate.model_copy(deep=True)


def build_route_action_binding_draft_v1(
    intent: RouteActionIntentV1 | dict[str, Any],
    *,
    decision: RouteDecisionV1 | dict[str, Any],
    current_evidence_bundle: EvidenceBundleV2 | dict[str, Any],
) -> RouteActionBindingDraftV1:
    """Bind explicit intent to the selected typed graph without publishing it.

    The decision and current bundle must be supplied by trusted evaluators.
    This draft is detached and revalidated, but cannot authorize Research V2
    publication or Device execution.
    """

    bound, selected_decision, bundle, candidate = _validated_binding_inputs(
        intent, decision=decision,
        current_evidence_bundle=current_evidence_bundle,
    )
    return _fresh(RouteActionBindingDraftV1, {
        "route_id": bound.route_id,
        "decision_id": selected_decision.decision_id,
        "candidate_digest": bound.candidate_digest,
        "evidence_snapshot_hash": selected_decision.evidence_snapshot_hash,
        "device_contract_snapshot_hash": selected_decision.device_contract_snapshot_hash,
        "evidence_bundle_id": bundle.bundle_id,
        "evidence_bundle_digest": bound.evidence_bundle_digest,
        "stage": bound.stage.model_dump(mode="json"),
        "macro_action": bound.macro_action.model_dump(mode="json"),
        "macro_steps": [step.model_dump(mode="json") for step in candidate.material_graph],
        "evidence_bundle": bundle.model_dump(mode="json"),
    })


def validate_route_action_binding_draft_v1(
    draft: RouteActionBindingDraftV1 | dict[str, Any],
    *,
    decision: RouteDecisionV1 | dict[str, Any],
    current_evidence_bundle: EvidenceBundleV2 | dict[str, Any],
) -> RouteActionBindingDraftV1:
    """Rebind a saved draft to the current trusted decision and evidence.

    A draft's own digests are self-consistency checks, not an authority token.
    Any later consumer must compare it to a fresh binding from the trusted
    decision and current evidence before using its graph or action fields.
    """

    supplied = _fresh(RouteActionBindingDraftV1, draft)
    intent = RouteActionIntentV1(
        route_id=supplied.route_id,
        candidate_digest=supplied.candidate_digest,
        decision_id=supplied.decision_id,
        evidence_bundle_id=supplied.evidence_bundle_id,
        evidence_bundle_digest=supplied.evidence_bundle_digest,
        stage=supplied.stage,
        macro_action=supplied.macro_action,
    )
    expected = build_route_action_binding_draft_v1(
        intent, decision=decision,
        current_evidence_bundle=current_evidence_bundle,
    )
    if supplied != expected:
        raise ValueError("binding draft differs from current selected route graph or snapshots")
    return expected

"""Lossless typed route-to-Research package projection for review.

This is a package draft, not a Research publication or a Device handoff.
The Research publication gates and the Device raw/canonical consistency gate
must still run before execution. In particular, a route binding draft has no
original raw step or observation mirrors from which to attest transport
digests; this module does not manufacture them.
"""

from __future__ import annotations

import copy
from typing import Any, TypeVar

from pydantic import BaseModel

from .route_action_intent import (
    RouteActionIntentV1,
    RouteActionBindingDraftV1,
    validate_route_action_binding_draft_v1,
)
from .route_decision import RouteDecisionV1
from .v2 import (
    EvidenceBundleV2,
    MacroActionV2,
    ResearchActionPackageV2,
    RouteBindingV1,
    StageV2,
    canonical_digest,
    route_material_graph_digest_v1,
)


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _fresh(model_type: type[_ModelT], value: _ModelT | dict[str, Any]) -> _ModelT:
    """Detach and revalidate nested model instances, including mutable fields."""

    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return model_type.model_validate(copy.deepcopy(payload), strict=True)


def build_route_research_package_draft_v2(
    draft: RouteActionBindingDraftV1,
    *,
    decision: RouteDecisionV1,
    current_evidence_bundle: EvidenceBundleV2,
    stage: StageV2,
    macro_action: MacroActionV2,
    campaign_id: str,
) -> ResearchActionPackageV2:
    """Project a freshly rebound route graph to an unpublishable typed V2 draft.

    ``decision`` and ``current_evidence_bundle`` must be typed objects supplied
    by current trusted evaluators, not restored status JSON. Explicit
    Stage/Action intent must match the bound draft exactly. The hash-bound
    route binding records identities, not a replacement source attestation.
    """

    for path, value, model_type in (
        ("draft", draft, RouteActionBindingDraftV1),
        ("decision", decision, RouteDecisionV1),
        ("current_evidence_bundle", current_evidence_bundle, EvidenceBundleV2),
        ("stage", stage, StageV2),
        ("macro_action", macro_action, MacroActionV2),
    ):
        if not isinstance(value, model_type):
            raise TypeError(f"{path} must be a current typed {model_type.__name__}")
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        raise ValueError("campaign_id must be explicitly nonempty")
    if campaign_id != campaign_id.strip():
        raise ValueError("campaign_id must not have surrounding whitespace")

    # Use one detached trusted snapshot throughout. The draft is rebuilt from
    # that snapshot, rather than treating its own digests as authority.
    selected_decision = _fresh(RouteDecisionV1, decision)
    bundle = _fresh(EvidenceBundleV2, current_evidence_bundle)
    bound = validate_route_action_binding_draft_v1(
        draft,
        decision=selected_decision,
        current_evidence_bundle=bundle,
    )
    supplied_stage = _fresh(StageV2, stage)
    supplied_action = _fresh(MacroActionV2, macro_action)
    if supplied_stage != bound.stage:
        raise ValueError("explicit stage differs from current route binding")
    if supplied_action != bound.macro_action:
        raise ValueError("explicit macro_action differs from current route binding")

    selected = next(
        item for item in selected_decision.candidates
        if item.route_id == selected_decision.selected_route_id
    )
    receipt = selected.validation
    if receipt is None or receipt.scientific_completeness is None:
        raise ValueError("selected route lacks a current scientific completeness audit")
    if (
        selected.candidate.origin == "paper_experimental_group"
        and not receipt.source_route_signature_review_digest
    ):
        raise ValueError("selected paper route lacks a source signature review digest")
    if not selected_decision.device_contract_snapshot_hash.strip():
        raise ValueError("selected route lacks a device contract snapshot")
    if any(
        step.raw_step_digest_scope is not None or step.raw_step_sha256
        for step in bound.macro_steps
    ):
        raise ValueError("route graph has unattested raw step transport digests")

    candidate = selected.candidate
    scope = candidate.source_scope
    intent = RouteActionIntentV1(
        route_id=bound.route_id,
        candidate_digest=bound.candidate_digest,
        decision_id=bound.decision_id,
        evidence_bundle_id=bound.evidence_bundle_id,
        evidence_bundle_digest=bound.evidence_bundle_digest,
        stage=bound.stage,
        macro_action=bound.macro_action,
    )
    route_binding = RouteBindingV1(
        origin=candidate.origin,
        route_id=bound.route_id,
        candidate_digest=bound.candidate_digest,
        decision_id=bound.decision_id,
        validation_receipt_digest=selected.validation_receipt_digest,
        source_paper_id=scope.paper_id if scope is not None else "",
        experimental_group_id=(
            scope.experimental_group_id if scope is not None else ""
        ),
        source_section=scope.section if scope is not None else "",
        source_locator=scope.locator if scope is not None else "",
        source_digest=scope.source_digest if scope is not None else "",
        source_attestation_digest=receipt.source_attestation_digest,
        source_route_signature_review_digest=(
            receipt.source_route_signature_review_digest
        ),
        source_identity_doi=receipt.source_identity_doi,
        required_capabilities=list(candidate.required_capabilities),
        evidence_snapshot_hash=selected_decision.evidence_snapshot_hash,
        evidence_bundle_digest=bound.evidence_bundle_digest,
        intent_digest=canonical_digest(intent),
        material_graph_digest=route_material_graph_digest_v1(bound.macro_steps),
        device_contract_snapshot_hash=selected_decision.device_contract_snapshot_hash,
    )

    package = ResearchActionPackageV2.model_validate({
        "campaign_id": campaign_id,
        "capability_snapshot_id": selected_decision.device_contract_snapshot_hash,
        "stage": bound.stage.model_dump(mode="json"),
        "macro_action": bound.macro_action.model_dump(mode="json"),
        "macro_steps": [step.model_dump(mode="json") for step in bound.macro_steps],
        "evidence_bundle": bundle.model_dump(mode="json"),
        "scientific_completeness": receipt.scientific_completeness.model_dump(mode="json"),
        "route_binding": route_binding.model_dump(mode="json"),
    }, strict=True)

    # V2's hash has compatibility rules based on fields set in the model.
    # Revalidate the actual JSON representation, as the existing adapter does.
    wire_payload = package.model_dump(mode="json", exclude_none=True)
    wire_payload.pop("research_contract_hash", None)
    result = ResearchActionPackageV2.model_validate(wire_payload, strict=True)
    ResearchActionPackageV2.model_validate(
        result.model_dump(mode="json", exclude_none=True), strict=True
    )

    # Any conversion of an Any-valued scientific field or provenance would
    # violate the lossless typed projection. Do not return such a package.
    if (
        result.stage != bound.stage
        or result.macro_action != bound.macro_action
        or result.macro_steps != bound.macro_steps
        or result.evidence_bundle != bundle
        or result.scientific_completeness != receipt.scientific_completeness
        or result.route_binding != route_binding
    ):
        raise ValueError("V2 package projection changed bound route fields")
    return result

"""Turn a task-grounded stage arm and a fresh route decision into action intent.

The task producer supplies objectives and comparison roles.  The selected,
validated graph supplies operation order and execution IDs.  This module does
not infer chemistry or change a candidate graph to make the two agree.
"""

from __future__ import annotations

from chem_agent_contracts.route_action_intent import (
    RouteActionIntentV1, validate_route_action_intent_v1,
)
from chem_agent_contracts.route_decision import RouteDecisionV1
from chem_agent_contracts.v2 import (
    EvidenceBundleV2, ExperimentGroupV2, MacroActionV2, StageV2,
    canonical_digest,
)

from .stage_task_coverage import StageTaskRequirementsV1


def action_for_selected_stage_arm(
    requirements: StageTaskRequirementsV1,
    arm_id: str,
    decision: RouteDecisionV1,
) -> tuple[StageV2, MacroActionV2]:
    """Create one non-chemical action shell without editing selected facts."""

    req = StageTaskRequirementsV1.model_validate(
        requirements.model_dump(mode="json"), strict=True,
    )
    current = RouteDecisionV1.model_validate(
        decision.model_dump(mode="json"), strict=True,
    )
    matches = [arm for arm in req.arms if arm.arm_id == arm_id]
    if len(matches) != 1:
        raise ValueError("stage arm is not unique in current task requirements")
    arm = matches[0]
    if current.goal != arm.route_goal:
        raise ValueError("route decision goal differs from current stage arm")
    if current.status != "selected_for_planning":
        raise ValueError("stage arm has no selected route")
    selected = next(
        item for item in current.candidates
        if item.route_id == current.selected_route_id
    )
    graph = selected.candidate.material_graph
    if not graph:
        raise ValueError("selected route has no material graph")
    action_ids = {step.macro_action_id for step in graph}
    sample_ids = {step.sample_id for step in graph}
    if len(action_ids) != 1 or len(sample_ids) != 1 or not all(
        action_ids | sample_ids
    ):
        raise ValueError("selected route graph has ambiguous execution IDs")
    action_id = next(iter(action_ids))
    sample_id = next(iter(sample_ids))
    requirements_caps = sorted(set(
        req.stage.capability_requirements
    ) | set(selected.candidate.required_capabilities))
    stage = req.stage.model_copy(
        update={"capability_requirements": requirements_caps}, deep=True,
    )
    semantic = arm.action_intent
    action = MacroActionV2(
        macro_action_id=action_id,
        stage_id=stage.stage_id,
        observation_point_id=(
            "OP-" + canonical_digest({
                "stage_id": stage.stage_id,
                "arm_id": arm.arm_id,
                "observation_point": stage.observation_point,
            }).removeprefix("sha256_")[:16]
        ),
        experiment_group=ExperimentGroupV2(
            group_id=arm.arm_id,
            role=arm.role,
            sample_id=sample_id,
            hypothesis=semantic.hypothesis,
            comparison_to=list(semantic.comparison_to_arm_ids),
            variables=dict(semantic.variables),
        ),
        objective=semantic.objective,
        planned_operations=[step.operation for step in graph],
        expected_observation=semantic.expected_observation,
        completion_condition=semantic.completion_condition,
    )
    return stage, action


def finalize_selected_route_action_intent(
    *,
    decision: RouteDecisionV1,
    evidence_bundle: EvidenceBundleV2,
    stage: StageV2,
    macro_action: MacroActionV2,
) -> RouteActionIntentV1:
    """Attach fresh decision/evidence identities and run existing preflight."""

    current = RouteDecisionV1.model_validate(
        decision.model_dump(mode="json"), strict=True,
    )
    bundle = EvidenceBundleV2.model_validate(
        evidence_bundle.model_dump(mode="json"), strict=True,
    )
    if current.status != "selected_for_planning":
        raise ValueError("cannot form action intent without a selected route")
    selected = next(
        item for item in current.candidates
        if item.route_id == current.selected_route_id
    )
    intent = RouteActionIntentV1(
        route_id=selected.route_id,
        candidate_digest=selected.candidate_digest,
        decision_id=current.decision_id,
        evidence_bundle_id=bundle.bundle_id,
        evidence_bundle_digest=canonical_digest(bundle),
        stage=stage,
        macro_action=macro_action,
    )
    validate_route_action_intent_v1(
        intent, decision=current, current_evidence_bundle=bundle,
    )
    return intent


__all__ = [
    "action_for_selected_stage_arm", "finalize_selected_route_action_intent",
]

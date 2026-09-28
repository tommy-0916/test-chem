"""Deterministic saved-state projection for a freshly selected route.

The raw macro plan here is a transport mirror projected from the selected
typed graph. Its digest attests those exact persisted bytes; it is not a new
attestation of the source PDF. Publication and independent scientific review
remain the responsibility of the existing Research gates.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from .adapters import research_state_to_v2
from .route_action_intent import RouteActionBindingDraftV1
from .route_decision import RouteDecisionV1
from .route_research_package_draft import build_route_research_package_draft_v2
from .v2 import (
    RAW_OBSERVATIONS_DIGEST_SCOPE_V1,
    RAW_STEP_DIGEST_SCOPE_V1,
    EvidenceBundleV2,
    ResearchActionPackageV2,
    canonical_raw_observations_digest,
    canonical_raw_step_digest,
    validate_raw_steps_against_canonical,
)


class RouteSavedStateMismatch(ValueError):
    """A persisted mirror would change a fact from the selected typed route."""


def _json_copy(value: Any, path: str) -> Any:
    """Require the caller's raw observations to survive a JSON save/reload."""

    try:
        saved = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} must be finite JSON data") from exc
    difference = _first_difference(value, saved, path)
    if difference is not None:
        raise ValueError(f"{path} changes across JSON save/reload at {difference}")
    return saved


def _first_difference(expected: Any, actual: Any, path: str) -> str | None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(expected.keys() | actual.keys(), key=str):
            child = f"{path}.{key}"
            if key not in expected or key not in actual:
                return child
            difference = _first_difference(expected[key], actual[key], child)
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return path
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = _first_difference(left, right, f"{path}[{index}]")
            if difference is not None:
                return difference
        return None
    if type(expected) is not type(actual) or expected != actual:
        return path
    return None


def _semantic_payload(package: ResearchActionPackageV2) -> dict[str, Any]:
    """Exclude only adapter-generated transport metadata from comparison."""

    payload = package.model_dump(mode="json", exclude_none=False)
    for field in (
        "identity_encoding",
        "raw_observations_digest_scope",
        "raw_observations_sha256",
        "research_contract_hash",
    ):
        payload.pop(field, None)
    for step in payload["macro_steps"]:
        step.pop("raw_step_digest_scope", None)
        step.pop("raw_step_sha256", None)
    return payload


def _assert_projection_equal(
    selected: ResearchActionPackageV2,
    rebuilt: ResearchActionPackageV2,
    raw_steps: list[dict[str, Any]],
    observations: list[Any],
) -> None:
    difference = _first_difference(
        _semantic_payload(selected), _semantic_payload(rebuilt), "research_package"
    )
    if difference is not None:
        raise RouteSavedStateMismatch(
            f"selected route changed during saved-state reconstruction: {difference}"
        )
    if rebuilt.raw_observations_digest_scope != RAW_OBSERVATIONS_DIGEST_SCOPE_V1:
        raise RouteSavedStateMismatch("raw observations digest scope changed")
    if rebuilt.raw_observations_sha256 != canonical_raw_observations_digest(observations):
        raise RouteSavedStateMismatch("raw observations digest changed")
    for index, (raw, step) in enumerate(zip(raw_steps, rebuilt.macro_steps)):
        if step.raw_step_digest_scope != RAW_STEP_DIGEST_SCOPE_V1:
            raise RouteSavedStateMismatch(f"macro_plan[{index}] digest scope changed")
        if step.raw_step_sha256 != canonical_raw_step_digest(raw):
            raise RouteSavedStateMismatch(f"macro_plan[{index}] raw digest changed")
    validate_raw_steps_against_canonical(raw_steps, rebuilt)


def _parameter_display(step: Any) -> str:
    """Render only exact facts already present in the typed step."""

    rendered: list[str] = []
    for parameter in step.parameters:
        value = parameter.value
        if value is None or (isinstance(value, str) and not value):
            continue
        if isinstance(value, (dict, list)):
            displayed = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            displayed = str(value)
        rendered.append(
            f"{parameter.name}: {displayed}{(' ' + parameter.unit) if parameter.unit else ''}"
        )
    if rendered:
        return "; ".join(rendered)
    for requirement in step.quantity_requirements:
        value = requirement.get("value")
        unit = requirement.get("unit")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and unit:
            rendered.append(
                f"{requirement.get('material') or requirement.get('material_id') or ''}: "
                f"{value} {unit}"
            )
    if rendered:
        return "; ".join(rendered)
    for port in (
        step.material_inputs + step.material_intermediates + step.material_outputs
    ):
        quantity = port.quantity
        if quantity is not None and quantity.mode == "exact":
            rendered.append(f"{port.name}: {quantity.value} {quantity.unit}")
    return "; ".join(rendered)


def build_selected_route_saved_state_v2(
    draft: RouteActionBindingDraftV1,
    *,
    decision: RouteDecisionV1,
    current_evidence_bundle: EvidenceBundleV2,
    campaign_id: str,
    observations: list[Any],
) -> dict[str, Any]:
    """Project a trusted selected route into a coherent, unpublished V2 state.

    The caller must supply raw observations explicitly. A mismatch in any
    scientific field, including material graph, parameter, provenance, stage,
    evidence, or hash-bound route identity, fails before returning a state.
    """

    if not isinstance(observations, list):
        raise TypeError("observations must be an explicitly supplied array")
    raw_observations = _json_copy(observations, "observations")
    selected = build_route_research_package_draft_v2(
        draft,
        decision=decision,
        current_evidence_bundle=current_evidence_bundle,
        stage=draft.stage,
        macro_action=draft.macro_action,
        campaign_id=campaign_id,
    )
    if selected.route_binding is None:
        raise RouteSavedStateMismatch("selected typed V2 package lacks route_binding")

    raw_steps: list[dict[str, Any]] = []
    for step in selected.macro_steps:
        # Keep explicit nulls. For native V2, null is distinct from an absent
        # material contract declaration and must not be upgraded by the adapter.
        raw = step.model_dump(mode="json", exclude_none=False)
        raw["container_requirements"] = raw.pop("logical_containers")
        raw["intermediate_returns"] = raw.pop("expected_return")
        raw["observation_point_id"] = selected.macro_action.observation_point_id
        raw["步骤序号"] = step.sequence
        raw["操作"] = step.operation
        raw["试剂/对象"] = step.reagent_or_object
        raw["参数"] = _parameter_display(step)
        raw_steps.append(raw)

    action_raw = selected.macro_action.model_dump(mode="json", exclude_none=False)
    action_raw.update({
        "current_stage_plan": selected.stage.objective,
        "observation_point": selected.stage.observation_point,
        "stage_completion_condition": selected.stage.completion_condition,
        "capability_requirements": copy.deepcopy(selected.stage.capability_requirements),
    })
    bundle = selected.evidence_bundle
    results = []
    for item in bundle.items:
        result = item.model_dump(mode="json", exclude_none=False)
        result["evidence_excerpt"] = result.pop("excerpt")
        results.append(result)
    evidence_raw = {
        "bundle_id": bundle.bundle_id,
        "scope": bundle.scope,
        "query": bundle.query,
        "objective": bundle.objective,
        "retrieval_status": bundle.retrieval_status,
        "current_invocation_only": bundle.current_invocation_only,
        "results": results,
        "errors": copy.deepcopy(bundle.errors),
    }
    state: dict[str, Any] = {
        "contract_version": "v2",
        "route_binding_status_v1": "selected_unbound",
        "campaign_id": selected.campaign_id,
        "device_snapshot_id": selected.capability_snapshot_id,
        "current_stage": selected.stage.name,
        "current_stage_plan": selected.stage.objective,
        "macro_action": action_raw,
        "macro_plan": raw_steps,
        "observations": raw_observations,
        "current_evidence_bundle": evidence_raw,
        "scientific_completeness": (
            selected.scientific_completeness.model_dump(mode="json", exclude_none=False)
            if selected.scientific_completeness is not None else None
        ),
        "route_binding": selected.route_binding.model_dump(mode="json", exclude_none=True),
    }
    rebuilt = research_state_to_v2(state)
    _assert_projection_equal(selected, rebuilt, raw_steps, raw_observations)
    canonical = rebuilt.model_dump(mode="json", exclude_none=True)
    state["research_action_package_v2"] = copy.deepcopy(canonical)
    state["device_adaptation_handoff"] = {
        "contract_version": "v2",
        "route_binding_status_v1": "selected_unbound",
        "campaign_id": selected.campaign_id,
        "当前 stage": selected.stage.name,
        "当前 macro action": copy.deepcopy(action_raw),
        "待执行 macro plan": copy.deepcopy(raw_steps),
        "设备能力快照ID": selected.capability_snapshot_id,
        "observations": copy.deepcopy(raw_observations),
        "route_binding": copy.deepcopy(state["route_binding"]),
        "research_action_package_v2": copy.deepcopy(canonical),
    }
    state["persistent_outputs"] = copy.deepcopy(state["device_adaptation_handoff"])
    return state


def validate_selected_route_saved_state_v2(state: dict[str, Any]) -> ResearchActionPackageV2:
    """Reload a saved route state and reject divergent raw/canonical mirrors."""

    restored = _json_copy(state, "saved_state")
    canonical_raw = restored.get("research_action_package_v2")
    if not isinstance(canonical_raw, dict):
        raise RouteSavedStateMismatch("saved state lacks canonical V2 package")
    canonical = ResearchActionPackageV2.model_validate(canonical_raw, strict=True)
    if canonical.route_binding is None:
        raise RouteSavedStateMismatch("canonical V2 package lacks route_binding")
    if restored.get("route_binding") != canonical.route_binding.model_dump(
        mode="json", exclude_none=True
    ):
        raise RouteSavedStateMismatch("top-level route_binding differs from canonical V2")
    raw_steps = restored.get("macro_plan")
    observations = restored.get("observations")
    if not isinstance(raw_steps, list) or not raw_steps or not isinstance(observations, list):
        raise RouteSavedStateMismatch("saved state lacks explicit macro_plan or observations")
    handoff = restored.get("device_adaptation_handoff")
    if not isinstance(handoff, dict) or any(
        handoff.get(key) != value for key, value in (
            ("route_binding_status_v1", restored.get("route_binding_status_v1")),
            ("待执行 macro plan", raw_steps),
            ("observations", observations),
            ("route_binding", restored["route_binding"]),
            ("research_action_package_v2", canonical_raw),
        )
    ):
        raise RouteSavedStateMismatch("Device handoff mirror differs from saved state")
    persistent = restored.get("persistent_outputs")
    if not isinstance(persistent, dict) or any(
        persistent.get(key) != handoff.get(key) for key in (
            "route_binding_status_v1",
            "待执行 macro plan",
            "observations",
            "route_binding",
            "research_action_package_v2",
        )
    ):
        raise RouteSavedStateMismatch("persistent output mirror differs from Device handoff")
    rebuilt = research_state_to_v2(restored)
    difference = _first_difference(
        canonical.model_dump(mode="json", exclude_none=False),
        rebuilt.model_dump(mode="json", exclude_none=False),
        "research_package",
    )
    if difference is not None:
        raise RouteSavedStateMismatch(f"saved canonical V2 differs from raw rebuild: {difference}")
    validate_raw_steps_against_canonical(raw_steps, canonical)
    return canonical

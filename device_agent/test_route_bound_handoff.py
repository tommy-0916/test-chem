"""Route-bound Research V2 must reach Device through one intact canonical view."""

from __future__ import annotations

import copy
import json
import sys

import pytest

from chem_agent_contracts.adapters import research_state_to_v2
from chem_agent_contracts.route_saved_state import build_selected_route_saved_state_v2
from chem_agent_contracts.test_route_saved_state import _rich_selected_route
from device_agent.run_from_research_state import (
    build_device_agent_input_package,
    device_input_package_to_text,
    main,
    validate_v2_research_handoff_consistency,
)
from device_agent.single_agent import SingleDeviceAgent


def _published_route_state():
    draft, decision, bundle = _rich_selected_route()
    saved = build_selected_route_saved_state_v2(
        draft, decision=decision, current_evidence_bundle=bundle,
        campaign_id="generic-route-device-test", observations=[],
    )
    saved["route_binding_status_v1"] = "publishable"
    for name in ("device_adaptation_handoff", "persistent_outputs"):
        saved[name]["route_binding_status_v1"] = "publishable"
    return saved


def _validated_direct_handoff(saved):
    canonical = validate_v2_research_handoff_consistency(
        saved, saved["macro_plan"]
    )
    return build_device_agent_input_package(
        saved, saved["macro_plan"], canonical_v2_package=canonical,
    )


def test_route_bound_cli_and_direct_entry_keep_canonical_material_facts():
    saved = _published_route_state()
    handoff = _validated_direct_handoff(saved)
    received = SingleDeviceAgent._validate_v2_raw_canonical_handoff(handoff)
    selected = saved["research_action_package_v2"]

    assert received["research_action_package_v2"] == selected
    assert received["route_binding_status_v1"] == "publishable"
    assert received["macro_action_steps"] == saved["macro_plan"]
    assert received["observations"] == saved["observations"]
    assert received["research_action_package_v2"]["route_binding"] == saved["route_binding"]
    assert received["research_action_package_v2"]["macro_steps"][0]["material_inputs"] == (
        selected["macro_steps"][0]["material_inputs"]
    )
    material = received["research_action_package_v2"]["macro_steps"][0]["material_inputs"][0]
    assert material["material_id"] == "nickel_salt"
    assert material["quantity"]["value"] == 2
    assert material["quantity"]["unit"] == "mmol"
    assert material["provenance"] == selected["macro_steps"][0]["material_inputs"][0]["provenance"]
    assert received["research_action_package_v2"]["route_binding"]["route_id"] == saved["route_binding"]["route_id"]
    assert received["research_action_package_v2"]["route_binding"]["experimental_group_id"] == "group-A"
    assert "route_published_research_state_v2" in handoff
    assert "route_published_research_state_v2" not in received
    assert "route_published_research_state_v2" not in device_input_package_to_text(handoff)


def test_route_bound_builder_rejects_unpublished_saved_state():
    draft, decision, bundle = _rich_selected_route()
    unpublished = build_selected_route_saved_state_v2(
        draft, decision=decision, current_evidence_bundle=bundle,
        campaign_id="generic-route-device-test", observations=[],
    )
    with pytest.raises(ValueError, match="requires a published Research state"):
        build_device_agent_input_package(
            unpublished, unpublished["macro_plan"],
            canonical_v2_package=unpublished["research_action_package_v2"],
        )


@pytest.mark.parametrize("missing", [
    "macro_plan", "observations", "research_action_package_v2",
    "route_binding", "route_binding_status_v1", "device_adaptation_handoff",
    "persistent_outputs",
])
def test_route_bound_cli_requires_every_published_saved_state_mirror(missing):
    saved = _published_route_state()
    saved.pop(missing)
    with pytest.raises(SystemExit, match="route-bound V2"):
        validate_v2_research_handoff_consistency(
            saved, saved.get("macro_plan", []),
        )


@pytest.mark.parametrize("mirror,key", [
    ("device_adaptation_handoff", "待执行 macro plan"),
    ("persistent_outputs", "待执行 macro plan"),
    ("device_adaptation_handoff", "observations"),
    ("persistent_outputs", "research_action_package_v2"),
    ("device_adaptation_handoff", "route_binding"),
    ("persistent_outputs", "route_binding_status_v1"),
])
def test_route_bound_cli_rejects_missing_nested_mirror(mirror, key):
    saved = _published_route_state()
    saved[mirror].pop(key)
    with pytest.raises(SystemExit, match="route-bound V2"):
        validate_v2_research_handoff_consistency(saved, saved["macro_plan"])


def test_route_bound_cli_rejects_quantity_change_even_if_raw_mirrors_agree():
    saved = _published_route_state()
    saved["macro_plan"][0]["material_inputs"][0]["quantity"] = {
        "mode": "exact", "semantic": "planned_target", "value": 999, "unit": "mmol",
    }
    for mirror in ("device_adaptation_handoff", "persistent_outputs"):
        saved[mirror]["待执行 macro plan"] = copy.deepcopy(saved["macro_plan"])
    with pytest.raises(SystemExit, match="raw/canonical|canonical package"):
        validate_v2_research_handoff_consistency(saved, saved["macro_plan"])


def test_route_bound_cli_rejects_observation_change_even_if_mirrors_agree():
    saved = _published_route_state()
    saved["observations"].append({"observation_id": "forged"})
    for mirror in ("device_adaptation_handoff", "persistent_outputs"):
        saved[mirror]["observations"] = copy.deepcopy(saved["observations"])
    with pytest.raises(SystemExit, match="canonical package"):
        validate_v2_research_handoff_consistency(saved, saved["macro_plan"])


@pytest.mark.parametrize("missing", [
    "macro_action_steps", "observations", "route_binding_status_v1",
    "route_published_research_state_v2",
])
def test_route_bound_direct_entry_requires_raw_transport_and_publish_marker(missing):
    handoff = _validated_direct_handoff(_published_route_state())
    handoff.pop(missing)
    with pytest.raises(ValueError, match="route-bound V2"):
        SingleDeviceAgent._validate_v2_raw_canonical_handoff(handoff)


def test_route_bound_direct_entry_rejects_raw_quantity_tamper():
    handoff = _validated_direct_handoff(_published_route_state())
    handoff["macro_action_steps"][0]["material_inputs"][0]["quantity"]["value"] = 999
    with pytest.raises(ValueError, match="raw steps differ from published Research state"):
        SingleDeviceAgent._validate_v2_raw_canonical_handoff(handoff)


def test_self_reported_publishable_annotated_draft_cannot_enter_device():
    draft, decision, bundle = _rich_selected_route()
    unpublished = build_selected_route_saved_state_v2(
        draft, decision=decision, current_evidence_bundle=bundle,
        campaign_id="generic-route-device-test", observations=[],
    )
    handoff = {
        "contract_version": "v2",
        "research_action_package_v2": copy.deepcopy(unpublished["research_action_package_v2"]),
        "macro_action_steps": copy.deepcopy(unpublished["macro_plan"]),
        "observations": copy.deepcopy(unpublished["observations"]),
        "route_binding_status_v1": "publishable",
    }
    with pytest.raises(ValueError, match="complete published Research state"):
        SingleDeviceAgent._validate_v2_raw_canonical_handoff(handoff)
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    agent._contract_version = "v2"
    agent._default_exp_id = lambda: "route-bound-test"
    agent._strip_untrusted_approval_fields = lambda value: (value, {})
    with pytest.raises(ValueError, match="complete published Research state"):
        agent.run_state(handoff)


def test_route_bound_direct_entry_rechecks_saved_state_mirrors():
    handoff = _validated_direct_handoff(_published_route_state())
    handoff["route_published_research_state_v2"]["persistent_outputs"].pop(
        "research_action_package_v2"
    )
    with pytest.raises(ValueError, match="published Research state failed validation"):
        SingleDeviceAgent._validate_v2_raw_canonical_handoff(handoff)


def test_run_state_rejects_route_bound_missing_observations_before_device_work():
    handoff = _validated_direct_handoff(_published_route_state())
    handoff.pop("observations")
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    agent._contract_version = "v2"
    agent._default_exp_id = lambda: "route-bound-test"
    agent._strip_untrusted_approval_fields = lambda value: (value, {})
    with pytest.raises(ValueError, match="route-bound V2 Device entry lacks raw observations"):
        agent.run_state(handoff)


def test_route_bound_direct_entry_cannot_downgrade_to_v1():
    handoff = _validated_direct_handoff(_published_route_state())
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    agent._contract_version = "v1"
    agent._default_exp_id = lambda: "route-bound-test"
    agent._strip_untrusted_approval_fields = lambda value: (value, {})
    with pytest.raises(ValueError, match="requires V2 Device entry"):
        agent.run_state(handoff)


def test_route_bound_cli_cannot_downgrade_to_v1(tmp_path, monkeypatch):
    path = tmp_path / "published-route.json"
    path.write_text(json.dumps(_published_route_state()), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "run_from_research_state.py", "--research-state", str(path),
        "--contract-version", "v1",
    ])
    with pytest.raises(SystemExit, match="requires V2 Device entry"):
        main()


def test_unbound_historical_v2_keeps_previous_device_entry_contract():
    saved = _published_route_state()
    for key in ("route_binding", "route_binding_status_v1"):
        saved.pop(key, None)
    for name in ("device_adaptation_handoff", "persistent_outputs"):
        for key in ("route_binding", "route_binding_status_v1"):
            saved[name].pop(key, None)
    historical = research_state_to_v2(saved).model_dump(mode="json", exclude_none=True)
    for container in (saved, saved["device_adaptation_handoff"], saved["persistent_outputs"]):
        container["research_action_package_v2"] = copy.deepcopy(historical)
    handoff = _validated_direct_handoff(saved)
    handoff.pop("observations")
    # Historical V2 handoffs may omit an explicit empty observations mirror;
    # the canonical observation digest still protects the reconstructed value.
    result = SingleDeviceAgent._validate_v2_raw_canonical_handoff(handoff)
    assert result["research_action_package_v2"].get("route_binding") is None

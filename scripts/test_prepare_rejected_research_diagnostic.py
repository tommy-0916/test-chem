"""Safety and compatibility checks for rejected Research diagnostic replay."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from device_agent.run_from_research_state import (
    build_device_agent_input_package,
    extract_macro_plan,
)
from scripts.prepare_rejected_research_diagnostic import (
    DiagnosticPreparationError,
    prepare_state,
    write_diagnostic,
)


def _source():
    return {
        "contract_version": "v2",
        "status": "manual_required",
        "failure_category": "macro_quality_error",
        "campaign_id": "local-campaign-not-a-lab-task",
        "event": {"query": "A02; send to 303 only after authorization"},
        "macro_plan": [],
        "research_action_package_v2": {},
        "current_evidence_bundle": {"items": [{"id": "paper-1"}]},
        "knowledge_hits": [{"title": "paper"}],
        "raw_llm_outputs": {
            "macro_action_design_bootstrap": {"objective": "candidate"},
            "macro_plan_design_retry_1": {
                "current_stage_plan": "One stage; quantities unresolved",
                "macro_plan": [{"macro_step_id": "step-1", "操作": "candidate"}],
            },
        },
    }


def test_prepared_state_is_diagnostic_only_and_v1_compatible():
    source = _source()
    original = copy.deepcopy(source)
    state = prepare_state(source)
    assert source == original
    assert state["contract_version"] == "v1"
    assert state["status"] == "manual_required"
    assert state["diagnostic_forced_continue"]["formal_v2_validated"] is False
    assert state["diagnostic_forced_continue"]["real_dispatch"] is False
    assert "research_action_package_v2" not in state
    assert extract_macro_plan(state) == source["raw_llm_outputs"][
        "macro_plan_design_retry_1"
    ]["macro_plan"]
    package = build_device_agent_input_package(state, extract_macro_plan(state))
    assert package["contract_version"] == "v1"
    assert package["task"]["current_stage_plan"] == "One stage; quantities unresolved"
    assert package["macro_action"]["diagnostic_forced_continue"][
        "dispatchable"
    ] is False


@pytest.mark.parametrize("status", ["completed", "running", "failed", None])
def test_only_manual_required_source_is_eligible(status):
    source = _source()
    source["status"] = status
    with pytest.raises(DiagnosticPreparationError, match="manual_required"):
        prepare_state(source)


def test_refuses_state_with_formally_accepted_plan_or_package():
    source = _source()
    source["device_adaptation_handoff"] = {"macro_plan": [{"step": 1}]}
    with pytest.raises(DiagnosticPreparationError, match="already has accepted"):
        prepare_state(source)
    source = _source()
    source["research_action_package_v2"] = {"schema_version": "2"}
    with pytest.raises(DiagnosticPreparationError, match="nonempty Research V2"):
        prepare_state(source)


@pytest.mark.parametrize(
    "mirror",
    [
        "device_adaptation_handoff",
        "persistent_outputs",
        "A. research layer 内部持久化输出",
        "B. 发给下游 device adaptation layer agent 的外部交接输出",
    ],
)
def test_refuses_nested_v2_package_mirror(mirror):
    source = _source()
    source[mirror] = {"research_action_package_v2": {"schema_version": "v2"}}
    with pytest.raises(DiagnosticPreparationError, match="research_action_package_v2"):
        prepare_state(source)


def test_refuses_missing_candidate_and_does_not_create_output(tmp_path):
    source = _source()
    source["raw_llm_outputs"]["macro_plan_design_retry_1"]["macro_plan"] = []
    source_path = tmp_path / "source.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    target = tmp_path / "new-diagnostic"
    with pytest.raises(DiagnosticPreparationError, match="nonempty steps"):
        write_diagnostic(source_path, target)
    assert not target.exists()


def test_manifest_hashes_and_unique_directory(tmp_path):
    source_path = tmp_path / "source.json"
    source_bytes = json.dumps(_source(), ensure_ascii=False).encode("utf-8")
    source_path.write_bytes(source_bytes)
    output_dir = tmp_path / "run-1"
    state_path, manifest_path = write_diagnostic(source_path, output_dir)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert source_path.read_bytes() == source_bytes
    assert manifest["source_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["diagnostic_forced_continue"]["source_sha256"] == manifest[
        "source_sha256"
    ]
    assert manifest["source_status"] == "manual_required"
    assert manifest["candidate_step_count"] == 1
    assert manifest["runtime_contract"] == "v1_compat"
    assert manifest["real_dispatch"] is False
    assert manifest["dispatchable"] is False
    assert state["macro_plan"]
    with pytest.raises(FileExistsError):
        write_diagnostic(source_path, output_dir)

from __future__ import annotations

from device_agent.single_agent import SingleDeviceAgent
from device_agent.v2_validation import (
    chunk_hashes,
    device_step_hashes,
    locked_chunk_violations,
    merge_scoped_device_step_repair,
)


def test_v2_policy_is_ten_targeted_rounds(monkeypatch):
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    agent._contract_version = "v2"
    agent._configured_workflow_repair_limit = None
    monkeypatch.setenv("CHEM_DEVICE_WORKFLOW_REPAIR_LIMIT", "99")
    monkeypatch.setenv("CHEM_DEVICE_WORKFLOW_VERIFICATION", "llm")
    monkeypatch.setenv("CHEM_DEVICE_TRANSLATION_CHUNK_SIZE", "8")
    assert agent._workflow_repair_limit() == 10
    assert agent._workflow_verification_mode() == "deterministic"
    assert agent._translation_chunk_size() == 1


def test_unchanged_chunks_are_hash_locked():
    before = {
        0: {"steps": [{"device_step_id": "DS_1", "parameters": {"x": 1}}]},
        1: {"steps": [{"device_step_id": "DS_2", "parameters": {"x": 2}}]},
    }
    expected = {"chunk_0": chunk_hashes(before)["chunk_0"]}
    repaired = {
        0: before[0],
        1: {"steps": [{"device_step_id": "DS_2", "parameters": {"x": 3}}]},
    }
    assert locked_chunk_violations(expected, chunk_hashes(repaired)) == []
    repaired[0] = {"steps": [{"device_step_id": "DS_1", "parameters": {"x": 9}}]}
    assert locked_chunk_violations(expected, chunk_hashes(repaired))


def test_scoped_repair_replaces_only_reported_device_step():
    previous = [
        {"device_step_id": "DS_1", "parameters": {"x": 1}},
        {"device_step_id": "DS_2", "parameters": {"x": 2}},
    ]
    candidate = [
        {"device_step_id": "DS_1", "parameters": {"x": 99}},
        {"device_step_id": "DS_2", "parameters": {"x": 3}},
    ]
    locked = device_step_hashes(previous, exclude={"DS_2"})
    merged, errors = merge_scoped_device_step_repair(previous, candidate, {"DS_2"})
    assert errors == []
    assert merged[0] == previous[0]
    assert merged[1] == candidate[1]
    assert locked_chunk_violations(locked, device_step_hashes(merged)) == []

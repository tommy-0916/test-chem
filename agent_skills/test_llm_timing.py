"""Offline request-timing telemetry tests."""

from __future__ import annotations

import json

import pytest

from agent_skills.llm_timing import measure_llm_request


def test_timing_counts_success_and_keeps_failure_separate(tmp_path, monkeypatch):
    output = tmp_path / "timing.jsonl"
    monkeypatch.setenv("CHEM_LLM_TIMING_JSONL", str(output))

    with measure_llm_request(
        component="research", model="test", transport="responses"
    ):
        pass
    with pytest.raises(TimeoutError):
        with measure_llm_request(
            component="device", model="test", transport="responses"
        ):
            raise TimeoutError("simulated stall")

    events = [json.loads(line) for line in output.read_text().splitlines()]
    assert [event["status"] for event in events] == [
        "started", "success", "started", "failed",
    ]
    completed = [event for event in events if event["status"] in {"success", "failed"}]
    assert completed[0]["counted_llm_seconds"] >= 0
    assert completed[1]["counted_llm_seconds"] == 0
    assert completed[1]["error_type"] == "TimeoutError"


def test_timing_file_contains_metadata_only(tmp_path, monkeypatch):
    output = tmp_path / "timing.jsonl"
    monkeypatch.setenv("CHEM_LLM_TIMING_JSONL", str(output))

    with measure_llm_request(
        component="device", model="test-model", transport="chat_native_tools"
    ) as timing:
        timing.observe("response.in_progress")
        timing.observe("response.completed")

    raw = output.read_text()
    assert "PRIVATE_PROMPT" not in raw
    event = json.loads(raw.splitlines()[-1])
    assert event["last_event_type"] == "response.completed"
    assert event["status"] == "success"


def test_timing_rejects_provider_controlled_event_type(tmp_path, monkeypatch):
    output = tmp_path / "timing.jsonl"
    monkeypatch.setenv("CHEM_LLM_TIMING_JSONL", str(output))
    canary = "sk-kimi-PRIVATE_PROMPT/" + "X" * 10000

    with measure_llm_request(
        component="device", model="test", transport="responses"
    ) as timing:
        timing.observe(canary)

    raw = output.read_text()
    assert "PRIVATE_PROMPT" not in raw
    assert len(raw) < 5000
    event = json.loads(raw.splitlines()[-1])
    assert event["last_event_type"] == "response.unknown"

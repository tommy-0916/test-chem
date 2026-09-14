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
    assert [event["status"] for event in events] == ["success", "failed"]
    assert events[0]["counted_llm_seconds"] >= 0
    assert events[1]["counted_llm_seconds"] == 0
    assert events[1]["error_type"] == "TimeoutError"

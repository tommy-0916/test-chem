"""Offline tests for the fragment replay evidence tool."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fragment_replay
from feasibility_fragments import merge_fragment


MACROS = ["10", "20"]
MATRIX = [{"sample_id": "A", "role": "control"}]


def step(number, source):
    return {
        "plan_step": number,
        "workstation": "Alpha",
        "source_macro_step": source,
        "source_macro_steps": [source],
        "key_values": {"temperature": "120 C"},
        "containers": {"容器类型": "进样瓶", "容器编号": [1]},
        "source_material_identity_ids": ["frozen_identity"],
    }


def fragment(source, number, **extra):
    return {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": [step(number, source)],
        **extra,
    }


def attempt_record(attempt, step_name, current, ok, *, prefix=None, fragment_value=None, error=None):
    record = {
        "attempt": attempt,
        "step_name": step_name,
        "current_macro_id": current,
        "prefix_sha256": fragment_replay._digest(prefix) if isinstance(prefix, dict) else None,
        "ok": ok,
    }
    if prefix is not None:
        record["prefix"] = prefix
        record["matrix"] = MATRIX
        record["all_macro_ids"] = MACROS
    if fragment_value is not None:
        record["fragment"] = fragment_value
    if error:
        record["error"] = error
    return record


def recorded_attempts():
    frag1 = fragment("10", 1)
    prefix1 = merge_fragment({}, frag1, "10", MACROS, MATRIX)
    bad = fragment("20", 2)
    bad["device_feasible"] = True
    frag2 = fragment("20", 2)
    return [
        attempt_record(1, "feasibility_device_plan_chunk_1_of_2", "10", True, prefix={}, fragment_value=frag1),
        attempt_record(1, "feasibility_device_plan_chunk_2_of_2", "20", False, prefix=prefix1, fragment_value=bad,
                       error={"code": "UNKNOWN_FIELDS", "path": "fragment", "message": "unsupported fields", "details": {"fields": ["device_feasible"]}}),
        attempt_record(2, "feasibility_device_plan_chunk_2_of_2", "20", True, fragment_value=frag2),
    ]


def test_replay_reproduces_recorded_outcomes_and_chain():
    report = fragment_replay.replay_progress(
        {"status": "assembled_pending_global_audit", "fragment_attempts": recorded_attempts()}
    )
    assert report["chain_matches"] is True
    assert report["summary"]["attempts_replayed"] == 3
    assert report["summary"]["record_mismatches"] == 0
    chunk1, chunk2 = report["chunks"]
    assert chunk1["attempts"][0]["replay_ok"] is True
    assert chunk1["chunk_prefix_matches_running_chain"] is None  # chain starts at chunk 1
    failed = chunk2["attempts"][0]
    assert failed["replay_ok"] is False
    assert failed["replay_error"]["code"] == "UNKNOWN_FIELDS"
    assert failed["matches_record"] is True
    assert chunk2["attempts"][1]["replay_ok"] is True
    assert chunk2["chunk_prefix_matches_running_chain"] is True


def test_replay_flags_tampered_recorded_outcome():
    attempts = recorded_attempts()
    attempts[1]["ok"] = True  # the recorded run now claims the bad fragment was accepted
    attempts[1].pop("error")
    report = fragment_replay.replay_progress({"status": "failed", "fragment_attempts": attempts})
    assert report["summary"]["record_mismatches"] == 1
    mismatch = report["summary"]["mismatches"][0]
    assert mismatch["recorded_ok"] is True
    assert mismatch["replay_ok"] is False


def test_replay_surfaces_whole_batch_consumer_identities():
    root = {
        "batch_id": "whole_root", "quantity_mode": "whole_batch",
        "material_identity_id": "frozen_identity", "sample_id": "A",
        "is_root_batch": True, "consumer_ids": ["op_a"],
        "source_macro_steps": ["10"], "source_plan_steps": [1],
    }
    prefix1 = merge_fragment({}, fragment("10", 1, batch_plan=[root]), "10", MACROS, MATRIX)
    conflict = fragment("20", 2, prior_record_updates=[
        {"table": "batch_plan", "id": "whole_root", "consumer_ids": ["op_b"]},
    ])
    attempts = [
        attempt_record(1, "feasibility_device_plan_chunk_1_of_2", "10", True, prefix={}, fragment_value=fragment("10", 1, batch_plan=[root])),
        attempt_record(1, "feasibility_device_plan_chunk_2_of_2", "20", False, prefix=prefix1, fragment_value=conflict,
                       error={"code": "CONFLICTING_RECORD", "path": "prior_record_updates[0]", "message": "whole_batch may have at most one total consumer", "details": {}}),
    ]
    report = fragment_replay.replay_progress({"status": "failed", "fragment_attempts": attempts})
    assert report["summary"]["consumer_conflicts"] == [{
        "step_name": "feasibility_device_plan_chunk_2_of_2",
        "attempt": 1,
        "batch_id": "whole_root",
        "existing_consumers": ["op_a"],
        "added_consumers": ["op_b"],
    }]


def test_replay_detects_prefix_chain_drift():
    attempts = recorded_attempts()
    drifted = copy.deepcopy(attempts[1]["prefix"])
    drifted.setdefault("batch_plan", []).append(
        {"batch_id": "ghost", "quantity_mode": "whole_batch", "consumer_ids": []}
    )
    attempts[1]["prefix"] = drifted
    attempts[1]["prefix_sha256"] = fragment_replay._digest(drifted)
    report = fragment_replay.replay_progress({"status": "failed", "fragment_attempts": attempts})
    assert report["chain_matches"] is False
    assert report["chunks"][1]["chunk_prefix_matches_running_chain"] is False


def test_replay_state_accepts_saved_device_state_shape():
    state = {"feasibility_progress": [{"status": "failed", "fragment_attempts": recorded_attempts()}]}
    reports = fragment_replay.replay_state(state)
    assert reports[0]["progress_index"] == 0
    assert reports[0]["summary"]["attempts_replayed"] == 3
    json.dumps(reports, ensure_ascii=False)


def test_replay_reports_incomplete_context_instead_of_guessing():
    attempts = recorded_attempts()
    del attempts[0]["matrix"]  # old capture without replay context
    report = fragment_replay.replay_progress({"status": "failed", "fragment_attempts": attempts})
    assert "error" in report["chunks"][0]
    assert report["chain_matches"] is False

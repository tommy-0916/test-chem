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


def attempt_record(
    attempt,
    step_name,
    current,
    ok,
    *,
    prefix=None,
    fragment_value=None,
    error=None,
    macro_ids=None,
):
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
        record["all_macro_ids"] = MACROS if macro_ids is None else macro_ids
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


def recorded_progress_with_integrity():
    """Return the current runtime evidence shape, including all digest layers."""

    attempts = recorded_attempts()
    for item in attempts:
        item["fragment_sha256"] = (
            f"sha256_{fragment_replay._digest(item['fragment'])}"
        )
    first_candidate = merge_fragment(
        {}, attempts[0]["fragment"], "10", MACROS, MATRIX
    )
    final_candidate = merge_fragment(
        first_candidate, attempts[2]["fragment"], "20", MACROS, MATRIX
    )
    return {
        "status": "assembled_pending_global_audit",
        "fragment_attempts": attempts,
        "completed_chunks": [
            {
                "macro_id": "10",
                "step_name": "feasibility_device_plan_chunk_1_of_2",
                "fragment_digest": (
                    f"sha256_{fragment_replay._digest(attempts[0]['fragment'])}"
                ),
                "candidate_digest": (
                    f"sha256_{fragment_replay._digest(first_candidate)}"
                ),
            },
            {
                "macro_id": "20",
                "step_name": "feasibility_device_plan_chunk_2_of_2",
                "fragment_digest": (
                    f"sha256_{fragment_replay._digest(attempts[2]['fragment'])}"
                ),
                "candidate_digest": (
                    f"sha256_{fragment_replay._digest(final_candidate)}"
                ),
            },
        ],
        "candidate": final_candidate,
    }


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


def test_replay_keeps_legacy_records_without_digest_sidecars_compatible():
    attempts = recorded_attempts()
    for item in attempts:
        item.pop("prefix_sha256", None)

    report = fragment_replay.replay_progress(
        {"status": "assembled_pending_global_audit", "fragment_attempts": attempts}
    )

    assert report["chain_matches"] is True
    assert report["candidate_matches_running_chain"] is None
    assert report["summary"]["record_mismatches"] == 0


def test_replay_validates_current_attempt_completion_and_candidate_digests():
    report = fragment_replay.replay_progress(recorded_progress_with_integrity())

    assert report["chain_matches"] is True
    assert report["candidate_matches_running_chain"] is True
    assert report["summary"]["record_mismatches"] == 0
    assert all(
        item["recorded_fragment_digest_valid"] is True
        for chunk in report["chunks"]
        for item in chunk["attempts"]
    )
    assert all(
        chunk["completed_chunk"]["recorded_fragment_digest_valid"] is True
        and chunk["completed_chunk"]["recorded_candidate_digest_valid"] is True
        for chunk in report["chunks"]
    )


def test_replay_rejects_tampered_last_accepted_fragment_even_when_mergeable():
    progress = recorded_progress_with_integrity()
    accepted = progress["fragment_attempts"][-1]["fragment"]
    accepted["device_plan"][0]["key_values"]["temperature"] = "130 C"

    report = fragment_replay.replay_progress(progress)

    assert report["chunks"][-1]["attempts"][-1]["replay_ok"] is True
    assert report["chunks"][-1]["attempts"][-1]["matches_record"] is True
    assert report["chunks"][-1]["attempts"][-1][
        "recorded_fragment_digest_valid"
    ] is False
    assert report["chunks"][-1]["completed_chunk"][
        "recorded_fragment_digest_valid"
    ] is False
    assert report["chunks"][-1]["completed_chunk"][
        "recorded_candidate_digest_valid"
    ] is False
    assert report["candidate_matches_running_chain"] is False
    assert report["chain_matches"] is False
    assert report["summary"]["record_mismatches"] == 4


def test_replay_rejects_tampered_failed_fragment_even_if_failure_replays():
    progress = recorded_progress_with_integrity()
    rejected = progress["fragment_attempts"][1]
    rejected["fragment"]["device_feasible"] = "still-an-unknown-field"

    report = fragment_replay.replay_progress(progress)

    failed = report["chunks"][1]["attempts"][0]
    assert failed["replay_ok"] is False
    assert failed["replay_error"]["code"] == "UNKNOWN_FIELDS"
    assert failed["matches_record"] is True
    assert failed["recorded_fragment_digest_valid"] is False
    assert report["candidate_matches_running_chain"] is True
    assert report["chain_matches"] is False
    assert report["summary"]["record_mismatches"] == 1


def test_replay_rejects_tampered_completed_candidate_digest():
    progress = recorded_progress_with_integrity()
    progress["completed_chunks"][-1]["candidate_digest"] = "sha256_deadbeef"

    report = fragment_replay.replay_progress(progress)

    assert report["chunks"][-1]["completed_chunk"][
        "recorded_candidate_digest_valid"
    ] is False
    assert report["candidate_matches_running_chain"] is True
    assert report["chain_matches"] is False
    assert report["summary"]["record_mismatches"] == 1


def test_replay_requires_completion_for_each_accepted_chunk_when_sidecar_exists():
    report = fragment_replay.replay_progress({
        "status": "assembled_pending_global_audit",
        "fragment_attempts": recorded_attempts(),
        "completed_chunks": [],
    })

    assert report["chain_matches"] is False
    assert report["summary"]["record_mismatches"] == 2
    assert {
        item["mismatch_type"] for item in report["summary"]["mismatches"]
    } == {"missing_completed_chunk"}


def test_replay_rejects_unconsumed_ghost_completion_record():
    progress = recorded_progress_with_integrity()
    progress["completed_chunks"].append({
        "macro_id": "ghost",
        "step_name": "feasibility_device_plan_chunk_99_of_99",
        "fragment_digest": f"sha256_{'0' * 64}",
        "candidate_digest": f"sha256_{'0' * 64}",
    })

    report = fragment_replay.replay_progress(progress)

    assert report["chain_matches"] is False
    assert report["candidate_matches_running_chain"] is True
    assert report["summary"]["record_mismatches"] == 1
    assert report["summary"]["mismatches"][0]["mismatch_type"] == (
        "unexpected_completed_chunk"
    )


def test_replay_rejects_modern_completion_record_without_identity():
    progress = recorded_progress_with_integrity()
    progress["completed_chunks"].append({
        "fragment_digest": f"sha256_{'0' * 64}",
        "candidate_digest": f"sha256_{'0' * 64}",
    })

    report = fragment_replay.replay_progress(progress)

    assert report["chain_matches"] is False
    assert report["summary"]["record_mismatches"] == 1
    assert report["summary"]["mismatches"][0]["mismatch_type"] == (
        "invalid_completed_chunk_identity"
    )


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


def test_replay_detects_tampered_recorded_prefix_snapshot():
    attempts = recorded_attempts()
    attempts[1]["prefix"]["macro_plan_summary"] = "tampered snapshot"
    # Keep the original digest: replay must verify the stored snapshot itself,
    # not merely compare the running chain to the old digest.

    report = fragment_replay.replay_progress(
        {"status": "failed", "fragment_attempts": attempts}
    )

    assert report["chain_matches"] is False
    assert report["chunks"][1]["recorded_prefix_digest_valid"] is False


def test_replay_accepts_runtime_prefixed_prefix_digest():
    attempts = recorded_attempts()
    bare = attempts[1]["prefix_sha256"]
    attempts[1]["prefix_sha256"] = f"sha256_{bare}"

    report = fragment_replay.replay_progress(
        {"status": "assembled_pending_global_audit", "fragment_attempts": attempts}
    )

    assert report["chain_matches"] is True
    assert report["chunks"][1]["chunk_prefix_matches_running_chain"] is True


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


def test_replay_preserves_zero_macro_id_and_advances_chain():
    macro_ids = [0, 2]
    fragment_zero = fragment(0, 1)
    prefix_after_zero = merge_fragment({}, fragment_zero, 0, macro_ids, MATRIX)
    fragment_two = fragment(2, 2)
    attempts = [
        attempt_record(
            1,
            "feasibility_device_plan_chunk_1_of_2",
            0,
            True,
            prefix={},
            fragment_value=fragment_zero,
            macro_ids=macro_ids,
        ),
        attempt_record(
            1,
            "feasibility_device_plan_chunk_2_of_2",
            2,
            True,
            prefix=prefix_after_zero,
            fragment_value=fragment_two,
            macro_ids=macro_ids,
        ),
    ]

    report = fragment_replay.replay_progress(
        {"status": "assembled_pending_global_audit", "fragment_attempts": attempts}
    )

    assert report["chain_matches"] is True
    assert report["summary"]["attempts_replayed"] == 2
    assert report["summary"]["record_mismatches"] == 0
    assert report["chunks"][0]["attempts"][0]["replay_ok"] is True
    assert report["chunks"][1]["chunk_prefix_matches_running_chain"] is True


def test_replay_keeps_integer_and_string_macro_ids_distinct():
    macro_ids = [1, "1"]
    integer_fragment = fragment(1, 1)
    prefix_after_integer = merge_fragment({}, integer_fragment, 1, macro_ids, MATRIX)
    string_fragment = fragment("1", 2)
    attempts = [
        attempt_record(
            1,
            "feasibility_device_plan_chunk_1_of_2",
            1,
            True,
            prefix={},
            fragment_value=integer_fragment,
            macro_ids=macro_ids,
        ),
        attempt_record(
            1,
            "feasibility_device_plan_chunk_2_of_2",
            "1",
            True,
            prefix=prefix_after_integer,
            fragment_value=string_fragment,
            macro_ids=macro_ids,
        ),
    ]

    report = fragment_replay.replay_progress(
        {"status": "assembled_pending_global_audit", "fragment_attempts": attempts}
    )

    assert report["chain_matches"] is True
    assert report["summary"]["attempts_replayed"] == 2
    assert report["summary"]["record_mismatches"] == 0
    assert all(
        item["replay_ok"] is True
        for chunk in report["chunks"]
        for item in chunk["attempts"]
    )


def test_replay_fails_closed_on_invalid_macro_identity_context():
    bad_context = attempt_record(
        1,
        "feasibility_device_plan_chunk_1_of_1",
        1,
        True,
        prefix={},
        fragment_value=fragment(1, 1),
        macro_ids=[{"not": "a scalar"}],
    )

    report = fragment_replay.replay_progress(
        {"status": "failed", "fragment_attempts": [bad_context]}
    )

    assert report["chain_matches"] is False
    assert "INVALID_MACRO_ID" in report["chunks"][0]["error"]
    assert report["summary"]["attempts_replayed"] == 0

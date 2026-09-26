from __future__ import annotations

import pytest

from device_agent.single_agent import SingleDeviceAgent
from device_agent.v2_validation import (
    build_validation_issues_v2,
    chunk_hashes,
    device_step_hashes,
    device_step_hashes_for_chunks,
    locked_chunk_violations,
    merge_scoped_device_step_repair,
    namespaced_lock_hashes,
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


@pytest.mark.parametrize("case", ["add", "omit", "split"])
def test_scoped_repair_rejects_node_set_changes_outside_authorized_ids(case):
    previous = [
        {"device_step_id": "DS_LOCKED", "parameters": {"x": 1}},
        {"device_step_id": "DS_MUTABLE", "parameters": {"x": 2}},
    ]
    if case == "add":
        candidate = [
            previous[0],
            {"device_step_id": "DS_MUTABLE", "parameters": {"x": 3}},
            {"device_step_id": "DS_NEW", "parameters": {"x": 4}},
        ]
    elif case == "omit":
        candidate = [previous[0]]
    else:
        candidate = [
            previous[0],
            {"device_step_id": "DS_SPLIT_A", "parameters": {"x": 3}},
            {"device_step_id": "DS_SPLIT_B", "parameters": {"x": 4}},
        ]

    merged, errors = merge_scoped_device_step_repair(
        previous, candidate, {"DS_MUTABLE"}
    )

    assert errors
    assert merged[0] == previous[0]
    if case in {"omit", "split"}:
        assert any("omitted authorized" in error for error in errors)
        assert merged[1] == previous[1]
    if case in {"add", "split"}:
        assert any("unauthorized" in error for error in errors)


@pytest.mark.parametrize("bad_id", [1, 1.0, True, None, ""])
def test_device_step_hashes_rejects_non_string_or_empty_ids(bad_id):
    with pytest.raises(ValueError, match="device_step_id"):
        device_step_hashes([{"device_step_id": bad_id, "parameters": {}}])


def test_device_step_hashes_rejects_duplicate_ids_before_last_wins_overwrite():
    with pytest.raises(ValueError, match="duplicate device_step_id"):
        device_step_hashes(
            [
                {"device_step_id": "DS_DUP", "parameters": {"x": 1}},
                {"device_step_id": "DS_DUP", "parameters": {"x": 2}},
            ]
        )


def test_full_chunk_cache_hashing_rejects_duplicate_device_ids_across_chunks():
    with pytest.raises(ValueError, match="duplicate device_step_id"):
        device_step_hashes_for_chunks(
            {
                0: {
                    "steps": [
                        {"device_step_id": "DS_DUP", "parameters": {"x": 1}}
                    ]
                },
                1: {
                    "steps": [
                        {"device_step_id": "DS_DUP", "parameters": {"x": 2}}
                    ]
                },
            }
        )


def test_combined_repair_locks_namespace_chunk_keys_from_device_ids():
    before = {
        0: {"steps": [{"device_step_id": "DS_LOCKED", "parameters": {"x": 1}}]},
        1: {"steps": [{"device_step_id": "chunk_0", "parameters": {"x": 2}}]},
    }
    expected = namespaced_lock_hashes(
        chunk_digests={"chunk_0": chunk_hashes(before)["chunk_0"]},
        device_step_digests={
            "chunk_0": device_step_hashes_for_chunks(before)["chunk_0"]
        },
    )

    assert locked_chunk_violations(
        expected,
        namespaced_lock_hashes(
            chunk_digests=chunk_hashes(before),
            device_step_digests=device_step_hashes_for_chunks(before),
        ),
    ) == []

    changed = {
        0: {"steps": [{"device_step_id": "DS_LOCKED", "parameters": {"x": 9}}]},
        1: before[1],
    }
    violations = locked_chunk_violations(
        expected,
        namespaced_lock_hashes(
            chunk_digests=chunk_hashes(changed),
            device_step_digests=device_step_hashes_for_chunks(changed),
        ),
    )
    assert violations == [
        "chunk::chunk_0 changed outside the authorized repair scope"
    ]


def test_scoped_repair_is_atomic_on_typed_or_duplicate_device_ids():
    previous = [
        {"device_step_id": 1, "parameters": {"x": 1}},
        {"device_step_id": "1", "parameters": {"x": 2}},
    ]
    candidate = [
        {"device_step_id": "1", "parameters": {"x": 3}},
        {"device_step_id": "1", "parameters": {"x": 4}},
    ]

    merged, errors = merge_scoped_device_step_repair(previous, candidate, {"1"})

    assert merged == [previous[0], previous[1]]
    assert any("nonempty string" in error for error in errors)
    assert any("duplicate device_step_id" in error for error in errors)


def _validation_error(step_number: int) -> str:
    return (
        f"第 {step_number} 步（物料站/物料拿取）参数 `未知参数` "
        "不在该工作站可下发参数中（unknown_parameter）。"
    )


def test_validation_issues_preserve_zero_leading_zero_and_typed_macro_ids():
    workflow = {
        "steps": [
            {
                "step_number": 1,
                "device_step_id": "DS_0",
                "source_macro_step_id": 0,
            },
            {
                "step_number": 2,
                "device_step_id": "DS_INT",
                "source_macro_step_id": 1,
            },
            {
                "step_number": 3,
                "device_step_id": "DS_STR",
                "source_macro_step_id": "1",
            },
            {
                "step_number": 4,
                "device_step_id": "DS_PADDED",
                "source_macro_step_id": "001",
            },
        ]
    }

    issues = build_validation_issues_v2(
        [_validation_error(number) for number in range(1, 5)], workflow
    )

    assert [issue["macro_step_id"] for issue in issues] == [0, 1, "1", "001"]
    assert type(issues[1]["macro_step_id"]) is int
    assert type(issues[2]["macro_step_id"]) is str


def test_validation_issue_never_reinterprets_macro_action_as_macro_step():
    workflow = {
        "steps": [
            {
                "step_number": 1,
                "device_step_id": "DS_ACTION_ZERO",
                "macro_action_id": 0,
            },
            {
                "step_number": 2,
                "device_step_id": "DS_ACTION_PADDED",
                "macro_action_id": "001",
            },
        ]
    }

    issues = build_validation_issues_v2(
        [_validation_error(1), _validation_error(2)], workflow
    )

    assert [issue["macro_step_id"] for issue in issues] == ["", ""]


def test_cross_step_observations_do_not_become_v2_mutation_authority():
    workflow = {
        "steps": [
            {"step_number": 1, "device_step_id": "DS_A"},
            {"step_number": 2, "device_step_id": "DS_B"},
        ]
    }

    issue = build_validation_issues_v2(
        [
            {
                "type": "container_state_mismatch",
                "message": "A/B state continuity mismatch",
                "step_numbers": [1, 2],
            }
        ],
        workflow,
    )[0]

    assert issue["device_step_id"] == ""
    assert issue["repair_scope"] == "macro_step"
    assert issue["actual"] == {
        "observed_step_numbers": [1, 2],
        "observed_device_step_ids": ["DS_A", "DS_B"],
    }
    assert issue["expected"]["authorized_device_step_ids"] == []


def test_validation_issue_fails_closed_for_conflicting_step_identity_mirrors():
    workflow = {
        "steps": [
            {
                "step_number": 1,
                "device_step_id": "DS_CONFLICTING_ALIASES",
                "macro_step_id": 1,
                "logical_step_id": "1",
                "macro_action_id": "MA_SHARED",
            }
        ]
    }

    issue = build_validation_issues_v2([_validation_error(1)], workflow)[0]

    assert issue["macro_step_id"] == ""


def test_validation_issue_prefers_typed_step_source_over_lossy_error_text():
    workflow = {
        "steps": [
            {
                "step_number": 1,
                "device_step_id": "DS_TYPED",
                "source_macro_step_id": 1,
            }
        ]
    }
    error = _validation_error(1) + " source_macro_step=1"

    issue = build_validation_issues_v2([error], workflow)[0]

    assert issue["macro_step_id"] == 1
    assert type(issue["macro_step_id"]) is int


def test_validation_issue_fails_closed_for_invalid_or_conflicting_sources():
    workflow = {
        "steps": [
            {
                "step_number": 1,
                "device_step_id": "DS_INVALID",
                "source_macro_step_id": False,
                "macro_action_id": "must-not-be-guessed",
            },
            {
                "step_number": 2,
                "device_step_id": "DS_CONFLICT",
                "source_macro_step": 1,
                "source_macro_steps": ["1"],
                "macro_action_id": "must-not-be-guessed",
            },
            {
                "step_number": 3,
                "device_step_id": "DS_AMBIGUOUS",
                "source_macro_steps": ["left", "right"],
                "macro_action_id": "must-not-be-guessed",
            },
            {
                "step_number": 4,
                "device_step_id": "DS_NULL",
                "source_macro_step_id": None,
                "macro_step_id": 4,
            },
        ]
    }

    issues = build_validation_issues_v2(
        [_validation_error(number) for number in range(1, 5)], workflow
    )

    assert [issue["macro_step_id"] for issue in issues] == ["", "", "", ""]

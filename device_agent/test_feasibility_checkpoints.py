"""Offline checkpoint/resume regressions for fragmented Device planning."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import device_agent.checkpoints as checkpoints_module
from device_agent.checkpoints import DeviceCheckpointStore, digest as checkpoint_digest
from device_agent.run_from_research_state import build_parser, default_checkpoint_dir
from device_agent.single_agent import SingleDeviceAgent, SingleDeviceAgentState


def _handoff(count: int = 6) -> dict:
    return {
        "macro_action_steps": [
            {"步骤序号": index, "操作": f"offline macro {index}"}
            for index in range(1, count + 1)
        ],
        "sample_control_matrix": [
            {"sample_id": "sample-a", "condition": "frozen"}
        ],
    }


def _fragment(source: int, matrix: list[dict]) -> dict:
    return {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "sample_control_matrix": copy.deepcopy(matrix),
        "device_plan": [
            {
                "plan_step": source,
                "workstation": "Offline_Station",
                "source_macro_step": source,
                "source_macro_steps": [source],
                "objective": f"offline operation {source}",
            }
        ],
    }


def _agent(checkpoint_root: Path, binding: dict) -> SingleDeviceAgent:
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    agent._contract_version = "v2"
    agent._active_semantic_analysis = {
        "status": "semantic_analysis",
        "analysis_digest": "semantic_offline",
    }
    agent._checkpoint_store = DeviceCheckpointStore(
        checkpoint_root,
        binding,
        resume=True,
        metadata={"exp_id": "offline"},
    )
    agent._assert_workstation_snapshot_current = Mock()
    agent._sync_skill_load_state = Mock()
    return agent


def _state(count: int = 6) -> SingleDeviceAgentState:
    return SingleDeviceAgentState(
        research_handoff=_handoff(count),
        exp_id="offline",
        device_truth_sha256="truth_offline",
    )


def test_chunk_six_failure_resumes_contiguous_prefix_without_repeating_one_to_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHEM_DEVICE_FEASIBILITY_MODE", raising=False)
    binding = {
        "research_handoff_sha256": "research-v1",
        "semantic_analysis_sha256": "semantic-v1",
        "device_truth_sha256": "truth-v1",
        "implementation_sha256": "implementation-v1",
        "fragment_contract_sha256": "fragment-v1",
    }
    first_agent = _agent(tmp_path, binding)
    first_state = _state()
    first_calls: list[int] = []

    def fail_on_six(current: SingleDeviceAgentState, **kwargs: object) -> dict:
        index = len(first_calls) + 1
        first_calls.append(index)
        if index == 6:
            raise RuntimeError("bounded offline failure at chunk 6")
        return _fragment(index, current.research_handoff["sample_control_matrix"])

    first_agent._invoke_feasibility_request = fail_on_six
    with pytest.raises(RuntimeError, match="chunk 6"):
        first_agent._invoke_feasibility_plan(first_state)

    assert first_calls == [1, 2, 3, 4, 5, 6]
    assert first_state.feasibility_progress[-1]["status"] == "failed"
    assert len(first_state.feasibility_progress[-1]["completed_chunks"]) == 5
    assert first_state.feasibility_accepted is False
    assert first_state.workflow_json == {}

    resumed_agent = _agent(tmp_path, binding)
    resumed_state = _state()
    resumed_calls: list[str] = []

    def finish_six(current: SingleDeviceAgentState, **kwargs: object) -> dict:
        resumed_calls.append(str(kwargs["step_name"]))
        return _fragment(6, current.research_handoff["sample_control_matrix"])

    resumed_agent._invoke_feasibility_request = finish_six
    candidate = resumed_agent._invoke_feasibility_plan(resumed_state)

    assert resumed_calls == ["feasibility_device_plan_chunk_6_of_6"]
    assert len(candidate["device_plan"]) == 6
    assert len(resumed_state.feasibility_progress[-1]["completed_chunks"]) == 6
    assert resumed_state.feasibility_progress[-1]["status"] == (
        "assembled_pending_global_audit"
    )
    assert resumed_state.checkpoints["resumed_stages"] == [
        f"feasibility_device_plan_chunk_{index}_of_6"
        for index in range(1, 6)
    ]
    assert resumed_state.feasibility_accepted is False
    assert resumed_state.workflow_json == {}


@pytest.mark.parametrize(
    ("changed_key", "changed_value"),
    [
        ("research_handoff_sha256", "research-v2"),
        ("implementation_sha256", "implementation-v2"),
    ],
)
def test_research_or_code_binding_change_invalidates_fragment_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_key: str,
    changed_value: str,
) -> None:
    monkeypatch.delenv("CHEM_DEVICE_FEASIBILITY_MODE", raising=False)
    binding = {
        "research_handoff_sha256": "research-v1",
        "semantic_analysis_sha256": "semantic-v1",
        "device_truth_sha256": "truth-v1",
        "implementation_sha256": "implementation-v1",
        "fragment_contract_sha256": "fragment-v1",
    }
    first_agent = _agent(tmp_path, binding)
    first_state = _state(count=2)
    first_agent._invoke_feasibility_request = Mock(
        side_effect=[
            _fragment(1, first_state.research_handoff["sample_control_matrix"]),
            _fragment(2, first_state.research_handoff["sample_control_matrix"]),
        ]
    )
    first_agent._invoke_feasibility_plan(first_state)

    changed_binding = dict(binding)
    changed_binding[changed_key] = changed_value
    second_agent = _agent(tmp_path, changed_binding)
    second_state = _state(count=2)
    second_agent._invoke_feasibility_request = Mock(
        side_effect=[
            _fragment(1, second_state.research_handoff["sample_control_matrix"]),
            _fragment(2, second_state.research_handoff["sample_control_matrix"]),
        ]
    )
    second_agent._invoke_feasibility_plan(second_state)

    assert second_agent._invoke_feasibility_request.call_count == 2
    assert second_state.checkpoints["resumed_stages"] == []
    assert (
        first_agent._checkpoint_store.directory
        != second_agent._checkpoint_store.directory
    )


def test_semantic_binding_change_invalidates_fragment_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHEM_DEVICE_FEASIBILITY_MODE", raising=False)
    binding = {"research": "same", "implementation": "same"}
    first_agent = _agent(tmp_path, binding)
    first_state = _state(count=1)
    first_agent._invoke_feasibility_request = Mock(
        return_value=_fragment(
            1, first_state.research_handoff["sample_control_matrix"]
        )
    )
    first_agent._invoke_feasibility_plan(first_state)

    changed_agent = _agent(tmp_path, binding)
    changed_agent._active_semantic_analysis = {
        "status": "semantic_analysis",
        "analysis_digest": "semantic_changed",
    }
    changed_state = _state(count=1)
    changed_agent._invoke_feasibility_request = Mock(
        return_value=_fragment(
            1, changed_state.research_handoff["sample_control_matrix"]
        )
    )
    changed_agent._invoke_feasibility_plan(changed_state)

    changed_agent._invoke_feasibility_request.assert_called_once()
    assert changed_state.checkpoints["resumed_stages"] == []


def test_corrupt_payload_hash_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHEM_DEVICE_FEASIBILITY_MODE", raising=False)
    binding = {"research": "same", "implementation": "same"}
    first_agent = _agent(tmp_path, binding)
    first_state = _state(count=1)
    first_agent._invoke_feasibility_request = Mock(
        return_value=_fragment(
            1, first_state.research_handoff["sample_control_matrix"]
        )
    )
    first_agent._invoke_feasibility_plan(first_state)

    stage_path = next(
        first_agent._checkpoint_store.directory.glob(
            "feasibility_device_plan_chunk_1_of_1-*.json"
        )
    )
    document = json.loads(stage_path.read_text(encoding="utf-8"))
    document["payload"]["candidate"]["device_plan"][0]["objective"] = (
        "tampered without a matching payload hash"
    )
    # Simulate an old-but-well-formed envelope: the generic file hash passes,
    # while the fragment's domain-specific candidate hash remains stale.
    document["payload_sha256"] = checkpoint_digest(document["payload"])
    stage_path.write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    resumed_agent = _agent(tmp_path, binding)
    resumed_state = _state(count=1)
    resumed_agent._invoke_feasibility_request = Mock(
        return_value=_fragment(
            1, resumed_state.research_handoff["sample_control_matrix"]
        )
    )
    candidate = resumed_agent._invoke_feasibility_plan(resumed_state)

    resumed_agent._invoke_feasibility_request.assert_called_once()
    assert candidate["device_plan"][0]["objective"] == "offline operation 1"
    assert resumed_state.checkpoints["resumed_stages"] == []


def test_semantic_stage_is_atomic_and_only_success_is_reused(tmp_path: Path) -> None:
    binding = {"implementation_sha256": "offline-v1"}
    first_agent = _agent(tmp_path, binding)
    first_state = _state(count=1)

    with pytest.raises(RuntimeError, match="semantic failed"):
        first_agent._checkpoint_stage(
            first_state,
            "semantic_analysis",
            first_state.research_handoff,
            lambda: (_ for _ in ()).throw(RuntimeError("semantic failed")),
        )

    second_agent = _agent(tmp_path, binding)
    second_state = _state(count=1)
    calls: list[str] = []

    def completed_semantic() -> dict:
        calls.append("computed")
        return {"status": "semantic_analysis", "analysis_digest": "stable"}

    expected = second_agent._checkpoint_stage(
        second_state,
        "semantic_analysis",
        second_state.research_handoff,
        completed_semantic,
    )
    assert calls == ["computed"]
    assert not list(second_agent._checkpoint_store.directory.glob("*.tmp"))

    third_agent = _agent(tmp_path, binding)
    third_state = _state(count=1)
    restored = third_agent._checkpoint_stage(
        third_state,
        "semantic_analysis",
        third_state.research_handoff,
        lambda: (_ for _ in ()).throw(AssertionError("must not recompute")),
    )
    assert restored == expected
    assert third_state.checkpoints["resumed_stages"] == ["semantic_analysis"]


def test_no_resume_starts_a_new_run_without_falling_back_to_old_fragments(
    tmp_path: Path,
) -> None:
    binding = {"contract": "same"}
    previous = DeviceCheckpointStore(tmp_path, binding)
    old_key = previous.key("fragment_1", {"macro": 1})
    previous.write(old_key, "fragment_1", {"status": "old"})

    fresh = DeviceCheckpointStore(tmp_path, binding, resume=False)
    new_key = fresh.key("semantic_analysis", {"handoff": 1})
    fresh.write(new_key, "semantic_analysis", {"status": "new"})

    resumed = DeviceCheckpointStore(tmp_path, binding)
    assert previous.run_id != fresh.run_id == resumed.run_id
    assert resumed.read(resumed.key("fragment_1", {"macro": 1})) is None
    assert resumed.read(
        resumed.key("semantic_analysis", {"handoff": 1})
    ) == {"status": "new"}


def test_success_closes_active_run_and_same_input_gets_new_exp_id(
    tmp_path: Path,
) -> None:
    binding = {"research": "same", "device_truth": "same"}
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    first_exp_id = agent._default_exp_id()
    first = DeviceCheckpointStore(
        tmp_path,
        binding,
        metadata={"exp_id": first_exp_id},
    )
    old_key = first.key("fragment_1", {"macro": 1})
    first.write(old_key, "fragment_1", {"status": "accepted-prefix"})
    agent._checkpoint_store = first
    first_state = SingleDeviceAgentState(
        research_handoff=_handoff(1), exp_id=first_exp_id
    )
    first_package = {"status": "success", "workflow_json": {"steps": [{}]}}

    agent._finalize_checkpoint_run(first_state, first_package)

    assert first.lifecycle_state == "complete"
    assert first.dispatchable is True
    assert first_package["checkpoints"]["lifecycle_state"] == "complete"
    assert (first.directory / "completion.json").is_file()

    second_exp_id = agent._default_exp_id()
    second = DeviceCheckpointStore(
        tmp_path,
        binding,
        metadata={"exp_id": second_exp_id},
    )
    assert second_exp_id != first_exp_id
    assert second.run_id != first.run_id
    assert second.metadata["exp_id"] == second_exp_id
    assert second.lifecycle_state == "active"
    assert second.read(second.key("fragment_1", {"macro": 1})) is None


def test_ready_for_dispatch_closes_checkpoint_as_dispatchable(
    tmp_path: Path,
) -> None:
    store = DeviceCheckpointStore(
        tmp_path / "ready-for-dispatch",
        {"research": "same"},
        metadata={"exp_id": "ready-for-dispatch"},
    )
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    agent._checkpoint_store = store
    state = SingleDeviceAgentState(
        research_handoff=_handoff(1), exp_id="ready-for-dispatch"
    )
    package = {
        "status": "ready_for_dispatch",
        "workflow_json": {"steps": [{"step_number": 1}]},
    }

    agent._finalize_checkpoint_run(state, package)

    assert store.lifecycle_state == "complete"
    assert store.dispatchable is True
    assert package["checkpoints"]["terminal_status"] == "ready_for_dispatch"
    assert package["checkpoints"]["dispatchable"] is True


def test_run_state_success_then_same_handoff_starts_fresh_identity(
    tmp_path: Path,
) -> None:
    """Exercise the lifecycle hook at the real run_state boundary."""
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    agent._contract_version = "v1"
    agent._model = object()
    agent._txt_format_reference = ""
    agent._json_format_reference = ""
    agent._skill_session = None
    agent._refresh_workstation_snapshot = Mock()
    agent._workstation_skill_session = Mock(
        return_value=SimpleNamespace(
            discovery_context=lambda: "offline",
            truth_digest=lambda: "frozen-device-truth",
        )
    )
    agent._assert_workstation_snapshot_current = Mock()
    agent._sync_skill_load_state = Mock()
    agent._llm_semantic_analysis_enabled = Mock(return_value=False)
    agent._verified_stage1_core_route_gap_result = Mock(return_value=None)
    agent._invoke_feasibility_plan = Mock(return_value={"status": "device_plan"})
    agent._promote_feasible_quantity_human_plan = Mock(
        side_effect=lambda plan: plan
    )
    agent._remove_implicit_connectivity_blockers = Mock(
        side_effect=lambda _state, plan: plan
    )
    agent._retry_adaptable_feedback = Mock(
        side_effect=lambda _state, plan, _handoff: plan
    )
    agent._normalize_plan_handoff_steps = Mock(
        side_effect=lambda _state, plan: plan
    )
    agent._repair_plan_level_findings = Mock(
        side_effect=lambda _state, plan: plan
    )
    agent._plan_is_accepted = Mock(return_value=True)
    agent._accept_feasibility_plan = Mock(side_effect=lambda _state, plan: plan)
    agent._run_accepted_device_plan = Mock(return_value={"status": "mapped"})
    agent._normalize_terminal_package = Mock(
        side_effect=lambda state, _result: {
            "status": "success",
            "exp_id": state.exp_id,
            "workflow_json": {"steps": [{"step_number": 1}]},
            "workflow_txt": "offline",
        }
    )
    handoff = _handoff(1)

    first = agent.run_state(handoff, checkpoint_dir=str(tmp_path))
    second = agent.run_state(handoff, checkpoint_dir=str(tmp_path))

    assert first.status == second.status == "completed"
    assert first.exp_id != second.exp_id
    assert first.checkpoints["run_id"] != second.checkpoints["run_id"]
    assert first.checkpoints["lifecycle_state"] == "complete"
    assert second.checkpoints["lifecycle_state"] == "complete"
    assert first.terminal_package["exp_id"] == first.exp_id
    assert second.terminal_package["exp_id"] == second.exp_id


def test_completion_marker_wins_if_active_rotation_was_interrupted(
    tmp_path: Path,
) -> None:
    binding = {"research": "same"}
    first = DeviceCheckpointStore(
        tmp_path,
        binding,
        metadata={"exp_id": "first-exp"},
    )
    stale_active = json.loads(first.active_path.read_text(encoding="utf-8"))
    first.complete(terminal_status="success", dispatchable=True)
    # Recreate the only dangerous crash window: completion.json was durable,
    # but active.json still contained the prior active envelope.
    first.active_path.write_text(
        json.dumps(stale_active, ensure_ascii=False), encoding="utf-8"
    )

    next_run = DeviceCheckpointStore(
        tmp_path,
        binding,
        metadata={"exp_id": "second-exp"},
    )

    assert next_run.run_id != first.run_id
    assert next_run.metadata["exp_id"] == "second-exp"
    assert next_run.lifecycle_state == "active"


def test_active_completion_survives_secondary_audit_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = {"research": "same"}
    first = DeviceCheckpointStore(
        tmp_path,
        binding,
        metadata={"exp_id": "first-exp"},
    )
    real_atomic_json = checkpoints_module._atomic_json
    calls: list[Path] = []

    def fail_second_write(path: Path, value: object) -> None:
        calls.append(path)
        if len(calls) == 2:
            raise OSError("injected run-local audit write failure")
        real_atomic_json(path, value)

    monkeypatch.setattr(checkpoints_module, "_atomic_json", fail_second_write)

    first.complete(terminal_status="success", dispatchable=True)

    assert calls[0] == first.active_path
    assert calls[1] == first.directory / "completion.json"
    assert first.lifecycle_state == "complete"
    assert not (first.directory / "completion.json").exists()
    authoritative = json.loads(first.active_path.read_text(encoding="utf-8"))
    assert authoritative["lifecycle_state"] == "complete"

    next_run = DeviceCheckpointStore(
        tmp_path,
        binding,
        metadata={"exp_id": "second-exp"},
    )
    assert next_run.run_id != first.run_id
    assert next_run.metadata["exp_id"] == "second-exp"


def test_failed_run_stays_active_and_resumes_last_completed_fragment(
    tmp_path: Path,
) -> None:
    binding = {"research": "same", "device_truth": "same"}
    first_exp_id = "failed-run-exp"
    first = DeviceCheckpointStore(
        tmp_path,
        binding,
        metadata={"exp_id": first_exp_id},
    )
    old_key = first.key("fragment_1", {"macro": 1})
    first.write(old_key, "fragment_1", {"status": "accepted-prefix"})
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    agent._checkpoint_store = first
    failed_state = SingleDeviceAgentState(
        research_handoff=_handoff(2), exp_id=first_exp_id
    )
    failed_package = {"status": "failed", "workflow_json": {}}

    agent._finalize_checkpoint_run(failed_state, failed_package)

    assert first.lifecycle_state == "active"
    assert failed_package["checkpoints"]["lifecycle_state"] == "active"
    assert not (first.directory / "completion.json").exists()

    resumed = DeviceCheckpointStore(
        tmp_path,
        binding,
        metadata={"exp_id": "must-not-replace-resumed-exp"},
    )
    assert resumed.run_id == first.run_id
    assert resumed.metadata["exp_id"] == first_exp_id
    assert resumed.read(
        resumed.key("fragment_1", {"macro": 1})
    ) == {"status": "accepted-prefix"}


@pytest.mark.parametrize(
    "status",
    ["manual_required", "feasibility_error", "terminal_unmappable"],
)
def test_non_retryable_business_terminal_statuses_close_run(
    tmp_path: Path,
    status: str,
) -> None:
    store = DeviceCheckpointStore(
        tmp_path / status,
        {"research": "same"},
        metadata={"exp_id": status},
    )
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    agent._checkpoint_store = store
    state = SingleDeviceAgentState(research_handoff=_handoff(1), exp_id=status)
    package = {"status": status, "workflow_json": {}}

    agent._finalize_checkpoint_run(state, package)

    assert store.lifecycle_state == "complete"
    assert store.dispatchable is False


def test_runner_exposes_checkpoint_controls() -> None:
    args = build_parser().parse_args(
        [
            "--research-state",
            "research.json",
            "--checkpoint-dir",
            "saved-checkpoints",
            "--no-resume-checkpoints",
        ]
    )
    assert args.checkpoint_dir == "saved-checkpoints"
    assert args.no_resume_checkpoints is True


def test_default_checkpoint_directory_is_independent_of_output_directory(
    tmp_path: Path,
) -> None:
    first_research = tmp_path / "result-1" / "research_state.json"
    moved_research = tmp_path / "result-2" / "research_state.json"
    first = default_checkpoint_dir(str(first_research))
    assert first_research.parent != moved_research.parent
    assert first == default_checkpoint_dir(str(moved_research))
    assert Path(first).name == "device_checkpoints"
    assert Path(first).parent.name == "result"


def test_planning_model_binding_tracks_backend_model_and_effort_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "CHEM_DEVICE_FEASIBILITY_REASONING_EFFORT", raising=False
    )
    first_backend = SimpleNamespace(
        model_name="kimi-k3",
        base_url="https://gateway.example/v1?api_key=query-secret",
        reasoning_effort="high",
        max_tokens=32768,
        api_key="attribute-secret",
        name="https://gateway.example/v1:kimi-k3",
    )
    first_pool = SimpleNamespace(
        backends=[first_backend],
        max_rounds=1,
        round_backoff_seconds=[2, 5],
    )
    first = SingleDeviceAgent._planning_model_checkpoint_binding(first_pool)
    encoded = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert "kimi-k3" in encoded
    assert "gateway.example" in encoded
    assert "query-secret" not in encoded
    assert "attribute-secret" not in encoded

    credential_changed_backend = copy.copy(first_backend)
    credential_changed_backend.base_url = (
        "https://other-user:other-password@gateway.example/"
        "v1?api_key=another-query-secret"
    )
    credential_changed_backend.api_key = "another-attribute-secret"
    credential_changed_backend.name = (
        "https://other-user:other-password@gateway.example/v1:kimi-k3"
    )
    credential_changed_pool = SimpleNamespace(
        backends=[credential_changed_backend],
        max_rounds=1,
        round_backoff_seconds=[2, 5],
    )
    assert SingleDeviceAgent._planning_model_checkpoint_binding(
        credential_changed_pool
    ) == first

    monkeypatch.setenv("CHEM_DEVICE_FEASIBILITY_REASONING_EFFORT", "xhigh")
    effort_changed = SingleDeviceAgent._planning_model_checkpoint_binding(
        first_pool
    )
    assert effort_changed != first

    changed_backend = copy.copy(first_backend)
    changed_backend.model_name = "gpt-5.6-sol"
    changed_backend.reasoning_effort = "xhigh"
    changed_pool = SimpleNamespace(
        backends=[changed_backend],
        max_rounds=1,
        round_backoff_seconds=[2, 5],
    )
    changed = SingleDeviceAgent._planning_model_checkpoint_binding(changed_pool)
    assert changed != first

    route_changed_backend = copy.copy(first_backend)
    route_changed_backend.base_url = "https://gateway.example/v2?api_key=x"
    route_changed_pool = SimpleNamespace(
        backends=[route_changed_backend],
        max_rounds=1,
        round_backoff_seconds=[2, 5],
    )
    monkeypatch.delenv(
        "CHEM_DEVICE_FEASIBILITY_REASONING_EFFORT", raising=False
    )
    assert SingleDeviceAgent._planning_model_checkpoint_binding(
        route_changed_pool
    ) != first

    versioned_backend = copy.copy(first_backend)
    versioned_backend.base_url = (
        "https://gateway.example/v1?api_key=hidden&api-version=2026-09-01"
    )
    versioned_pool = SimpleNamespace(
        backends=[versioned_backend],
        max_rounds=1,
        round_backoff_seconds=[2, 5],
    )
    assert SingleDeviceAgent._planning_model_checkpoint_binding(
        versioned_pool
    ) != first

    reordered_backend = copy.copy(versioned_backend)
    reordered_backend.base_url = (
        "https://gateway.example/v1?api-version=2026-09-01&api_key=changed"
    )
    reordered_pool = SimpleNamespace(
        backends=[reordered_backend],
        max_rounds=1,
        round_backoff_seconds=[2, 5],
    )
    assert SingleDeviceAgent._planning_model_checkpoint_binding(
        reordered_pool
    ) == SingleDeviceAgent._planning_model_checkpoint_binding(versioned_pool)


def test_api_key_change_does_not_invalidate_five_completed_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHEM_DEVICE_FEASIBILITY_MODE", raising=False)
    monkeypatch.delenv(
        "CHEM_DEVICE_FEASIBILITY_REASONING_EFFORT", raising=False
    )

    def model_binding(user: str, password: str, query_key: str) -> dict:
        backend = SimpleNamespace(
            model_name="kimi-k3",
            base_url=(
                f"https://{user}:{password}@gateway.example/"
                f"v1?api_key={query_key}"
            ),
            reasoning_effort="high",
            api_key=query_key,
        )
        return SingleDeviceAgent._planning_model_checkpoint_binding(backend)

    first_binding = {"planning_model": model_binding("u1", "p1", "key-1")}
    changed_key_binding = {
        "planning_model": model_binding("u2", "p2", "key-2")
    }
    assert changed_key_binding == first_binding

    first_agent = _agent(tmp_path, first_binding)
    first_state = _state(count=5)
    first_agent._invoke_feasibility_request = Mock(
        side_effect=[
            _fragment(
                index, first_state.research_handoff["sample_control_matrix"]
            )
            for index in range(1, 6)
        ]
    )
    first_agent._invoke_feasibility_plan(first_state)

    resumed_agent = _agent(tmp_path, changed_key_binding)
    resumed_state = _state(count=5)
    resumed_agent._invoke_feasibility_request = Mock(
        side_effect=AssertionError("completed chunks must be restored")
    )
    candidate = resumed_agent._invoke_feasibility_plan(resumed_state)

    resumed_agent._invoke_feasibility_request.assert_not_called()
    assert len(candidate["device_plan"]) == 5
    assert resumed_state.checkpoints["resumed_stages"] == [
        f"feasibility_device_plan_chunk_{index}_of_5"
        for index in range(1, 6)
    ]

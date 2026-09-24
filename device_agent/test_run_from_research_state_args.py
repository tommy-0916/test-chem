from __future__ import annotations

import copy
import json
import sys
import types

import pytest

import device_agent.run_from_research_state as run_module
from device_agent.run_from_research_state import (
    build_parser,
    device_input_package_to_text,
    validate_repair_contract_boundary,
)


def test_exp_id_argument_is_accepted() -> None:
    args = build_parser().parse_args(
        [
            "--research-state",
            "state.json",
            "--exp-id",
            "A01-20260718-013500",
        ]
    )

    assert args.exp_id == "A01-20260718-013500"


def test_manual_device_repair_arguments_are_accepted() -> None:
    args = build_parser().parse_args(
        [
            "--research-state",
            "state.json",
            "--device-plan-override",
            "device_plan_override.json",
            "--prior-repair-request",
            "device_repair_request.json",
        ]
    )

    assert args.device_plan_override == "device_plan_override.json"
    assert args.prior_repair_request == "device_repair_request.json"


def test_workflow_verification_argument_is_accepted() -> None:
    args = build_parser().parse_args(
        [
            "--research-state",
            "state.json",
            "--workflow-verification",
            "deterministic",
        ]
    )

    assert args.workflow_verification == "deterministic"


def test_responses_safe_default_output_budget() -> None:
    args = build_parser().parse_args(["--research-state", "state.json"])

    assert args.max_tokens == 32768


def test_runtime_approval_capability_is_redacted_from_printable_handoff() -> None:
    package = {
        "human_quantity_approval_validation": {
            "validated": True,
            "approval_capability_token": "secret-one-time-token",
        }
    }

    printable = device_input_package_to_text(package)

    assert "secret-one-time-token" not in printable
    assert "approval_capability_token" not in printable
    assert package["human_quantity_approval_validation"][
        "approval_capability_token"
    ] == "secret-one-time-token"


def test_main_passes_typed_approval_bundle_only_as_internal_run_kwarg(
    monkeypatch,
    tmp_path,
) -> None:
    research_path = tmp_path / "research.json"
    request_path = tmp_path / "request.json"
    override_path = tmp_path / "override.json"
    research_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "macro_plan": [
                    {
                        "步骤序号": 1,
                        "操作": "干燥",
                        "试剂/对象": "前驱体A",
                        "参数": "0.18 mmol",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    request_path.write_text(
        json.dumps(
            {
                "request_id": "device-repair-001",
                "contract_version": "v2",
                "contract_resolution": {
                    "requested": "v2",
                    "effective": "v2",
                    "requested_matches_effective": True,
                },
                "feasibility_certificate": {
                    "accepted": True,
                    "contract_version": "v2",
                },
            }
        ),
        encoding="utf-8",
    )
    override_path.write_text(
        json.dumps(
            {
                "request_id": "device-repair-001",
                "contract_version": "v2",
                "device_plan": [{"plan_step": 1}],
                "human_quantity_approvals": [{"untrusted": True}],
            }
        ),
        encoding="utf-8",
    )

    typed_bundle = object()

    def fake_validate(override, request, **kwargs):
        sanitized = dict(override)
        sanitized.pop("human_quantity_approvals", None)
        return sanitized, {"validated": True}, [{"approval_id": "A"}], typed_bundle

    monkeypatch.setattr(
        run_module,
        "validate_human_quantity_approvals",
        fake_validate,
    )
    monkeypatch.setattr(
        run_module,
        "approval_request_sha256",
        lambda path: "a" * 64,
    )

    captured = {}

    class FakeFactory:
        @staticmethod
        def create(**kwargs):
            return object()

    class FakeState:
        terminal_package = {"status": "success"}
        status = "completed"
        exp_id = "approval-wiring-test"

        @staticmethod
        def to_dict():
            return {"status": "completed"}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["agent_kwargs"] = kwargs

        def run_state(self, handoff, **kwargs):
            captured["handoff"] = handoff
            captured["kwargs"] = kwargs
            return FakeState()

    fake_utils = types.ModuleType("utils")
    fake_utils.__path__ = []
    fake_factory_module = types.ModuleType("utils.llm_factory")
    fake_factory_module.LLMFactory = FakeFactory
    fake_single_agent = types.ModuleType("single_agent")
    fake_single_agent.SingleDeviceAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "utils", fake_utils)
    monkeypatch.setitem(sys.modules, "utils.llm_factory", fake_factory_module)
    monkeypatch.setitem(sys.modules, "single_agent", fake_single_agent)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_from_research_state.py",
            "--research-state",
            str(research_path),
            "--prior-repair-request",
            str(request_path),
            "--device-plan-override",
            str(override_path),
        ],
    )

    assert run_module.main() == 0
    assert captured["agent_kwargs"]["contract_version"] == "v2"
    assert captured["handoff"]["contract_version"] == "v2"
    assert captured["handoff"]["contract_resolution"] == {
        "requested": "v2",
        "source_input": "v1",
        "effective": "v2",
        "requested_matches_effective": True,
        "source_matches_effective": False,
    }
    assert captured["kwargs"]["human_quantity_approval_bundle"] is typed_bundle
    assert "human_quantity_approvals" not in captured["kwargs"][
        "device_plan_override"
    ]
    assert "human_quantity_approval_validation" not in captured["handoff"]
    assert "validated_human_quantity_approvals" not in captured["handoff"]


def test_repair_contract_boundary_rejects_request_override_or_runtime_drift():
    request = {
        "contract_version": "v2",
        "contract_resolution": {
            "requested": "v2",
            "effective": "v2",
            "requested_matches_effective": True,
        },
        "feasibility_certificate": {
            "accepted": True,
            "contract_version": "v2",
        },
    }
    override = {"contract_version": "v2"}

    validate_repair_contract_boundary(request, override, "v2")

    bad_override = dict(override, contract_version="v1")
    with pytest.raises(SystemExit, match="override contract_version"):
        validate_repair_contract_boundary(request, bad_override, "v2")
    with pytest.raises(SystemExit, match="runtime --contract-version"):
        validate_repair_contract_boundary(request, override, "v1")
    bad_resolution = copy.deepcopy(request)
    bad_resolution["contract_resolution"]["effective"] = "v1"
    with pytest.raises(SystemExit, match="contract_resolution"):
        validate_repair_contract_boundary(bad_resolution, override, "v2")

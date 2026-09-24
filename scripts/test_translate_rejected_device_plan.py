"""No-network tests for the rejected Device-plan translation harness."""

from __future__ import annotations

import hashlib
import json

import pytest

from scripts.translate_rejected_device_plan import (
    DiagnosticTranslationError,
    load_rejected_candidate,
    main,
)
from single_agent import SingleDeviceAgent, device_plan_contract_digest


class FakeModel:
    def __init__(self) -> None:
        self.calls = 0

    def invoke_json_object(self, messages):
        self.calls += 1
        assert "本次只需翻译的范围" in messages[-1].content
        return json.dumps({
            "workflow_txt": "第1步 耐热瓶物料站：诊断模拟",
            "workflow_json": {
                "steps": [
                    {
                        "step_number": 1,
                        "workstation": "耐热瓶物料站",
                        "operation": "诊断模拟",
                        "parameters": {},
                        "source_plan_step": 1,
                    }
                ]
            },
        }, ensure_ascii=False)


def _source(model):
    agent = SingleDeviceAgent(model, contract_version="v1")
    agent._refresh_workstation_snapshot()
    step = {"plan_step": 1, "workstation": "耐热瓶物料站", "source_macro_step": 1}
    raw = {
        "status": "manual_required",
        "failure_stage": "plan_level_audit",
        "feasibility_accepted": False,
        "feasibility_certificate": {},
        "workflow_json": {},
        "workflow_txt": "",
        "device_plan": [step],
    }
    digest = device_plan_contract_digest(raw)
    raw["plan_audit_record"] = {
        "run_layer_status": "blocked",
        "findings": [{"type": "deliberate_test_block"}],
        "device_plan_contract_digest": digest,
    }
    raw["plan_audit_binding"] = {"scope": "device_plan_contract/v1", "digest": digest}
    return {
        "contract_version": "v1",
        "status": "manual_required",
        "feasibility_accepted": False,
        "feasibility_certificate": {},
        "workflow_json": {},
        "workflow_txt": "",
        "terminal_package": {"status": "manual_required"},
        "raw_llm_output": raw,
        "research_handoff": {
            "macro_action": {
                "diagnostic_forced_continue": {
                    "runtime_contract": "v1_compat",
                    "formal_v2_validated": False,
                    "dispatchable": False,
                    "real_dispatch": False,
                }
            }
        },
        "device_truth_sha256": agent._full_device_truth_digest(),
        "txt_format_reference": agent._txt_format_reference,
        "json_format_reference": agent._json_format_reference,
    }


def _write_source(tmp_path, source):
    path = tmp_path / "device_state.json"
    payload = json.dumps(source, ensure_ascii=False).encode("utf-8")
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def test_fake_model_entrypoint_yields_only_unvalidated_artifact(tmp_path):
    model = FakeModel()
    source_path, digest = _write_source(tmp_path, _source(model))
    output_dir = tmp_path / "fresh-output"
    exit_code = main(
        [
            "--source-device-state", str(source_path),
            "--output-dir", str(output_dir),
            "--source-sha256", digest,
            "--expected-steps", "1",
        ],
        model_factory=lambda: model,
    )
    assert exit_code == 0
    assert model.calls == 1
    result = json.loads((output_dir / "diagnostic_workflow.json").read_text(encoding="utf-8"))
    manifest = json.loads((output_dir / "diagnostic_manifest.json").read_text(encoding="utf-8"))
    assert result["status"] == "diagnostic_unvalidated"
    assert result["translation_status"] == "produced"
    assert result["translated_workflow_steps"] == 1
    assert result["plan_audit_findings"] == [{"type": "deliberate_test_block"}]
    assert result["dispatchable"] is False
    assert result["real_dispatch"] is False
    assert result["feasibility_certificate"] == {}
    assert "dispatch_payload" not in result
    assert manifest["raw_candidate_sha256"]
    assert manifest["source_device_state_sha256"] == digest
    assert source_path.read_bytes() == json.dumps(_source(FakeModel()), ensure_ascii=False).encode("utf-8")


def test_refuses_hash_or_formal_success_before_creating_output(tmp_path):
    source = _source(FakeModel())
    path, digest = _write_source(tmp_path, source)
    with pytest.raises(DiagnosticTranslationError, match="SHA-256 mismatch"):
        load_rejected_candidate(path, expected_source_sha256="0" * 64, expected_steps=1)
    source["feasibility_certificate"] = {"claimed": True}
    path, digest = _write_source(tmp_path, source)
    with pytest.raises(DiagnosticTranslationError, match="no Device feasibility acceptance"):
        load_rejected_candidate(path, expected_source_sha256=digest, expected_steps=1)
    assert not (tmp_path / "fresh-output").exists()


def test_refuses_existing_output_without_model_call(tmp_path):
    model = FakeModel()
    path, digest = _write_source(tmp_path, _source(model))
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    with pytest.raises(FileExistsError):
        main(
            [
                "--source-device-state", str(path),
                "--output-dir", str(output_dir),
                "--source-sha256", digest,
                "--expected-steps", "1",
            ],
            model_factory=lambda: model,
        )
    assert model.calls == 0

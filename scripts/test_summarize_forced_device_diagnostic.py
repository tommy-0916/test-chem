"""The forced replay summary must never become an actionable Device result."""

from __future__ import annotations

import json

import pytest

from scripts.prepare_rejected_research_diagnostic import write_diagnostic
from scripts.summarize_forced_device_diagnostic import (
    DiagnosticSummaryError,
    write_summary,
)


def _manifest(tmp_path):
    source = {
        "contract_version": "v2",
        "status": "manual_required",
        "failure_category": "macro_quality_error",
        "event": {"query": "A02"},
        "macro_plan": [],
        "research_action_package_v2": {},
        "raw_llm_outputs": {
            "macro_action_design_bootstrap": {"objective": "candidate"},
            "macro_plan_design_retry_1": {
                "current_stage_plan": "Unresolved constraints",
                "macro_plan": [{"macro_step_id": "step-1"}],
            },
        },
    }
    source_path = tmp_path / "source.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    _, manifest_path = write_diagnostic(source_path, tmp_path / "handoff")
    return manifest_path, source_path


def test_raw_success_stays_nonactionable_and_counts_only(tmp_path):
    manifest_path, _ = _manifest(tmp_path)
    raw = {
        "status": "success",
        "contract_version": "v1",
        "exp_id": "local-exp-not-lab-id",
        "verification_summary": {"result": "accepted"},
        "feasibility": {"is_feasible": True},
        "feasibility_certificate": {"token": "CERT_CANARY"},
        "dispatch_payload": {
            "experiment_steps": {"steps": [{"secret": "DO_NOT_DISPATCH"}]}
        },
        "device_plan": [{"plan_step": 1}, {"plan_step": 2}],
        "workflow_json": {
            "steps": [{"operation": "ACTION_CANARY"}],
            "offline_handoffs": [],
        },
        "workflow_txt": "ACTION_CANARY",
        "dispatch_formatting": {
            "mapped_steps": 1,
            "unmapped_steps": 0,
            "warnings": ["untrusted"],
        },
    }
    raw_path = tmp_path / "raw_device_package.json"
    raw_bytes = json.dumps(raw).encode("utf-8")
    raw_path.write_bytes(raw_bytes)
    output_path = tmp_path / "summary.json"
    write_summary(manifest_path, raw_path, output_path)
    result_text = output_path.read_text(encoding="utf-8")
    result = json.loads(result_text)
    assert result["status"] == "diagnostic_only_unvalidated"
    assert result["formal_success"] is False
    assert result["feasibility_certified"] is False
    assert result["dispatchable"] is False
    assert result["real_dispatch"] is False
    assert result["laboratory_task_id"] is None
    assert result["measured"]["raw_device_reported_status"] == "success"
    assert result["measured"]["workflow_step_count"] == 1
    assert result["measured"]["device_plan_step_count"] == 2
    assert result["measured"]["raw_dispatch_payload_present"] is True
    assert result["measured"]["raw_certificate_present"] is True
    assert "CERT_CANARY" not in result_text
    assert "DO_NOT_DISPATCH" not in result_text
    assert "ACTION_CANARY" not in result_text
    with pytest.raises(FileExistsError):
        write_summary(manifest_path, raw_path, output_path)


def test_rejects_tampered_handoff(tmp_path):
    manifest_path, source_path = _manifest(tmp_path)
    package_path = tmp_path / "package.json"
    package_path.write_text('{"status":"failed"}', encoding="utf-8")
    source_path.write_text('{"status":"completed"}', encoding="utf-8")
    with pytest.raises(DiagnosticSummaryError, match="SHA mismatch"):
        write_summary(manifest_path, package_path, tmp_path / "summary.json")
    assert not (tmp_path / "summary.json").exists()

"""Regression tests for the persistent, permission-constrained repair flow.

The four weighing-step fixtures replicate the real failed candidates from the
2026-09-17 kimi-k3 run (campaigns/a02-device-k3-20260917-183006): each one
failed the file-dosing recipe audit for a different, precisely diagnosed
reason.  None of these cases may pass because rules were relaxed.
"""

from __future__ import annotations

import copy
import json

import pytest

import plan_repair
from plan_repair import (
    build_repair_context,
    create_repair_case,
    diagnose_candidate,
    failed_checks,
    propose_repair,
    recipe_auditor,
    run_repair_loop,
    validate_repair,
)

WEIGH_STEP = 25
OTHER_WORKSTATION = "神秘离心机_X9"


def _other_step():
    return {
        "plan_step": 99,
        "workstation": OTHER_WORKSTATION,
        "operation_intent": "离心分离",
        "key_values": {"转速_rpm": 8000, "时长_min": 10},
        "notes": "标准离心步骤",
    }


def _candidate(weigh_step):
    return {
        "status": "device_plan",
        "device_plan": [copy.deepcopy(weigh_step), _other_step()],
        "sample_control_matrix": [
            {"sample_id": "SAMPLE_GRP_MA_S01_R00_01_01", "group": "control"}
        ],
    }


def _candidate_1():
    """Prose recipe row inside key_values; not a recognized wrapper."""
    step = {
        "plan_step": WEIGH_STEP,
        "workstation": "多通道固体称量工作站_V1",
        "operation_intent": "固体进样-文件传参-机器人（定量分样）",
        "key_values": {
            "上传文件配方行": "瓶号1（对应容器编号3）, 加样量 0.010 g, 料罐号 1",
            "加样量范围核对": "0.010∈[0,50] g",
            "料罐来源": "料斗1中的 batch_005 母批粉体",
        },
    }
    return _candidate(step)


def _candidate_2():
    """Structured rows with field names outside the contract aliases."""
    step = {
        "plan_step": WEIGH_STEP,
        "workstation": "多通道固体称量工作站_V1",
        "operation_intent": "固体进样-文件传参-机器人",
        "key_values": {
            "容器类型": "进样瓶",
            "容器数量": 1,
            "容器编号": [3],
            "上传文件": "Multi_Channel_Solid_Weighing_Workstation_V1_固体进样文件传参.xlsx",
            "上传文件配方": [
                {
                    "文件瓶号": "1",
                    "实际容器编号": "3",
                    "加样量_g": 0.01,
                    "料罐号": 1,
                    "csv_row": "1,0.010,1",
                }
            ],
            "文件总行数": 1,
        },
    }
    return _candidate(step)


def _candidate_3():
    """Contract bottle/hopper names; mass field still off-contract; string bottle."""
    step = {
        "plan_step": WEIGH_STEP,
        "workstation": "多通道固体称量工作站_V1",
        "operation_intent": "固体进样-文件传参-机器人",
        "key_values": {
            "上传文件": "Multi_Channel_Solid_Weighing_Workstation_V1_固体进样文件传参.xlsx",
            "上传文件逐瓶配方": [
                {"瓶号": "1", "加样量_g": 0.01, "料罐号": 1, "对应实际容器编号": "3"}
            ],
            "上传文件CSV内容": "瓶号,加样量(g),料罐号\n1,0.010,1",
        },
    }
    return _candidate(step)


def _candidate_4():
    """All contract field names; all three values are numeric strings."""
    step = {
        "plan_step": WEIGH_STEP,
        "workstation": "多通道固体称量工作站_V1",
        "operation_intent": "固体进样-文件传参-机器人",
        "key_values": {
            "容器类型": "进样瓶",
            "容器数量": 1,
            "容器编号": [3],
            "上传文件": "Multi_Channel_Solid_Weighing_Workstation_V1_固体进样文件传参.xlsx",
            "上传文件逐瓶配方": [
                {"瓶号": "1", "实际容器编号": "3", "加样量(g)": "0.010", "料罐号": "1"}
            ],
            "上传文件CSV内容": "瓶号,加样量(g),料罐号\n1,0.010,1",
        },
    }
    return _candidate(step)


def _weigh_key_values(candidate):
    step = next(
        s
        for s in candidate["device_plan"]
        if s.get("plan_step") == WEIGH_STEP
    )
    return step["key_values"]


# ---------------------------------------------------------------------------
# 1. the four original failed candidates still fail, with accurate reasons
# ---------------------------------------------------------------------------


def test_original_candidates_still_fail_with_accurate_reasons():
    one = recipe_auditor(_candidate_1())
    assert len(one) == 1 and one[0]["type"] == "missing_concrete_recipe_evidence"
    checks = failed_checks(one)
    assert [c["rule"] for c in checks] == ["legacy_text_recipe"]
    assert checks[0]["status"] == "failed"

    two = recipe_auditor(_candidate_2())
    checks_two = failed_checks(two)
    # 文件瓶号/加样量_g are outside the contract, so no structured recipe is
    # recognized at all and the prose fallback is empty.
    assert checks_two[0]["rule"] == "legacy_text_recipe"

    three = recipe_auditor(_candidate_3())
    failed_three = {c["field"]: c for c in failed_checks(three)}
    assert failed_three["瓶号"]["rule"] == "json_integer"
    assert failed_three["瓶号"]["actual_type"] == "str"
    assert failed_three["加样量(g)"]["status"] == "failed"
    passed_three = [
        c["field"] for c in three[0]["details"]["checks"] if c["status"] == "passed"
    ]
    assert passed_three == ["料罐号"]

    four = recipe_auditor(_candidate_4())
    failed_four = {c["field"] for c in failed_checks(four)}
    assert failed_four == {"瓶号", "加样量(g)", "料罐号"}
    assert all(
        c["actual_type"] == "str" for c in failed_checks(four)
    )


def test_findings_carry_contract_version_and_paths():
    findings = recipe_auditor(_candidate_4())
    details = findings[0]["details"]
    assert details["contract_version"] == plan_repair.recipe_contract_version()
    assert details["path"].startswith(f"device_plan[step={WEIGH_STEP}]")
    for check in details["checks"]:
        assert check["path"].startswith("key_values.")


# ---------------------------------------------------------------------------
# 2. authorized minimal patch passes the hierarchy, preserving meaning
# ---------------------------------------------------------------------------


def test_minimal_patch_repairs_candidate_4_without_changing_meaning():
    result = run_repair_loop(_candidate_4(), recipe_auditor)
    assert result["status"] == "accepted"
    assert result["stop_reason"] == "accepted"
    assert result["case"]["budgets"]["patches_used"] == 1

    repaired = result["final_candidate"]
    row = _weigh_key_values(repaired)["上传文件逐瓶配方"][0]
    assert row["瓶号"] == 1 and isinstance(row["瓶号"], int)
    assert row["加样量(g)"] == 0.01 and isinstance(row["加样量(g)"], float)
    assert row["料罐号"] == 1 and isinstance(row["料罐号"], int)
    # Mapping and provenance preserved untouched.
    assert row["实际容器编号"] == "3"
    assert _weigh_key_values(repaired)["上传文件CSV内容"] == "瓶号,加样量(g),料罐号\n1,0.010,1"

    # Baseline and frozen state unchanged; log records the full chain.
    baseline_row = _weigh_key_values(result["case"]["baseline"]["candidate"])[
        "上传文件逐瓶配方"
    ][0]
    assert baseline_row["加样量(g)"] == "0.010"
    assert result["case"]["baseline"]["candidate"]["sample_control_matrix"] == [
        {"sample_id": "SAMPLE_GRP_MA_S01_R00_01_01", "group": "control"}
    ]
    kinds = [entry["kind"] for entry in result["case"]["log"]]
    assert kinds[0] == "open" and "propose" in kinds and "accept" in kinds
    assert result["case"]["progress"]["verified"]


def test_coercion_is_lossless_and_conservative():
    coerce = plan_repair._coerce_numeric_string
    assert coerce("0.010", "json_number") == 0.01
    assert coerce("12", "json_integer") == 12
    assert coerce("1e3", "json_number") is None
    assert coerce("-5", "json_integer") is None
    assert coerce("0", "json_number") is None
    assert coerce("适量", "json_number") is None
    assert coerce("0.5", "json_integer") is None
    assert coerce(5, "json_integer") is None


def test_partially_patchable_candidate_returns_controlled_verdict():
    case = create_repair_case(
        _candidate_3(), recipe_auditor(_candidate_3())
    )
    context = build_repair_context(case)
    proposal = propose_repair(context, case["draft"]["candidate"])
    assert proposal["status"] == "partial"
    assert proposal["patchable_count"] == 1  # only 瓶号
    assert any(
        item["field"] == "加样量(g)" for item in proposal["unpatchable"]
    )


# ---------------------------------------------------------------------------
# 3. patches touching frozen state or unrelated steps are rejected
# ---------------------------------------------------------------------------


def _valid_patch(case):
    context = build_repair_context(case)
    proposal = propose_repair(context, case["draft"]["candidate"])
    assert proposal["status"] == "patch_ready"
    return proposal["patch"]


def test_patch_touching_frozen_matrix_is_rejected():
    case = create_repair_case(
        _candidate_4(), recipe_auditor(_candidate_4())
    )
    patch = _valid_patch(case)
    patch["ops"] = [
        {"op": "replace", "path": "sample_control_matrix[0].group", "value": "treated"}
    ]
    result = validate_repair(case, patch, recipe_auditor)
    assert result["status"] == "rejected"
    assert result["reason"].startswith("frozen_key_modified")
    assert _weigh_key_values(case["draft"]["candidate"])["上传文件逐瓶配方"][0][
        "加样量(g)"
    ] == "0.010"


def test_patch_touching_unrelated_step_is_rejected():
    case = create_repair_case(
        _candidate_4(), recipe_auditor(_candidate_4())
    )
    patch = _valid_patch(case)
    patch["ops"] = [
        {"op": "replace", "path": "device_plan[step=99].notes", "value": "篡改"}
    ]
    result = validate_repair(case, patch, recipe_auditor)
    assert result["status"] == "rejected"
    assert result["reason"].startswith("path_not_authorized")
    other = next(
        s for s in case["draft"]["candidate"]["device_plan"] if s.get("plan_step") == 99
    )
    assert other["notes"] == "标准离心步骤"


def test_patch_adding_steps_is_rejected():
    case = create_repair_case(
        _candidate_4(), recipe_auditor(_candidate_4())
    )
    patch = _valid_patch(case)
    patch["ops"] = [
        {
            "op": "replace",
            "path": "device_plan[step=99].key_values.时长_min",
            "value": 99,
        }
    ]
    result = validate_repair(case, patch, recipe_auditor)
    assert result["status"] == "rejected"


# ---------------------------------------------------------------------------
# 4. regressions after a accepted field fix are detected
# ---------------------------------------------------------------------------


def test_regression_after_fix_is_detected_by_test_op():
    case = create_repair_case(
        _candidate_4(), recipe_auditor(_candidate_4())
    )
    first = _valid_patch(case)
    promoted = validate_repair(case, first, recipe_auditor)
    assert promoted["status"] == "promoted"

    revert = copy.deepcopy(first)
    revert["base_draft_version"] = case["draft"]["version"]
    result = validate_repair(case, revert, recipe_auditor)
    assert result["status"] == "rejected"
    assert result["reason"].startswith("test_failed")
    # Draft still holds the repaired numeric values.
    assert _weigh_key_values(case["draft"]["candidate"])["上传文件逐瓶配方"][0][
        "加样量(g)"
    ] == 0.01


def test_progress_table_is_validator_owned():
    case = create_repair_case(
        _candidate_4(), recipe_auditor(_candidate_4())
    )
    verified_before = case["progress"]["verified"]
    assert verified_before == []  # model self-reports are not accepted
    result = validate_repair(case, _valid_patch(case), recipe_auditor)
    assert result["status"] == "promoted"
    assert any("瓶号" in item for item in case["progress"]["verified"])
    assert case["progress"]["open_issues"] == []


# ---------------------------------------------------------------------------
# 5. contract drift invalidates old patches
# ---------------------------------------------------------------------------


def test_patch_with_stale_contract_version_is_rejected():
    case = create_repair_case(
        _candidate_4(), recipe_auditor(_candidate_4())
    )
    patch = _valid_patch(case)
    patch["contract_version"] = "stale-version"
    result = validate_repair(case, patch, recipe_auditor)
    assert result["status"] == "rejected"
    assert result["reason"] == "contract_mismatch"


def test_current_contract_drift_is_rejected(monkeypatch):
    case = create_repair_case(
        _candidate_4(), recipe_auditor(_candidate_4())
    )
    patch = _valid_patch(case)
    monkeypatch.setattr(
        plan_repair._single_agent, "RECIPE_FIELD_CONTRACT_VERSION", "drifted-v2"
    )
    result = validate_repair(case, patch, recipe_auditor)
    assert result["status"] == "rejected"
    assert result["reason"] == "contract_mismatch_current"


# ---------------------------------------------------------------------------
# 6. repeating the same error without new information stops as no_progress
# ---------------------------------------------------------------------------


def test_loop_without_progress_stops_instead_of_retrying_forever():
    frozen_findings = recipe_auditor(_candidate_4())

    def stubborn_auditor(_candidate):
        return copy.deepcopy(frozen_findings)

    result = run_repair_loop(_candidate_4(), stubborn_auditor, patch_limit=3)
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "no_progress"
    assert result["case"]["budgets"]["patches_used"] == 1


def test_non_numeric_error_routes_to_controlled_verdict():
    vague = {
        "plan_step": WEIGH_STEP,
        "workstation": "多通道固体称量工作站_V1",
        "operation_intent": "固体进样-文件传参-机器人",
        "key_values": {
            "上传文件逐瓶配方": [
                {"瓶号": "1", "加样量(g)": "按实测质量", "料罐号": "1"}
            ]
        },
    }
    result = run_repair_loop(_candidate(vague), recipe_auditor)
    assert result["status"] == "stopped"
    assert result["stop_reason"] in {"partial", "requires_llm", "manual"}
    assert result["case"]["draft"]["candidate"]["device_plan"][0]["key_values"][
        "上传文件逐瓶配方"
    ][0]["加样量(g)"] == "按实测质量"


# ---------------------------------------------------------------------------
# 7. context contents: contract + errors, never the whole plan or secrets
# ---------------------------------------------------------------------------


def test_context_contains_contract_and_errors_but_not_the_whole_plan(monkeypatch):
    monkeypatch.setenv("REFINER_LLM_API_KEY", "unit-secret-sentinel")
    case = create_repair_case(
        _candidate_4(), recipe_auditor(_candidate_4())
    )
    context = build_repair_context(case)
    assert context["contract"]["version"] == plan_repair.recipe_contract_version()
    assert {e["field"] for e in context["field_errors"]} == {
        "瓶号",
        "加样量(g)",
        "料罐号",
    }
    assert context["target"]["draft_version"] == 1
    assert len(context["draft_excerpt"]) == 1  # only the failing step
    serialized = json.dumps(context, ensure_ascii=False)
    assert OTHER_WORKSTATION not in serialized  # unrelated step excluded
    assert "unit-secret-sentinel" not in serialized
    assert json.dumps(
        _candidate_4()["sample_control_matrix"], ensure_ascii=False
    ) not in serialized


def test_history_constraints_are_bound_to_contract_version():
    case = create_repair_case(
        _candidate_4(), recipe_auditor(_candidate_4())
    )
    validate_repair(case, _valid_patch(case), recipe_auditor)
    case["contracts"]["recipe_contract_version"] = "new-contract"
    context = build_repair_context(case)
    assert context["history_constraints"] == []


# ---------------------------------------------------------------------------
# 8. persistence round-trip and CLI
# ---------------------------------------------------------------------------


def test_case_persists_and_reloads(tmp_path):
    result = run_repair_loop(
        _candidate_4(), recipe_auditor, case_dir=tmp_path
    )
    saved = tmp_path / "repair_case.json"
    assert saved.is_file()
    loaded = plan_repair.load_repair_case(saved)
    assert loaded["case_id"] == result["case"]["case_id"]
    assert loaded["draft"]["status"] == "accepted"
    assert loaded["stop_reason"] == "accepted"


def test_cli_repairs_saved_state_offline(tmp_path, capsys):
    state = {
        "feasibility_progress": [
            {"status": "assembled_pending_global_audit", "candidate": _candidate_4()}
        ]
    }
    state_path = tmp_path / "device_state.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    report_path = tmp_path / "report.json"
    exit_code = plan_repair.main(
        [
            "--device-state",
            str(state_path),
            "--progress-index",
            "0",
            "--case-dir",
            str(tmp_path / "case"),
            "--output",
            str(report_path),
        ]
    )
    assert exit_code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "accepted"
    assert report["patches_used"] == 1
    assert report["final_candidate_digest"] != report["baseline_digest"]
    assert (tmp_path / "case" / "repair_case.json").is_file()


def test_cli_rejects_out_of_range_progress_index(tmp_path):
    state_path = tmp_path / "device_state.json"
    state_path.write_text(
        json.dumps({"feasibility_progress": []}), encoding="utf-8"
    )
    with pytest.raises(SystemExit):
        plan_repair.main(
            ["--device-state", str(state_path), "--progress-index", "4"]
        )


# ---------------------------------------------------------------------------
# 9. integration with the Stage-1 repair entry point
# ---------------------------------------------------------------------------


from single_agent import SingleDeviceAgent, SingleDeviceAgentState  # noqa: E402


class _NoLLMModel:
    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        raise AssertionError("patch-first repair must not call the LLM")


def _agent_state():
    return SingleDeviceAgentState(research_handoff={}, exp_id="unit-repair")


def test_stage1_repair_uses_patch_first_without_llm(monkeypatch):
    agent = SingleDeviceAgent(model=_NoLLMModel())
    plan = _candidate_4()
    monkeypatch.setattr(
        agent,
        "_plan_level_findings",
        lambda state, candidate: recipe_auditor(candidate),
    )
    state = _agent_state()
    repaired = agent._repair_plan_level_findings(state, plan)
    row = _weigh_key_values(repaired)["上传文件逐瓶配方"][0]
    assert row["加样量(g)"] == 0.01 and isinstance(row["加样量(g)"], float)
    assert row["瓶号"] == 1 and isinstance(row["瓶号"], int)
    assert repaired["status"] == "device_plan"
    assert any("patch-first repair accepted" in line for line in state.logs)


def test_stage1_repair_falls_back_to_regeneration_on_stop(monkeypatch):
    agent = SingleDeviceAgent(model=_NoLLMModel())
    plan = _candidate_4()
    frozen_findings = recipe_auditor(plan)
    monkeypatch.setattr(
        agent,
        "_plan_level_findings",
        lambda state, candidate: copy.deepcopy(frozen_findings),
    )
    monkeypatch.setattr(
        agent, "_verified_stage1_core_route_gap_result", lambda state, current: None
    )

    regenerated = _candidate_4()
    regenerated["device_plan"].append(
        {
            "plan_step": 100,
            "workstation": "再生成的标记步骤站",
            "operation_intent": "标记",
        }
    )
    monkeypatch.setattr(
        agent,
        "_invoke_feasibility_plan",
        lambda state, extra_instruction=None: copy.deepcopy(regenerated),
    )
    state = _agent_state()
    result = agent._repair_plan_level_findings(state, plan)
    assert any(
        step.get("plan_step") == 100 for step in result.get("device_plan", [])
    )
    assert any(
        "falling back to full regeneration" in line for line in state.logs
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

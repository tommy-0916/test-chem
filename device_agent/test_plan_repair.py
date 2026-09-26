"""Regression tests for the persistent, permission-constrained repair flow.

The four weighing-step fixtures replicate the real failed candidates from the
2026-09-17 kimi-k3 run (campaigns/a02-device-k3-20260917-183006): each one
failed the file-dosing recipe audit for a different, precisely diagnosed
reason.  None of these cases may pass because rules were relaxed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

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
    assert [c["rule"] for c in checks] == [
        plan_repair.STRUCTURED_RECIPE_ADAPTER_RULE
    ]
    assert checks[0]["rule_version"] == (
        plan_repair.STRUCTURED_RECIPE_ADAPTER_VERSION
    )
    assert checks[0]["status"] == "failed"

    two = recipe_auditor(_candidate_2())
    checks_two = failed_checks(two)
    # 文件瓶号/加样量_g are outside the contract, so no structured recipe is
    # recognized at all and the prose fallback is empty.
    assert checks_two[0]["rule"] == plan_repair.STRUCTURED_RECIPE_ADAPTER_RULE

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


def test_diagnose_candidate_blocks_malformed_auditor_findings():
    findings = diagnose_candidate(lambda _candidate: ["MALFORMED_FINDING"], {})

    assert len(findings) == 1
    assert findings[0]["type"] == "invalid_contract_shape"
    assert findings[0]["path"] == "auditor.findings[0]"


def test_diagnose_candidate_blocks_non_array_auditor_result():
    findings = diagnose_candidate(lambda _candidate: {"message": "not a list"}, {})

    assert len(findings) == 1
    assert findings[0]["type"] == "invalid_contract_shape"
    assert findings[0]["path"] == "auditor.findings"


def test_recipe_auditor_blocks_malformed_device_plan_records():
    findings = recipe_auditor({"device_plan": ["MALFORMED_STEP"]})

    assert len(findings) == 1
    assert findings[0]["type"] == "invalid_contract_shape"
    assert findings[0]["path"] == "device_plan[0]"


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


def test_plan_step_paths_preserve_scalar_type_and_zero():
    candidate = {
        "device_plan": [
            {"plan_step": 1, "marker": "integer-one"},
            {"plan_step": "1", "marker": "string-one"},
            {"plan_step": 0, "marker": "integer-zero"},
            {"plan_step": "0", "marker": "string-zero"},
        ]
    }
    for index, identifier, marker in (
        (0, 1, "integer-one"),
        (1, "1", "string-one"),
        (2, 0, "integer-zero"),
        (3, "0", "string-zero"),
    ):
        step_ref = plan_repair._plan_step_ref(identifier)
        container, key, _ = plan_repair.resolve_path(
            candidate, f"{step_ref}.marker"
        )
        assert container[key] == marker
        assert plan_repair.canonical_path(candidate, f"{step_ref}.marker") == (
            f"device_plan[{index}].marker"
        )

    assert plan_repair._plan_step_ref(1) != plan_repair._plan_step_ref("1")
    assert plan_repair._plan_step_ref(0) != plan_repair._plan_step_ref("0")


def test_plan_step_resolution_rejects_duplicate_typed_identity():
    candidate = {
        "device_plan": [
            {"plan_step": 0, "marker": "first"},
            {"plan_step": 0, "marker": "second"},
        ]
    }
    with pytest.raises(KeyError, match="ambiguous"):
        plan_repair.resolve_path(
            candidate, f"{plan_repair._plan_step_ref(0)}.marker"
        )


def test_failing_step_excerpt_does_not_cross_match_string_and_integer_ids():
    candidate = {
        "device_plan": [
            {"plan_step": 1, "workstation": "integer-station"},
            {"plan_step": "1", "workstation": "string-station"},
        ]
    }
    findings = [{"details": {"plan_step": "1", "checks": []}}]
    excerpts = plan_repair._failing_step_excerpts(candidate, findings)
    assert len(excerpts) == 1
    assert excerpts[0]["plan_step"] == "1"
    assert excerpts[0]["workstation"] == "string-station"
    assert excerpts[0]["step_ref"] == plan_repair._plan_step_ref("1")


def test_finding_and_check_identities_keep_plan_step_types_distinct():
    finding_int = {
        "type": "same",
        "message": "same",
        "details": {"plan_step": 1},
    }
    finding_text = copy.deepcopy(finding_int)
    finding_text["details"]["plan_step"] = "1"
    assert plan_repair._finding_identity(finding_int) != plan_repair._finding_identity(
        finding_text
    )
    check_int = {"plan_step": 0, "path": "x", "field": "y", "rule": "z"}
    check_text = {**check_int, "plan_step": "0"}
    assert plan_repair._check_identity(check_int) != plan_repair._check_identity(
        check_text
    )


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


def _weigh_step_numeric_strings(plan_step):
    """Patchable weigh step: contract field names, numeric-string values."""
    return {
        "plan_step": plan_step,
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


def _candidate_two_faults():
    """Two independently patchable recipe faults on distinct plan steps."""
    return {
        "status": "device_plan",
        "device_plan": [
            _weigh_step_numeric_strings(WEIGH_STEP),
            _weigh_step_numeric_strings(WEIGH_STEP + 1),
            _other_step(),
        ],
        "sample_control_matrix": [
            {"sample_id": "SAMPLE_GRP_MA_S01_R00_01_01", "group": "control"}
        ],
    }


def test_interrupted_repair_resumes_from_saved_draft(tmp_path):
    candidate = _candidate_two_faults()
    baseline_snapshot = copy.deepcopy(candidate)

    # First run: zero patch budget — the 403-quota kill shape.  The loop
    # stops before the first patch with every finding still open.
    first = run_repair_loop(
        candidate, recipe_auditor, case_dir=tmp_path, patch_limit=0
    )
    assert first["status"] == "stopped"
    assert first["stop_reason"] == "patch_budget_exhausted"
    case = plan_repair.load_repair_case(tmp_path / "repair_case.json")
    case_id = case["case_id"]
    assert case["draft"]["version"] == 1
    assert case["budgets"]["patches_used"] == 0
    # remaining_findings is the fresh diagnosis truth: both faults still open.
    assert len(case["remaining_findings"]) == 2
    assert case["baseline"]["candidate"] == baseline_snapshot

    # Operator grants budget and resumes: same case, no re-import, no restart.
    case["budgets"]["patch_limit"] = 2
    second = plan_repair.resume_repair_loop(case, recipe_auditor, case_dir=tmp_path)
    assert second["status"] == "accepted"
    final_case = second["case"]
    assert final_case["case_id"] == case_id
    assert final_case["draft"]["version"] == 2
    assert final_case["draft"]["parent_version"] == 1
    assert final_case["budgets"]["patches_used"] == 1
    assert final_case["remaining_findings"] == []
    # Log continuity: open -> stop -> resume -> ... -> promote -> accept.
    kinds = [entry["kind"] for entry in final_case["log"]]
    assert kinds[0] == "open"
    assert kinds.count("stop") == 1
    resume_entry = next(e for e in final_case["log"] if e["kind"] == "resume")
    assert resume_entry["draft_version"] == 1
    assert resume_entry["patches_used"] == 0
    assert resume_entry["remaining_findings"] == 2
    promote_entries = [e for e in final_case["log"] if e["kind"] == "promote"]
    assert [e["parent_version"] for e in promote_entries] == [1]
    assert [e["draft_version"] for e in promote_entries] == [2]
    assert all(e.get("patch_digest") for e in promote_entries)
    # Baseline still untouched; both faults actually repaired at the end.
    assert final_case["baseline"]["candidate"] == baseline_snapshot
    assert plan_repair.recipe_auditor(second["final_candidate"]) == []
    reloaded = plan_repair.load_repair_case(tmp_path / "repair_case.json")
    assert reloaded["draft"]["version"] == 2
    assert reloaded["draft"]["status"] == "accepted"


def test_resume_refuses_contract_drift(tmp_path, monkeypatch):
    run_repair_loop(_candidate_4(), recipe_auditor, case_dir=tmp_path)
    case = plan_repair.load_repair_case(tmp_path / "repair_case.json")
    # Force the accepted case back to working so the contract gate is what
    # stops the resume, not the already-accepted short-circuit.
    case["draft"]["status"] = "working"
    monkeypatch.setattr(
        plan_repair, "recipe_contract_version", lambda: "drifted-contract"
    )
    try:
        plan_repair.resume_repair_loop(case, recipe_auditor)
    except ValueError as exc:
        assert "contract_mismatch" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("resume must refuse a drifted contract")


def test_resume_on_accepted_case_is_noop(tmp_path):
    first = run_repair_loop(
        _candidate_4(), recipe_auditor, case_dir=tmp_path
    )
    assert first["status"] == "accepted"
    log_len = len(first["case"]["log"])
    second = plan_repair.resume_repair_loop(
        tmp_path / "repair_case.json", recipe_auditor
    )
    assert second["status"] == "accepted"
    assert second["case"]["budgets"]["patches_used"] == 1
    # No new rounds are driven on an already-accepted case.
    assert len(second["case"]["log"]) == log_len


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


def test_stage1_repair_controlled_stop_by_default_on_patch_stop(monkeypatch):
    monkeypatch.delenv("CHEM_DEVICE_ALLOW_PLAN_REGEN", raising=False)
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

    def forbidden_regen(state, extra_instruction=None):  # noqa: ANN001
        raise AssertionError("full regeneration must not run without authorization")

    monkeypatch.setattr(agent, "_invoke_feasibility_plan", forbidden_regen)
    state = _agent_state()
    result = agent._repair_plan_level_findings(state, plan)
    assert result.get("status") == "manual_required"
    assert result.get("failure_stage") == "plan_level_audit"
    error_package = result.get("error_package") or {}
    assert error_package.get("type") == "device_plan_audit_blocked"
    assert error_package.get("stop_reason") == "patch_first_repair_exhausted"
    assert error_package.get("structured_errors")
    assert not any(
        step.get("plan_step") == 100 for step in result.get("device_plan", [])
    )
    # The complete finding set is persisted before any repair decision.
    assert state.plan_audit_records
    record = state.plan_audit_records[-1]
    assert record["finding_count"] == len(frozen_findings)
    assert record["phase"] == "pre_repair"
    assert record["checks_executed"]
    assert result.get("plan_audit_record", {}).get("finding_signature")


def test_stage1_repair_regenerates_only_with_explicit_authorization(monkeypatch):
    monkeypatch.setenv("CHEM_DEVICE_ALLOW_PLAN_REGEN", "1")
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


def test_stage1_repair_non_patchable_findings_stop_without_regen(monkeypatch):
    monkeypatch.delenv("CHEM_DEVICE_ALLOW_PLAN_REGEN", raising=False)
    agent = SingleDeviceAgent(model=_NoLLMModel())
    plan = _candidate_4()
    judgement_findings = [
        {
            "type": "sample_matrix_drift",
            "message": "独立重复被合并后按质量分发，谱系不可恢复",
        }
    ]
    monkeypatch.setattr(
        agent,
        "_plan_level_findings",
        lambda state, candidate: copy.deepcopy(judgement_findings),
    )

    def forbidden_regen(state, extra_instruction=None):  # noqa: ANN001
        raise AssertionError("judgement findings must not trigger regeneration")

    def forbidden_patch(state, candidate):  # noqa: ANN001
        raise AssertionError("judgement findings must not enter patch-first")

    monkeypatch.setattr(agent, "_invoke_feasibility_plan", forbidden_regen)
    monkeypatch.setattr(agent, "_run_patch_first_repair", forbidden_patch)
    state = _agent_state()
    result = agent._repair_plan_level_findings(state, plan)
    assert result.get("status") == "manual_required"
    error_package = result.get("error_package") or {}
    assert error_package.get("stop_reason") == "plan_findings_require_judgement"
    assert "sample_matrix_drift" in json.dumps(
        error_package.get("structured_errors"), ensure_ascii=False
    )
    assert state.plan_audit_records


def test_plan_audit_record_persisted_to_disk_before_repair(monkeypatch, tmp_path):
    """Quota kills must never lose the diagnosis again: the complete finding
    set is written atomically to disk before any repair is dispatched."""
    from checkpoints import DeviceCheckpointStore

    agent = SingleDeviceAgent(model=_NoLLMModel())
    agent._checkpoint_store = DeviceCheckpointStore(
        tmp_path / "checkpoints",
        {"binding": "quota_survival_test"},
        resume=True,
        metadata={"exp_id": "quota_test"},
    )
    plan = _candidate_4()
    judgement_findings = [
        {
            "type": "sample_matrix_drift",
            "message": "独立重复被合并后按质量分发",
            "details": {"path": "device_plan[step=55]", "evidence": "step55 text"},
        }
    ]
    monkeypatch.setattr(
        agent,
        "_plan_level_findings",
        lambda state, candidate: copy.deepcopy(judgement_findings),
    )

    def quota_kill(state, extra_instruction=None):  # noqa: ANN001
        raise RuntimeError("simulated 403 quota stop")

    monkeypatch.setattr(agent, "_invoke_feasibility_plan", quota_kill)
    state = _agent_state()
    state.exp_id = "quota_test"
    result = agent._repair_plan_level_findings(state, plan)
    assert result.get("status") == "manual_required"

    audit_dir = tmp_path / "device_plan_audits" / "quota_test"
    records = sorted(audit_dir.glob("audit_*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["finding_count"] == 1
    assert record["findings"][0]["type"] == "sample_matrix_drift"
    assert record["findings"][0]["path"] == "device_plan[step=55]"
    assert record["checks_executed"]
    # The same record is also in runtime state for the terminal package.
    assert state.plan_audit_records[0]["finding_signature"] == record["finding_signature"]


def test_candidate_json_import_marks_unverified_draft(tmp_path):
    """Importing a preserved candidate never counts as a checkpoint hit."""
    candidate_src = (
        Path(__file__).resolve().parents[1]
        / "regression_inputs"
        / "a01_crash1"
        / "candidate_1_83steps_UNAPPROVED.json"
    )
    if not candidate_src.exists():
        pytest.skip("A01 crash regression input not available")
    candidate = tmp_path / "candidate.json"
    candidate.write_text(candidate_src.read_text(encoding="utf-8"), encoding="utf-8")
    case_dir = tmp_path / "case"
    report_path = tmp_path / "report.json"

    rc = plan_repair.main(
        [
            "--candidate-json",
            str(candidate),
            "--case-dir",
            str(case_dir),
            "--output",
            str(report_path),
        ]
    )
    assert rc == 1  # stopped (requires_llm), not accepted
    case = json.loads((case_dir / "repair_case.json").read_text(encoding="utf-8"))
    provenance = case.get("provenance") or {}
    assert provenance.get("channel") == "candidate_json_import"
    assert provenance.get("imported_as") == "unverified_draft"
    assert provenance.get("verification_status") == "not_reverified"
    # The baseline preserves the imported candidate verbatim (83 steps).
    assert len(case["baseline"]["candidate"]["device_plan"]) == 83
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["provenance"]["channel"] == "candidate_json_import"


def test_quota_stop_preserves_scientific_review_flag(monkeypatch):
    """Run-layer failure (quota/transport) must not flip the candidate's
    requires_scientific_review obligation to false in the terminal package."""
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    agent._active_semantic_analysis = {}
    agent._dispatch_catalog = object()
    monkeypatch.setattr(agent, "_device_snapshot_id", lambda: "test-snapshot")
    monkeypatch.setattr(agent, "_full_device_truth_digest", lambda: "test-truth")
    monkeypatch.setattr(
        agent, "_verified_stage1_core_route_gap_result", lambda *_: None
    )
    state = _agent_state()
    state.feasibility_progress = [
        {
            "candidate_number": 1,
            "status": "failed",
            "candidate": {
                "requires_scientific_review": True,
                "device_plan": [{"plan_step": 55, "operation_intent": "称量"}],
            },
        }
    ]
    result = {
        "status": "failed",
        "feedback_type": "device_internal_error",
        "workflow_json": {},
        "workflow_txt": "",
        "device_plan": [],
        "dispatch_validation": {"status": "failed", "errors": ["quota stop"]},
    }
    package = agent._normalize_terminal_package(state, result)
    assert package["requires_scientific_review"] is True


# ---------------------------------------------------------------------------
# Strict unstructured-recipe schema adaptation (0-token)
# ---------------------------------------------------------------------------


def _legacy_weigh_step(plan_step, *, rows=((1, "0.0742", 1, 5), (2, "0.0742", 1, 6)),
                       mass_override=None, mapping_override=None,
                       containers_override=None, header_override=None):
    """Complete wrapper-schema recipe from an arbitrary experiment context."""
    csv_rows = []
    mapping_parts = []
    containers = []
    hoppers = set()
    for bottle, mass, hopper, container in rows:
        csv_rows.append(f"{bottle},{mass_override or mass},{hopper}")
        mapping_parts.append(f"文件瓶号{bottle}=容器{container}")
        containers.append(container)
        hoppers.add(hopper)
    assert len(hoppers) == 1
    kv = {
        "容器类型": "进样瓶",
        "容器数量": str(len(rows)),
        "容器编号": "[" + ",".join(str(c) for c in containers) + "]",
        "上传文件CSV表头": header_override or "瓶号,加样量(g),料罐号",
        "上传文件CSV行": csv_rows,
        "瓶号映射": mapping_override or "；".join(mapping_parts),
        "料罐号约束": f"单文件料罐号全局统一={next(iter(hoppers))}",
    }
    if containers_override is not None:
        kv["容器编号"] = containers_override
    return {
        "plan_step": plan_step,
        "workstation": "多通道固体称量工作站_V1",
        "operation_intent": "固体进样-文件传参-机器人",
        "key_values": kv,
        "notes": "None",
    }


def _legacy_candidate(*steps):
    return {
        "status": "device_plan",
        "device_plan": list(steps) + [_other_step()],
        "sample_control_matrix": [
            {"sample_id": "arbitrary-experiment-control", "group": "control"}
        ],
    }


def test_legacy_recipe_normalized_deterministically():
    candidate = _legacy_candidate(_legacy_weigh_step(WEIGH_STEP))
    result = run_repair_loop(candidate, recipe_auditor)
    assert result["status"] == "accepted"
    final = result["final_candidate"]
    step = next(s for s in final["device_plan"] if s.get("plan_step") == WEIGH_STEP)
    kv = step["key_values"]
    rows = kv["上传文件逐瓶配方"]
    assert rows == [
        {"瓶号": 1, "实际容器编号": 5, "加样量(g)": 0.0742, "料罐号": 1},
        {"瓶号": 2, "实际容器编号": 6, "加样量(g)": 0.0742, "料罐号": 1},
    ]
    for legacy_key in ("上传文件CSV表头", "上传文件CSV行", "瓶号映射", "料罐号约束"):
        assert legacy_key not in kv
    assert kv["容器编号"] == [5, 6]
    assert kv["容器数量"] == 2
    # The original CSV text is preserved as materialization evidence.
    assert "1,0.0742,1" in kv["上传文件CSV内容"]
    assert recipe_auditor(final) == []

    # Re-running the repair on a contract-form candidate is a no-op.
    rerun = run_repair_loop(copy.deepcopy(final), recipe_auditor)
    assert rerun["status"] == "accepted"
    assert rerun["final_candidate"] == final
    assert rerun["case"]["budgets"]["patches_used"] == 0


def test_recipe_wrapper_conflicting_derived_target_is_refused():
    step = _legacy_weigh_step(WEIGH_STEP)
    step["key_values"]["上传文件逐瓶配方"] = [
        {"瓶号": 1, "实际容器编号": 999, "加样量(g)": 0.0742, "料罐号": 1}
    ]
    result = run_repair_loop(_legacy_candidate(step), recipe_auditor)
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "requires_llm"
    assert result["case"]["draft"]["version"] == 1


def test_legacy_two_steps_fixed_by_one_batched_patch():
    candidate = _legacy_candidate(
        _legacy_weigh_step(WEIGH_STEP),
        _legacy_weigh_step(
            WEIGH_STEP + 1, rows=((1, "0.0100", 3, 11), (2, "0.0100", 3, 12))
        ),
    )
    result = run_repair_loop(candidate, recipe_auditor)
    assert result["status"] == "accepted"
    # One patch covers both steps: deterministic repair batches per round.
    assert result["case"]["budgets"]["patches_used"] == 1
    assert result["case"]["remaining_findings"] == []


def test_legacy_ambiguous_mass_is_never_materialized():
    step = _legacy_weigh_step(WEIGH_STEP, mass_override="适量")
    result = run_repair_loop(_legacy_candidate(step), recipe_auditor)
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "requires_llm"
    stop = next(e for e in result["case"]["log"] if e["kind"] == "stop")
    reasons = json.dumps(stop.get("unpatchable") or [], ensure_ascii=False)
    assert "歧义" in reasons or "not unambiguously normalizable" in reasons
    # The draft is left untouched — the finding stays open.
    assert result["case"]["draft"]["version"] == 1
    assert len(result["case"]["remaining_findings"]) == 1


@pytest.mark.parametrize("mass", ["0.0742 g", "0.0742 克", "0.0742 G"])
def test_recipe_wrapper_uses_shared_exact_mass_unit_aliases(mass):
    step = _legacy_weigh_step(WEIGH_STEP, mass_override=mass)
    result = run_repair_loop(_legacy_candidate(step), recipe_auditor)
    assert result["status"] == "accepted"
    rows = _weigh_key_values(result["final_candidate"])["上传文件逐瓶配方"]
    assert [row["加样量(g)"] for row in rows] == [0.0742, 0.0742]


def test_recipe_wrapper_rejects_dimensionally_wrong_mass_unit():
    step = _legacy_weigh_step(WEIGH_STEP, mass_override="0.0742 mg")
    result = run_repair_loop(_legacy_candidate(step), recipe_auditor)
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "requires_llm"
    assert result["case"]["draft"]["version"] == 1


def test_legacy_incomplete_mapping_is_never_guessed():
    step = _legacy_weigh_step(WEIGH_STEP, mapping_override="文件瓶号1=容器5")
    result = run_repair_loop(_legacy_candidate(step), recipe_auditor)
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "requires_llm"
    assert result["case"]["draft"]["version"] == 1


def test_legacy_container_mismatch_stays_open():
    step = _legacy_weigh_step(WEIGH_STEP, containers_override="[5,7]")
    result = run_repair_loop(_legacy_candidate(step), recipe_auditor)
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "requires_llm"
    assert result["case"]["draft"]["version"] == 1


def test_legacy_header_mismatch_stays_open():
    step = _legacy_weigh_step(WEIGH_STEP, header_override="瓶号,质量,料罐")
    result = run_repair_loop(_legacy_candidate(step), recipe_auditor)
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "requires_llm"
    assert result["case"]["draft"]["version"] == 1


def test_wrapper_hopper_constraint_with_unstructured_comment_rejected():
    step = _legacy_weigh_step(WEIGH_STEP)
    step["key_values"]["料罐号约束"] = (
        "单文件料罐号全局统一=1，因此该组分独立进行第二次称量运行"
    )
    result = run_repair_loop(_legacy_candidate(step), recipe_auditor)
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "requires_llm"
    assert result["case"]["draft"]["version"] == 1


def test_legacy_hopper_constraint_with_digit_comment_rejected():
    step = _legacy_weigh_step(WEIGH_STEP)
    step["key_values"]["料罐号约束"] = "单文件料罐号全局统一=1，但第3行改用2号料罐"
    result = run_repair_loop(_legacy_candidate(step), recipe_auditor)
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "requires_llm"
    assert result["case"]["draft"]["version"] == 1


def test_semantic_regression_rejects_patch():
    """A patch that re-breaks a resolved semantic finding is rejected."""

    def regressing_auditor(candidate):
        recipe_findings = recipe_auditor(candidate)
        if recipe_findings:
            return recipe_findings
        # The patch cleaned the recipe, but the sample matrix "drifted"
        # under it — a semantic regression the gate must catch.
        return [
            {
                "type": "sample_matrix_drift",
                "message": "sample matrix changed under the patch",
                "details": {},
            }
        ]

    result = run_repair_loop(_candidate_4(), regressing_auditor)
    assert result["status"] == "stopped"
    assert result["stop_reason"].startswith(
        "patch_rejected:semantic_regression:sample_matrix_drift"
    )
    assert result["case"]["draft"]["version"] == 1
    kinds = [e["kind"] for e in result["case"]["log"]]
    assert "regression" in kinds
    assert "promote" not in kinds


# ---------------------------------------------------------------------------
# Contract-declared mass scalars in unstructured recipe rows
# ---------------------------------------------------------------------------


def _unit_mass_candidate(raw="0.0178 g", extra_row_keys=None, rows=None):
    if rows is None:
        row = {"瓶号": 1, "加样量": raw, "料罐号": 7}
        if extra_row_keys:
            row.update(extra_row_keys)
        rows = row
    step = {
        "plan_step": WEIGH_STEP,
        "workstation": "多通道固体称量工作站_V1",
        "operation_intent": "固体进样-文件传参-机器人",
        "key_values": {
            "容器类型": "进样瓶",
            "容器数量": 1,
            "容器编号": [5],
            "上传文件": "Multi_Channel_Solid_Weighing_Workstation_V1_固体进样文件传参.xlsx",
            "配方行": rows,
        },
    }
    return _candidate(step)


def test_unit_mass_form_is_flagged_with_precise_check():
    findings = recipe_auditor(_unit_mass_candidate())
    assert len(findings) == 1
    assert findings[0]["type"] == "missing_concrete_recipe_evidence"
    checks = failed_checks(findings)
    assert len(checks) == 1
    check = checks[0]
    assert check["field"] == "加样量(g)"
    assert check["rule"] == "json_number"
    assert check["actual"] is None
    assert check["path"] == "key_values.配方行"
    passed = {
        c["field"] for c in findings[0]["details"]["checks"] if c["status"] == "passed"
    }
    assert passed == {"瓶号", "料罐号"}


@pytest.mark.parametrize("raw", ["0.0178 g", "0.0178 克", "0.0178 G"])
def test_contract_mass_exact_unit_alias_normalized_and_accepted(raw):
    result = run_repair_loop(_unit_mass_candidate(raw=raw), recipe_auditor)
    assert result["status"] == "accepted"
    assert result["case"]["budgets"]["patches_used"] == 1
    row = _weigh_key_values(result["final_candidate"])["配方行"]
    assert row == {"瓶号": 1, "料罐号": 7, "加样量(g)": 0.0178}
    assert isinstance(row["加样量(g)"], float)
    # Precision: the decimal text round-trips exactly through JSON.
    assert json.dumps(row["加样量(g)"]) == "0.0178"
    # 瓶号/料罐号/容器映射 untouched; baseline keeps the raw text.
    kv = _weigh_key_values(result["final_candidate"])
    assert kv["容器编号"] == [5] and kv["容器数量"] == 1
    baseline_row = _weigh_key_values(result["case"]["baseline"]["candidate"])["配方行"]
    assert baseline_row["加样量"] == raw
    # The patch op records rule version and original raw text (provenance).
    log = result["case"]["log"]
    assert any(e["kind"] == "promote" for e in log)

    rerun = run_repair_loop(copy.deepcopy(result["final_candidate"]), recipe_auditor)
    assert rerun["status"] == "accepted"
    assert rerun["final_candidate"] == result["final_candidate"]
    assert rerun["case"]["budgets"]["patches_used"] == 0


def test_contract_mass_patch_records_shared_normalizer_evidence():
    candidate = _unit_mass_candidate(raw="0.0178 克")
    case = create_repair_case(candidate, recipe_auditor(candidate))
    proposal = propose_repair(build_repair_context(case), case["draft"]["candidate"])
    assert proposal["status"] == "patch_ready"
    replace = next(op for op in proposal["patch"]["ops"] if op["op"] == "replace")
    assert replace["rule"] == plan_repair.CONTRACT_SCALAR_RULE_ID
    assert replace["adapter_rule"] == plan_repair.CONTRACT_MASS_FIELD_ADAPTER_RULE
    assert replace["adapter_rule_version"] == (
        plan_repair.CONTRACT_MASS_FIELD_ADAPTER_VERSION
    )
    assert replace["normalization"] == {
        "rule_id": plan_repair.CONTRACT_SCALAR_RULE_ID,
        "declared_type": "number",
        "declared_unit": "g",
        "input_unit": "克",
        "canonical_unit": "g",
        "reason": "normalized",
    }


@pytest.mark.parametrize(
    "raw",
    [
        "约0.0178 g",  # approximation marker
        "0.01–0.02 g",  # range, not a single value
        "0.0178 mg",  # unsupported unit
        "根据实测值",  # runtime-unknown
        "0.0178 g左右",  # trailing commentary
        "1 g; 2 g",  # two values in one string
    ],
)
def test_unit_mass_ambiguous_values_are_refused(raw):
    result = run_repair_loop(_unit_mass_candidate(raw=raw), recipe_auditor)
    assert result["status"] == "stopped"
    assert result["case"]["draft"]["version"] == 1  # nothing promoted
    row = _weigh_key_values(result["case"]["draft"]["candidate"])["配方行"]
    assert row["加样量"] == raw  # untouched
    assert "加样量(g)" not in row


def test_unit_mass_non_string_value_is_refused():
    # A bare number under the off-contract key is a rename, out of v1 scope.
    result = run_repair_loop(_unit_mass_candidate(raw=0.0178), recipe_auditor)
    assert result["status"] == "stopped"
    assert result["case"]["draft"]["version"] == 1


def test_unit_mass_conflict_with_existing_contract_key_is_refused():
    candidate = _unit_mass_candidate(extra_row_keys={"加样量(g)": 9.9})
    check = {
        "field": "加样量(g)",
        "path": "key_values.配方行",
        "rule": "json_number",
        "actual": None,
    }
    normalized, reasons = plan_repair.normalize_contract_mass_row(
        candidate, f"device_plan[step={WEIGH_STEP}]", check
    )
    assert normalized is None
    assert any("不一致" in reason for reason in reasons)


def test_unit_mass_list_rows_patch_only_failing_row():
    rows = [
        {"瓶号": 1, "加样量": "0.010 g", "料罐号": 1},
        {"瓶号": 2, "加样量(g)": 0.02, "料罐号": 1},
    ]
    result = run_repair_loop(_unit_mass_candidate(rows=rows), recipe_auditor)
    assert result["status"] == "accepted"
    repaired_rows = _weigh_key_values(result["final_candidate"])["配方行"]
    assert repaired_rows[0] == {"瓶号": 1, "料罐号": 1, "加样量(g)": 0.01}
    assert repaired_rows[1] == {"瓶号": 2, "加样量(g)": 0.02, "料罐号": 1}


def test_mixed_findings_promote_recipe_patch_and_keep_semantic_blocked():
    """Item-wise triage: recipe fixed deterministically; semantic stays open."""

    def mixed_auditor(candidate):
        findings = recipe_auditor(candidate)
        findings.append(
            {
                "type": "frozen_material_transition_coverage_missing",
                "message": "macro step 6 缺 drying 覆盖",
                "details": {},
            }
        )
        return findings

    result = run_repair_loop(_unit_mass_candidate(), mixed_auditor)
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "manual_no_patchable_checks"
    assert result["draft_status"] == "working"
    assert result["case"]["budgets"]["patches_used"] == 1
    # Progress saved: the draft carries the repaired recipe row...
    row = _weigh_key_values(result["final_candidate"])["配方行"]
    assert row["加样量(g)"] == 0.0178 and "加样量" not in row
    # ...and exactly the semantic finding remains, still blocking.
    remaining = result["case"]["remaining_findings"]
    assert len(remaining) == 1
    assert remaining[0]["type"] == "frozen_material_transition_coverage_missing"
    kinds = [e["kind"] for e in result["case"]["log"]]
    assert "promote_partial" in kinds and "promote" not in kinds
    # Baseline untouched.
    assert "加样量" in _weigh_key_values(result["case"]["baseline"]["candidate"])["配方行"]


def test_subset_promotion_rejects_patch_that_introduces_new_finding():
    """A patch fixing its own check but surfacing a NEW finding is rejected."""

    def mutating_auditor(candidate):
        findings = recipe_auditor(candidate)
        step = candidate["device_plan"][0]
        row = step.get("key_values", {}).get("配方行")
        if isinstance(row, dict) and "加样量(g)" in row:
            findings.append(
                {
                    "type": "offline_handoff_gap",
                    "message": "a brand new problem that was never diagnosed",
                    "details": {},
                }
            )
        return findings

    result = run_repair_loop(_unit_mass_candidate(), mutating_auditor)
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "no_progress"
    assert result["case"]["draft"]["version"] == 1  # patch never promoted
    row = _weigh_key_values(result["case"]["draft"]["candidate"])["配方行"]
    assert row["加样量"] == "0.0178 g"


def test_partial_patch_promotes_resolved_checks_only():
    """One check fixed, one not: progress saves at check granularity."""
    step = {
        "plan_step": WEIGH_STEP,
        "workstation": "多通道固体称量工作站_V1",
        "operation_intent": "固体进样-文件传参-机器人",
        "key_values": {
            "容器类型": "进样瓶",
            "容器数量": 1,
            "容器编号": [5],
            "上传文件": "Multi_Channel_Solid_Weighing_Workstation_V1_固体进样文件传参.xlsx",
            "配方行": {"瓶号": 1, "加样量": "0.0178 g", "料罐号": "运行时确定"},
        },
    }
    case = create_repair_case(_candidate(step), recipe_auditor(_candidate(step)))
    context = build_repair_context(case)
    proposal = propose_repair(context, case["draft"]["candidate"])
    assert proposal["status"] == "partial"
    assert proposal["patch"] is not None  # patchable subset still proposed
    assert proposal["patchable_count"] == 1
    assert any(item["field"] == "料罐号" for item in proposal["unpatchable"])
    result = validate_repair(case, proposal["patch"], recipe_auditor)
    assert result["status"] == "promoted_partial"
    assert result["remaining_findings"] == 1
    row = _weigh_key_values(case["draft"]["candidate"])["配方行"]
    assert row["加样量(g)"] == 0.0178  # resolved check landed
    assert row["料罐号"] == "运行时确定"  # unresolved check untouched
    remaining_checks = failed_checks(case["draft"]["diagnosis"])
    assert [c["field"] for c in remaining_checks] == ["料罐号"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

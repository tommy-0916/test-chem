"""Main workflow end-to-end smoke tests."""

import json
import logging
import os
from typing import Optional

from utils.llm_factory import LLMFactory
from utils.paths import exp_logs_dir
from workflow import MainWorkflow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


SUCCESS_MACRO_PLAN = """
在水相中采用 EDTA 辅助共沉淀法合成 K2Mn[Fe(CN)6] 基线样品。先完成 Mn-EDTA 预络合，再与 K4[Fe(CN)6] 体系缓慢接触。全流程使用 2 个进样瓶并行，单瓶总液体体积控制在约 20 mL，室温磁搅 4 h，随后留固离心洗涤 3 次，每次后超声重分散约 60 s，最后在 80 ℃ 干燥 8-12 h。全过程严禁酸化，废液按碱性含氰废液单独收集。
""".strip()

FEASIBILITY_ERROR_MACRO_PLAN = """
本轮要求同时并行完成三条互不相同的样品线，而且三条线必须同步开始、同步结束，不能分批串行。A 线要求在磁力搅拌工作站以 700 r/min 室温搅拌 4 h，B 线要求在另一套独立磁力搅拌工作站以 1200 r/min 室温搅拌 4 h，C 线要求在超声清洗工作站持续超声 120 s。三条线完成后，还要求 A/B/C 三组样品同时进入纯化工作站各自完成留固离心，且每条线都必须独占一套设备，不能互相轮流占用。若当前实验室只有单套磁搅、单套超声和单套纯化设备，则直接判定本轮不可执行。
""".strip()


def save_test_results(state, result_package, case_name):
    test_results_dir = os.path.join(str(exp_logs_dir()), state.exp_id)
    os.makedirs(test_results_dir, exist_ok=True)

    with open(os.path.join(test_results_dir, f"test_result_package_{case_name}.json"), "w", encoding="utf-8") as f:
        json.dump(result_package, f, ensure_ascii=False, indent=2)

    with open(os.path.join(test_results_dir, f"test_state_summary_{case_name}.json"), "w", encoding="utf-8") as f:
        json.dump({
            "case_name": case_name,
            "exp_id": state.exp_id,
            "iteration_id": state.iteration_id,
            "workflow_id": state.workflow_id,
            "macro_plan": state.macro_plan,
            "verification_result": state.verification_result,
            "verification_category": state.verification_category,
            "blocking_constraints": state.blocking_constraints,
            "status": state.status,
        }, f, ensure_ascii=False, indent=2)


def run_case(case_name: str, macro_plan: str, expected_status: Optional[str] = None):
    model = LLMFactory.create()
    workflow = MainWorkflow(
        model=model,
        use_temp_data_flow=True,
        use_new_format=True,
        max_verify_retries=3,
    )

    state = workflow.run_state(macro_plan)
    result_package = state.terminal_package
    logger.info("[%s] terminal status: %s", case_name, result_package.get("status"))
    logger.info("[%s] verification result: %s / %s", case_name, state.verification_result, state.verification_category)
    logger.info("[%s] workflow id: %s", case_name, state.workflow_id)
    if result_package.get("status") == "success":
        logger.info("[%s] steps: %s", case_name, len((result_package.get("workflow_json") or {}).get("steps", [])))
    else:
        logger.info("[%s] blocking constraints: %s", case_name, result_package.get("error_package", {}).get("blocking_constraints"))

    save_test_results(state, result_package, case_name)
    if expected_status is not None and result_package.get("status") != expected_status:
        raise AssertionError(f"{case_name}: expected {expected_status}, got {result_package.get('status')}")
    return state, result_package


def test_main_workflow_success():
    return run_case("success", SUCCESS_MACRO_PLAN, "success")


def smoke_test_main_workflow_feasibility_case():
    return run_case("feasibility_case", FEASIBILITY_ERROR_MACRO_PLAN, None)


if __name__ == "__main__":
    run_case("success", SUCCESS_MACRO_PLAN, None)
    run_case("feasibility_case", FEASIBILITY_ERROR_MACRO_PLAN, None)

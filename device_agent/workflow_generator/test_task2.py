"""Workflow Generator Task2 测试。"""

import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

TEST_EXP_DIR = "/workspace/chem_resources/exp_logs/test_task2_exp_20260319_003"


def load_test_data():
    iter_path = os.path.join(TEST_EXP_DIR, "iteration0.json")
    exp_log_path = os.path.join(TEST_EXP_DIR, "exp_log.json")

    with open(iter_path, encoding="utf-8") as f:
        iter_data = json.load(f)
    with open(exp_log_path, encoding="utf-8") as f:
        exp_log = json.load(f)

    workflows = iter_data.get("workflows", [])
    assert workflows, "测试数据中没有 workflow 记录"
    source_workflow = workflows[0]
    assert source_workflow.get("verification_result") == "refused", "测试数据的 workflow 不是 refused"

    return {
        "final_goal": exp_log.get("final_goal", ""),
        "macro_plan": exp_log.get("macro_plan", exp_log.get("final_goal", "")),
        "macro_plan_summary": iter_data.get("macro_plan_summary", ""),
        "observation_requirements": iter_data.get("observation_requirements", {}),
        "goal_in_this_iteration": iter_data.get("goal_in_this_iteration", ""),
        "knowledge": iter_data.get("knowledge", ""),
        "related_workflows_txt": iter_data.get("related_workflows_txt", ""),
        "source_workflow_txt": source_workflow.get("workflow_txt", ""),
        "verification_suggestion": source_workflow.get("verification_suggestion", ""),
        "source_workflow_id": source_workflow.get("workflow_id", 0),
    }


def run_test():
    from workflow_generator.workflow import WorkflowGenerator
    from workflow_generator.state import WorkflowGeneratorTestState

    data = load_test_data()
    with open("/workspace/chem_resources/format_reference/reference.txt", encoding="utf-8") as f:
        txt_format_reference = f.read()

    exp_log_path = os.path.join(TEST_EXP_DIR, "exp_log.json")
    state = WorkflowGeneratorTestState(
        final_goal=data["final_goal"],
        macro_plan=data["macro_plan"],
        macro_plan_summary=data["macro_plan_summary"],
        observation_requirements=data["observation_requirements"],
        goal_in_this_iteration=data["goal_in_this_iteration"],
        knowledge=data["knowledge"],
        related_workflows_txt=data["related_workflows_txt"],
        txt_format_reference=txt_format_reference,
        iteration_id=0,
        workflow_id=data["source_workflow_id"] + 1,
        exp_log_path=exp_log_path,
    )

    agent = WorkflowGenerator(exp_log_path=exp_log_path, use_new_format=True)
    result_state = agent.run_task2(
        state=state,
        source_workflow_txt=data["source_workflow_txt"],
        verification_suggestion=data["verification_suggestion"],
    )

    assert result_state.workflow_skeleton_txt.strip(), "workflow_skeleton_txt 为空"
    assert result_state.workflow_txt.strip(), "workflow_txt 为空"

    iter_path = os.path.join(TEST_EXP_DIR, "iteration0.json")
    with open(iter_path, encoding="utf-8") as f:
        iter_data_after = json.load(f)

    workflows_after = iter_data_after.get("workflows", [])
    assert len(workflows_after) >= 2, "workflows 未追加新记录"
    new_workflow = workflows_after[-1]
    assert new_workflow.get("workflow_id") == result_state.workflow_id, "workflow_id 未对齐"
    assert new_workflow.get("workflow_txt"), "新 workflow_txt 为空"
    assert new_workflow.get("workflow_skeleton_txt"), "新 workflow_skeleton_txt 为空"

    print("Task2 test passed")
    return result_state


if __name__ == "__main__":
    os.chdir("/workspace/chem_agent")
    sys.path.insert(0, "/workspace/chem_agent")
    run_test()

"""
反馈数据通路回归测试
====================

覆盖内容:
1. 主 workflow 在 task2 返回实际 workflow_id 后，应继续沿用该 ID
2. Workflow Generator task2 应以日志中实际追加后的 workflow_id 为准

运行方式:
    cd /workspace/chem_agent
    source .venv/bin/activate
    python test_feedback_data_flow.py
"""

import json
import os
import shutil
import uuid

from state import WorkflowState
from workflow import MainWorkflow
from workflow_generator.state import WorkflowGeneratorTestState
from workflow_generator.workflow import WorkflowGenerator
from utils.paths import exp_logs_dir


EXP_LOG_BASE = str(exp_logs_dir())


class DummyModel:
    """不会真的发请求，只是满足 BaseAgent 初始化要求。"""

    def invoke(self, messages):
        raise AssertionError("DummyModel.invoke should not be called in this test")


class FakeVerifyAgent:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def run(self, state):
        result, suggestion = self._responses.pop(0)
        self.calls.append(
            {
                "workflow_id": state.workflow_id,
                "workflow_txt": state.workflow_txt,
                "result": result,
                "suggestion": suggestion,
            }
        )
        state.verification_result = result
        state.verification_suggestion = suggestion
        state.status = "success"
        return state


class FakeWorkflowGenerator:
    def __init__(self, actual_workflow_ids):
        self._actual_workflow_ids = list(actual_workflow_ids)
        self.calls = []

    def run_task2(self, state, source_workflow_txt, verification_suggestion):
        actual_workflow_id = self._actual_workflow_ids.pop(0)
        self.calls.append(
            {
                "requested_workflow_id": state.workflow_id,
                "actual_workflow_id": actual_workflow_id,
                "source_workflow_txt": source_workflow_txt,
                "verification_suggestion": verification_suggestion,
            }
        )
        state.workflow_id = actual_workflow_id
        state.workflow_txt = f"regen-{actual_workflow_id}-from-{source_workflow_txt}"
        state.status = "success"
        return state


class StubLoader:
    def format_for_prompt(self):
        return "[stub workstation descriptions]"


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def prepare_iteration_dir():
    exp_id = f"test_feedback_data_flow_{uuid.uuid4().hex[:8]}"
    exp_dir = os.path.join(EXP_LOG_BASE, exp_id)
    os.makedirs(exp_dir, exist_ok=True)

    exp_log_path = os.path.join(exp_dir, "exp_log.json")
    iteration_path = os.path.join(exp_dir, "iteration0.json")

    exp_log = {
        "final_goal": "优化普鲁士蓝的电化学性能",
        "iterations": [
            {
                "iteration_id": 0,
                "iteration_file": iteration_path,
            }
        ],
    }
    iteration = {
        "iteration_id": 0,
        "knowledge": "knowledge",
        "related_workflows_txt": "related workflows",
        "goal_in_this_iteration": "goal in this iteration",
        "workflows": [
            {
                "workflow_id": 0,
                "workflow_txt": "workflow-0",
                "verification_result": "refused",
                "verification_suggestion": "fix workflow-0",
            },
            {
                "workflow_id": 1,
                "workflow_txt": "workflow-1",
                "verification_result": "refused",
                "verification_suggestion": "fix workflow-1",
            },
        ],
    }

    with open(exp_log_path, "w", encoding="utf-8") as f:
        json.dump(exp_log, f, ensure_ascii=False, indent=4)
    with open(iteration_path, "w", encoding="utf-8") as f:
        json.dump(iteration, f, ensure_ascii=False, indent=4)

    return exp_dir, exp_log_path, iteration_path


def test_main_workflow_uses_task2_returned_workflow_id():
    main_workflow = object.__new__(MainWorkflow)
    main_workflow._max_verify_retries = 3
    main_workflow._verify_agent = FakeVerifyAgent(
        [
            ("refused", "first fix"),
            ("accepted", "looks good"),
        ]
    )
    main_workflow._workflow_generator = FakeWorkflowGenerator([7])

    state = WorkflowState(
        final_goal="goal",
        exp_id="exp-test",
        exp_log_path="/tmp/exp-test/exp_log.json",
        iteration_id=0,
        workflow_id=0,
        workstation_descriptions=[],
        txt_format_reference="reference",
        json_format_reference="{}",
        use_temp_data_flow=False,
        knowledge="knowledge",
        related_workflows_txt="related workflows",
        goal_in_this_iteration="goal in this iteration",
        workflow_txt="workflow-0",
    )

    updated_state = main_workflow._step4_verify_agent_with_retry(state)

    verify_ids = [call["workflow_id"] for call in main_workflow._verify_agent.calls]
    task2_requested_ids = [
        call["requested_workflow_id"] for call in main_workflow._workflow_generator.calls
    ]

    assert_equal(verify_ids, [0, 7], "Verify Agent should inspect the regenerated workflow ID")
    assert_equal(task2_requested_ids, [1], "Task 2 should still request the next workflow slot first")
    assert_equal(updated_state.workflow_id, 7, "Main workflow should sync to task2 returned workflow_id")
    assert_equal(
        updated_state.workflow_txt,
        "regen-7-from-workflow-0",
        "Main workflow should keep the regenerated workflow text",
    )
    assert_equal(updated_state.verification_result, "accepted", "Final verification result mismatch")


def test_run_task2_syncs_actual_append_workflow_id():
    exp_dir, exp_log_path, iteration_path = prepare_iteration_dir()
    try:
        agent = WorkflowGenerator(
            model=DummyModel(),
            use_knowledge_agent=False,
            use_research_agent=False,
            exp_log_path=exp_log_path,
            use_new_format=True,
        )
        agent._workstation_loader = StubLoader()
        agent._invoke_with_retry_direct = lambda messages, max_retries=5: (
            "### 实验方案\n第1步 物料站：\n- 操作：物料拿取\n"
        )

        state = WorkflowGeneratorTestState(
            final_goal="优化普鲁士蓝的电化学性能",
            goal_in_this_iteration="goal in this iteration",
            knowledge="knowledge",
            related_workflows_txt="related workflows",
            txt_format_reference="reference format",
            iteration_id=0,
            workflow_id=1,
            exp_log_path=exp_log_path,
        )

        result_state = agent.run_task2(
            state=state,
            source_workflow_txt="workflow-1",
            verification_suggestion="fix workflow-1",
        )

        with open(iteration_path, "r", encoding="utf-8") as f:
            iteration_data = json.load(f)

        appended_workflow = iteration_data["workflows"][-1]

        assert_equal(len(iteration_data["workflows"]), 3, "Task 2 should append a new workflow record")
        assert_equal(result_state.workflow_id, 2, "Task 2 should sync state.workflow_id to the append index")
        assert_equal(appended_workflow["workflow_id"], 2, "Appended workflow_id mismatch")
        assert_equal(
            appended_workflow["workflow_txt"],
            result_state.workflow_txt,
            "Appended workflow_txt should match task2 output",
        )
    finally:
        shutil.rmtree(exp_dir, ignore_errors=True)


def main():
    print("=" * 60)
    print("反馈数据通路回归测试")
    print("=" * 60)

    test_main_workflow_uses_task2_returned_workflow_id()
    print("[PASS] 主 workflow 会沿用 task2 返回的 workflow_id")

    test_run_task2_syncs_actual_append_workflow_id()
    print("[PASS] task2 会把 state.workflow_id 同步为实际追加索引")

    print("=" * 60)
    print("所有反馈数据通路回归测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()

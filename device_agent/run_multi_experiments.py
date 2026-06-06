"""
多次运行实验脚本
=================

功能说明:
连续运行多次主工作流，生成多份JSON实验方案。

版本: v0.1
创建日期: 2026-03-18
"""

import os
import json
import logging
from datetime import datetime

from utils.llm_factory import LLMFactory
from utils.paths import exp_logs_dir
from workflow import MainWorkflow

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def run_single_experiment(experiment_num: int, final_goal: str) -> dict:
    """
    运行单次实验

    Args:
        experiment_num: 实验序号
        final_goal: 实验目标

    Returns:
        实验结果摘要
    """
    logger.info("=" * 80)
    logger.info(f"开始第 {experiment_num} 次实验")
    logger.info("=" * 80)

    # 创建LLM实例
    model = LLMFactory.create()

    # 创建主工作流
    workflow = MainWorkflow(
        model=model,
        use_temp_data_flow=True,
        use_new_format=True  # 使用新版工作站描述格式
    )

    # 运行主工作流
    state = workflow.run(final_goal)

    # 返回结果摘要
    result = {
        "experiment_num": experiment_num,
        "exp_id": state.exp_id,
        "iteration_id": state.iteration_id,
        "workflow_id": state.workflow_id,
        "status": state.status,
        "verification_result": state.verification_result,
        "workflow_json": state.workflow_json,
        "workflow_txt": state.workflow_txt,
        "goal_in_this_iteration": state.goal_in_this_iteration
    }

    logger.info(f"第 {experiment_num} 次实验完成，状态: {state.status}")
    return result


def save_results(results: list, output_dir: str):
    """
    保存所有实验结果

    Args:
        results: 实验结果列表
        output_dir: 输出目录
    """
    os.makedirs(output_dir, exist_ok=True)

    # 保存汇总
    summary_path = os.path.join(output_dir, "all_experiments_summary.json")
    summary = []
    for r in results:
        summary.append({
            "experiment_num": r["experiment_num"],
            "exp_id": r["exp_id"],
            "status": r["status"],
            "verification_result": r["verification_result"],
            "goal_in_this_iteration": r["goal_in_this_iteration"]
        })

    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    logger.info(f"汇总结果已保存到: {summary_path}")

    # 保存每次实验的详细结果
    for r in results:
        exp_dir = os.path.join(output_dir, f"experiment_{r['experiment_num']}")
        os.makedirs(exp_dir, exist_ok=True)

        # 保存JSON方案
        if r["workflow_json"]:
            json_path = os.path.join(exp_dir, "workflow.json")
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(r["workflow_json"], f, ensure_ascii=False, indent=2)
            logger.info(f"第 {r['experiment_num']} 次实验JSON已保存到: {json_path}")

        # 保存TXT方案
        if r["workflow_txt"]:
            txt_path = os.path.join(exp_dir, "workflow.txt")
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write(r["workflow_txt"])
            logger.info(f"第 {r['experiment_num']} 次实验TXT已保存到: {txt_path}")


def main():
    """
    主函数：运行多次实验
    """
    # 实验配置
    num_experiments = 3
    final_goal = "优化普鲁士蓝的电化学性能"

    # 输出目录
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.join(str(exp_logs_dir()), f"multi_experiments_{timestamp}")

    logger.info(f"将运行 {num_experiments} 次实验")
    logger.info(f"实验目标: {final_goal}")
    logger.info(f"输出目录: {output_dir}")

    # 运行实验
    results = []
    for i in range(1, num_experiments + 1):
        try:
            result = run_single_experiment(i, final_goal)
            results.append(result)
        except Exception as e:
            logger.error(f"第 {i} 次实验失败: {str(e)}")
            results.append({
                "experiment_num": i,
                "status": "failed",
                "error": str(e)
            })

    # 保存结果
    save_results(results, output_dir)

    # 打印最终汇总
    logger.info("=" * 80)
    logger.info("所有实验完成")
    logger.info("=" * 80)
    for r in results:
        status = r.get("status", "unknown")
        verification = r.get("verification_result", "N/A")
        logger.info(f"实验 {r['experiment_num']}: 状态={status}, 审核结果={verification}")

    return results


if __name__ == "__main__":
    main()

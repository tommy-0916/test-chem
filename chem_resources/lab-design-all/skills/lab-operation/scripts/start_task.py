#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Blocked experiment start entrypoint.

The real laboratory task-start API is intentionally disabled in this
repository. This script only returns a structured blocked result.
"""

import argparse
import json

from dispatch_guard import blocked_dispatch_result
from logger_config import setup_logger, log_function_call


logger = setup_logger("start_task")


def start_task(task_id: str, app_label: str) -> dict:
    script_name = "start_task.py"
    log_function_call(logger, script_name, "start_task", task_id=task_id, app_label=app_label)

    if not task_id:
        logger.warning("任务id为空")
        return {
            "success": False,
            "message": "任务id不能为空",
        }

    logger.warning(
        "实验任务启动已被安全阻断: task_id=%s, app_label=%s",
        task_id,
        app_label,
    )
    return blocked_dispatch_result("start_task", app_label=app_label)


def main() -> None:
    parser = argparse.ArgumentParser(description="启动实验任务（已阻断）")
    parser.add_argument("--task-id", type=str, required=True, help="实验任务id")
    parser.add_argument("--app-label", type=str, required=True, help="目标实验室标签")
    args = parser.parse_args()

    result = start_task(args.task_id, args.app_label)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

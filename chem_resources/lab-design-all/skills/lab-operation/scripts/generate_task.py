#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Blocked experiment dispatch entrypoint.

The real laboratory task-generation API is intentionally disabled in this
repository. This script only returns a structured blocked result.
"""

import argparse
import json

from dispatch_guard import blocked_dispatch_result
from logger_config import setup_logger, log_function_call


logger = setup_logger("generate_task")


def generate_task(template_id: str, template_source_label: str, app_label: str) -> dict:
    script_name = "generate_task.py"
    log_function_call(
        logger,
        script_name,
        "generate_task",
        template_id=template_id,
        template_source_label=template_source_label,
        app_label=app_label,
    )
    logger.warning(
        "实验任务下发已被安全阻断: template_id=%s, template_source_label=%s, app_label=%s",
        template_id,
        template_source_label,
        app_label,
    )
    return blocked_dispatch_result("generate_task", app_label=app_label)


def main() -> None:
    parser = argparse.ArgumentParser(description="下发实验任务（已阻断）")
    parser.add_argument("--template-id", type=str, required=True, help="实验方案模板id")
    parser.add_argument(
        "--template-source-label",
        type=str,
        required=True,
        help="模板来源实验室标签",
    )
    parser.add_argument("--app-label", type=str, required=True, help="目标实验室标签")
    args = parser.parse_args()

    result = generate_task(args.template_id, args.template_source_label, args.app_label)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

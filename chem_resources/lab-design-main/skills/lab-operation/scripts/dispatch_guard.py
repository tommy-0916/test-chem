"""Safety guard for real laboratory dispatch operations.

This repository is allowed to plan and format experiments, but it must not
submit or start real laboratory tasks unless this guard is intentionally
removed by a human operator.
"""

from __future__ import annotations

from typing import Dict


def blocked_dispatch_result(action: str, *, app_label: str = "") -> Dict[str, object]:
    return {
        "success": False,
        "blocked": True,
        "dispatch_blocked": True,
        "action": action,
        "app_label": app_label,
        "message": (
            "实验下发链路已被本仓库安全阻断；未访问云网关，也未创建或启动真实实验任务。"
        ),
    }

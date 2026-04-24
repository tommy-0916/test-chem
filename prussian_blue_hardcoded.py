from copy import deepcopy
from types import SimpleNamespace


# Canonical hardcoded process description for the Prussian blue synthesis flow.
# Operation names are normalized to the shorter workstation-facing form.
PRUSSIAN_BLUE_FLOW = {
    "steps": [
        {
            "step_number": 1,
            "workstation": "物料站",
            "operation": "物料拿取",
            "parameters": {
                "进样瓶编号": [1, 2],
            },
        },
        {
            "step_number": 2,
            "workstation": "液体进样站",
            "operation": "加液",
            "parameters": {
                "进样瓶编号": [1, 2],
                "原液编号": [1, 2, 3, 4, 5],
                "原液量": [
                    [5.0, 5.0, 0.5, 0.5, 0.0],
                    [5.0, 5.0, 0.5, 0.5, 0.0],
                ],
            },
        },
        {
            "step_number": 3,
            "workstation": "磁力搅拌工作站",
            "operation": "磁力搅拌",
            "parameters": {
                "进样瓶编号": [1, 2],
                "搅拌时间（分钟）": 60,
            },
        },
        {
            "step_number": 4,
            "workstation": "置物工作站",
            "operation": "静置老化",
            "parameters": {
                "进样瓶编号": [1, 2],
                "静置时间（分钟）": 720,
            },
        },
        {
            "step_number": 5,
            "workstation": "纯化工作站",
            "operation": "离心",
            "parameters": {
                "进样瓶编号": [1, 2],
                "方案": "留固",
                "时间（分钟）": 5,
                "速度": 6000,
                "清洗次数": 1,
                "清洗液": "水",
                "自动设置加液状态": True,
                "手动设置加液量（毫升）": 15,
                "是否保留清洗液": False,
            },
        },
        {
            "step_number": 6,
            "workstation": "液体进样站",
            "operation": "加液",
            "parameters": {
                "进样瓶编号": [1, 2],
                "原液编号": [5],
                "原液量": [
                    [10.0],
                    [10.0],
                ],
            },
        },
        {
            "step_number": 7,
            "workstation": "超声清洗",
            "operation": "超声清洗",
            "parameters": {
                "进样瓶编号": [1, 2],
                "清洗时间（秒）": 600,
            },
        },
        {
            "step_number": 8,
            "workstation": "纯化工作站",
            "operation": "离心",
            "parameters": {
                "进样瓶编号": [1, 2],
                "方案": "留固",
                "时间（分钟）": 5,
                "速度": 6000,
                "清洗次数": 1,
                "清洗液": "水",
                "自动设置加液状态": True,
                "手动设置加液量（毫升）": 15,
                "是否保留清洗液": False,
            },
        },
        {
            "step_number": 9,
            "workstation": "液体进样站",
            "operation": "加液",
            "parameters": {
                "进样瓶编号": [1, 2],
                "原液编号": [5],
                "原液量": [
                    [10.0],
                    [10.0],
                ],
            },
        },
        {
            "step_number": 10,
            "workstation": "纯化工作站",
            "operation": "离心",
            "parameters": {
                "进样瓶编号": [1, 2],
                "方案": "留固",
                "时间（分钟）": 5,
                "速度": 6000,
                "清洗次数": 1,
                "清洗液": "水",
                "自动设置加液状态": True,
                "手动设置加液量（毫升）": 15,
                "是否保留清洗液": False,
            },
        },
        {
            "step_number": 11,
            "workstation": "液体进样站",
            "operation": "加液",
            "parameters": {
                "进样瓶编号": [1, 2],
                "原液编号": [6],
                "原液量": [
                    [10.0],
                    [10.0],
                ],
            },
        },
        {
            "step_number": 12,
            "workstation": "纯化工作站",
            "operation": "离心",
            "parameters": {
                "进样瓶编号": [1, 2],
                "方案": "留固",
                "时间（分钟）": 5,
                "速度": 6000,
                "清洗次数": 1,
                "清洗液": "水",
                "自动设置加液状态": True,
                "手动设置加液量（毫升）": 15,
                "是否保留清洗液": False,
            },
        },
        {
            "step_number": 13,
            "workstation": "烘干机",
            "operation": "静置烘干",
            "parameters": {
                "进样瓶编号": [1, 2],
                "恒温温度（℃）": 60,
                "烘干时间（分钟）": 720,
            },
        },
        {
            "step_number": 14,
            "workstation": "置物工作站",
            "operation": "物料放置",
            "parameters": {
                "进样瓶编号": [1, 2],
            },
        },
    ],
    "unknown_steps": None,
}


def get_prussian_blue_flow():
    """Return a deep-copied dict payload."""
    return deepcopy(PRUSSIAN_BLUE_FLOW)


def get_prussian_blue_flow_container_style():
    """
    Return a container-style payload closer to the generic dataTransform input.
    This keeps existing parameters and adds 容器类型/容器编号.
    """
    flow = deepcopy(PRUSSIAN_BLUE_FLOW)
    for step in flow["steps"]:
        parameters = step["parameters"]
        if "进样瓶编号" in parameters:
            parameters["容器类型"] = "进样瓶"
            parameters["容器编号"] = parameters["进样瓶编号"]
    return flow


def build_prussian_blue_ori_steps():
    """
    Build the SimpleNamespace structure expected by server_workstation.gen_all().
    """
    flow = get_prussian_blue_flow()
    return SimpleNamespace(
        steps=[SimpleNamespace(**step) for step in flow["steps"]]
    )


if __name__ == "__main__":
    flow = get_prussian_blue_flow()
    print(flow)

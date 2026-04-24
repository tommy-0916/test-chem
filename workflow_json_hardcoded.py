import json
from copy import deepcopy
from types import SimpleNamespace


# MOD: 这是从 workflow_json.json 直接固化出来的硬编码流程，
# 后续即使不读取外部 JSON 文件，也可以直接使用这份数据。
_WORKFLOW_JSON_TEXT = r"""
{
    "steps": [
        {
            "step_number": 1,
            "workstation": "物料站",
            "operation": "物料拿取",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    1,
                    2
                ]
            }
        },
        {
            "step_number": 2,
            "workstation": "固体进样工作站",
            "operation": "固体加样",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    1
                ],
                "固体试剂编号": [
                    1
                ],
                "固体用量": [
                    [
                        297.0
                    ]
                ],
                "分散方式": "直接加入",
                "reagent_declaration": "用到1号（氯化锰四水合物, MnCl2·4H2O）固体试剂"
            }
        },
        {
            "step_number": 3,
            "workstation": "液体进样站",
            "operation": "加液",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    1
                ],
                "原液编号": [
                    1
                ],
                "原液量": [
                    [
                        30.0
                    ]
                ],
                "reagent_declaration": "用到1号（去离子水）液体药品"
            }
        },
        {
            "step_number": 4,
            "workstation": "磁力搅拌工作站",
            "operation": "磁力搅拌",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    1
                ],
                "时间（分钟）": 10,
                "搅拌速度": 500
            }
        },
        {
            "step_number": 5,
            "workstation": "固体进样工作站",
            "operation": "固体加样",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    2
                ],
                "固体试剂编号": [
                    2
                ],
                "固体用量": [
                    [
                        634.0
                    ]
                ],
                "分散方式": "直接加入",
                "reagent_declaration": "用到2号（铁氰化钾, K4[Fe(CN)6]·3H2O）固体试剂"
            }
        },
        {
            "step_number": 6,
            "workstation": "液体进样站",
            "operation": "加液",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    2
                ],
                "原液编号": [
                    1
                ],
                "原液量": [
                    [
                        30.0
                    ]
                ],
                "reagent_declaration": "用到1号（去离子水）液体药品"
            }
        },
        {
            "step_number": 7,
            "workstation": "磁力搅拌工作站",
            "operation": "磁力搅拌",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    2
                ],
                "时间（分钟）": 10,
                "搅拌速度": 500
            }
        },
        {
            "step_number": 8,
            "workstation": "液体进样站",
            "operation": "加液",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    2
                ],
                "原液编号": [
                    2
                ],
                "原液量": [
                    [
                        30.0
                    ]
                ],
                "reagent_declaration": "用到2号（氯化锰溶液）液体药品"
            }
        },
        {
            "step_number": 9,
            "workstation": "磁力搅拌工作站",
            "operation": "磁力搅拌",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    2
                ],
                "时间（分钟）": 360,
                "搅拌速度": 500
            }
        },
        {
            "step_number": 10,
            "workstation": "纯化工作站",
            "operation": "最新纯化离心流程",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    2
                ],
                "离心速度": 3000,
                "离心时间": 10,
                "方案": "保留清洗液",
                "是否保留清洗液": true,
                "清洗次数": 1,
                "清洗液": "去离子水",
                "自动设置加液状态": false,
                "手动设置加液量（毫升）": 0
            }
        },
        {
            "step_number": 11,
            "workstation": "液体进样站",
            "operation": "加液",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    2
                ],
                "原液编号": [
                    1
                ],
                "原液量": [
                    [
                        30.0
                    ]
                ],
                "reagent_declaration": "用到1号（去离子水）液体药品"
            }
        },
        {
            "step_number": 12,
            "workstation": "磁力搅拌工作站",
            "operation": "磁力搅拌",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    2
                ],
                "时间（分钟）": 10,
                "搅拌速度": 500
            }
        },
        {
            "step_number": 13,
            "workstation": "纯化工作站",
            "operation": "最新纯化离心流程",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    2
                ],
                "离心速度": 3000,
                "离心时间": 10,
                "方案": "不保留清洗液",
                "是否保留清洗液": false,
                "清洗次数": 1,
                "清洗液": "去离子水",
                "自动设置加液状态": false,
                "手动设置加液量（毫升）": 0
            }
        },
        {
            "step_number": 14,
            "workstation": "烘干机",
            "operation": "参数设置",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    2
                ],
                "恒温温度": 80,
                "烘干时间（分钟）": 720,
                "容器状态": "无盖"
            }
        },
        {
            "step_number": 15,
            "workstation": "置物工作站",
            "operation": "进样瓶归位",
            "id": 1562664140571648,
            "parameters": {
                "进样瓶编号": [
                    2
                ],
                "存储位置": "常温区",
                "归位方式": "顺序归位",
                "标记状态": "已完成"
            }
        }
    ],
    "unknown_steps": null
}
"""


WORKFLOW_JSON_HARDCODED = json.loads(_WORKFLOW_JSON_TEXT)


def get_workflow_json():
    return deepcopy(WORKFLOW_JSON_HARDCODED)


def build_ori_steps():
    workflow_json = get_workflow_json()
    return SimpleNamespace(
        steps=[SimpleNamespace(**step) for step in workflow_json["steps"]]
    )


def run_via_server_workstation():
    from server_workstation import gen_all_from_json_data

    return gen_all_from_json_data(get_workflow_json())


if __name__ == "__main__":
    result = run_via_server_workstation()
    print(json.dumps(result, ensure_ascii=False, indent=2))

import requests
import json
from datetime import datetime
from collections import defaultdict
import re
from typing import Any
import os

control_station = ['开始', '加锁', '结束', '解锁']
global station_dict, station_in_303
station_in_303 = ['物料站', '液体进样站', '磁力搅拌工作站', '烘干机', '纯化工作站', '超声清洗', '双工位电化学工作站','固体进样工作站','置物工作站']
station_dict = {
    station_in_303[1]: 1418510906065920,
    station_in_303[2]: 1486930819875840,
    station_in_303[3]: 1213184425559040,
    station_in_303[4]: 1327807622448128,
    station_in_303[5]: 1213123578463232,
    station_in_303[6]: 1390252863751168,
    station_in_303[7]: 1396669754016768,
    station_in_303[0]: 1427568512205824,
    station_in_303[8]: 1421325849887744,
}
def get_details(id):
    get_details_link = f"http://10.88.0.21:8018/worker/workstation/definition/detail?id={id}"

    # MOD: 为详情接口增加超时，避免远端服务异常时长时间阻塞整个离线回退链路。
    response = requests.get(get_details_link, timeout=3)
    status = response.json()['data']
    return status

def get_functions(id, name, code):
    get_functions_link = f"http://10.88.0.21:8018/worker/workstation/definition/pipeline?id={id}&name={name}&code={code}"
    # MOD: 为 pipeline 接口增加超时，避免远端服务异常时长时间阻塞整个离线回退链路。
    response = requests.get(get_functions_link, timeout=3)
    status = response.json()['data']
    return status

def get_all():
    # MOD: 优先走线上定义服务；若本地调试或服务暂时不可用，则回退到缓存文件。
    cache_file = "./workstation_type.json"
    if not os.getenv("LOCAL_TEST", "False") == "True":
        try:
            get_name = "http://10.88.0.21:8018/worker/workstation/definition/list?page=1&size=200&release=1"
            get_name = "http://10.88.0.21:8018/worker/workstation/definition/list?page=1&size=200"
            response = requests.get(get_name, timeout=3)
            try:
                data = response.json()
            except Exception:
                data = response.text
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            status = response.json()['data']
            return status
        except Exception as exc:
            print(f"[MOD] get_all 线上获取失败，尝试读取本地缓存: {exc}")
            if os.path.exists(cache_file):
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("data", [])
            return []
    
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("data", [])
    return []


def parse_ser_to_json(ser_str):
    # 去掉 SER(...) 的包裹
    ser_str = ser_str.strip()
    ser_str = re.sub(r'^SER\s*\(\s*|\s*\)\s*$', '', ser_str, flags=re.DOTALL)

    # 匹配形如 xxx.key("...").action("...").note("...") 的结构
    pattern = re.compile(
        r'(?P<type>\w+)\.key\("(?P<key>.*?)"\)\.action\("(?P<action>.*?)"\)\.note\("(?P<note>.*?)"\)',
        re.DOTALL
    )

    # 用正则全部匹配出来
    results = []
    for match in pattern.finditer(ser_str):
        results.append(match.groupdict())

    return results


def search_item(station_name, status):
    print(station_name)
    return [item for item in status if item['name'] == station_name]

def get_303_stations():
    global station_dict
    status = get_all()
    for i in range(len(station_in_303)):
        temp_dict = {}
        temp_dict['id'] = station_dict[station_in_303[i]]
        station_dict[station_in_303[i]] = temp_dict

    # 将status列表转换为以id为键的字典，以便快速查找
    status_dict = {item['id']: item for item in status}

    # 使用字典推导式更新station_dict
    station_dict = {
        station: status_dict.get(station_info['id'], station_info)
        for station, station_info in station_dict.items()
    }
    return station_dict
station_dict = get_303_stations()

# MOD: 当线上工作站定义服务不可达时，使用这份静态元数据继续做离线格式转换。
OFFLINE_WORKSTATION_METADATA = {
    "物料站": {
        "id": 1427568512205824,
        "code": "starting_station",
        "name": "物料站",
        "version": "303物料站",
    },
    "液体进样站": {
        "id": 1418510906065920,
        "code": "liquid_dispensing",
        "name": "液体进样站",
        "version": "移液平台2.0-1ml",
    },
    "磁力搅拌工作站": {
        "id": 1486930819875840,
        "code": "magnetic_stirring",
        "name": "磁力搅拌工作站",
        "version": "303-25通道磁力搅拌控制器",
    },
    "烘干机": {
        "id": 1213184425559040,
        "code": "dryer",
        "name": "烘干机",
        "version": "gxq_1.0",
    },
    "纯化工作站": {
        "id": 1327807622448128,
        "code": "pure",
        "name": "纯化工作站",
        "version": "1.0",
    },
    "超声清洗": {
        "id": 1213123578463232,
        "code": "ultrasonic_cleaning",
        "name": "超声清洗",
        "version": "1.0",
    },
    "双工位电化学工作站": {
        "id": 1390252863751168,
        "code": "dual_electrochemical",
        "name": "双工位电化学工作站",
        "version": "1.0",
    },
    "固体进样工作站": {
        "id": 1396669754016768,
        "code": "solid_dispensing",
        "name": "固体进样工作站",
        "version": "1.0",
    },
    "置物工作站": {
        "id": 1421325849887744,
        "code": "elec_chem_storage_workstation",
        "name": "置物工作站",
        "version": "1.0",
    },
}

def build_workstation_name_to_id():
    # MOD: 统一从 station_dict 动态生成名称到 ID 的映射，避免主流程遗漏新工作站。
    output = {}
    for station_name, station_info in station_dict.items():
        if isinstance(station_info, dict) and 'id' in station_info:
            output[station_name] = station_info['id']
        elif isinstance(station_info, int):
            output[station_name] = station_info
    for station_name, station_info in OFFLINE_WORKSTATION_METADATA.items():
        output.setdefault(station_name, station_info['id'])
    return output

def get_workstation_meta(station_name):
    # MOD: 优先使用线上/缓存元数据；若拿不到 code/version，则回退到静态元数据。
    station_info = station_dict.get(station_name, {})
    if isinstance(station_info, dict) and station_info.get('code') and station_info.get('name'):
        return station_info
    return OFFLINE_WORKSTATION_METADATA.get(
        station_name,
        {
            "id": None,
            "code": station_name,
            "name": station_name,
            "version": "",
        }
    )

def containers(parameters):
    return_list = []
    for index in parameters:
        selected_container = {}
        selected_container['logicNo'] = index
        selected_container['selected'] = True
        return_list.append(selected_container)
    return return_list

def check_input(action):
    input_params = []
    input_flag = False
    for param_id, param in enumerate(action['params']):
        if param['ioType'] == 'INPUT':
            input_params.append(param_id)
            input_flag = True
    return input_flag, input_params

def convert_all_numbers_to_str(obj: Any) -> Any:
    """
    递归将 dict / list / tuple 中的所有 int 和 float 类型的值转换为字符串。
    其他类型保持不变。
    """
    if isinstance(obj, dict):
        return {k: convert_all_numbers_to_str(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_all_numbers_to_str(item) for item in obj]
    elif isinstance(obj, (int, float)):
        return str(obj)
    else:
        return obj
    
def stringify_param_values(obj):
    """
    递归处理嵌套结构，若找到 {"paramCode": ..., "value": dict}，则将 dict 的值转为字符串并 json.dumps。
    """
    if isinstance(obj, list):
        return [stringify_param_values(item) for item in obj]
    
    if isinstance(obj, dict):
        # 如果是目标结构：包含 paramCode 且 value 是 dict
        if "paramCode" in obj and isinstance(obj.get("value"), dict):
            # 所有 value 内部字段值转成字符串
            value_dict = {k: str(v) for k, v in obj["value"].items()}
            obj["value"] = json.dumps(value_dict, ensure_ascii=False)
            return obj
        else:
            return {k: stringify_param_values(v) for k, v in obj.items()}
    
    return obj


def fuzzy_search(dictionary, target_key):
    # MOD: workflow_json 里的参数名可能和工作站原始定义略有差异，这里增加一层归一化匹配。
    def normalize_param_name(text):
        text = str(text).strip()
        text = text.replace("（", "(").replace("）", ")")
        text = re.sub(r"\(.*?\)", "", text)
        text = re.sub(r"\s+", "", text)
        alias_map = {
            "进样瓶编号": "容器编号",
            "进样瓶": "容器",
            "搅拌时间": "时间",
            "离心时间": "时间",
            "清洗时间": "时间",
            "搅拌速度": "速度",
            "离心速度": "速度",
            "恒温温度": "温度",
        }
        for source, target in alias_map.items():
            text = text.replace(source, target)
        return text

    if dictionary is None:
        return None
    if target_key in dictionary:
        return dictionary[target_key]

    normalized_target = normalize_param_name(target_key)
    for key in dictionary:
        if target_key in key or key in target_key:
            return dictionary[key]
        normalized_key = normalize_param_name(key)
        if normalized_target and (normalized_target in normalized_key or normalized_key in normalized_target):
            return dictionary[key]
    return None
def get_value_by_label(json_str, target_label):
    try:
        # 解析 JSON 字符串
        data = json.loads(json_str)
        # 遍历列表查找匹配项
        for item in data:
            if item["label"] == target_label:
                return item["value"]
        return None  # 未找到匹配项
    except json.JSONDecodeError:
        print("JSON 解析错误")
        return None


def explain_params(params, list_num):
    outputs = []
    for param in params:
        if param['ioType'] != 'INPUT':
            continue
        output = {}
        output['paramCode'] = param['paramCode']
        # print(param['dataType'])
        if param['dataType'] == 'array':
            output['value'] = []
            childparams = param['childParams']
            if param['paramName']!='加样方案' and param['paramName']!='开盖的瓶号':
                list_num = 1
            for _ in range(list_num):
                single_output = {}
                for childparam in childparams:
                    single_output[childparam['paramCode']] = childparam['defaultValue']
                output['value'].append(single_output)
        elif param['dataType'] == 'int' or param['dataType'] == 'string' or param['dataType'] == 'float':
            output['value'] = param['defaultValue']
        elif param['dataType'] == 'object':
            childparams = param['childParams']
            single_output = {}
            for childparam in childparams:
                single_output[childparam['paramCode']] = childparam['defaultValue']
            output['value'] = single_output
        outputs.append(output)
    return outputs

def get_action_param(ex_pipelines, gen_step, ex_details):
    # 找到对应pipeline
    for ex_pipeline in ex_pipelines['pipelines']:
        if ex_pipeline['name'] == gen_step.operation:
            break
    # 分解pipeline
    ex_pipeline_details = parse_ser_to_json(ex_pipeline['pipeline'])
    # 收集所有func中的keys和actions
    ex_pipeline_keys_action = {}
    for ex_pipeline_detail in ex_pipeline_details:
        if ex_pipeline_detail['type']=='func':
            ex_pipeline_keys_action[ex_pipeline_detail['key']] = ex_pipeline_detail['action']
    # 根据keys回到action中找到每个keys的对应动作
    all_actionParams = []
    for ex_pipeline_key in ex_pipeline_keys_action:
        ex_pipeline_actions = []
        actionParams = []
        for ex_action in ex_details['actions']:
            if ex_action['code'] == ex_pipeline_keys_action[ex_pipeline_key]:
                flag, param_id = check_input(ex_action)
                if not flag:
                    continue
                actionParam = {}
                actionParam["actionCode"] = ex_action['code']
                actionParam["key"] = ex_pipeline_key
                actionParam["params"] = explain_params(ex_action['params'], len(gen_step.parameters['进样瓶编号']))
                ex_pipeline_actions.append(ex_action)
                actionParams.append(actionParam)
                break
        if actionParams != []:
            all_actionParams+=actionParams
    return all_actionParams, ex_pipeline, ex_pipeline_keys_action

def convert_all_numbers_to_str(obj: Any) -> Any:
    """
    递归将 dict / list / tuple 中的所有 int 和 float 类型的值转换为字符串。
    其他类型保持不变。
    """
    if isinstance(obj, dict):
        return {k: convert_all_numbers_to_str(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_all_numbers_to_str(item) for item in obj]
    elif isinstance(obj, (int, float)):
        return str(obj)
    else:
        return obj

def stringify_param_values(obj):
    """
    递归处理嵌套结构，若找到 {"paramCode": ..., "value": ...}，则将 value 无论什么类型都转为字符串。
    """
    if isinstance(obj, list):
        return [stringify_param_values(item) for item in obj]
    
    if isinstance(obj, dict):
        # 如果是目标结构：包含 paramCode 和 value
        if "paramCode" in obj and "value" in obj:
            value = obj["value"]
            # 仅对非字符串类型的 value 进行序列化处理
            if not isinstance(value, str):
                try:
                    # 非字符串类型用 json.dumps 转为字符串（保留结构）
                    obj["value"] = json.dumps(value, ensure_ascii=False)
                except TypeError:
                    # 极端情况：无法 JSON 序列化的类型，直接用 str() 转换
                    obj["value"] = str(value)
            # 字符串类型不处理，保持原样
            return obj
        else:
            return {k: stringify_param_values(v) for k, v in obj.items()}
    
    return obj

from server_utils import *
import requests
import json
from types import SimpleNamespace

try:
    # MOD: UnifiedAgent 目前不是主链路必需依赖；本地没有该模块时不应阻塞整个流程。
    from workstation.unified_agent import UnifiedAgent
except ModuleNotFoundError:
    UnifiedAgent = None

# 0:物料站
# ex_details = get_details(station_dict[steps.steps[0].workstation]['id'])
def starting_station_data(gen_step):
    print("gen_step----------", gen_step)
    ex_details = get_details(station_dict[gen_step.workstation]['id'])
    ex_pipelines = get_functions(station_dict[gen_step.workstation]['id'],station_dict[gen_step.workstation]['name'],station_dict[gen_step.workstation]['code'])
    for ex_pipeline in ex_pipelines['pipelines']:
        if ex_pipeline['name'] == gen_step.operation:
            break
    return {
            "stepType": "workstation",
            "workstationType": ex_details['code'],
            "workstationTypeName": ex_details['name'],
            "definitionVersion": ex_details['version'],
            "pipeline": ex_pipeline['code'],
            "actionParams": [

            ],
            "container": {
                "allocation": [
                    {
                        "containerCount": len(gen_step.parameters['进样瓶编号']),
                        "containerTypeCode": "bottle_storage",
                        "containerTypeName": "进样瓶",
                        "allocateMode": "new",
                        "selectedContainers": containers(gen_step.parameters['进样瓶编号'])
                    },
                    {
                        "containerCount": 0,
                        "containerTypeCode": "96_hole_plate",
                        "containerTypeName": "96孔板",
                        "allocateMode": "new",
                        "selectedContainers": []
                    },
                    {
                        "containerCount": 0,
                        "containerTypeCode": "50ml_heat_resisting_tube",
                        "containerTypeName": "50ml耐热瓶",
                        "allocateMode": "extend",
                        "selectedContainers": []
                    }
                ]
            }
        }
# starting_station_data(steps.steps[0])


# 1:液体进样
def liquid_add_data(gen_step):

    ex_details = get_details(station_dict[gen_step.workstation]['id'])        
    ex_pipelines = get_functions(station_dict[gen_step.workstation]['id'],station_dict[gen_step.workstation]['name'],station_dict[gen_step.workstation]['code'])

    actionParams, ex_pipeline, ex_pipeline_keys_action = get_action_param(ex_pipelines=ex_pipelines, ex_details=ex_details, gen_step=gen_step)
    # print("action--------------liquid_add_data", actionParams,"\n", ex_pipeline, "\n", ex_pipeline_keys_action)
    for key_id, ex_pipeline_key in enumerate(ex_pipeline_keys_action):
        ex_action = next( 
            (a for a in ex_details['actions'] if a['code'] == ex_pipeline_keys_action[ex_pipeline_key]),
            None
        )
        flag, param_id = check_input(ex_action)
        if not flag:
            continue
        ex_param_map = {p['paramCode']: p for p in ex_action['params']}
        for actionParam in actionParams:
            if actionParam['key'] == ex_pipeline_key:
                break
        # print("ex_param_map---------", gen_step.parameters, "\n", actionParam)
        
        # 加液操作解析
        if ex_pipeline_key == "add_liquid":
            # 遍历进样瓶编号并设置 tube_num
            for tube_index, tube in enumerate(gen_step.parameters['进样瓶编号']):
                for param in actionParam['params']:
                    if param['paramCode'] in ex_param_map:
                        # 安全地确保 param['value'] 是 list 且长度够
                        if isinstance(param.get('value'), list) and tube_index < len(param['value']):
                            param['value'][tube_index]['tube_num'] = tube
                            for ori_tube_index, ori_tube in enumerate(gen_step.parameters['原液编号']):
                                param['value'][tube_index]['bottle'+str(ori_tube-1)] = gen_step.parameters['原液量'][tube_index][ori_tube_index]   
                        else:
                            raise IndexError(f"param['value'] 不存在或长度不足，tube_index={tube_index}")
        elif ex_pipeline_key == "open_cap":
            for tube_index, tube in enumerate(gen_step.parameters['进样瓶编号']):
                for param in actionParam['params']:
                    if param['paramCode'] in ex_param_map:
                        # 安全地确保 param['value'] 是 list 且长度够
                        # print("param['value']---------", tube_index, tube, param['value'])
                        if 'value' in param and isinstance(param['value'], list):
                            param['value'][tube_index]['tube_num'] = tube
                            # print("param-------", param)
                        if param['paramCode'] == "save_cap":
                            param['value'] = 0

            
    # 找到cmd中的keys
    return {
            "stepType": "workstation",
            "workstationType": ex_details['code'],
            "workstationTypeName": ex_details['name'],
            "definitionVersion": ex_details['version'],
            "pipeline": ex_pipeline['code'],
            "actionParams": actionParams,
            "container":{                
                "allocation": [
                    {
                        "containerCount": len(gen_step.parameters['进样瓶编号']),
                        "containerTypeCode": "bottle_storage",
                        "containerTypeName": "进样瓶",
                        "allocateMode": "extend",
                        "selectedContainers": containers(gen_step.parameters['进样瓶编号'])
                    },
                    {
                        "containerCount": 0,
                        "containerTypeCode": "96_hole_plate",
                        "containerTypeName": "96孔板",
                        "allocateMode": "extend",
                        "selectedContainers": []
                    },
                    {
                        "containerCount": 0,
                        "containerTypeCode": "bottle",
                        "containerTypeName": "西林瓶",
                        "allocateMode": "extend",
                        "selectedContainers": []
                    },
                    {
                        "containerCount": 0,
                        "containerTypeCode": "50ml_heat_resisting_tube",
                        "containerTypeName": "50ml耐热瓶",
                        "allocateMode": "extend",
                        "selectedContainers": []
                    }
                ]
            }
        }
# liquid_add_data(steps.steps[1])


# 2:磁力搅拌
def magnetic_data(gen_step):
    
    ex_details = get_details(station_dict[gen_step.workstation]['id'])        
    ex_pipelines = get_functions(station_dict[gen_step.workstation]['id'],station_dict[gen_step.workstation]['name'],station_dict[gen_step.workstation]['code'])
    actionParams, ex_pipeline, ex_pipeline_keys_action = get_action_param(ex_pipelines=ex_pipelines, ex_details=ex_details, gen_step=gen_step)
    for key_id, ex_pipeline_key in enumerate(ex_pipeline_keys_action):
        ex_action = next( 
            (a for a in ex_details['actions'] if a['code'] == ex_pipeline_keys_action[ex_pipeline_key]),
            None
        )
        flag, param_id = check_input(ex_action)
        if not flag:
            continue
        ex_param_map = {p['paramCode']: p for p in ex_action['params']}
        for actionParam in actionParams:
            if actionParam['key'] == ex_pipeline_key:
                break
        for param in actionParam['params']:
            if param['paramCode'] in ex_param_map:
                if ex_param_map[param['paramCode']]['constraintValue'] != '' and ex_param_map[param['paramCode']]['constraintType'] == 'ENUM':
                     answer = get_value_by_label(ex_param_map[param['paramCode']]['constraintValue'], fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip()))
                elif ex_param_map[param['paramCode']]['dataType'] == 'array':
                    single_output = {}
                    for childparam in ex_param_map[param['paramCode']]['childParams']:
                        if childparam['constraintValue'] != '' and childparam['constraintType'] == 'ENUM':
                            single_output[childparam['paramCode']] = get_value_by_label(childparam['constraintValue'], fuzzy_search(gen_step.parameters, childparam['paramName'].strip()))
                        else:
                            single_output[childparam['paramCode']] = fuzzy_search(gen_step.parameters, childparam['paramName'].strip())
                    answer = [single_output]
                else:
                    answer = fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip())
                param['value'] = answer if answer else param['value']
            # break
    return {
            "stepType": "workstation",
            "workstationType": ex_details['code'],
            "workstationTypeName": ex_details['name'],
            "definitionVersion": ex_details['version'],
            "pipeline": ex_pipeline['code'],
            "actionParams": actionParams,
            "container":{                
                "allocation": [
                    {
                        "containerCount": len(gen_step.parameters['进样瓶编号']),
                        "containerTypeCode": "bottle_storage",
                        "containerTypeName": "进样瓶",
                        "allocateMode": "extend",
                        "selectedContainers": containers(gen_step.parameters['进样瓶编号'])
                    }
                ]
            }
        }
# magnetic_data(steps.steps[2])


# 3:烘干
def dryer_data(gen_step):
        
    ex_details = get_details(station_dict[gen_step.workstation]['id'])        
    ex_pipelines = get_functions(station_dict[gen_step.workstation]['id'],station_dict[gen_step.workstation]['name'],station_dict[gen_step.workstation]['code'])
    all_actionParams, ex_pipeline, ex_pipeline_keys_action = get_action_param(ex_pipelines=ex_pipelines, ex_details=ex_details, gen_step=gen_step)
    for key_id, ex_pipeline_key in enumerate(ex_pipeline_keys_action):
        ex_action = next( 
            (a for a in ex_details['actions'] if a['code'] == ex_pipeline_keys_action[ex_pipeline_key]),
            None
        )
        flag, param_id = check_input(ex_action)
        if not flag:
            continue
        ex_param_map = {p['paramCode']: p for p in ex_action['params']}
    # ex_pipeline_key, ex_action
    # ex_param_map
        for actionParams in all_actionParams:
            if actionParams['key'] == ex_pipeline_key:
                break
        for param in actionParams['params']:
            if param['paramCode'] in ex_param_map:
                if ex_param_map[param['paramCode']]['constraintValue'] != '' and ex_param_map[param['paramCode']]['constraintType'] == 'ENUM':
                     answer = get_value_by_label(ex_param_map[param['paramCode']]['constraintValue'], fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip()))
                elif ex_param_map[param['paramCode']]['dataType'] == 'array':
                    single_output = {}
                    for childparam in ex_param_map[param['paramCode']]['childParams']:
                        if childparam['constraintValue'] != '' and childparam['constraintType'] == 'ENUM':
                            single_output[childparam['paramCode']] = get_value_by_label(childparam['constraintValue'], fuzzy_search(gen_step.parameters, childparam['paramName'].strip()))
                        else:
                            single_output[childparam['paramCode']] = fuzzy_search(gen_step.parameters, childparam['paramName'].strip())
                    answer = [single_output]
                else:
                    answer = fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip())
                param['value'] = answer if answer else param['value']
    return {
            "stepType": "workstation",
            "workstationType": ex_details['code'],
            "workstationTypeName": ex_details['name'],
            "definitionVersion": ex_details['version'],
            "pipeline": ex_pipeline['code'],
            "actionParams": all_actionParams,
            "container":{                
                "allocation": [
                    {
                        "containerCount": len(gen_step.parameters['进样瓶编号']),
                        "containerTypeCode": "bottle_storage",
                        "containerTypeName": "进样瓶",
                        "allocateMode": "extend",
                        "selectedContainers": containers(gen_step.parameters['进样瓶编号'])
                    },
                    {
                        "containerCount": 0,
                        "containerTypeCode": "50ml_heat_resisting_tube",
                        "containerTypeName": "50ml耐热瓶",
                        "allocateMode": "extend",
                        "selectedContainers": []
                    }
                ]
            }
        }
# dryer_data(steps.steps[5])


# 4:纯化工作站
def pure_data(gen_step):
    
    gen_step.operation = '最新纯化离心流程'
    ex_details = get_details(station_dict[gen_step.workstation]['id'])        
    ex_pipelines = get_functions(station_dict[gen_step.workstation]['id'],station_dict[gen_step.workstation]['name'],station_dict[gen_step.workstation]['code'])
    all_actionParams, ex_pipeline, ex_pipeline_keys_action = get_action_param(ex_pipelines=ex_pipelines, ex_details=ex_details, gen_step=gen_step)
    # print("所有要输入的参数")
    # print(json.dumps(ex_pipeline_action_params, indent=2,ensure_ascii=False))
    #         break
    for key_id, ex_pipeline_key in enumerate(ex_pipeline_keys_action):
        ex_action = next( 
            (a for a in ex_details['actions'] if a['code'] == ex_pipeline_keys_action[ex_pipeline_key]),
            None
        )
        flag, param_id = check_input(ex_action)
        if not flag:
            continue
        ex_param_map = {p['paramCode']: p for p in ex_action['params']}
    # ex_pipeline_key, ex_action
    # ex_param_map
        for actionParams in all_actionParams:
            if actionParams['key'] == ex_pipeline_key:
                break
        for param in actionParams['params']:
            if param['paramCode'] in ex_param_map:
                # if param['paramCode'] == "washSolutions":
                #     param['value'] = [{"washSolutions": 1}]
                if ex_param_map[param['paramCode']]['constraintValue'] != '' and ex_param_map[param['paramCode']]['constraintType'] == 'ENUM':
                     answer = get_value_by_label(ex_param_map[param['paramCode']]['constraintValue'], fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip()))
                elif ex_param_map[param['paramCode']]['dataType'] == 'array':
                    single_output = {}
                    for childparam in ex_param_map[param['paramCode']]['childParams']:
                        if childparam['constraintValue'] != '' and childparam['constraintType'] == 'ENUM':
                            single_output[childparam['paramCode']] = get_value_by_label(childparam['constraintValue'], fuzzy_search(gen_step.parameters, childparam['paramName'].strip()))
                        else:
                            single_output[childparam['paramCode']] = fuzzy_search(gen_step.parameters, childparam['paramName'].strip())
                    answer = [single_output]
                else:
                    answer = fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip())
                param['value'] = answer if answer else param['value']
    data = {
            "stepType": "workstation",
            "workstationType": ex_details['code'],
            "workstationTypeName": ex_details['name'],
            "definitionVersion": ex_details['version'],
            "pipeline": ex_pipeline['code'],
            "actionParams": all_actionParams,
            "container":{                
                "allocation": [
                    {
                        "containerCount": len(gen_step.parameters['进样瓶编号']),
                        "containerTypeCode": "bottle_storage",
                        "containerTypeName": "进样瓶",
                        "allocateMode": "extend",
                        "selectedContainers": containers(gen_step.parameters['进样瓶编号'])
                    }
                ]
            }
        }
    if '留样瓶编号' in gen_step.parameters:
        data["container"]["allocation"].append(
                        {
                            "containerCount": len(gen_step.parameters['留样瓶编号']),
                            "containerTypeCode": "bottle_sample_retention",
                            "containerTypeName": "留样瓶",
                            "allocateMode": "extend",
                            "selectedContainers": containers(gen_step.parameters['留样瓶编号'])
                        }
        )
    else:
        data["container"]["allocation"].append(
                        {
                            "containerCount": 0,
                            "containerTypeCode": "bottle_sample_retention",
                            "containerTypeName": "留样瓶",
                            "allocateMode": "extend",
                            "selectedContainers": containers([0])
                        }
        )
        
    return data
# pure_data(steps.steps[6])


# 5:超声工作站
def ultrasonic_data(gen_step):
    
    ex_details = get_details(station_dict[gen_step.workstation]['id'])        
    ex_pipelines = get_functions(station_dict[gen_step.workstation]['id'],station_dict[gen_step.workstation]['name'],station_dict[gen_step.workstation]['code'])
    all_actionParams, ex_pipeline, ex_pipeline_keys_action = get_action_param(ex_pipelines=ex_pipelines, ex_details=ex_details, gen_step=gen_step)
    # print("所有要输入的参数")
    # print(json.dumps(ex_pipeline_action_params, indent=2,ensure_ascii=False))
    #         break
    for key_id, ex_pipeline_key in enumerate(ex_pipeline_keys_action):
        ex_action = next( 
            (a for a in ex_details['actions'] if a['code'] == ex_pipeline_keys_action[ex_pipeline_key]),
            None
        )
        flag, param_id = check_input(ex_action)
        if not flag:
            continue
        ex_param_map = {p['paramCode']: p for p in ex_action['params']}
    # ex_pipeline_key, ex_action
    # ex_param_map
        for actionParams in all_actionParams:
            if actionParams['key'] == ex_pipeline_key:
                break
        for param in actionParams['params']:
            if param['paramCode'] in ex_param_map:
                if ex_param_map[param['paramCode']]['constraintValue'] != '' and ex_param_map[param['paramCode']]['constraintType'] == 'ENUM':
                     answer = get_value_by_label(ex_param_map[param['paramCode']]['constraintValue'], fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip()))
                elif ex_param_map[param['paramCode']]['dataType'] == 'array':
                    single_output = {}
                    for childparam in ex_param_map[param['paramCode']]['childParams']:
                        if childparam['constraintValue'] != '' and childparam['constraintType'] == 'ENUM':
                            single_output[childparam['paramCode']] = get_value_by_label(childparam['constraintValue'], fuzzy_search(gen_step.parameters, childparam['paramName'].strip()))
                        else:
                            single_output[childparam['paramCode']] = fuzzy_search(gen_step.parameters, childparam['paramName'].strip())
                    answer = [single_output]
                else:
                    answer = fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip())
                param['value'] = answer if answer else param['value']
    data = {
            "stepType": "workstation",
            "workstationType": ex_details['code'],
            "workstationTypeName": ex_details['name'],
            "definitionVersion": ex_details['version'],
            "pipeline": ex_pipeline['code'],
            "actionParams": all_actionParams,
            "container":{                
                "allocation": [
                    {
                        "containerCount": len(gen_step.parameters['进样瓶编号']),
                        "containerTypeCode": "bottle_storage",
                        "containerTypeName": "进样瓶",
                        "allocateMode": "extend",
                        "selectedContainers": containers(gen_step.parameters['进样瓶编号'])
                    },
                    {
                        "containerCount": 0,
                        "containerTypeCode": "50ml_heat_resisting_tube",
                        "containerTypeName": "50ml耐热瓶",
                        "allocateMode": "extend",
                        "selectedContainers": []
                    }
                ]
            }
        }
    return data
# ultrasonic_data(steps.steps[11])


# 7:固体进样工作站
def solid_data(gen_step):
        
    ex_details = get_details(station_dict[gen_step.workstation]['id'])        
    ex_pipelines = get_functions(station_dict[gen_step.workstation]['id'],station_dict[gen_step.workstation]['name'],station_dict[gen_step.workstation]['code'])
    all_actionParams, ex_pipeline, ex_pipeline_keys_action = get_action_param(ex_pipelines=ex_pipelines, ex_details=ex_details, gen_step=gen_step)
    for key_id, ex_pipeline_key in enumerate(ex_pipeline_keys_action):
        ex_action = next( 
            (a for a in ex_details['actions'] if a['code'] == ex_pipeline_keys_action[ex_pipeline_key]),
            None
        )
        flag, param_id = check_input(ex_action)
        if not flag:
            continue
        ex_param_map = {p['paramCode']: p for p in ex_action['params']}
    # ex_pipeline_key, ex_action
    # ex_param_map
        for actionParams in all_actionParams:
            if actionParams['key'] == ex_pipeline_key:
                break
        for param in actionParams['params']:
            if param['paramCode'] in ex_param_map:
                if ex_param_map[param['paramCode']]['constraintValue'] != '' and ex_param_map[param['paramCode']]['constraintType'] == 'ENUM':
                     answer = get_value_by_label(ex_param_map[param['paramCode']]['constraintValue'], fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip()))
                elif ex_param_map[param['paramCode']]['dataType'] == 'array':
                    single_output = {}
                    for childparam in ex_param_map[param['paramCode']]['childParams']:
                        if childparam['constraintValue'] != '' and childparam['constraintType'] == 'ENUM':
                            single_output[childparam['paramCode']] = get_value_by_label(childparam['constraintValue'], fuzzy_search(gen_step.parameters, childparam['paramName'].strip()))
                        else:
                            single_output[childparam['paramCode']] = fuzzy_search(gen_step.parameters, childparam['paramName'].strip())
                    answer = [single_output]
                else:
                    answer = fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip())
                param['value'] = answer if answer else param['value']
    return {
            "stepType": "workstation",
            "workstationType": ex_details['code'],
            "workstationTypeName": ex_details['name'],
            "definitionVersion": ex_details['version'],
            "pipeline": ex_pipeline['code'],
            "actionParams": all_actionParams,
            "container":{                
                "allocation": [
                    {
                        "containerCount": len(gen_step.parameters['进样瓶编号']),
                        "containerTypeCode": "bottle_storage",
                        "containerTypeName": "进样瓶",
                        "allocateMode": "extend",
                        "selectedContainers": containers(gen_step.parameters['进样瓶编号'])
                    },
                    {
                        "containerCount": 0,
                        "containerTypeCode": "50ml_heat_resisting_tube",
                        "containerTypeName": "50ml耐热瓶",
                        "allocateMode": "extend",
                        "selectedContainers": []
                    }
                ]
            }
        }


# 电化学工作站
def chemical_test_data(gen_step):
    ex_details = get_details(station_dict[gen_step.workstation]['id'])        
    ex_pipelines = get_functions(station_dict[gen_step.workstation]['id'],station_dict[gen_step.workstation]['name'],station_dict[gen_step.workstation]['code'])
    all_actionParams, ex_pipeline, ex_pipeline_keys_action = get_action_param(ex_pipelines=ex_pipelines, ex_details=ex_details, gen_step=gen_step)
    # print("所有要输入的参数")
    # print(json.dumps(ex_pipeline_action_params, indent=2,ensure_ascii=False))
    #         break
    for key_id, ex_pipeline_key in enumerate(ex_pipeline_keys_action):
        ex_action = next( 
            (a for a in ex_details['actions'] if a['code'] == ex_pipeline_keys_action[ex_pipeline_key]),
            None
        )
        flag, param_id = check_input(ex_action)
        if not flag:
            continue
        ex_param_map = {p['paramCode']: p for p in ex_action['params']}
    # ex_pipeline_key, ex_action
    # ex_param_map
        for actionParams in all_actionParams:
            if actionParams['key'] == ex_pipeline_key:
                break
        for param in actionParams['params']:
            if param['paramCode'] in ex_param_map:
                if ex_param_map[param['paramCode']]['constraintValue'] != '' and ex_param_map[param['paramCode']]['constraintType'] == 'ENUM':
                        answer = get_value_by_label(ex_param_map[param['paramCode']]['constraintValue'], fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip()))
                elif ex_param_map[param['paramCode']]['dataType'] == 'array':
                    single_output = {}
                    for childparam in ex_param_map[param['paramCode']]['childParams']:
                        if childparam['constraintValue'] != '' and childparam['constraintType'] == 'ENUM':
                            single_output[childparam['paramCode']] = get_value_by_label(childparam['constraintValue'], fuzzy_search(gen_step.parameters, childparam['paramName'].strip()))
                        else:
                            single_output[childparam['paramCode']] = fuzzy_search(gen_step.parameters, childparam['paramName'].strip())
                    answer = [single_output]
                elif ex_param_map[param['paramCode']]['dataType'] == 'object':
                    single_output = {}
                    for childparam in ex_param_map[param['paramCode']]['childParams']:
                        if ex_param_map[param['paramCode']]['paramName'] not in gen_step.parameters:
                            single_output[childparam['paramCode']] = param['value'][childparam['paramCode']]
                            continue
                        
                        if childparam['constraintValue'] != '' and childparam['constraintType'] == 'ENUM':
                            # print("childparam--------", childparam, "\n", fuzzy_search(gen_step.parameters[ex_param_map[param['paramCode']]['paramName']], childparam['paramName'].strip()))
                            temp_single_output = get_value_by_label(childparam['constraintValue'], fuzzy_search(gen_step.parameters[ex_param_map[param['paramCode']]['paramName']], childparam['paramName'].strip()))
                            # print("temp_single_output:----------", temp_single_output, "\n", gen_step.parameters[ex_param_map[param['paramCode']]['paramName']], "\n", childparam['paramName'].strip())
                        else:
                            temp_single_output = fuzzy_search(gen_step.parameters[ex_param_map[param['paramCode']]['paramName']], childparam['paramName'].strip())
                            
                        single_output[childparam['paramCode']] = temp_single_output if temp_single_output else param['value'][childparam['paramCode']]
                    answer = single_output
                else:
                    answer = fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip())
                param['value'] = answer if answer else param['value']
            # 临时固定模板
            if param['paramCode']=='testTemplate':
                param['value'] = [{
                    "id":1549499770798080,
                    "modelName":"智能数据调试",
                    "modelValue":[{"order":0,
                                   "workDetectionType":"linearSweepVoltammetry",
                                   "param":{"initE":"0",
                                            "finalE":"0.8",
                                            "scanRate":"0.05",
                                            "sampleInterval":"0.001",
                                            "quietTime":"2",
                                            "sensitivity":"1e-001"}
                                   }]
                    }]
    data = {
            "stepType": "workstation",
            "workstationType": ex_details['code'],
            "workstationTypeName": ex_details['name'],
            "definitionVersion": ex_details['version'],
            "pipeline": ex_pipeline['code'],
            "actionParams": all_actionParams,
            "container":{                
                "allocation": [
                    {
                        "containerCount": len(gen_step.parameters['进样瓶编号']),
                        "containerTypeCode": "bottle_storage",
                        "containerTypeName": "进样瓶",
                        "allocateMode": "extend",
                        "selectedContainers": containers(gen_step.parameters['进样瓶编号'])
                    }
                ]
            }
        }
    return data
# chemical_test_data(steps.steps[-1])


# 置物工作站
def recover_data(gen_step):
    
    ex_details = get_details(station_dict[gen_step.workstation]['id'])        
    ex_pipelines = get_functions(station_dict[gen_step.workstation]['id'],station_dict[gen_step.workstation]['name'],station_dict[gen_step.workstation]['code'])
    actionParams, ex_pipeline, ex_pipeline_keys_action = get_action_param(ex_pipelines=ex_pipelines, ex_details=ex_details, gen_step=gen_step)
    for key_id, ex_pipeline_key in enumerate(ex_pipeline_keys_action):
        ex_action = next( 
            (a for a in ex_details['actions'] if a['code'] == ex_pipeline_keys_action[ex_pipeline_key]),
            None
        )
        flag, param_id = check_input(ex_action)
        if not flag:
            continue
        ex_param_map = {p['paramCode']: p for p in ex_action['params']}
        for actionParam in actionParams:
            if actionParam['key'] == ex_pipeline_key:
                break
        for param in actionParam['params']:
            if param['paramCode'] in ex_param_map:
                if ex_param_map[param['paramCode']]['constraintValue'] != '' and ex_param_map[param['paramCode']]['constraintType'] == 'ENUM':
                     answer = get_value_by_label(ex_param_map[param['paramCode']]['constraintValue'], fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip()))
                elif ex_param_map[param['paramCode']]['dataType'] == 'array':
                    single_output = {}
                    for childparam in ex_param_map[param['paramCode']]['childParams']:
                        if childparam['constraintValue'] != '' and childparam['constraintType'] == 'ENUM':
                            single_output[childparam['paramCode']] = get_value_by_label(childparam['constraintValue'], fuzzy_search(gen_step.parameters, childparam['paramName'].strip()))
                        else:
                            single_output[childparam['paramCode']] = fuzzy_search(gen_step.parameters, childparam['paramName'].strip())
                    answer = [single_output]
                else:
                    answer = fuzzy_search(gen_step.parameters, ex_param_map[param['paramCode']]['paramName'].strip())
                param['value'] = answer if answer else param['value']
            # break
    return {
            "stepType": "workstation",
            "workstationType": ex_details['code'],
            "workstationTypeName": ex_details['name'],
            "definitionVersion": ex_details['version'],
            "pipeline": ex_pipeline['code'],
            "actionParams": actionParams,
            "container":{                
                "allocation": [
                    {
                        "containerCount": len(gen_step.parameters['进样瓶编号']),
                        "containerTypeCode": "bottle_storage",
                        "containerTypeName": "进样瓶",
                        "allocateMode": "extend",
                        "selectedContainers": containers(gen_step.parameters['进样瓶编号'])
                    },
                    {
                        "containerCount": 0,
                        "containerTypeCode": "50ml_heat_resisting_tube",
                        "containerTypeName": "50ml耐热瓶",
                        "allocateMode": "extend",
                        "selectedContainers": []
                    }
                ]
            }
        }

def start_data():
    return {        
            "stepType": "ProcessControl",
            "workstationType": "start",
            "workstationTypeName": "开始",
            "definitionVersion": None,
            "pipeline": None,
            "actionParams": [

            ],
            "container": None
    }
def end_data():
    return {        
            "stepType": "ProcessControl",
            "workstationType": "end",
            "workstationTypeName": "结束",
            "definitionVersion": None,
            "pipeline": None,
            "actionParams": [

            ],
            "container": None
    }
def unlock_data():
    return {        
            "stepType": "ProcessControl",
            "workstationType": "unlock",
            "workstationTypeName": "解锁",
            "definitionVersion": None,
            "pipeline": None,
            "actionParams": [

            ],
            "container": None
    }
def lock_data():
    return {        
            "stepType": "ProcessControl",
            "workstationType": "lock",
            "workstationTypeName": "加锁",
            "definitionVersion": None,
            "pipeline": None,
            "actionParams": [

            ],
            "container": None
    }
    

OFFLINE_PIPELINE_MAP = {
    # MOD: 线上定义服务不可用时，使用这份静态 pipeline 名称做离线格式转换。
    "物料站": "common_take_flow",
    "液体进样站": "multi_add_flow",
    "磁力搅拌工作站": "multi_main_flow",
    "烘干机": "multi_main_flow",
    "纯化工作站": "new_pure_flow",
    "超声清洗": "multi_main_flow",
    "双工位电化学工作站": "multi_main_flow",
    "固体进样工作站": "solid_add_flow",
    "置物工作站": "put_flow",
}


def offline_container_data(gen_step):
    # MOD: 当线上定义拉取失败时，按站点类型生成一份通用 container 结构。
    allocations = []
    bottle_ids = gen_step.parameters.get('进样瓶编号') or gen_step.parameters.get('容器编号')
    if bottle_ids:
        allocations.append(
            {
                "containerCount": len(bottle_ids),
                "containerTypeCode": "bottle_storage",
                "containerTypeName": "进样瓶",
                "allocateMode": "extend",
                "selectedContainers": containers(bottle_ids),
            }
        )

    if gen_step.workstation == "液体进样站":
        allocations.extend([
            {
                "containerCount": 0,
                "containerTypeCode": "96_hole_plate",
                "containerTypeName": "96孔板",
                "allocateMode": "extend",
                "selectedContainers": [],
            },
            {
                "containerCount": 0,
                "containerTypeCode": "bottle",
                "containerTypeName": "西林瓶",
                "allocateMode": "extend",
                "selectedContainers": [],
            },
        ])
    elif gen_step.workstation in ("烘干机", "固体进样工作站", "超声清洗", "置物工作站"):
        allocations.append(
            {
                "containerCount": 0,
                "containerTypeCode": "50ml_heat_resisting_tube",
                "containerTypeName": "50ml耐热瓶",
                "allocateMode": "extend",
                "selectedContainers": [],
            }
        )
    elif gen_step.workstation == "纯化工作站":
        allocations.append(
            {
                "containerCount": len(gen_step.parameters.get('留样瓶编号', [])),
                "containerTypeCode": "bottle_sample_retention",
                "containerTypeName": "留样瓶",
                "allocateMode": "extend",
                "selectedContainers": containers(gen_step.parameters.get('留样瓶编号', [])) if gen_step.parameters.get('留样瓶编号') else [],
            }
        )

    return {"allocation": allocations}


def offline_action_params(gen_step):
    # MOD: 通用离线 actionParams，确保在缺少远端 action 定义时仍能完成格式转换。
    if gen_step.workstation in ("物料站", "置物工作站"):
        return []
    return [
        {
            "actionCode": gen_step.operation,
            "key": gen_step.operation,
            "params": [
                {
                    "paramCode": key,
                    "value": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
                }
                for key, value in gen_step.parameters.items()
            ],
        }
    ]


def offline_step_data(gen_step):
    # MOD: 线上服务全部不可达时的最终兜底格式转换。
    meta = get_workstation_meta(gen_step.workstation)
    return {
        "stepType": "workstation",
        "workstationType": meta["code"],
        "workstationTypeName": meta["name"],
        "definitionVersion": meta.get("version", ""),
        "pipeline": OFFLINE_PIPELINE_MAP.get(gen_step.workstation, gen_step.operation),
        "actionParams": offline_action_params(gen_step),
        "container": offline_container_data(gen_step),
    }


def gen_nodemap(ori_steps):
    counter = defaultdict(int)
    output = {}
    output["开始节点id"] = {
        "nodeId": "开始节点id",
        "dataId": "开始节点属性",
        "stepType": start_data()["stepType"],
        "workstationType": start_data()["workstationType"],
        "workstationTypeName": start_data()["workstationTypeName"]
    }
    output["结束节点id"] = {
        "nodeId": "结束节点id",
        "dataId": "结束节点属性",
        "stepType": end_data()["stepType"],
        "workstationType": end_data()["workstationType"],
        "workstationTypeName": end_data()["workstationTypeName"]
    }
    # output["加锁节点id"] = {
    #     "nodeId": "加锁节点id",
    #     "dataId": "加锁节点属性",
    #     "stepType": lock_data()["stepType"],
    #     "workstationType": lock_data()["workstationType"],
    #     "workstationTypeName": lock_data()["workstationTypeName"]
    # }
    # output["解锁节点id"] = {
    #     "nodeId": "解锁节点id",
    #     "dataId": "解锁节点属性",
    #     "stepType": unlock_data()["stepType"],
    #     "workstationType": unlock_data()["workstationType"],
    #     "workstationTypeName": unlock_data()["workstationTypeName"]
    # }

    for step in ori_steps.steps:
        # print(step)
        count = counter[step.workstation]
        node_name = f"{step.workstation}{count}节点id"
        data_name = f"{step.workstation}{count}节点属性"
        meta = get_workstation_meta(step.workstation)
        output[node_name] = {
            "nodeId": node_name,
            "dataId": data_name,
            "stepType": "workstation",
            "workstationType": meta['code'],
            "workstationTypeName": meta['name']
        }
        counter[step.workstation]+=1
    return output
# gen_nodemap(steps)


def gen_path(ori_step):
    path_dict = {}
    counter = defaultdict(int)
    # path_dict["开始节点id"] = '加锁节点id'
    for step_id, step in enumerate(ori_step.steps):
        # print(step)
        count = counter[step.workstation]
        # if "加锁节点id" not in path_dict:
        #     path_dict["加锁节点id"] = step.workstation+str(count)+"节点id"
        if "开始节点id" not in path_dict:
            path_dict["开始节点id"] = step.workstation+str(count)+"节点id"
        else:
            path_dict[ori_step.steps[step_id-1].workstation+str(counter[ori_step.steps[step_id-1].workstation]-1)+"节点id"] = step.workstation+str(count)+"节点id"
        counter[step.workstation]+=1
        last_station = step.workstation+str(count)+"节点id"
    # path_dict[last_station] = '解锁节点id'
    # path_dict['解锁节点id'] = '结束节点id'
    path_dict[last_station] = '结束节点id'
    # print(last_station)
    return path_dict
# gen_path(steps)


def gen_data_map(ori_steps):
    # MOD: 当前主链路是“远端 dataTransform + 本地 fallback”，UnifiedAgent 缺失时允许继续运行。
    if UnifiedAgent is not None:
        UnifiedAgent(temperature=0.1)

    # MOD: 改为动态读取 station_dict，避免固体进样工作站等站点被硬编码遗漏。
    workstation_name_to_id = build_workstation_name_to_id()

    # 控制节点的处理函数（保持不变）
    control_nodes = {
        "开始": start_data,
        "结束": end_data,
        # "解锁": unlock_data,
        # "加锁": lock_data,
    }

    datamap_dict = {}

    # 处理控制节点
    for item, value in control_nodes.items():
        other_container = value()
        other_container['dataId'] = item+"节点属性"
        datamap_dict[other_container['dataId']] = other_container

    # 处理工作站节点 - 使用统一智能体
    counter = defaultdict(int)
    # 操作映射字典
    operation_mapping = {"离心": "纯化离心", "静置烘干": "烘干主流程",}
    
    for step in ori_steps.steps:
        # 获取工作站ID
        workstation_id = workstation_name_to_id.get(step.workstation)

        if workstation_id:
            print(f"[调试] 处理步骤: {step.step_number}, 工作站: {step.workstation}, 操作: {step.operation}")
            # 如果operation在映射字典中，则使用映射后的值
            mapped_operation = operation_mapping.get(step.operation, step.operation)
            if step.operation in operation_mapping:
                print(f"[调试] 操作映射: {step.operation} -> {mapped_operation}")
            
            # 使用统一智能体进行数据转换
            user_input = {
                "step_number": step.step_number,
                "workstation": step.workstation,
                "id": workstation_id,
                "operation": mapped_operation,
                "parameters": step.parameters
            }
            try:
                print(f"[调试] 开始处理工作站: {step.workstation} (ID: {workstation_id})")
                user_input_json = json.dumps(user_input, ensure_ascii=False)
                print(f"[调试] 请求数据(JSON): {user_input_json}")
                
                # 调用HTTP API获取数据转换结果（同步方式）
                response = requests.post(
                    "http://10.88.0.21:8018/worker/workstation/definition/dataTransform",
                    json=user_input,
                    headers={'Content-Type': 'application/json'},
                    timeout=30
                )
                print(f"[调试] HTTP响应状态码: {response.status_code}")
                response.raise_for_status()
                
                datamap_contain = response.json()['data']
                print(f"[调试] 获取到的数据: {datamap_contain}")
                
                # 检查返回的数据是否为None
                if datamap_contain is None:
                    raise ValueError(f"工作站 {step.workstation} (ID: {workstation_id}) 数据转换返回None，请检查API返回数据，请求数据: {user_input}")
                
                datamap_contain = stringify_param_values(datamap_contain)

                # 暂时没采用新格式，还是用原逻辑处理
                # fallback_functions = {
                #     "物料站": starting_station_data,
                #     "置物工作站": recover_data,
                #     "液体进样站": liquid_add_data,
                #     "磁力搅拌工作站": magnetic_data,
                #     "烘干机": dryer_data,
                #     "纯化工作站": pure_data,
                #     "超声清洗": ultrasonic_data,
                #     "双工位电化学工作站": chemical_test_data,
                # }
                # datamap_contain = stringify_param_values(fallback_functions[step.workstation](step))
                

            except Exception as e:
                print(f"警告: 工作站 {step.workstation} (ID: {workstation_id}) 转换失败: {e}")
                # MOD: 只有拿到了完整工作站元数据时，才继续尝试本地模板函数；
                # 否则直接走离线通用转换，避免在 get_details/get_functions 上重复超时。
                station_info = station_dict.get(step.workstation, {})
                can_use_local_fallback = isinstance(station_info, dict) and station_info.get('code') and station_info.get('name')
                if can_use_local_fallback:
                    fallback_functions = {
                        "物料站": starting_station_data,
                        "置物工作站": recover_data,
                        "液体进样站": liquid_add_data,
                        "磁力搅拌工作站": magnetic_data,
                        "烘干机": dryer_data,
                        "纯化工作站": pure_data,
                        "超声清洗": ultrasonic_data,
                        "固体进样工作站": solid_data,
                        "双工位电化学工作站": chemical_test_data,
                    }
                    try:
                        datamap_contain = stringify_param_values(fallback_functions[step.workstation](step))
                    except Exception as fallback_error:
                        print(f"[MOD] 工作站 {step.workstation} 本地模板也不可用，改走离线通用转换: {fallback_error}")
                        datamap_contain = offline_step_data(step)
                else:
                    print(f"[MOD] 工作站 {step.workstation} 缺少完整元数据，直接走离线通用转换")
                    datamap_contain = offline_step_data(step)
        else:
            print(f"警告: 未找到工作站 {step.workstation} 的ID映射")
            continue

        datamap_contain['dataId'] = step.workstation+str(counter[step.workstation])+"节点属性"
        datamap_dict[datamap_contain['dataId']] = datamap_contain
        counter[step.workstation]+=1

    return datamap_dict
# gen_data_map(steps)



def gen_control_data(datamap_dict):
    output = {}
    if '加锁节点属性' in datamap_dict:
        output['加锁节点属性'] = [
            {
                "dataId": datamap_dict['加锁节点属性']['dataId'],
                "section": "gx_303_lab"
            }
        ]
    return output
# gen_control_data(datamap_dict)


def gen_all(ori_steps):
    output = {}
    output['startNodeId'] ="开始节点id"
    output['endNodeId'] = '结束节点id'
    output['nodeMap'] = gen_nodemap(ori_steps)
    output['path'] = gen_path(ori_steps)
    output['dataMap'] = gen_data_map(ori_steps)
    output['lockMap'] = gen_control_data(output['dataMap'])
    return output


def build_ori_steps_from_json_data(workflow_json):
    # MOD: 支持直接把 workflow_json.json 这类中间层 JSON 转成 gen_all 可用的对象结构。
    steps = workflow_json.get("steps", [])
    return SimpleNamespace(
        steps=[SimpleNamespace(**step) for step in steps]
    )


def gen_all_from_json_data(workflow_json):
    # MOD: 允许外部直接传入 dict 版 workflow_json，减少重复封装代码。
    return gen_all(build_ori_steps_from_json_data(workflow_json))


def gen_all_from_json_file(json_path):
    # MOD: 允许外部直接传入 JSON 文件路径，让 workflow_json.json 可以直接走通现有主流程。
    with open(json_path, "r", encoding="utf-8") as f:
        workflow_json = json.load(f)
    return gen_all_from_json_data(workflow_json)

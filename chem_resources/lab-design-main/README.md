# Skill-OpenClaw

自动化实验工作流生成系统，包含实验方案设计和工作流生成两个核心技能模块。

## 📋 项目概述

本项目提供了一套完整的实验自动化解决方案，从用户需求到可执行工作流的端到端流程：

1. **实验方案设计** (`experiments-design`) - 基于用户输入生成结构化的实验方案
2. **工作流生成器** (`workflow-generator`) - 将结构化方案转换为工作站可执行的工作流模板

## 🚀 快速开始

### 完整工作流示例

```bash
# 步骤 1：设计实验方案
python experiments-design/scripts/run_design.py --user_input "设计一个DNA提取实验"

# 步骤 2：生成工作流模板
python workflow-generator-1.0.0/scripts/generate.py '{
  "experiment_steps": {
    "steps": [
      {
        "step_number": 1,
        "workstation": "物料站",
        "operation": "物料拿取",
        "parameters": {
          "容器类型": "进样瓶",
          "容器编号": [1, 2]
        }
      }
    ],
    "unknown_steps": null
  },
  "plan_name": "DNA提取实验"
}'
```

## 📦 技能模块

### 1️⃣ 实验方案设计 (experiments-design)

**功能描述**：基于用户目标生成可被转换接口处理的完整实验流程。

#### 前置条件
- 实验设计智能体服务地址：`http://127.0.0.1:8027`
- 依赖服务：experiments_design

#### 使用方法

```bash
python experiments-design/scripts/run_design.py --user_input '<用户输入>'
```

#### 触发关键词
- 实验设计
- 设计方案

#### 输出格式
返回结构化的实验方案 JSON，可直接供 workflow-generator 转换使用。

#### 错误处理

| 错误类型 | HTTP 状态码 | 处理方式 |
|---------|------------|---------|
| 智能体执行失败 | 500 | 返回错误详情 |
| 请求超时 | - | 返回超时提示 |

---

### 2️⃣ 工作流生成器 (workflow-generator)

**功能描述**：⚙️ 将结构化实验步骤转换为工作站可执行的工作流模板。

#### 前置条件

**Token 配置**：
- 需要在 OpenClaw 中配置 `WORKFLOW_TOKEN` 环境变量
- Token 格式：`Bearer xxxxxx`
- 如未提供，将使用默认 token

#### 使用方法

```bash
python workflow-generator-1.0.0/scripts/generate.py '<JSON>'
```

#### 请求参数

| 参数 | 类型 | 必填 | 默认值 | 描述 |
|------|------|------|--------|------|
| experiment_steps | Object | 是 | - | 实验步骤对象 |
| experiment_steps.steps | Array | 是 | - | 实验步骤数组 |
| experiment_steps.steps[].step_number | int | 是 | - | 步骤编号 |
| experiment_steps.steps[].workstation | str | 是 | - | 工作站名称（如"物料站"、"液体进样站"） |
| experiment_steps.steps[].operation | str | 是 | - | 操作类型（如"物料拿取"、"加液"） |
| experiment_steps.steps[].parameters | Object | 否 | {} | 操作参数（根据工作站不同而变化） |
| experiment_steps.unknown_steps | Array | 否 | null | 无法解析的步骤 |
| plan_name | str | 是 | - | 实验方案名称 |
| token | str | 否 | env.WORKFLOW_TOKEN | 认证令牌（覆盖环境变量） |

#### 使用示例

**基础工作流生成**：
```bash
python workflow-generator-1.0.0/scripts/generate.py '{
  "experiment_steps": {
    "steps": [
      {
        "step_number": 1,
        "workstation": "物料站",
        "operation": "物料拿取",
        "parameters": {
          "容器类型": "进样瓶",
          "容器编号": [1, 2]
        }
      }
    ],
    "unknown_steps": null
  },
  "plan_name": "test_plan"
}'
```

**多步骤液体处理**：
```bash
python workflow-generator-1.0.0/scripts/generate.py '{
  "experiment_steps": {
    "steps": [
      {
        "step_number": 1,
        "workstation": "物料站",
        "operation": "物料拿取",
        "parameters": {
          "容器类型": "进样瓶",
          "容器编号": [1, 2]
        }
      },
      {
        "step_number": 2,
        "workstation": "液体进样站",
        "operation": "加液",
        "parameters": {
          "容器类型": "进样瓶",
          "容器编号": [1, 2],
          "原液编号": [1, 2, 3],
          "原液量": [[0.2, 1.0, 2.8], [0.2, 1.0, 2.8]]
        }
      }
    ],
    "unknown_steps": null
  },
  "plan_name": "multi_step_plan"
}'
```

**使用自定义 Token**：
```bash
python workflow-generator-1.0.0/scripts/generate.py '{
  "experiment_steps": {
    "steps": [
      {
        "step_number": 1,
        "workstation": "物料站",
        "operation": "物料拿取",
        "parameters": {
          "容器类型": "进样瓶",
          "容器编号": [1]
        }
      }
    ],
    "unknown_steps": null
  },
  "plan_name": "custom_token_plan",
  "token": "$AICHEM_APP_TOKEN"
}'
```

#### 响应格式

**成功响应**：
```json
{
  "code": 200,
  "message": "工作流生成成功，请根据template_id前往平台查看",
  "data": {
    "template_id": "template_id"
  }
}
```

**错误响应**：
```json
{
  "code": 500,
  "message": "工作站指令解析失败: 错误详情",
  "data": null
}
```

## 🔗 模块联动

两个技能模块可以无缝协作，形成完整的实验自动化流程：

```bash
# 步骤 1：生成标准化 JSON 格式的实验方案
python experiments-design/scripts/run_design.py --user_input '<用户需求>'

# 步骤 2：使用返回的 template 调用 workflow-generator
python workflow-generator-1.0.0/scripts/generate.py '<返回的JSON>'
```

## ⚙️ 环境配置

### 必需的环境变量

```bash
# 工作流生成器认证令牌
export WORKFLOW_TOKEN="Bearer xxxxxx"
```

### 必需的依赖

- Python 3.x
- requests 库

安装依赖：
```bash
pip install requests
```

## 📝 注意事项

1. **容器编号**：所有容器编号必须为正整数
2. **参数单位**：参数单位必须与字段名称匹配
3. **服务端点**：
   - 实验设计服务：`http://127.0.0.1:8027`
   - 工作站解析服务：`http://114.214.215.131:30080/parse_workstation`
4. **超时设置**：实验设计请求超时时间为 3600 秒（1小时）

## 🛠️ 故障排查

### 常见问题

**Q: 实验设计服务连接失败**
```
A: 确保实验设计智能体服务在 http://127.0.0.1:8027 上运行
```

**Q: 工作流生成失败，提示认证错误**
```
A: 检查 WORKFLOW_TOKEN 环境变量是否正确配置，格式为 "Bearer xxxxxx"
```

**Q: JSON 解析错误**
```
A: 确保 JSON 格式正确，所有字段名称和值都使用双引号
```

## 📊 项目结构

```
skills/
├── experiments-design/          # 实验方案设计模块
│   ├── SKILL.md                # 技能说明文档
│   └── scripts/
│       └── run_design.py       # 实验设计脚本
├── workflow-generator-1.0.0/   # 工作流生成器模块
│   ├── SKILL.md                # 技能说明文档
│   ├── _meta.json              # 元数据配置
│   ├── references/             # 参考文档
│   └── scripts/
│       └── generate.py         # 工作流生成脚本
└── README.md                   # 项目说明文档（本文件）
```

## 📄 许可证

本项目为内部使用工具，具体许可证信息请咨询项目维护者。

## 🤝 贡献

如有问题或建议，请联系项目维护团队。

---

**当前状态**：两个模块均已完全功能化，可投入生产使用。

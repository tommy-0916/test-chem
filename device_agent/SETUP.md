# 化学实验Agent系统 - 开发环境设置指南

> **版本**: v0.5
> **最后更新**: 2026-01-30
> **Python版本**: 3.8.13+

---

## 目录

- [环境要求](#环境要求)
- [虚拟环境设置](#虚拟环境设置)
- [依赖安装](#依赖安装)
- [Git版本管理](#git版本管理)
- [运行测试](#运行测试)
- [开发规范](#开发规范)

---

## 环境要求

### 系统要求
- **操作系统**: Linux (推荐Ubuntu 20.04+)
- **Python**: 3.8.13+
- **Git**: 2.25.1+
- **内存**: 最少4GB可用内存
- **磁盘**: 最少1GB可用空间

### Python包管理器
- **推荐**: uv (快速、现代的Python包管理器)
- **备选**: pip + venv

---

## 虚拟环境设置

### 方法1: 使用uv（推荐）

```bash
# 安装uv（如果未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 进入项目目录
cd /workspace/chem_agent

# 创建虚拟环境
uv venv .venv

# 激活虚拟环境
source .venv/bin/activate

# 安装依赖
uv pip install -r requirements.txt
```

### 方法2: 使用venv

```bash
# 进入项目目录
cd /workspace/chem_agent

# 创建虚拟环境
python3 -m venv .venv

# 激活虚拟环境
source .venv/bin/activate

# 升级pip
pip install --upgrade pip

# 安装依赖
pip install -r requirements.txt
```

### 验证环境

```bash
# 检查Python版本
python --version  # 应该输出: Python 3.8.13+

# 检查虚拟环境
which python      # 应该指向: /workspace/chem_agent/.venv/bin/python

# 检查关键依赖
python -c "import langchain; print(f'langchain: {langchain.__version__}')"
python -c "import langgraph; print(f'langgraph: {langgraph.__version__}')"
python -c "import pydantic; print(f'pydantic: {pydantic.__version__}')"
```

---

## 依赖安装

### 依赖列表

创建 `requirements.txt` 文件：

```txt
# LangChain和LangGraph
langchain==1.2.7
langchain-openai==1.1.7
langgraph==1.0.7

# 数据验证
pydantic==2.12.5

# 环境变量
python-dotenv==1.2.1

# 重试机制
tenacity==9.1.2

# 其他
orjson==3.10.12
```

### 安装命令

```bash
# 使用uv
uv pip install -r requirements.txt

# 或使用pip
pip install -r requirements.txt
```

---

## Git版本管理

### Git配置

项目已初始化Git仓库，配置信息：

```bash
# 查看当前配置
git config --list

# 全局配置（如需修改）
git config --global user.email "your.email@example.com"
git config --global user.name "Your Name"

# 项目级配置（已配置）
git config user.email "ai@chem_agent.local"
git config user.name "Chem Agent Developer"
```

### Git工作流

#### 查看提交历史

```bash
# 查看提交历史
git log --oneline

# 查看最新提交详情
git log -1 --stat

# 查看某个提交的详细信息
git show <commit-id>
```

#### 创建新分支（开发时）

```bash
# 创建功能分支
git checkout -b feature/main-workflow

# 查看所有分支
git branch

# 切换分支
git checkout master
```

#### 提交代码

```bash
# 查看修改
git status

# 查看具体修改内容
git diff

# 添加文件
git add .

# 提交（注意提交信息格式）
git commit -m "v0.6: 完成主工作流集成

- 实现ChemExperimentWorkflow
- 串联4个Agent
- 实现条件路由
- 集成LogManager
- 编写端到端测试"
```

#### 查看标签

```bash
# 列出所有标签
git tag

# 创建标签
git tag v0.6

# 推送标签（如使用远程仓库）
git push origin v0.6
```

### 版本命名规范

```
v<主版本>.<次版本>.<修订版本>

示例:
- v0.1: 初始版本
- v0.2: 功能迭代
- v0.3: Bug修复
- v1.0: 稳定版本
```

### 当前Git状态

```bash
# 最新提交
commit 45d007b
版本: v0.5
日期: 2026-01-30

# 包含内容
- 54个文件
- 11555行代码
- 4个Agent完整实现
- 核心文件和工具类
- 所有文档和测试
```

---

## 运行测试

### 环境变量设置

创建 `.env` 文件（示例）：

```env
# LLM配置
OPENAI_API_KEY=your_api_key_here
OPENAI_API_BASE=https://apis.iflow.cn/v1
MODEL_NAME=qwen3-vl-plus
```

### 运行单个Agent测试

```bash
# 激活虚拟环境
source .venv/bin/activate

# Pre-flow Agent测试
python test_preflow_agent.py

# Workflow Generator测试
python test_workflow_generator.py

# Verify Agent测试
python test_verify_agent.py

# Format Translate Agent测试
python test_format_translate_agent.py
```

### 运行完整工作流测试（阶段5完成后）

```bash
# 完整工作流测试
python test_main_workflow.py

# 查看测试结果
cat /workspace/chem_resources/exp_logs/exp_XXXXXXXXX/iteration0.json
```

---

## 开发规范

### 代码结构

```
chem_agent/
├── core.py              # 基类和核心功能
├── workflow.py          # 工作流基类
├── common/              # 通用模块
├── utils/               # 工具类
├── pre_flow_agent/      # Pre-flow Agent
├── workflow_generator/  # Workflow Generator
├── verify_agent/        # Verify Agent
├── format_translate_agent/  # Format Translate Agent
└── test_*.py            # 测试脚本
```

### Agent开发规范

每个Agent目录应包含：

```
agent_name/
├── __init__.py
├── state.py            # 状态定义（Pydantic BaseModel）
├── workflow.py         # 工作流编排
├── prompts/
│   ├── __init__.py
│   ├── system_prompt.py   # 系统prompt
│   └── task_prompts.py    # 任务prompts
├── tools/
│   ├── __init__.py
│   └── *.py               # 工具定义（使用@tool装饰器）
└── ARCHITECTURE.md      # 架构文档
```

### 注释规范

```python
"""
模块级文档字符串

功能描述...
"""

class ClassName:
    """类文档字符串"""

    def method_name(self, param: str) -> str:
        """
        方法文档字符串

        Args:
            param: 参数描述

        Returns:
            返回值描述
        """
        pass
```

### 提交信息规范

```
v<版本>: <简洁描述>

- 详细描述1
- 详细描述2
- 详细描述3
```

示例：

```
v0.6: 完成主工作流集成

- 实现ChemExperimentWorkflow类
- 串联4个Agent（Pre-flow → Workflow Generator → Verify → Format Translate）
- 实现条件路由（Verify refused时返回Workflow Generator，最多重试3次）
- 集成LogManager进行日志记录
- 编写端到端测试脚本
- 更新PROGRESS.md和ARCHITECTURE.md
```

---

## 常见问题

### Q1: 虚拟环境激活失败

```bash
# 检查Python版本
python3 --version

# 重新创建虚拟环境
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
```

### Q2: 依赖安装失败

```bash
# 升级pip
pip install --upgrade pip

# 使用国内镜像源
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### Q3: Git提交失败

```bash
# 检查配置
git config --list

# 重新配置
git config user.email "your.email@example.com"
git config user.name "Your Name"
```

### Q4: 测试运行失败

```bash
# 检查环境变量
cat .env

# 检查知识库文件
ls -la /workspace/chem_resources/knowledge_agent/

# 查看日志
tail -f /workspace/chem_resources/exp_logs/*/iteration0.json
```

---

## 联系方式

- **项目路径**: /workspace/chem_agent
- **Git仓库**: 本地仓库（.git目录）
- **知识库**: /workspace/chem_resources/knowledge_agent/
- **日志目录**: /workspace/chem_resources/exp_logs/

---

> **最后更新**: 2026-01-30
> **维护者**: Chem Agent Developer
> **版本**: v0.5
# 版本记录

本仓库从审查后的源码快照建立独立历史；不继承旧 `chem-agent` 仓库的提交、实验输出和凭据。版本号描述代码快照，不表示实验可下发。

## 0.1.0-alpha.1 — 2026-09-24

- 初始导入 Research、Device、编排、共享合同、测试、必要配置和工作站能力文件。
- Research V2 的本地证据映射仅从当前知识库源文件提取可逐字核对的摘录，并在质量检查前绑定当前证据摘要；模型提示和压缩证据索引与校验合同对齐。
- 已验证的定向离线测试：`python -X utf8 -B -m unittest reaserch_agent.test_v2_contract reaserch_agent.test_skill_stages chem_agent_contracts.test_v2`（65 项通过）。这不是全套回归测试通过的声明。
- A02 Fresh V2 诊断仍在 `manual_required`：当前证据不足以确定前驱体数量、比例、溶剂体积等操作语义，模型候选也未满足完整物料合同；没有有效 Research→Device 交接、正式 workflow、可行性证书或真实下发。旧 Device 21 步计划仅作诊断材料。

## 后续版本约定

- 功能和修复先在独立分支开发，经对应离线测试与审查后合并到 `main`。
- `main` 上的版本使用 `VERSION` 与同名 Git 标签对应；未通过完整质量门或真实设备校验前使用预发布版本。
- 运行产物、日志、checkpoint、本地知识库和凭据不提交；实验结论必须注明对应代码版本、输入与门禁终态。

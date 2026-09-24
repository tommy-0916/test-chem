# 版本记录

本仓库从审查后的源码快照建立独立历史；不继承旧 `chem-agent` 仓库的提交、实验输出和凭据。版本号描述代码快照，不表示实验可下发。

## 未发布 — `fix/fresh-v2-a02-contract`（2026-09-24）

- 修正 V2 宏计划生成与原始证据/数量语义的若干共享映射、重试及表征能力投影问题；保留严格质量门和无效计划拒绝。
- 将 5 条经审查的 A02 本地论文摘录与来源许可说明纳入版本控制；新本地 JSON 证据 ID 和 bundle 指纹不再依赖绝对路径，旧状态仍可读取，核验状态仍为 `local_file`。普通运行时知识库、结果和 checkpoint 继续忽略。
- 两轮全新 A02 Research 诊断：第一轮模型输出在 32,768 token 上限截断；第二轮提高上限后，生成 8 步候选并重试，最终仍有 25 项质量问题，状态 `manual_required` / `macro_quality_error`，没有可交接 V2 包、Device 工作流或真实下发。首个误报涉及中文物料名与英文论文摘录的字面匹配；洗涤剂量、干燥条件、后续 XRD 制样等另有真实缺口，不能靠放宽校验消除。
- 干净 Windows 克隆需要短目标路径与 `core.longpaths=true`；在独立克隆中 5 条证据可见，相关 86 项离线测试通过。此验证不是 A02 全链路成功证明。

## 0.1.0-alpha.2 — 2026-09-24

- 本地知识库检索在排序后、截取 top-k 前合并科学内容完全相同的 JSON 副本；摄入时间、镜像来源等 `_ingestion_metadata` 不参与指纹，同标题但实验参数不同的记录仍保留。新增定向回归测试。
- 定向离线测试：`python -X utf8 -B -m unittest reaserch_agent.test_v2_contract reaserch_agent.test_skill_stages chem_agent_contracts.test_v2 reaserch_agent.test_corpus_search -q`，67 项通过。扩展证据测试另有 5 项 Windows SQLite 临时文件句柄清理错误，未宣称全套通过。
- A02 的 2018/2023 原始论文片段已联网核对并保存在本地忽略知识库；两轮全新 V2 Research 均在 `macro_plan_design` 模型调用发生 `LogicalCallDeadlineExceeded`，终态 `manual_required` / `macro_generation_error`。没有形成 V2 交接合同、Device 计划、workflow 或真实实验室任务；补证产物未纳入公开仓库。

## 0.1.0-alpha.1 — 2026-09-24

- 初始导入 Research、Device、编排、共享合同、测试、必要配置和工作站能力文件。
- Research V2 的本地证据映射仅从当前知识库源文件提取可逐字核对的摘录，并在质量检查前绑定当前证据摘要；模型提示和压缩证据索引与校验合同对齐。
- 已验证的定向离线测试：`python -X utf8 -B -m unittest reaserch_agent.test_v2_contract reaserch_agent.test_skill_stages chem_agent_contracts.test_v2`（65 项通过）。这不是全套回归测试通过的声明。
- A02 Fresh V2 诊断仍在 `manual_required`：当前证据不足以确定前驱体数量、比例、溶剂体积等操作语义，模型候选也未满足完整物料合同；没有有效 Research→Device 交接、正式 workflow、可行性证书或真实下发。旧 Device 21 步计划仅作诊断材料。

## 后续版本约定

- 功能和修复先在独立分支开发，经对应离线测试与审查后合并到 `main`。
- `main` 上的版本使用 `VERSION` 与同名 Git 标签对应；未通过完整质量门或真实设备校验前使用预发布版本。
- 运行产物、日志、checkpoint、普通本地知识库和凭据不提交；审查过的可复现证据作为明确标注的 fixture 例外。实验结论必须注明对应代码版本、输入与门禁终态。

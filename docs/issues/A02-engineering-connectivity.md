# [A02/V2] 无下发诊断 Research → Device → workflow 工程联通性

> 正式质量门不得标记为通过；本 Issue 的目标是定位工程链路能走到哪一层，不是证明 A02 科学方案可执行。

## 已测基线

- 分支：`fix/fresh-v2-a02-contract`。Fresh Research 运行使用 `1616aa7`、独立新状态、5 条已审查的本地证据、`kimi-k3`、关闭在线文献和 web search，`real_dispatch=false`；未复用旧 Research state 或 Device checkpoint。
- 首轮模型响应在 32,768 output-token 上限处 `response.incomplete`。提高到 65,536 后，首次及重试分别生成 8 步候选；原质量门复核分别报 44 和 25 项。最终 `manual_required / macro_quality_error`，正式 `macro_plan=[]`、`research_action_package_v2={}`。
- 直接将该正式 Research state 交给 Device V2，因无已接受宏计划而在 Device 生成前退出。把被拒绝候选尝试转换为 V2 合同，首个阻断是 `quantity_requirements[1] literature_calculation requires frozen paper evidence`。目前没有有效 V2 handoff、Device 证书、正式 workflow 或 303 实验室 task ID。
- 后续代码已保存完整被拒绝计划与质量问题，且将 5 条审查证据及来源说明纳入 `reaserch_agent/fixtures/a02_verified_kb/`；干净 Windows 克隆上的 127 项 Research/合同测试、184 项 Device/边界测试和工作站索引检查通过。这些组件测试**不是** A02 全链路通过。

## 已测：强制 V1 兼容诊断

为单独测试接口连通性，将上述 **被拒绝的原始候选**复制进隔离的诊断 state，以 `--contract-version v1` 调用 Device，并使用全新的结果及 checkpoint 目录、`--no-resume-checkpoints`、`real_dispatch=false`。这是刻意越过 Research 正式接收条件的诊断输入：不得改写原正式 state，不得将诊断输出称为 V2 通过、可行性证书或可下发 workflow。记录 Device 到达的最深阶段、具体失败字段与产物路径；即使能导出文件，也必须显著标记为 `diagnostic_unvalidated`，不得进入真实实验室下发。

本次隔离输入在本机 `result/A02-forced-v1-compat-handoff-20260924-201554/`；原 Research JSON 的 SHA-256 为 `27d1da6f6d6db8a9593757c3d9496f324b8d267fd5d1d531e18befcb8cf35648`，原文件未修改。使用仓库 `.venv`、`kimi-k3`/`codex_responses`/`high`、独立 checkpoint、`--no-resume-checkpoints` 运行，结果在本机 `result/A02-forced-v1-compat-device-20260924-191621/`。

- 第一次误用全局 Python，缺少 `langchain_core`，尚未进入 Device；改用仓库 `.venv` 后启动成功。
- Device 完成 `semantic_analysis`，覆盖 8 个宏步骤，并开始 `feasibility_device_plan`。随后 Kimi 返回 HTTP 403 `access_terminated_error`：5 小时使用额度已达上限。因此模型接口和第一阶段可联通，但规划、编译、workflow 导出**尚未验证完成**。
- CLI 退出码为 0，实际 `device_package.json.status=failed`、`failure_stage=device_internal_error`、`feasibility_accepted=false`。没有设备计划、workflow、证书、dispatch payload 或 303 实验室 task ID；不能把退出码当成功。
- `diagnostic_summary.json` 强制标为 `diagnostic_only_unvalidated`、`formal_success=false`、`dispatchable=false`、`real_dispatch=false`，只记录状态和计数，不复制可下发内容。原始 Device 包保留在忽略的本地目录用于排错，未交给下发适配器。

待 Kimi 额度恢复后，应从同一被拒绝的 Fresh V2 原始候选生成**新的隔离结果与 checkpoint**，再次使用 `--no-resume-checkpoints`，继续测到 Device 可行性、编译和 workflow 终态；即使届时 V1 兼容路径成功，也不代表 V2 合同通过。

## 已知未解决字段及责任边界

| 字段/步骤 | 当前值或证据 | 所需修复或输入 |
| --- | --- | --- |
| `R[0].quantity_requirements[0]` | 计划为 80 mL `去离子水`；2018 原文摘录为 `80 ml of deionized water` | 这是中文名与英文摘录的字面匹配问题；需可核验的双语物料绑定或保留来源原名，不应直接放宽全局 provenance 门。 |
| `R[1..3].quantity_requirements[0]` | `whole_batch` 前驱液、凝胶沉淀和冷却浆料；原文分别称 `the solution`、`reaction mixture`、收集的沉淀 | 逐步确认同批次身份和转移范围，不能把代词或混合物自动等同于沉淀。 |
| `R[4].material_inputs[1].quantity`、`material_contract_status.material_inputs` | 主动加入的洗涤水为 `null` / `unresolved`；2018 文献仅说用水洗若干次 | 需有授权依据的计划用量；不得把待测量或设备容量伪装为已有库存。依赖该水输入的 `material_relations` 也须重验。 |
| `R[5].material_contract_status.{material_intermediates,material_outputs,material_relations}`、`operation_segments[0].provenance` | 洗后干燥的状态变化与条件无来源支持，且上一步湿固体未获接受 | 先补权威同路线的干燥/洗后处理依据，再绑定输入、输出和操作；不能统一补 `state_change`。 |
| `R[6].material_inputs[0..1]`、`material_contract_status.*` | XRD 制样依赖未获接受的干粉；乙醇数量为 `null`，4.0 mL 只见于工作站约束，来源为 `agent_inferred` | 设备要求不构成添加外部试剂的科学授权；需用户或论文依据、计划量、状态变化及上游 lineage。 |
| `R[7].quantity_requirements[0]`、`material_inputs[0].parent_output_refs[0]` | XRD 悬液 `whole_batch` 指向上一步并不存在的合格输出 | 上一步输出获接受后重新核对样品身份和引用。 |
| 303 能力匹配 | 文献路线为 80 mL 前驱液、100 mL 回流瓶；索引加热瓶为 50 mL。电极文献为 Ni foam 喷涂，索引操作为碳纸滴涂 | 分别做容量、装置和基底的可行性审查；不得静默缩放或替换。 |

`R[i]` 是第二轮 `raw_llm_outputs.macro_plan_design_retry_1.macro_plan[i]` 的零基序号。2018/2023 论文来源及摘录见版本化的 `reaserch_agent/fixtures/a02_verified_kb/NOTICE.md` 和相邻 JSON。25 项错误含关联级联，不应解释为 25 个独立科学事实缺口。

## 验收标准

- [x] 诊断运行保留独立输入、代码 HEAD、模型/接口配置（不含密钥）、新 checkpoint 与产物路径；写明 Device 到达阶段、首个失败原因和终端状态。
- [x] 强制兼容运行的安全摘要标注 `diagnostic_only_unvalidated`，不将原始包转换为正式 `completed`、可行性证书、`dispatchable=true` 或 303 task ID；未调用真实下发。
- [ ] 区分并修复双语物料绑定误报与真正缺失的洗涤剂量、干燥条件、XRD 试剂授权/数量；每项记录字段、实际值、约束、来源和责任模块。
- [ ] 使用补足且可追溯的输入重新运行全新 Fresh V2 Research。仅在正式 Research 合同通过后才运行真正 V2 Device 与 workflow 检查；旧 V1 候选及 checkpoint 不得作为通过证据。

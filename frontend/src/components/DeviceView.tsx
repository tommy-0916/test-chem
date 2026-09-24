import {
  AlertTriangle,
  CheckCircle2,
  Container,
  FileText,
  FlaskConical,
  Gauge,
  ListChecks,
  MapPin,
  PackageOpen,
  ShieldCheck,
  Wrench,
  XCircle,
} from "lucide-react";
import type { DevicePackage, WorkflowStep } from "../types/api";
import { objectList, stringList } from "../utils/campaign";
import { StatusBadge } from "./StatusBadge";
import { WorkstationMap } from "./WorkstationMap";

interface DeviceViewProps {
  device: DevicePackage | null;
}

export function DeviceView({ device }: DeviceViewProps) {
  if (!device) {
    return (
      <div className="tab-empty">
        <Wrench size={24} />
        <strong>设备映射尚未开始</strong>
        <span>当前任务没有 workstation workflow 产物。</span>
      </div>
    );
  }

  const assessment = asObject(device.feasibility_assessment);
  const nestedFeasibility = asObject(assessment.feasibility);
  const feasibility = Object.keys(asObject(device.feasibility)).length
    ? asObject(device.feasibility)
    : nestedFeasibility;
  const errorPackage = asObject(device.error_package);
  const blocking = stringList(errorPackage.blocking_constraints).length
    ? stringList(errorPackage.blocking_constraints)
    : stringList(feasibility.blocking_constraints);
  const unsupported = objectList(errorPackage.unsupported_items).length
    ? objectList(errorPackage.unsupported_items)
    : objectList(feasibility.unsupported_items);
  const workflow = asObject(device.workflow_json);
  const steps = Array.isArray(workflow.steps) ? (workflow.steps as WorkflowStep[]) : [];
  const handoffs = objectList(workflow.offline_handoffs);
  const selfChecks = Object.entries(asObject(device.device_self_check));
  const isError = device.status === "feasibility_error" || device.feedback_type === "device_feasibility_error";

  return (
    <div className="tab-stack">
      <section className={`device-verdict ${isError ? "device-verdict--error" : "device-verdict--success"}`}>
        <div className="device-verdict__icon">
          {isError ? <AlertTriangle size={22} /> : <ShieldCheck size={22} />}
        </div>
        <div>
          <span className="eyebrow">DEVICE VERDICT</span>
          <h3>{isError ? "当前路线存在设备硬约束" : "设备映射已通过"}</h3>
          <p>{String(errorPackage.message || device.macro_plan_summary || (isError ? "请查看阻塞约束与建议修订。" : "Macro action 已映射为 workstation workflow。"))}</p>
        </div>
        <StatusBadge status={device.status || (isError ? "feasibility_error" : "success")} />
      </section>

      {blocking.length > 0 && (
        <section className="section-band section-band--danger">
          <header className="section-heading">
            <div>
              <span className="eyebrow">BLOCKING CONSTRAINTS</span>
              <h3><XCircle size={17} /> 阻塞约束</h3>
            </div>
            <span className="count-label">{blocking.length}</span>
          </header>
          <ol className="constraint-list">
            {blocking.map((constraint, index) => (
              <li key={`${constraint}-${index}`}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <p>{constraint}</p>
              </li>
            ))}
          </ol>
          {unsupported.length > 0 && (
            <div className="unsupported-grid">
              {unsupported.map((item, index) => (
                <article key={index}>
                  <strong>{String(item.requirement || item.macro_step || `Unsupported ${index + 1}`)}</strong>
                  <p>{String(item.reason || "未提供详细原因")}</p>
                  {item.suggested_research_revision ? <span>建议：{String(item.suggested_research_revision)}</span> : null}
                </article>
              ))}
            </div>
          )}
        </section>
      )}

      <WorkstationMap />

      <section className="section-band">
        <header className="section-heading">
          <div>
            <span className="eyebrow">WORKFLOW</span>
            <h3><ListChecks size={17} /> 工作站执行序列</h3>
          </div>
          <span className="count-label">{steps.length} STEPS</span>
        </header>
        {steps.length ? <WorkflowTable steps={steps} /> : <div className="compact-empty"><PackageOpen size={18} /> 暂无设备步骤</div>}
      </section>

      {(selfChecks.length > 0 || objectList(device.reagent_slot_plan).length > 0 || objectList(device.container_plan).length > 0) && (
        <div className="device-detail-grid">
          <section className="section-band">
            <header className="section-heading">
              <h3><Gauge size={17} /> Device self-check</h3>
            </header>
            {selfChecks.length ? (
              <dl className="self-check-list">
                {selfChecks.map(([key, value]) => {
                  const failed = /fail|失败|未通过|不通过/i.test(String(value));
                  return (
                    <div key={key}>
                      <dt>{failed ? <XCircle size={14} /> : <CheckCircle2 size={14} />}{key.replaceAll("_", " ")}</dt>
                      <dd>{String(value)}</dd>
                    </div>
                  );
                })}
              </dl>
            ) : <div className="compact-empty">未返回自检字段</div>}
          </section>
          <section className="section-band">
            <header className="section-heading">
              <h3><FlaskConical size={17} /> 原液与容器</h3>
            </header>
            <ResourceList title="原液槽位" icon={<MapPin size={14} />} items={objectList(device.reagent_slot_plan)} />
            <ResourceList title="容器台账" icon={<Container size={14} />} items={objectList(device.container_plan)} />
          </section>
        </div>
      )}

      {handoffs.length > 0 && (
        <section className="section-band section-band--handoff">
          <header className="section-heading">
            <div>
              <span className="eyebrow">OFFLINE HANDOFF</span>
              <h3><PackageOpen size={17} /> 离线观察点</h3>
            </div>
          </header>
          <div className="handoff-grid">
            {handoffs.map((handoff, index) => (
              <article key={index}>
                <strong>{String(handoff.name || `Handoff ${index + 1}`)}</strong>
                <span>{String(handoff.sample || handoff.object || "待回传样品")}</span>
                <JsonFacts value={handoff} omit={["name", "sample", "object"]} />
              </article>
            ))}
          </div>
        </section>
      )}

      {device.workflow_txt && (
        <details className="raw-workflow">
          <summary><FileText size={15} /> TXT workflow 原文</summary>
          <pre>{device.workflow_txt}</pre>
        </details>
      )}
    </div>
  );
}

function WorkflowTable({ steps }: { steps: WorkflowStep[] }) {
  return (
    <div className="table-scroll">
      <table className="data-table workflow-table">
        <thead><tr><th>STEP</th><th>工作站</th><th>操作</th><th>参数</th><th>MACRO</th></tr></thead>
        <tbody>
          {steps.map((step, index) => (
            <tr key={`${String(step.step_number || index)}-${String(step.workstation || "")}`}>
              <td><span className="step-index">{String(step.step_number || index + 1).padStart(2, "0")}</span></td>
              <td><strong>{step.workstation || "未标注工作站"}</strong></td>
              <td>{step.operation || "--"}{step.notes ? <small>{step.notes}</small> : null}</td>
              <td><JsonFacts value={asObject(step.parameters)} /></td>
              <td><span className="source-label">{step.source_macro_step ?? "--"}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ResourceList({ title, icon, items }: { title: string; icon: React.ReactNode; items: Record<string, unknown>[] }) {
  return (
    <div className="resource-list">
      <h4>{icon}{title}<span>{items.length}</span></h4>
      {items.length ? items.map((item, index) => (
        <div className="resource-list__row" key={index}>
          <strong>{String(item["名称"] || item["容器类型"] || item.name || `${title} ${index + 1}`)}</strong>
          <span>{String(item["浓度或说明"] || item["用途"] || item.description || "--")}</span>
        </div>
      )) : <span className="resource-list__empty">未返回数据</span>}
    </div>
  );
}

function JsonFacts({ value, omit = [] }: { value: Record<string, unknown>; omit?: string[] }) {
  const entries = Object.entries(value).filter(([key, item]) => !omit.includes(key) && item !== "" && item !== null && item !== undefined);
  if (!entries.length) return <span>--</span>;
  return (
    <dl className="json-facts">
      {entries.map(([key, item]) => (
        <div key={key}>
          <dt>{key.replaceAll("_", " ")}</dt>
          <dd>{typeof item === "object" ? JSON.stringify(item, null, 0) : String(item)}</dd>
        </div>
      ))}
    </dl>
  );
}

function asObject(value: unknown): Record<string, any> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, any>) : {};
}

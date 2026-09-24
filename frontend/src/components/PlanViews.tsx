import { AlertCircle, Check, Circle, ClipboardList, GitCommitHorizontal } from "lucide-react";
import type { MacroPlanStep, PlanRevision } from "../types/api";

interface StageRouteProps {
  stages: string[];
  currentStage?: string;
  closed?: boolean;
}

export function StageRoute({ stages, currentStage, closed = false }: StageRouteProps) {
  if (!stages.length) {
    return (
      <div className="compact-empty">
        <GitCommitHorizontal size={18} />
        Stage route 尚未生成
      </div>
    );
  }

  const currentIndex = Math.max(0, stages.indexOf(currentStage || ""));
  return (
    <ol className="stage-route">
      {stages.map((stage, index) => {
        const complete = closed || index < currentIndex;
        const active = !closed && index === currentIndex;
        return (
          <li className={`stage-route__item ${complete ? "stage-route__item--complete" : ""} ${active ? "stage-route__item--active" : ""}`} key={`${stage}-${index}`}>
            <span className="stage-route__marker">
              {complete ? <Check size={14} /> : active ? <Circle size={13} fill="currentColor" /> : index + 1}
            </span>
            <span>
              <small>STAGE {String(index + 1).padStart(2, "0")}</small>
              <strong>{stage}</strong>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

interface MacroPlanTableProps {
  steps: MacroPlanStep[];
}

export function MacroPlanTable({ steps }: MacroPlanTableProps) {
  if (!steps.length) {
    return (
      <div className="compact-empty compact-empty--success">
        <Check size={18} />
        当前没有待执行 macro action
      </div>
    );
  }

  return (
    <div className="table-scroll">
      <table className="data-table macro-table">
        <thead>
          <tr>
            <th>STEP</th>
            <th>操作</th>
            <th>试剂 / 对象</th>
            <th>关键参数</th>
            <th>来源</th>
          </tr>
        </thead>
        <tbody>
          {steps.map((step, index) => (
            <tr key={`${String(step["步骤序号"] || index)}-${String(step["操作"] || "")}`}>
              <td><span className="step-index">{String(step["步骤序号"] || index + 1).padStart(2, "0")}</span></td>
              <td><strong>{String(step["操作"] || "未命名操作")}</strong></td>
              <td>{String(step["试剂/对象"] || "--")}</td>
              <td className="macro-table__parameters">{String(step["参数"] || "--")}</td>
              <td><span className="source-label">{String(step["来源"] || "agent")}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface RevisionTimelineProps {
  revisions: PlanRevision[];
}

export function RevisionTimeline({ revisions }: RevisionTimelineProps) {
  if (!revisions.length) {
    return (
      <div className="compact-empty">
        <ClipboardList size={18} />
        暂无计划版本记录
      </div>
    );
  }

  return (
    <ol className="revision-list">
      {[...revisions].reverse().map((revision, index) => (
        <li key={`${revision.plan_version || index}-${revision.recorded_at || ""}`}>
          <span className="revision-list__node"><GitCommitHorizontal size={14} /></span>
          <div className="revision-list__content">
            <div className="revision-list__heading">
              <strong>v{revision.plan_version || revisions.length - index}</strong>
              <span>{revision.event || "updated"}</span>
              <span>{revision.scope || "plan"}</span>
              <small>{revision.recorded_at || ""}</small>
            </div>
            <p>{revision.reason || "未记录修改原因"}</p>
            {revision.observation_summary && (
              <div className="revision-list__observation">
                <AlertCircle size={13} />
                {revision.observation_summary}
              </div>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}

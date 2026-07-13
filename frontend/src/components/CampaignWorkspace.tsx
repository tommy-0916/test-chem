import { useEffect, useState } from "react";
import {
  BookOpenText,
  Braces,
  ClipboardList,
  FlaskConical,
  LayoutDashboard,
  ListChecks,
  Plus,
  Radio,
  RefreshCw,
  Square,
  SquareTerminal,
  Wrench,
} from "lucide-react";
import type { CampaignDetail, CampaignListItem, ObservationInput } from "../types/api";
import {
  campaignStatus,
  canCancel,
  compactId,
  devicePackage,
  formatDateTime,
  isActive,
  isAwaitingObservation,
  itemId,
  researchState,
} from "../utils/campaign";
import { DeviceView } from "./DeviceView";
import { EvidenceView, JsonView, LogsView } from "./EvidenceLogsViews";
import { ObservationForm } from "./ObservationForm";
import { MacroPlanTable, RevisionTimeline, StageRoute } from "./PlanViews";
import { OverviewView } from "./OverviewView";
import { StatusBadge } from "./StatusBadge";

type TabId = "overview" | "plan" | "device" | "evidence" | "logs" | "json";

const TABS: { id: TabId; label: string; icon: typeof LayoutDashboard }[] = [
  { id: "overview", label: "概览", icon: LayoutDashboard },
  { id: "plan", label: "计划", icon: ListChecks },
  { id: "device", label: "设备", icon: Wrench },
  { id: "evidence", label: "证据", icon: BookOpenText },
  { id: "logs", label: "日志", icon: SquareTerminal },
  { id: "json", label: "JSON", icon: Braces },
];

interface CampaignWorkspaceProps {
  detail: CampaignDetail;
  selectedItem: CampaignListItem | null;
  connection: "connecting" | "live" | "polling";
  onCreate: () => void;
  onRefresh: () => void;
  onCancel: (id: string) => void;
  onObservation: (id: string, observation: ObservationInput) => Promise<unknown>;
}

export function CampaignWorkspace({
  detail,
  selectedItem,
  connection,
  onCreate,
  onRefresh,
  onCancel,
  onObservation,
}: CampaignWorkspaceProps) {
  const [tab, setTab] = useState<TabId>("overview");
  const job = detail.job || selectedItem || {};
  const id = itemId(job);
  const research = researchState(detail);
  const device = devicePackage(detail);
  const campaign = detail.campaign;
  const summary = campaign?.summary;
  const status = campaignStatus(job);
  const revisions = campaign?.plan_revisions?.length ? campaign.plan_revisions : research?.plan_revisions || [];

  useEffect(() => setTab("overview"), [id]);

  return (
    <main className="workspace">
      <header className="workspace-header">
        <div className="workspace-header__identity">
          <span className="eyebrow">CAMPAIGN / {compactId(id)}</span>
          <div>
            <StatusBadge status={status} pulse={isActive(job)} />
            <span className="mode-label">{String(job.mode || "research_preview").replaceAll("_", " ")}</span>
          </div>
        </div>
        <div className="workspace-header__actions">
          <span className={`sync-state sync-state--${connection}`}>
            <Radio size={13} /> {connection === "live" ? "LIVE" : connection === "connecting" ? "LINKING" : "POLL"}
          </span>
          <button className="icon-button" type="button" onClick={onRefresh} title="刷新详情"><RefreshCw size={16} /><span className="sr-only">刷新详情</span></button>
          {canCancel(job) && (
            <button className="button button--danger" type="button" onClick={() => onCancel(id)}><Square size={13} fill="currentColor" />取消</button>
          )}
          <button className="button button--primary" type="button" onClick={onCreate}><Plus size={15} />创建任务</button>
        </div>
      </header>

      <section className="workspace-meta" aria-label="Campaign 元数据">
        <div><span>PHASE</span><strong>{job.phase || research?.last_completed_branch || "--"}</strong></div>
        <div><span>ITERATION</span><strong>{job.current_iteration ?? summary?.iterations_run ?? 0} / {job.max_iterations ?? "--"}</strong></div>
        <div><span>ADAPTER</span><strong>{job.execution_adapter || summary?.adapter || "preview"}</strong></div>
        <div><span>UPDATED</span><strong>{formatDateTime(job.updated_at || summary?.finished_at || job.created_at)}</strong></div>
      </section>

      {isAwaitingObservation(detail) && (
        <ObservationForm campaignId={id} onSubmit={(observation) => onObservation(id, observation)} />
      )}

      <nav className="workspace-tabs" aria-label="Campaign 详情视图">
        {TABS.map(({ id: tabId, label, icon: Icon }) => (
          <button className={tab === tabId ? "workspace-tabs__item workspace-tabs__item--active" : "workspace-tabs__item"} type="button" key={tabId} onClick={() => setTab(tabId)} aria-current={tab === tabId ? "page" : undefined}>
            <Icon size={15} />{label}
            {tabId === "plan" && research?.macro_plan?.length ? <span>{research.macro_plan.length}</span> : null}
            {tabId === "logs" && detail.logs?.lines?.length ? <span>{detail.logs.lines.length}</span> : null}
          </button>
        ))}
      </nav>

      <div className="workspace-content">
        {tab === "overview" && <OverviewView job={job} research={research} device={device} campaign={campaign} />}
        {tab === "plan" && (
          <div className="tab-stack">
            <section className="section-band">
              <header className="section-heading"><div><span className="eyebrow">STAGE ROUTE</span><h3><FlaskConical size={17} /> 阶段与当前计划</h3></div></header>
              <StageRoute stages={research?.stage_route || []} currentStage={research?.current_stage} closed={summary?.goal_reached} />
              <div className="stage-plan-copy"><span>当前 stage 计划</span><p>{research?.current_stage_plan || "尚未生成"}</p></div>
            </section>
            <section className="section-band">
              <header className="section-heading"><div><span className="eyebrow">MACRO ACTIONS</span><h3><ListChecks size={17} /> 待执行 Macro Plan</h3></div><span className="count-label">{research?.macro_plan?.length || 0} STEPS</span></header>
              <MacroPlanTable steps={research?.macro_plan || []} />
            </section>
            <section className="section-band">
              <header className="section-heading"><div><span className="eyebrow">PLAN LEDGER</span><h3><ClipboardList size={17} /> 版本演化</h3></div></header>
              <RevisionTimeline revisions={revisions} />
            </section>
          </div>
        )}
        {tab === "device" && <DeviceView device={device} />}
        {tab === "evidence" && <EvidenceView research={research} campaign={campaign} />}
        {tab === "logs" && <LogsView logs={detail.logs} research={research} device={device} />}
        {tab === "json" && <JsonView detail={detail} />}
      </div>
    </main>
  );
}

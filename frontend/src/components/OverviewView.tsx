import {
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle2,
  CircleDashed,
  FlaskConical,
  Microscope,
  Network,
  ScanSearch,
} from "lucide-react";
import type { CampaignArtifacts, DevicePackage, JobState, ResearchState } from "../types/api";
import { objectList, stringList } from "../utils/campaign";
import { StageRoute } from "./PlanViews";
import { StatusBadge } from "./StatusBadge";

interface OverviewViewProps {
  job: JobState;
  research: ResearchState | null;
  device: DevicePackage | null;
  campaign: CampaignArtifacts | null | undefined;
}

export function OverviewView({ job, research, device, campaign }: OverviewViewProps) {
  const summary = campaign?.summary;
  const route = Array.isArray(research?.stage_route) ? research.stage_route : [];
  const observations = objectList(research?.observations);
  const latestState = asObject(research?.latest_observation);
  const latest = Object.keys(latestState).length > 0
    ? latestState
    : observations.at(-1) || null;
  const interpretation = asObject(research?.observation_interpretation);
  const fit = asObject(research?.observation_stage_fit);
  const stageProgress = asObject(research?.stage_progress);
  const errors = stringList(research?.errors);
  const deviceError = device?.status === "feasibility_error" || device?.feedback_type === "device_feasibility_error";
  const blocking = stringList(asObject(device?.error_package).blocking_constraints);

  return (
    <div className="tab-stack">
      <section className="campaign-readout">
        <div className="campaign-readout__goal">
          <span className="eyebrow">RESEARCH OBJECTIVE</span>
          <h2>{job.query || summary?.query || "未命名研究任务"}</h2>
          <p>{research?.route_message || "等待 agent 返回下一条状态。"}</p>
        </div>
        <dl className="campaign-readout__metrics">
          <div><dt>ITERATION</dt><dd>{String(job.current_iteration ?? summary?.iterations_run ?? 0).padStart(2, "0")}</dd></div>
          <div><dt>PLAN VER.</dt><dd>{String(campaign?.plan_revisions?.length || summary?.plan_versions || research?.plan_revisions?.length || 0).padStart(2, "0")}</dd></div>
          <div><dt>OBS.</dt><dd>{String(observations.length).padStart(2, "0")}</dd></div>
        </dl>
      </section>

      <Pipeline
        phase={job.phase || job.status || "queued"}
        finished={Boolean(summary?.stop_reason)}
        goalReached={Boolean(summary?.goal_reached)}
        previewComplete={job.mode === "research_preview" && job.status === "completed"}
      />

      {(research?.manual_handoff || errors.length > 0 || deviceError) && (
        <section className="attention-band">
          <AlertTriangle size={19} />
          <div>
            <span className="eyebrow">ATTENTION REQUIRED</span>
            <h3>{research?.manual_handoff ? "研究层请求人工接管" : deviceError ? "设备层返回可行性错误" : "运行记录包含错误"}</h3>
            <p>{research?.manual_handoff || blocking[0] || errors.at(-1) || "请检查日志与原始状态。"}</p>
          </div>
          <StatusBadge status={research?.manual_handoff ? "manual_required" : deviceError ? "feasibility_error" : "failed"} />
        </section>
      )}

      <section className="section-band">
        <header className="section-heading">
          <div>
            <span className="eyebrow">STAGE ROUTE</span>
            <h3><Network size={17} /> 实验阶段路线</h3>
          </div>
          <span className="count-label">{route.length} STAGES</span>
        </header>
        <StageRoute stages={route} currentStage={research?.current_stage} closed={summary?.goal_reached} />
        {research?.current_stage_reason && <p className="stage-reason">{research.current_stage_reason}</p>}
      </section>

      <div className="overview-split">
        <section className="section-band current-stage-panel">
          <header className="section-heading">
            <div>
              <span className="eyebrow">CURRENT STAGE</span>
              <h3><FlaskConical size={17} /> {research?.current_stage || "等待 stage 设计"}</h3>
            </div>
            <StatusBadge status={research?.stage_progress_status || research?.status || "pending"} compact />
          </header>
          <p>{research?.current_stage_plan || "当前 stage 的化学语义实验计划尚未生成。"}</p>
          {Object.keys(stageProgress).length > 0 && (
            <div className="progress-note">
              <ArrowRight size={14} />
              {String(stageProgress.progress_summary || stageProgress.status || "阶段状态已更新")}
            </div>
          )}
        </section>

        <section className="section-band latest-observation-panel">
          <header className="section-heading">
            <div>
              <span className="eyebrow">LATEST OBSERVATION</span>
              <h3><Microscope size={17} /> 最新结果</h3>
            </div>
            {fit.status ? <StatusBadge status={String(fit.status)} compact /> : null}
          </header>
          {latest ? (
            <>
              <p>{String(latest.summary || latest.result || "已收到结构化 observation")}</p>
              {fit.reason ? <span className="fit-reason">{String(fit.reason)}</span> : null}
              <SignalRows interpretation={interpretation} />
            </>
          ) : (
            <div className="compact-empty"><ScanSearch size={18} /> 暂无 observation</div>
          )}
        </section>
      </div>

      {campaign?.iterations?.length ? (
        <section className="section-band">
          <header className="section-heading">
            <div><span className="eyebrow">ITERATION TRACE</span><h3><CircleDashed size={17} /> 最近迭代</h3></div>
          </header>
          <div className="iteration-strip">
            {campaign.iterations.slice(-6).map((iteration, index) => (
              <div key={`${iteration.iteration ?? index}-${iteration.phase || ""}`}>
                <strong>I-{String(iteration.iteration ?? index).padStart(2, "0")}</strong>
                <span>{iteration.phase || iteration.status || "recorded"}</span>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function Pipeline({
  phase,
  finished,
  goalReached,
  previewComplete,
}: {
  phase: string;
  finished: boolean;
  goalReached: boolean;
  previewComplete: boolean;
}) {
  const normalized = phase.toLowerCase();
  let active = 0;
  if (normalized.includes("device")) active = 1;
  if (normalized.includes("execut") || normalized.includes("awaiting")) active = 2;
  if (normalized.includes("evaluat") || normalized.includes("observation")) active = 3;
  if (finished) active = goalReached ? 4 : active;
  const items = [
    { label: "Research", icon: Bot },
    { label: "Device map", icon: Network },
    { label: "Execution", icon: FlaskConical },
    { label: "Evaluation", icon: Microscope },
  ];

  return (
    <ol className="pipeline">
      {items.map(({ label, icon: Icon }, index) => (
        <li className={`${index < active || active === 4 || (previewComplete && index === 0) ? "pipeline__item--complete" : ""} ${!previewComplete && index === active && active < 4 ? "pipeline__item--active" : ""}`} key={label}>
          <span>{index < active || active === 4 || (previewComplete && index === 0) ? <CheckCircle2 size={16} /> : <Icon size={16} />}</span>
          <strong>{label}</strong>
        </li>
      ))}
    </ol>
  );
}

function SignalRows({ interpretation }: { interpretation: Record<string, unknown> }) {
  const groups = [
    ["positive_signals", "正向信号", "positive"],
    ["negative_signals", "异常信号", "negative"],
    ["uncertainties", "不确定项", "uncertain"],
  ] as const;
  const visible = groups.filter(([key]) => stringList(interpretation[key]).length);
  if (!visible.length) return null;
  return (
    <div className="signal-groups">
      {visible.map(([key, label, tone]) => (
        <div className={`signal-group signal-group--${tone}`} key={key}>
          <strong>{label}</strong>
          <span>{stringList(interpretation[key]).join(" · ")}</span>
        </div>
      ))}
    </div>
  );
}

function asObject(value: unknown): Record<string, any> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, any>) : {};
}

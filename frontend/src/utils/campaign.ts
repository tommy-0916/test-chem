import type {
  CampaignDetail,
  CampaignListItem,
  DevicePackage,
  ResearchState,
} from "../types/api";

export type StatusTone = "neutral" | "info" | "success" | "warning" | "danger";

const ACTIVE_STATUSES = new Set([
  "queued",
  "running",
  "researching",
  "research_bootstrap",
  "device_mapping",
  "executing",
  "awaiting_observation",
  "evaluating_observation",
  "cancelling",
]);

const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  researching: "研究规划",
  research_bootstrap: "研究初始化",
  device_mapping: "设备映射",
  executing: "等待执行",
  awaiting_observation: "等待结果",
  evaluating_observation: "分析结果",
  completed: "本轮完成",
  normal: "结果正常",
  abnormal: "结果异常",
  inconclusive: "结论不足",
  continue_current_stage: "继续当前阶段",
  advance_to_next_stage: "推进下一阶段",
  success: "可执行",
  goal_reached: "目标达成",
  closure_ready: "准备收束",
  manual_required: "需要人工介入",
  feasibility_error: "设备不可行",
  feasibility_deadlock: "可行性死锁",
  max_iterations: "达到轮次上限",
  research_error: "研究层错误",
  device_error: "设备层错误",
  failed: "失败",
  cancelled: "已取消",
  cancelling: "取消中",
  not_implemented: "尚未实现",
  pending: "待处理",
  unknown: "状态未知",
};

export function itemId(item?: CampaignListItem | null): string {
  return String(item?.id || item?.campaign_id || "");
}

export function researchState(detail?: CampaignDetail | null): ResearchState | null {
  const value = detail?.research;
  if (!value || typeof value !== "object") return null;
  if ("state" in value && value.state && typeof value.state === "object") {
    return value.state as ResearchState;
  }
  return value as ResearchState;
}

export function devicePackage(detail?: CampaignDetail | null): DevicePackage | null {
  const value = detail?.device;
  if (!value || typeof value !== "object") return null;
  const candidate = value.terminal_package && typeof value.terminal_package === "object"
    ? value.terminal_package
    : value.package && typeof value.package === "object"
      ? value.package
      : value;
  const hasWorkflow = Boolean(
    candidate.workflow_txt ||
    (candidate.workflow_json &&
      Array.isArray(candidate.workflow_json.steps) &&
      candidate.workflow_json.steps.length > 0),
  );
  const hasOutcome = Boolean(
    candidate.status ||
    candidate.feedback_type ||
    candidate.exp_id ||
    candidate.error_package ||
    candidate.feasibility ||
    candidate.feasibility_assessment,
  );
  return hasWorkflow || hasOutcome ? candidate : null;
}

export function campaignStatus(item?: CampaignListItem | null): string {
  return String(item?.stop_reason || item?.status || item?.phase || "unknown").toLowerCase();
}

export function statusLabel(status?: string | null): string {
  const normalized = String(status || "unknown").toLowerCase();
  return STATUS_LABELS[normalized] || String(status || "状态未知").replaceAll("_", " ");
}

export function statusTone(status?: string | null): StatusTone {
  const normalized = String(status || "").toLowerCase();
  if (["goal_reached", "success", "closure_ready"].includes(normalized)) return "success";
  if (
    [
      "manual_required",
      "feasibility_error",
      "feasibility_deadlock",
      "max_iterations",
      "awaiting_observation",
    ].includes(normalized)
  ) {
    return "warning";
  }
  if (["failed", "research_error", "device_error", "cancelled"].includes(normalized)) {
    return "danger";
  }
  if (ACTIVE_STATUSES.has(normalized) || normalized === "completed") return "info";
  return "neutral";
}

export function isActive(item?: CampaignListItem | null): boolean {
  return ACTIVE_STATUSES.has(campaignStatus(item));
}

export function canCancel(item?: CampaignListItem | null): boolean {
  const status = campaignStatus(item);
  return ACTIVE_STATUSES.has(status) && status !== "cancelling";
}

export function isAwaitingObservation(detail?: CampaignDetail | null): boolean {
  const status = campaignStatus(detail?.job);
  const phase = String(detail?.job?.phase || "").toLowerCase();
  return status === "awaiting_observation" || phase === "awaiting_observation";
}

export function formatDateTime(value?: string | null): string {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function compactId(value?: string | null): string {
  if (!value) return "未分配";
  return value.length > 23 ? `${value.slice(0, 14)}…${value.slice(-6)}` : value;
}

export function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item)).filter(Boolean);
}

export function objectList(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"));
}

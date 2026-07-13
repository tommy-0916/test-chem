import { Clock3, FlaskConical, RefreshCw, Square, Waves } from "lucide-react";
import type { CampaignListItem } from "../types/api";
import {
  campaignStatus,
  canCancel,
  compactId,
  formatDateTime,
  isActive,
  itemId,
} from "../utils/campaign";
import { StatusBadge } from "./StatusBadge";

interface QueuePanelProps {
  campaigns: CampaignListItem[];
  selectedId: string;
  connection: "connecting" | "live" | "polling";
  onSelect: (id: string) => void;
  onCancel: (id: string) => void;
  onRefresh: () => void;
}

export function QueuePanel({
  campaigns,
  selectedId,
  connection,
  onSelect,
  onCancel,
  onRefresh,
}: QueuePanelProps) {
  const ordered = [...campaigns].sort((a, b) => Number(isActive(b)) - Number(isActive(a))).slice(0, 10);
  const activeCount = campaigns.filter(isActive).length;

  return (
    <aside className="queue-panel" aria-label="运行队列">
      <header className="queue-panel__header">
        <div>
          <span className="eyebrow">RUN QUEUE</span>
          <h2>运行队列</h2>
        </div>
        <button className="icon-button" type="button" onClick={onRefresh} title="刷新队列">
          <RefreshCw size={16} />
          <span className="sr-only">刷新队列</span>
        </button>
      </header>

      <div className="queue-summary">
        <div>
          <strong>{activeCount.toString().padStart(2, "0")}</strong>
          <span>正在运行</span>
        </div>
        <div>
          <strong>{campaigns.length.toString().padStart(2, "0")}</strong>
          <span>总任务数</span>
        </div>
      </div>

      <div className="connection-line">
        <Waves size={14} />
        <span>{connection === "live" ? "SSE 实时同步" : connection === "connecting" ? "正在建立实时连接" : "轮询同步"}</span>
        <span className={`connection-line__dot connection-line__dot--${connection}`} />
      </div>

      <div className="queue-list">
        {ordered.map((item) => {
          const id = itemId(item);
          const status = campaignStatus(item);
          return (
            <article
              className={`queue-item ${selectedId === id ? "queue-item--active" : ""}`}
              key={id}
            >
              <button className="queue-item__main" type="button" onClick={() => onSelect(id)}>
                <span className="queue-item__topline">
                  <StatusBadge status={status} pulse={isActive(item)} compact />
                  <small>I-{Number(item.current_iteration ?? item.iterations_run ?? 0).toString().padStart(2, "0")}</small>
                </span>
                <strong>{item.query || "未命名实验任务"}</strong>
                <span className="queue-item__meta">
                  <Clock3 size={12} />
                  {formatDateTime(item.updated_at || item.finished_at || item.created_at || item.started_at)}
                </span>
                <code>{compactId(id)}</code>
              </button>
              {canCancel(item) && (
                <button
                  className="icon-button icon-button--danger queue-item__cancel"
                  type="button"
                  onClick={() => onCancel(id)}
                  title="取消任务"
                >
                  <Square size={13} fill="currentColor" />
                  <span className="sr-only">取消 {id}</span>
                </button>
              )}
            </article>
          );
        })}
        {!ordered.length && (
          <div className="queue-empty">
            <FlaskConical size={20} />
            <span>队列为空</span>
          </div>
        )}
      </div>
    </aside>
  );
}

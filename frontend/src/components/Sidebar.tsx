import {
  Activity,
  Beaker,
  FlaskConical,
  History,
  Plus,
  Server,
} from "lucide-react";
import type { CampaignListItem, HealthResponse } from "../types/api";
import { campaignStatus, compactId, itemId, statusTone } from "../utils/campaign";

interface SidebarProps {
  campaigns: CampaignListItem[];
  selectedId: string;
  health: HealthResponse | null;
  healthError: boolean;
  onSelect: (id: string) => void;
  onCreate: () => void;
}

export function Sidebar({
  campaigns,
  selectedId,
  health,
  healthError,
  onSelect,
  onCreate,
}: SidebarProps) {
  const healthStatus = healthError ? "offline" : String(health?.status || "checking").toLowerCase();
  const healthOk = ["ok", "healthy", "ready"].includes(healthStatus);

  return (
    <aside className="sidebar" aria-label="主导航">
      <div className="brand-lockup">
        <div className="brand-lockup__mark" aria-hidden="true">
          <Beaker size={20} />
        </div>
        <div>
          <strong>CHEM/AGENT</strong>
          <span>实验规划控制台</span>
        </div>
      </div>

      <button className="sidebar-create" type="button" onClick={onCreate}>
        <Plus size={16} />
        新建 Campaign
      </button>

      <nav className="sidebar-nav" aria-label="功能区">
        <button className="sidebar-nav__item sidebar-nav__item--active" type="button">
          <Activity size={16} />
          运行工作台
        </button>
      </nav>

      <section className="sidebar-history" aria-labelledby="history-heading">
        <div className="sidebar-section-title">
          <span id="history-heading">
            <History size={14} />
            最近任务
          </span>
          <span>{campaigns.length}</span>
        </div>
        <div className="sidebar-history__list">
          {campaigns.slice(0, 7).map((item) => {
            const id = itemId(item);
            const tone = statusTone(campaignStatus(item));
            return (
              <button
                className={`history-item ${selectedId === id ? "history-item--active" : ""}`}
                key={id}
                type="button"
                onClick={() => onSelect(id)}
                title={item.query || id}
              >
                <span className={`history-item__status history-item__status--${tone}`} />
                <span className="history-item__copy">
                  <strong>{item.query || "未命名实验任务"}</strong>
                  <small>{compactId(id)}</small>
                </span>
              </button>
            );
          })}
          {!campaigns.length && (
            <div className="sidebar-history__empty">
              <FlaskConical size={16} />
              暂无历史任务
            </div>
          )}
        </div>
      </section>

      <div className="system-readout">
        <div className="system-readout__heading">
          <Server size={14} />
          后端服务
        </div>
        <div className="system-readout__status">
          <span className={healthOk ? "indicator indicator--ok" : "indicator indicator--warn"} />
          <strong>{healthOk ? "API ONLINE" : healthError ? "API OFFLINE" : "CHECKING"}</strong>
          <span>{health?.version ? `v${health.version}` : "port 8000"}</span>
        </div>
      </div>
    </aside>
  );
}

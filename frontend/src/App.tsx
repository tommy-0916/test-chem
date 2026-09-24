import { useState } from "react";
import { AlertTriangle, X } from "lucide-react";
import { CampaignWorkspace } from "./components/CampaignWorkspace";
import { EmptyState, ErrorState, LoadingState } from "./components/FeedbackState";
import { NewCampaignDialog } from "./components/NewCampaignDialog";
import { QueuePanel } from "./components/QueuePanel";
import { Sidebar } from "./components/Sidebar";
import { useCampaignConsole } from "./hooks/useCampaignConsole";

export default function App() {
  const consoleState = useCampaignConsole();
  const [createOpen, setCreateOpen] = useState(false);
  const [actionError, setActionError] = useState("");

  const cancel = async (id: string) => {
    if (!window.confirm(`确认取消 Campaign ${id}？`)) return;
    try {
      setActionError("");
      await consoleState.cancelCampaign(id);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "取消任务失败");
    }
  };

  const center = (() => {
    if (consoleState.listLoading && !consoleState.campaigns.length) {
      return <div className="workspace workspace--feedback"><LoadingState label="正在读取 Campaign 历史" /></div>;
    }
    if (consoleState.listError && !consoleState.campaigns.length) {
      return <div className="workspace workspace--feedback"><ErrorState message={consoleState.listError} onRetry={() => void consoleState.refresh()} /></div>;
    }
    if (!consoleState.campaigns.length && !consoleState.selectedId) {
      return <div className="workspace workspace--feedback"><EmptyState onCreate={() => setCreateOpen(true)} /></div>;
    }
    if (consoleState.detailError && !consoleState.detail) {
      return <div className="workspace workspace--feedback"><ErrorState message={consoleState.detailError} onRetry={() => void consoleState.refresh()} /></div>;
    }
    if (consoleState.detailLoading || !consoleState.detail) {
      return <div className="workspace workspace--feedback"><LoadingState label="正在装载 Campaign 工作台" /></div>;
    }
    return (
      <CampaignWorkspace
        detail={consoleState.detail}
        selectedItem={consoleState.selectedItem}
        connection={consoleState.connection}
        onCreate={() => setCreateOpen(true)}
        onRefresh={() => void consoleState.refresh()}
        onCancel={(id) => void cancel(id)}
        onObservation={consoleState.submitObservation}
      />
    );
  })();

  return (
    <div className="app-shell">
      <Sidebar
        campaigns={consoleState.campaigns}
        selectedId={consoleState.selectedId}
        health={consoleState.health}
        healthError={consoleState.healthError}
        onSelect={consoleState.setSelectedId}
        onCreate={() => setCreateOpen(true)}
      />
      {center}
      <QueuePanel
        campaigns={consoleState.campaigns}
        selectedId={consoleState.selectedId}
        connection={consoleState.connection}
        onSelect={consoleState.setSelectedId}
        onCancel={(id) => void cancel(id)}
        onRefresh={() => void consoleState.refresh()}
      />

      <NewCampaignDialog open={createOpen} onClose={() => setCreateOpen(false)} onCreate={consoleState.createCampaign} />

      {actionError && (
        <div className="action-toast" role="alert">
          <AlertTriangle size={16} />
          <span>{actionError}</span>
          <button type="button" onClick={() => setActionError("")} title="关闭错误提示"><X size={14} /><span className="sr-only">关闭</span></button>
        </div>
      )}
    </div>
  );
}

import { AlertTriangle, FlaskConical, LoaderCircle, RefreshCw } from "lucide-react";

interface LoadingStateProps {
  label?: string;
}

export function LoadingState({ label = "正在同步实验状态" }: LoadingStateProps) {
  return (
    <div className="feedback-state" role="status">
      <LoaderCircle className="feedback-state__spinner" size={24} />
      <div>
        <strong>{label}</strong>
        <span>等待后端响应</span>
      </div>
    </div>
  );
}

interface EmptyStateProps {
  onCreate: () => void;
}

export function EmptyState({ onCreate }: EmptyStateProps) {
  return (
    <div className="feedback-state feedback-state--empty">
      <FlaskConical size={28} />
      <div>
        <strong>还没有 Campaign</strong>
        <span>Campaign 历史为空</span>
      </div>
      <button className="button button--primary" type="button" onClick={onCreate}>
        创建首个任务
      </button>
    </div>
  );
}

interface ErrorStateProps {
  message: string;
  onRetry: () => void;
}

export function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <div className="feedback-state feedback-state--error" role="alert">
      <AlertTriangle size={24} />
      <div>
        <strong>数据连接失败</strong>
        <span>{message}</span>
      </div>
      <button className="button button--secondary" type="button" onClick={onRetry}>
        <RefreshCw size={15} />
        重试
      </button>
    </div>
  );
}

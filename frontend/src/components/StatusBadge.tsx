import { CircleDot, LoaderCircle } from "lucide-react";
import { statusLabel, statusTone } from "../utils/campaign";

interface StatusBadgeProps {
  status?: string | null;
  pulse?: boolean;
  compact?: boolean;
}

export function StatusBadge({ status, pulse = false, compact = false }: StatusBadgeProps) {
  const tone = statusTone(status);
  return (
    <span className={`status-badge status-badge--${tone} ${compact ? "status-badge--compact" : ""}`}>
      {pulse ? <LoaderCircle className="status-badge__spin" size={12} /> : <CircleDot size={12} />}
      <span>{statusLabel(status)}</span>
    </span>
  );
}

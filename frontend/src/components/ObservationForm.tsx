import { useState } from "react";
import { AlertTriangle, LoaderCircle, Send } from "lucide-react";
import type { ObservationInput } from "../types/api";

interface ObservationFormProps {
  campaignId: string;
  onSubmit: (observation: ObservationInput) => Promise<unknown>;
}

export function ObservationForm({ campaignId, onSubmit }: ObservationFormProps) {
  const [summary, setSummary] = useState("");
  const [observationType, setObservationType] = useState("");
  const [status, setStatus] = useState("");
  const [metrics, setMetrics] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!summary.trim()) {
      setError("请填写结果摘要。");
      return;
    }
    let parsedMetrics: Record<string, unknown> | undefined;
    if (metrics.trim()) {
      try {
        const parsed = JSON.parse(metrics);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("metrics 必须是 JSON object");
        }
        parsedMetrics = parsed as Record<string, unknown>;
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Metrics JSON 格式不正确");
        return;
      }
    }

    setSubmitting(true);
    setError("");
    try {
      await onSubmit({
        summary: summary.trim(),
        observation_type: observationType.trim() || undefined,
        status: status || undefined,
        metrics: parsedMetrics,
        notes: notes.trim() || undefined,
      });
      setSummary("");
      setMetrics("");
      setNotes("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Observation 提交失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="observation-form-panel" aria-labelledby="observation-form-title">
      <header>
        <div>
          <span className="eyebrow">MANUAL RETURN</span>
          <h3 id="observation-form-title">回传实验结果</h3>
        </div>
        <code>{campaignId}</code>
      </header>
      <form className="observation-form" onSubmit={submit}>
        <label className="observation-form__summary">
          <span className="field-label">结果摘要 <b>必填</b></span>
          <textarea
            className="textarea"
            value={summary}
            onChange={(event) => setSummary(event.target.value)}
            rows={4}
            placeholder="例如：XRD 特征峰与目标 PBA 相匹配，未见明显杂相。"
          />
        </label>
        <label>
          <span className="field-label">Observation 类型</span>
          <input className="text-input" value={observationType} onChange={(event) => setObservationType(event.target.value)} placeholder="XRD / Raman / electrochemistry" />
        </label>
        <label>
          <span className="field-label">执行状态</span>
          <select className="select-input" value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">未指定</option>
            <option value="success">Success</option>
            <option value="partial">Partial</option>
            <option value="failed">Failed</option>
          </select>
        </label>
        <label className="observation-form__metrics">
          <span className="field-label">Metrics JSON</span>
          <textarea className="textarea textarea--mono" value={metrics} onChange={(event) => setMetrics(event.target.value)} rows={3} placeholder={'{"phase_match": true, "peak_2theta": 17.3}'} />
        </label>
        <label className="observation-form__notes">
          <span className="field-label">补充记录</span>
          <input className="text-input" value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="样品、仪器或异常说明" />
        </label>
        {error && (
          <div className="form-error observation-form__error" role="alert">
            <AlertTriangle size={14} />
            {error}
          </div>
        )}
        <button className="button button--primary observation-form__submit" type="submit" disabled={submitting}>
          {submitting ? <LoaderCircle className="spin" size={15} /> : <Send size={15} />}
          {submitting ? "正在回传" : "提交 Observation"}
        </button>
      </form>
    </section>
  );
}

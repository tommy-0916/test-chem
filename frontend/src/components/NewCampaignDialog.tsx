import { useId, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  FileText,
  FlaskConical,
  LoaderCircle,
  Play,
  Settings2,
  ShieldCheck,
  Upload,
  Workflow,
  X,
} from "lucide-react";
import { api } from "../api/client";
import type {
  CampaignDetail,
  CampaignMode,
  CreateCampaignInput,
  ExecutionAdapter,
  WireApi,
} from "../types/api";

interface NewCampaignDialogProps {
  open: boolean;
  onClose: () => void;
  onCreate: (input: CreateCampaignInput) => Promise<CampaignDetail>;
}

export function NewCampaignDialog({ open, onClose, onCreate }: NewCampaignDialogProps) {
  const titleId = useId();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<CampaignMode>("research_preview");
  const [referenceText, setReferenceText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [adapter, setAdapter] = useState<ExecutionAdapter>("mock");
  const [maxIterations, setMaxIterations] = useState(6);
  const [enableMemory, setEnableMemory] = useState(false);
  const [includeDeviceContext, setIncludeDeviceContext] = useState(true);
  const [onlineLiterature, setOnlineLiterature] = useState(false);
  const [webSearch, setWebSearch] = useState(false);
  const [downloadPdfs, setDownloadPdfs] = useState(false);
  const [wireApi, setWireApi] = useState<WireApi>("chat");
  const [submitting, setSubmitting] = useState(false);
  const [submitStage, setSubmitStage] = useState("");
  const [error, setError] = useState("");

  if (!open) return null;

  const resetAndClose = () => {
    if (submitting) return;
    setError("");
    onClose();
  };

  const handleFiles = (incoming: FileList | null) => {
    if (!incoming) return;
    const next = Array.from(incoming).filter((file) => file.size <= 25 * 1024 * 1024);
    setFiles((current) => [...current, ...next].slice(0, 10));
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const normalizedQuery = query.trim();
    if (!normalizedQuery) {
      setError("请输入明确的研究目标。");
      return;
    }

    setSubmitting(true);
    setError("");
    try {
      const textReferences = referenceText
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean);
      const uploadedReferences: string[] = [];
      for (const [index, file] of files.entries()) {
        setSubmitStage(`上传参考资料 ${index + 1}/${files.length}`);
        const result = await api.upload(file);
        uploadedReferences.push(result.upload.reference);
      }

      setSubmitStage("创建 Campaign");
      await onCreate({
        query: normalizedQuery,
        mode,
        references: [...textReferences, ...uploadedReferences],
        execution_adapter: adapter,
        max_iterations: maxIterations,
        enable_memory: enableMemory,
        include_device_context: includeDeviceContext,
        online_literature: onlineLiterature,
        web_search: webSearch,
        download_pdfs: onlineLiterature && downloadPdfs,
        wire_api: wireApi,
      });
      setQuery("");
      setReferenceText("");
      setFiles([]);
      setOnlineLiterature(false);
      setWebSearch(false);
      setDownloadPdfs(false);
      setSubmitStage("");
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "任务创建失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) resetAndClose();
    }}>
      <section className="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className="dialog__header">
          <div>
            <span className="eyebrow">NEW CAMPAIGN</span>
            <h2 id={titleId}>创建实验任务</h2>
          </div>
          <button className="icon-button" type="button" onClick={resetAndClose} title="关闭">
            <X size={18} />
            <span className="sr-only">关闭</span>
          </button>
        </header>

        <form className="campaign-form" onSubmit={submit}>
          <div className="form-section">
            <div className="form-section__index">01</div>
            <div className="form-section__body">
              <label className="field-label" htmlFor="campaign-query">
                研究目标 <span>必填</span>
              </label>
              <textarea
                id="campaign-query"
                className="textarea textarea--query"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="例如：合成 K-PBA 粉末并通过离线 XRD 确认目标相"
                rows={4}
                autoFocus
              />
            </div>
          </div>

          <div className="form-section">
            <div className="form-section__index">02</div>
            <div className="form-section__body">
              <span className="field-label">运行模式</span>
              <div className="mode-segment" role="radiogroup" aria-label="运行模式">
                <button
                  className={mode === "research_preview" ? "mode-segment__item mode-segment__item--active" : "mode-segment__item"}
                  type="button"
                  role="radio"
                  aria-checked={mode === "research_preview"}
                  onClick={() => setMode("research_preview")}
                >
                  <ShieldCheck size={18} />
                  <span>
                    <strong>Research Preview</strong>
                    <small>安全预览</small>
                  </span>
                  {mode === "research_preview" && <Check size={15} />}
                </button>
                <button
                  className={mode === "full_campaign" ? "mode-segment__item mode-segment__item--active" : "mode-segment__item"}
                  type="button"
                  role="radio"
                  aria-checked={mode === "full_campaign"}
                  onClick={() => setMode("full_campaign")}
                >
                  <Workflow size={18} />
                  <span>
                    <strong>Full Campaign</strong>
                    <small>完整闭环</small>
                  </span>
                  {mode === "full_campaign" && <Check size={15} />}
                </button>
              </div>
              {mode === "full_campaign" && (
                <div className="inline-notice inline-notice--warning">
                  <AlertTriangle size={15} />
                  设备 workflow 由 LLM 生成；真实下发仍保持阻断。
                </div>
              )}
            </div>
          </div>

          <div className="form-section">
            <div className="form-section__index">03</div>
            <div className="form-section__body">
              <label className="field-label" htmlFor="campaign-references">
                参考资料 <span>每行一条，可选</span>
              </label>
              <textarea
                id="campaign-references"
                className="textarea"
                value={referenceText}
                onChange={(event) => setReferenceText(event.target.value)}
                placeholder={"10.1021/example.doi\n论文标题或 arXiv ID"}
                rows={3}
              />
              <div className="upload-row">
                <input
                  ref={fileInputRef}
                  className="sr-only"
                  type="file"
                  multiple
                  accept=".pdf,.json,.txt,.md"
                  onChange={(event) => handleFiles(event.target.files)}
                />
                <button className="button button--secondary" type="button" onClick={() => fileInputRef.current?.click()}>
                  <Upload size={15} />
                  添加文件
                </button>
                <span>PDF / JSON / TXT / MD，单文件不超过 25 MB</span>
              </div>
              {files.length > 0 && (
                <div className="file-list">
                  {files.map((file, index) => (
                    <div className="file-chip" key={`${file.name}-${file.lastModified}`}>
                      <FileText size={14} />
                      <span>{file.name}</span>
                      <small>{Math.max(1, Math.round(file.size / 1024))} KB</small>
                      <button type="button" onClick={() => setFiles((current) => current.filter((_, currentIndex) => currentIndex !== index))} title="移除文件">
                        <X size={13} />
                        <span className="sr-only">移除 {file.name}</span>
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          <details className="advanced-options">
            <summary>
              <Settings2 size={16} />
              高级选项
            </summary>
            <div className="advanced-options__grid">
              {mode === "full_campaign" && (
                <>
                  <div className="control-group">
                    <span className="field-label">结果回传</span>
                    <div className="mini-segment">
                      <button className={adapter === "mock" ? "mini-segment__item mini-segment__item--active" : "mini-segment__item"} type="button" onClick={() => setAdapter("mock")}>Mock</button>
                      <button className={adapter === "manual" ? "mini-segment__item mini-segment__item--active" : "mini-segment__item"} type="button" onClick={() => setAdapter("manual")}>Manual</button>
                    </div>
                  </div>
                  <label className="control-group">
                    <span className="field-label">最大迭代轮数</span>
                    <input
                      className="number-input"
                      type="number"
                      min={1}
                      max={20}
                      value={maxIterations}
                      onChange={(event) => setMaxIterations(Math.min(20, Math.max(1, Number(event.target.value) || 1)))}
                    />
                  </label>
                  <div className="control-group">
                    <span className="field-label">Wire API</span>
                    <div className="mini-segment">
                      <button className={wireApi === "chat" ? "mini-segment__item mini-segment__item--active" : "mini-segment__item"} type="button" onClick={() => setWireApi("chat")}>Chat</button>
                      <button className={wireApi === "codex_responses" ? "mini-segment__item mini-segment__item--active" : "mini-segment__item"} type="button" onClick={() => setWireApi("codex_responses")}>Responses</button>
                    </div>
                  </div>
                </>
              )}
              <div className="toggle-stack">
                <Toggle label="Campaign memory" checked={enableMemory} onChange={setEnableMemory} />
                <Toggle label="Device context" checked={includeDeviceContext} onChange={setIncludeDeviceContext} />
                <Toggle
                  label="Online literature"
                  checked={onlineLiterature}
                  onChange={(checked) => {
                    setOnlineLiterature(checked);
                    if (!checked) setDownloadPdfs(false);
                  }}
                />
                {onlineLiterature && (
                  <Toggle label="Download PDFs" checked={downloadPdfs} onChange={setDownloadPdfs} />
                )}
                <Toggle label="Web search" checked={webSearch} onChange={setWebSearch} />
              </div>
            </div>
          </details>

          {error && (
            <div className="form-error" role="alert">
              <AlertTriangle size={15} />
              {error}
            </div>
          )}

          <footer className="dialog__footer">
            <button className="button button--ghost" type="button" onClick={resetAndClose} disabled={submitting}>
              取消
            </button>
            <button className="button button--primary button--submit" type="submit" disabled={submitting}>
              {submitting ? <LoaderCircle className="spin" size={16} /> : mode === "research_preview" ? <FlaskConical size={16} /> : <Play size={16} />}
              {submitting ? submitStage : "创建并运行"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

interface ToggleProps {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}

function Toggle({ label, checked, onChange }: ToggleProps) {
  return (
    <label className="toggle-row">
      <span>{label}</span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <span className="toggle-row__track" aria-hidden="true"><span /></span>
    </label>
  );
}

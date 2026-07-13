import { useMemo, useState } from "react";
import {
  BookOpenText,
  Check,
  Clipboard,
  FileSearch,
  FileText,
  Quote,
  ScrollText,
  SearchX,
  SquareTerminal,
} from "lucide-react";
import type {
  CampaignArtifacts,
  CampaignDetail,
  CampaignLogs,
  DevicePackage,
  ResearchState,
  SearchHit,
} from "../types/api";
import { objectList, stringList } from "../utils/campaign";

export function EvidenceView({ research, campaign }: { research: ResearchState | null; campaign?: CampaignArtifacts | null }) {
  const report = asObject(research?.survey_report);
  const hits = Array.isArray(research?.knowledge_hits) ? research.knowledge_hits : [];
  const protocols = objectList(research?.extracted_protocols);
  const evidenceRefs = Array.from(new Set((campaign?.plan_revisions || research?.plan_revisions || []).flatMap((revision) => stringList(revision.evidence_refs))));

  if (!Object.keys(report).length && !hits.length && !protocols.length && !campaign?.final_report) {
    return <div className="tab-empty"><SearchX size={24} /><strong>暂无证据数据</strong><span>Research agent 尚未返回调研报告或文献命中。</span></div>;
  }

  return (
    <div className="tab-stack">
      {Object.keys(report).length > 0 && (
        <section className="section-band evidence-report">
          <header className="section-heading">
            <div><span className="eyebrow">SURVEY REPORT</span><h3><BookOpenText size={17} /> 调研摘要</h3></div>
          </header>
          <p className="evidence-report__summary">{String(report.summary || "调研报告已生成。")}</p>
          <div className="evidence-report__columns">
            <ReportList title="关键发现" items={stringList(report.key_findings)} />
            <ReportList title="路线启示" items={stringList(report.route_implications)} />
            <ReportList title="开放问题" items={stringList(report.open_questions)} />
          </div>
        </section>
      )}

      <section className="section-band">
        <header className="section-heading">
          <div><span className="eyebrow">KNOWLEDGE HITS</span><h3><FileSearch size={17} /> 证据命中</h3></div>
          <span className="count-label">{hits.length} SOURCES</span>
        </header>
        {hits.length ? (
          <div className="evidence-list">
            {hits.map((hit, index) => <EvidenceHit key={`${hit.title || "hit"}-${index}`} hit={hit} index={index} />)}
          </div>
        ) : <div className="compact-empty"><SearchX size={18} /> 本轮没有知识库命中</div>}
      </section>

      {(protocols.length > 0 || evidenceRefs.length > 0) && (
        <section className="section-band">
          <header className="section-heading">
            <div><span className="eyebrow">PROVENANCE</span><h3><Quote size={17} /> 过程与引用</h3></div>
          </header>
          <div className="provenance-grid">
            {protocols.slice(0, 8).map((protocol, index) => (
              <article key={index}>
                <strong>{String(protocol.source_title || protocol.title || `Protocol ${index + 1}`)}</strong>
                <span>{String(protocol.verification_status || protocol.full_text_status || "local evidence")}</span>
                <small>{Array.isArray(protocol.steps) ? `${protocol.steps.length} extracted steps` : "structured protocol"}</small>
              </article>
            ))}
            {evidenceRefs.map((reference) => (
              <article key={reference}><strong>{reference}</strong><span>plan evidence ref</span></article>
            ))}
          </div>
        </section>
      )}

      {campaign?.final_report && (
        <details className="final-report" open>
          <summary><FileText size={15} /> Campaign 最终报告</summary>
          <pre>{campaign.final_report}</pre>
        </details>
      )}
    </div>
  );
}

function ReportList({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <div>
      <h4>{title}</h4>
      <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul>
    </div>
  );
}

function EvidenceHit({ hit, index }: { hit: SearchHit; index: number }) {
  return (
    <article className="evidence-hit">
      <span className="evidence-hit__index">REF {String(index + 1).padStart(2, "0")}</span>
      <div>
        <h4>{hit.title || "未命名来源"}</h4>
        <p>{hit.synthesis_summary || hit.problem || "无摘要"}</p>
        <div className="evidence-hit__footer">
          {typeof hit.score === "number" && <span>SCORE {hit.score.toFixed(2)}</span>}
          {stringList(hit.matched_terms).slice(0, 6).map((term) => <span key={term}>{term}</span>)}
        </div>
      </div>
    </article>
  );
}

export function LogsView({ logs, research, device }: { logs?: CampaignLogs | null; research: ResearchState | null; device: DevicePackage | null }) {
  const lines = useMemo(() => {
    const collected = [
      ...stringList(logs?.lines),
      ...stringList(research?.logs),
      ...stringList(device?.logs),
      ...stringList(research?.errors).map((line) => `[research error] ${line}`),
      ...stringList(device?.errors).map((line) => `[device error] ${line}`),
    ];
    return Array.from(new Set(collected));
  }, [device?.errors, device?.logs, logs?.lines, research?.errors, research?.logs]);

  return (
    <section className="log-console">
      <header>
        <div><span className="eyebrow">RUNTIME LOG</span><h3><SquareTerminal size={17} /> 运行日志</h3></div>
        <span>{lines.length} LINES · cursor {String(logs?.cursor ?? "--")}</span>
      </header>
      {lines.length ? (
        <ol>{lines.map((line, index) => <li key={`${line}-${index}`}><span>{String(index + 1).padStart(3, "0")}</span><code>{line}</code></li>)}</ol>
      ) : <div className="log-console__empty"><ScrollText size={19} /> 等待日志输出</div>}
    </section>
  );
}

export function JsonView({ detail }: { detail: CampaignDetail }) {
  const [copied, setCopied] = useState(false);
  const json = JSON.stringify(detail, null, 2);
  const copy = async () => {
    await navigator.clipboard.writeText(json);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };
  return (
    <section className="json-view">
      <header>
        <div><span className="eyebrow">RAW PAYLOAD</span><h3>Campaign detail JSON</h3></div>
        <button className="button button--secondary" type="button" onClick={copy}>
          {copied ? <Check size={14} /> : <Clipboard size={14} />}
          {copied ? "已复制" : "复制 JSON"}
        </button>
      </header>
      <pre>{json}</pre>
    </section>
  );
}

function asObject(value: unknown): Record<string, any> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, any>) : {};
}

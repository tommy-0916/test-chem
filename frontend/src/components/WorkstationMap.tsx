import { ImageOff, Maximize2, Network } from "lucide-react";
import { useState } from "react";
import { workstationMapUrl } from "../api/client";

export function WorkstationMap() {
  const [failed, setFailed] = useState(false);

  return (
    <section className="workstation-map" aria-labelledby="workstation-map-title">
      <header>
        <div>
          <span className="eyebrow">DEVICE TOPOLOGY</span>
          <h3 id="workstation-map-title">
            <Network size={17} />
            工作站资源表
          </h3>
        </div>
        <div className="workstation-map__actions">
          <span className="workstation-map__live"><span /> TRUTH SOURCE</span>
          <a
            className="icon-button"
            href={workstationMapUrl()}
            target="_blank"
            rel="noreferrer"
            title="查看原始资源表"
          >
            <Maximize2 size={15} />
            <span className="sr-only">查看原始资源表</span>
          </a>
        </div>
      </header>
      <div className="workstation-map__viewport">
        {failed ? (
          <div className="workstation-map__fallback">
            <ImageOff size={25} />
            <strong>资源图暂不可用</strong>
            <span>设备数据仍可从 workflow 表格查看。</span>
          </div>
        ) : (
          <img
            src={workstationMapUrl()}
            alt="自动化化学工作站英文名与中文名对照表"
            onError={() => setFailed(true)}
          />
        )}
      </div>
    </section>
  );
}

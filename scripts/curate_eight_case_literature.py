from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SELECTIONS = {
    "A01": ["Operando Analysis of NiFe", "Operando Raman Spectroscopy", "Operando Reconstruction"],
    "A02": ["Promoting Surface Reconstruction", "In-situ structure and catalytic mechanism", "Promoting nickel oxidation state transitions"],
    "B01": ["NiMo/CoMoO4 Heterostructure", "Role of Oxidized Mo Species", "Superhydrophilic and superaerophobic Ni"],
    "B02": ["Exploring the Sub-nanoscale Structure", "Amorphous Phosphorus-Incorporated", "Effect of Ion Diffusion"],
    "C01": ["In situ self-growth of NiOOH", "Unfolding the Significance", "Unveiling the Role of Iron"],
    "C02": ["Electronic Structure Modulation Via Iron", "Nitrogen-doped mesoporous nickel cobaltite", "Revealing Adsorption Conformation"],
    "D01": ["Cobalt-Modified Palladium", "Palladium/Cobalt Carbonate", "Ethanol Oxidation on Noble and Non-Noble"],
    "D02": ["ZIF-67-Derived NiCo", "Seed-Assisted In Situ Encapsulation", "Different Interlayer Anions"],
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    curated = {}
    lines = ["# 八道题联网论文检索结果", "", "仅执行论文检索与相关性筛选，未运行实验规划或 Device 层。", ""]
    for case, title_fragments in SELECTIONS.items():
        payload = json.loads((args.run / f"{case}.json").read_text(encoding="utf-8"))
        selected = []
        for fragment in title_fragments:
            match = next(
                (paper for paper in payload["candidates"] if fragment.lower() in paper["title"].lower()),
                None,
            )
            if match is None:
                raise ValueError(f"{case}: selected title not found: {fragment}")
            match = dict(match)
            match["doi"] = re.sub(r"\.s\d+$", "", match.get("doi") or "", flags=re.I)
            selected.append(match)
        curated[case] = {
            "queries": payload["queries"],
            "selected_papers": selected,
            "selection_note": "按催化体系、目标反应和题目研究变量人工复核后保留。",
        }
        lines.extend([f"## {case}", ""])
        for paper in selected:
            identifier = paper.get("doi") or paper.get("url") or "无强标识"
            lines.append(
                f"- {paper['title']} ({paper.get('year') or '年份未返回'}), "
                f"{paper.get('venue') or '期刊元数据未返回'}, {identifier}"
            )
        lines.append("")
    (args.run / "curated_papers.json").write_text(
        json.dumps(curated, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.run / "curated_papers.md").write_text("\n".join(lines), encoding="utf-8")
    print(args.run / "curated_papers.json")
    print(args.run / "curated_papers.md")


if __name__ == "__main__":
    main()

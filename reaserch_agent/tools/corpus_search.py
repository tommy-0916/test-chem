"""Local corpus search used by the research bootstrap workflow."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from ..state import SearchHit

TOKEN_RE = re.compile(r"[A-Za-z0-9\-\+\./]+|[\u4e00-\u9fff]+")


class LocalExperimentCorpus:
    """Search over the local structured benchmark corpus."""

    def __init__(self, corpus_dir: str | Path | None = None) -> None:
        default_dir = Path(__file__).resolve().parents[2] / "structured_outputs"
        self._corpus_dir = Path(corpus_dir or default_dir)
        self._records = self._load_records()

    @property
    def corpus_dir(self) -> Path:
        return self._corpus_dir

    def _load_records(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        if not self._corpus_dir.exists():
            return records

        for path in sorted(self._corpus_dir.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue

            if path.suffix.lower() == ".json":
                record = self._load_json_record(path)
            elif path.suffix.lower() == ".pdf":
                record = self._load_pdf_record(path)
            else:
                record = None

            if record is not None:
                records.append(record)

        return records

    def _load_json_record(self, json_path: Path) -> Dict[str, Any] | None:
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        synthesis = data.get("2. 具体的合成步骤", {}) or {}
        steps = synthesis.get("参数列表", []) or []
        performance = data.get("3. 性能", []) or []
        search_text = self._build_search_text(data, synthesis, steps, performance)

        return {
            "title": data.get("文献题目", json_path.stem),
            "file_path": str(json_path),
            "problem": data.get("1. 解决的问题", ""),
            "synthesis_summary": synthesis.get("描述性总结", ""),
            "steps": steps,
            "performance": performance,
            "title_text": str(data.get("文献题目", "")).lower(),
            "problem_text": str(data.get("1. 解决的问题", "")).lower(),
            "summary_text": str(synthesis.get("描述性总结", "")).lower(),
            "search_text": search_text.lower(),
        }

    def _load_pdf_record(self, pdf_path: Path) -> Dict[str, Any] | None:
        text = self._extract_pdf_text(pdf_path)
        if not text.strip():
            return None

        summary = self._summarize_pdf_text(text)
        search_text = "\n".join([pdf_path.stem, text])
        return {
            "title": pdf_path.stem,
            "file_path": str(pdf_path),
            "problem": summary,
            "synthesis_summary": summary,
            "steps": [],
            "performance": [],
            "title_text": pdf_path.stem.lower(),
            "problem_text": summary.lower(),
            "summary_text": summary.lower(),
            "search_text": search_text.lower(),
        }

    def _extract_pdf_text(self, pdf_path: Path, max_pages: int = 12, max_chars: int = 24000) -> str:
        try:
            from pypdf import PdfReader
        except Exception:
            return ""

        try:
            reader = PdfReader(str(pdf_path))
        except Exception:
            return ""

        segments: List[str] = []
        for page in reader.pages[:max_pages]:
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            if page_text.strip():
                segments.append(page_text.strip())
            if sum(len(segment) for segment in segments) >= max_chars:
                break

        return self._normalize_text("\n".join(segments))[:max_chars]

    def _summarize_pdf_text(self, text: str, max_chars: int = 2000) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        cleaned_lines: List[str] = []
        for line in lines:
            lowered = line.lower()
            if lowered in {"references", "acknowledgements"}:
                break
            cleaned_lines.append(line)
            if sum(len(item) for item in cleaned_lines) >= max_chars:
                break
        summary = " ".join(cleaned_lines)
        return summary[:max_chars]

    def _normalize_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _build_search_text(
        self,
        data: Dict[str, Any],
        synthesis: Dict[str, Any],
        steps: Sequence[Dict[str, Any]],
        performance: Sequence[Dict[str, Any]],
    ) -> str:
        segments: List[str] = [
            str(data.get("文献题目", "")),
            str(data.get("1. 解决的问题", "")),
            str(synthesis.get("描述性总结", "")),
        ]

        for step in steps:
            segments.extend(
                [
                    str(step.get("操作", "")),
                    str(step.get("试剂/对象", "")),
                    str(step.get("参数", "")),
                ]
            )

        for item in performance:
            segments.extend(
                [
                    str(item.get("指标", "")),
                    str(item.get("数值", "")),
                    str(item.get("条件", "")),
                ]
            )

        return "\n".join(segment for segment in segments if segment)

    def search(self, queries: Sequence[str], top_k: int = 5) -> List[SearchHit]:
        if not self._records:
            return []

        normalized_queries = [query.strip() for query in queries if query and query.strip()]
        if not normalized_queries:
            normalized_queries = ["普鲁士蓝 类似物 合成"]

        scored: List[Tuple[float, SearchHit]] = []
        for record in self._records:
            score, matched_terms = self._score_record(record, normalized_queries)
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    SearchHit(
                        title=record["title"],
                        file_path=record["file_path"],
                        score=score,
                        problem=record["problem"],
                        synthesis_summary=record["synthesis_summary"],
                        steps=list(record["steps"]),
                        performance=list(record["performance"]),
                        matched_terms=matched_terms,
                    ),
                )
            )

        scored.sort(key=lambda item: (-item[0], item[1].title))
        return [item[1] for item in scored[:top_k]]

    def _score_record(self, record: Dict[str, Any], queries: Sequence[str]) -> Tuple[float, List[str]]:
        score = 0.0
        matched_terms: List[str] = []
        title_text = record["title_text"]
        problem_text = record["problem_text"]
        summary_text = record["summary_text"]
        search_text = record["search_text"]

        for query in queries:
            lowered_query = query.lower()
            if lowered_query in title_text:
                score += 18.0
                matched_terms.append(query)
            elif lowered_query in problem_text:
                score += 14.0
                matched_terms.append(query)
            elif lowered_query in summary_text:
                score += 10.0
                matched_terms.append(query)
            elif lowered_query in search_text:
                score += 8.0
                matched_terms.append(query)

            for token in self._tokenize(query):
                lowered_token = token.lower()
                if not lowered_token:
                    continue
                if lowered_token in title_text:
                    score += 4.0
                    matched_terms.append(token)
                elif lowered_token in problem_text:
                    score += 3.0
                    matched_terms.append(token)
                elif lowered_token in summary_text:
                    score += 2.0
                    matched_terms.append(token)
                elif lowered_token in search_text:
                    score += 1.0
                    matched_terms.append(token)

        deduped_terms = sorted({term for term in matched_terms if term})
        return score, deduped_terms

    def format_hits_for_prompt(self, hits: Iterable[SearchHit]) -> str:
        blocks: List[str] = []
        for index, hit in enumerate(hits, start=1):
            step_preview = "; ".join(
                f"{step.get('步骤序号', '?')}. {step.get('操作', '')}"
                for step in hit.steps[:4]
            )
            perf_preview = "; ".join(
                f"{item.get('指标', '')}: {item.get('数值', '')} ({item.get('条件', '')})"
                for item in hit.performance[:3]
            )
            blocks.append(
                "\n".join(
                    [
                        f"[案例 {index}] {hit.title}",
                        f"- 文件: {hit.file_path}",
                        f"- 匹配词: {', '.join(hit.matched_terms) if hit.matched_terms else '无'}",
                        f"- 解决的问题: {hit.problem or '未提供'}",
                        f"- 合成摘要: {hit.synthesis_summary or '未提供'}",
                        f"- 关键步骤预览: {step_preview or '未提供'}",
                        f"- 性能预览: {perf_preview or '未提供'}",
                    ]
                )
            )
        return "\n\n".join(blocks)

    def _tokenize(self, text: str) -> List[str]:
        return [token for token in TOKEN_RE.findall(text) if token.strip()]

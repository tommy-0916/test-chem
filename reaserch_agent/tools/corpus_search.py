"""Local corpus search used by the research bootstrap workflow."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from ..state import SearchHit
except ImportError:  # unittest discovery with reaserch_agent as start directory
    from reaserch_agent.state import SearchHit

TOKEN_RE = re.compile(r"[A-Za-z0-9\-\+\./]+|[\u4e00-\u9fff]+")
EXPERIMENT_KEYWORDS = [
    "synthesis",
    "synthesized",
    "prepared",
    "preparation",
    "experimental",
    "method",
    "materials",
    "electrode",
    "slurry",
    "electrolyte",
    "separator",
    "current collector",
    "characterization",
    "xrd",
    "sem",
    "tem",
    "ftir",
    "xps",
    "cv",
    "eis",
    "galvanostatic",
    "capacity",
    "cycle",
    "centrifuged",
    "washed",
    "dried",
    "vacuum",
    "stirred",
    "aged",
    "annealed",
    "calcined",
    "solution",
    "nanocube",
    "hexacyanoferrate",
    "prussian",
    "合成",
    "制备",
    "配制",
    "电极",
    "浆料",
    "电解液",
    "表征",
    "测试",
    "洗涤",
    "干燥",
    "离心",
    "陈化",
]
EXPERIMENT_UNIT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mmol|mol|mg|g|mL|L|M|h|min|s|°C|C|mA|A|V|mV|"
    r"mAh\s*g\s*[-−]?\s*1|mA\s*g\s*[-−]?\s*1|mV\s*s\s*[-−]?\s*1)\b",
    re.IGNORECASE,
)


class LocalExperimentCorpus:
    """Search over the local structured benchmark corpus."""

    def __init__(self, corpus_dir: str | Path | None = None) -> None:
        default_dir = Path(__file__).resolve().parents[1] / "chem_kb"
        self._corpus_dir = Path(corpus_dir or default_dir)
        self._records = self._load_records()

    def refresh(self) -> None:
        """Re-scan the corpus directory; newly ingested files become searchable."""
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
        experiment_details = self._summarize_json_experiment_details(synthesis, steps, performance)
        search_text = self._build_search_text(data, synthesis, steps, performance)

        return {
            "title": data.get("文献题目", json_path.stem),
            "file_path": str(json_path),
            # Ingestion timestamps/source bookkeeping can differ for copies of
            # the same paper. Retain every other field, including full steps,
            # so same-title experimental variants are not merged.
            "scientific_payload_digest": hashlib.sha256(
                json.dumps(
                    {key: value for key, value in data.items() if key != "_ingestion_metadata"},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "problem": data.get("1. 解决的问题", ""),
            "synthesis_summary": synthesis.get("描述性总结", ""),
            "experiment_details": experiment_details,
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
        experiment_details = self._extract_experiment_details(text)
        search_text = "\n".join([pdf_path.stem, text, experiment_details])
        return {
            "title": pdf_path.stem,
            "file_path": str(pdf_path),
            "problem": summary,
            "synthesis_summary": experiment_details or summary,
            "experiment_details": experiment_details,
            "steps": [],
            "performance": [],
            "title_text": pdf_path.stem.lower(),
            "problem_text": summary.lower(),
            "summary_text": "\n".join([summary, experiment_details]).lower(),
            "search_text": search_text.lower(),
        }

    def _extract_pdf_text(
        self,
        pdf_path: Path,
        max_pages: Optional[int] = None,
        max_chars: int = 180000,
    ) -> str:
        fitz_text = self._extract_pdf_text_with_fitz(
            pdf_path,
            max_pages=max_pages,
            max_chars=max_chars,
        )
        if fitz_text.strip():
            return fitz_text

        return self._extract_pdf_text_with_pypdf(
            pdf_path,
            max_pages=max_pages,
            max_chars=max_chars,
        )

    def _extract_pdf_text_with_fitz(
        self,
        pdf_path: Path,
        max_pages: Optional[int] = None,
        max_chars: int = 180000,
    ) -> str:
        try:
            import fitz
        except Exception:
            return ""

        try:
            document = fitz.open(str(pdf_path))
        except Exception:
            return ""

        segments: List[str] = []
        try:
            page_count = len(document) if max_pages is None else min(len(document), max_pages)
            for page_index in range(page_count):
                page = document[page_index]
                blocks = page.get_text("blocks") or []
                blocks = sorted(blocks, key=lambda block: (round(block[1], 1), round(block[0], 1)))
                page_segments: List[str] = []
                for block in blocks:
                    if len(block) < 5:
                        continue
                    block_text = str(block[4] or "").strip()
                    if not block_text:
                        continue
                    page_segments.append(block_text)
                if page_segments:
                    segments.append(f"[p.{page_index + 1}]\n" + "\n".join(page_segments))
                if sum(len(segment) for segment in segments) >= max_chars:
                    break
        finally:
            document.close()

        return self._normalize_pdf_text("\n\n".join(segments))[:max_chars]

    def _extract_pdf_text_with_pypdf(
        self,
        pdf_path: Path,
        max_pages: Optional[int] = None,
        max_chars: int = 180000,
    ) -> str:
        try:
            from pypdf import PdfReader
        except Exception:
            return ""

        try:
            reader = PdfReader(str(pdf_path))
        except Exception:
            return ""

        segments: List[str] = []
        pages = reader.pages if max_pages is None else reader.pages[:max_pages]
        for page_index, page in enumerate(pages):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            if page_text.strip():
                segments.append(f"[p.{page_index + 1}]\n" + page_text.strip())
            if sum(len(segment) for segment in segments) >= max_chars:
                break

        return self._normalize_pdf_text("\n".join(segments))[:max_chars]

    def _normalize_pdf_text(self, text: str) -> str:
        text = text.replace("\r", "\n")
        text = re.sub(r"([A-Za-z])-\s*\n\s*([A-Za-z])", r"\1\2", text)
        text = re.sub(r"([A-Za-z])-\s+([A-Za-z])", r"\1\2", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

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

    def _summarize_json_experiment_details(
        self,
        synthesis: Dict[str, Any],
        steps: Sequence[Dict[str, Any]],
        performance: Sequence[Dict[str, Any]],
        max_chars: int = 12000,
    ) -> str:
        blocks: List[str] = []
        summary = str(synthesis.get("描述性总结", "")).strip()
        if summary:
            blocks.append(f"合成摘要: {summary}")

        if steps:
            blocks.append("结构化实验步骤:")
            for step in steps:
                blocks.append(
                    " | ".join(
                        [
                            f"{step.get('步骤序号', '?')}. {step.get('操作', '')}",
                            f"试剂/对象: {step.get('试剂/对象', '')}",
                            f"参数: {step.get('参数', '')}",
                        ]
                    )
                )

        if performance:
            blocks.append("性能与测试条件:")
            for item in performance:
                blocks.append(
                    " | ".join(
                        [
                            f"指标: {item.get('指标', '')}",
                            f"数值: {item.get('数值', '')}",
                            f"条件: {item.get('条件', '')}",
                        ]
                    )
                )

        return self._normalize_text("\n".join(blocks))[:max_chars]

    def _extract_experiment_details(self, text: str, max_chars: int = 12000) -> str:
        """Summarize experiment-relevant details from full PDF text without an LLM call."""
        if not text.strip():
            return ""

        section = self._extract_likely_experimental_section(text)
        candidates = self._split_candidate_sentences(section or text)

        selected: List[str] = []
        seen = set()
        for sentence in candidates:
            normalized = self._normalize_text(sentence)
            if len(normalized) < 30 or normalized in seen:
                continue
            if self._is_experiment_sentence(normalized):
                selected.append(normalized)
                seen.add(normalized)
            if sum(len(item) for item in selected) >= max_chars:
                break

        if not selected and section:
            selected = [self._normalize_text(section)[:max_chars]]

        return " ".join(selected)[:max_chars]

    def _extract_likely_experimental_section(self, text: str, max_chars: int = 50000) -> str:
        lowered = text.lower()
        starts = [
            "supporting information",
            "supplementary information",
            "experimental details",
            "experimental section",
            "experimental procedures",
            "experimental procedure",
            "experimental",
            "materials and reagents",
            "materials and methods",
            "materials",
            "synthesis and characterization",
            "synthesis",
            "preparation",
            "methods",
            "methods",
            "实验部分",
            "实验方法",
            "材料与方法",
            "合成方法",
            "制备方法",
        ]
        ends = [
            "references",
            "acknowledgements",
            "acknowledgments",
            "associated content",
            "author information",
            "supporting information available",
            "参考文献",
            "致谢",
        ]

        start_indexes = []
        for marker in starts:
            for match in re.finditer(re.escape(marker), lowered):
                start_indexes.append(match.start())

        if not start_indexes:
            return self._collect_experiment_paragraphs(text, max_chars=max_chars)

        start = min(start_indexes)
        end_candidates = [
            lowered.find(marker, start + 500)
            for marker in ends
            if lowered.find(marker, start + 500) >= 0
        ]
        end = min(end_candidates) if end_candidates else min(len(text), start + max_chars)
        return text[start:end][:max_chars]

    def _collect_experiment_paragraphs(self, text: str, max_chars: int = 50000) -> str:
        paragraphs = [
            paragraph.strip()
            for paragraph in re.split(r"\n\s*\n|(?<=[.!?。])\s{2,}", text)
            if paragraph.strip()
        ]
        selected: List[str] = []
        seen = set()
        for paragraph in paragraphs:
            normalized = self._normalize_text(paragraph)
            if len(normalized) < 40 or normalized in seen:
                continue
            lowered = normalized.lower()
            unit_count = len(EXPERIMENT_UNIT_RE.findall(normalized))
            keyword_count = sum(1 for keyword in EXPERIMENT_KEYWORDS if keyword in lowered)
            formula_count = len(
                re.findall(
                    r"\b(?:K\d*Fe|K\d*\[|Na\d*\[|Fe\(|Co\(|Ni\(|Cu\(|Zn\(|Mn\(|CN|NO3|Cl\d?|citrate|PVP|sulfur)\b",
                    normalized,
                    flags=re.IGNORECASE,
                )
            )
            if keyword_count >= 2 or (unit_count >= 2 and formula_count >= 1):
                selected.append(normalized)
                seen.add(normalized)
            if sum(len(item) for item in selected) >= max_chars:
                break
        return "\n".join(selected)[:max_chars]

    def _split_candidate_sentences(self, text: str) -> List[str]:
        normalized = self._normalize_pdf_text(text)
        return [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?。；;])\s+", normalized)
            if sentence.strip()
        ]

    def _is_experiment_sentence(self, sentence: str) -> bool:
        lowered = sentence.lower()
        keyword_hit = any(keyword in lowered for keyword in EXPERIMENT_KEYWORDS)
        unit_hit = bool(EXPERIMENT_UNIT_RE.search(sentence))
        formula_hit = bool(re.search(r"\bK\d*Fe|Fe\(|Fe\[|CN\)|KNO3|KCl|KOH|HCl|NaOH", sentence))
        return keyword_hit or (unit_hit and formula_hit)

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

        bm25_boosts = self._bm25_boosts(normalized_queries)
        scored: List[Tuple[float, SearchHit, str | None]] = []
        for index, record in enumerate(self._records):
            score, matched_terms = self._score_record(record, normalized_queries)
            if bm25_boosts is not None:
                score += bm25_boosts[index]
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
                        experiment_details=record.get("experiment_details", ""),
                        steps=list(record["steps"]),
                        performance=list(record["performance"]),
                        matched_terms=matched_terms,
                        scientific_payload_digest=(
                            record.get("scientific_payload_digest") or ""
                        ),
                    ),
                    record.get("scientific_payload_digest"),
                )
            )

        scored.sort(key=lambda item: (-item[0], item[1].title))
        ranked_hits: List[SearchHit] = []
        seen_json_content: set[str] = set()
        for _, hit, content_digest in scored:
            if content_digest is not None:
                if content_digest in seen_json_content:
                    continue
                seen_json_content.add(content_digest)
            ranked_hits.append(hit)
        return ranked_hits[:top_k]

    def _bm25_boosts(self, queries: Sequence[str]) -> List[float] | None:
        """Optional BM25 boost on top of the deterministic keyword scorer.

        Returns None (scoring unchanged) when rank-bm25 is not installed.
        """
        try:
            from ..memory.scoring import build_bm25_boosts
        except ImportError:  # pragma: no cover - defensive
            return None
        return build_bm25_boosts(
            queries,
            [str(record.get("search_text", "")) for record in self._records],
        )

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
                        f"- 实验相关全文摘要: {hit.experiment_details or '未提供'}",
                        f"- 关键步骤预览: {step_preview or '未提供'}",
                        f"- 性能预览: {perf_preview or '未提供'}",
                    ]
                )
            )
        return "\n\n".join(blocks)

    def _tokenize(self, text: str) -> List[str]:
        return [token for token in TOKEN_RE.findall(text) if token.strip()]

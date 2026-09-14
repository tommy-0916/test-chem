"""Knowledge ingestion utilities for the research agent.

The research layer reads local JSON/PDF files through LocalExperimentCorpus.
This module adds a small ingestion layer that converts local files and external
paper metadata into the same structured JSON schema.
"""

from __future__ import annotations

import html
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

import fcntl

try:
    from ..memory import LayeredChemMemory
except ImportError:  # unittest discovery with reaserch_agent as start directory
    from reaserch_agent.memory import LayeredChemMemory
from .corpus_search import LocalExperimentCorpus
from .paper_download import (
    OpenAccessPdfDownloader,
    PdfDownloadAttempt,
)


SUPPORTED_LOCAL_SUFFIXES = {".json", ".pdf", ".txt", ".md"}
DEFAULT_USER_AGENT = "chemagent-knowledge-ingestion/0.1"

DOI_REFERENCE_PATTERN = re.compile(
    r"^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$",
    re.IGNORECASE,
)
ARXIV_REFERENCE_PATTERN = re.compile(
    r"^(?:arxiv\s*:\s*)?(\d{4}\.\d{4,5})(v\d+)?$",
    re.IGNORECASE,
)
ARXIV_URL_PATTERN = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(v\d+)?",
    re.IGNORECASE,
)


def classify_reference(raw: str) -> Dict[str, Any]:
    """Classify one user-supplied reference into a structured entry.

    Kinds: local_file / local_dir (existing paths), doi, arxiv, title.
    Resolution of doi/arxiv/title into papers is the online ingestion
    layer's job; this helper only decides what the reference *is*.
    """
    text = str(raw or "").strip()
    entry: Dict[str, Any] = {"raw": text}
    if not text:
        entry["kind"] = "empty"
        return entry

    candidate = Path(text).expanduser()
    if candidate.exists():
        entry["kind"] = "local_dir" if candidate.is_dir() else "local_file"
        entry["path"] = str(candidate.resolve())
        return entry

    doi_match = DOI_REFERENCE_PATTERN.match(text)
    if doi_match:
        entry["kind"] = "doi"
        entry["doi"] = doi_match.group(1)
        return entry

    arxiv_match = ARXIV_REFERENCE_PATTERN.match(text) or ARXIV_URL_PATTERN.search(text)
    if arxiv_match:
        entry["kind"] = "arxiv"
        entry["arxiv_id"] = arxiv_match.group(1) + (arxiv_match.group(2) or "")
        return entry

    entry["kind"] = "title"
    return entry


@dataclass
class ExternalPaper:
    """Metadata returned by an external scholarly source."""

    title: str
    abstract: str = ""
    source: str = ""
    source_id: str = ""
    url: str = ""
    pdf_url: str = ""
    doi: str = ""
    year: str = ""
    authors: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    arxiv_id: str = ""
    venue: str = ""
    citation_count: int = 0


class KnowledgeIngestion:
    """Convert local or external paper sources into corpus JSON records."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        memory_root: str | Path | None = None,
        add_to_memory: bool = False,
        dry_run: bool = False,
        pdf_downloader: Optional[OpenAccessPdfDownloader] = None,
    ) -> None:
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.add_to_memory = add_to_memory
        self.dry_run = dry_run
        self._extractor = LocalExperimentCorpus(corpus_dir=self.output_dir)
        self.pdf_downloader = pdf_downloader or OpenAccessPdfDownloader()
        self.last_external_results: List[Dict[str, Any]] = []
        self._memory = (
            LayeredChemMemory(root_dir=memory_root) if add_to_memory else None
        )

    def ingest_path(
        self,
        path: str | Path,
        *,
        recursive: bool = True,
    ) -> List[Path]:
        """Ingest one file or all supported files under a directory."""
        root = Path(path).expanduser().resolve()
        if not root.exists():
            raise FileNotFoundError(f"input path not found: {root}")

        paths = [root] if root.is_file() else self._iter_files(root, recursive=recursive)
        written: List[Path] = []
        for source_path in paths:
            record = self.record_from_file(source_path)
            if record is None:
                continue
            written.append(self.write_record(record))
            self._remember_record(record)
        return written

    def ingest_external_papers(
        self,
        papers: Sequence[ExternalPaper],
        *,
        download_pdfs: bool = False,
        pdf_dir: str | Path | None = None,
    ) -> List[Path]:
        """Ingest paper metadata, optionally downloading open PDFs first."""
        written: List[Path] = []
        self.last_external_results = []
        resolved_pdf_dir = Path(pdf_dir).expanduser().resolve() if pdf_dir else self.output_dir
        for paper in papers:
            metadata_record = self.record_from_external_paper(paper)
            record = metadata_record
            full_text_status = "metadata_only"
            download_payload: Dict[str, Any] = {}
            if download_pdfs and self.dry_run:
                full_text_status = "download_planned"
                download_payload = {
                    "downloaded": False,
                    "path": "",
                    "provider": "",
                    "url": "",
                    "attempts": [],
                    "errors": [],
                    "status": "dry_run",
                }
            elif download_pdfs:
                validated_pdf_record: Optional[Dict[str, Any]] = None

                def validate_pdf(path: Path) -> bool:
                    nonlocal validated_pdf_record
                    candidate_record = self.record_from_file(path)
                    if candidate_record is None:
                        raise ValueError("PDF contained no meaningful extractable text")
                    validated_pdf_record = candidate_record
                    return True

                download_result = self.pdf_downloader.download(
                    paper,
                    resolved_pdf_dir,
                    candidate_validator=validate_pdf,
                )
                if download_result.path is not None:
                    try:
                        pdf_record = validated_pdf_record or self.record_from_file(
                            download_result.path
                        )
                        if pdf_record is not None:
                            pdf_record["_ingestion_metadata"]["external_metadata"] = dict(
                                metadata_record["_ingestion_metadata"]
                            )
                            record = pdf_record
                            full_text_status = "parsed"
                        else:
                            full_text_status = "parse_failed"
                            download_result.attempts.append(
                                PdfDownloadAttempt(
                                    provider="pdf_parser",
                                    status="parse_failed",
                                    error="PDF contained no meaningful extractable text",
                                )
                            )
                    except Exception as exc:
                        full_text_status = "parse_failed"
                        download_result.attempts.append(
                            PdfDownloadAttempt(
                                provider="pdf_parser",
                                status="parse_failed",
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        )
                else:
                    full_text_status = (
                        "parse_failed"
                        if any(
                            attempt.status == "parse_failed"
                            for attempt in download_result.attempts
                        )
                        else "download_failed"
                    )
                download_payload = download_result.to_dict()

            ingestion_metadata = record.setdefault("_ingestion_metadata", {})
            ingestion_metadata["full_text_status"] = full_text_status
            if download_payload:
                ingestion_metadata["pdf_download"] = download_payload
            output_path = self.write_record(record)
            written.append(output_path)
            self._remember_record(record)
            self.last_external_results.append(
                {
                    "title": paper.title,
                    "record_path": str(output_path),
                    "full_text_status": full_text_status,
                    "pdf_download": download_payload,
                }
            )
        return written

    def record_from_file(self, path: str | Path) -> Dict[str, Any] | None:
        source_path = Path(path).expanduser().resolve()
        suffix = source_path.suffix.lower()
        if suffix not in SUPPORTED_LOCAL_SUFFIXES:
            return None
        if suffix == ".json":
            return self._record_from_json(source_path)
        if suffix == ".pdf":
            text = self._extractor._extract_pdf_text(source_path)
            if not self._has_meaningful_pdf_text(text):
                return None
            title = self._title_from_pdf(source_path, text)
            source_type = "pdf"
        else:
            text = source_path.read_text(encoding="utf-8")
            title = self._title_from_text(source_path, text)
            source_type = suffix.lstrip(".")
        return self.record_from_text(
            title=title,
            text=text,
            source_path=str(source_path),
            source_type=source_type,
        )

    @staticmethod
    def _has_meaningful_pdf_text(text: str) -> bool:
        without_page_markers = re.sub(r"\[p\.\d+\]", " ", text or "")
        normalized = re.sub(r"\s+", " ", without_page_markers).strip()
        tokens = re.findall(r"[0-9A-Za-z一-鿿]{2,}", normalized)
        return len(normalized) >= 80 and len(tokens) >= 10

    def record_from_external_paper(self, paper: ExternalPaper) -> Dict[str, Any]:
        text = "\n\n".join(
            part
            for part in [
                paper.title,
                paper.abstract,
                " ".join(paper.authors),
            ]
            if part
        )
        record = self.record_from_text(
            title=paper.title or paper.source_id or "untitled paper",
            text=text,
            source_path=paper.url or paper.pdf_url or paper.source_id,
            source_type=paper.source or "external",
        )
        record["_ingestion_metadata"].update(
            {
                "source": paper.source,
                "source_id": paper.source_id,
                "url": paper.url,
                "pdf_url": paper.pdf_url,
                "doi": paper.doi,
                "year": paper.year,
                "authors": paper.authors,
                "arxiv_id": paper.arxiv_id,
                "venue": paper.venue,
                "citation_count": paper.citation_count,
            }
        )
        return record

    def record_from_text(
        self,
        *,
        title: str,
        text: str,
        source_path: str,
        source_type: str,
    ) -> Dict[str, Any]:
        """Build the JSON schema consumed by LocalExperimentCorpus."""
        normalized = self._extractor._normalize_pdf_text(text or "")
        problem = self._problem_summary(normalized, title)
        experiment_details = self._extractor._extract_experiment_details(normalized)
        if not experiment_details:
            experiment_details = self._extractor._summarize_pdf_text(normalized)
        steps = self._steps_from_text(experiment_details or normalized)
        performance = self._performance_from_text(normalized)
        return {
            "文献题目": title.strip() or "untitled paper",
            "1. 解决的问题": problem,
            "2. 具体的合成步骤": {
                "描述性总结": experiment_details,
                "参数列表": steps,
            },
            "3. 性能": performance,
            "_ingestion_metadata": {
                "source_path": source_path,
                "source_type": source_type,
                "ingested_at": self._timestamp(),
                "parser": "reaserch_agent.tools.ingestion.KnowledgeIngestion",
            },
        }

    def write_record(self, record: Dict[str, Any]) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        title = str(record.get("文献题目", "paper")).strip() or "paper"
        base_path = self.output_dir / self._record_filename(title, record)
        if self.dry_run:
            return self._dedupe_path(base_path)
        payload = json.dumps(record, ensure_ascii=False, indent=2)
        with self._output_lock():
            output_path = self._dedupe_path(base_path)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{output_path.stem}.",
                suffix=".tmp",
                dir=self.output_dir,
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, output_path)
            finally:
                temp_path.unlink(missing_ok=True)
            return output_path

    def _record_filename(self, title: str, record: Dict[str, Any]) -> str:
        metadata = record.get("_ingestion_metadata") or {}
        identity = {
            key: metadata.get(key)
            for key in (
                "doi",
                "arxiv_id",
                "source",
                "source_id",
                "source_path",
                "year",
                "authors",
            )
            if metadata.get(key)
        }
        suffix = ""
        if identity:
            canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True)
            suffix = "_" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:10]
        return f"{self._slugify(title)}{suffix}.json"

    @contextmanager
    def _output_lock(self) -> Iterator[None]:
        lock_path = self.output_dir / ".records.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _record_from_json(self, path: Path) -> Dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON record: {path}: {exc}") from exc
        if not isinstance(data, dict):
            return None

        if "文献题目" in data and "2. 具体的合成步骤" in data:
            record = dict(data)
            record.setdefault("1. 解决的问题", "")
            record.setdefault("3. 性能", [])
            synthesis = record.get("2. 具体的合成步骤")
            if not isinstance(synthesis, dict):
                synthesis = {"描述性总结": str(synthesis), "参数列表": []}
            synthesis.setdefault("描述性总结", "")
            synthesis.setdefault("参数列表", [])
            record["2. 具体的合成步骤"] = synthesis
        else:
            title = str(
                data.get("title")
                or data.get("paper_title")
                or data.get("name")
                or path.stem
            )
            text = json.dumps(data, ensure_ascii=False, indent=2)
            record = self.record_from_text(
                title=title,
                text=text,
                source_path=str(path),
                source_type="json",
            )

        metadata = dict(record.get("_ingestion_metadata") or {})
        metadata.update(
            {
                "source_path": str(path),
                "source_type": "json",
                "ingested_at": metadata.get("ingested_at") or self._timestamp(),
            }
        )
        record["_ingestion_metadata"] = metadata
        return record

    _PAGE_MARKER_RE = re.compile(r"\[p\.(\d+)\]")

    def _page_segments(self, text: str) -> List[tuple[Optional[int], str]]:
        """Split text on [p.N] markers into (page, segment) pairs."""
        parts = self._PAGE_MARKER_RE.split(text or "")
        if len(parts) == 1:
            return [(None, text or "")]
        segments: List[tuple[Optional[int], str]] = []
        if parts[0].strip():
            segments.append((None, parts[0]))
        for index in range(1, len(parts) - 1, 2):
            segments.append((int(parts[index]), parts[index + 1]))
        return segments

    def _steps_from_text(self, text: str, max_steps: int = 12) -> List[Dict[str, Any]]:
        selected: List[tuple[Optional[int], str]] = []
        seen = set()
        for page, segment in self._page_segments(text):
            for sentence in self._extractor._split_candidate_sentences(segment):
                normalized = self._extractor._normalize_text(
                    self._PAGE_MARKER_RE.sub(" ", sentence)
                )
                if len(normalized) < 25 or normalized in seen:
                    continue
                if self._extractor._is_experiment_sentence(normalized):
                    selected.append((page, normalized))
                    seen.add(normalized)
                if len(selected) >= max_steps:
                    break
            if len(selected) >= max_steps:
                break

        steps: List[Dict[str, Any]] = []
        for index, (page, sentence) in enumerate(selected, start=1):
            step: Dict[str, Any] = {
                "步骤序号": index,
                "操作": self._infer_operation(sentence),
                "试剂/对象": self._infer_objects(sentence),
                "参数": self._infer_parameters(sentence),
                "evidence": sentence[:500],
            }
            if page is not None:
                step["page"] = page
            steps.append(step)
        return steps

    def _performance_from_text(self, text: str, max_items: int = 8) -> List[Dict[str, Any]]:
        patterns = [
            ("比容量", r"\d+(?:\.\d+)?\s*mAh\s*g\s*[-−]?\s*1"),
            ("电流密度", r"\d+(?:\.\d+)?\s*mA\s*g\s*[-−]?\s*1"),
            ("扫描速率", r"\d+(?:\.\d+)?\s*mV\s*s\s*[-−]?\s*1"),
            ("容量保持率", r"\d+(?:\.\d+)?\s*%"),
            ("过电位/电压", r"\d+(?:\.\d+)?\s*(?:mV|V)"),
            ("循环次数", r"\d+(?:,\d{3})*\s*cycles?"),
        ]
        items: List[Dict[str, Any]] = []
        seen = set()
        for sentence in self._extractor._split_candidate_sentences(text):
            lowered = sentence.lower()
            if not any(
                keyword in lowered
                for keyword in [
                    "capacity",
                    "retention",
                    "cycle",
                    "current density",
                    "overpotential",
                    "mAh",
                    "mA",
                    "容量",
                    "保持率",
                    "循环",
                    "过电位",
                ]
            ):
                continue
            for label, pattern in patterns:
                for match in re.findall(pattern, sentence, flags=re.IGNORECASE):
                    key = (label, match)
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(
                        {
                            "序号": len(items) + 1,
                            "指标": label,
                            "数值": match,
                            "条件": self._extractor._normalize_text(sentence)[:400],
                        }
                    )
                    if len(items) >= max_items:
                        return items
        return items

    def _problem_summary(self, text: str, title: str) -> str:
        summary = self._extractor._summarize_pdf_text(text, max_chars=1600)
        if summary:
            return summary
        return f"本条目来自 {title}，ingestion 阶段未抽取到摘要。"

    def _infer_operation(self, sentence: str) -> str:
        lowered = sentence.lower()
        if any(token in lowered for token in ["xrd", "pxrd", "sem", "tem", "xps", "ftir"]):
            return "表征/测试"
        if any(token in lowered for token in ["washed", "washing", "洗涤"]):
            return "洗涤与纯化"
        if any(token in lowered for token in ["dried", "drying", "干燥"]):
            return "干燥处理"
        if any(token in lowered for token in ["centrifug", "filter", "separat", "离心", "过滤"]):
            return "固液分离"
        if any(token in lowered for token in ["stir", "mixed", "aged", "coprecipitation", "搅拌", "混合", "陈化"]):
            return "混合/反应"
        if any(token in lowered for token in ["solution", "dissolv", "溶液", "溶解", "配制"]):
            return "配制溶液"
        if any(token in lowered for token in ["electrode", "slurry", "电极", "浆料"]):
            return "电极制备"
        return "论文实验步骤"

    def _infer_objects(self, sentence: str) -> str:
        candidates = re.findall(
            r"(?:[A-Z][A-Za-z0-9\[\]\(\)·\.\-]+(?:\s*[+/]\s*[A-Z][A-Za-z0-9\[\]\(\)·\.\-]+)*)",
            sentence,
        )
        cleaned = []
        for candidate in candidates:
            if len(candidate) <= 1 or candidate.lower() in {"the", "this", "after"}:
                continue
            cleaned.append(candidate)
        if cleaned:
            return ", ".join(cleaned[:6])
        if "普鲁士蓝" in sentence or "PBA" in sentence:
            return "PBA 样品"
        return "文献实验对象"

    def _infer_parameters(self, sentence: str) -> str:
        unit_matches = re.findall(
            r"\d+(?:\.\d+)?\s*(?:mmol|mol|mg|g|mL|L|M|h|min|s|°C|℃|C|rpm|V|mV|mA|A|Å|nm|μm|um|%)",
            sentence,
            flags=re.IGNORECASE,
        )
        condition_terms = [
            term
            for term in [
                "room temperature",
                "overnight",
                "dropwise",
                "stirring",
                "aged",
                "vacuum",
                "室温",
                "过夜",
                "滴加",
                "搅拌",
                "陈化",
                "真空",
            ]
            if term.lower() in sentence.lower()
        ]
        if unit_matches or condition_terms:
            return self._extractor._normalize_text(sentence)[:500]
        return "文献未说明"

    def _title_from_pdf(self, path: Path, text: str) -> str:
        try:
            import fitz

            document = fitz.open(str(path))
            metadata_title = (document.metadata or {}).get("title", "")
            document.close()
            if metadata_title and len(metadata_title.strip()) > 5:
                return self._extractor._normalize_text(metadata_title)
        except Exception:
            pass
        return self._title_from_text(path, text)

    def _title_from_text(self, path: Path, text: str) -> str:
        for line in (text or "").splitlines()[:20]:
            candidate = self._extractor._normalize_text(line)
            if 10 <= len(candidate) <= 220 and not candidate.lower().startswith("abstract"):
                return candidate
        return path.stem

    def _iter_files(self, root: Path, *, recursive: bool) -> List[Path]:
        iterator = root.rglob("*") if recursive else root.iterdir()
        return sorted(
            path
            for path in iterator
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in SUPPORTED_LOCAL_SUFFIXES
        )

    def _download_pdf(self, paper: ExternalPaper, output_dir: Path) -> Path | None:
        """Compatibility wrapper around the redundant OA downloader."""
        if self.dry_run:
            return output_dir / f"{self._slugify(paper.title)}.pdf"
        return self.pdf_downloader.download(paper, output_dir).path

    def _remember_record(self, record: Dict[str, Any]) -> None:
        if self._memory is None or self.dry_run:
            return
        synthesis = record.get("2. 具体的合成步骤", {}) or {}
        self._memory.add_literature_protocol(
            title=str(record.get("文献题目", "")),
            protocol_summary=str(synthesis.get("描述性总结", "")),
            source_file=str((record.get("_ingestion_metadata") or {}).get("source_path", "")),
            steps=list(synthesis.get("参数列表", []) or []),
            performance=list(record.get("3. 性能", []) or []),
            metadata=dict(record.get("_ingestion_metadata") or {}),
        )

    def _dedupe_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        for index in range(2, 1000):
            candidate = path.with_name(f"{stem}_{index}{suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"could not allocate unique output path for {path}")

    def _slugify(self, text: str, max_length: int = 96) -> str:
        cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text.strip(), flags=re.UNICODE)
        cleaned = re.sub(r"_+", "_", cleaned).strip("._")
        return (cleaned or "paper")[:max_length]

    def _timestamp(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class ExternalKnowledgeClient:
    """Small stdlib client for scholarly metadata APIs."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        semantic_scholar_api_key: str = "",
        crossref_mailto: str = "",
        timeout_seconds: int = 30,
    ) -> None:
        self.user_agent = user_agent
        self.semantic_scholar_api_key = (
            semantic_scholar_api_key
            or os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
            or os.getenv("S2_API_KEY", "")
        )
        self.crossref_mailto = crossref_mailto or os.getenv("CROSSREF_MAILTO", "")
        self.timeout_seconds = timeout_seconds
        self.last_errors: List[str] = []
        self.last_attempts: List[Dict[str, Any]] = []

    def search(
        self,
        query: str,
        *,
        sources: Sequence[str],
        max_results: int = 5,
    ) -> List[ExternalPaper]:
        source_batches: List[List[ExternalPaper]] = []
        self.last_errors = []
        self.last_attempts = []
        for source in sources:
            normalized = source.strip().lower()
            try:
                if normalized == "arxiv":
                    source_papers = self.search_arxiv(query, max_results=max_results)
                elif normalized == "crossref":
                    source_papers = self.search_crossref(query, max_results=max_results)
                elif normalized in {"semantic_scholar", "semanticscholar", "s2"}:
                    source_papers = self.search_semantic_scholar(
                        query, max_results=max_results
                    )
                elif normalized == "openalex":
                    source_papers = self.search_openalex(query, max_results=max_results)
                elif normalized == "pubmed":
                    source_papers = self.search_pubmed(query, max_results=max_results)
                elif normalized in {"google_scholar", "scholar"}:
                    source_papers = self.search_google_scholar(
                        query, max_results=max_results
                    )
                else:
                    raise ValueError(f"unsupported external source: {source}")
                source_batches.append(source_papers)
                self.last_attempts.append(
                    {
                        "source": normalized,
                        "status": "success" if source_papers else "empty",
                        "result_count": len(source_papers),
                        "error": "",
                    }
                )
            except Exception as exc:
                source_batches.append([])
                error = f"{type(exc).__name__}: {exc}"
                self.last_errors.append(f"{source}: {error}")
                self.last_attempts.append(
                    {
                        "source": normalized,
                        "status": "error",
                        "result_count": 0,
                        "error": error,
                    }
                )
        interleaved: List[ExternalPaper] = []
        largest_batch = max((len(batch) for batch in source_batches), default=0)
        for index in range(largest_batch):
            for batch in source_batches:
                if index < len(batch):
                    interleaved.append(batch[index])
        return self._dedupe_papers(interleaved)

    def search_arxiv(self, query: str, *, max_results: int = 5) -> List[ExternalPaper]:
        params = urllib.parse.urlencode(
            {
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": max_results,
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
        )
        url = f"https://export.arxiv.org/api/query?{params}"
        root = ET.fromstring(self._get_text(url))
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        return [
            self._paper_from_arxiv_entry(entry, ns)
            for entry in root.findall("atom:entry", ns)
        ]

    def _paper_from_arxiv_entry(
        self,
        entry: ET.Element,
        ns: Dict[str, str],
    ) -> ExternalPaper:
        title = self._xml_text(entry, "atom:title", ns)
        abstract = self._xml_text(entry, "atom:summary", ns)
        source_id = self._xml_text(entry, "atom:id", ns)
        published = self._xml_text(entry, "atom:published", ns)
        authors = [
            self._xml_text(author, "atom:name", ns)
            for author in entry.findall("atom:author", ns)
        ]
        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href", "")
        return ExternalPaper(
            title=self._clean_text(title),
            abstract=self._clean_text(abstract),
            source="arxiv",
            source_id=source_id.rsplit("/", 1)[-1],
            url=source_id,
            pdf_url=pdf_url,
            arxiv_id=source_id.rsplit("/", 1)[-1],
            year=published[:4],
            authors=[author for author in authors if author],
        )

    def search_crossref(self, query: str, *, max_results: int = 5) -> List[ExternalPaper]:
        params = {
            "query.bibliographic": query,
            "rows": str(max_results),
        }
        if self.crossref_mailto:
            params["mailto"] = self.crossref_mailto
        url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
        payload = json.loads(self._get_text(url))
        items = ((payload.get("message") or {}).get("items") or [])[:max_results]
        return [self._paper_from_crossref_item(item) for item in items]

    def _paper_from_crossref_item(self, item: Dict[str, Any]) -> ExternalPaper:
        title = self._first_text(item.get("title")) or item.get("DOI", "")
        abstract = self._strip_html(str(item.get("abstract", "")))
        authors = [
            " ".join(
                part
                for part in [author.get("given", ""), author.get("family", "")]
                if part
            ).strip()
            for author in item.get("author", []) or []
            if isinstance(author, dict)
        ]
        links = [link for link in item.get("link", []) or [] if isinstance(link, dict)]
        pdf_url = next(
            (
                str(link.get("URL") or "")
                for link in links
                if "pdf" in str(link.get("content-type") or "").lower()
                and link.get("URL")
            ),
            "",
        )
        return ExternalPaper(
            title=self._clean_text(title),
            abstract=self._clean_text(abstract),
            source="crossref",
            source_id=str(item.get("DOI", "")),
            url=str(item.get("URL", "")),
            pdf_url=pdf_url,
            doi=str(item.get("DOI", "")),
            year=self._crossref_year(item),
            authors=[author for author in authors if author],
            raw={
                "container_title": self._first_text(item.get("container-title")),
                "links": links,
            },
            venue=self._first_text(item.get("container-title")),
            citation_count=int(item.get("is-referenced-by-count") or 0),
        )

    def search_semantic_scholar(
        self,
        query: str,
        *,
        max_results: int = 5,
    ) -> List[ExternalPaper]:
        params = urllib.parse.urlencode(
            {
                "query": query,
                "limit": max_results,
                "fields": (
                    "paperId,title,abstract,year,url,authors,externalIds,"
                    "openAccessPdf,venue,citationCount"
                ),
            }
        )
        url = f"https://api.semanticscholar.org/graph/v1/paper/search?{params}"
        payload = json.loads(self._get_s2_text(url))
        return [
            self._paper_from_s2_item(item)
            for item in payload.get("data", []) or []
            if isinstance(item, dict)
        ]

    def _paper_from_s2_item(self, item: Dict[str, Any]) -> ExternalPaper:
        external_ids = item.get("externalIds") or {}
        open_pdf = item.get("openAccessPdf") or {}
        return ExternalPaper(
            title=self._clean_text(str(item.get("title", ""))),
            abstract=self._clean_text(str(item.get("abstract", "") or "")),
            source="semantic_scholar",
            source_id=str(item.get("paperId", "")),
            url=str(item.get("url", "")),
            pdf_url=str(open_pdf.get("url", "")) if isinstance(open_pdf, dict) else "",
            doi=str(external_ids.get("DOI", "")) if isinstance(external_ids, dict) else "",
            year=str(item.get("year", "") or ""),
            authors=[
                str(author.get("name", "")).strip()
                for author in item.get("authors", []) or []
                if isinstance(author, dict) and author.get("name")
            ],
            raw={"external_ids": external_ids},
            arxiv_id=(
                str(external_ids.get("ArXiv", ""))
                if isinstance(external_ids, dict)
                else ""
            ),
            venue=str(item.get("venue", "") or ""),
            citation_count=int(item.get("citationCount") or 0),
        )

    # ------------------------------------------------------------------
    # single-paper lookups (seed reference resolution)
    # ------------------------------------------------------------------

    def search_openalex(self, query: str, *, max_results: int = 5) -> List[ExternalPaper]:
        """OpenAlex works search (open metadata, no key; mailto recommended)."""
        params: Dict[str, str] = {
            "search": query,
            "per_page": str(max_results),
            "select": (
                "id,title,display_name,publication_year,doi,ids,"
                "abstract_inverted_index,authorships,primary_location,open_access"
                ",cited_by_count"
            ),
        }
        mailto = os.getenv("OPENALEX_MAILTO", "") or self.crossref_mailto
        if mailto:
            params["mailto"] = mailto
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        payload = json.loads(self._get_text(url))
        papers: List[ExternalPaper] = []
        for item in payload.get("results", []) or []:
            if not isinstance(item, dict):
                continue
            papers.append(self._paper_from_openalex_work(item))
        return papers

    def _paper_from_openalex_work(self, item: Dict[str, Any]) -> ExternalPaper:
        ids = item.get("ids") or {}
        raw_doi = str(item.get("doi") or ids.get("doi") or "")
        doi = raw_doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
        open_access = item.get("open_access") or {}
        primary_location = item.get("primary_location") or {}
        pdf_url = str(
            primary_location.get("pdf_url")
            or open_access.get("oa_url")
            or ""
        )
        authors = [
            str(((authorship.get("author") or {}).get("display_name")) or "").strip()
            for authorship in item.get("authorships", []) or []
            if isinstance(authorship, dict)
        ]
        raw_arxiv = str(ids.get("arxiv") or "")
        arxiv_match = ARXIV_URL_PATTERN.search(raw_arxiv)
        arxiv_id = arxiv_match.group(1) + (arxiv_match.group(2) or "") if arxiv_match else ""
        source_info = primary_location.get("source") or {}
        return ExternalPaper(
            title=self._clean_text(
                str(item.get("title") or item.get("display_name") or "")
            ),
            abstract=self._reconstruct_openalex_abstract(
                item.get("abstract_inverted_index")
            ),
            source="openalex",
            source_id=str(ids.get("openalex") or item.get("id") or ""),
            url=str(item.get("id") or ""),
            pdf_url=pdf_url,
            doi=doi,
            year=str(item.get("publication_year") or ""),
            authors=[author for author in authors if author],
            arxiv_id=arxiv_id,
            venue=str(source_info.get("display_name") or ""),
            citation_count=int(item.get("cited_by_count") or 0),
        )

    @staticmethod
    def _reconstruct_openalex_abstract(inverted_index: Any) -> str:
        """OpenAlex stores abstracts as {word: [positions]}; rebuild the text."""
        if not isinstance(inverted_index, dict) or not inverted_index:
            return ""
        positioned: List[tuple[int, str]] = []
        for word, positions in inverted_index.items():
            if not isinstance(positions, list):
                continue
            for position in positions:
                if isinstance(position, int):
                    positioned.append((position, str(word)))
        positioned.sort(key=lambda pair: pair[0])
        return " ".join(word for _, word in positioned)

    def search_pubmed(self, query: str, *, max_results: int = 5) -> List[ExternalPaper]:
        """PubMed via NCBI eutils (esearch → esummary)."""
        api_key = os.getenv("NCBI_API_KEY", "")
        search_params: Dict[str, str] = {
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "retmax": str(max_results),
        }
        if api_key:
            search_params["api_key"] = api_key
        search_payload = json.loads(
            self._get_text(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
                + urllib.parse.urlencode(search_params)
            )
        )
        id_list = ((search_payload.get("esearchresult") or {}).get("idlist")) or []
        if not id_list:
            return []
        summary_params: Dict[str, str] = {
            "db": "pubmed",
            "id": ",".join(str(pmid) for pmid in id_list[:max_results]),
            "retmode": "json",
        }
        if api_key:
            summary_params["api_key"] = api_key
        summary_payload = json.loads(
            self._get_text(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
                + urllib.parse.urlencode(summary_params)
            )
        )
        result_block = summary_payload.get("result") or {}
        papers: List[ExternalPaper] = []
        for pmid in id_list[:max_results]:
            item = result_block.get(str(pmid))
            if not isinstance(item, dict):
                continue
            doi = ""
            for article_id in item.get("articleids", []) or []:
                if isinstance(article_id, dict) and article_id.get("idtype") == "doi":
                    doi = str(article_id.get("value", "")).strip()
                    break
            papers.append(
                ExternalPaper(
                    title=self._clean_text(str(item.get("title", ""))),
                    abstract="",
                    source="pubmed",
                    source_id=str(pmid),
                    url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    doi=doi,
                    year=str(item.get("pubdate", ""))[:4],
                    authors=[
                        str(author.get("name", "")).strip()
                        for author in item.get("authors", []) or []
                        if isinstance(author, dict) and author.get("name")
                    ],
                )
            )
        return papers

    def search_google_scholar(
        self,
        query: str,
        *,
        max_results: int = 5,
    ) -> List[ExternalPaper]:
        """Google Scholar leads via Serper /scholar (high recall, unverified).

        Scholar hits carry no DOI, so they enter the pipeline as unverified
        candidates until Crossref/arXiv confirms their identity.
        """
        from .web_search import WebSearchClient

        web_client = WebSearchClient()
        leads = web_client.search_scholar(query, max_results=max_results)
        papers: List[ExternalPaper] = []
        for lead in leads:
            papers.append(
                ExternalPaper(
                    title=self._clean_text(lead.get("title", "")),
                    abstract=self._clean_text(lead.get("snippet", "")),
                    source="google_scholar",
                    source_id=lead.get("url", ""),
                    url=lead.get("url", ""),
                    pdf_url=lead.get("pdf_url", ""),
                    year=str(lead.get("year", "")),
                    raw={"publication_info": lead.get("publication_info", "")},
                    venue=str(lead.get("publication_info", "")),
                )
            )
        return papers

    def lookup_doi(self, doi: str) -> Optional[ExternalPaper]:
        """Resolve one DOI through Crossref, S2, then OpenAlex."""
        cleaned = (doi or "").strip()
        if not cleaned:
            return None
        cleaned = re.sub(
            r"^https?://(?:dx\.)?doi\.org/", "", cleaned, flags=re.IGNORECASE
        )
        return self._first_resolved(
            "lookup_doi",
            [
                ("crossref", lambda: self._lookup_crossref_doi(cleaned)),
                (
                    "semantic_scholar",
                    lambda: self._lookup_s2_identifier(f"DOI:{cleaned}"),
                ),
                ("openalex", lambda: self._lookup_openalex_doi(cleaned)),
            ],
        )

    def lookup_arxiv(self, arxiv_id: str) -> Optional[ExternalPaper]:
        """Resolve one arXiv id through arXiv, S2, then OpenAlex."""
        cleaned = (arxiv_id or "").strip()
        cleaned = re.sub(r"^arxiv\s*:\s*", "", cleaned, flags=re.IGNORECASE)
        if not cleaned:
            return None
        normalized = re.sub(r"v\d+$", "", cleaned, flags=re.IGNORECASE)
        return self._first_resolved(
            "lookup_arxiv",
            [
                ("arxiv", lambda: self._lookup_arxiv_atom(cleaned)),
                (
                    "semantic_scholar",
                    lambda: self._lookup_s2_identifier(f"ARXIV:{normalized}"),
                ),
                ("openalex", lambda: self._lookup_openalex_arxiv(normalized)),
            ],
        )

    def _lookup_crossref_doi(self, doi: str) -> Optional[ExternalPaper]:
        url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
        if self.crossref_mailto:
            url += "?" + urllib.parse.urlencode({"mailto": self.crossref_mailto})
        payload = json.loads(self._get_text(url))
        item = payload.get("message")
        if not isinstance(item, dict) or not item:
            return None
        return self._paper_from_crossref_item(item)

    def _lookup_arxiv_atom(self, arxiv_id: str) -> Optional[ExternalPaper]:
        params = urllib.parse.urlencode({"id_list": arxiv_id, "max_results": 1})
        url = f"https://export.arxiv.org/api/query?{params}"
        root = ET.fromstring(self._get_text(url))
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            paper = self._paper_from_arxiv_entry(entry, ns)
            if paper.title:
                return paper
        return None

    def _lookup_s2_identifier(self, identifier: str) -> Optional[ExternalPaper]:
        fields = urllib.parse.urlencode(
            {
                "fields": (
                    "paperId,title,abstract,year,url,authors,externalIds,"
                    "openAccessPdf,venue,citationCount"
                )
            }
        )
        url = (
            "https://api.semanticscholar.org/graph/v1/paper/"
            f"{urllib.parse.quote(identifier, safe=':')}?{fields}"
        )
        item = json.loads(self._get_s2_text(url))
        if not isinstance(item, dict) or not item.get("title"):
            return None
        return self._paper_from_s2_item(item)

    def _lookup_openalex_doi(self, doi: str) -> Optional[ExternalPaper]:
        for candidate in self.search_openalex(doi, max_results=5):
            if candidate.doi.strip().lower() == doi.strip().lower():
                return candidate
        return None

    def _lookup_openalex_arxiv(self, arxiv_id: str) -> Optional[ExternalPaper]:
        wanted = re.sub(r"v\d+$", "", arxiv_id.strip().lower())
        for candidate in self.search_openalex(arxiv_id, max_results=5):
            candidate_id = re.sub(
                r"v\d+$", "", candidate.arxiv_id.strip().lower()
            )
            if candidate_id == wanted:
                return candidate
        return None

    def _first_resolved(
        self,
        operation: str,
        resolvers: Sequence[tuple[str, Any]],
    ) -> Optional[ExternalPaper]:
        for source, resolver in resolvers:
            try:
                paper = resolver()
            except Exception as exc:
                self.last_errors.append(
                    f"{operation}/{source}: {type(exc).__name__}: {exc}"
                )
                continue
            if paper is not None and paper.title:
                return paper
        return None

    def lookup_title(self, title: str) -> Optional[ExternalPaper]:
        """Resolve one paper title across independent scholarly providers."""
        cleaned = (title or "").strip()
        if not cleaned:
            return None
        for search_fn in (
            self.search_semantic_scholar,
            self.search_openalex,
            self.search_crossref,
            self.search_arxiv,
        ):
            try:
                candidates = search_fn(cleaned, max_results=3)
            except Exception as exc:
                self.last_errors.append(
                    f"lookup_title/{search_fn.__name__}: {type(exc).__name__}: {exc}"
                )
                continue
            best = self._best_title_match(cleaned, candidates)
            if best is not None:
                return best
        return None

    @staticmethod
    def _normalized_title(title: str) -> str:
        return re.sub(r"[^0-9a-z一-鿿]+", "", (title or "").lower())

    def _best_title_match(
        self,
        wanted: str,
        candidates: Sequence[ExternalPaper],
    ) -> Optional[ExternalPaper]:
        wanted_normalized = self._normalized_title(wanted)
        if not wanted_normalized:
            return None
        for candidate in candidates:
            candidate_normalized = self._normalized_title(candidate.title)
            if not candidate_normalized:
                continue
            if candidate_normalized == wanted_normalized:
                return candidate
            length_ratio = min(len(wanted_normalized), len(candidate_normalized)) / max(
                len(wanted_normalized), len(candidate_normalized)
            )
            if length_ratio >= 0.8 and (
                wanted_normalized in candidate_normalized
                or candidate_normalized in wanted_normalized
            ):
                return candidate

        wanted_tokens = self._title_tokens(wanted)
        if not wanted_tokens:
            return None
        scored: List[tuple[float, ExternalPaper]] = []
        for candidate in candidates:
            candidate_tokens = self._title_tokens(candidate.title)
            if not candidate_tokens:
                continue
            overlap = len(wanted_tokens & candidate_tokens)
            f1_score = (2 * overlap) / (len(wanted_tokens) + len(candidate_tokens))
            scored.append((f1_score, candidate))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if not scored or scored[0][0] < 0.8:
            return None
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
            return None
        return scored[0][1]

    @staticmethod
    def _title_tokens(title: str) -> set[str]:
        lowered = (title or "").lower()
        tokens = set(re.findall(r"[0-9a-z][0-9a-z+().-]*", lowered))
        for chunk in re.findall(r"[一-鿿]+", lowered):
            if len(chunk) == 1:
                tokens.add(chunk)
            else:
                tokens.update(
                    chunk[index : index + 2] for index in range(len(chunk) - 1)
                )
        return tokens

    # ------------------------------------------------------------------
    # citation snowball (depth-1 references / citations)
    # ------------------------------------------------------------------

    def _s2_paper_identifier(self, paper: ExternalPaper) -> str:
        if paper.source == "semantic_scholar" and paper.source_id:
            return paper.source_id
        if paper.doi:
            return f"DOI:{paper.doi}"
        if paper.arxiv_id:
            return "ARXIV:" + re.sub(r"v\d+$", "", paper.arxiv_id)
        return ""

    def fetch_references(
        self,
        paper: ExternalPaper,
        *,
        limit: int = 20,
    ) -> List[ExternalPaper]:
        return self._fetch_s2_links(paper, "references", "citedPaper", limit)

    def fetch_citations(
        self,
        paper: ExternalPaper,
        *,
        limit: int = 20,
    ) -> List[ExternalPaper]:
        return self._fetch_s2_links(paper, "citations", "citingPaper", limit)

    def _fetch_s2_links(
        self,
        paper: ExternalPaper,
        endpoint: str,
        item_key: str,
        limit: int,
    ) -> List[ExternalPaper]:
        identifier = self._s2_paper_identifier(paper)
        if not identifier:
            return []
        params = urllib.parse.urlencode(
            {
                "fields": (
                    "paperId,title,abstract,year,url,authors,externalIds,"
                    "openAccessPdf,venue,citationCount"
                ),
                "limit": max(1, limit),
            }
        )
        url = (
            "https://api.semanticscholar.org/graph/v1/paper/"
            f"{urllib.parse.quote(identifier, safe=':')}/{endpoint}?{params}"
        )
        payload = json.loads(self._get_s2_text(url))
        papers: List[ExternalPaper] = []
        for row in payload.get("data", []) or []:
            item = row.get(item_key) if isinstance(row, dict) else None
            if isinstance(item, dict) and item.get("title"):
                papers.append(self._paper_from_s2_item(item))
        return self._dedupe_papers(papers)

    def _get_text(self, url: str, *, headers: Optional[Dict[str, str]] = None) -> str:
        resolved_headers = {"User-Agent": self.user_agent}
        resolved_headers.update(headers or {})
        request = urllib.request.Request(url, headers=resolved_headers)
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return response.read().decode("utf-8", errors="replace")

    def _get_s2_text(self, url: str) -> str:
        """Use the configured S2 key, retrying anonymously if it is rejected."""
        headers = {}
        if self.semantic_scholar_api_key:
            headers["x-api-key"] = self.semantic_scholar_api_key
        try:
            return self._get_text(url, headers=headers)
        except urllib.error.HTTPError as exc:
            if not headers or exc.code not in {401, 403}:
                raise
            try:
                payload = self._get_text(url)
            except Exception as fallback_exc:
                raise RuntimeError(
                    f"configured credential returned HTTP {exc.code}; "
                    "anonymous retry failed: "
                    f"{type(fallback_exc).__name__}: {fallback_exc}"
                ) from fallback_exc
            self.last_errors.append(
                f"semantic_scholar auth fallback: configured credential returned "
                f"HTTP {exc.code}; anonymous retry succeeded"
            )
            return payload

    def _xml_text(self, node: ET.Element, path: str, ns: Dict[str, str]) -> str:
        found = node.find(path, ns)
        return found.text.strip() if found is not None and found.text else ""

    def _first_text(self, value: Any) -> str:
        if isinstance(value, list) and value:
            return str(value[0])
        if isinstance(value, str):
            return value
        return ""

    def _strip_html(self, value: str) -> str:
        return self._clean_text(re.sub(r"<[^>]+>", " ", html.unescape(value or "")))

    def _clean_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", html.unescape(value or "")).strip()

    def _crossref_year(self, item: Dict[str, Any]) -> str:
        for key in ["published-print", "published-online"]:
            date_parts = ((item.get(key) or {}).get("date-parts") or [])
            if date_parts and date_parts[0]:
                return str(date_parts[0][0])
        return ""

    def _dedupe_papers(self, papers: Iterable[ExternalPaper]) -> List[ExternalPaper]:
        deduped: List[ExternalPaper] = []
        doi_index: Dict[str, int] = {}
        arxiv_index: Dict[str, int] = {}
        title_index: Dict[str, List[int]] = {}
        for paper in papers:
            doi_key = paper.doi.strip().lower()
            arxiv_key = re.sub(r"v\d+$", "", paper.arxiv_id.strip().lower())
            title_key = self._normalized_title(paper.title)
            index: Optional[int] = doi_index.get(doi_key) if doi_key else None
            if index is None and arxiv_key and arxiv_key in arxiv_index:
                candidate_index = arxiv_index[arxiv_key]
                if not self._paper_identity_conflicts(
                    deduped[candidate_index], paper
                ):
                    index = candidate_index
            if index is None and title_key:
                compatible = [
                    candidate_index
                    for candidate_index in title_index.get(title_key, [])
                    if self._title_fallback_compatible(
                        deduped[candidate_index], paper
                    )
                ]
                if len(compatible) == 1:
                    index = compatible[0]
            if index is None:
                index = len(deduped)
                deduped.append(paper)
            else:
                paper = self._merge_papers(deduped[index], paper)
                deduped[index] = paper

            if paper.doi:
                doi_index[paper.doi.strip().lower()] = index
            if paper.arxiv_id:
                arxiv_index[
                    re.sub(r"v\d+$", "", paper.arxiv_id.strip().lower())
                ] = index
            normalized_title = self._normalized_title(paper.title)
            if normalized_title:
                indexes = title_index.setdefault(normalized_title, [])
                if index not in indexes:
                    indexes.append(index)
        return deduped

    @classmethod
    def _title_fallback_compatible(
        cls,
        left: ExternalPaper,
        right: ExternalPaper,
    ) -> bool:
        if cls._paper_identity_conflicts(left, right):
            return False
        if left.year and right.year and left.year != right.year:
            return False
        left_author = cls._first_author_key(left.authors)
        right_author = cls._first_author_key(right.authors)
        return not (left_author and right_author and left_author != right_author)

    @staticmethod
    def _paper_identity_conflicts(left: ExternalPaper, right: ExternalPaper) -> bool:
        left_doi = left.doi.strip().lower()
        right_doi = right.doi.strip().lower()
        if left_doi and right_doi and left_doi != right_doi:
            return True
        left_arxiv = re.sub(r"v\d+$", "", left.arxiv_id.strip().lower())
        right_arxiv = re.sub(r"v\d+$", "", right.arxiv_id.strip().lower())
        return bool(left_arxiv and right_arxiv and left_arxiv != right_arxiv)

    @staticmethod
    def _first_author_key(authors: Sequence[str]) -> str:
        if not authors:
            return ""
        tokens = re.findall(r"[0-9a-z一-鿿]+", str(authors[0]).lower())
        return tokens[-1] if tokens else ""

    @staticmethod
    def _merge_papers(primary: ExternalPaper, incoming: ExternalPaper) -> ExternalPaper:
        """Merge duplicate provider records without losing provenance."""
        if len(incoming.abstract) > len(primary.abstract):
            primary.abstract = incoming.abstract
        for field_name in (
            "title",
            "source_id",
            "url",
            "pdf_url",
            "doi",
            "year",
            "arxiv_id",
            "venue",
        ):
            if not getattr(primary, field_name) and getattr(incoming, field_name):
                setattr(primary, field_name, getattr(incoming, field_name))
        primary.authors = list(dict.fromkeys(primary.authors + incoming.authors))
        primary.citation_count = max(primary.citation_count, incoming.citation_count)
        discovery_sources = list(primary.raw.get("discovery_sources", []))
        for source in (primary.source, incoming.source):
            if source and source not in discovery_sources:
                discovery_sources.append(source)
        primary.raw.update(incoming.raw)
        primary.raw["discovery_sources"] = discovery_sources
        return primary

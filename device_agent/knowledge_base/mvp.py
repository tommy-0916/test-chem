"""Minimal paper knowledge-base MVP for PreFlow-only retrieval."""

from __future__ import annotations

import json
import pickle
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import fitz
import jieba
from rank_bm25 import BM25Okapi


DEFAULT_RAW_DIR = Path("/workspace/chem_resources/knowledge_base/raw_papers") / "普鲁士蓝"
DEFAULT_OUTPUT_DIR = Path("/workspace/chem_resources/knowledge_base/mvp_prussian_blue_10")
DEFAULT_SELECTION_FILE = Path(__file__).with_name("seed_documents.json")


@dataclass
class SeedDocument:
    filename: str
    title: str = ""
    aliases: List[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class DocumentRecord:
    doc_id: str
    filename: str
    title: str
    aliases: List[str]
    summary: str
    full_text: str
    page_count: int


@dataclass
class ChunkRecord:
    chunk_id: str
    doc_id: str
    filename: str
    title: str
    source: str
    text: str


class KnowledgeBaseBuilder:
    """Offline builder for a very small PDF-backed knowledge base."""

    def __init__(
        self,
        *,
        raw_dir: Path | str = DEFAULT_RAW_DIR,
        output_dir: Path | str = DEFAULT_OUTPUT_DIR,
        selection_file: Path | str = DEFAULT_SELECTION_FILE,
        chunk_size: int = 1100,
        chunk_overlap: int = 180,
        min_chunk_chars: int = 160,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.output_dir = Path(output_dir)
        self.selection_file = Path(selection_file)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_chars = min_chunk_chars

    def load_selection(self) -> List[SeedDocument]:
        payload = json.loads(self.selection_file.read_text(encoding="utf-8"))

        documents_payload = payload.get("documents")
        if documents_payload:
            documents: List[SeedDocument] = []
            for item in documents_payload:
                documents.append(
                    SeedDocument(
                        filename=str(item.get("filename") or "").strip(),
                        title=self._normalize_inline_text(item.get("title") or ""),
                        aliases=[
                            self._normalize_inline_text(alias)
                            for alias in item.get("aliases", [])
                            if self._normalize_inline_text(alias)
                        ],
                        summary=self._normalize_inline_text(item.get("summary") or ""),
                    )
                )
            documents = [doc for doc in documents if doc.filename]
            if not documents:
                raise ValueError(f"No valid documents configured in {self.selection_file}")
            return documents

        filenames = payload.get("filenames") or []
        if not filenames:
            raise ValueError(f"No documents configured in {self.selection_file}")
        return [SeedDocument(filename=str(item).strip()) for item in filenames if str(item).strip()]

    def resolve_documents(self) -> List[Tuple[SeedDocument, Path]]:
        selected = self.load_selection()
        resolved: List[Tuple[SeedDocument, Path]] = []
        missing: List[str] = []
        for seed in selected:
            path = self.raw_dir / seed.filename
            if path.exists():
                resolved.append((seed, path))
            else:
                missing.append(seed.filename)
        if missing:
            raise FileNotFoundError(f"Missing selected source files: {missing}")
        return resolved

    def build(self) -> Dict[str, Any]:
        selected_paths = self.resolve_documents()
        parsed_dir = self.output_dir / "parsed"
        chunks_dir = self.output_dir / "chunks"
        index_dir = self.output_dir / "index"
        parsed_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        index_dir.mkdir(parents=True, exist_ok=True)

        documents: List[DocumentRecord] = []
        chunks: List[ChunkRecord] = []

        for doc_index, (seed, source_path) in enumerate(selected_paths, start=1):
            document, page_texts = self._parse_pdf(source_path, seed, doc_index)
            documents.append(document)
            doc_chunks = self._build_chunks(document, page_texts)
            chunks.extend(doc_chunks)

        documents_by_id = {doc.doc_id: doc for doc in documents}
        tokenized_corpus = [
            self._tokenize(self._build_index_text(chunk, documents_by_id[chunk.doc_id]))
            for chunk in chunks
        ]
        bm25 = BM25Okapi(tokenized_corpus)

        self._write_jsonl(parsed_dir / "documents.jsonl", documents)
        self._write_jsonl(chunks_dir / "chunks.jsonl", chunks)
        with (index_dir / "bm25.pkl").open("wb") as fh:
            pickle.dump(bm25, fh)
        (index_dir / "tokenized_corpus_sizes.json").write_text(
            json.dumps(
                {
                    "chunk_count": len(chunks),
                    "document_count": len(documents),
                    "token_counts": [len(tokens) for tokens in tokenized_corpus],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        build_info = {
            "raw_dir": str(self.raw_dir),
            "output_dir": str(self.output_dir),
            "selection_file": str(self.selection_file),
            "selected_filenames": [path.name for _, path in selected_paths],
            "document_count": len(documents),
            "chunk_count": len(chunks),
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "min_chunk_chars": self.min_chunk_chars,
        }
        (self.output_dir / "build_info.json").write_text(
            json.dumps(build_info, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return build_info

    def _parse_pdf(
        self,
        path: Path,
        seed: SeedDocument,
        doc_index: int,
    ) -> Tuple[DocumentRecord, List[Tuple[int, str]]]:
        with fitz.open(path) as pdf:
            page_texts: List[Tuple[int, str]] = []
            first_page_text = ""
            metadata_title = self._normalize_inline_text(pdf.metadata.get("title") or "")
            for page_number in range(pdf.page_count):
                page = pdf.load_page(page_number)
                text = page.get_text("text")
                normalized = self._normalize_page_text(text)
                if not normalized:
                    continue
                if not first_page_text:
                    first_page_text = normalized
                page_texts.append((page_number + 1, normalized))

            full_text = "\n\n".join(text for _, text in page_texts)
            title = self._extract_title(
                filename=path.name,
                metadata_title=metadata_title,
                first_page_text=first_page_text,
                seed_title=seed.title,
            )
            document = DocumentRecord(
                doc_id=f"paper_{doc_index:04d}",
                filename=path.name,
                title=title,
                aliases=list(seed.aliases),
                summary=seed.summary,
                full_text=full_text,
                page_count=pdf.page_count,
            )
            return document, page_texts

    def _extract_title(
        self,
        *,
        filename: str,
        metadata_title: str,
        first_page_text: str,
        seed_title: str,
    ) -> str:
        if seed_title:
            return seed_title

        if self._is_probably_valid_title(metadata_title):
            return metadata_title

        candidate = self._extract_title_from_page_text(first_page_text)
        if self._is_probably_valid_title(candidate):
            return candidate

        return Path(filename).stem

    def _extract_title_from_page_text(self, first_page_text: str) -> str:
        lines = [self._normalize_inline_text(line) for line in first_page_text.splitlines()]
        lines = [line for line in lines if line]
        banned_exact = {
            "Articles",
            "Article",
            "ARTICLE",
            "Perspective",
            "Communication",
            "Batteries",
            "Abstract",
            "SUMMARY",
            "HIGHLIGHTS",
            "Read Online",
            "ACCESS",
            "Metrics & More",
            "Supporting Information",
        }
        banned_fragments = [
            "doi.org",
            "@",
            "School of",
            "Department of",
            "Received",
            "Accepted",
            "Cite This:",
            "wileyonlinelibrary.com",
            "This journal is",
            "Energy Environ. Sci.",
            "Nature Communications",
        ]
        candidate_lines: List[str] = []
        for line in lines[:20]:
            if line in banned_exact:
                continue
            if any(fragment in line for fragment in banned_fragments):
                continue
            if re.fullmatch(r"[\d\W_]+", line):
                continue
            if len(line) < 12:
                continue
            candidate_lines.append(line)
            joined = " ".join(candidate_lines)
            if len(joined) >= 50:
                break

        candidate = " ".join(candidate_lines).strip()
        candidate = re.sub(r"\s{2,}", " ", candidate)
        candidate = candidate.replace(" - ", "-")
        return candidate

    def _is_probably_valid_title(self, title: str) -> bool:
        candidate = self._normalize_inline_text(title)
        if not candidate:
            return False
        if len(candidate) < 12 or len(candidate) > 220:
            return False
        if "doi.org" in candidate.lower():
            return False
        if re.search(r"\b\d+\.\.\d+\b", candidate):
            return False
        if re.fullmatch(r"[A-Za-z0-9_. -]+", candidate) and re.search(r"\b\d{4}\b", candidate):
            return False
        if candidate.lower().startswith(("article", "articles", "communication", "perspective")):
            return False
        return True

    def _build_chunks(
        self,
        document: DocumentRecord,
        page_texts: Sequence[Tuple[int, str]],
    ) -> List[ChunkRecord]:
        chunks: List[ChunkRecord] = []
        chunk_index = 0
        for page_number, page_text in page_texts:
            if len(page_text) < self.min_chunk_chars:
                continue
            start = 0
            while start < len(page_text):
                end = min(len(page_text), start + self.chunk_size)
                if end < len(page_text):
                    boundary = self._find_boundary(page_text, start, end)
                    if boundary > start + self.min_chunk_chars:
                        end = boundary
                text = page_text[start:end].strip()
                if len(text) >= self.min_chunk_chars:
                    chunk_index += 1
                    chunks.append(
                        ChunkRecord(
                            chunk_id=f"{document.doc_id}_chunk_{chunk_index:03d}",
                            doc_id=document.doc_id,
                            filename=document.filename,
                            title=document.title,
                            source=f"page {page_number}",
                            text=text,
                        )
                    )
                if end >= len(page_text):
                    break
                start = max(end - self.chunk_overlap, start + 1)
        return chunks

    def _build_index_text(self, chunk: ChunkRecord, document: DocumentRecord) -> str:
        searchable_parts = [
            document.title,
            document.summary,
            " ".join(document.aliases),
            Path(document.filename).stem.replace("_", " "),
            chunk.text,
        ]
        return "\n".join(part for part in searchable_parts if part).strip()

    def _find_boundary(self, text: str, start: int, end: int) -> int:
        window = text[start:end]
        candidates = [
            window.rfind(marker)
            for marker in ["。", "；", "：", ".", ";", ":", "\n"]
        ]
        boundary = max(candidates)
        if boundary < 0:
            return end
        return start + boundary + 1

    def _normalize_page_text(self, text: str) -> str:
        cleaned = text.replace("\x00", "")
        cleaned = cleaned.replace("-\n", "")
        cleaned = cleaned.replace("\r", "\n")
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{2,}", "\n", cleaned)
        lines = [self._normalize_inline_text(line) for line in cleaned.splitlines()]
        lines = [line for line in lines if line and not self._should_drop_line(line)]
        return "\n".join(lines).strip()

    def _normalize_inline_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _should_drop_line(self, line: str) -> bool:
        exact_noise = {
            "Articles",
            "Article",
            "ARTICLE",
            "Perspective",
            "Communication",
            "Read Online",
            "ACCESS",
            "Metrics & More",
            "Article Recommendations",
            "Supporting Information",
            "HIGHLIGHTS",
            "*",
            "†",
            "‡",
            "§",
        }
        if line in exact_noise:
            return True

        lowered = line.lower()
        fragment_noise = [
            "https://doi.org/",
            "wileyonlinelibrary.com",
            "this journal is ©",
            "this journal is c",
            "cite this:",
            "read online",
            "metrics & more",
            "article recommendations",
            "supporting information",
            "e-mail:",
            "downloaded by",
            "view article online",
            "terms and conditions",
            "creative commons license",
            "published on",
            "oa articles are governed",
            "acknowledges support",
            "office of energy efficiency",
            "vehicle technologies office",
            "energy & environmental science paper",
            "conflicts of interest",
            "author contributions",
            "data availability",
        ]
        if any(fragment in lowered for fragment in fragment_noise):
            return True

        affiliation_starts = (
            "school of ",
            "department of ",
            "college of ",
            "faculty of ",
            "centre for ",
            "center for ",
            "key laboratory",
            "state key laboratory",
            "institute of ",
            "laboratory of ",
        )
        if lowered.startswith(affiliation_starts):
            return True
        if re.match(r"^\d+\s*(school of|department of|college of|faculty of|centre for|center for|key laboratory|state key laboratory|institute of|laboratory of)\b", lowered):
            return True
        if (
            any(marker in lowered for marker in [" university", " institute", " school of", " department of", " college of", " laboratory", " centre for", " center for"])
            and any(place in lowered for place in ["p. r. china", "united states", "australia", "usa", "beijing", "tianjin", "changsha", "austin"])
        ):
            return True

        if re.fullmatch(r"\d+\s*\|\s*[A-Za-z].*", line):
            return True
        if re.fullmatch(r"\(?\d+\s+of\s+\d+\)?", lowered):
            return True
        if re.fullmatch(r"[0-9./:;,\- ]{1,12}", line):
            return True
        return False

    def _tokenize(self, text: str) -> List[str]:
        return tokenize_text(text)

    def _write_jsonl(self, path: Path, rows: Iterable[Any]) -> None:
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


class KnowledgeBaseStore:
    """Runtime loader and retriever for the minimal paper knowledge base."""

    def __init__(
        self,
        *,
        output_dir: Path | str = DEFAULT_OUTPUT_DIR,
        documents: Optional[List[DocumentRecord]] = None,
        chunks: Optional[List[ChunkRecord]] = None,
        bm25: Optional[BM25Okapi] = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.documents = documents or []
        self.chunks = chunks or []
        self.bm25 = bm25
        self._documents_by_id = {doc.doc_id: doc for doc in self.documents}
        self._chunks_by_id = {chunk.chunk_id: chunk for chunk in self.chunks}

    @classmethod
    def load(cls, output_dir: Path | str = DEFAULT_OUTPUT_DIR) -> "KnowledgeBaseStore":
        output_path = Path(output_dir)
        documents = [
            DocumentRecord(**payload)
            for payload in _read_jsonl(output_path / "parsed" / "documents.jsonl")
        ]
        chunks = [
            ChunkRecord(**payload)
            for payload in _read_jsonl(output_path / "chunks" / "chunks.jsonl")
        ]
        with (output_path / "index" / "bm25.pkl").open("rb") as fh:
            bm25 = pickle.load(fh)
        return cls(output_dir=output_path, documents=documents, chunks=chunks, bm25=bm25)

    def stats(self) -> Dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "document_count": len(self.documents),
            "chunk_count": len(self.chunks),
        }

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        max_hits_per_doc: Optional[int] = 2,
    ) -> Dict[str, Any]:
        if not self.bm25 or not self.chunks:
            raise RuntimeError("Knowledge base is empty or not loaded")
        tokens = tokenize_text(query)
        if not tokens:
            return {"query": query, "hits": []}

        scores = self.bm25.get_scores(tokens)
        ranked = sorted(
            enumerate(scores),
            key=lambda item: item[1],
            reverse=True,
        )
        hits: List[Dict[str, Any]] = []
        doc_hit_counts: Dict[str, int] = {}
        for index, score in ranked:
            if len(hits) >= top_k:
                break
            if float(score) <= 0:
                continue
            chunk = self.chunks[index]
            if max_hits_per_doc is not None and doc_hit_counts.get(chunk.doc_id, 0) >= max_hits_per_doc:
                continue
            doc_hit_counts[chunk.doc_id] = doc_hit_counts.get(chunk.doc_id, 0) + 1
            hits.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "filename": chunk.filename,
                    "title": chunk.title,
                    "source": chunk.source,
                    "score": float(score),
                    "text": chunk.text,
                }
            )
        return {"query": query, "hits": hits}

    def render_search_result(
        self,
        query: str,
        top_k: int = 5,
        max_chars: int = 1200,
        *,
        max_hits_per_doc: Optional[int] = 2,
    ) -> str:
        result = self.search(query=query, top_k=top_k, max_hits_per_doc=max_hits_per_doc)
        if not result["hits"]:
            return ""
        blocks: List[str] = []
        for index, hit in enumerate(result["hits"], start=1):
            snippet = hit["text"][:max_chars].rstrip()
            document = self._documents_by_id.get(hit["doc_id"])
            lines = [
                f"### Hit {index}",
                f"Title: {hit['title']}",
                f"Filename: {hit['filename']}",
                f"Source: {hit['source']}",
                f"Score: {hit['score']:.4f}",
            ]
            if document and document.summary:
                lines.append(f"Summary: {document.summary}")
            lines.extend(["Text:", snippet])
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks).strip()

    def get_chunk(self, chunk_id: str) -> Optional[Dict[str, Any]]:
        chunk = self._chunks_by_id.get(chunk_id)
        if not chunk:
            return None
        return asdict(chunk)

    def get_doc(self, doc_id: str) -> Optional[Dict[str, Any]]:
        doc = self._documents_by_id.get(doc_id)
        if not doc:
            return None
        return asdict(doc)


def tokenize_text(text: str) -> List[str]:
    normalized = re.sub(r"\s+", " ", str(text or "").lower()).strip()
    if not normalized:
        return []

    tokens: List[str] = []
    for token in jieba.lcut(normalized, cut_all=False):
        token = token.strip()
        if len(token) <= 1 and not re.search(r"[a-z0-9\u4e00-\u9fff]", token):
            continue
        tokens.append(token)

    for token in re.findall(r"[a-z0-9\u4e00-\u9fff\-\+\[\]\(\)\./]{2,}", normalized):
        if token not in tokens:
            tokens.append(token)
    return tokens


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows

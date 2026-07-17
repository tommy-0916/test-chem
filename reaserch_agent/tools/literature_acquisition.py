"""Seed resolution + citation snowball + campaign archiving.

Turns user references (DOI / arXiv / title / local files) into verified,
deduplicated, campaign-tagged corpus records that the research layer can
retrieve. All network failures degrade into ``errors`` entries — literature
acquisition must never abort a planning branch.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .ingestion import ExternalKnowledgeClient, ExternalPaper, KnowledgeIngestion
from .paper_registry import PaperRegistry
from .query_sanitizer import sanitize_search_queries, sanitize_search_query
from .web_search import WebSearchClient, WebSearchResult

RelevanceFn = Callable[[ExternalPaper, str], float]


def default_kb_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "chem_kb"


def default_keyword_sources() -> List[str]:
    """Scholarly sources for the keyword line, keyed by configured credentials.

    Always: Semantic Scholar / Crossref / arXiv / OpenAlex (all keyless).
    Adds Google Scholar when SERPER_API_KEY is set; PubMed stays opt-in
    (biomedical bias) via the keyword_sources parameter.
    """
    sources = ["semantic_scholar", "crossref", "arxiv", "openalex"]
    if os.getenv("SERPER_API_KEY", "").strip():
        sources.append("google_scholar")
    return sources


def token_overlap_relevance(paper: ExternalPaper, anchor_text: str) -> float:
    """Deterministic candidate pre-filter: anchor-token coverage in the paper."""
    anchor_tokens = _relevance_tokens(anchor_text)
    if not anchor_tokens:
        return 0.0
    paper_text = f"{paper.title} {paper.abstract[:600]}".lower()
    matched = sum(1 for token in anchor_tokens if token in paper_text)
    return matched / len(anchor_tokens)


_RELEVANCE_STOPWORDS = {
    "and",
    "are",
    "based",
    "for",
    "from",
    "high",
    "into",
    "its",
    "low",
    "new",
    "of",
    "study",
    "the",
    "their",
    "toward",
    "towards",
    "using",
    "via",
    "with",
}


def _relevance_tokens(text: str) -> List[str]:
    lowered = (text or "").lower()
    latin_tokens = [
        token
        for token in re.findall(r"[0-9a-z][0-9a-z()\-]{2,}", lowered)
        if token not in _RELEVANCE_STOPWORDS
    ]
    cjk_chunks = re.findall(r"[一-鿿]{2,}", lowered)
    cjk_tokens: List[str] = []
    for chunk in cjk_chunks:
        cjk_tokens.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
    tokens: List[str] = []
    seen = set()
    for token in latin_tokens + cjk_tokens:
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens[:64]


class LiteratureAcquisition:
    """One campaign's literature acquisition pipeline (B1 seeds + B2 repair)."""

    def __init__(
        self,
        *,
        kb_dir: str | Path | None = None,
        campaign_id: str = "",
        client: Optional[ExternalKnowledgeClient] = None,
        ingestion: Optional[KnowledgeIngestion] = None,
        registry: Optional[PaperRegistry] = None,
        web_client: Optional[WebSearchClient] = None,
        enable_web_search: bool = False,
        enable_scholarly_search: bool = True,
        keyword_sources: Optional[Sequence[str]] = None,
        max_keyword_results: int = 5,
        max_snowball_per_seed: int = 10,
        max_total_new: int = 25,
        max_web_results: int = 4,
        max_web_pages: int = 2,
        download_pdfs: bool = False,
        relevance_threshold: float = 0.1,
        relevance_fn: Optional[RelevanceFn] = None,
    ) -> None:
        self.kb_dir = Path(kb_dir).expanduser().resolve() if kb_dir else default_kb_dir()
        self.campaign_id = campaign_id
        self.client = client or ExternalKnowledgeClient(
            semantic_scholar_api_key=os.getenv("SEMANTIC_SCHOLAR_API_KEY", ""),
            crossref_mailto=os.getenv("CROSSREF_MAILTO", ""),
        )
        self.ingestion = ingestion or KnowledgeIngestion(self.kb_dir)
        self.registry = registry or PaperRegistry(self.kb_dir)
        self.web_client = web_client
        self.enable_web_search = enable_web_search
        self.enable_scholarly_search = enable_scholarly_search
        self.keyword_sources = list(
            default_keyword_sources() if keyword_sources is None else keyword_sources
        )
        self.max_keyword_results = max_keyword_results
        self.max_snowball_per_seed = max_snowball_per_seed
        self.max_total_new = max_total_new
        self.max_web_results = max_web_results
        self.max_web_pages = max_web_pages
        self.download_pdfs = download_pdfs
        self.relevance_threshold = relevance_threshold
        self.relevance_fn: RelevanceFn = relevance_fn or token_overlap_relevance
        self.errors: List[str] = []
        self.last_candidate_log: List[Dict[str, Any]] = []

    def _resolve_web_client(self) -> Optional[WebSearchClient]:
        if not self.enable_web_search:
            return None
        if self.web_client is None:
            self.web_client = WebSearchClient()
        return self.web_client

    # ------------------------------------------------------------------
    # bootstrap acquisition: seeds -> snowball -> keyword line
    # ------------------------------------------------------------------

    def acquire_for_bootstrap(
        self,
        query: str,
        reference_inputs: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        self.errors = []
        # Issue 1: strip automation/device/lab task context before anything
        # goes to a scholarly engine. The caller's original query is untouched.
        search_query = sanitize_search_query(query)
        if self.enable_scholarly_search:
            seeds = self._resolve_seeds(reference_inputs)
        else:
            seeds = []
            self._register_local_references(reference_inputs)

        candidates: List[Tuple[str, ExternalPaper]] = []
        for seed in seeds:
            for fetch, label in [
                (self.client.fetch_references, "references"),
                (self.client.fetch_citations, "citations"),
            ]:
                linked = self._safe(
                    lambda: fetch(seed, limit=self.max_snowball_per_seed),
                    f"snowball {label} of '{seed.title[:60]}'",
                )
                candidates.extend(("snowball", paper) for paper in linked or [])

        if (
            self.enable_scholarly_search
            and search_query.strip()
            and self.max_keyword_results > 0
        ):
            keyword_papers = self._safe(
                lambda: self.client.search(
                    search_query,
                    sources=tuple(self.keyword_sources),
                    max_results=self.max_keyword_results,
                ),
                "keyword search",
            )
            self.errors.extend(self.client.last_errors)
            candidates.extend(("keyword", paper) for paper in keyword_papers or [])

        anchor_text = " ".join([search_query] + [seed.title for seed in seeds])
        kept = self._filter_candidates(candidates, anchor_text, seeds)

        written_files: List[str] = []
        registered = 0
        for role, paper in [("seed", seed) for seed in seeds] + kept:
            paths, was_new = self._archive_paper(paper, role=role)
            written_files.extend(paths)
            registered += 1 if was_new else 0

        web_summary = self._acquire_web_knowledge(search_query, anchor_text)
        written_files.extend(web_summary.get("written_files", []))

        summary = {
            "seeds": [self._paper_summary(seed) for seed in seeds],
            "snowball_kept": sum(1 for role, _ in kept if role == "snowball"),
            "keyword_kept": sum(1 for role, _ in kept if role == "keyword"),
            "keyword_sources": list(self.keyword_sources),
            "scholarly_enabled": self.enable_scholarly_search,
            # the query actually sent to paper databases (auditable, issue 1)
            "keyword_query": search_query,
            # full candidate set + per-candidate filter verdicts (issue 3)
            "candidates_log": list(getattr(self, "last_candidate_log", [])),
            "web_kept": web_summary.get("kept", 0),
            "web_engine": web_summary.get("engine", ""),
            "candidates_seen": len(candidates),
            "written_files": written_files,
            "newly_registered": registered,
            "errors": list(self.errors),
        }
        return summary

    # ------------------------------------------------------------------
    # B2 repair acquisition: bounded online round for abnormal paths
    # ------------------------------------------------------------------

    def acquire_for_repair(
        self,
        queries: Sequence[str],
        *,
        stage: str = "",
        max_results: int = 5,
    ) -> Dict[str, Any]:
        self.errors = []
        written_files: List[str] = []
        kept_count = 0
        # Issue 1: repair-round queries carry stage/observation prose that may
        # include device context — sanitize before they leave the process.
        repair_queries = sanitize_search_queries(queries)
        for query in repair_queries[:2]:
            if not self.enable_scholarly_search:
                break
            papers = self._safe(
                lambda: self.client.search(
                    query,
                    sources=tuple(self.keyword_sources),
                    max_results=max_results,
                ),
                f"repair search '{query[:60]}'",
            )
            self.errors.extend(self.client.last_errors)
            for paper in papers or []:
                if self.relevance_fn(paper, query) < self.relevance_threshold:
                    continue
                paths, _ = self._archive_paper(paper, role="b2_repair", stage=stage)
                written_files.extend(paths)
                kept_count += 1

        web_summary: Dict[str, Any] = {}
        if repair_queries:
            web_summary = self._acquire_web_knowledge(
                repair_queries[0],
                repair_queries[0],
                role="b2_repair_web",
                stage=stage,
                max_pages=1,
            )
            written_files.extend(web_summary.get("written_files", []))
        return {
            "kept": kept_count,
            "web_kept": web_summary.get("kept", 0),
            "search_queries": repair_queries[:2],
            "written_files": written_files,
            "errors": list(self.errors),
        }

    # ------------------------------------------------------------------
    # web line: search engines + page reading (candidates, not evidence)
    # ------------------------------------------------------------------

    def _acquire_web_knowledge(
        self,
        query: str,
        anchor_text: str,
        *,
        role: str = "web",
        stage: str = "",
        max_pages: int | None = None,
    ) -> Dict[str, Any]:
        """Search the open web, read top pages, archive as unverified leads."""
        web = self._resolve_web_client()
        if web is None or not query.strip():
            return {}
        page_budget = self.max_web_pages if max_pages is None else max_pages
        results = self._safe(
            lambda: web.search(query, max_results=self.max_web_results),
            "web search",
        )
        self.errors.extend(getattr(web, "last_errors", []) or [])
        if not results:
            return {"kept": 0, "engine": "", "written_files": []}

        anchor_tokens_text = anchor_text or query
        scored: List[tuple[float, WebSearchResult]] = []
        for result in results:
            pseudo = ExternalPaper(
                title=result.title,
                abstract=result.snippet or result.content[:400],
            )
            scored.append((self.relevance_fn(pseudo, anchor_tokens_text), result))
        scored.sort(key=lambda pair: -pair[0])

        written_files: List[str] = []
        kept = 0
        engine = ""
        for score, result in scored:
            if kept >= page_budget:
                break
            if score < self.relevance_threshold:
                continue
            page_text = self._safe(
                lambda: web.fetch_page(result.url),
                f"web fetch '{result.url[:60]}'",
            )
            if not page_text or not str(page_text).strip():
                continue
            record_paths = self._safe(
                lambda: [
                    self.ingestion.write_record(
                        self.ingestion.record_from_text(
                            title=result.title or result.url,
                            text=str(page_text),
                            source_path=result.url,
                            source_type="web",
                        )
                    )
                ],
                f"web ingest '{result.title[:50]}'",
            )
            paths = [str(path) for path in record_paths or []]
            if not paths:
                continue
            self.registry.upsert(
                title=result.title or result.url,
                source=f"web:{result.engine}",
                url=result.url,
                verification_status="web_unverified",
                full_text_status="parsed",
                corpus_file=paths[0],
                campaign_id=self.campaign_id,
                role=role,
                stage=stage,
            )
            written_files.extend(paths)
            engine = result.engine
            kept += 1
        return {"kept": kept, "engine": engine, "written_files": written_files}

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _resolve_seeds(
        self,
        reference_inputs: Sequence[Dict[str, Any]],
    ) -> List[ExternalPaper]:
        seeds: List[ExternalPaper] = []
        for entry in reference_inputs or []:
            kind = str(entry.get("kind", ""))
            if kind in {"local_file", "local_dir"}:
                self._register_local_reference(entry)
                continue
            if entry.get("status") not in {"pending_resolution", "resolution_failed"}:
                continue

            paper: Optional[ExternalPaper] = None
            if kind == "doi":
                paper = self._safe(
                    lambda: self.client.lookup_doi(str(entry.get("doi", ""))),
                    f"resolve doi {entry.get('doi')}",
                )
            elif kind == "arxiv":
                paper = self._safe(
                    lambda: self.client.lookup_arxiv(str(entry.get("arxiv_id", ""))),
                    f"resolve arxiv {entry.get('arxiv_id')}",
                )
            elif kind == "title":
                paper = self._safe(
                    lambda: self.client.lookup_title(str(entry.get("raw", ""))),
                    f"resolve title '{str(entry.get('raw', ''))[:60]}'",
                )
            else:
                continue

            if paper is not None and paper.title:
                entry["status"] = "resolved"
                entry["resolved_title"] = paper.title
                if paper.doi:
                    entry["doi"] = paper.doi
                seeds.append(paper)
            else:
                entry["status"] = "resolution_failed"
        return seeds

    def _register_local_references(
        self,
        reference_inputs: Sequence[Dict[str, Any]],
    ) -> None:
        for entry in reference_inputs or []:
            if str(entry.get("kind", "")) in {"local_file", "local_dir"}:
                self._register_local_reference(entry)

    def _register_local_reference(self, entry: Dict[str, Any]) -> None:
        for written in entry.get("written_records", []) or []:
            title = Path(written).stem
            self.registry.upsert(
                title=title,
                source="local_file",
                verification_status="local_file",
                full_text_status="parsed",
                corpus_file=str(written),
                campaign_id=self.campaign_id,
                role="seed",
            )

    def _filter_candidates(
        self,
        candidates: Sequence[Tuple[str, ExternalPaper]],
        anchor_text: str,
        seeds: Sequence[ExternalPaper],
    ) -> List[Tuple[str, ExternalPaper]]:
        seed_keys = {self._paper_key(seed) for seed in seeds}
        # Issue 3 (repeatability): candidate order must not depend on network
        # response order. Score first, then sort deterministically (relevance
        # desc, snowball before keyword, then title) BEFORE the cap applies,
        # and keep an auditable log of every candidate + the filter verdict.
        role_priority = {"snowball": 0, "keyword": 1}
        scored: List[Tuple[float, int, str, str, ExternalPaper]] = []
        for role, paper in candidates:
            score = self.relevance_fn(paper, anchor_text)
            scored.append(
                (score, role_priority.get(role, 9), role, paper.title.strip(), paper)
            )
        scored.sort(key=lambda item: (-item[0], item[1], item[3].lower()))

        kept: List[Tuple[str, ExternalPaper]] = []
        candidate_log: List[Dict[str, Any]] = []
        seen = set(seed_keys)
        for score, _, role, title, paper in scored:
            entry = {
                "title": title[:160],
                "doi": paper.doi,
                "role": role,
                "relevance": round(float(score), 4),
            }
            if not title:
                continue
            key = self._paper_key(paper)
            if key in seen:
                entry.update({"kept": False, "reason": "duplicate_of_seed_or_earlier"})
                candidate_log.append(entry)
                continue
            if score < self.relevance_threshold:
                entry.update(
                    {
                        "kept": False,
                        "reason": f"relevance {score:.3f} < threshold {self.relevance_threshold}",
                    }
                )
                candidate_log.append(entry)
                continue
            if len(kept) >= self.max_total_new:
                entry.update({"kept": False, "reason": "candidate_cap_reached"})
                candidate_log.append(entry)
                continue
            seen.add(key)
            kept.append((role, paper))
            entry.update({"kept": True, "reason": "passed_relevance_filter"})
            candidate_log.append(entry)
        if len(kept) >= self.max_total_new:
            self.errors.append(
                f"candidate cap reached ({self.max_total_new}); remaining candidates dropped"
            )
        self.last_candidate_log = candidate_log[:60]
        return kept

    def _archive_paper(
        self,
        paper: ExternalPaper,
        *,
        role: str,
        stage: str = "",
    ) -> Tuple[List[str], bool]:
        """Ingest into the corpus (once) and tag the campaign in the registry."""
        existing = self.registry.find(
            doi=paper.doi,
            arxiv_id=paper.arxiv_id,
            title=paper.title,
            year=paper.year,
            authors=paper.authors,
            source=paper.source,
            verification_status=self._verification_status(paper),
        )
        written: List[str] = []
        should_ingest = existing is None or not existing.get("corpus_files")
        if self.download_pdfs and existing is not None:
            should_ingest = should_ingest or existing.get("full_text_status") != "parsed"
        if should_ingest:
            paths = self._safe(
                lambda: self.ingestion.ingest_external_papers(
                    [paper],
                    download_pdfs=self.download_pdfs,
                    pdf_dir=self.kb_dir / "_pdf_sources" / (self.campaign_id or "shared"),
                ),
                f"ingest '{paper.title[:60]}'",
            )
            written = [str(path) for path in paths or []]

        full_text_status = "metadata_only"
        pdf_file = ""
        download_attempts: List[Dict[str, Any]] = []
        resolved_pdf_url = paper.pdf_url
        latest: Optional[Dict[str, Any]] = None
        if written:
            for candidate in reversed(
                list(getattr(self.ingestion, "last_external_results", []) or [])
            ):
                if not isinstance(candidate, dict):
                    continue
                if str(candidate.get("title") or "") != paper.title:
                    continue
                if str(candidate.get("record_path") or "") not in written:
                    continue
                latest = candidate
                break
        if latest is not None:
            full_text_status = str(
                latest.get("full_text_status") or "metadata_only"
            )
            download_payload = latest.get("pdf_download") or {}
            if isinstance(download_payload, dict):
                pdf_file = str(download_payload.get("path") or "")
                resolved_pdf_url = str(
                    download_payload.get("url") or resolved_pdf_url
                )
                download_attempts = [
                    dict(item)
                    for item in download_payload.get("attempts", []) or []
                    if isinstance(item, dict)
                ]
                if self.download_pdfs and full_text_status != "parsed":
                    errors = [
                        str(item)
                        for item in download_payload.get("errors", []) or []
                        if str(item).strip()
                    ]
                    if errors:
                        self.errors.append(
                            f"PDF acquisition for '{paper.title[:60]}': "
                            + "; ".join(errors[:3])
                        )
        elif existing is not None:
            full_text_status = str(
                existing.get("full_text_status") or "metadata_only"
            )

        record, created = self.registry.upsert(
            title=paper.title,
            doi=paper.doi,
            arxiv_id=paper.arxiv_id,
            source=paper.source,
            url=paper.url,
            pdf_url=resolved_pdf_url,
            year=paper.year,
            authors=paper.authors,
            verification_status=self._verification_status(paper),
            full_text_status=full_text_status,
            corpus_file=written[0] if written else "",
            pdf_file=pdf_file,
            download_attempts=download_attempts,
            campaign_id=self.campaign_id,
            role=role,
            stage=stage,
        )
        return written, created

    @staticmethod
    def _verification_status(paper: ExternalPaper) -> str:
        if paper.doi:
            return "verified_doi"
        if paper.arxiv_id:
            return "verified_arxiv"
        if paper.source == "semantic_scholar" and paper.source_id:
            return "verified_semantic_scholar"
        return "unverified"

    @staticmethod
    def _paper_key(paper: ExternalPaper) -> str:
        if paper.doi:
            return f"doi:{paper.doi.lower()}"
        if paper.arxiv_id:
            return "arxiv:" + re.sub(r"v\d+$", "", paper.arxiv_id.lower())
        if paper.source_id:
            return f"{paper.source}:{paper.source_id.lower()}"
        title_key = re.sub(r"[^0-9a-z一-鿿]+", "", paper.title.lower())
        author_key = ""
        if paper.authors:
            author_tokens = re.findall(
                r"[0-9a-z一-鿿]+", paper.authors[0].lower()
            )
            author_key = author_tokens[-1] if author_tokens else ""
        return f"title:{title_key}|year:{paper.year}|author:{author_key}"

    def _paper_summary(self, paper: ExternalPaper) -> Dict[str, Any]:
        return {
            "title": paper.title,
            "doi": paper.doi,
            "source": paper.source,
            "source_id": paper.source_id,
            "arxiv_id": paper.arxiv_id,
            "year": paper.year,
            "url": paper.url,
            "citation_count": paper.citation_count,
            "verification_status": self._verification_status(paper),
        }

    def _safe(self, fn: Callable[[], Any], label: str) -> Any:
        try:
            return fn()
        except Exception as exc:
            self.errors.append(f"{label}: {type(exc).__name__}: {exc}")
            return None

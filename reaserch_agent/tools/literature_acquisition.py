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


# ---------------------------------------------------------------------------
# Issue 8 P0-3: material-system AND reaction/observation joint hard gate.
# Weak-word overlap (碱性/乙醇/氧化/实验/CV…) let a poultry-nutrition paper into
# a NiCo EOR campaign. A candidate must now share BOTH ≥1 material token and
# ≥1 reaction/observation token with the anchor before the score threshold is
# even consulted. The gate self-disables when the anchor itself lacks either
# category, so campaigns outside this vocabulary keep the old behaviour.
# ---------------------------------------------------------------------------

# element symbol → (case-sensitive symbol regex, english name, chinese name).
# Symbol regex: literal symbol NOT followed by a lowercase letter, so `Co`
# matches Co3O4/CoOOH/NiCo/Co-based but never Composite/copper/CO2.
_ELEMENT_SIGNALS: Dict[str, Tuple[re.Pattern, str, str]] = {
    symbol: (re.compile(rf"{symbol}(?![a-z])"), english, chinese)
    for symbol, english, chinese in (
        ("Ni", "nickel", "镍"),
        ("Fe", "iron", "铁"),
        ("Co", "cobalt", "钴"),
        ("Mo", "molybdenum", "钼"),
        ("Mn", "manganese", "锰"),
        ("Cu", "copper", "铜"),
        ("Zn", "zinc", "锌"),
        ("W", "tungsten", "钨"),
        ("V", "vanadium", "钒"),
        ("Ru", "ruthenium", "钌"),
        ("Ir", "iridium", "铱"),
        ("Pt", "platinum", "铂"),
        ("Pd", "palladium", "钯"),
        ("Ce", "cerium", "铈"),
        ("Ti", "titanium", "钛"),
    )
}

# material-class names count as material signals too (paper may use the class
# name without spelling out elements).
_MATERIAL_CLASS_TERMS = (
    "prussian blue", "hexacyanoferrate", "pba", "普鲁士蓝",
    "layered double hydroxide", "ldh", "层状双氢氧化物", "水滑石",
    "mof", "zif", "金属有机框架", "perovskite", "钙钛矿",
    "spinel", "尖晶石", "oxyhydroxide", "羟基氧化物",
)

# target reaction / core observation vocabulary, grouped by SYNONYM: a paper
# saying 析氧 and an anchor saying OER refer to the same reaction and must
# intersect at the group level, not the literal-term level.
_REACTION_GROUPS: Dict[str, Tuple[str, ...]] = {
    "OER": ("oer", "oxygen evolution", "析氧"),
    "HER": ("her", "hydrogen evolution", "析氢"),
    "UOR": ("uor", "urea oxidation", "尿素氧化"),
    "EOR": ("eor", "ethanol oxidation", "ethanol electrooxidation", "乙醇氧化", "乙醇电氧化"),
    "MOR": ("methanol oxidation", "甲醇氧化"),
    "ORR": ("orr", "oxygen reduction", "氧还原"),
    "CO2RR": ("co2rr", "co2 reduction", "二氧化碳还原"),
    "NRR": ("nrr", "nitrogen reduction", "固氮"),
    "water_splitting": ("water splitting", "水分解", "电解水"),
    "electrocatalysis": (
        "electrocataly", "electrochemical", "electrooxidation",
        "电催化", "电化学", "电氧化",
    ),
    "kinetics": ("overpotential", "过电位", "tafel", "塔菲尔"),
    "reconstruction": ("surface reconstruction", "表面重构"),
    "valence": ("valence", "价态"),
    "battery": ("battery", "电池", "supercapacitor", "超级电容"),
    "photocatalysis": ("photocataly", "光催化"),
    "catalyst": ("catalyst", "催化剂"),
}


def _match_elements(text: str) -> set:
    matched = set()
    lowered = text.lower()
    for symbol, (symbol_re, english, chinese) in _ELEMENT_SIGNALS.items():
        if symbol_re.search(text) or english in lowered or chinese in text:
            matched.add(symbol)
    return matched


def _match_material_classes(text: str) -> set:
    lowered = text.lower()
    return {term for term in _MATERIAL_CLASS_TERMS if term in lowered}


def _match_reactions(text: str) -> set:
    """Return the SYNONYM-GROUP keys the text mentions (any variant counts)."""
    lowered = text.lower()
    return {
        group
        for group, variants in _REACTION_GROUPS.items()
        if any(term in lowered for term in variants)
    }


def chemistry_gate(paper: ExternalPaper, anchor_text: str) -> Tuple[bool, str]:
    """(passes, reason). Hard AND-gate on material system + reaction terms.

    Disabled (always passes) when the anchor itself lacks either category —
    the gate must never reject candidates the anchor cannot describe.
    """
    anchor = str(anchor_text or "")
    anchor_elements = _match_elements(anchor)
    anchor_classes = _match_material_classes(anchor)
    anchor_reactions = _match_reactions(anchor)
    if (not anchor_elements and not anchor_classes) or not anchor_reactions:
        return True, "gate_inactive(anchor lacks material or reaction vocabulary)"

    paper_text = f"{paper.title} {paper.abstract[:800]}"
    material_hit = (
        (_match_elements(paper_text) & anchor_elements)
        or (_match_material_classes(paper_text) & anchor_classes)
    )
    if not material_hit:
        return False, (
            "material gate: paper lacks anchor material system "
            f"({'/'.join(sorted(anchor_elements | anchor_classes))})"
        )
    reaction_hit = _match_reactions(paper_text) & anchor_reactions
    if not reaction_hit:
        return False, (
            "reaction gate: paper lacks anchor reaction/observation terms"
        )
    return True, f"material={'/'.join(sorted(material_hit))}"


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


def classify_retrieval_status(found: bool, errors: Sequence[str]) -> str:
    """Keep provider outages distinct from a valid zero-result search."""
    if found:
        return "success"
    for error in errors:
        if re.search(
            r"http(?:error)?\s*[:_-]?\s*(?:error\s*)?(?:429|403|5\d\d)\b|timed\s*out|timeout|credential|connection|unreachable",
            str(error or ""), re.I,
        ):
            return "provider_failure"
    return "no_relevant_papers"


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
        *,
        survey_queries: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        self.errors = []
        # Issue 1: strip automation/device/lab task context before anything
        # goes to a scholarly engine. The caller's original query is untouched.
        search_query = sanitize_search_query(query)
        # Issue 8 P0-1: when Research has already generated focused chemistry
        # survey queries, the scholarly line MUST consume them — one short
        # query per API call — instead of the (long) sanitized user query.
        # The long query is only the fallback when no survey queries exist.
        generated = [q for q in (survey_queries or []) if str(q).strip()]
        scholarly_queries = sanitize_search_queries(generated)[:4]
        if not scholarly_queries and search_query.strip():
            scholarly_queries = [search_query]
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

        attempted_queries: List[str] = []
        if self.enable_scholarly_search and self.max_keyword_results > 0:
            per_query = max(3, self.max_keyword_results // max(1, len(scholarly_queries) or 1))
            for line_query in scholarly_queries:
                attempted_queries.append(line_query)
                keyword_papers = self._safe(
                    lambda q=line_query: self.client.search(
                        q,
                        sources=tuple(self.keyword_sources),
                        max_results=per_query,
                    ),
                    f"keyword search '{line_query[:50]}'",
                )
                self.errors.extend(self.client.last_errors)
                candidates.extend(("keyword", paper) for paper in keyword_papers or [])

        anchor_text = " ".join(
            scholarly_queries + [search_query] + [seed.title for seed in seeds]
        )
        kept = self._filter_candidates(candidates, anchor_text, seeds)

        # Issue 8 P1: bounded zero-hit retrieval repair. When the first round
        # keeps nothing, run at most two deterministic retry rounds (strip
        # characterization terms → latin/english core query). Every retry
        # query is recorded; no unbounded loops.
        retry_rounds: List[Dict[str, Any]] = []
        if self.enable_scholarly_search and not kept and self.max_keyword_results > 0:
            for round_label, retry_queries in self._zero_hit_retry_plan(
                scholarly_queries, anchor_text
            ):
                if not retry_queries:
                    continue
                new_candidates: List[Tuple[str, ExternalPaper]] = []
                for retry_query in retry_queries[:3]:
                    attempted_queries.append(retry_query)
                    papers = self._safe(
                        lambda q=retry_query: self.client.search(
                            q,
                            sources=tuple(self.keyword_sources),
                            max_results=max(3, self.max_keyword_results // 2),
                        ),
                        f"zero-hit retry [{round_label}] '{retry_query[:40]}'",
                    )
                    self.errors.extend(self.client.last_errors)
                    new_candidates.extend(("keyword", paper) for paper in papers or [])
                retry_rounds.append(
                    {
                        "round": round_label,
                        "queries": list(retry_queries[:3]),
                        "returned": len(new_candidates),
                    }
                )
                if new_candidates:
                    candidates.extend(new_candidates)
                    kept = self._filter_candidates(candidates, anchor_text, seeds)
                    if kept:
                        break

        written_files: List[str] = []
        registered = 0
        for role, paper in [("seed", seed) for seed in seeds] + kept:
            paths, was_new = self._archive_paper(paper, role=role)
            written_files.extend(paths)
            registered += 1 if was_new else 0

        web_line_query = scholarly_queries[0] if scholarly_queries else search_query
        web_summary = self._acquire_web_knowledge(web_line_query, anchor_text)
        written_files.extend(web_summary.get("written_files", []))

        # Issue 8 P1: distinguish "provider broke" from "keywords found
        # nothing relevant" — they demand different fixes and must never be
        # reported as the same failure.
        provider_failure = any(
            re.search(
                r"http(?:error)?\s*[:_-]?\s*(?:error\s*)?"
                r"(?:429|403|5\d\d)\b|timed\s*out|timeout|credential|"
                r"connection|unreachable",
                str(error or ""),
                re.I,
            )
            is not None
            for error in self.errors
        )
        if kept or seeds:
            retrieval_status = "success"
        elif provider_failure:
            retrieval_status = "provider_failure"
        else:
            retrieval_status = "no_relevant_papers"

        summary = {
            "seeds": [self._paper_summary(seed) for seed in seeds],
            "snowball_kept": sum(1 for role, _ in kept if role == "snowball"),
            "keyword_kept": sum(1 for role, _ in kept if role == "keyword"),
            "keyword_sources": list(self.keyword_sources),
            "scholarly_enabled": self.enable_scholarly_search,
            # the queries actually sent to paper databases (auditable, issue 8)
            "keyword_query": " | ".join(attempted_queries),
            "generated_survey_queries": list(generated),
            "actual_scholarly_queries": list(attempted_queries),
            "zero_hit_retry_rounds": retry_rounds,
            "retrieval_status": retrieval_status,
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

    _CHARACTERIZATION_TERMS = (
        "XRD", "XPS", "SEM", "TEM", "EDS", "EIS", "CV", "LSV", "XAS",
        "STEM", "拉曼", "Raman", "红外", "FTIR", "紫外", "表征",
    )

    def _zero_hit_retry_plan(
        self,
        scholarly_queries: Sequence[str],
        anchor_text: str,
    ) -> List[Tuple[str, List[str]]]:
        """Deterministic, bounded retry rounds after a zero-keep first pass.

        Round 1 strips characterization terms (they narrow scholarly matching
        without changing the topic); round 2 keeps only the latin-script
        material/reaction tokens (english/formula core, most portable across
        scholarly APIs). Two rounds max — issue 8 forbids unbounded retries.
        """
        rounds: List[Tuple[str, List[str]]] = []

        stripped: List[str] = []
        for query in scholarly_queries:
            reduced = query
            for term in self._CHARACTERIZATION_TERMS:
                reduced = re.sub(rf"\s*{re.escape(term)}\s*", " ", reduced)
            reduced = re.sub(r"\s{2,}", " ", reduced).strip()
            if reduced and reduced != query and len(reduced) >= 6:
                stripped.append(reduced)
        rounds.append(("strip_characterization", stripped))

        latin: List[str] = []
        seen = set()
        for source_text in list(scholarly_queries) + [anchor_text]:
            tokens = re.findall(r"[A-Za-z][A-Za-z0-9()\-]{1,}", source_text)
            core = " ".join(dict.fromkeys(tokens))[:80].strip()
            if core and core.lower() not in seen and len(core.split()) >= 2:
                seen.add(core.lower())
                latin.append(core)
        rounds.append(("latin_core", latin[:2]))
        return rounds

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
        candidate_log: List[Dict[str, Any]] = []
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
                relevance = self.relevance_fn(paper, query)
                kept = relevance >= self.relevance_threshold
                candidate_log.append({
                    "title": paper.title, "doi": paper.doi, "arxiv_id": paper.arxiv_id,
                    "kept": kept, "relevance": round(float(relevance), 4),
                    "reason": "passed_relevance_filter" if kept else "below_relevance_threshold",
                })
                if not kept:
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
            "actual_scholarly_queries": repair_queries[:2] if self.enable_scholarly_search else [],
            "candidates_log": candidate_log,
            "retrieval_status": classify_retrieval_status(kept_count > 0, self.errors),
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
                "title": title,
                "doi": paper.doi,
                "arxiv_id": paper.arxiv_id,
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
            # Issue 8 P0-3: joint material+reaction hard gate BEFORE the
            # weak-overlap threshold — poultry-nutrition papers scored above
            # threshold on 碱性/乙醇/氧化 alone and must be refused here.
            gate_ok, gate_reason = chemistry_gate(paper, anchor_text)
            if not gate_ok:
                entry.update({"kept": False, "reason": gate_reason})
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

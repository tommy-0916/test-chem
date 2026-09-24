"""Unit tests for literature acquisition, registry, and B1 integration (P3)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, List

from reaserch_agent.tools.ingestion import ExternalPaper
from reaserch_agent.tools.literature_acquisition import (
    LiteratureAcquisition,
    token_overlap_relevance,
)
from reaserch_agent.tools.paper_registry import PaperRegistry
from reaserch_agent.tools.web_search import WebSearchResult
from reaserch_agent.workflow import ResearchAgent

SEED_PAPER = ExternalPaper(
    title="NiFe Prussian Blue Analogue Electrochemical Activation for OER",
    abstract="NiFe PBA electrochemical activation produces an active OER electrocatalyst.",
    source="crossref",
    source_id="10.1000/seed",
    doi="10.1000/seed",
    url="https://doi.org/10.1000/seed",
    year="2024",
    authors=["A. Chemist"],
)
RELEVANT_REF = ExternalPaper(
    title="Prussian blue analogue derived NiFe oxyhydroxide as OER catalyst",
    abstract="Electrochemical activation of NiFe PBA yields oxyhydroxide OER catalysts.",
    source="semantic_scholar",
    source_id="s2ref1",
    doi="10.1000/ref1",
    year="2023",
)
IRRELEVANT_REF = ExternalPaper(
    title="Transformer architectures in natural language processing",
    abstract="Attention mechanisms improve neural machine translation quality.",
    source="semantic_scholar",
    source_id="s2nlp",
    doi="10.1000/nlp",
    year="2022",
)
DUPLICATE_CITATION = ExternalPaper(
    title="Prussian blue analogue derived NiFe oxyhydroxide as OER catalyst",
    abstract="Duplicate of ref1 arriving through the citations line.",
    source="semantic_scholar",
    source_id="s2ref1-dup",
    doi="10.1000/ref1",
    year="2023",
)
KEYWORD_PAPER = ExternalPaper(
    title="Kinetics of NiFe PBA electrochemical activation in alkaline media",
    abstract="CV cycling activates NiFe Prussian blue analogue electrodes for OER.",
    source="crossref",
    source_id="10.1000/kw1",
    doi="10.1000/kw1",
    year="2025",
)

QUERY = "NiFe-PBA electrochemical activation OER 优化实验"


class FakeClient:
    def __init__(self) -> None:
        self.last_errors: List[str] = []
        self.calls: List[str] = []

    def lookup_doi(self, doi: str):
        self.calls.append(f"lookup_doi:{doi}")
        return SEED_PAPER if doi == SEED_PAPER.doi else None

    def lookup_arxiv(self, arxiv_id: str):
        self.calls.append(f"lookup_arxiv:{arxiv_id}")
        return None

    def lookup_title(self, title: str):
        self.calls.append(f"lookup_title:{title}")
        return SEED_PAPER if "prussian" in title.lower() else None

    def fetch_references(self, paper: ExternalPaper, *, limit: int = 20):
        self.calls.append(f"fetch_references:{paper.doi}")
        return [RELEVANT_REF, IRRELEVANT_REF]

    def fetch_citations(self, paper: ExternalPaper, *, limit: int = 20):
        self.calls.append(f"fetch_citations:{paper.doi}")
        return [DUPLICATE_CITATION]

    def search(self, query: str, *, sources, max_results: int = 5):
        self.calls.append(f"search:{query[:30]}")
        self.last_errors = []
        return [KEYWORD_PAPER]


class BrokenClient:
    def __init__(self) -> None:
        self.last_errors: List[str] = []
        self.calls: List[str] = []

    def _boom(self, label: str):
        self.calls.append(label)
        raise ConnectionError("network unreachable")

    def lookup_doi(self, doi: str):
        self._boom("lookup_doi")

    def lookup_arxiv(self, arxiv_id: str):
        self._boom("lookup_arxiv")

    def lookup_title(self, title: str):
        self._boom("lookup_title")

    def fetch_references(self, paper, *, limit: int = 20):
        self._boom("fetch_references")

    def fetch_citations(self, paper, *, limit: int = 20):
        self._boom("fetch_citations")

    def search(self, query: str, *, sources, max_results: int = 5):
        self._boom("search")


class FakeWebClient:
    def __init__(self) -> None:
        self.last_errors: List[str] = []
        self.search_calls: List[str] = []
        self.fetch_calls: List[str] = []

    def search(self, query: str, *, max_results: int = 5):
        self.search_calls.append(query)
        return [
            WebSearchResult(
                title="NiFe PBA electrochemical activation OER protocol",
                url="https://example.com/nife-pba",
                snippet="NiFe PBA activation for OER",
                engine="tavily",
            )
        ]

    def fetch_page(self, url: str, **kwargs):
        self.fetch_calls.append(url)
        return (
            "NiFe PBA electrochemical activation OER protocol. "
            "The electrode was cycled in alkaline electrolyte for activation."
        )


class StaleIngestion:
    def __init__(self) -> None:
        self.last_external_results: List[dict[str, Any]] = [
            {
                "title": "Paper A",
                "record_path": "/kb/a.json",
                "full_text_status": "parsed",
                "pdf_download": {
                    "path": "/kb/a.pdf",
                    "url": "https://example.com/a.pdf",
                    "attempts": [{"provider": "arxiv", "status": "downloaded"}],
                },
            }
        ]

    def ingest_external_papers(self, *args, **kwargs):
        raise AssertionError("existing paper must not be re-ingested")


class RelevanceTest(unittest.TestCase):
    def test_relevant_paper_scores_above_irrelevant(self) -> None:
        anchor = QUERY + " " + SEED_PAPER.title
        relevant = token_overlap_relevance(RELEVANT_REF, anchor)
        irrelevant = token_overlap_relevance(IRRELEVANT_REF, anchor)
        self.assertGreater(relevant, 0.2)
        self.assertLess(irrelevant, 0.1)


class ChemistryGateTest(unittest.TestCase):
    """Issue 8 P0-3: material-system AND reaction joint hard gate. The three
    false positives named in the issue (D01 cracking-catalyst + teaching-case
    papers, D02 poultry-nutrition paper) must be refused; genuinely relevant
    papers in either language must pass; the gate self-disables when the
    anchor lacks the vocabulary."""

    D01_ANCHOR = "Co 基催化剂 表面氧化态 碱性 EOR 乙醇氧化 Co(OH)2 Co3O4 CoOOH 液相产物"
    D02_ANCHOR = "NiCo 双金属 碱性 EOR 乙醇电氧化 活性 抗失活"
    A01_ANCHOR = "NiFe 层状双氢氧化物 共沉淀 Fe配位环境 碱性 OER"

    def test_issue8_false_positives_are_refused(self) -> None:
        from reaserch_agent.tools.literature_acquisition import chemistry_gate
        from reaserch_agent.tools.ingestion import ExternalPaper

        cracking = ExternalPaper(
            title="碱性氮化合物在裂化催化剂上的吸附 Ⅳ.氮化合物对废催化剂表面性质的影响",
            abstract="裂化催化剂 吸附 表面性质",
        )
        teaching = ExternalPaper(
            title="面向科学探究思维的实验教学六步框架设计与应用——乙醇催化氧化实验改进的说课案例",
            abstract="高中化学 教学 说课",
        )
        poultry = ExternalPaper(
            title="实验性诱导营养不良对鸡脑部抑郁和情绪中等以及血液影响的生物化学作用",
            abstract="AChE TAC TNF GPx SOD CBC 营养不良 鸡",
        )
        for anchor, paper in (
            (self.D01_ANCHOR, cracking),
            (self.D01_ANCHOR, teaching),
            (self.D02_ANCHOR, poultry),
        ):
            passes, reason = chemistry_gate(paper, anchor)
            self.assertFalse(passes, f"should refuse: {paper.title[:40]} ({reason})")

    def test_relevant_papers_pass_in_both_languages(self) -> None:
        from reaserch_agent.tools.literature_acquisition import chemistry_gate
        from reaserch_agent.tools.ingestion import ExternalPaper

        english = ExternalPaper(
            title="NiCo bimetallic catalysts for alkaline ethanol oxidation reaction",
            abstract="NiCo synergy EOR ethanol electrooxidation stability",
        )
        chinese = ExternalPaper(
            title="NiFe层状双氢氧化物的共沉淀制备及碱性析氧性能",
            abstract="镍铁 层状双氢氧化物 析氧 过电位",
        )
        self.assertTrue(chemistry_gate(english, self.D02_ANCHOR)[0])
        self.assertTrue(chemistry_gate(chinese, self.A01_ANCHOR)[0])

    def test_gate_inactive_when_anchor_lacks_vocabulary(self) -> None:
        from reaserch_agent.tools.literature_acquisition import chemistry_gate
        from reaserch_agent.tools.ingestion import ExternalPaper

        paper = ExternalPaper(title="anything at all", abstract="no chemistry")
        passes, reason = chemistry_gate(paper, "某个不含材料反应词的题目")
        self.assertTrue(passes)
        self.assertIn("gate_inactive", reason)


class PaperRegistryTest(unittest.TestCase):
    def test_upsert_dedup_and_campaign_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = PaperRegistry(tmp)
            first, created_first = registry.upsert(
                title=RELEVANT_REF.title,
                doi=RELEVANT_REF.doi,
                verification_status="verified_doi",
                corpus_file="/kb/a.json",
                campaign_id="cmp_a",
                role="snowball",
            )
            second, created_second = registry.upsert(
                title=RELEVANT_REF.title.upper(),
                doi=RELEVANT_REF.doi,
                verification_status="unverified",
                campaign_id="cmp_b",
                role="keyword",
            )

            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(first["paper_id"], second["paper_id"])
            self.assertEqual(second["verification_status"], "verified_doi")
            self.assertEqual(set(second["campaigns"]), {"cmp_a", "cmp_b"})
            self.assertEqual(registry.count(), 1)

            reloaded = PaperRegistry(tmp)
            self.assertEqual(reloaded.count(), 1)
            self.assertEqual(len(reloaded.campaign_papers("cmp_b")), 1)

    def test_stale_registry_instances_merge_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = PaperRegistry(tmp)
            stale_second = PaperRegistry(tmp)

            first.upsert(title="Paper A", doi="10.1000/a")
            stale_second.upsert(title="Paper B", doi="10.1000/b")

            reloaded = PaperRegistry(tmp)
            self.assertEqual(reloaded.count(), 2)
            self.assertIsNotNone(reloaded.find(doi="10.1000/a"))
            self.assertIsNotNone(reloaded.find(doi="10.1000/b"))

    def test_same_title_with_different_dois_creates_distinct_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = PaperRegistry(tmp)
            registry.upsert(
                title="Shared chemistry title",
                doi="10.1000/a",
                year="2025",
                authors=["Alice Smith"],
            )
            registry.upsert(
                title="Shared chemistry title",
                doi="10.1000/b",
                year="2025",
                authors=["Alice Smith"],
            )

            self.assertEqual(registry.count(), 2)
            self.assertIsNotNone(registry.find(doi="10.1000/a"))
            self.assertIsNotNone(registry.find(doi="10.1000/b"))
            self.assertIsNone(registry.find(title="Shared chemistry title"))

    def test_web_page_never_merges_into_verified_same_title_paper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = PaperRegistry(tmp)
            academic, _ = registry.upsert(
                title="Identical visible title",
                doi="10.1000/evidence",
                source="crossref",
                verification_status="verified_doi",
                full_text_status="metadata_only",
            )
            web, _ = registry.upsert(
                title="Identical visible title",
                source="web:tavily",
                url="https://example.com/evidence",
                verification_status="web_unverified",
                full_text_status="parsed",
                corpus_file="/kb/web-page.json",
            )

            self.assertEqual(registry.count(), 2)
            self.assertNotEqual(academic["paper_id"], web["paper_id"])
            academic_reloaded = registry.find(doi="10.1000/evidence")
            self.assertEqual(
                academic_reloaded["verification_status"], "verified_doi"
            )
            self.assertEqual(
                academic_reloaded["full_text_status"], "metadata_only"
            )
            self.assertEqual(academic_reloaded["corpus_files"], [])
            web_reloaded = registry.find(
                title="Identical visible title",
                source="web:tavily",
                verification_status="web_unverified",
            )
            self.assertEqual(web_reloaded["corpus_files"], ["/kb/web-page.json"])


class LiteratureAcquisitionTest(unittest.TestCase):
    def test_bootstrap_acquisition_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "kb"
            kb_dir.mkdir()
            client = FakeClient()
            acquisition = LiteratureAcquisition(
                kb_dir=kb_dir,
                campaign_id="cmp_lit",
                client=client,
            )
            reference_inputs = [
                {
                    "raw": SEED_PAPER.doi,
                    "kind": "doi",
                    "doi": SEED_PAPER.doi,
                    "status": "pending_resolution",
                }
            ]

            summary = acquisition.acquire_for_bootstrap(QUERY, reference_inputs)

            self.assertEqual(len(summary["seeds"]), 1)
            self.assertEqual(summary["seeds"][0]["verification_status"], "verified_doi")
            self.assertEqual(reference_inputs[0]["status"], "resolved")
            # relevant ref kept once (duplicate citation deduped); NLP paper dropped
            self.assertEqual(summary["snowball_kept"], 1)
            self.assertEqual(summary["keyword_kept"], 1)
            self.assertEqual(summary["errors"], [])

            written = [Path(path) for path in summary["written_files"]]
            self.assertEqual(len(written), 3)  # seed + relevant + keyword
            for path in written:
                self.assertTrue(path.exists())
                record = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("文献题目", record)

            registry = PaperRegistry(kb_dir)
            self.assertEqual(registry.count(), 3)
            papers = registry.campaign_papers("cmp_lit")
            roles = {
                record["title"]: record["campaigns"]["cmp_lit"]["role"]
                for record in papers
            }
            self.assertEqual(roles[SEED_PAPER.title], "seed")
            self.assertEqual(roles[RELEVANT_REF.title], "snowball")
            self.assertEqual(roles[KEYWORD_PAPER.title], "keyword")

    def test_repair_acquisition_tags_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            acquisition = LiteratureAcquisition(
                kb_dir=tmp,
                campaign_id="cmp_lit",
                client=client,
            )
            summary = acquisition.acquire_for_repair(
                ["NiFe PBA activation anomaly"],
                stage="电化学活化 stage",
            )
            self.assertEqual(summary["kept"], 1)
            registry = PaperRegistry(tmp)
            record = registry.find(doi=KEYWORD_PAPER.doi)
            self.assertIsNotNone(record)
            self.assertEqual(
                record["campaigns"]["cmp_lit"]["stage"], "电化学活化 stage"
            )
            self.assertEqual(record["campaigns"]["cmp_lit"]["role"], "b2_repair")

    def test_web_only_mode_does_not_call_scholarly_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            web = FakeWebClient()
            acquisition = LiteratureAcquisition(
                kb_dir=tmp,
                campaign_id="cmp_web_only",
                client=client,
                web_client=web,
                enable_web_search=True,
                enable_scholarly_search=False,
            )

            summary = acquisition.acquire_for_bootstrap(QUERY, [])

            self.assertEqual(client.calls, [])
            self.assertEqual(summary["keyword_kept"], 0)
            self.assertEqual(summary["web_kept"], 1)
            self.assertEqual(web.search_calls, [QUERY])
            self.assertEqual(len(web.fetch_calls), 1)

    def test_existing_paper_does_not_reuse_previous_download_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = PaperRegistry(tmp)
            registry.upsert(
                title="Paper B",
                doi="10.1000/b",
                corpus_file="/kb/b.json",
            )
            acquisition = LiteratureAcquisition(
                kb_dir=tmp,
                campaign_id="cmp_b",
                client=FakeClient(),
                ingestion=StaleIngestion(),  # type: ignore[arg-type]
                registry=registry,
            )

            acquisition._archive_paper(
                ExternalPaper(title="Paper B", doi="10.1000/b"),
                role="keyword",
            )

            record = PaperRegistry(tmp).find(doi="10.1000/b")
            self.assertEqual(record["pdf_files"], [])
            self.assertEqual(record["download_attempts"], [])

    def test_same_title_without_ids_archives_distinct_year_author_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            acquisition = LiteratureAcquisition(
                kb_dir=tmp,
                campaign_id="cmp_same_title",
                client=FakeClient(),
            )
            first = ExternalPaper(
                title="Shared title without identifiers",
                abstract="First paper with enough metadata for the local record.",
                year="2024",
                authors=["Alice Smith"],
            )
            second = ExternalPaper(
                title="Shared title without identifiers",
                abstract="Second paper with distinct authorship and publication year.",
                year="2025",
                authors=["Bob Jones"],
            )

            first_paths, _ = acquisition._archive_paper(first, role="keyword")
            second_paths, _ = acquisition._archive_paper(second, role="keyword")

            self.assertEqual(len(first_paths), 1)
            self.assertEqual(len(second_paths), 1)
            self.assertNotEqual(first_paths[0], second_paths[0])
            registry = PaperRegistry(tmp)
            first_record = registry.find(
                title=first.title,
                year=first.year,
                authors=first.authors,
            )
            second_record = registry.find(
                title=second.title,
                year=second.year,
                authors=second.authors,
            )
            self.assertEqual(len(first_record["corpus_files"]), 1)
            self.assertEqual(len(second_record["corpus_files"]), 1)


class ResearchAgentAcquisitionIntegrationTest(unittest.TestCase):
    def test_b1_auto_acquisition_with_pending_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "kb"
            kb_dir.mkdir()
            client = FakeClient()
            agent = ResearchAgent(
                model=None,
                use_llm=False,
                knowledge_base_dir=str(kb_dir),
                literature_client=client,
                enable_web_search=False,
            )

            state = agent.run(
                event_type="bootstrap",
                query=QUERY,
                campaign_id="cmp_int",
                reference_inputs=[
                    {
                        "raw": SEED_PAPER.doi,
                        "kind": "doi",
                        "doi": SEED_PAPER.doi,
                        "status": "pending_resolution",
                    }
                ],
            )

            self.assertEqual(state.status, "completed")
            self.assertEqual(len(state.seed_papers), 1)
            self.assertEqual(state.seed_papers[0]["title"], SEED_PAPER.title)
            self.assertEqual(state.reference_inputs[0]["status"], "resolved")
            self.assertTrue(
                any("literature acquisition completed" in log for log in state.logs)
            )
            corpus_files = list(kb_dir.glob("*.json"))
            self.assertGreaterEqual(len(corpus_files), 3)
            # freshly ingested seed is retrievable within the same run
            hit_titles = [hit.title for hit in state.knowledge_hits]
            self.assertTrue(
                any("Prussian Blue" in title or "PBA" in title for title in hit_titles),
                hit_titles,
            )

    def test_b1_without_references_uses_default_online_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            agent = ResearchAgent(
                model=None,
                use_llm=False,
                knowledge_base_dir=tmp,
                literature_client=client,
                enable_web_search=False,
            )
            state = agent.run(event_type="bootstrap", query=QUERY)

            self.assertEqual(state.status, "completed")
            self.assertTrue(
                any(call.startswith("search:") for call in client.calls),
                client.calls,
            )
            self.assertEqual(state.seed_papers, [])

    def test_b1_survives_broken_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = BrokenClient()
            agent = ResearchAgent(
                model=None,
                use_llm=False,
                knowledge_base_dir=tmp,
                literature_client=client,
                enable_web_search=False,
            )
            state = agent.run(
                event_type="bootstrap",
                query=QUERY,
                reference_inputs=[
                    {
                        "raw": "10.1000/seed",
                        "kind": "doi",
                        "doi": "10.1000/seed",
                        "status": "pending_resolution",
                    }
                ],
            )

            self.assertEqual(state.status, "completed")
            self.assertEqual(state.seed_papers, [])
            self.assertEqual(state.reference_inputs[0]["status"], "resolution_failed")
            self.assertTrue(
                any("literature acquisition" in log for log in state.logs)
            )

    def test_web_only_agent_run_fetches_web_without_scholarly_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            web = FakeWebClient()
            agent = ResearchAgent(
                model=None,
                use_llm=False,
                knowledge_base_dir=tmp,
                enable_online_literature=False,
                literature_client=client,
                enable_web_search=True,
                web_search_client=web,
            )

            state = agent.run(event_type="bootstrap", query=QUERY)

            self.assertEqual(state.status, "completed")
            self.assertEqual(client.calls, [])
            self.assertEqual(web.search_calls, [QUERY])
            self.assertTrue(
                any("web_kept=1" in log for log in state.logs),
                state.logs,
            )


if __name__ == "__main__":
    unittest.main()

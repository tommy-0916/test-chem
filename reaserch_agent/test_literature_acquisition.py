"""Unit tests for literature acquisition, registry, and B1 integration (P3)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import List

from reaserch_agent.tools.ingestion import ExternalPaper
from reaserch_agent.tools.literature_acquisition import (
    LiteratureAcquisition,
    token_overlap_relevance,
)
from reaserch_agent.tools.paper_registry import PaperRegistry
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


class RelevanceTest(unittest.TestCase):
    def test_relevant_paper_scores_above_irrelevant(self) -> None:
        anchor = QUERY + " " + SEED_PAPER.title
        relevant = token_overlap_relevance(RELEVANT_REF, anchor)
        irrelevant = token_overlap_relevance(IRRELEVANT_REF, anchor)
        self.assertGreater(relevant, 0.2)
        self.assertLess(irrelevant, 0.1)


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

    def test_b1_without_references_skips_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            agent = ResearchAgent(
                model=None,
                use_llm=False,
                knowledge_base_dir=tmp,
                literature_client=client,
            )
            state = agent.run(event_type="bootstrap", query=QUERY)

            self.assertEqual(state.status, "completed")
            self.assertEqual(client.calls, [])
            self.assertEqual(state.seed_papers, [])

    def test_b1_survives_broken_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = BrokenClient()
            agent = ResearchAgent(
                model=None,
                use_llm=False,
                knowledge_base_dir=tmp,
                literature_client=client,
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


if __name__ == "__main__":
    unittest.main()

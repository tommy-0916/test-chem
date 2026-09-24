"""Tests for knowledge-base ingestion."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from reaserch_agent.tools import (
    ExternalKnowledgeClient,
    ExternalPaper,
    KnowledgeIngestion,
    KnowledgeQuery,
)


class KnowledgeIngestionTests(unittest.TestCase):
    def test_ingests_text_file_to_searchable_json(self) -> None:
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as output_dir:
            source = Path(source_dir) / "pba_protocol.txt"
            source.write_text(
                "\n".join(
                    [
                        "Aqueous potassium Prussian blue cathode protocol",
                        "K4Fe(CN)6 was dissolved in 10 mL water and stirred for 30 min.",
                        "The product was washed with water three times and dried at 60 C overnight.",
                        "The cathode delivered 120 mAh g-1 after 100 cycles.",
                    ]
                ),
                encoding="utf-8",
            )

            written = KnowledgeIngestion(output_dir).ingest_path(source)

            self.assertEqual(len(written), 1)
            payload = json.loads(written[0].read_text(encoding="utf-8"))
            self.assertEqual(
                payload["文献题目"],
                "Aqueous potassium Prussian blue cathode protocol",
            )
            self.assertTrue(payload["2. 具体的合成步骤"]["参数列表"])

            hits = KnowledgeQuery(corpus_dir=output_dir).search(
                ["K4Fe(CN)6 water stirred cathode"]
            )
            self.assertTrue(hits)
            self.assertIn("Prussian blue cathode", hits[0].title)

    def test_external_paper_metadata_uses_current_schema(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            paper = ExternalPaper(
                title="Prussian Blue Analogue Potassium Cathode",
                abstract=(
                    "The material was prepared by dissolving K4Fe(CN)6 in 20 mL water, "
                    "stirring at room temperature, washing, and drying at 70 C."
                ),
                source="arxiv",
                source_id="1234.5678",
                url="https://arxiv.org/abs/1234.5678",
            )

            written = KnowledgeIngestion(output_dir).ingest_external_papers([paper])

            self.assertEqual(len(written), 1)
            payload = json.loads(written[0].read_text(encoding="utf-8"))
            self.assertEqual(
                payload["_ingestion_metadata"]["source"],
                "arxiv",
            )
            self.assertTrue(payload["2. 具体的合成步骤"]["描述性总结"])

    def test_concurrent_same_identity_records_do_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            ingestion = KnowledgeIngestion(output_dir)
            barrier = threading.Barrier(2)

            def write(marker: str) -> Path:
                record = ingestion.record_from_text(
                    title="Concurrent shared title",
                    text=f"Distinct chemistry record marker {marker} " * 10,
                    source_path="shared-source",
                    source_type="test",
                )
                record["marker"] = marker
                barrier.wait(timeout=2)
                return ingestion.write_record(record)

            with ThreadPoolExecutor(max_workers=2) as pool:
                paths = list(pool.map(write, ["A", "B"]))

            self.assertEqual(len(set(paths)), 2)
            self.assertEqual(
                {
                    json.loads(path.read_text(encoding="utf-8"))["marker"]
                    for path in paths
                },
                {"A", "B"},
            )


class ExternalKnowledgeClientTests(unittest.TestCase):
    def test_search_continues_after_provider_failure_and_records_attempts(self) -> None:
        client = ExternalKnowledgeClient()
        openalex_paper = ExternalPaper(
            title="OpenAlex fallback result",
            source="openalex",
            source_id="W1",
            doi="10.1000/fallback",
        )
        with mock.patch.object(
            client,
            "search_semantic_scholar",
            side_effect=ConnectionError("rate limited"),
        ), mock.patch.object(
            client,
            "search_openalex",
            return_value=[openalex_paper],
        ):
            papers = client.search(
                "fallback query",
                sources=["semantic_scholar", "openalex"],
                max_results=2,
            )

        self.assertEqual([paper.title for paper in papers], [openalex_paper.title])
        self.assertEqual(client.last_attempts[0]["status"], "error")
        self.assertEqual(client.last_attempts[1]["status"], "success")
        self.assertIn("rate limited", client.last_errors[0])

    def test_lookup_doi_falls_back_from_crossref_to_semantic_scholar(self) -> None:
        client = ExternalKnowledgeClient()
        fallback = ExternalPaper(
            title="Semantic Scholar DOI result",
            source="semantic_scholar",
            source_id="s2-1",
            doi="10.1000/fallback",
        )
        with mock.patch.object(
            client,
            "_lookup_crossref_doi",
            side_effect=ConnectionError("crossref unavailable"),
        ), mock.patch.object(
            client,
            "_lookup_s2_identifier",
            return_value=fallback,
        ) as s2_lookup, mock.patch.object(
            client,
            "_lookup_openalex_doi",
        ) as openalex_lookup:
            paper = client.lookup_doi("https://doi.org/10.1000/fallback")

        self.assertIs(paper, fallback)
        s2_lookup.assert_called_once_with("DOI:10.1000/fallback")
        openalex_lookup.assert_not_called()
        self.assertIn("crossref unavailable", client.last_errors[0])

    def test_semantic_scholar_retries_without_rejected_key(self) -> None:
        client = ExternalKnowledgeClient(semantic_scholar_api_key="rejected")
        forbidden = urllib.error.HTTPError(
            "https://api.semanticscholar.org",
            403,
            "Forbidden",
            None,
            None,
        )
        payload = json.dumps(
            {
                "data": [
                    {
                        "paperId": "s2-1",
                        "title": "Anonymous S2 fallback",
                        "externalIds": {},
                    }
                ]
            }
        )
        with mock.patch.object(
            client,
            "_get_text",
            side_effect=[forbidden, payload],
        ) as get_text:
            papers = client.search_semantic_scholar("fallback", max_results=1)

        self.assertEqual(papers[0].title, "Anonymous S2 fallback")
        self.assertIn("x-api-key", get_text.call_args_list[0].kwargs["headers"])
        self.assertEqual(get_text.call_args_list[1].kwargs, {})
        self.assertIn("anonymous retry succeeded", client.last_errors[-1])

    def test_same_title_with_conflicting_dois_is_not_merged(self) -> None:
        client = ExternalKnowledgeClient()
        papers = client._dedupe_papers(
            [
                ExternalPaper(
                    title="Shared title",
                    doi="10.1000/a",
                    year="2025",
                    authors=["Alice Smith"],
                ),
                ExternalPaper(
                    title="Shared title",
                    doi="10.1000/b",
                    year="2025",
                    authors=["Alice Smith"],
                ),
            ]
        )

        self.assertEqual(len(papers), 2)
        self.assertEqual({paper.doi for paper in papers}, {"10.1000/a", "10.1000/b"})

    def test_same_doi_from_multiple_providers_merges_provenance(self) -> None:
        client = ExternalKnowledgeClient()
        papers = client._dedupe_papers(
            [
                ExternalPaper(
                    title="Provider A title",
                    abstract="short",
                    source="crossref",
                    doi="10.1000/shared",
                ),
                ExternalPaper(
                    title="Provider B title",
                    abstract="a much longer abstract from another provider",
                    source="openalex",
                    doi="10.1000/shared",
                ),
            ]
        )

        self.assertEqual(len(papers), 1)
        self.assertIn("much longer", papers[0].abstract)
        self.assertEqual(
            papers[0].raw["discovery_sources"], ["crossref", "openalex"]
        )

    def test_search_interleaves_provider_results(self) -> None:
        client = ExternalKnowledgeClient()
        s2 = [
            ExternalPaper(title="S2 first", source="semantic_scholar"),
            ExternalPaper(title="S2 second", source="semantic_scholar"),
        ]
        openalex = [
            ExternalPaper(title="OpenAlex first", source="openalex"),
            ExternalPaper(title="OpenAlex second", source="openalex"),
        ]
        with mock.patch.object(
            client, "search_semantic_scholar", return_value=s2
        ), mock.patch.object(client, "search_openalex", return_value=openalex):
            papers = client.search(
                "query",
                sources=["semantic_scholar", "openalex"],
                max_results=2,
            )

        self.assertEqual(
            [paper.title for paper in papers],
            ["S2 first", "OpenAlex first", "S2 second", "OpenAlex second"],
        )

    def test_title_match_rejects_generic_prefix(self) -> None:
        client = ExternalKnowledgeClient()
        candidate = ExternalPaper(title="Prussian blue")

        matched = client._best_title_match(
            "Prussian blue analogues for aqueous potassium ion batteries",
            [candidate],
        )

        self.assertIsNone(matched)

    def test_title_match_accepts_punctuation_and_short_subtitle_difference(self) -> None:
        client = ExternalKnowledgeClient()
        candidate = ExternalPaper(
            title=(
                "Prussian Blue Analogues for Aqueous Potassium-Ion Batteries: "
                "A Review"
            )
        )

        matched = client._best_title_match(
            "Prussian-blue analogues for aqueous potassium ion batteries",
            [candidate],
        )

        self.assertIs(matched, candidate)


if __name__ == "__main__":
    unittest.main()

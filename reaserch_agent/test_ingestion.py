"""Tests for knowledge-base ingestion."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reaserch_agent.tools import ExternalPaper, KnowledgeIngestion, KnowledgeQuery


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


if __name__ == "__main__":
    unittest.main()

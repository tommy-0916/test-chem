"""Focused tests for ranked local corpus retrieval."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reaserch_agent.tools.corpus_search import LocalExperimentCorpus


def _paper(title: str, summary: str, parameter: str) -> dict:
    return {
        "文献题目": title,
        "1. 解决的问题": "electrode preparation",
        "2. 具体的合成步骤": {
            "描述性总结": summary,
            "参数列表": [
                {"步骤序号": 1, "操作": "mix", "试剂/对象": "sample", "参数": parameter}
            ],
        },
        "3. 性能": [],
    }


def _write_paper(directory: Path, name: str, paper: dict, *, reverse_keys: bool = False) -> Path:
    path = directory / name
    if reverse_keys:
        paper = dict(reversed(list(paper.items())))
    path.write_text(json.dumps(paper, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class LocalExperimentCorpusDedupTests(unittest.TestCase):
    def test_exact_json_copies_do_not_crowd_out_other_ranked_papers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            duplicate = _paper("NiFe electrode", "NiFe synthesis", "10 mL")
            duplicate["_ingestion_metadata"] = {
                "source_path": "https://example.org/paper/1",
                "ingested_at": "2026-09-15T00:00:00Z",
            }
            first = _write_paper(directory, "a.json", duplicate)
            later_copy = {**duplicate, "_ingestion_metadata": {
                "source_path": "https://mirror.example.org/paper/1",
                "ingested_at": "2026-09-24T00:00:00Z",
            }}
            _write_paper(directory, "b.json", later_copy, reverse_keys=True)
            other = _write_paper(
                directory,
                "c.json",
                _paper("Other electrode", "NiFe synthesis variant", "20 mL"),
            )

            hits = LocalExperimentCorpus(directory).search(["NiFe"], top_k=2)

            self.assertEqual([hit.file_path for hit in hits], [str(first), str(other)])
            self.assertGreaterEqual(hits[0].score, hits[1].score)

    def test_same_title_distinct_experiment_content_is_not_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            first = _write_paper(directory, "a.json", _paper("NiFe electrode", "Same summary", "10 mL"))
            second = _write_paper(directory, "b.json", _paper("NiFe electrode", "Same summary", "20 mL"))

            hits = LocalExperimentCorpus(directory).search(["NiFe"], top_k=2)

            self.assertEqual({hit.file_path for hit in hits}, {str(first), str(second)})
            self.assertEqual({hit.steps[0]["参数"] for hit in hits}, {"10 mL", "20 mL"})


if __name__ == "__main__":
    unittest.main()

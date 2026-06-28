"""Tests for the two-layer chem memory implementation."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from reaserch_agent.memory import ChemMemoryLayer, LayeredChemMemory
from reaserch_agent.tools import KnowledgeQuery, MemoryQuery
from reaserch_agent.workflow import ResearchAgent


class ChemMemoryLayerTests(unittest.TestCase):
    def test_layered_memory_add_search_update_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = LayeredChemMemory(root_dir=tmpdir)

            exp_result = memory.add_experiment(
                "NiFe PBA OER activation showed high overpotential after alkaline aging.",
                metadata={
                    "title": "NiFe PBA activation anomaly",
                    "query": "NiFe PBA OER",
                    "result": "high overpotential",
                },
                run_id="run-1",
            )
            lit_result = memory.add_literature(
                "Prussian blue analogue potassium cathodes use aqueous K-ion electrolyte.",
                metadata={
                    "title": "PBA potassium cathode paper",
                    "source_path": "paper.json",
                },
            )

            exp_id = exp_result["results"][0]["id"]
            lit_id = lit_result["results"][0]["id"]

            exp_hits = memory.search_experiments("NiFe OER overpotential")
            lit_hits = memory.search_literature("potassium cathode electrolyte")

            self.assertEqual(exp_hits["results"][0]["id"], exp_id)
            self.assertEqual(exp_hits["results"][0]["layer"], ChemMemoryLayer.EXPERIMENT)
            self.assertEqual(lit_hits["results"][0]["id"], lit_id)
            self.assertEqual(lit_hits["results"][0]["layer"], ChemMemoryLayer.LITERATURE)

            memory.update(exp_id, data="NiFe PBA OER activation recovered after lower alkaline aging time.")
            history = memory.history(exp_id)

            self.assertEqual([item["event"] for item in history], ["ADD", "UPDATE"])
            self.assertIn("lower alkaline aging", memory.get(exp_id)["memory"])

    def test_query_tools_use_separate_experiment_and_literature_layers(self) -> None:
        old_store_dir = os.environ.get("RESEARCH_MEMORY_STORE_DIR")
        try:
            with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as corpus_dir:
                os.environ["RESEARCH_MEMORY_STORE_DIR"] = tmpdir
                memory = LayeredChemMemory(root_dir=tmpdir)
                memory.add_experiment(
                    "Repeated PBA coprecipitation memory: slow feed caused broad XRD peaks.",
                    metadata={"title": "coprecipitation broad XRD memory"},
                )
                memory.add_literature(
                    "Literature protocol: potassium manganese hexacyanoferrate cathode uses KNO3 electrolyte.",
                    metadata={"title": "KMHCF cathode literature"},
                )

                memory_hits = MemoryQuery(corpus_dir=corpus_dir).search(["slow feed broad XRD"])
                knowledge_hits = KnowledgeQuery(corpus_dir=corpus_dir).search(["KMHCF KNO3 electrolyte"])

                self.assertEqual(memory_hits[0].title, "coprecipitation broad XRD memory")
                self.assertEqual(knowledge_hits[0].title, "KMHCF cathode literature")
        finally:
            if old_store_dir is None:
                os.environ.pop("RESEARCH_MEMORY_STORE_DIR", None)
            else:
                os.environ["RESEARCH_MEMORY_STORE_DIR"] = old_store_dir

    def test_research_agent_memory_uses_experiment_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = LayeredChemMemory(root_dir=tmpdir)
            memory.add_experiment(
                "Historical NiFe PBA OER activation memory: alkaline aging improved current density.",
                metadata={"title": "NiFe OER activation memory"},
            )
            structured_outputs_dir = Path(__file__).resolve().parents[1] / "structured_outputs"
            agent = ResearchAgent(
                model=None,
                use_llm=False,
                knowledge_base_dir=str(structured_outputs_dir),
                memory_dir=tmpdir,
                enable_memory=True,
            )

            state = agent.run(
                event_type="bootstrap",
                query="设计 NiFe PBA OER activation 首轮实验",
            )

            self.assertTrue(
                any(hit.title == "NiFe OER activation memory" for hit in state.memory_hits)
            )


if __name__ == "__main__":
    unittest.main()

"""Unit tests for evidence-grade provenance (P6).

Covers: page-tagged extraction, registry provenance on protocols, honest
macro-step source annotation, evidence packet in the LLM context, and
evidence_refs flowing into the plan ledger and trajectory memory.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from reaserch_agent.memory import ChemMemoryLayer, LayeredChemMemory
from reaserch_agent.tools.ingestion import KnowledgeIngestion
from reaserch_agent.tools.paper_registry import PaperRegistry
from reaserch_agent.workflow import ResearchAgent

SAMPLE_TITLE = "测试 K-PBA 合成路线"
SAMPLE_CORPUS_RECORD = {
    "文献题目": SAMPLE_TITLE,
    "1. 解决的问题": "低成本钾离子电池正极材料的可控合成。",
    "2. 具体的合成步骤": {
        "描述性总结": "共沉淀法合成 K-PBA 粉末并离线 XRD 表征。",
        "参数列表": [
            {
                "步骤序号": 1,
                "操作": "配制前驱体溶液",
                "试剂/对象": "K4Fe(CN)6·3H2O、去离子水",
                "参数": "0.5 mmol K4Fe(CN)6 溶于 10 mL 去离子水",
            },
            {
                "步骤序号": 2,
                "操作": "共沉淀反应",
                "试剂/对象": "FeCl3 溶液",
                "参数": "室温搅拌 30 min, 陈化 12 h",
            },
        ],
    },
    "3. 性能": [],
}

PAGE_MARKED_TEXT = (
    "[p.1]\n"
    "Introduction discusses Prussian blue analogues for potassium storage in detail.\n"
    "[p.2]\n"
    "0.5 mmol K4Fe(CN)6 was dissolved in 10 mL deionized water and stirred for 10 min.\n"
    "[p.3]\n"
    "The precipitate was centrifuged, washed 3 times, and dried at 60 C overnight in vacuum.\n"
)


class PageProvenanceTest(unittest.TestCase):
    def test_steps_inherit_page_numbers_from_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ingestion = KnowledgeIngestion(tmp)
            record = ingestion.record_from_text(
                title="Paged synthesis paper",
                text=PAGE_MARKED_TEXT,
                source_path="/tmp/fake.pdf",
                source_type="pdf",
            )
            steps = record["2. 具体的合成步骤"]["参数列表"]
            self.assertTrue(steps)
            pages = {step.get("page") for step in steps}
            self.assertTrue(pages & {2, 3}, steps)
            for step in steps:
                self.assertNotIn("[p.", step["evidence"])

    def test_text_without_markers_has_no_page_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ingestion = KnowledgeIngestion(tmp)
            record = ingestion.record_from_text(
                title="Unmarked paper",
                text=(
                    "0.5 mmol K4Fe(CN)6 was dissolved in 10 mL deionized water "
                    "and stirred for 10 min at room temperature."
                ),
                source_path="/tmp/fake.txt",
                source_type="txt",
            )
            for step in record["2. 具体的合成步骤"]["参数列表"]:
                self.assertNotIn("page", step)


class ProvenanceWorkflowTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.kb_dir = root / "kb"
        self.kb_dir.mkdir()
        (self.kb_dir / "sample.json").write_text(
            json.dumps(SAMPLE_CORPUS_RECORD, ensure_ascii=False),
            encoding="utf-8",
        )
        # register the corpus paper with verified identity but metadata-only text
        registry = PaperRegistry(self.kb_dir)
        registry.upsert(
            title=SAMPLE_TITLE,
            doi="10.1000/kpba",
            verification_status="verified_doi",
            full_text_status="metadata_only",
            campaign_id="cmp_prior",
            role="seed",
        )
        self.memory_root = root / "memstore"
        self._previous_env = os.environ.get("RESEARCH_MEMORY_STORE_DIR")
        os.environ["RESEARCH_MEMORY_STORE_DIR"] = str(self.memory_root)
        self.agent = ResearchAgent(
            model=None,
            use_llm=False,
            knowledge_base_dir=str(self.kb_dir),
            enable_memory=True,
            enable_online_literature=False,
            enable_web_search=False,
        )

    def tearDown(self) -> None:
        if self._previous_env is None:
            os.environ.pop("RESEARCH_MEMORY_STORE_DIR", None)
        else:
            os.environ["RESEARCH_MEMORY_STORE_DIR"] = self._previous_env
        self._tmp.cleanup()

    def _bootstrap(self, campaign_id: str = "cmp_evidence"):
        return self.agent.run(
            event_type="bootstrap",
            query="合成 K-PBA 粉末并通过离线 XRD 确认目标相",
            campaign_id=campaign_id,
        )


class ProtocolProvenanceTest(ProvenanceWorkflowTestBase):
    def test_protocols_carry_registry_identity(self) -> None:
        state = self._bootstrap()

        self.assertEqual(state.status, "completed")
        self.assertTrue(state.extracted_protocols)
        protocol = state.extracted_protocols[0]
        self.assertEqual(protocol["paper_id"], "doi_10_1000_kpba")
        self.assertEqual(protocol["verification_status"], "verified_doi")
        self.assertEqual(protocol["full_text_status"], "metadata_only")

    def test_unregistered_protocol_is_labelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp) / "kb2"
            kb.mkdir()
            (kb / "sample.json").write_text(
                json.dumps(SAMPLE_CORPUS_RECORD, ensure_ascii=False),
                encoding="utf-8",
            )
            agent = ResearchAgent(
                model=None,
                use_llm=False,
                knowledge_base_dir=str(kb),
                enable_online_literature=False,
                enable_web_search=False,
            )
            state = agent.run(
                event_type="bootstrap",
                query="合成 K-PBA 粉末并通过离线 XRD 确认目标相",
            )
            self.assertEqual(
                state.extracted_protocols[0]["verification_status"],
                "unregistered_local",
            )


class MacroPlanSourceTest(ProvenanceWorkflowTestBase):
    def test_every_macro_step_has_source_annotation(self) -> None:
        state = self._bootstrap()

        self.assertTrue(state.macro_plan)
        for step in state.macro_plan:
            source = step.get("来源", "")
            self.assertTrue(source, step)
            self.assertTrue(
                source.startswith("protocol:") or source.startswith("agent补全"),
                source,
            )
        # handoff contract keys intact (plus additive 来源)
        for step in state.device_adaptation_handoff["待执行 macro plan"]:
            for key in ("步骤序号", "操作", "试剂/对象", "参数"):
                self.assertIn(key, step)

    def test_match_step_to_protocol_cites_page(self) -> None:
        protocol = {
            "paper_id": "doi_10_1000_kpba",
            "source_title": SAMPLE_TITLE,
            "steps": [
                {
                    "操作": "配制前驱体溶液",
                    "试剂/对象": "K4Fe(CN)6·3H2O、去离子水",
                    "参数": "0.5 mmol K4Fe(CN)6 溶于 10 mL 去离子水",
                    "page": 4,
                }
            ],
        }
        step = {
            "操作": "配制前驱体溶液",
            "试剂/对象": "K4Fe(CN)6·3H2O、去离子水",
            "参数": "0.5 mmol K4Fe(CN)6 溶于 10 mL 去离子水",
        }
        citation = self.agent._match_step_to_protocol(step, [protocol])
        self.assertEqual(citation, "protocol: doi_10_1000_kpba p.4")

        unrelated = {
            "操作": "组装全电池",
            "试剂/对象": "隔膜、电解液",
            "参数": "手套箱内组装 2032 扣式电池",
        }
        self.assertEqual(self.agent._match_step_to_protocol(unrelated, [protocol]), "")

        unsafe_protocol = dict(protocol)
        unsafe_protocol["verification_status"] = "web_unverified"
        unsafe_protocol["full_text_status"] = "parsed"
        self.assertEqual(
            self.agent._match_step_to_protocol(step, [unsafe_protocol]),
            "",
        )

    def test_normalize_macro_plan_preserves_source(self) -> None:
        normalized = self.agent._normalize_macro_plan(
            [
                {
                    "步骤序号": 1,
                    "操作": "配液",
                    "试剂/对象": "K4Fe(CN)6",
                    "参数": "0.5 mmol in 10 mL",
                    "来源": "protocol: doi_x p.2",
                },
                {
                    "步骤序号": 2,
                    "操作": "搅拌",
                    "试剂/对象": "反应液",
                    "参数": "600 rpm 30 min",
                },
            ]
        )
        self.assertEqual(normalized[0]["来源"], "protocol: doi_x p.2")
        self.assertNotIn("来源", normalized[1])


class EvidencePacketAndRefsTest(ProvenanceWorkflowTestBase):
    def test_compact_context_contains_evidence_packet(self) -> None:
        state = self._bootstrap()
        context = json.loads(self.agent._compact_state_context(state, "unit_task"))

        self.assertIn("evidence_packet", context)
        packet = context["evidence_packet"]
        self.assertEqual(
            packet["sources"][0]["verification_status"], "verified_doi"
        )
        self.assertTrue(
            any("仅有元数据" in gap for gap in packet["known_gaps"]),
            packet["known_gaps"],
        )
        self.assertIn("citation_rule", packet)

    def test_evidence_refs_flow_into_ledger_and_memory(self) -> None:
        PaperRegistry(self.kb_dir).upsert(
            title=SAMPLE_TITLE,
            doi="10.1000/kpba",
            verification_status="verified_doi",
            full_text_status="parsed",
            corpus_file=str(self.kb_dir / "sample.json"),
        )
        state = self._bootstrap("cmp_refs")

        record = state.plan_revisions[-1]
        self.assertTrue(record["evidence_refs"])
        self.assertIn("doi_10_1000_kpba", record["evidence_refs"])

        memory = LayeredChemMemory(root_dir=self.memory_root)
        nodes = memory.get_all(
            layers=[ChemMemoryLayer.EXPERIMENT],
            filters={"campaign_id": "cmp_refs"},
            top_k=10,
        ).get("results", [])
        self.assertTrue(nodes)
        payload = json.loads(nodes[0]["memory"])
        self.assertIn("evidence_refs", payload)
        self.assertIn("doi_10_1000_kpba", payload["evidence_refs"])

    def test_web_and_parse_failed_sources_are_explicit_known_gaps(self) -> None:
        state = self._bootstrap()
        state.extracted_protocols = [
            {
                "source_title": "Unverified web page",
                "paper_id": "web_title_x",
                "verification_status": "web_unverified",
                "full_text_status": "parsed",
                "source_file": "/kb/web.json",
                "steps": [],
            },
            {
                "source_title": "Broken DOI PDF",
                "paper_id": "doi_broken",
                "verification_status": "verified_doi",
                "full_text_status": "parse_failed",
                "source_file": "/kb/broken.json",
                "steps": [],
            },
        ]

        packet = self.agent._evidence_packet(state)

        self.assertTrue(
            any("网页内容未经论文身份验证" in gap for gap in packet["known_gaps"])
        )
        self.assertTrue(
            any("全文状态为 parse_failed" in gap for gap in packet["known_gaps"])
        )


if __name__ == "__main__":
    unittest.main()

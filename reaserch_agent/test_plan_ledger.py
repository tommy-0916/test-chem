"""Unit tests for campaign skeleton and plan-version ledger (P1)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from reaserch_agent.plan_ledger import (
    PlanLedger,
    default_ledger_path,
    generate_campaign_id,
)
from reaserch_agent.run_research_agent import resolve_reference_inputs
from reaserch_agent.tools.ingestion import classify_reference
from reaserch_agent.workflow import ResearchAgent


SAMPLE_CORPUS_RECORD = {
    "文献题目": "测试 K-PBA 合成路线",
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
    "3. 性能": [{"指标": "首圈容量", "数值": "120 mAh g-1"}],
}


def write_sample_kb(root: Path) -> Path:
    kb_dir = root / "chem_kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    (kb_dir / "sample_kpba.json").write_text(
        json.dumps(SAMPLE_CORPUS_RECORD, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return kb_dir


class PlanLedgerFileTest(unittest.TestCase):
    def test_append_and_read_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "plan_versions.jsonl"
            ledger = PlanLedger(path)
            ledger.append({"plan_version": 1, "event": "initial", "理由": "首个计划"})
            ledger.append({"plan_version": 2, "event": "revised"})

            records = PlanLedger(path).read_all()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["event"], "initial")
            self.assertEqual(records[0]["理由"], "首个计划")
            self.assertEqual(records[1]["plan_version"], 2)

    def test_read_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PlanLedger(Path(tmp) / "absent.jsonl")
            self.assertEqual(ledger.read_all(), [])

    def test_default_ledger_path_layout(self) -> None:
        path = default_ledger_path("cmp_20260708_abcd1234", campaigns_root="/tmp/camps")
        self.assertTrue(str(path).endswith("cmp_20260708_abcd1234/plan_versions.jsonl"))


class CampaignIdTest(unittest.TestCase):
    def test_format_and_determinism(self) -> None:
        now = datetime(2026, 7, 8, 10, 30)
        first = generate_campaign_id("NiFe-PBA 电化学活化", now=now)
        second = generate_campaign_id("NiFe-PBA 电化学活化", now=now)
        other = generate_campaign_id("另一个 query", now=now)

        self.assertRegex(first, r"^cmp_20260708_[0-9a-f]{8}$")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)


class ClassifyReferenceTest(unittest.TestCase):
    def test_doi_forms(self) -> None:
        for raw in [
            "10.1038/s41586-2024-001",
            "doi:10.1021/jacs.3c01234",
            "https://doi.org/10.1002/anie.202300000",
        ]:
            entry = classify_reference(raw)
            self.assertEqual(entry["kind"], "doi", raw)
            self.assertTrue(entry["doi"].startswith("10."), raw)

    def test_arxiv_forms(self) -> None:
        for raw, expected in [
            ("2401.12345", "2401.12345"),
            ("arXiv:2401.12345v2", "2401.12345v2"),
            ("https://arxiv.org/abs/2401.12345", "2401.12345"),
        ]:
            entry = classify_reference(raw)
            self.assertEqual(entry["kind"], "arxiv", raw)
            self.assertEqual(entry["arxiv_id"], expected, raw)

    def test_title_and_empty(self) -> None:
        self.assertEqual(
            classify_reference("High-Entropy Prussian Blue Cathodes")["kind"],
            "title",
        )
        self.assertEqual(classify_reference("  ")["kind"], "empty")

    def test_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "paper.txt"
            file_path.write_text("PBA synthesis notes", encoding="utf-8")
            entry = classify_reference(str(file_path))
            self.assertEqual(entry["kind"], "local_file")
            self.assertEqual(entry["path"], str(file_path.resolve()))


class ResolveReferenceInputsTest(unittest.TestCase):
    def test_local_file_is_ingested_and_others_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "kb"
            kb_dir.mkdir()
            note = root / "notes.txt"
            note.write_text(
                "K-PBA synthesis: 0.5 mmol K4Fe(CN)6 in 10 mL water, stir 10 min.",
                encoding="utf-8",
            )

            entries = resolve_reference_inputs(
                [str(note), "10.1038/s41586-2024-001", "Some PBA paper title"],
                str(kb_dir),
            )

            self.assertEqual(entries[0]["status"], "ingested")
            self.assertEqual(len(entries[0]["written_records"]), 1)
            self.assertTrue(Path(entries[0]["written_records"][0]).exists())
            self.assertEqual(entries[1]["kind"], "doi")
            self.assertEqual(entries[1]["status"], "pending_resolution")
            self.assertEqual(entries[2]["kind"], "title")
            self.assertEqual(entries[2]["status"], "pending_resolution")


class ResearchAgentLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.kb_dir = write_sample_kb(Path(self._tmp.name))
        self.agent = ResearchAgent(
            model=None,
            use_llm=False,
            knowledge_base_dir=str(self.kb_dir),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _bootstrap(self):
        return self.agent.run(
            event_type="bootstrap",
            query="合成 K-PBA 粉末并通过离线 XRD 确认目标相",
            campaign_id="cmp_test_123",
            reference_inputs=[{"raw": "notes.txt", "kind": "local_file", "status": "ingested"}],
        )

    def test_bootstrap_records_initial_plan(self) -> None:
        state = self._bootstrap()

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.campaign_id, "cmp_test_123")
        self.assertEqual(len(state.reference_inputs), 1)
        self.assertEqual(len(state.plan_revisions), 1)

        record = state.plan_revisions[0]
        self.assertEqual(record["event"], "initial")
        self.assertEqual(record["scope"], "full_plan")
        self.assertEqual(record["trigger"], "bootstrap")
        self.assertEqual(record["branch_path"], "B1")
        self.assertEqual(record["campaign_id"], "cmp_test_123")
        self.assertEqual(record["plan_version"], 1)
        self.assertIsNone(record["previous_plan"])
        self.assertTrue(record["new_plan"]["macro_plan"])
        self.assertTrue(record["reason"].strip())

    def test_observation_turn_inherits_campaign_and_records_revision(self) -> None:
        bootstrap_state = self._bootstrap()
        previous = bootstrap_state.debug_snapshot()

        state = self.agent.run(
            event_type="new_observation",
            payload={
                "observation": {
                    "summary": "XRD 结果显示目标相纯相，与计划一致，按计划完成当前 stage 目标",
                }
            },
            previous_state=previous,
        )

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.campaign_id, "cmp_test_123")
        self.assertEqual(len(state.plan_revisions), 2)

        record = state.plan_revisions[-1]
        self.assertEqual(record["plan_version"], 2)
        self.assertEqual(record["trigger"], "observation")
        self.assertIn(record["event"], {"advanced", "closure", "revised"})
        self.assertIsNotNone(record["previous_plan"])
        self.assertTrue(record["previous_plan"]["macro_plan"])
        self.assertTrue(record["observation_summary"])

    def test_device_feasibility_error_records_device_trigger(self) -> None:
        bootstrap_state = self._bootstrap()
        previous = bootstrap_state.debug_snapshot()

        state = self.agent.run(
            event_type="new_observation",
            payload={
                "feedback_type": "device_feasibility_error",
                "status": "feasibility_error",
                "error_package": {
                    "type": "physical_infeasible",
                    "blocking_constraints": ["缺少反应釜，无法执行水热反应"],
                    "unsupported_items": [
                        {
                            "macro_step": 2,
                            "requirement": "水热反应",
                            "reason": "没有反应釜",
                            "suggested_research_revision": "改为常压瓶内合成路线",
                        }
                    ],
                },
                "observation": {"summary": "设备适应层返回 feasibility_error"},
            },
            previous_state=previous,
        )

        self.assertEqual(state.status, "completed")
        record = state.plan_revisions[-1]
        self.assertEqual(record["trigger"], "device_feasibility_error")
        self.assertEqual(record["event"], "revised")
        self.assertEqual(record["scope"], "macro_plan")
        self.assertEqual(record["branch_path"], "B2/device_adaptation")
        self.assertIn("设备阻塞约束", record["reason"])

    def test_b2_without_context_records_abandoned(self) -> None:
        state = self.agent.run(
            event_type="new_observation",
            payload={"observation": {"summary": "无上下文的观测"}},
        )

        self.assertEqual(state.status, "manual_required")
        record = state.plan_revisions[-1]
        self.assertEqual(record["event"], "abandoned")
        self.assertEqual(record["trigger"], "unrecoverable_error")
        self.assertEqual(record["branch_path"], "B2/exception")

    def test_old_state_without_campaign_fields_still_works(self) -> None:
        previous = {
            "event": {"event_type": "bootstrap", "query": "旧任务 K-PBA"},
            "stage_route": ["合成并离线 XRD"],
            "current_stage": "合成并离线 XRD",
            "current_stage_plan": "合成 K-PBA 粉末并送外部 XRD 表征",
            "macro_plan": [
                {
                    "步骤序号": 1,
                    "操作": "配制前驱体",
                    "试剂/对象": "K4Fe(CN)6",
                    "参数": "0.5 mmol in 10 mL",
                }
            ],
        }

        state = self.agent.run(
            event_type="new_observation",
            payload={
                "observation": {
                    "summary": "XRD 结果显示目标相纯相，与计划一致，按计划完成当前 stage 目标",
                }
            },
            previous_state=previous,
        )

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.campaign_id, "")
        self.assertEqual(len(state.plan_revisions), 1)
        self.assertIsNotNone(state.plan_revisions[0]["previous_plan"])


if __name__ == "__main__":
    unittest.main()

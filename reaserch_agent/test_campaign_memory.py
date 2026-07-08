"""Unit tests for campaign trajectory memory, three-layer recall, scoring (P4)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from reaserch_agent.memory import ChemMemoryLayer, LayeredChemMemory
from reaserch_agent.memory.recall import build_campaign_memory_context
from reaserch_agent.memory.scoring import (
    bm25_available,
    build_bm25_boosts,
    tokenize_mixed,
)
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
            }
        ],
    },
    "3. 性能": [],
}

NORMAL_OBSERVATION = {
    "observation": {
        "summary": "XRD 结果显示目标相纯相，与计划一致，按计划完成当前 stage 目标",
    }
}
DEVICE_ERROR_PAYLOAD = {
    "feedback_type": "device_feasibility_error",
    "status": "feasibility_error",
    "error_package": {
        "type": "physical_infeasible",
        "blocking_constraints": ["缺少反应釜，无法执行水热反应"],
    },
    "observation": {"summary": "设备适应层返回 feasibility_error，缺少反应釜"},
}


class ScoringTest(unittest.TestCase):
    def test_tokenizer_keeps_chemical_formulas_whole(self) -> None:
        tokens = tokenize_mixed("0.5 mmol K3Fe(CN)6 in 10 mL 去离子水")
        self.assertIn("k3fe(cn)6", tokens)
        self.assertIn("mmol", tokens)
        # CJK covered either by jieba words or bigram fallback
        self.assertTrue(any("去离" in token or token == "去离子水" for token in tokens))

    def test_bm25_boosts_degrade_gracefully(self) -> None:
        boosts = build_bm25_boosts(
            ["NiFe PBA activation"],
            ["NiFe PBA activation study", "unrelated text about NLP"],
        )
        if bm25_available():
            self.assertIsNotNone(boosts)
            self.assertGreater(boosts[0], boosts[1])
        else:
            self.assertIsNone(boosts)


class CampaignMemoryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.kb_dir = root / "kb"
        self.kb_dir.mkdir()
        (self.kb_dir / "sample.json").write_text(
            json.dumps(SAMPLE_CORPUS_RECORD, ensure_ascii=False),
            encoding="utf-8",
        )
        self.memory_root = root / "memstore"
        self._previous_env = os.environ.get("RESEARCH_MEMORY_STORE_DIR")
        os.environ["RESEARCH_MEMORY_STORE_DIR"] = str(self.memory_root)
        self.agent = ResearchAgent(
            model=None,
            use_llm=False,
            knowledge_base_dir=str(self.kb_dir),
            enable_memory=True,
        )

    def tearDown(self) -> None:
        if self._previous_env is None:
            os.environ.pop("RESEARCH_MEMORY_STORE_DIR", None)
        else:
            os.environ["RESEARCH_MEMORY_STORE_DIR"] = self._previous_env
        self._tmp.cleanup()

    def _bootstrap(self, campaign_id: str = "cmp_mem_test"):
        return self.agent.run(
            event_type="bootstrap",
            query="合成 K-PBA 粉末并通过离线 XRD 确认目标相",
            campaign_id=campaign_id,
        )

    def _memory(self) -> LayeredChemMemory:
        return LayeredChemMemory(root_dir=self.memory_root)

    def _campaign_nodes(self, campaign_id: str, node_type: str | None = None):
        filters = {"campaign_id": campaign_id}
        if node_type:
            filters["node_type"] = node_type
        return self._memory().get_all(
            layers=[ChemMemoryLayer.EXPERIMENT],
            filters=filters,
            top_k=50,
        ).get("results", [])


class TrajectoryNodeTest(CampaignMemoryTestBase):
    def test_nodes_written_per_turn(self) -> None:
        bootstrap_state = self._bootstrap()
        self.assertEqual(
            len(self._campaign_nodes("cmp_mem_test", "bootstrap")), 1
        )

        state = self.agent.run(
            event_type="new_observation",
            payload=NORMAL_OBSERVATION,
            previous_state=bootstrap_state.debug_snapshot(),
        )
        self.assertEqual(state.status, "completed")
        turns = self._campaign_nodes("cmp_mem_test", "observation_turn")
        self.assertEqual(len(turns), 1)
        metadata = turns[0]["metadata"]
        self.assertEqual(metadata["campaign_id"], "cmp_mem_test")
        self.assertEqual(metadata["turn_index"], 2)
        payload = json.loads(turns[0]["memory"])
        self.assertIn("decision_reason", payload)
        self.assertIn("observation_summary", payload)

    def test_stage_rollup_written_on_closure_or_stage_change(self) -> None:
        bootstrap_state = self._bootstrap("cmp_rollup")
        state = self.agent.run(
            event_type="new_observation",
            payload=NORMAL_OBSERVATION,
            previous_state=bootstrap_state.debug_snapshot(),
        )
        self.assertEqual(state.status, "completed")
        last_event = state.plan_revisions[-1]["event"]
        rollups = self._campaign_nodes("cmp_rollup", "stage_summary")
        if last_event == "closure" or (
            state.plan_revisions[-1]["previous_plan"]["current_stage"]
            != state.current_stage
        ):
            self.assertGreaterEqual(len(rollups), 1)
            self.assertTrue(rollups[0]["memory"].startswith("stage '"))
        else:
            self.assertEqual(rollups, [])


class RecallContextTest(CampaignMemoryTestBase):
    def test_compact_state_context_includes_campaign_memory(self) -> None:
        bootstrap_state = self._bootstrap("cmp_recall")
        state = self.agent.run(
            event_type="new_observation",
            payload=NORMAL_OBSERVATION,
            previous_state=bootstrap_state.debug_snapshot(),
        )

        context_json = self.agent._compact_state_context(state, "unit_test_task")
        context = json.loads(context_json)
        self.assertIn("campaign_memory_context", context)
        memory_context = context["campaign_memory_context"]
        self.assertIn("path_summary", memory_context)
        self.assertIn("recent_stage_turns", memory_context)
        self.assertLessEqual(len(memory_context["path_summary"]), 2000)
        path_summary = json.loads(memory_context["path_summary"])
        self.assertEqual(path_summary["stage_route"], state.stage_route)

    def test_cross_campaign_layer_only_on_abnormal_signal(self) -> None:
        # seed a similar failure from ANOTHER campaign
        other = self._memory()
        other.add_experiment(
            "设备适应层返回 feasibility_error：缺少反应釜，无法执行水热反应；改为常压瓶内合成后成功。",
            metadata={
                "title": "cmp_other turn 2",
                "campaign_id": "cmp_other",
                "node_type": "observation_turn",
                "stage": "合成 stage",
                "turn_index": 2,
            },
            run_id="cmp_other",
        )

        bootstrap_state = self._bootstrap("cmp_cross")
        normal_state = self.agent.run(
            event_type="new_observation",
            payload=NORMAL_OBSERVATION,
            previous_state=bootstrap_state.debug_snapshot(),
        )
        normal_context = self.agent._campaign_memory_context(normal_state)
        self.assertNotIn("cross_campaign_cases", normal_context)

        error_state = self.agent.run(
            event_type="new_observation",
            payload=DEVICE_ERROR_PAYLOAD,
            previous_state=bootstrap_state.debug_snapshot(),
        )
        error_context = self.agent._campaign_memory_context(error_state)
        self.assertIn("cross_campaign_cases", error_context)
        cases = json.loads(error_context["cross_campaign_cases"])
        self.assertTrue(cases, "expected at least one cross-campaign case")
        self.assertEqual(cases[0]["campaign_id"], "cmp_other")


class RecallBuilderTest(unittest.TestCase):
    def test_recall_layers_and_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LayeredChemMemory(root_dir=tmp)
            for turn in range(1, 6):
                memory.add_experiment(
                    f"turn {turn} detail " + "x" * 300,
                    metadata={
                        "campaign_id": "cmp_b",
                        "node_type": "observation_turn",
                        "stage": "stage A",
                        "turn_index": turn,
                    },
                )
            memory.add_experiment(
                "stage 'stage A' 前期总结",
                metadata={
                    "campaign_id": "cmp_b",
                    "node_type": "stage_summary",
                    "stage": "stage 0",
                },
            )

            context = build_campaign_memory_context(
                memory,
                campaign_id="cmp_b",
                current_stage="stage A",
                stage_route=["stage 0", "stage A"],
                signal_text="",
                include_cross_campaign=False,
            )

            self.assertLessEqual(len(context["path_summary"]), 2000)
            self.assertLessEqual(len(context["recent_stage_turns"]), 3000)
            recent = json.loads(context["recent_stage_turns"])
            self.assertEqual(len(recent), 3)  # K=3, not all 5
            self.assertEqual([r["turn"] for r in recent], [3, 4, 5])
            path = json.loads(context["path_summary"])
            self.assertEqual(path["total_turns"], 5)
            self.assertEqual(len(path["completed_stage_summaries"]), 1)


if __name__ == "__main__":
    unittest.main()

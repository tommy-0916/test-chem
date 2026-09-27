"""The extraction boundary must preserve group and source coordinates."""

from __future__ import annotations

import json
import unittest

from reaserch_agent.prompts.task_prompts import PAPER_PROTOCOL_EXTRACT_PROMPT
from reaserch_agent.workflow import ResearchAgent


class RouteProtocolNormalizationTests(unittest.TestCase):
    def test_group_source_and_candidate_proposal_survive_normalization(self) -> None:
        agent = ResearchAgent(
            model=object(),
            use_llm=False,
            enable_memory=False,
            enable_online_literature=False,
            enable_web_search=False,
        )
        raw = {
            "source_title": "Example paper",
            "source_file": "source.md",
            "experimental_group_id": "control preparation",
            "group_role": "synthesis",
            "source": {
                "source_document": "source.md",
                "section": "Methods",
                "locator": "lines:10-20",
                "experimental_group_id": "control preparation",
                "source_digest": "sha256_example",
                "excerpt_hash": None,
                "untrusted_extra": "drop",
            },
            "target": {"material": "sample"},
            "route_signature": {"operations": ["mix", "wash"]},
            "evidence_bundle": [{"evidence_id": "E1"}],
            "evidence_matrix": [{"field_path": "material_graph[0].temperature"}],
            "material_graph": [{"macro_step_id": "MS_001"}],
            "required_capabilities": ["mixing"],
            "source_digest": "sha256_example",
            "steps": [{
                "操作": "mix",
                "step_role": "synthesis",
                "参数": "10 min",
                "evidence": "mix for 10 min",
                "source": {
                    "source_document": "source.md",
                    "section": "Methods",
                    "locator": "lines:12-12",
                    "experimental_group_id": "control preparation",
                },
            }],
        }
        normalized = agent._normalize_extracted_protocols([raw], [])
        self.assertEqual(len(normalized), 1)
        protocol = normalized[0]
        self.assertEqual(protocol["experimental_group_id"], "control preparation")
        self.assertEqual(protocol["group_role"], "synthesis")
        self.assertEqual(protocol["source"]["locator"], "lines:10-20")
        self.assertEqual(protocol["source"]["source_digest"], "sha256_example")
        self.assertNotIn("untrusted_extra", protocol["source"])
        self.assertEqual(protocol["steps"][0]["source"]["locator"], "lines:12-12")
        self.assertEqual(protocol["steps"][0]["source"]["experimental_group_id"], "control preparation")
        self.assertEqual(protocol["steps"][0]["step_role"], "synthesis")
        self.assertEqual(protocol["route_signature"], raw["route_signature"])
        self.assertEqual(protocol["evidence_matrix"], raw["evidence_matrix"])
        self.assertIsNot(protocol["evidence_matrix"], raw["evidence_matrix"])

    def test_prompt_example_is_valid_json_and_separates_groups(self) -> None:
        prompt = PAPER_PROTOCOL_EXTRACT_PROMPT.format(query="q", knowledge_context="k")
        example = prompt.split("只输出 JSON：", 1)[1].split("要求：", 1)[0].strip()
        payload = json.loads(example)
        self.assertIn("experimental_group_id", payload["protocols"][0])
        self.assertIn("group_role", payload["protocols"][0])
        self.assertIn("source", payload["protocols"][0]["steps"][0])
        self.assertIn("不得跨组拼接参数", prompt)

    def test_nested_group_extraction_is_not_dropped_for_lack_of_parent_steps(self) -> None:
        agent = ResearchAgent(
            model=object(), use_llm=False, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
        )
        groups = [{"experimental_group_id": "control", "steps": [{"操作": "mix"}]}]
        normalized = agent._normalize_extracted_protocols(
            [{"source_title": "Study", "experimental_groups": groups}], []
        )
        self.assertEqual(normalized[0]["experimental_groups"], groups)
        self.assertIsNot(normalized[0]["experimental_groups"], groups)

    def test_route_facts_survive_flat_and_nested_group_normalization(self) -> None:
        agent = ResearchAgent(
            model=object(), use_llm=False, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
        )
        fact = {
            "fact_id": "amount", "field_path": "material_graph[0].parameters[0].value",
            "value": 2, "unit": "mmol", "excerpt": "Add 2 mmol salt.",
            "source": {"locator": "lines:4-4"},
        }
        flat = {"source_title": "Study", "route_facts": [fact]}
        nested = {
            "source_title": "Study", "experimental_groups": [{
                "experimental_group_id": "Group A", "route_facts": [fact],
            }],
        }
        normalized = agent._normalize_extracted_protocols([flat, nested], [])
        self.assertEqual(len(normalized), 2)
        self.assertEqual(normalized[0]["route_facts"], [fact])
        self.assertEqual(normalized[1]["experimental_groups"][0]["route_facts"], [fact])
        self.assertIsNot(normalized[0]["route_facts"], flat["route_facts"])
        self.assertIsNot(normalized[1]["experimental_groups"], nested["experimental_groups"])


if __name__ == "__main__":
    unittest.main()

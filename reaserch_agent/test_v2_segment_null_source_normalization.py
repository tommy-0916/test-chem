"""Offline regression for null absent-evidence fields in inferred V2 segments."""

from __future__ import annotations

import unittest
from copy import deepcopy

from pydantic import ValidationError

from chem_agent_contracts.v2 import MaterialOperationSegmentV2
from reaserch_agent.workflow import ResearchAgent


class V2SegmentNullSourceNormalizationTests(unittest.TestCase):
    def agent(self) -> ResearchAgent:
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        return agent

    def unresolved_b01_step(self) -> dict:
        return {
            "步骤序号": 1,
            "操作": "镍源/钼源称量与前驱体配制（发布阻断）",
            "试剂/对象": "镍源、钼源身份 unresolved",
            "参数": "投料量、Ni/Mo 比和溶剂体积 unresolved",
            "material_inputs": [],
            "material_intermediates": [],
            "material_outputs": [],
            "material_relations": [],
            "material_applicability": [],
            "container_requirements": [],
            "material_contract_status": {
                "material_inputs": "unresolved",
                "material_intermediates": "unresolved",
                "material_outputs": "unresolved",
                "logical_containers": "unresolved",
                "material_relations": "unresolved",
            },
            "operation_segments": [
                {
                    "segment_id": "SEG-NIMO-AIR-OX-001-01-PRECURSOR-PREP",
                    "material_effect": "unknown",
                    "source_operation_ref": "SEG-NIMO-AIR-OX-001-01-PRECURSOR-PREP",
                    "provenance": {
                        "kind": "agent_inferred",
                        "reference": None,
                        "source_path": None,
                        "excerpt": None,
                        "rationale": "物料身份和投料量均无证据支持。",
                    },
                }
            ],
        }

    def test_null_absent_sources_become_empty_without_clearing_v2_block(self) -> None:
        raw = self.unresolved_b01_step()
        original = deepcopy(raw)
        normalized = self.agent()._normalize_macro_plan([raw])[0]
        provenance = normalized["operation_segments"][0]["provenance"]

        self.assertEqual(raw, original)
        self.assertEqual(
            [provenance[key] for key in ("reference", "source_path", "excerpt")],
            ["", "", ""],
        )
        segment = MaterialOperationSegmentV2.model_validate(
            normalized["operation_segments"][0], strict=True
        )
        self.assertEqual(segment.material_effect, "unknown")
        self.assertEqual(segment.provenance.kind, "agent_inferred")
        issues = ResearchAgent._v2_material_contract_issues(
            1, normalized, allowed_device_capability_ids=set()
        )
        self.assertTrue(any("material_inputs" in item and "unresolved" in item for item in issues))
        self.assertTrue(any("material_outputs" in item and "unresolved" in item for item in issues))

    def test_evidence_claim_with_null_source_is_not_repaired(self) -> None:
        raw = self.unresolved_b01_step()
        raw["operation_segments"][0]["provenance"]["kind"] = "paper"
        normalized = self.agent()._normalize_macro_plan([raw])[0]
        provenance = normalized["operation_segments"][0]["provenance"]
        self.assertIsNone(provenance["reference"])
        with self.assertRaises(ValidationError):
            MaterialOperationSegmentV2.model_validate(
                normalized["operation_segments"][0], strict=True
            )


if __name__ == "__main__":
    unittest.main()

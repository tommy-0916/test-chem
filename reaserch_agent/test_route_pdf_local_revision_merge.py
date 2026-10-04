"""G1 requirement 3: local-revision merge through the real workflow entry.

These tests drive ``ResearchAgent._propose_attested_route_protocols`` — the
real workflow entry — with the signed-inventory enumeration patched to the
archived NiFe Control group fixture and ``invoke_text`` returning archived or
programmatically derived envelopes.  No LLM is involved; every byte the
pipeline consumes is fixed by the fixtures.

Two scenarios pin the two honest outcomes of the bounded local-repair loop:

1. ``test_real_workflow_merges_single_defect_revision`` — a three-step lean
   chain (split -> first centrifugation-redispersion -> redisperse/age, the
   archived r10 steps 4/5/6) with one injected literal defect is repaired
   through the real repair prompt and merged (``merged_unreviewed``).  The
   test then pins G1 version consistency: the locator artifact's
   ``convention_state_proof_dags`` rows, the merged final proposal digest and
   the diagnostic fact receipt must all describe the SAME post-revision
   version.

2. ``test_real_workflow_budget_skip_keeps_ms7a_cascade_blocked`` — the full
   archived r10 proposal (87 facts, 10 steps) exceeds the repair prompt
   budget, so the repair is skipped honestly
   (``local_revision_prompt_char_budget_exceeded``), no revision is merged,
   and the real ms7a.out semantic-binding gap plus its downstream cascade
   stay BLOCKED.  The DAG-aware assessment (hole-2 fix) keeps exactly the
   14 real issues and proves two state paths via dual-verified DAGs.

The Control group fixture is the exported real signed-group content (23
blocks, pdf:p2:b54-b76); the r10 envelope is the archived real model output.
"""

import json
import re
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent import ResearchAgent
from reaserch_agent.route_pdf_groups import (
    PdfExperimentalGroupV1,
    PdfGroupEnumerationResultV1,
    PdfSourceBlockV1,
)
from reaserch_agent.route_pdf_local_repair import _digest
from reaserch_agent.route_state_proof_dag import build_verified_state_proof_dags
from reaserch_agent.state import ResearchAgentState, ResearchEvent

ROOT = Path(__file__).resolve().parents[1]
GROUP_FIXTURE = (
    ROOT / "reaserch_agent" / "fixtures" / "route_pdf_nife_control_group.json"
)
R10_ENVELOPE = (
    ROOT / "result" / "operation-structure-20260928"
    / "local-revision-r10-proposal.json"
)

# Lean chain: archived r10 steps 4 (split), 5 (first centrifugation-
# redispersion), 6 (redisperse and age) renumbered to 0/1/2.
_STEP_MAP = {4: 0, 5: 1, 6: 2}
_DEFECT_PATH = "material_graph[2].operation"
# The two water-input states the PDF never states for this chain.  The
# revision adds required=true facts for these exact leaves (as the repair
# prompt demands) with values the source does not support, so they stay
# honestly unresolved instead of vanishing from coverage.
_ADDED_FACTS = {
    "material_graph[1].material_inputs[0].state": (
        "liquid",
        "by a centrifugation−redispersion protocol using deionized water three",
    ),
    "material_graph[2].material_inputs[1].state": (
        "liquid", "were dispersed in 30 mL of water and aged for 20 h",
    ),
}


def _load_group() -> PdfExperimentalGroupV1:
    raw = json.loads(GROUP_FIXTURE.read_text(encoding="utf-8"))
    scope = ExperimentalGroupScopeV1(
        paper_id=raw["paper_id"],
        experimental_group_id=raw["experimental_group_id"],
        section=raw.get("section", ""),
        locator=raw.get("locator", ""),
        source_digest=raw["source_digest"],
    )
    return PdfExperimentalGroupV1(
        source_scope=scope,
        source_document=raw.get("source_document", ""),
        blocks=tuple(
            PdfSourceBlockV1(
                locator=block["locator"], text=block["text"],
                caption=bool(block.get("caption", False)),
            )
            for block in raw["blocks"]
        ),
    )


def _renumber(field_path: str) -> str:
    match = re.match(r"material_graph\[(\d+)\]", field_path)
    if match and int(match.group(1)) in _STEP_MAP:
        return (
            f"material_graph[{_STEP_MAP[int(match.group(1))]}]"
            + field_path[match.end():]
        )
    match = re.match(r"route_signature\.operations\[(\d+)\]", field_path)
    if match and int(match.group(1)) in _STEP_MAP:
        return f"route_signature.operations[{_STEP_MAP[int(match.group(1))]}]"
    return field_path


def _lean_proposal() -> dict:
    """Three-step chain from the archived r10 envelope, plus one defect.

    The defect changes ONLY the fact value at ``material_graph[2].operation``
    (the graph claim itself stays source-backed), so the initial coverage and
    split structuring still succeed and the failure surfaces as a literal
    field issue — repairable by restoring the source value.
    """
    r10 = json.loads(R10_ENVELOPE.read_text(encoding="utf-8"))["proposals"][0]
    graph = [deepcopy(r10["material_graph"][index]) for index in (4, 5, 6)]
    for new_index, step in enumerate(graph):
        step["sequence"] = new_index
    signature = deepcopy(r10["route_signature"])
    signature["operations"] = [
        r10["route_signature"]["operations"][index] for index in (4, 5, 6)
    ]
    facts = []
    for fact in r10["route_facts"]:
        path = fact["field_path"]
        match = re.match(
            r"material_graph\[(\d+)\]|route_signature\.operations\[(\d+)\]",
            path,
        )
        if match:
            index = int(match.group(1) or match.group(2))
            if index not in _STEP_MAP:
                continue
            fact = dict(fact)
            fact["field_path"] = _renumber(path)
        facts.append(fact)
    for fact in facts:
        if fact["field_path"] == _DEFECT_PATH:
            fact["value"] = "vortexed vigorously"
    return {
        "source_group_ref": deepcopy(r10["source_group_ref"]),
        "material_graph": graph,
        "route_signature": signature,
        "target": deepcopy(r10["target"]),
        "role_hint": r10.get("role_hint", ""),
        "route_facts": facts,
    }


def _strip_recomputed_locators(proposals: list) -> list:
    """Undo the locator production pass (it only rewrites fact locators).

    The program recomputes ``block_locator`` from the signed blocks, so the
    located proposals differ from the merged revision exactly by that
    non-authoritative metadata field.
    """
    stripped = deepcopy(proposals)
    for proposal in stripped:
        for fact in proposal.get("route_facts", []):
            fact.pop("block_locator", None)
    return stripped


def _fresh_dag_rows(proposal: dict, group: PdfExperimentalGroupV1) -> list:
    ref = proposal["source_group_ref"]
    entries = build_verified_state_proof_dags(
        proposal["material_graph"], proposal["route_facts"],
        paper_id=ref["paper_id"],
        experimental_group_id=ref["experimental_group_id"],
        source_digest=ref["source_digest"],
        blocks=[(block.locator, block.text) for block in group.blocks],
        caption_block_locators=[
            block.locator for block in group.blocks if block.caption
        ],
    )
    return [
        {"proposal_index": 0, "status": "unreviewed_prerequisites_only",
         **entry}
        for entry in entries.values()
    ]


class LocalRevisionMergeWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.group = _load_group()
        self.agent = ResearchAgent(
            model=object(), use_llm=True, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2",
            signed_route_source_events=[{"signed": "input"}],
        )
        self.state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare product"),
            contract_version="v2",
        )

    def _run(self, fake_invoke):
        with patch(
            "reaserch_agent.route_pdf_groups"
            ".enumerate_attested_pdf_experimental_groups",
            return_value=PdfGroupEnumerationResultV1(groups=[self.group]),
        ), patch.object(self.agent, "invoke_text", side_effect=fake_invoke):
            return self.agent._propose_attested_route_protocols(
                self.state, "."
            )

    def test_real_workflow_merges_single_defect_revision(self) -> None:
        proposal = _lean_proposal()
        first_response = json.dumps({"proposals": [proposal]},
                                    ensure_ascii=False)
        calls: list[str] = []

        def fake_invoke(system_prompt: str, prompt: str) -> str:
            calls.append(prompt)
            if len(calls) == 1:
                return first_response
            # The repair round: answer in the same raw (pre-structure) form
            # as the first response; the hooks re-expand the split step
            # deterministically.  Fix only the defect and add the required
            # facts the repair prompt demands for missing graph leaves.
            marker = "\nFailed fields (JSON):\n"
            issues = json.loads(prompt[prompt.index(marker) + len(marker):])
            revised = deepcopy(proposal)
            for fact in revised["route_facts"]:
                if fact["field_path"] == _DEFECT_PATH:
                    fact["value"] = revised["material_graph"][2]["operation"]
            existing = {fact["field_path"] for fact in revised["route_facts"]}
            added = 0
            for issue in issues:
                if issue["reason_code"] != "required_graph_fact_missing":
                    continue
                path = issue["field_path"]
                if path in existing:
                    continue
                value, excerpt = _ADDED_FACTS[path]
                added += 1
                revised["route_facts"].append({
                    "fact_id": f"f_rev_add_{added}", "field_path": path,
                    "value": value, "unit": "", "excerpt": excerpt,
                    "required": True,
                })
            return json.dumps({"proposals": [revised]}, ensure_ascii=False)

        protocols = self._run(fake_invoke)

        # Nothing is promoted: the merged proposal still carries honestly
        # unresolved fields, so no route candidate leaves the workflow.
        self.assertEqual(protocols, [])
        self.assertEqual(self.state.route_unreviewed_group_proposals_v1, [])
        self.assertEqual(len(calls), 2, "one proposal round + one repair")

        artifact = self.state.route_pdf_locator_production_v1
        revision = artifact["local_revision"]
        self.assertEqual(revision["status"], "reassessed_unreviewed")
        self.assertEqual(len(revision["revisions"]), 1)
        entry = revision["revisions"][0]
        # The merge actually happened, inside the prompt budget (the real
        # repair prompt was built and answered; no budget skip).
        self.assertEqual(entry.get("status"), "merged_unreviewed")
        self.assertFalse(entry.get("reason_code"))
        self.assertLess(entry["prompt_chars"], 48_000)
        self.assertEqual(
            sorted(entry["changed_field_paths"]),
            sorted([_DEFECT_PATH, *_ADDED_FACTS]),
        )
        self.assertEqual(entry["changed_non_fact_paths"], [])

        # The missing-leaf gaps were closed by the revision; what remains is
        # exactly the two unsupported water-state facts, kept unresolved.
        initial_reasons = {
            issue["reason_code"] for issue in revision["initial_issues"]
        }
        self.assertIn("required_graph_fact_missing", initial_reasons)
        self.assertIn("fact_graph_value_mismatch", initial_reasons)
        final = revision["final_issues"]
        self.assertEqual(
            {(issue["reason_code"], issue["field_path"]) for issue in final},
            {
                (reason, path)
                for path in _ADDED_FACTS
                for reason in (
                    "fact_graph_value_mismatch", "semantic_binding_pending",
                )
            },
        )
        self.assertTrue(all(
            issue["proposal_index"] == 0 for issue in final
        ))

        # Version consistency 1: the located (final, post-revision) proposals
        # hash to the revision report's final digest once the recomputed
        # locator metadata is stripped, and the merged revision digest pins
        # the same bytes for the single group.
        located = artifact["located_proposals"]
        stripped = _strip_recomputed_locators(located)
        self.assertEqual(
            _digest(stripped), revision["final_proposals_digest"],
        )
        self.assertEqual(_digest(stripped[0]), entry["revised_group_digest"])
        self.assertEqual(
            _digest(located), artifact["located_proposals_digest"],
        )

        # Version consistency 2: the artifact's audit DAG rows are rebuilt
        # from the FINAL proposals; a fresh dual-verified build on the
        # located proposal reproduces them byte-for-byte.
        fresh_rows = _fresh_dag_rows(located[0], self.group)
        self.assertEqual(
            artifact["convention_state_proof_dags"], fresh_rows,
        )
        self.assertEqual(len(fresh_rows), 15)

        # Version consistency 3: the diagnostic receipt describes the same
        # version — its DAG-proven paths are a subset of the PASS rows.
        receipt = self.state.route_group_fact_receipts_v1
        self.assertEqual(receipt["status"], "blocked")
        (group_result,) = receipt["group_results"]
        self.assertEqual(group_result["status"], "blocked")
        dag_proven = group_result["dag_proven_state_field_paths"]
        self.assertEqual(
            list(dag_proven), ["material_graph[2].material_outputs[0].state"],
        )
        pass_rows = {
            row["field_path"] for row in fresh_rows
            if row.get("verdict") == "PASS"
        }
        self.assertTrue(set(dag_proven) <= pass_rows)
        # The two added facts are the only unresolved literal fields.
        self.assertEqual(len(group_result["reason_codes"]), 2)
        self.assertTrue(all(
            reason.endswith("semantic_binding_pending")
            for reason in group_result["reason_codes"]
        ))

        # Raw model anchors: first response and the merged revision envelope.
        self.assertIn("route_pdf_group_propose", self.state.raw_llm_outputs)
        self.assertEqual(
            len(self.state.raw_llm_outputs[
                "route_pdf_group_propose_revisions"]),
            1,
        )

    def test_real_workflow_budget_skip_keeps_ms7a_cascade_blocked(self) -> None:
        envelope = json.loads(R10_ENVELOPE.read_text(encoding="utf-8"))
        calls: list[str] = []

        def fake_invoke(system_prompt: str, prompt: str) -> str:
            calls.append(prompt)
            if len(calls) > 1:
                raise AssertionError(
                    "repair must be skipped by the prompt budget"
                )
            return json.dumps(envelope, ensure_ascii=False)

        protocols = self._run(fake_invoke)

        self.assertEqual(protocols, [])
        self.assertEqual(len(calls), 1, "budget skip happens before invoke")
        artifact = self.state.route_pdf_locator_production_v1
        revision = artifact["local_revision"]
        (entry,) = revision["revisions"]
        self.assertEqual(
            entry.get("reason_code"),
            "local_revision_prompt_char_budget_exceeded",
        )
        self.assertNotEqual(entry.get("status"), "merged_unreviewed")
        # Observed on the archived bytes: the repair prompt would need
        # 53,806 chars against the 48,000-char budget.
        self.assertGreater(entry["prompt_chars"], 48_000)

        # Hole-2 effect on the real ten-step proposal: exactly the 14 real
        # issues remain — five semantic-binding gaps on the ms7a chain and
        # nine PDF-unsupported input states; the two DAG-provable facts are
        # no longer flagged.
        self.assertEqual(len(revision["initial_issues"]), 14)
        self.assertEqual(
            {
                (issue["reason_code"], issue.get("field_path"))
                for issue in revision["initial_issues"]
                if issue["reason_code"] == "semantic_binding_pending"
            },
            {
                ("semantic_binding_pending", path)
                for path in (
                    "material_graph[7].material_outputs[0].state",
                    "material_graph[8].material_inputs[0].state",
                    "material_graph[8].material_outputs[0].state",
                    "material_graph[9].material_inputs[0].state",
                    "material_graph[9].material_outputs[0].state",
                )
            },
        )
        self.assertEqual(
            sum(
                1 for issue in revision["initial_issues"]
                if issue["reason_code"] == "required_graph_fact_missing"
            ),
            9,
        )
        self.assertEqual(revision["final_issues"], revision["initial_issues"])
        # No merge: the final version IS the original baseline.
        self.assertEqual(
            revision["final_proposals_digest"],
            revision["original_proposals_digest"],
        )

        # Version consistency also holds on the unmerged path.
        located = artifact["located_proposals"]
        stripped = _strip_recomputed_locators(located)
        self.assertEqual(
            _digest(stripped), revision["final_proposals_digest"],
        )
        fresh_rows = _fresh_dag_rows(located[0], self.group)
        self.assertEqual(
            artifact["convention_state_proof_dags"], fresh_rows,
        )
        self.assertEqual(len(fresh_rows), 26)
        verdicts = {row["field_path"]: row.get("verdict")
                    for row in fresh_rows}
        self.assertEqual(
            verdicts["material_graph[6].material_outputs[0].state"], "PASS",
        )
        self.assertEqual(
            verdicts["material_graph[7].material_inputs[0].state"], "PASS",
        )
        # ms7a.out and its downstream cascade stay BLOCKED.
        for path in (
            "material_graph[7].material_outputs[0].state",
            "material_graph[8].material_inputs[0].state",
            "material_graph[8].material_outputs[0].state",
            "material_graph[9].material_inputs[0].state",
            "material_graph[9].material_outputs[0].state",
        ):
            self.assertEqual(verdicts[path], "BLOCKED", path)

        receipt = self.state.route_group_fact_receipts_v1
        self.assertEqual(receipt["status"], "blocked")
        (group_result,) = receipt["group_results"]
        self.assertEqual(group_result["status"], "blocked")
        self.assertEqual(
            list(group_result["dag_proven_state_field_paths"]),
            [
                "material_graph[6].material_outputs[0].state",
                "material_graph[7].material_inputs[0].state",
            ],
        )
        pass_rows = {
            row["field_path"] for row in fresh_rows
            if row.get("verdict") == "PASS"
        }
        self.assertTrue(
            set(group_result["dag_proven_state_field_paths"]) <= pass_rows
        )
        self.assertEqual(len(group_result["reason_codes"]), 5)


if __name__ == "__main__":
    unittest.main()

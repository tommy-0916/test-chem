"""B1/B2 opt-in route boundary does not publish an unbound route."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent.route_pdf_groups import (
    PdfExperimentalGroupV1, PdfGroupEnumerationResultV1, PdfSourceBlockV1,
)
from reaserch_agent.route_pipeline import evaluate_route_decision_v1 as real_route_evaluate
from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.workflow import ResearchAgent


GOAL = {
    "goal_id": "goal-1",
    "target": {
        "material": "product", "desired_state": "powder", "objective": "prepare product",
    },
    "constraint": "open",
}


def _fake_result(status: str):
    return SimpleNamespace(decision=SimpleNamespace(
        status=status,
        decision_reasons=["required_evidence_absent"],
        selected_route_id="route-1" if status == "selected_for_planning" else None,
    ))


class RouteWorkflowHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = ResearchAgent(
            model=object(), use_llm=False, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2",
        )

    def _bootstrap(self, constraints: dict):
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap", query="prepare product", constraints=constraints,
            ),
            contract_version="v2",
        )
        with patch.object(self.agent, "_step_survey_query_generate", return_value=["q"]), patch.object(
            self.agent, "_maybe_acquire_literature"
        ), patch.object(
            self.agent._knowledge_query, "search", return_value=[]
        ), patch.object(
            self.agent, "_step_survey_expansion",
            return_value={"continue_research": False, "new_queries": []},
        ), patch.object(
            self.agent, "_step_paper_protocol_extract", return_value=[]
        ), patch.object(
            self.agent, "_attach_protocol_provenance"
        ), patch.object(
            self.agent, "_step_macro_plan_design",
            return_value={"current_stage_plan": "plan", "macro_plan": [{"操作": "mix"}]},
        ) as planner, patch.object(
            self.agent, "_publish_v2_contract"
        ) as publish, patch.object(
            self.agent, "_record_plan_revision"
        ), patch.object(
            self.agent, "_annotate_macro_plan_sources"
        ), patch.object(
            self.agent, "_build_macro_action_view"
        ), patch.object(
            self.agent, "_step_survey_report_generate", return_value={}
        ), patch.object(
            self.agent, "_step_stage_design", return_value={
                "stage_route": ["synthesis"], "current_stage": "synthesis",
                "stage_route_reason": "reason", "current_stage_reason": "reason",
            },
        ):
            result = self.agent._run_b1(state)
        return result, planner, publish

    def test_unresolved_decision_blocks_b1_before_action(self) -> None:
        with patch.object(
            self.agent, "evaluate_route_decision_v1",
            return_value=_fake_result("unresolved"),
        ) as evaluate:
            state, planner, publish = self._bootstrap({"route_decision_goal_v1": GOAL})
        evaluate.assert_called_once()
        planner.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(state.status, "manual_required")
        self.assertEqual(state.next_branch, "B8")
        self.assertEqual(state.macro_plan, [])
        self.assertEqual(state.research_action_package_v2, {})
        self.assertEqual(state.device_adaptation_handoff, {})
        self.assertEqual(state.route_binding_status_v1, "decision_unresolved")

    def test_real_empty_kb_bootstrap_abstains_without_publication(self) -> None:
        with tempfile.TemporaryDirectory() as kb_dir:
            agent = ResearchAgent(
                model=object(), use_llm=False, enable_memory=False,
                enable_online_literature=False, enable_web_search=False,
                contract_version="v2", knowledge_base_dir=kb_dir,
                memory_dir=kb_dir, max_survey_rounds=1,
            )
            state = agent.run(
                "bootstrap", query="prepare product",
                constraints={"route_decision_goal_v1": GOAL},
            )
        self.assertEqual(state.status, "manual_required")
        self.assertEqual(state.route_decision_v1["status"], "unresolved")
        self.assertEqual(state.macro_plan, [])
        self.assertEqual(state.research_action_package_v2, {})
        self.assertEqual(state.device_adaptation_handoff, {})

    def test_selected_decision_still_requires_lossless_plan_binding(self) -> None:
        with patch.object(
            self.agent, "evaluate_route_decision_v1",
            return_value=_fake_result("selected_for_planning"),
        ):
            state, planner, publish = self._bootstrap({"route_decision_goal_v1": GOAL})
        planner.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(state.route_binding_status_v1, "selected_unbound")
        self.assertIn("route_plan_binding_unavailable", state.manual_handoff)
        self.assertEqual(state.research_action_package_v2, {})

    def test_malformed_opt_in_goal_abstains_without_legacy_fallback(self) -> None:
        with patch.object(self.agent, "evaluate_route_decision_v1") as evaluate:
            state, planner, publish = self._bootstrap({"route_decision_goal_v1": None})
        evaluate.assert_not_called()
        planner.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(state.route_binding_status_v1, "decision_error")
        self.assertEqual(state.failure_category, "route_decision_error")
        self.assertEqual(state.device_adaptation_handoff, {})

    def test_legacy_path_still_reaches_existing_publication(self) -> None:
        with patch.object(self.agent, "evaluate_route_decision_v1") as evaluate:
            state, planner, publish = self._bootstrap({})
        evaluate.assert_not_called()
        planner.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(state.status, "completed")
        self.assertFalse(state.route_decision_enabled_v1)

    def test_b2_sticky_route_goal_rechecks_and_blocks_new_action(self) -> None:
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="new_observation", query="prepare product",
                payload={"observation": {"summary": "recovered solid"}},
            ),
            contract_version="v2", stage_route=["synthesis"], current_stage="synthesis",
            route_decision_enabled_v1=True, route_decision_goal_v1=dict(GOAL),
            macro_plan=[{"操作": "mix", "试剂/对象": "salt", "参数": "2 mmol"}],
            research_action_package_v2={"old": "package"},
        )
        with patch.object(
            self.agent, "evaluate_route_decision_v1", return_value=_fake_result("unresolved"),
        ) as evaluate, patch.object(
            self.agent, "_step_observation_stage_fit_judge"
        ) as fit, patch.object(
            self.agent, "_is_unauthorized_device_feedback", return_value=False,
        ) as device_feedback, patch.object(
            self.agent, "_complete_b2_ignored_device_feedback",
        ) as ignored:
            result = self.agent._run_b2(state)
        evaluate.assert_called_once()
        fit.assert_not_called()
        device_feedback.assert_called_once()
        ignored.assert_not_called()
        self.assertEqual(len(result.previous_macro_plan), 1)
        self.assertEqual(result.macro_plan, [])
        self.assertEqual(result.research_action_package_v2, {})
        self.assertEqual(result.device_adaptation_handoff, {})
        self.assertEqual(result.status, "manual_required")

    def test_device_local_feedback_cannot_trigger_route_replan(self) -> None:
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="new_observation", query="prepare product",
                payload={"observation": {"summary": "device local error"}},
            ),
            contract_version="v2", stage_route=["synthesis"], current_stage="synthesis",
            route_decision_enabled_v1=True, route_decision_goal_v1=dict(GOAL),
            macro_plan=[{"操作": "mix", "试剂/对象": "salt", "参数": "2 mmol"}],
            research_action_package_v2={"old": "package"},
        )
        with patch.object(
            self.agent, "evaluate_route_decision_v1"
        ) as evaluate, patch.object(
            self.agent, "_is_unauthorized_device_feedback", return_value=True,
        ), patch.object(
            self.agent, "_complete_b2_ignored_device_feedback", return_value=state,
        ) as ignored:
            result = self.agent._run_b2(state)
        evaluate.assert_not_called()
        ignored.assert_called_once()
        self.assertEqual(result.research_action_package_v2, {"old": "package"})

    def test_constructor_inventory_register_reaches_pipeline_without_constraint_override(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "registered-specs.json"
            spec.write_text(json.dumps({"schema": "candidate_supply_spec/v1", "items": []}),
                            encoding="utf-8")
            trusted = {"candidate_supply_spec/v1": str(spec)}
            agent = ResearchAgent(
                model=object(), use_llm=False, enable_memory=False,
                enable_online_literature=False, enable_web_search=False,
                contract_version="v2", knowledge_base_dir=directory, memory_dir=directory,
                trusted_inventory_register_paths=trusted,
            )
            injected = {"candidate_supply_spec/v1": str(Path(directory) / "model-specs.json")}
            state = ResearchAgentState(
                event=ResearchEvent(event_type="bootstrap", query="prepare product", constraints={
                    "trusted_inventory_register_paths": injected,
                    "route_trust_config": {"trusted_inventory_register_paths": injected},
                }),
                contract_version="v2",
            )
            with patch("reaserch_agent.route_pipeline.evaluate_route_decision_v1",
                       wraps=real_route_evaluate) as evaluate:
                result = agent.evaluate_route_decision_v1(state, GOAL, route_protocols=[])
            self.assertEqual(result.decision.status, "unresolved")
            kwargs = evaluate.call_args.kwargs
            self.assertEqual(kwargs["trusted_inventory_register_paths"], trusted)
            self.assertNotEqual(kwargs["trusted_inventory_register_paths"], injected)
            self.assertEqual(Path(kwargs["source_root"]).resolve(), Path(directory).resolve())
            self.assertEqual(state.research_action_package_v2, {})

    def test_constraints_cannot_create_inventory_trust_when_constructor_has_none(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = ResearchAgent(
                model=object(), use_llm=False, enable_memory=False,
                enable_online_literature=False, enable_web_search=False,
                contract_version="v2", knowledge_base_dir=directory, memory_dir=directory,
            )
            state = ResearchAgentState(
                event=ResearchEvent(event_type="bootstrap", query="prepare product", constraints={
                    "trusted_inventory_register_paths": {"candidate_supply_spec/v1": "forged.json"},
                }),
                contract_version="v2",
            )
            with patch("reaserch_agent.route_pipeline.evaluate_route_decision_v1",
                       wraps=real_route_evaluate) as evaluate:
                result = agent.evaluate_route_decision_v1(state, GOAL, route_protocols=[])
            self.assertFalse(evaluate.call_args.kwargs["trusted_inventory_register_paths"])
            self.assertEqual(result.decision.status, "unresolved")
            self.assertEqual(state.research_action_package_v2, {})

    def test_route_evaluation_uses_pdf_group_proposals_not_legacy_summaries(self) -> None:
        digest = "sha256_" + "a" * 64
        groups = [
            PdfExperimentalGroupV1(
                source_scope=ExperimentalGroupScopeV1(
                    paper_id="paper-1", experimental_group_id=group_id,
                    section="Methods", locator=locator,
                    source_digest=digest,
                ),
                source_document="/trusted/source.pdf",
                blocks=(PdfSourceBlockV1(locator, text),),
            )
            for group_id, locator, text in (
                ("synthesis", "pdf:p1:b1-p1:b1", "Mix 2 mmol salt."),
                ("testing", "pdf:p1:b2-p1:b2", "Measure current."),
            )
        ]
        keys = [
            (group.source_scope.paper_id,
             group.source_scope.experimental_group_id,
             group.source_scope.source_digest)
            for group in groups
        ]
        agent = ResearchAgent(
            model=object(), use_llm=True, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2", signed_route_source_events=[{"signed": "input"}],
            trusted_route_group_roles_by_group={
                keys[0]: "synthesis", keys[1]: "testing",
            },
            trusted_route_capabilities_by_group={keys[0]: ["mixing"]},
        )
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare product"),
            contract_version="v2",
            extracted_protocols=[{"source_title": "legacy summary", "steps": []}],
        )
        proposals = [
            {
                "source_group_ref": {
                    "paper_id": key[0], "experimental_group_id": key[1],
                    "source_digest": key[2],
                },
                "route_facts": [],
            }
            for key in keys
        ]
        proposals[0]["route_facts"] = [{
            "fact_id": "mix", "field_path": "route_signature.operations[0]",
            "value": "Mix", "unit": "", "excerpt": "Mix 2 mmol salt.",
            "block_locator": "pdf:p1:b2-p1:b2", "required": True,
        }]
        proposals[0]["route_signature"] = {"operations": ["Mix"]}
        captured: list[list[dict]] = []

        def evaluate(goal, protocols, **kwargs):
            captured.append(list(protocols))
            return real_route_evaluate(goal, protocols, **kwargs)

        with patch(
            "reaserch_agent.route_pdf_groups.enumerate_attested_pdf_experimental_groups",
            return_value=PdfGroupEnumerationResultV1(groups=groups),
        ), patch.object(
            agent, "invoke_text", return_value=json.dumps({"proposals": proposals}),
        ) as invoke, patch(
            "reaserch_agent.route_pipeline.evaluate_route_decision_v1",
            side_effect=evaluate,
        ):
            result = agent.evaluate_route_decision_v1(state, GOAL)
        self.assertEqual(result.decision.status, "unresolved")
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(captured[0]), 2)
        self.assertEqual(
            [item["group_role"] for item in captured[0]],
            ["synthesis", "testing"],
        )
        self.assertTrue(all(item.get("source", {}).get("source_digest") == digest
                            for item in captured[0]))
        self.assertEqual(
            captured[0][0]["route_facts"][0]["source"]["locator"],
            "pdf:p1:b1-p1:b1",
        )
        self.assertEqual(
            state.raw_llm_outputs["route_pdf_group_propose"]["proposals"][0]
            ["route_facts"][0]["block_locator"], "pdf:p1:b2-p1:b2",
        )
        self.assertNotIn("legacy summary", repr(captured[0]))
        self.assertEqual(state.route_group_proposal_diagnostics_v1, [])
        self.assertEqual(invoke.call_count, 1)
        self.assertIn("PDF group inventory", invoke.call_args.args[1])
        self.assertNotIn("legacy summary", "".join(invoke.call_args.args))
        self.assertLessEqual(sum(len(item) for item in invoke.call_args.args), 48_000)
        self.assertEqual(state.research_action_package_v2, {})

    def test_failed_route_proposal_attempt_clears_previous_model_output(self) -> None:
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare product"),
            contract_version="v2",
            raw_llm_outputs={"route_pdf_group_propose": {"proposals": ["stale"]}},
        )
        with tempfile.TemporaryDirectory() as kb_dir:
            protocols = self.agent._propose_attested_route_protocols(state, kb_dir)
        self.assertEqual(protocols, [])
        self.assertNotIn("route_pdf_group_propose", state.raw_llm_outputs)
        self.assertEqual(
            state.route_group_proposal_diagnostics_v1[0]["reason_code"],
            "signed_source_events_missing",
        )

    def test_signed_pdf_can_yield_unreviewed_facts_without_route_authority(self) -> None:
        digest = "sha256_" + "a" * 64
        groups = [
            PdfExperimentalGroupV1(
                source_scope=ExperimentalGroupScopeV1(
                    paper_id="paper-1", experimental_group_id=group_id,
                    section="Methods", locator=locator, source_digest=digest,
                ),
                source_document="/trusted/source.pdf",
                blocks=(PdfSourceBlockV1(locator, text),),
            )
            for group_id, locator, text in (
                ("Sample synthesis", "pdf:p1:b1-p1:b1", "Mix 2 mmol salt."),
                ("Materials", "pdf:p1:b2-p1:b2", "Materials were purchased."),
            )
        ]
        proposals = []
        for group in groups:
            scope = group.source_scope
            proposal = {"source_group_ref": {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
            }, "route_facts": []}
            if scope.experimental_group_id == "Sample synthesis":
                proposal["route_signature"] = {"operations": ["Mix"]}
                proposal["route_facts"] = [{
                    "fact_id": "mix", "field_path": "route_signature.operations[0]",
                    "value": "Mix", "unit": "", "excerpt": "Mix 2 mmol salt.",
                    "block_locator": scope.locator, "required": True,
                }]
            proposals.append(proposal)
        agent = ResearchAgent(
            model=object(), use_llm=True, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2", signed_route_source_events=[{"signed": "input"}],
        )
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare product"),
            contract_version="v2",
        )
        with patch(
            "reaserch_agent.route_pdf_groups.enumerate_attested_pdf_experimental_groups",
            return_value=PdfGroupEnumerationResultV1(groups=groups),
        ), patch.object(
            agent, "invoke_text", return_value=json.dumps({"proposals": proposals}),
        ) as invoke:
            reviewed_protocols = agent._propose_attested_route_protocols(state, ".")
        self.assertEqual(reviewed_protocols, [])
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(len(state.route_unreviewed_group_proposals_v1), 2)
        self.assertTrue(all(
            item["group_role"] == "unclassified"
            and "required_capabilities" not in item
            for item in state.route_unreviewed_group_proposals_v1
        ))
        self.assertIn("route_pdf_group_propose", state.raw_llm_outputs)
        self.assertIn("review_work_orders", state.route_group_fact_receipts_v1)
        self.assertFalse(any(
            order["human_chemical_review_completed"]
            for order in state.route_group_fact_receipts_v1["review_work_orders"]
        ))
        self.assertIn(
            "trusted_group_role_missing",
            [item["reason_code"] for item in state.route_group_proposal_diagnostics_v1],
        )

    def test_workflow_preserves_raw_model_anchor_and_records_computed_locator(self) -> None:
        digest = "sha256_" + "d" * 64
        group = PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id="paper-4", experimental_group_id="Sample synthesis",
                section="Methods", locator="pdf:p1:b1-p1:b2",
                source_digest=digest,
            ),
            source_document="/trusted/source.pdf",
            blocks=(
                PdfSourceBlockV1("pdf:p1:b1-p1:b1", "Sample synthesis"),
                PdfSourceBlockV1("pdf:p1:b2-p1:b2", "Mix 2 mmol salt."),
            ),
        )
        proposal = {
            "source_group_ref": {
                "paper_id": "paper-4",
                "experimental_group_id": "Sample synthesis",
                "source_digest": digest,
            },
            "route_facts": [{
                "fact_id": "mix", "field_path": "route_signature.operations[0]",
                "value": "Mix", "unit": "", "excerpt": "Mix 2 mmol salt.",
                "block_locator": "pdf:p1:b1-p1:b1", "required": True,
            }],
            "route_signature": {"operations": ["Mix"]},
        }
        raw_envelope = {"proposals": [proposal]}
        original = deepcopy(raw_envelope)
        agent = ResearchAgent(
            model=object(), use_llm=True, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2", signed_route_source_events=[{"signed": "input"}],
        )
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare product"),
            contract_version="v2",
        )
        with patch(
            "reaserch_agent.route_pdf_groups.enumerate_attested_pdf_experimental_groups",
            return_value=PdfGroupEnumerationResultV1(groups=[group]),
        ), patch.object(
            agent, "invoke_text", return_value=json.dumps(raw_envelope),
        ) as invoke:
            reviewed_protocols = agent._propose_attested_route_protocols(state, ".")

        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(reviewed_protocols, [])
        self.assertEqual(raw_envelope, original)
        self.assertEqual(state.raw_llm_outputs["route_pdf_group_propose"], original)
        self.assertEqual(len(state.route_unreviewed_group_proposals_v1), 1)
        self.assertEqual(
            state.route_unreviewed_group_proposals_v1[0]["route_facts"][0]
            ["source"]["locator"], "pdf:p1:b2-p1:b2",
        )
        location = state.route_pdf_locator_production_v1
        self.assertEqual(
            location["located_proposals"][0]["route_facts"][0]["block_locator"],
            "pdf:p1:b2-p1:b2",
        )
        self.assertEqual(location["field_records"][0]["raw_block_locator"],
                         "pdf:p1:b1-p1:b1")
        self.assertEqual(location["field_records"][0]["resolved_block_locator"],
                         "pdf:p1:b2-p1:b2")
        self.assertEqual(location["field_records"][0]["source_digest"], digest)
        restored = agent._state_from_dict(state.to_dict())
        self.assertEqual(restored.route_pdf_locator_production_v1, location)
        self.assertEqual(
            restored.raw_llm_outputs["route_pdf_group_propose"], original,
        )
        self.assertIn(
            "trusted_group_role_missing",
            [item["reason_code"] for item in state.route_group_proposal_diagnostics_v1],
        )
        self.assertEqual(state.research_action_package_v2, {})

    def test_bad_pdf_quote_keeps_scoped_review_work_order(self) -> None:
        digest = "sha256_" + "b" * 64
        group = PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id="paper-2", experimental_group_id="Preparation",
                section="Methods", locator="pdf:p1:b1-p1:b1",
                source_digest=digest,
            ),
            source_document="/trusted/other.pdf",
            blocks=(PdfSourceBlockV1("pdf:p1:b1-p1:b1", "Mix 2 mmol salt."),),
        )
        proposal = {"source_group_ref": {
            "paper_id": "paper-2", "experimental_group_id": "Preparation",
            "source_digest": digest,
        }, "route_facts": [{
            "fact_id": "bad", "field_path": "route_signature.operations[0]",
            "value": "mix", "unit": "", "excerpt": "Mix 3 mmol salt.",
            "block_locator": "pdf:p1:b1-p1:b1", "required": True,
        }]}
        agent = ResearchAgent(
            model=object(), use_llm=True, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2", signed_route_source_events=[{"signed": "input"}],
        )
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare product"),
            contract_version="v2",
        )
        with patch(
            "reaserch_agent.route_pdf_groups.enumerate_attested_pdf_experimental_groups",
            return_value=PdfGroupEnumerationResultV1(groups=[group]),
        ), patch.object(
            agent, "invoke_text", return_value=json.dumps({"proposals": [proposal]}),
        ):
            self.assertEqual(agent._propose_attested_route_protocols(state, "."), [])
        self.assertEqual(state.route_unreviewed_group_proposals_v1, [])
        self.assertIn(
            "fact_excerpt_not_in_block",
            [item["reason_code"] for item in state.route_group_proposal_diagnostics_v1],
        )
        self.assertEqual(
            len(state.route_group_fact_receipts_v1["review_work_orders"]), 1,
        )
        self.assertFalse(
            state.route_group_fact_receipts_v1["review_work_orders"][0]
            ["human_chemical_review_completed"],
        )

    def test_blocked_literal_receipt_never_reaches_reviewed_association(self) -> None:
        digest = "sha256_" + "c" * 64
        locator = "pdf:p1:b1-p1:b1"
        group = PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id="paper-3", experimental_group_id="Preparation",
                section="Methods", locator=locator, source_digest=digest,
            ),
            source_document="/trusted/paper.pdf",
            blocks=(PdfSourceBlockV1(locator, "Mix 2 mmol salt."),),
        )
        key = ("paper-3", "Preparation", digest)
        proposal = {
            "source_group_ref": {
                "paper_id": key[0], "experimental_group_id": key[1],
                "source_digest": key[2],
            },
            "route_facts": [{
                "fact_id": "mix", "field_path": "route_signature.operations[0]",
                "value": "Mix", "unit": 123, "excerpt": "Mix 2 mmol salt.",
                "block_locator": locator, "required": True,
            }],
        }
        agent = ResearchAgent(
            model=object(), use_llm=True, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2", signed_route_source_events=[{"signed": "input"}],
            trusted_route_group_roles_by_group={key: "synthesis"},
            trusted_route_capabilities_by_group={key: ["mixing"]},
        )
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare product"),
            contract_version="v2",
        )
        with patch(
            "reaserch_agent.route_pdf_groups.enumerate_attested_pdf_experimental_groups",
            return_value=PdfGroupEnumerationResultV1(groups=[group]),
        ), patch.object(
            agent, "invoke_text", return_value=json.dumps({"proposals": [proposal]}),
        ), patch(
            "reaserch_agent.route_pdf_group_extraction.propose_pdf_group_protocols",
        ) as reviewed:
            self.assertEqual(agent._propose_attested_route_protocols(state, "."), [])
        reviewed.assert_not_called()
        self.assertEqual(state.route_group_fact_receipts_v1["status"], "blocked")
        self.assertIn(
            "fact[0]:fact_unit_invalid",
            state.route_group_fact_receipts_v1["group_results"][0]["reason_codes"],
        )
        self.assertIn(
            "group_literal_check_failed",
            [item["reason_code"] for item in state.route_group_proposal_diagnostics_v1],
        )


if __name__ == "__main__":
    unittest.main()

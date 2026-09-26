"""Offline regression tests for explicit references at the V2 action boundary.

An ingested reference must remain auditable after the ordinary top-five search,
but its numbers can reach the macro planner only after the chosen route matches.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.workflow import ResearchAgent


DOI_LDH = "10.1038/s41598-018-22630-0"
LDH_TITLE = (
    "Low-temperature synthesis and investigation into the formation mechanism "
    "of high quality Ni-Fe layered double hydroxides hexagonal platelets"
)
UREA_EXCERPT = (
    "Ni(NO3)2·6H2O, Fe(NO3)3·9H2O and urea were dissolved in 80 mL "
    "of deionized water to final concentrations of 7.5, 2.5 and 17.5 mM. "
    "Then 0.8 mmol TEA was added and the mixture was refluxed at 100 °C."
)
COPPT_EXCERPT = (
    "Ni(NO3)2·6H2O and Fe(NO3)3·9H2O were dissolved in 80 mL water. "
    "NaOH and Na2CO3 were added dropwise during co-precipitation of NiFe-LDH."
)
PBA_EXCERPT = (
    "For NiFe-PBA nanocubes, 4 mmol Ni(NO3)2·6H2O and sodium citrate "
    "were mixed with K3Fe(CN)6 solution and aged for 24 h."
)


def _write_paper(
    directory: Path,
    name: str,
    *,
    title: str,
    summary: str,
    steps: list[dict],
    doi: str,
) -> Path:
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "文献题目": title,
                "1. 解决的问题": f"Experimental synthesis of {title}",
                "2. 具体的合成步骤": {
                    "描述性总结": summary,
                    "参数列表": steps,
                },
                "3. 性能": [],
                "_ingestion_metadata": {"doi": doi},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _step(operation: str, material: str, parameter: str, excerpt: str) -> dict:
    return {
        "步骤序号": 1,
        "操作": operation,
        "试剂/对象": material,
        "参数": parameter,
        "evidence": excerpt,
    }


class ActionEvidenceSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.kb = self.root / "kb"
        self.kb.mkdir()
        memory_root = self.root / "memory"
        environment = patch.dict(
            os.environ, {"RESEARCH_MEMORY_STORE_DIR": str(memory_root)}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def _agent(self) -> ResearchAgent:
        return ResearchAgent(
            model=object(),
            use_llm=False,
            knowledge_base_dir=str(self.kb),
            memory_dir=str(self.root / "memory"),
            enable_memory=False,
            enable_online_literature=False,
            enable_web_search=False,
            contract_version="v2",
        )

    def _state(self, *references: Path) -> ResearchAgentState:
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap",
                query="NiFe PBA ferricyanide citrate nanocubes synthesis",
            ),
            contract_version="v2",
        )
        state.current_stage = "Catalyst synthesis before XRD"
        state.survey_queries = ["NiFe PBA ferricyanide citrate nanocubes"]
        state.reference_inputs = [
            {
                "raw": str(path),
                "kind": "local_file",
                "path": str(path),
                "status": "ingested",
                "written_records": [str(path)],
            }
            for path in references
        ]
        return state

    @staticmethod
    def _choose_coprecipitation(state: ResearchAgentState) -> None:
        state.pending_macro_action = {
            "objective": "Synthesize NiFe-LDH using NaOH/Na2CO3 co-precipitation",
            "planned_operations": [
                "Dissolve Ni and Fe nitrate salts in water",
                "Add NaOH and Na2CO3 dropwise to co-precipitate NiFe-LDH",
                "Age, centrifuge, wash, and dry the LDH precipitate",
            ],
            "experiment_group": {
                "variables": {
                    "target_material_family": "NiFe layered double hydroxide / NiFe-LDH",
                    "Fe_introduction_mode": "lattice_coprecipitation",
                }
            },
        }

    def _refresh_and_choose(self, agent: ResearchAgent, state: ResearchAgentState) -> None:
        agent._refresh_action_evidence(
            state, planning_mode="bootstrap", observation_point="XRD"
        )
        self._choose_coprecipitation(state)
        agent._select_action_evidence_for_route(state)

    def _pba_distractors(self, count: int = 6) -> None:
        for index in range(count):
            _write_paper(
                self.kb,
                f"pba_{index}.json",
                title=f"NiFe PBA ferricyanide citrate nanocubes synthesis {index}",
                summary=PBA_EXCERPT,
                steps=[
                    _step(
                        "Prepare NiFe-PBA ferricyanide nanocubes",
                        "Ni nitrate; K3Fe(CN)6; sodium citrate",
                        "4 mmol Ni nitrate; age 24 h",
                        PBA_EXCERPT,
                    )
                ],
                doi=f"10.1000/nife-pba-{index}",
            )

    def test_explicit_reference_below_natural_top_five_is_audited_and_projected_when_compatible(self) -> None:
        self._pba_distractors()
        compatible = _write_paper(
            self.kb,
            "specified_coprecipitation.json",
            title="NiFe-LDH alkaline carbonate co-precipitation protocol",
            summary=COPPT_EXCERPT,
            steps=[
                _step(
                    "Co-precipitate NiFe-LDH with NaOH and Na2CO3",
                    "Ni(NO3)2·6H2O; Fe(NO3)3·9H2O; NaOH; Na2CO3",
                    "80 mL water; add NaOH and Na2CO3 dropwise",
                    COPPT_EXCERPT,
                )
            ],
            doi="10.1000/nife-ldh-coppt",
        )
        agent = self._agent()
        state = self._state(compatible)

        natural_top_five = agent._knowledge_query.search(
            [state.event.query], top_k=5
        )
        self.assertNotIn(str(compatible), [hit.file_path for hit in natural_top_five])
        agent._refresh_action_evidence(
            state, planning_mode="bootstrap", observation_point="XRD"
        )
        candidates = state.current_evidence_bundle["reference_candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["status"], "candidate")
        self.assertEqual(candidates[0]["reason_code"], "awaiting_route")
        self.assertEqual(candidates[0]["source_file"], str(compatible))
        self.assertNotIn("80 mL", json.dumps(candidates[0]["route_outline"]))

        self._choose_coprecipitation(state)
        agent._select_action_evidence_for_route(state)
        selected = state.current_evidence_bundle["reference_candidates"][0]
        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["reason_code"], "route_match")
        results = state.current_evidence_bundle["results"]
        self.assertEqual(results[0]["paper_id"], selected["paper_id"])
        self.assertTrue(results[0]["paper_id"])
        projected = agent._compact_action_evidence_for_planning(results)
        self.assertLessEqual(len(projected), 2)
        self.assertEqual(projected[0]["paper_id"], selected["paper_id"])
        self.assertEqual(
            projected[0]["evidence_source_path"], "evidence_bundle.items[0].excerpt"
        )
        self.assertIn("NaOH and Na2CO3", projected[0]["evidence_excerpt"])
        self.assertTrue(state.current_evidence_bundle["selection_log"])

    def test_urea_reflux_and_pba_amounts_do_not_enter_coprecipitation_plan(self) -> None:
        urea = _write_paper(
            self.kb,
            "specified_2018_urea_reflux.json",
            title=LDH_TITLE,
            summary=UREA_EXCERPT,
            steps=[
                _step(
                    "Dissolve Ni and Fe salts with urea, add TEA, reflux",
                    "Ni(NO3)2·6H2O; Fe(NO3)3·9H2O; urea; TEA",
                    "7.5 mM Ni salt; 0.8 mmol TEA; reflux at 100 °C",
                    UREA_EXCERPT,
                )
            ],
            doi=DOI_LDH,
        )
        pba = _write_paper(
            self.kb,
            "specified_pba.json",
            title="NiFe-PBA nanocubes from ferricyanide",
            summary=PBA_EXCERPT,
            steps=[
                _step(
                    "Make NiFe-PBA nanocubes",
                    "Ni nitrate; sodium citrate; K3Fe(CN)6",
                    "4 mmol Ni nitrate; age 24 h",
                    PBA_EXCERPT,
                )
            ],
            doi="10.1000/pba-route",
        )
        agent = self._agent()
        state = self._state(urea, pba)
        self._refresh_and_choose(agent, state)

        candidates = state.current_evidence_bundle["reference_candidates"]
        self.assertEqual({item["status"] for item in candidates}, {"excluded"})
        self.assertEqual(
            {item["reason_code"] for item in candidates}, {"route_mismatch"}
        )
        projected = agent._compact_action_evidence_for_planning(
            state.current_evidence_bundle["results"]
        )
        projected_text = json.dumps(projected, ensure_ascii=False)
        self.assertNotIn("7.5 mM", projected_text)
        self.assertNotIn("0.8 mmol TEA", projected_text)
        self.assertNotIn("4 mmol", projected_text)

    def test_duplicate_explicit_doi_has_one_selected_identity(self) -> None:
        first = _write_paper(
            self.kb,
            "specified_first.json",
            title="NiFe-LDH NaOH carbonate precipitation",
            summary=COPPT_EXCERPT,
            steps=[
                _step(
                    "Co-precipitate NiFe-LDH with NaOH and Na2CO3",
                    "Ni nitrate; Fe nitrate; NaOH; Na2CO3",
                    "80 mL water; NaOH/Na2CO3 dropwise",
                    COPPT_EXCERPT,
                )
            ],
            doi="10.1000/same-ldh-route",
        )
        second = _write_paper(
            self.kb,
            "specified_copy.json",
            title="Copy of NiFe-LDH NaOH carbonate precipitation",
            summary=COPPT_EXCERPT,
            steps=[
                _step(
                    "Co-precipitate NiFe-LDH with NaOH and Na2CO3",
                    "Ni nitrate; Fe nitrate; NaOH; Na2CO3",
                    "80 mL water; NaOH/Na2CO3 dropwise",
                    COPPT_EXCERPT,
                )
            ],
            doi="10.1000/same-ldh-route",
        )
        agent = self._agent()
        state = self._state(first, second)
        self._refresh_and_choose(agent, state)

        candidates = state.current_evidence_bundle["reference_candidates"]
        self.assertEqual(len(candidates), 2)
        stable_ids = {item["paper_id"] for item in candidates}
        self.assertEqual(len(stable_ids), 1)
        self.assertTrue(next(iter(stable_ids)))
        self.assertEqual(sum(item["status"] == "selected" for item in candidates), 1)
        self.assertEqual(sum(item["reason_code"] == "duplicate" for item in candidates), 1)
        results = state.current_evidence_bundle["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(
            sum(item.get("paper_id") in stable_ids for item in results),
            1,
        )

    def test_unknown_action_route_does_not_authorize_natural_or_explicit_numbers(self) -> None:
        pba = _write_paper(
            self.kb,
            "specified_pba.json",
            title="NiFe-PBA nanocubes from ferricyanide",
            summary=PBA_EXCERPT,
            steps=[
                _step(
                    "Make NiFe-PBA nanocubes",
                    "Ni nitrate; sodium citrate; K3Fe(CN)6",
                    "4 mmol Ni nitrate; age 24 h",
                    PBA_EXCERPT,
                )
            ],
            doi="10.1000/pba-unknown-route",
        )
        agent = self._agent()
        state = self._state(pba)
        agent._refresh_action_evidence(
            state, planning_mode="bootstrap", observation_point="XRD"
        )
        self.assertTrue(state.current_evidence_bundle["results"])
        state.pending_macro_action = {
            "objective": "Prepare a catalyst for XRD",
            "planned_operations": ["Mix reagents and collect sample"],
        }

        agent._select_action_evidence_for_route(state)

        self.assertEqual(state.current_evidence_bundle["results"], [])
        self.assertEqual(
            state.current_evidence_bundle["reference_candidates"][0]["reason_code"],
            "route_unknown",
        )

    def test_natural_hit_is_route_screened_without_explicit_references(self) -> None:
        _write_paper(
            self.kb,
            "natural_pba.json",
            title="NiFe-PBA nanocubes from ferricyanide",
            summary=PBA_EXCERPT,
            steps=[
                _step(
                    "Make NiFe-PBA nanocubes",
                    "Ni nitrate; sodium citrate; K3Fe(CN)6",
                    "4 mmol Ni nitrate; age 24 h",
                    PBA_EXCERPT,
                )
            ],
            doi="10.1000/natural-pba-route",
        )
        agent = self._agent()
        state = self._state()
        agent._refresh_action_evidence(
            state, planning_mode="bootstrap", observation_point="XRD"
        )
        self.assertTrue(state.current_evidence_bundle["results"])
        self._choose_coprecipitation(state)

        agent._select_action_evidence_for_route(state)

        self.assertEqual(state.current_evidence_bundle["results"], [])
        self.assertTrue(
            any(
                item["reason_code"] == "route_mismatch"
                for item in state.current_evidence_bundle["selection_log"]
            )
        )

    def test_bootstrap_and_post_observation_prompts_consume_selected_route_only(self) -> None:
        self._pba_distractors()
        compatible = _write_paper(
            self.kb,
            "specified_coprecipitation.json",
            title="NiFe-LDH alkaline carbonate co-precipitation protocol",
            summary=COPPT_EXCERPT,
            steps=[
                _step(
                    "Co-precipitate NiFe-LDH with NaOH and Na2CO3",
                    "Ni nitrate; Fe nitrate; NaOH; Na2CO3",
                    "80 mL water; add NaOH and Na2CO3 dropwise",
                    COPPT_EXCERPT,
                )
            ],
            doi="10.1000/selected-nife-ldh",
        )

        captured: dict[str, tuple[str, list[str]]] = {}
        for mode, planner in (
            ("bootstrap", "_step_macro_plan_design"),
            ("post_observation", "_step_post_observation_macro_plan_design"),
        ):
            agent = self._agent()
            agent._use_llm = True
            state = self._state(compatible)

            def invoke_json(
                _system_prompt: str,
                contextual_prompt: str,
                **_kwargs: object,
            ) -> dict:
                if not state.pending_macro_action:
                    action = {}
                    self._choose_coprecipitation(state)
                    action.update(state.pending_macro_action)
                    state.pending_macro_action = {}
                    action["completion_condition"] = "XRD observation recorded"
                    return action
                captured[mode] = (
                    contextual_prompt,
                    [
                        item["paper_id"]
                        for item in state.current_evidence_bundle["results"]
                    ],
                )
                raise ValueError("stop after recording the planning prompt")

            agent.invoke_json = invoke_json
            with self.assertRaisesRegex(RuntimeError, "stop after recording"):
                getattr(agent, planner)(state)

        bootstrap_prompt, bootstrap_ids = captured["bootstrap"]
        post_prompt, post_ids = captured["post_observation"]
        self.assertEqual(bootstrap_ids, post_ids)
        self.assertEqual(len(bootstrap_ids), 1)
        for prompt in (bootstrap_prompt, post_prompt):
            self.assertIn(bootstrap_ids[0], prompt)
            self.assertIn("NaOH and Na2CO3 were added dropwise", prompt)
            self.assertNotIn("4 mmol Ni nitrate", prompt)

    def test_post_observation_prompt_uses_one_bounded_evidence_projection(self) -> None:
        references = [
            _write_paper(
                self.kb,
                f"compatible_{index}.json",
                title=f"NiFe-LDH NaOH carbonate co-precipitation route {index}",
                summary=COPPT_EXCERPT,
                steps=[
                    _step(
                        "Co-precipitate NiFe-LDH with NaOH and Na2CO3",
                        "Ni nitrate; Fe nitrate; NaOH; Na2CO3",
                        f"80 mL water; add NaOH and Na2CO3 dropwise; batch {index}",
                        COPPT_EXCERPT,
                    )
                ],
                doi=f"10.1000/compatible-{index}",
            )
            for index in range(3)
        ]
        agent = self._agent()
        agent._use_llm = True
        state = self._state(*references)
        captured: dict[str, object] = {}

        def invoke_json(
            _system_prompt: str,
            contextual_prompt: str,
            **_kwargs: object,
        ) -> dict:
            if not state.pending_macro_action:
                self._choose_coprecipitation(state)
                action = {
                    **state.pending_macro_action,
                    "completion_condition": "XRD observation recorded",
                }
                state.pending_macro_action = {}
                return action
            results = state.current_evidence_bundle["results"]
            captured["prompt"] = contextual_prompt
            captured["all_ids"] = [item["paper_id"] for item in results]
            captured["projected_ids"] = [
                item["paper_id"]
                for item in agent._compact_action_evidence_for_planning(results)
            ]
            raise ValueError("stop after recording the planning prompt")

        agent.invoke_json = invoke_json
        with self.assertRaisesRegex(RuntimeError, "stop after recording"):
            agent._step_post_observation_macro_plan_design(state)

        prompt = captured["prompt"]
        all_ids = captured["all_ids"]
        projected_ids = captured["projected_ids"]
        self.assertEqual(len(all_ids), 3)
        self.assertEqual(len(projected_ids), 2)
        for paper_id in projected_ids:
            self.assertEqual(prompt.count(paper_id), 1)
        for paper_id in set(all_ids) - set(projected_ids):
            self.assertNotIn(paper_id, prompt)

    def test_ldh_urea_route_is_not_misclassified_by_later_koh_test(self) -> None:
        urea_with_koh_test = _write_paper(
            self.kb,
            "urea_synthesis_koh_oer.json",
            title="NiFe-LDH urea co-precipitation and alkaline OER testing",
            summary=(
                "NiFe-LDH was synthesized by urea co-precipitation, then heated "
                "at 100 °C under reflux for 48 h. The resulting catalyst was "
                "tested separately for OER in 1 M KOH."
            ),
            steps=[
                _step(
                    "Urea co-precipitation and reflux synthesis of NiFe-LDH",
                    "Ni nitrate; Fe nitrate; urea",
                    "100 °C under reflux for 48 h",
                    UREA_EXCERPT,
                ),
                _step(
                    "OER electrochemical testing",
                    "NiFe-LDH catalyst; KOH electrolyte",
                    "1 M KOH",
                    "The NiFe-LDH catalyst was evaluated for OER in 1 M KOH.",
                ),
            ],
            doi="10.1000/urea-ldh-koh-oer",
        )
        agent = self._agent()
        state = self._state(urea_with_koh_test)
        self._refresh_and_choose(agent, state)

        candidate = state.current_evidence_bundle["reference_candidates"][0]
        self.assertEqual(candidate["status"], "excluded")
        self.assertIn(candidate["reason_code"], {"route_mismatch", "route_unknown"})
        self.assertNotIn(
            candidate["paper_id"],
            [item["paper_id"] for item in state.current_evidence_bundle["results"]],
        )

    def test_na2co3_formula_counts_as_compatible_alkali_coprecipitation(self) -> None:
        carbonate = _write_paper(
            self.kb,
            "na2co3_ldh.json",
            title="NiFe-LDH co-precipitation synthesis",
            summary=(
                "NiFe-LDH was formed by co-precipitation. Na2CO3 was added "
                "dropwise to Ni and Fe nitrate precursor solution."
            ),
            steps=[
                _step(
                    "Co-precipitate NiFe-LDH with 10 mL Na2CO3 for 20 min",
                    "Ni nitrate; Fe nitrate; Na2CO3",
                    "Add Na2CO3 dropwise to co-precipitate NiFe-LDH",
                    "Na2CO3 was added dropwise to Ni and Fe nitrate precursor solution.",
                )
            ],
            doi="10.1000/na2co3-ldh",
        )
        agent = self._agent()
        state = self._state(carbonate)
        self._refresh_and_choose(agent, state)

        candidate = state.current_evidence_bundle["reference_candidates"][0]
        self.assertEqual(candidate["status"], "selected")
        self.assertEqual(candidate["reason_code"], "route_match")
        route_outline = json.dumps(candidate["route_outline"], ensure_ascii=False)
        self.assertNotIn("10 mL", route_outline)
        self.assertNotIn("20 min", route_outline)
        self.assertEqual(
            state.current_evidence_bundle["results"][0]["paper_id"],
            candidate["paper_id"],
        )

    def test_explicit_compatible_source_survives_two_higher_scored_natural_hits(self) -> None:
        for index in range(2):
            _write_paper(
                self.kb,
                f"natural_high_score_{index}.json",
                title=(
                    "NiFe LDH NaOH Na2CO3 coprecipitation XRD "
                    f"high score route {index}"
                ),
                summary=COPPT_EXCERPT,
                steps=[
                    _step(
                        "Co-precipitate NiFe-LDH with NaOH and Na2CO3",
                        "Ni nitrate; Fe nitrate; NaOH; Na2CO3",
                        "80 mL water; add NaOH and Na2CO3 dropwise",
                        COPPT_EXCERPT,
                    )
                ],
                doi=f"10.1000/natural-high-{index}",
            )
        explicit = _write_paper(
            self.kb,
            "explicit_low_score.json",
            title="Protocol C",
            summary=COPPT_EXCERPT,
            steps=[
                _step(
                    "Co-precipitate NiFe-LDH with NaOH and Na2CO3",
                    "Ni nitrate; Fe nitrate; NaOH; Na2CO3",
                    "80 mL water; add NaOH and Na2CO3 dropwise",
                    COPPT_EXCERPT,
                )
            ],
            doi="10.1000/explicit-low-score",
        )
        agent = self._agent()
        state = self._state(explicit)
        state.event.query = "NiFe LDH NaOH Na2CO3 coprecipitation XRD"
        state.survey_queries = [state.event.query]
        self._refresh_and_choose(agent, state)

        results = state.current_evidence_bundle["results"]
        explicit_id = state.current_evidence_bundle["reference_candidates"][0]["paper_id"]
        self.assertGreaterEqual(len(results), 3)
        self.assertEqual(results[0]["paper_id"], explicit_id)
        self.assertGreater(results[1]["score"], results[0]["score"])
        projected = agent._compact_action_evidence_for_planning(results)
        self.assertLessEqual(len(projected), 2)
        self.assertIn(explicit_id, [item["paper_id"] for item in projected])

    def test_online_excerpt_can_establish_route_without_structured_steps(self) -> None:
        ldh = _write_paper(
            self.kb,
            "online_ldh.json",
            title="NiFe-LDH synthesis protocol",
            summary=(
                "NiFe-LDH was obtained by co-precipitation: Na2CO3 was added "
                "dropwise to the Ni and Fe nitrate precursor solution."
            ),
            steps=[
                _step(
                    "Co-precipitate NiFe-LDH",
                    "Ni nitrate; Fe nitrate; Na2CO3",
                    "Add Na2CO3 dropwise",
                    COPPT_EXCERPT,
                )
            ],
            doi="10.1000/online-ldh-coppt",
        )
        pba = _write_paper(
            self.kb,
            "online_pba.json",
            title="NiFe-PBA synthesis",
            summary=PBA_EXCERPT,
            steps=[
                _step(
                    "Make NiFe-PBA nanocubes",
                    "Ni nitrate; K3Fe(CN)6",
                    "4 mmol Ni nitrate",
                    PBA_EXCERPT,
                )
            ],
            doi="10.1000/online-pba",
        )
        oer = _write_paper(
            self.kb,
            "online_oer.json",
            title="NiFe-LDH OER test",
            summary="NiFe-LDH electrodes were tested for OER in 1 M KOH electrolyte.",
            steps=[
                _step(
                    "OER electrochemical test",
                    "NiFe-LDH electrode; KOH electrolyte",
                    "1 M KOH",
                    "NiFe-LDH electrodes were tested for OER in 1 M KOH electrolyte.",
                )
            ],
            doi="10.1000/online-ldh-oer",
        )

        class OnlineService:
            def run(self, **_kwargs: object) -> dict:
                return {
                    "status": "success",
                    "retrieval_status": "success",
                    "results": [
                        {
                            "title": "NiFe-LDH synthesis protocol",
                            "doi": "10.1000/online-ldh-coppt",
                            "paper_id": "doi_10_1000_online_ldh_coppt",
                            "verification_status": "verified_doi",
                            "full_text_status": "parsed",
                            "corpus_files": [str(ldh)],
                            "evidence_excerpt": ldh.read_text(encoding="utf-8")[:3000],
                        },
                        {
                            "title": "NiFe-PBA synthesis",
                            "doi": "10.1000/online-pba",
                            "paper_id": "doi_10_1000_online_pba",
                            "verification_status": "verified_doi",
                            "full_text_status": "parsed",
                            "corpus_files": [str(pba)],
                            "evidence_excerpt": pba.read_text(encoding="utf-8")[:3000],
                        },
                        {
                            "title": "NiFe-LDH OER test",
                            "doi": "10.1000/online-ldh-oer",
                            "paper_id": "doi_10_1000_online_ldh_oer",
                            "verification_status": "verified_doi",
                            "full_text_status": "parsed",
                            "corpus_files": [str(oer)],
                            "evidence_excerpt": oer.read_text(encoding="utf-8")[:3000],
                        },
                        {
                            "title": "NiFe-LDH synthesis with missing corpus file",
                            "doi": "10.1000/online-no-corpus",
                            "paper_id": "doi_10_1000_online_no_corpus",
                            "verification_status": "verified_doi",
                            "full_text_status": "parsed",
                            "evidence_excerpt": (
                                "NiFe-LDH was co-precipitated using Na2CO3, "
                                "but there is no local corpus file to verify this excerpt."
                            ),
                        },
                    ],
                    "errors": [],
                }

        agent = self._agent()
        agent._online_literature = True
        agent._online_research_service = lambda _state: OnlineService()
        state = self._state()
        agent._refresh_action_evidence(
            state, planning_mode="bootstrap", observation_point="XRD"
        )
        self._choose_coprecipitation(state)
        agent._select_action_evidence_for_route(state)

        selected = state.current_evidence_bundle["results"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["doi"], "10.1000/online-ldh-coppt")
        self.assertTrue(selected[0].get("paper_id") or selected[0].get("evidence_id"))
        self.assertIn("Na2CO3", selected[0]["evidence_excerpt"])
        excluded = {
            item["title"]: item["reason_code"]
            for item in state.current_evidence_bundle["selection_log"]
            if item["status"] == "excluded"
        }
        self.assertIn("NiFe-PBA synthesis", excluded)
        self.assertIn("NiFe-LDH OER test", excluded)
        self.assertEqual(
            excluded["NiFe-LDH synthesis with missing corpus file"], "route_unknown"
        )

    def test_frozen_natural_hit_without_doi_deduplicates_against_explicit_doi(self) -> None:
        title = "NiFe-LDH NaOH carbonate co-precipitation protocol"
        step = _step(
            "Co-precipitate NiFe-LDH with NaOH and Na2CO3",
            "Ni nitrate; Fe nitrate; NaOH; Na2CO3",
            "80 mL water; NaOH/Na2CO3 dropwise",
            COPPT_EXCERPT,
        )
        explicit = _write_paper(
            self.kb,
            "same_paper.json",
            title=title,
            summary=COPPT_EXCERPT,
            steps=[step],
            doi="10.1000/same-paper-old-bundle",
        )
        agent = self._agent()
        state = self._state(explicit)
        state.current_evidence_bundle = {
            "results": [
                {
                    "paper_id": "local_old_path_hash",
                    "title": title,
                    "source": "local_knowledge_base",
                    "corpus_files": [str(explicit)],
                    "synthesis_summary": COPPT_EXCERPT,
                    "steps": [step],
                    "evidence_excerpt": COPPT_EXCERPT,
                    "score": 50.0,
                }
            ],
            "reference_candidates": [],
        }
        self._choose_coprecipitation(state)

        agent._select_action_evidence_for_route(state)

        selected_id = state.current_evidence_bundle["reference_candidates"][0]["paper_id"]
        self.assertEqual(
            [item["paper_id"] for item in state.current_evidence_bundle["results"]],
            [selected_id],
        )
        self.assertTrue(
            any(
                item.get("origin") == "retrieval"
                and item["reason_code"] == "duplicate"
                for item in state.current_evidence_bundle["selection_log"]
            )
        )

    def test_same_title_without_doi_keeps_distinct_local_experiments(self) -> None:
        title = "NiFe-LDH alkaline co-precipitation protocol"
        references = [
            _write_paper(
                self.kb,
                f"same_title_distinct_{index}.json",
                title=title,
                summary=(
                    f"Experiment batch {index}: NiFe-LDH was made by "
                    "NaOH and Na2CO3 co-precipitation. " + COPPT_EXCERPT
                ),
                steps=[
                    _step(
                        "Co-precipitate NiFe-LDH with NaOH and Na2CO3",
                        "Ni nitrate; Fe nitrate; NaOH; Na2CO3",
                        f"Batch {index}: {10 + index} mL water; add NaOH/Na2CO3",
                        COPPT_EXCERPT,
                    )
                ],
                doi="",
            )
            for index in range(2)
        ]
        agent = self._agent()
        state = self._state(*references)
        self._refresh_and_choose(agent, state)

        candidates = state.current_evidence_bundle["reference_candidates"]
        self.assertEqual(len(candidates), 2)
        self.assertEqual([item["status"] for item in candidates], ["selected", "selected"])
        self.assertEqual(len({item["paper_id"] for item in candidates}), 2)
        self.assertEqual(len(state.current_evidence_bundle["results"]), 2)

    def test_synthesis_projection_does_not_carry_later_oer_koh_amount(self) -> None:
        synthesis = _write_paper(
            self.kb,
            "synthesis_then_oer.json",
            title="NiFe-LDH carbonate co-precipitation and OER test",
            summary=(
                "NiFe-LDH was synthesized by Na2CO3 co-precipitation. "
                "The resulting catalyst was later tested for OER in 1 M KOH."
            ),
            steps=[
                _step(
                    "Co-precipitate NiFe-LDH with Na2CO3",
                    "Ni nitrate; Fe nitrate; Na2CO3",
                    "Add Na2CO3 dropwise, then age for 24 h",
                    "Na2CO3 was added dropwise to Ni and Fe nitrate solution.",
                ),
                _step(
                    "Perform OER electrochemical test",
                    "NiFe-LDH electrode; KOH electrolyte",
                    "1 M KOH electrolyte for OER",
                    "The electrode was later tested for OER in 1 M KOH.",
                ),
            ],
            doi="10.1000/synthesis-then-oer",
        )
        agent = self._agent()
        state = self._state(synthesis)
        self._refresh_and_choose(agent, state)

        self.assertEqual(
            state.current_evidence_bundle["reference_candidates"][0]["status"],
            "selected",
        )
        projected = agent._compact_action_evidence_for_planning(
            state.current_evidence_bundle["results"]
        )
        projection_text = json.dumps(projected, ensure_ascii=False)
        self.assertIn("Na2CO3", projection_text)
        self.assertNotIn("1 M KOH", projection_text)

    def test_action_design_prompt_lists_all_four_explicit_sources_without_recipe_numbers(self) -> None:
        specs = [
            ("First LDH urea reflux", "7.5 mM", "urea reflux NiFe-LDH"),
            ("Second NiFe-PBA route", "4 mmol", "NiFe-PBA ferricyanide"),
            ("Third LDH carbonate route", "13 mL", "Na2CO3 co-precipitation NiFe-LDH"),
            ("Fourth LDH carbonate route", "27 g", "NaOH co-precipitation NiFe-LDH"),
        ]
        references = []
        for index, (title, amount, operation) in enumerate(specs):
            references.append(
                _write_paper(
                    self.kb,
                    f"explicit_{index}.json",
                    title=title,
                    summary=(
                        f"The {operation} synthesis used {amount} precursor "
                        "before product isolation."
                    ),
                    steps=[
                        _step(
                            f"{operation} using {amount} precursor",
                            "Ni and Fe precursors",
                            f"Use {amount} precursor",
                            f"The {operation} synthesis used {amount} precursor.",
                        )
                    ],
                    doi=f"10.1000/action-source-{index}",
                )
            )
        agent = self._agent()
        agent._use_llm = True
        state = self._state(*references)
        captured: dict[str, str] = {}

        def invoke(
            _state: ResearchAgentState,
            task_name: str,
            prompt: str,
            **_kwargs: object,
        ) -> dict:
            self.assertEqual(task_name, "macro_action_design_bootstrap")
            captured["context"] = agent._compact_state_context(state, task_name)
            return {
                "objective": "Synthesize NiFe-LDH by NaOH/Na2CO3 co-precipitation",
                "planned_operations": ["Co-precipitate NiFe-LDH using NaOH and Na2CO3"],
                "completion_condition": "XRD observation recorded",
            }

        agent._invoke_state_json = invoke
        agent._step_macro_action_design(state, "bootstrap")

        candidates = state.current_evidence_bundle["reference_candidates"]
        self.assertEqual(len(candidates), 4)
        context = json.loads(captured["context"])
        prompt = captured["context"]
        self.assertEqual(len(context["explicit_reference_candidates"]), 4)
        for title, amount, _operation in specs:
            self.assertIn(title, prompt)
            self.assertNotIn(amount, prompt)

    def test_separate_urea_and_reflux_steps_do_not_create_a_joint_route(self) -> None:
        source = {
            "title": "NiFe-LDH experimental collection",
            "synthesis_summary": (
                "The relationship between the following experimental steps "
                "is not specified in this record."
            ),
            "steps": [
                {
                    "操作": "Add urea to a NiFe precursor solution",
                    "参数": "17.5 mM urea",
                },
                {
                    "操作": "Reflux a separate mixture",
                    "参数": "100 °C for 48 h",
                },
            ],
        }

        self.assertEqual(
            ResearchAgent._source_route_profile(source), ("ldh", "unknown")
        )

    def test_source_with_two_distinct_synthesis_routes_is_unknown(self) -> None:
        source = {
            "title": "NiFe-LDH alternative synthesis routes",
            "synthesis_summary": (
                "Two alternative NiFe-LDH methods are reported: a urea reflux "
                "route and a NaOH co-precipitation route."
            ),
            "steps": [
                {
                    "操作": "Synthesize NiFe-LDH by urea reflux",
                    "参数": "17.5 mM urea; reflux at 100 °C for 48 h",
                },
                {
                    "操作": "Synthesize NiFe-LDH by NaOH co-precipitation",
                    "参数": "Add 10 mL NaOH dropwise",
                },
            ],
        }

        self.assertEqual(
            ResearchAgent._source_route_profile(source), ("ldh", "unknown")
        )


if __name__ == "__main__":
    unittest.main()

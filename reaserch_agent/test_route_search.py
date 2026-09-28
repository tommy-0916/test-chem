"""The search loop counts only facts accepted by trusted route receipts."""

from __future__ import annotations

import unittest

from chem_agent_contracts.route_decision import (
    RouteSearchBudgetV1, decide_routes,
)
from chem_agent_contracts.test_route_decision import candidate, goal, receipt
from reaserch_agent.route_discovery import RouteDiscoveryResultV1
from reaserch_agent.route_pipeline import RoutePipelineResultV1
from reaserch_agent.route_search import (
    RouteSearchAcquisitionV1,
    missing_required_route_fields,
    search_route_evidence_v1,
    verified_route_fact_ids,
)


class RouteSearchTest(unittest.TestCase):
    @staticmethod
    def _evaluate(protocols):
        task = goal()
        candidates = []
        for item in protocols:
            if "route_id" not in item:
                continue
            route = candidate(
                route_id=item["route_id"],
                status="supported" if item.get("verified") else "unknown",
            )
            if not item.get("verified"):
                # This acquisition has made no verified claim, including the
                # output name that a selected route now requires.
                for field in route.evidence_matrix:
                    field.status = "unknown"
                    field.provenance = None
                    field.evidence_id = ""
            candidates.append(route)
        decision = decide_routes(task, candidates, receipt)
        return RoutePipelineResultV1(
            goal=task,
            discovery=RouteDiscoveryResultV1(candidates=candidates),
            decision=decision,
        )

    def test_new_source_and_science_verified_fact_can_select(self) -> None:
        requests = []

        def acquire(queries, max_papers):
            requests.append((dict(queries), max_papers))
            return RouteSearchAcquisitionV1(
                protocols=({"paper_id": "paper-R1", "route_id": "R1", "verified": True},),
                paper_ids=("paper-R1",),
            )

        result = search_route_evidence_v1(
            goal(), [], evaluate=self._evaluate, acquire=acquire,
            budget=RouteSearchBudgetV1(max_search_rounds=2, max_candidate_papers=1),
        )
        self.assertEqual(result.pipeline.decision.status, "selected_for_planning")
        self.assertEqual(result.stop_reason, "decision_reached")
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(len(result.rounds[0].budget_round.new_verified_fact_ids), 2)
        self.assertEqual(requests[0][1], 1)
        self.assertEqual(list(requests[0][0]), goal().required_fields)

    def test_unknown_claim_is_not_a_new_verified_fact(self) -> None:
        calls = []

        def acquire(queries, max_papers):
            calls.append(1)
            return RouteSearchAcquisitionV1(
                protocols=({"paper_id": "paper-R1", "route_id": "R1"},),
                paper_ids=("paper-R1",),
            )

        result = search_route_evidence_v1(
            goal(), [], evaluate=self._evaluate, acquire=acquire,
        )
        self.assertEqual(result.pipeline.decision.status, "unresolved")
        self.assertEqual(result.stop_reason, "no_new_verified_facts")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.rounds[0].budget_round.new_verified_fact_ids, [])

    def test_budget_and_service_failure_are_distinct(self) -> None:
        no_budget = search_route_evidence_v1(
            goal(), [], evaluate=self._evaluate,
            acquire=lambda _queries, _max_papers: self.fail("should not search"),
            budget=RouteSearchBudgetV1(max_search_rounds=0),
        )
        self.assertEqual(no_budget.stop_reason, "round_budget_exhausted")
        failed = search_route_evidence_v1(
            goal(), [], evaluate=self._evaluate,
            acquire=lambda _queries, _max_papers: (_ for _ in ()).throw(OSError("offline")),
        )
        self.assertEqual(failed.stop_reason, "search_service_or_extraction_failure")
        self.assertEqual(failed.rounds, [])

    def test_unreported_paper_cannot_escape_paper_budget(self) -> None:
        result = search_route_evidence_v1(
            goal(), [], evaluate=self._evaluate,
            acquire=lambda _queries, _max_papers: RouteSearchAcquisitionV1(
                protocols=({"paper_id": "paper-R1", "route_id": "R1", "verified": True},),
                paper_ids=("another-paper",),
            ),
        )
        self.assertEqual(result.stop_reason, "search_service_or_extraction_failure")
        self.assertEqual(result.pipeline.decision.status, "unresolved")

    def test_source_and_science_receipts_both_required_for_fact_count(self) -> None:
        evaluated = self._evaluate([{"paper_id": "paper-R1", "route_id": "R1", "verified": True}])
        self.assertEqual(len(verified_route_fact_ids(evaluated)), 2)
        self.assertEqual(missing_required_route_fields(goal(), evaluated), [])
        record = evaluated.decision.candidates[0]
        record.validation.audited_field_paths = []
        self.assertEqual(verified_route_fact_ids(evaluated), set())
        self.assertEqual(missing_required_route_fields(goal(), evaluated), goal().required_fields)


if __name__ == "__main__":
    unittest.main()

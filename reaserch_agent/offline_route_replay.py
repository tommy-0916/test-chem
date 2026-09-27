"""Replay a recorded experimental-group proposal through the production route evaluator.

This command evaluates source, scientific, and abstract capability evidence. It
does not call the Research publication gate, Device hard gate, or an LLM. A JSON
protocol is still a proposal: only the independently configured signatures and
the evaluator's source checks can verify it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from chem_agent_contracts.route_candidate import RouteGoalV1

from .route_pipeline import evaluate_route_decision_v1
from .run_research_agent import load_route_trust_config
from .tools.literature_acquisition import default_kb_dir
from .workflow import ResearchAgent


def _read_json(path_text: str) -> Any:
    path = Path(path_text).expanduser().resolve(strict=True)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)


def replay_route_evaluation(
    goal: RouteGoalV1 | Mapping[str, Any],
    protocols: Sequence[Mapping[str, Any]],
    *,
    knowledge_base_dir: str | Path,
    route_trust: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate supplied proposals with real source, science, and capability gates.

    No fixture-only evaluator mode or mocked audit is used. With no signed
    source/review authority, the outcome remains unresolved. Candidate-level
    capability preflight reads the current capability index; it is not the
    Device Agent's plan-level hard gate.
    """
    typed_goal = RouteGoalV1.model_validate(goal, strict=True)
    if not isinstance(protocols, Sequence) or isinstance(protocols, (str, bytes)):
        raise ValueError("protocols must be a JSON array")
    if any(not isinstance(item, Mapping) for item in protocols):
        raise ValueError("each protocol must be a JSON object")
    root = Path(knowledge_base_dir).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("knowledge_base_dir must be a directory")
    trust = dict(route_trust or {})

    # The evaluator uses Research's Phase 1–5 methods, which do not need a
    # search corpus. Keep its otherwise eager legacy corpus loader away from
    # the goal/protocol JSON and from unrelated KB summaries. The inert model
    # prevents accidental LLM construction or invocation.
    with tempfile.TemporaryDirectory(prefix="chem-route-replay-") as empty_corpus:
        science_agent = ResearchAgent(
            model=object(), use_llm=False, contract_version="v2",
            knowledge_base_dir=empty_corpus, memory_dir=empty_corpus,
            enable_memory=False, enable_online_literature=False,
            enable_web_search=False,
        )
        result = evaluate_route_decision_v1(
            typed_goal, protocols, source_root=root,
            signed_source_events=trust.get("signed_route_source_events"),
            trusted_public_keys=trust.get("trusted_route_public_keys"),
            signed_route_signature_reviews=trust.get("signed_route_signature_reviews"),
            trusted_route_signature_public_keys=(
                trust.get("trusted_route_signature_public_keys")
            ),
            verified_capabilities_by_group=(
                trust.get("trusted_route_capabilities_by_group")
            ),
            verified_group_roles_by_group=(
                trust.get("trusted_route_group_roles_by_group")
            ),
            science_agent=science_agent,
        )
    input_blockers: list[str] = []
    if not protocols:
        input_blockers.append("protocols_missing")
    if not trust.get("signed_route_source_events"):
        input_blockers.append("signed_source_events_missing")
    if not trust.get("signed_route_signature_reviews"):
        input_blockers.append("signed_route_signature_reviews_missing")

    discovery = [item.model_dump(mode="json") for item in result.discovery.diagnostics]
    candidate_reasons = {
        item.route_id: list(item.reasons) for item in result.decision.candidates
    }
    reason_codes = {
        "input": input_blockers,
        "discovery": sorted({item["reason_code"] for item in discovery}),
        "compilation": sorted({
            item["reason_code"] for item in result.compilation_diagnostics
        }),
        "validation": dict(result.validation_diagnostics),
        "candidates": candidate_reasons,
        "decision": list(result.decision.decision_reasons),
    }
    return {
        "schema_version": "offline-route-replay/v1",
        "scope": "route_evaluation_only",
        "research_publication_executed": False,
        "device_hard_gate_executed": False,
        "decision_status": result.decision.status,
        "selected_route_id": result.decision.selected_route_id,
        "reason_codes": reason_codes,
        "discovery_diagnostics": discovery,
        "compilation_diagnostics": result.compilation_diagnostics,
        "validation_diagnostics": result.validation_diagnostics,
        "decision": result.decision.model_dump(mode="json"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal-json", required=True, help="RouteGoalV1 JSON file")
    parser.add_argument(
        "--protocols-json", required=True,
        help="Recorded experimental-group proposal JSON array",
    )
    parser.add_argument(
        "--knowledge-base-dir", default=str(default_kb_dir()),
        help="KB root containing registered original PDFs",
    )
    parser.add_argument(
        "--route-trust-config",
        help="Independent deployment trust JSON, outside the KB",
    )
    parser.add_argument("--output-json", help="Optional report file; default stdout")
    args = parser.parse_args(argv)
    try:
        goal = _read_json(args.goal_json)
        protocols = _read_json(args.protocols_json)
        trust = load_route_trust_config(
            args.route_trust_config, args.knowledge_base_dir,
        )
        report = replay_route_evaluation(
            goal, protocols, knowledge_base_dir=args.knowledge_base_dir,
            route_trust=trust,
        )
    except (OSError, UnicodeError, ValueError, TypeError, SystemExit) as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output_json:
        Path(args.output_json).expanduser().resolve().write_text(
            rendered + "\n", encoding="utf-8",
        )
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

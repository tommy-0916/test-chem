#!/usr/bin/env python3
"""Run capability checks for the B2 post-observation path."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reaserch_agent import ResearchAgent


DEFAULT_QUERY = "合成普鲁士蓝样品并通过 XRD 确认目标物相"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "B2_test"


SCENARIOS: Dict[str, Dict[str, Any]] = {
    "normal": {
        "label": "正常 observation：目标物相匹配",
        "observation": {
            "observation_type": "XRD",
            "summary": (
                "XRD characteristic peaks matched the target Prussian Blue phase; "
                "observation completed successfully."
            ),
            "metrics": {"phase_match": True, "impurity_phase": False},
        },
        "expected_paths": {"normal_progress"},
    },
    "abnormal_impurity": {
        "label": "异常 observation：XRD 杂相",
        "observation": {
            "observation_type": "XRD",
            "summary": (
                "XRD failed: strong impurity phase appeared and target Prussian Blue "
                "peaks were weak."
            ),
            "metrics": {"phase_match": False, "impurity_phase": True},
        },
        "expected_paths": {"stage_internal", "current_stage", "stage_route"},
    },
    "abnormal_no_precipitate": {
        "label": "异常 observation：无沉淀",
        "observation": {
            "observation_type": "visual",
            "summary": (
                "No precipitate formed after mixing; the solution stayed clear and "
                "the expected blue product was not observed."
            ),
            "metrics": {"precipitate": False},
        },
        "expected_paths": {"stage_internal", "current_stage", "stage_route"},
    },
    "route_invalid": {
        "label": "路线级异常：目标 observation point 可能错误",
        "observation": {
            "observation_type": "diagnostic",
            "summary": (
                "route invalid: the target observation point is wrong and the current "
                "route cannot explain the result."
            ),
            "metrics": {"route_valid": False},
        },
        "expected_paths": {"stage_route", "manual_handoff", "current_stage"},
    },
    "inconclusive": {
        "label": "不确定 observation：信号噪声较大",
        "observation": {
            "observation_type": "XRD",
            "summary": (
                "The XRD signal is noisy and inconclusive; several weak peaks may be "
                "related to Prussian Blue but the phase assignment is unclear."
            ),
            "metrics": {"phase_match": None, "noise": "high"},
        },
        "expected_paths": {"normal_progress"},
    },
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bootstrap B1 once, then feed B2 one or more observation scenarios "
            "to evaluate dynamic adjustment behavior."
        )
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help=f"Research query used for the initial B1 bootstrap. Default: {DEFAULT_QUERY}",
    )
    parser.add_argument(
        "--scenario",
        choices=["all", *SCENARIOS.keys()],
        default="all",
        help="Which B2 observation scenario to run. Default: all",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Directory for saved B1/B2 state JSON files. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--previous-state",
        help="Optional existing B1/B2 state JSON. If omitted, the script runs B1 first.",
    )
    parser.add_argument(
        "--disable-llm",
        action="store_true",
        help="Force heuristic mode. Omit this to use configured LLM credentials.",
    )
    parser.add_argument(
        "--model-name",
        help="Optional model name override, e.g. deepseek-v4-pro.",
    )
    parser.add_argument(
        "--api-key",
        help="Optional API key override. Prefer environment variables.",
    )
    parser.add_argument(
        "--base-url",
        help="Optional OpenAI-compatible base URL override.",
    )
    parser.add_argument(
        "--knowledge-base-dir",
        help="Optional knowledge base directory. Defaults to reaserch_agent/chem_kb.",
    )
    parser.add_argument(
        "--memory-dir",
        help="Optional memory directory. Defaults to the knowledge base directory.",
    )
    parser.add_argument(
        "--enable-memory",
        action="store_true",
        help="Enable memory retrieval. By default memory is disabled.",
    )
    parser.add_argument(
        "--max-survey-rounds",
        type=int,
        default=2,
        help="Maximum survey rounds for B1 and abnormal B2 repair. Default: 2",
    )
    parser.add_argument(
        "--print-state-json",
        action="store_true",
        help="Print each final B2 state JSON.",
    )
    return parser


def configure_model_env(args: argparse.Namespace) -> None:
    if args.model_name:
        os.environ["REFINER_LLM_MODEL_NAME"] = args.model_name
    if args.api_key:
        os.environ["REFINER_LLM_API_KEY"] = args.api_key
    if args.base_url:
        os.environ["REFINER_LLM_ENDPOINT_URL"] = args.base_url


def slugify(text: str, max_len: int = 56) -> str:
    normalized = re.sub(r"\s+", "_", text.strip())
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_\-]+", "", normalized)
    normalized = normalized.strip("_-")
    return (normalized[:max_len] or "scenario").strip("_-")


def build_state_json(state: Any) -> str:
    payload = state.debug_snapshot() if hasattr(state, "debug_snapshot") else state.to_dict()
    return json.dumps(payload, ensure_ascii=False, indent=2)


def load_state(path_text: str) -> Dict[str, Any]:
    path = Path(path_text).expanduser().resolve()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"previous-state 不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("previous-state 必须是 JSON object。")
    return parsed


def save_state(state: Any, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename
    output_path.write_text(build_state_json(state), encoding="utf-8")
    return output_path


def scenario_names(selection: str) -> Iterable[str]:
    if selection == "all":
        return SCENARIOS.keys()
    return [selection]


def print_b1_summary(state: Any, path: Path) -> None:
    print("B1 bootstrap")
    print(f"  status: {state.status}")
    print(f"  stage_route: {state.stage_route}")
    print(f"  current_stage: {state.current_stage}")
    if state.knowledge_hits:
        print(f"  top_knowledge_hit: {state.knowledge_hits[0].title}")
    print(f"  saved_state: {path}")


def print_b2_summary(name: str, scenario: Dict[str, Any], state: Any, path: Path) -> None:
    expected_paths = scenario["expected_paths"]
    actual_path = state.post_observation_repair_path or "(empty)"
    fit = state.observation_stage_fit.get("fits_current_stage")
    status = state.observation_stage_fit.get("status")
    macro_ops = [str(step.get("操作", "")) for step in state.macro_plan[:5]]
    path_ok = actual_path in expected_paths
    completed = state.status in {"completed", "manual_required"}

    print(f"\nB2 scenario: {name}")
    print(f"  label: {scenario['label']}")
    print(f"  status: {state.status}")
    print(f"  fit_status: {status}")
    print(f"  fits_current_stage: {fit}")
    print(f"  repair_path: {actual_path}")
    print(f"  expected_paths: {sorted(expected_paths)}")
    print(f"  path_check: {'PASS' if path_ok else 'WARN'}")
    print(f"  completion_check: {'PASS' if completed else 'FAIL'}")
    print(f"  current_stage: {state.current_stage}")
    print(f"  macro_ops: {macro_ops if macro_ops else '(empty)'}")
    print(f"  saved_state: {path}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    configure_model_env(args)
    output_dir = Path(args.output_dir).expanduser().resolve()

    agent = ResearchAgent(
        model=None,
        use_llm=not args.disable_llm,
        knowledge_base_dir=args.knowledge_base_dir,
        memory_dir=args.memory_dir,
        max_survey_rounds=args.max_survey_rounds,
        enable_memory=args.enable_memory,
    )

    if args.previous_state:
        b1_state: Any = load_state(args.previous_state)
        b1_save_path = Path(args.previous_state).expanduser().resolve()
        print(f"Using previous state: {b1_save_path}")
    else:
        b1_state = agent.run(event_type="bootstrap", query=args.query)
        b1_save_path = save_state(
            b1_state,
            output_dir,
            f"b1_{slugify(args.query)}.json",
        )
        print_b1_summary(b1_state, b1_save_path)
        if b1_state.status != "completed":
            return 1

    overall_ok = True
    for name in scenario_names(args.scenario):
        scenario = SCENARIOS[name]
        previous_state = deepcopy(b1_state)
        b2_state = agent.run(
            event_type="new observation",
            payload={"observation": scenario["observation"]},
            previous_state=previous_state,
        )
        output_path = save_state(
            b2_state,
            output_dir,
            f"b2_{name}_{slugify(args.query)}.json",
        )
        print_b2_summary(name, scenario, b2_state, output_path)
        if b2_state.status not in {"completed", "manual_required"}:
            overall_ok = False
        if b2_state.post_observation_repair_path not in scenario["expected_paths"]:
            overall_ok = False
        if args.print_state_json:
            print("\nfull_state_json:")
            print(build_state_json(b2_state))

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())

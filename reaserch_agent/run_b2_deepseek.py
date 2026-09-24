#!/usr/bin/env python3
"""Run B2 post-observation repair with DeepSeek's OpenAI-compatible API."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reaserch_agent import ResearchAgent


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_PREVIOUS_STATE = (
    Path(__file__).resolve().parent / "B1_test" / "普鲁士蓝_deepseek_new.json"
)
DEFAULT_SAVE_STATE = (
    Path(__file__).resolve().parent / "B1_test" / "普鲁士蓝_B2_deepseek_repaired.json"
)
DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"

B2_LLM_KEYS = {
    "observation_stage_fit_judge",
    "stage_progress_update",
    "post_observation_macro_plan_design",
    "device_adaptation_macro_plan_design",
    "abnormal_observation_survey_query_generate",
    "similar_abnormal_case_search",
    "post_observation_report_update",
    "stage_internal_repair_assess",
    "current_stage_repair_assess",
    "stage_route_repair_assess",
    "new_route_stage_design",
    "manual_handoff_compose",
}

DEFAULT_FEEDBACK: Dict[str, Any] = {
    "feedback_type": "experimental_observation",
    "previous_macro_plan": [
        "Prepared solution A from K4Fe(CN)6·3H2O, sodium citrate, and water.",
        "Prepared solution B from FeCl2·4H2O and water.",
        "Added solution B dropwise into solution A.",
        "Added ethylene glycol and heated the suspension in a Teflon-lined autoclave at 80 °C for 24 h.",
        "Washed, dried, and measured PXRD.",
    ],
    "observation": {
        "synthesis_process": [
            "A precipitate formed immediately when solution B was added to solution A.",
            "The mixture became turbid before solvothermal treatment.",
            "The reaction medium was water-rich and approximately neutral rather than acidic.",
            "The final product was a pale blue powder.",
        ],
        "PXRD": [
            "The pattern shows broad PBA-like diffraction peaks.",
            "The peak positions and relative intensities do not cleanly match the target K2Fe[Fe(CN)6]·2H2O reference.",
            "Weak extra peaks and elevated background are observed.",
        ],
    },
    "request": (
        "Diagnose why the obtained product may deviate from the target phase, "
        "identify which steps in the previous macro-plan should be changed, "
        "and output a corrected macro-plan for the next synthesis attempt."
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the research agent B2 post-observation path using DeepSeek. "
            "By default this script uses the Prussian Blue DeepSeek B1 state and "
            "the abnormal observation discussed in B1_test."
        )
    )
    parser.add_argument(
        "--previous-state",
        default=str(DEFAULT_PREVIOUS_STATE),
        help=f"Saved B1/B2 state JSON used as B2 context. Default: {DEFAULT_PREVIOUS_STATE}",
    )
    parser.add_argument(
        "--observation-file",
        help=(
            "Optional JSON file for the B2 payload. It can be either a full payload "
            "with observation/previous_macro_plan, or a direct observation object."
        ),
    )
    parser.add_argument(
        "--observation-json",
        help=(
            "Optional inline JSON observation object. The script wraps it as "
            "payload.observation automatically."
        ),
    )
    parser.add_argument(
        "--observation-text",
        help="Optional plain-text observation summary wrapped as payload.observation.summary.",
    )
    parser.add_argument(
        "--payload-json",
        help=(
            "Optional inline JSON payload. Mutually exclusive with --observation-file. "
            "If omitted, the built-in Prussian Blue abnormal observation is used."
        ),
    )
    parser.add_argument(
        "--query",
        default="",
        help="Optional query override for the new observation event. Empty means inherit from previous state.",
    )
    parser.add_argument(
        "--api-key",
        help=(
            "DeepSeek API key. Prefer setting DEEPSEEK_API_KEY or "
            "REFINER_LLM_API_KEY instead of passing it on the command line."
        ),
    )
    parser.add_argument(
        "--model-name",
        default=DEFAULT_MODEL,
        help=f"DeepSeek model name. Default: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"OpenAI-compatible base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--llm-timeout-seconds",
        type=float,
        default=None,
        help=(
            "Optional per-request timeout for OpenAI-compatible LLM calls. "
            "Sets REFINER_LLM_TIMEOUT_SECONDS."
        ),
    )
    parser.add_argument(
        "--llm-max-retries",
        type=int,
        default=None,
        help=(
            "Optional maximum LLM retries. Sets REFINER_LLM_MAX_RETRIES. "
            "Use 1 for fast B2 debugging."
        ),
    )
    parser.add_argument(
        "--wire-api",
        choices=["chat", "codex_responses"],
        default="chat",
        help=(
            "LLM transport. Use codex_responses for Codex CLI providers configured "
            "with wire_api=responses. Default: chat."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        help="Optional reasoning effort for codex_responses, e.g. xhigh.",
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
        help="Maximum survey rounds for abnormal B2 repair. Default: 2",
    )
    parser.add_argument(
        "--knowledge-top-k",
        type=int,
        default=5,
        help="Number of knowledge hits to keep per round. Default: 5",
    )
    parser.add_argument(
        "--memory-top-k",
        type=int,
        default=3,
        help="Number of memory hits to keep. Default: 3",
    )
    parser.add_argument(
        "--save-state",
        default=str(DEFAULT_SAVE_STATE),
        help=f"Path for saving the final B2 state JSON. Default: {DEFAULT_SAVE_STATE}",
    )
    parser.add_argument(
        "--log-dir",
        default=str(DEFAULT_LOG_DIR),
        help=f"Directory for debug log JSON files. Default: {DEFAULT_LOG_DIR}",
    )
    parser.add_argument(
        "--print-state-json",
        action="store_true",
        help="Print the full returned B2 state JSON.",
    )
    return parser


def parse_json_dict(raw_text: str, label: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} 不是合法 JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SystemExit(f"{label} 必须是 JSON object。")
    return parsed


def load_json_dict(path_text: str, label: str) -> Dict[str, Any]:
    path = Path(path_text).expanduser().resolve()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"{label} 文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} 不是合法 JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SystemExit(f"{label} 必须是 JSON object: {path}")
    return parsed


def configure_deepseek_env(args: argparse.Namespace) -> None:
    api_key = (
        args.api_key
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("REFINER_LLM_API_KEY")
    )
    if not api_key:
        raise SystemExit(
            "缺少 DeepSeek API key。请设置 DEEPSEEK_API_KEY 或传入 --api-key。"
        )

    os.environ["REFINER_LLM_API_KEY"] = api_key
    os.environ["REFINER_LLM_ENDPOINT_URL"] = args.base_url
    os.environ["REFINER_LLM_MODEL_NAME"] = args.model_name
    if args.wire_api == "codex_responses":
        os.environ["REFINER_LLM_WIRE_API"] = "codex_responses"
    else:
        os.environ.pop("REFINER_LLM_WIRE_API", None)
    if args.reasoning_effort:
        os.environ["REFINER_LLM_REASONING_EFFORT"] = args.reasoning_effort
    if args.llm_timeout_seconds is not None:
        os.environ["REFINER_LLM_TIMEOUT_SECONDS"] = str(args.llm_timeout_seconds)
    if args.llm_max_retries is not None:
        os.environ["REFINER_LLM_MAX_RETRIES"] = str(max(1, args.llm_max_retries))


def load_b2_payload(args: argparse.Namespace) -> Dict[str, Any]:
    input_modes = [
        bool(args.observation_file),
        bool(args.observation_json),
        bool(args.observation_text),
        bool(args.payload_json),
    ]
    if sum(input_modes) > 1:
        raise SystemExit(
            "--observation-file、--observation-json、--observation-text 和 "
            "--payload-json 只能使用其中一个。"
        )

    if args.payload_json:
        payload = parse_json_dict(args.payload_json, "payload_json")
    elif args.observation_json:
        payload = {"observation": parse_json_dict(args.observation_json, "observation_json")}
    elif args.observation_text:
        payload = {"observation": {"summary": args.observation_text.strip()}}
    elif args.observation_file:
        payload = load_json_dict(args.observation_file, "observation_file")
    else:
        payload = dict(DEFAULT_FEEDBACK)

    observation_keys = {
        "summary",
        "observation_type",
        "metrics",
        "signals",
        "raw",
        "result",
        "status",
        "notes",
        "extracted_metrics",
        "synthesis_process",
        "PXRD",
    }
    if "observation" not in payload and any(key in payload for key in observation_keys):
        return {"observation": payload}
    return payload


def build_state_json(state: Any) -> str:
    payload = state.debug_snapshot() if hasattr(state, "debug_snapshot") else state.to_dict()
    return json.dumps(payload, ensure_ascii=False, indent=2)


def slugify(text: str, max_len: int = 48) -> str:
    normalized = re.sub(r"\s+", "_", text.strip())
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_\-]+", "", normalized)
    normalized = normalized.strip("_-")
    return (normalized[:max_len] or "b2").strip("_-")


def write_debug_log(state: Any, log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    created_at = getattr(state, "created_at", "") or ""
    timestamp = created_at.replace(":", "-").replace("T", "_").split(".", 1)[0]
    query = getattr(getattr(state, "event", None), "query", "") or "b2"
    output_path = log_dir / f"{timestamp}_{slugify(query)}_deepseek_b2.json"
    output_path.write_text(build_state_json(state), encoding="utf-8")
    return output_path


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def changed_llm_keys(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    changed: List[str] = []
    for key in sorted(B2_LLM_KEYS):
        if key not in after:
            continue
        if key not in before or canonical_json(before.get(key)) != canonical_json(after.get(key)):
            changed.append(key)
    return changed


def print_summary(
    state: Any,
    save_path: Path,
    debug_log_path: Path,
    llm_keys: Iterable[str],
) -> None:
    print(f"status: {state.status}")
    print(f"last_completed_branch: {state.last_completed_branch}")
    print(f"fit_status: {state.observation_stage_fit.get('status')}")
    print(f"fits_current_stage: {state.observation_stage_fit.get('fits_current_stage')}")
    print(f"repair_path: {state.post_observation_repair_path}")
    print(f"current_stage: {state.current_stage}")
    print(f"b2_llm_outputs_changed: {list(llm_keys)}")
    print(f"debug_state_log: {debug_log_path}")
    print(f"saved_state: {save_path}")

    negative_signals = (
        state.observation_stage_fit.get("observation_interpretation", {}).get(
            "negative_signals", []
        )
        if isinstance(state.observation_stage_fit, dict)
        else []
    )
    if negative_signals:
        print(f"negative_signals: {negative_signals}")

    if state.status == "manual_required":
        print("errors:")
        for error in getattr(state, "errors", [])[-5:]:
            print(f"  - {error}")
        print("macro_plan: (not updated; B2 did not complete)")
        return

    print("macro_plan:")
    if not state.macro_plan:
        print("  (empty)")
        return

    for step in state.macro_plan:
        print(f"  {step.get('步骤序号', '?')}. {step.get('操作', '')}")
        print(f"     试剂/对象: {step.get('试剂/对象', '')}")
        print(f"     参数: {step.get('参数', '')}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    configure_deepseek_env(args)
    previous_state = load_json_dict(args.previous_state, "previous_state")
    payload = load_b2_payload(args)
    previous_raw_outputs = dict(previous_state.get("raw_llm_outputs", {}) or {})

    agent = ResearchAgent(
        model=None,
        use_llm=True,
        knowledge_base_dir=args.knowledge_base_dir,
        memory_dir=args.memory_dir,
        max_survey_rounds=args.max_survey_rounds,
        knowledge_top_k=args.knowledge_top_k,
        memory_top_k=args.memory_top_k,
        enable_memory=args.enable_memory,
    )
    if not agent.has_model or not getattr(agent, "_use_llm", False):
        raise SystemExit(
            "DeepSeek 模型没有配置成功；请检查 langchain_openai 是否安装、API key、base_url 和 model-name。"
        )

    state = agent.run(
        event_type="new observation",
        query=args.query.strip(),
        payload=payload,
        previous_state=previous_state,
    )

    state_json = build_state_json(state)
    save_path = Path(args.save_state).expanduser().resolve()
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_text(state_json, encoding="utf-8")
    log_dir = Path(args.log_dir).expanduser().resolve()
    debug_log_path = write_debug_log(state, log_dir)

    new_llm_keys = changed_llm_keys(previous_raw_outputs, state.raw_llm_outputs)
    print_summary(state, save_path, debug_log_path, new_llm_keys)

    if args.print_state_json:
        print("\nfull_state_json:")
        print(state_json)

    if state.status == "manual_required":
        print(
            "\nERROR: B2 进入 manual_required，说明 LLM 没有给出可接受的修复结果；"
            "本脚本不会自动补答案。",
            file=sys.stderr,
        )
        return 4

    if state.status != "completed":
        return 1

    if not new_llm_keys:
        print(
            "\nERROR: 本次 B2 没有检测到新的 LLM raw output，可能没有真正调用后端模型。",
            file=sys.stderr,
        )
        return 2

    macro_plan_llm_keys = {
        "post_observation_macro_plan_design",
        "device_adaptation_macro_plan_design",
    }
    if state.macro_plan and not macro_plan_llm_keys.intersection(new_llm_keys):
        print(
            "\nERROR: B2 的 macro_plan 不是由 B2 macro-plan LLM 步骤生成，"
            "脚本已禁止自动补答案。",
            file=sys.stderr,
        )
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())

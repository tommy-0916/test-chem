#!/usr/bin/env python3
"""Run B1 -> device feasibility -> optional B2 repair with GPT-5.5."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reaserch_agent import ResearchAgent
from reaserch_agent.state import ResearchAgentState
from reaserch_agent.tools import load_device_context
from reaserch_agent.utils.llm_factory import CodexResponsesModel, LLMFactory


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUERY = (
    "针对水系 K 离子电池正极材料容量偏低、难以同时兼顾能量密度与循环寿命的问题。"
    "以亚铁氰化铁为正极，目标是在水系电解液中获得更高的可逆储 K+ 容量和更稳定的长循环表现。"
)
DEFAULT_BASE_URL = "https://a-ocnfniawgw.cn-shanghai.fcapp.run/v1"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_KNOWLEDGE_BASE_DIR = REPO_ROOT / "reaserch_agent" / "chem_kb"
DEFAULT_DEVICE_WORKSTATIONS_DIR = REPO_ROOT / "chem_resources" / "workstations_new"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reaserch_agent" / "e2e_test" / "gpt55"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "从 B1 开始测试 research agent -> device adaptation layer -> "
            "必要时 B2 设备适应修复的完整数据流。"
        )
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help="输入给 B1/research agent 的任务 query。",
    )
    parser.add_argument(
        "--knowledge-base-dir",
        default=str(DEFAULT_KNOWLEDGE_BASE_DIR),
        help=f"research agent 知识库路径。默认: {DEFAULT_KNOWLEDGE_BASE_DIR}",
    )
    parser.add_argument(
        "--device-workstations-dir",
        default=str(DEFAULT_DEVICE_WORKSTATIONS_DIR),
        help=(
            "输入给 B1 的设备工作站描述目录。默认使用当前 chem_resources/workstations_new。"
        ),
    )
    parser.add_argument(
        "--no-b1-device-context",
        action="store_true",
        help="不把设备能力上下文传给 B1；用于对比旧数据流。",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"保存 B1、设备 gate、B2 和 summary 的目录。默认: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--api-key",
        help=(
            "GPT-5.5 API key。建议用环境变量 REFINER_LLM_API_KEY 或 OPENAI_API_KEY，"
            "避免把 key 留在 shell history。"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"OpenAI provider base_url。默认: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--model-name",
        default=DEFAULT_MODEL,
        help=f"模型名。默认: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--wire-api",
        choices=["codex_responses", "chat"],
        default="codex_responses",
        help="gpt-5.5 这个 Codex endpoint 默认使用 codex_responses。",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="xhigh",
        help="codex_responses reasoning effort。默认: xhigh",
    )
    parser.add_argument(
        "--llm-timeout-seconds",
        type=float,
        default=240.0,
        help="每次 LLM 调用的 timeout。默认: 240",
    )
    parser.add_argument(
        "--llm-max-retries",
        type=int,
        default=8,
        help="research agent 内部每个 LLM 步骤最大重试次数。默认: 8",
    )
    parser.add_argument(
        "--max-survey-rounds",
        type=int,
        default=2,
        help="B1/B2 本地知识库检索轮数。默认: 2",
    )
    parser.add_argument(
        "--knowledge-top-k",
        type=int,
        default=5,
        help="每轮保留的知识库命中数量。默认: 5",
    )
    parser.add_argument(
        "--enable-memory",
        action="store_true",
        help="默认不启用 memory；加这个参数才会启用 memory retrieval。",
    )
    parser.add_argument(
        "--memory-dir",
        help="可选 memory 目录。默认不启用 memory。",
    )
    parser.add_argument(
        "--run-full-device-workflow",
        action="store_true",
        help=(
            "默认只跑设备适应层的 pre-feasibility gate；加这个参数才继续生成完整设备 workflow。"
        ),
    )
    parser.add_argument(
        "--print-final-json",
        action="store_true",
        help="在终端打印完整 summary JSON。",
    )
    return parser


def configure_env(args: argparse.Namespace) -> str:
    api_key = (
        args.api_key
        or os.getenv("REFINER_LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
    )
    if not api_key:
        raise SystemExit(
            "缺少 API key。请设置 REFINER_LLM_API_KEY/OPENAI_API_KEY，或传入 --api-key。"
        )

    os.environ["REFINER_LLM_API_KEY"] = api_key
    os.environ["OPENAI_API_KEY"] = api_key
    os.environ["REFINER_LLM_ENDPOINT_URL"] = args.base_url
    os.environ["REFINER_LLM_MODEL_NAME"] = args.model_name
    os.environ["REFINER_LLM_TIMEOUT_SECONDS"] = str(args.llm_timeout_seconds)
    os.environ["REFINER_LLM_MAX_RETRIES"] = str(max(1, args.llm_max_retries))
    os.environ["RESEARCH_ENABLE_MEMORY"] = "1" if args.enable_memory else "0"
    if args.wire_api == "codex_responses":
        os.environ["REFINER_LLM_WIRE_API"] = "codex_responses"
        os.environ["REFINER_LLM_REASONING_EFFORT"] = args.reasoning_effort
    else:
        os.environ.pop("REFINER_LLM_WIRE_API", None)
    return api_key


def build_model(args: argparse.Namespace, api_key: str) -> Any:
    if args.wire_api == "codex_responses":
        return CodexResponsesModel(
            model=args.model_name,
            api_key=api_key,
            base_url=args.base_url,
            reasoning_effort=args.reasoning_effort,
            timeout=args.llm_timeout_seconds,
            codex_path=os.getenv("REFINER_CODEX_CLI_PATH"),
        )
    return LLMFactory.create_or_none(
        model_name=args.model_name,
        api_key=api_key,
        base_url=args.base_url,
    )


def build_b1_constraints(args: argparse.Namespace) -> Dict[str, Any]:
    constraints: Dict[str, Any] = {
        "knowledge_base_dir": str(Path(args.knowledge_base_dir).expanduser().resolve()),
        "memory_enabled": bool(args.enable_memory),
    }
    if not args.no_b1_device_context:
        device_dir = Path(args.device_workstations_dir).expanduser().resolve()
        constraints["device_workstations_dir"] = str(device_dir)
        constraints["device_context"] = load_device_context(device_dir)
    return constraints


def import_device_main_workflow() -> Any:
    device_dir = str(REPO_ROOT / "device_agent")
    if device_dir not in sys.path:
        sys.path.insert(0, device_dir)
    module = importlib.import_module("workflow")
    return module.MainWorkflow


def state_json(state: Any) -> str:
    if hasattr(state, "debug_snapshot"):
        payload = state.debug_snapshot()
    elif hasattr(state, "to_dict"):
        payload = state.to_dict()
    else:
        payload = state
    return json.dumps(payload, ensure_ascii=False, indent=2)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def slugify(text: str, max_len: int = 48) -> str:
    normalized = re.sub(r"\s+", "_", text.strip())
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_\-]+", "", normalized)
    normalized = normalized.strip("_-")
    return (normalized[:max_len] or "query").strip("_-")


def output_dir_for_run(base_dir: str, query: str) -> Path:
    base = Path(base_dir).expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return base / f"{timestamp}_{slugify(query, 28)}"


def macro_plan_for_device_layer(b1_state: ResearchAgentState, query: str) -> str:
    payload = {
        "query": query,
        "stage_route": b1_state.stage_route,
        "current_stage": b1_state.current_stage,
        "current_stage_plan": b1_state.current_stage_plan,
        "macro_plan": b1_state.macro_plan,
        "knowledge_hits": [
            {
                "title": hit.title,
                "file_path": hit.file_path,
                "score": hit.score,
                "synthesis_summary": hit.synthesis_summary,
                "experiment_details": hit.experiment_details,
            }
            for hit in b1_state.knowledge_hits[:5]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def run_device_gate(
    model: Any,
    macro_plan_text: str,
    run_full_device_workflow: bool,
) -> Any:
    MainWorkflow = import_device_main_workflow()
    workflow = MainWorkflow(
        model=model,
        use_temp_data_flow=True,
        max_verify_retries=0,
        use_new_format=True,
    )
    if run_full_device_workflow:
        return workflow.run_state(macro_plan_text)

    state = workflow._step1_create_log_and_get_inputs(macro_plan_text)
    state = workflow._step_pre_feasibility_gate(state)
    if state.status == "feasibility_error":
        state.update_stage("completed")
    else:
        state.terminal_package = build_device_supported_package(workflow, state)
        state.status = "completed"
        state.update_stage("completed")
    return state


def build_device_supported_package(workflow: Any, state: Any) -> Dict[str, Any]:
    capabilities = workflow._device_capabilities_for_error_package(state)
    return {
        "feedback_type": "device_feasibility_ok",
        "status": "supported",
        "exp_id": state.exp_id,
        "iteration_id": state.iteration_id,
        "workflow_id": state.workflow_id,
        "macro_plan": state.macro_plan,
        "device_capabilities": capabilities,
        "feasibility_assessment": state.pre_feasibility_report,
        "message": "设备适应层判定当前 macro_plan 可以由当前设备描述支持。",
    }


def build_b2_payload(
    device_state: Any,
    b1_state: ResearchAgentState,
    query: str,
    device_input_text: str,
) -> Dict[str, Any]:
    package = dict(device_state.terminal_package or {})
    package.setdefault("feedback_type", "device_feasibility_error")
    package["query"] = query
    package["request"] = (
        "设备适应层判定上一版 macro action 无法由当前设备执行。"
        "请只做设备适应性修改：保留原始科学目标、目标材料、当前 stage、XRD observation point，"
        "结合当前设备可用容器/工作站和知识库论文依据，重新输出设备可执行的 macro_plan。"
    )
    package["previous_research_context"] = {
        "stage_route": b1_state.stage_route,
        "current_stage": b1_state.current_stage,
        "current_stage_plan": b1_state.current_stage_plan,
        "stage_route_reason": b1_state.stage_route_reason,
        "current_stage_reason": b1_state.current_stage_reason,
        "macro_plan": b1_state.macro_plan,
        "knowledge_hits": [
            {
                "title": hit.title,
                "file_path": hit.file_path,
                "score": hit.score,
                "synthesis_summary": hit.synthesis_summary,
                "experiment_details": hit.experiment_details,
            }
            for hit in b1_state.knowledge_hits[:5]
        ],
    }
    package["device_layer_input"] = device_input_text
    return package


def macro_step_titles(macro_plan: List[Dict[str, Any]]) -> List[str]:
    titles = []
    for step in macro_plan or []:
        index = step.get("步骤序号", "?")
        operation = step.get("操作", "")
        titles.append(f"{index}. {operation}")
    return titles


def main() -> int:
    args = build_parser().parse_args()
    api_key = configure_env(args)
    output_dir = output_dir_for_run(args.output_dir, args.query)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] 输出目录: {output_dir}", flush=True)
    print("[2/4] 运行 B1：检索知识库并生成 stage/macro action ...", flush=True)

    model = build_model(args, api_key)
    if model is None:
        raise SystemExit("模型没有创建成功，请检查 base_url/model/api_key。")

    research_agent = ResearchAgent(
        model=model,
        use_llm=True,
        knowledge_base_dir=args.knowledge_base_dir,
        memory_dir=args.memory_dir,
        max_survey_rounds=args.max_survey_rounds,
        knowledge_top_k=args.knowledge_top_k,
        enable_memory=args.enable_memory,
    )
    b1_state = research_agent.run(
        event_type="bootstrap",
        query=args.query.strip(),
        constraints=build_b1_constraints(args),
        payload={
            "requested_outputs": ["stage_route", "current_stage", "macro_plan"],
        },
    )
    b1_path = output_dir / "01_b1_research_state.json"
    save_text(b1_path, state_json(b1_state))
    if b1_state.status != "completed":
        summary = {
            "status": "failed_at_b1",
            "b1_state_path": str(b1_path),
            "errors": b1_state.errors,
        }
        save_json(output_dir / "summary.json", summary)
        print(f"B1 未完成，状态已保存: {b1_path}", flush=True)
        return 1

    print(
        f"B1 完成：current_stage={b1_state.current_stage}，macro steps={len(b1_state.macro_plan)}",
        flush=True,
    )
    print("[3/4] 运行设备适应层 pre-feasibility gate ...", flush=True)

    device_input_text = macro_plan_for_device_layer(b1_state, args.query)
    save_text(output_dir / "02_device_layer_input.json", device_input_text)
    device_state = run_device_gate(
        model=model,
        macro_plan_text=device_input_text,
        run_full_device_workflow=args.run_full_device_workflow,
    )
    device_state_path = output_dir / "03_device_gate_state.json"
    save_text(device_state_path, state_json(device_state))
    device_package = device_state.terminal_package or {}
    device_package_path = output_dir / "04_device_feedback_package.json"
    save_json(device_package_path, device_package)

    is_supported = device_package.get("status") == "supported" or bool(
        (device_state.pre_feasibility_report or {}).get("is_feasible")
    )
    print(
        "设备 gate 结果: "
        + ("supported，可以进入下游设备动作生成。" if is_supported else "unsupported，需要回流 B2。"),
        flush=True,
    )

    final_state = b1_state
    b2_path: Optional[Path] = None
    if not is_supported:
        print("[4/4] 设备不满足，调用 research agent B2 做设备适应性修复 ...", flush=True)
        b2_payload = build_b2_payload(
            device_state=device_state,
            b1_state=b1_state,
            query=args.query,
            device_input_text=device_input_text,
        )
        save_json(output_dir / "05_b2_input_payload.json", b2_payload)
        final_state = research_agent.run(
            event_type="new observation",
            query=args.query.strip(),
            payload=b2_payload,
            previous_state=b1_state,
        )
        b2_path = output_dir / "06_b2_research_state.json"
        save_text(b2_path, state_json(final_state))
        print(
            f"B2 完成：repair_path={final_state.post_observation_repair_path}，"
            f"macro steps={len(final_state.macro_plan)}",
            flush=True,
        )
    else:
        print("[4/4] 设备已满足，本轮不触发 B2。", flush=True)

    summary = {
        "status": "completed" if final_state.status == "completed" else final_state.status,
        "query": args.query,
        "model": args.model_name,
        "base_url": args.base_url,
        "wire_api": args.wire_api,
        "knowledge_base_dir": str(Path(args.knowledge_base_dir).expanduser().resolve()),
        "device_workstations_dir": str(Path(args.device_workstations_dir).expanduser().resolve()),
        "b1_device_context_enabled": not args.no_b1_device_context,
        "memory_enabled": bool(args.enable_memory),
        "b1_state_path": str(b1_path),
        "device_layer_input_path": str(output_dir / "02_device_layer_input.json"),
        "device_gate_state_path": str(device_state_path),
        "device_feedback_package_path": str(device_package_path),
        "b2_input_payload_path": str(output_dir / "05_b2_input_payload.json")
        if b2_path
        else None,
        "b2_state_path": str(b2_path) if b2_path else None,
        "device_supported": bool(is_supported),
        "device_status": device_package.get("status"),
        "device_blocking_constraints": (
            device_package.get("error_package", {}).get("blocking_constraints")
            if isinstance(device_package.get("error_package"), dict)
            else []
        ),
        "b1_current_stage": b1_state.current_stage,
        "b1_macro_step_titles": macro_step_titles(b1_state.macro_plan),
        "final_source": "B1_device_supported" if is_supported else "B2_device_adaptation",
        "final_current_stage": final_state.current_stage,
        "final_macro_step_titles": macro_step_titles(final_state.macro_plan),
        "final_macro_plan": final_state.macro_plan,
    }
    summary_path = output_dir / "summary.json"
    save_json(summary_path, summary)

    print(f"summary: {summary_path}", flush=True)
    if args.print_final_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    return 0 if final_state.status == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())

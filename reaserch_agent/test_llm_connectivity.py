#!/usr/bin/env python3
"""Smoke-test .env LLM credentials and research-agent integration.

This script intentionally uses only the Python standard library for the HTTP
ping path, so it can validate credentials before optional SDK dependencies are
installed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reaserch_agent import ResearchAgent


DEFAULT_QUERY = (
    "我希望基于现有自动化化学工作站，设计一个 NiFe 普鲁士蓝类似物（NiFe-PBA）"
    "电化学活化性能优化实验。目标不是做完整电池，而是合成 NiFe-PBA 前驱体，"
    "并通过电化学工作站测试其活化后的电化学响应。"
)


@dataclass
class LLMConfig:
    model: str
    api_key: str
    base_url: str
    wire_api: str
    reasoning_effort: str
    timeout_seconds: float


@dataclass
class EndpointResult:
    kind: str
    base_url: str
    endpoint_url: str
    response_text: str


class HTTPRequestError(RuntimeError):
    def __init__(self, url: str, status: int, body: str) -> None:
        self.url = url
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status} from {url}: {body[:800]}")


class DirectHTTPModel:
    """Minimal `.invoke(messages)` model compatible with ResearchAgent."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        kind: str,
        base_url: str,
        max_output_tokens: int,
    ) -> None:
        self.config = config
        self.kind = kind
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens

    def invoke(self, messages: Sequence[Any]) -> SimpleNamespace:
        if self.kind == "responses":
            payload = {
                "model": self.config.model,
                "input": self._messages_to_prompt(messages),
                "max_output_tokens": self.max_output_tokens,
                "store": False,
            }
            if self.config.reasoning_effort:
                payload["reasoning"] = {"effort": self.config.reasoning_effort}
            response = post_json(
                endpoint_for(self.base_url, "responses"),
                self.config.api_key,
                payload,
                timeout=self.config.timeout_seconds,
            )
            return SimpleNamespace(content=extract_response_text(response, "responses"))

        payload = {
            "model": self.config.model,
            "messages": [normalize_message(message) for message in messages],
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
        }
        response = post_json(
            endpoint_for(self.base_url, "chat"),
            self.config.api_key,
            payload,
            timeout=self.config.timeout_seconds,
        )
        return SimpleNamespace(content=extract_response_text(response, "chat"))

    def _messages_to_prompt(self, messages: Sequence[Any]) -> str:
        parts: List[str] = []
        for message in messages:
            normalized = normalize_message(message)
            parts.append(f"[{normalized['role']}]\n{normalized['content']}")
        return "\n\n".join(parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test .env LLM API access and ResearchAgent smoke integration."
    )
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).resolve().parents[1] / ".env"),
        help="Path to .env. Default: repo root .env.",
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help="ResearchAgent bootstrap query used for chemagent smoke.",
    )
    parser.add_argument(
        "--knowledge-base-dir",
        default=str(Path(__file__).resolve().parent / "chem_kb"),
        help="Knowledge-base directory used by ResearchAgent.",
    )
    parser.add_argument(
        "--include-device-context",
        action="store_true",
        help="Load default workstation context into the research smoke.",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Only test raw API connectivity; skip ResearchAgent smoke.",
    )
    parser.add_argument(
        "--max-survey-rounds",
        type=int,
        default=1,
        help="ResearchAgent survey rounds for smoke. Default: 1.",
    )
    parser.add_argument(
        "--knowledge-top-k",
        type=int,
        default=2,
        help="Knowledge hits retained per round. Default: 2.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=4096,
        help="Max output tokens for each direct HTTP LLM call. Default: 4096.",
    )
    parser.add_argument(
        "--save-state",
        help="Optional path to save the ResearchAgent smoke state JSON.",
    )
    return parser


def load_env(path: str | Path) -> None:
    env_path = Path(path).expanduser().resolve()
    if not env_path.exists():
        raise SystemExit(f".env not found: {env_path}")

    values: Dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        value = re.sub(
            r"\$\{([^}]+)\}",
            lambda match: values.get(match.group(1), os.getenv(match.group(1), "")),
            value,
        )
        values[key] = value
        os.environ[key] = value


def get_config() -> LLMConfig:
    model = first_env("REFINER_LLM_MODEL_NAME", "OPENAI_MODEL", "GEMINI_MODEL")
    api_key = first_env("REFINER_LLM_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")
    base_url = first_env("REFINER_LLM_ENDPOINT_URL", "OPENAI_BASE_URL", "GEMINI_BASE_URL")
    if not model:
        raise SystemExit("Missing model. Fill REFINER_LLM_MODEL_NAME in .env.")
    if not api_key:
        raise SystemExit("Missing API key. Fill REFINER_LLM_API_KEY in .env.")
    if not base_url:
        raise SystemExit("Missing base URL. Fill REFINER_LLM_ENDPOINT_URL in .env.")
    return LLMConfig(
        model=model,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        wire_api=os.getenv("REFINER_LLM_WIRE_API", "chat").strip().lower() or "chat",
        reasoning_effort=os.getenv("REFINER_LLM_REASONING_EFFORT", "").strip(),
        timeout_seconds=parse_float(os.getenv("REFINER_LLM_TIMEOUT_SECONDS"), 120.0),
    )


def first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def parse_float(value: Optional[str], default: float) -> float:
    try:
        return max(1.0, float(value or ""))
    except ValueError:
        return default


def base_url_candidates(base_url: str) -> List[str]:
    base = base_url.rstrip("/")
    candidates = [base]
    if not base.endswith("/v1"):
        candidates.append(base + "/v1")
    return dedupe(candidates)


def endpoint_for(base_url: str, kind: str) -> str:
    base = base_url.rstrip("/")
    if kind == "responses":
        return base + "/responses"
    if kind == "chat":
        return base + "/chat/completions"
    raise ValueError(f"unsupported endpoint kind: {kind}")


def preferred_endpoint_kinds(wire_api: str) -> List[str]:
    if wire_api == "codex_responses":
        return ["responses", "chat"]
    return ["chat", "responses"]


def post_json(
    url: str,
    api_key: str,
    payload: Dict[str, Any],
    *,
    timeout: float,
) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": os.getenv("REFINER_LLM_USER_AGENT", "chemagent-test/0.1"),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise HTTPRequestError(url, exc.code, raw) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"URL error from {url}: {exc}") from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Expected JSON object from {url}, got {type(parsed).__name__}")
    return parsed


def ping_payload(config: LLMConfig, kind: str) -> Dict[str, Any]:
    prompt = "Return exactly CHEMAGENT_API_OK and nothing else."
    if kind == "responses":
        payload: Dict[str, Any] = {
            "model": config.model,
            "input": prompt,
            "max_output_tokens": 32,
            "store": False,
        }
        if config.reasoning_effort:
            payload["reasoning"] = {"effort": config.reasoning_effort}
        return payload
    return {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 32,
    }


def extract_response_text(payload: Dict[str, Any], kind: str) -> str:
    if kind == "chat":
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict) and message.get("content"):
                return str(message["content"]).strip()

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = payload.get("output")
    if isinstance(output, list):
        parts: List[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("content")
                    if text:
                        parts.append(str(text))
        if parts:
            return "\n".join(parts).strip()

    return json.dumps(payload, ensure_ascii=False)[:1000]


def test_api(config: LLMConfig) -> EndpointResult:
    print("effective LLM config:")
    print(f"- model: {config.model}")
    print(f"- base_url: {config.base_url}")
    print(f"- api_key: set(len={len(config.api_key)})")
    print(f"- wire_api: {config.wire_api}")
    print(f"- timeout_seconds: {config.timeout_seconds:g}")

    failures: List[str] = []
    for kind in preferred_endpoint_kinds(config.wire_api):
        for base_url in base_url_candidates(config.base_url):
            url = endpoint_for(base_url, kind)
            try:
                response = post_json(
                    url,
                    config.api_key,
                    ping_payload(config, kind),
                    timeout=config.timeout_seconds,
                )
                text = extract_response_text(response, kind)
                print(f"API ping passed: kind={kind}, url={url}, response={text[:120]!r}")
                return EndpointResult(
                    kind=kind,
                    base_url=base_url,
                    endpoint_url=url,
                    response_text=text,
                )
            except Exception as exc:
                failures.append(f"{kind} {url}: {type(exc).__name__}: {exc}")

    print("API ping failed for all endpoint candidates:")
    for item in failures:
        print(f"- {item[:1200]}")
    raise SystemExit(2)


def run_research_smoke(
    config: LLMConfig,
    endpoint: EndpointResult,
    args: argparse.Namespace,
) -> Any:
    constraints: Dict[str, Any] = {}
    if args.include_device_context:
        constraints["include_default_device_context"] = True

    model = DirectHTTPModel(
        config,
        kind=endpoint.kind,
        base_url=endpoint.base_url,
        max_output_tokens=args.max_output_tokens,
    )
    agent = ResearchAgent(
        model=model,
        use_llm=True,
        knowledge_base_dir=args.knowledge_base_dir,
        max_survey_rounds=args.max_survey_rounds,
        knowledge_top_k=args.knowledge_top_k,
        enable_memory=False,
    )
    state = agent.run(
        event_type="bootstrap",
        query=args.query,
        constraints=constraints,
    )
    print("ResearchAgent smoke result:")
    print(f"- status: {state.status}")
    print(f"- current_stage: {state.current_stage}")
    print(f"- macro_steps: {len(state.macro_plan)}")
    if state.knowledge_hits:
        print(f"- top_knowledge_hit: {state.knowledge_hits[0].title}")
    if state.errors:
        print("- errors:")
        for item in state.errors[-5:]:
            print(f"  - {item}")
    return state


def normalize_message(message: Any) -> Dict[str, str]:
    if isinstance(message, dict):
        role = str(message.get("role") or message.get("type") or "user")
        content = str(message.get("content") or "")
        return {"role": role, "content": content}
    role = str(getattr(message, "role", None) or getattr(message, "type", None) or "user")
    content = str(getattr(message, "content", "") or "")
    return {"role": role, "content": content}


def state_to_jsonable(state: Any) -> Dict[str, Any]:
    if hasattr(state, "debug_snapshot"):
        return state.debug_snapshot()
    if hasattr(state, "to_dict"):
        return state.to_dict()
    return state


def dedupe(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def main() -> int:
    args = build_parser().parse_args()
    load_env(args.env_file)
    os.environ.setdefault("REFINER_LLM_MAX_RETRIES", "1")
    config = get_config()
    endpoint = test_api(config)
    if args.api_only:
        return 0

    state = run_research_smoke(config, endpoint, args)
    if args.save_state:
        output_path = Path(args.save_state).expanduser().resolve()
        output_path.write_text(
            json.dumps(state_to_jsonable(state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"state saved: {output_path}")
    return 0 if state.status == "completed" else 3


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Preflight a repository and optionally probe its real LLM provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from extract_cases import EXPECTED, extract_cases


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def first_value(env: dict[str, str], *names: str) -> tuple[str, str]:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value, name
    return "", ""


def first_key(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("["):
        parsed = json.loads(value)
        return str(parsed[0]).strip() if isinstance(parsed, list) and parsed else ""
    return value.replace(";", ",").split(",", 1)[0].strip()


def workstation_root(repo: Path) -> Optional[Path]:
    for candidate in (
        repo / "chem_resources/lab-design-all/skills/chemistry-experiment-workstation",
        repo / "chem_resources/lab-design-main/skills/chemistry-experiment-workstation",
    ):
        if (candidate / "SKILL.md").is_file():
            return candidate.resolve()
    return None


def public_help(python: Path, repo: Path) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            [str(python), str(repo / "run_campaign.py"), "--help"],
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout or ""


def api_endpoint(base_url: str, wire_api: str) -> str:
    base = base_url.rstrip("/")
    suffix = "/responses" if wire_api == "codex_responses" else "/chat/completions"
    return base if base.endswith(suffix) else base + suffix


def extract_response_text(payload: dict[str, Any], wire_api: str) -> str:
    if wire_api == "chat":
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first, dict) else None
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"].strip()
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    parts: list[str] = []
    for item in payload.get("output", []) if isinstance(payload.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    return "\n".join(parts)


def probe_api(
    *,
    endpoint: str,
    api_key: str,
    model: str,
    wire_api: str,
    reasoning_effort: str,
    timeout: int,
) -> dict[str, Any]:
    if wire_api == "codex_responses":
        payload: dict[str, Any] = {
            "model": model,
            "instructions": (
                "You are validating the configured model endpoint for a chemistry "
                "automation evaluation. Return only the requested JSON object; do "
                "not call tools and do not add prose."
            ),
            "input": (
                "A Chem Agent evaluation will inspect Research-to-Device workflow "
                "translation against a fixed workstation truth source while real "
                "device dispatch remains disabled. Confirm that this endpoint can "
                "perform the independent structured review by returning exactly "
                '{"status":"chem-agent-eval-ready","wire_api":"responses"}.'
            ),
            "max_output_tokens": 256,
            "store": False,
        }
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
    else:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are validating the configured model endpoint for a "
                        "chemistry automation evaluation. Return only the requested "
                        "JSON object."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Confirm readiness for structured Research-to-Device workflow "
                        "review with real device dispatch disabled. Return exactly "
                        '{"status":"chem-agent-eval-ready","wire_api":"chat"}.'
                    ),
                },
            ],
            "max_tokens": 256,
        }
    request = urllib.request.Request(
        api_endpoint(endpoint, wire_api),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "chem-agent-eval-preflight/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("Provider response is not a JSON object")
    text = extract_response_text(parsed, wire_api)
    if not text:
        raise RuntimeError("Provider returned no text output")
    return {
        "response_nonempty": True,
        "response_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight the Chem Agent eight-case evaluation.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--docx", type=Path, help="Default: <repo>/测试题目.docx")
    parser.add_argument("--api-key-env", help="Preferred API-key environment variable.")
    parser.add_argument("--python", type=Path)
    parser.add_argument("--probe-api", action="store_true")
    parser.add_argument("--probe-timeout", type=int, default=180)
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    docx = (args.docx or repo / "测试题目.docx").expanduser().resolve()
    python = args.python.expanduser().resolve() if args.python else repo / ".venv/bin/python"
    if not python.exists():
        python = Path(sys.executable).resolve()
    env = load_dotenv(repo / ".env")
    env.update(os.environ)
    checks: list[dict[str, Any]] = []

    def check(ok: bool, code: str, message: str, evidence: Any = None) -> None:
        item = {"ok": bool(ok), "code": code, "message": message}
        if evidence not in (None, "", [], {}):
            item["evidence"] = evidence
        checks.append(item)
        print(f"{'PASS' if ok else 'FAIL'}  {message}")

    check(repo.is_dir(), "repo", f"Chem Agent repository exists: {repo}")
    check(
        sys.version_info >= (3, 10),
        "python_version",
        f"Python 3.10+ is available (running {sys.version.split()[0]})",
    )
    required_paths = [repo / "run_campaign.py", repo / "reaserch_agent", repo / "device_agent", repo / "chem_resources"]
    missing = [str(path) for path in required_paths if not path.exists()]
    check(not missing, "repo_layout", "Chem Agent repository layout is compatible", missing)
    check(python.is_file(), "python", f"Python executable exists: {python}")

    try:
        cases = extract_cases(docx)
        check(
            [case["case_id"] for case in cases] == EXPECTED,
            "docx_cases",
            "DOCX contains exactly A01-D02 with unique Queries",
        )
    except Exception as exc:
        check(False, "docx_cases", f"DOCX extraction failed: {type(exc).__name__}: {exc}")

    workstations = workstation_root(repo)
    station_count = 0
    if workstations:
        station_count = sum(
            1
            for path in workstations.rglob("SKILL.md")
            if path.parent != workstations
        )
    check(
        workstations is not None and station_count > 0,
        "workstations",
        f"Workstation truth source is available ({station_count} workstation Skills)",
        str(workstations) if workstations else "",
    )

    return_code, help_text = public_help(python, repo)
    required_flags = {
        "--query",
        "--campaign-id",
        "--campaigns-root",
        "--online-literature",
        "--model-name",
        "--base-url",
        "--wire-api",
        "--reasoning-effort",
    }
    missing_flags = sorted(flag for flag in required_flags if flag not in help_text)
    check(
        return_code == 0 and not missing_flags,
        "public_entrypoint",
        "Public run_campaign.py exposes the required black-box and provider options",
        missing_flags,
    )

    model, model_env = first_value(env, "REFINER_LLM_MODEL_NAME", "OPENAI_MODEL")
    endpoint, endpoint_env = first_value(env, "REFINER_LLM_ENDPOINT_URL", "OPENAI_BASE_URL")
    wire_api, wire_env = first_value(env, "REFINER_LLM_WIRE_API")
    effort, effort_env = first_value(env, "REFINER_LLM_REASONING_EFFORT")
    key_names = tuple(
        name
        for name in (
            args.api_key_env,
            "REFINER_LLM_API_KEY",
            "OPENAI_API_KEY",
            "CHEM_AGENT_EVAL_API_KEYS",
        )
        if name
    )
    api_key, key_env = first_value(env, *key_names)
    api_key = first_key(api_key)
    check(bool(model), "model", f"Campaign model is configured through {model_env or 'environment'}")
    check(bool(endpoint), "endpoint", f"Campaign endpoint is configured through {endpoint_env or 'environment'}")
    check(wire_api in {"chat", "codex_responses"}, "wire_api", f"Wire API is configured through {wire_env or 'environment'}")
    check(bool(effort) or wire_api == "chat", "reasoning", f"Reasoning effort is configured through {effort_env or 'environment'}")
    check(bool(api_key), "api_key", f"API key is available through {key_env or 'environment'}; value was not printed")

    if args.probe_api and model and endpoint and wire_api and api_key:
        try:
            probe = probe_api(
                endpoint=endpoint,
                api_key=api_key,
                model=model,
                wire_api=wire_api,
                reasoning_effort=effort,
                timeout=args.probe_timeout,
            )
            check(True, "api_probe", "Provider returned a non-empty real LLM response", probe)
        except Exception as exc:
            check(False, "api_probe", f"Provider probe failed: {type(exc).__name__}: {exc}")
    elif args.probe_api:
        check(False, "api_probe", "Provider probe could not run because configuration is incomplete")
    else:
        print("SKIP  Real provider probe was not requested; formal runs require --probe-api")

    ready = all(item["ok"] for item in checks) and args.probe_api
    result = {"ready": ready, "api_probe_required_for_formal_run": True, "checks": checks}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())

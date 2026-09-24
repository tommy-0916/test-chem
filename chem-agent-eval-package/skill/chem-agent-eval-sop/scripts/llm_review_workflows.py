#!/usr/bin/env python3
"""Independent LLM review for workstation workflow schemas.

The deterministic auditor remains authoritative for mechanically provable
violations.  This second layer asks an independent LLM call to inspect the raw
workflow, the root workstation rules, the exact used workstation Skills, and
their audit rules.  An LLM pass can add failures but can never erase a
deterministic failure.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from audit_workflows import (
        StationSchema,
        _infer_repo,
        _infer_workstations,
        _load_json,
        load_workstation_catalog,
    )
except ModuleNotFoundError:  # imported as scripts.llm_review_workflows in tests
    from scripts.audit_workflows import (
        StationSchema,
        _infer_repo,
        _infer_workstations,
        _load_json,
        load_workstation_catalog,
    )


VALID_VERDICTS = {"yes", "no", "not_evaluable"}
JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)

SYSTEM_PROMPT = """You are an independent, safety-critical reviewer of chemistry workstation workflow schemas.

Review the RAW workflow_json exactly as emitted. Do not repair, normalize, coerce, or excuse it because a later formatter might fix it. Ignore the Device Agent's self-check, success status, and repository validator. Use only the supplied root rules, workstation Skills, audit rules, workflow, container/reagent plans, and dispatch-formatting evidence.

Check every step for:
- workstation identity and id;
- whether the operation belongs to that workstation;
- exact operation-level fields and required fields;
- nested structure defined by -, --, and deeper Skill rows;
- JSON types, enums, units, open/closed numeric ranges, and dynamic N fields;
- container, carrier, reagent-plan, file-input, lid-state, and source-step consistency;
- cross-step input/output state transitions and explicit Skill/audit constraints;
- formatter-unmapped steps, dropped parameters, or formatting failure.

Do not provide chain-of-thought. Return JSON only. Group repeated identical errors by listing all affected step_numbers. Every error must cite the supplied Skill/audit path and a short evidence quote when available. If any error exists, verdict must be no. Use not_evaluable only when no workflow exists or supplied evidence is insufficient to inspect it.
"""


def parse_key_list(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []
    if value.startswith("["):
        parsed = json.loads(value)
        return [str(item).strip() for item in parsed if str(item).strip()]
    normalized = value.replace(";", ",").replace("\n", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def discover_keys(env: dict[str, str], preferred_env: str | None = None) -> list[str]:
    if preferred_env:
        preferred = parse_key_list(env.get(preferred_env, ""))
        if preferred:
            return preferred
    direct = parse_key_list(env.get("CHEM_AGENT_EVAL_API_KEYS", ""))
    if direct:
        return direct
    pooled = [
        env[f"REFINER_LLM_POOL_{index}_API_KEY"].strip()
        for index in range(1, 100)
        if env.get(f"REFINER_LLM_POOL_{index}_API_KEY", "").strip()
    ]
    if pooled:
        return pooled
    fallback = env.get("REFINER_LLM_API_KEY", "").strip()
    return [fallback] if fallback else []


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _endpoint(base_url: str, wire_api: str) -> str:
    base = base_url.rstrip("/")
    suffix = "/responses" if wire_api == "codex_responses" else "/chat/completions"
    if base.endswith(suffix):
        return base
    return base + suffix


def _extract_response_text(payload: dict[str, Any], wire_api: str) -> str:
    if wire_api == "chat":
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first, dict) else None
            if isinstance(message, dict) and message.get("content"):
                return str(message["content"]).strip()
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    output = payload.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
        if parts:
            return "\n".join(parts).strip()
    raise ValueError("LLM response did not contain text output")


def _parse_json_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence = JSON_FENCE_RE.match(stripped)
    if fence:
        stripped = fence.group(1).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM review must be a JSON object")
    return parsed


def call_llm(
    *,
    prompt: str,
    api_key: str,
    endpoint: str,
    model: str,
    wire_api: str,
    reasoning_effort: str,
    timeout: int,
    max_output_tokens: int,
) -> str:
    if wire_api == "codex_responses":
        payload: dict[str, Any] = {
            "model": model,
            "instructions": SYSTEM_PROMPT,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
    else:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_output_tokens,
        }
    request = urllib.request.Request(
        _endpoint(endpoint, wire_api),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "chem-agent-eval-schema-review/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM URL error: {exc}") from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("LLM API response must be a JSON object")
    return _extract_response_text(parsed, wire_api)


def _audit_rule_path(workstations: Path, station_code: str) -> Path | None:
    audit_dir = workstations / "references_audit"
    exact = audit_dir / f"{station_code}_audit.md"
    if exact.exists():
        return exact
    if audit_dir.is_dir():
        matches = sorted(audit_dir.glob(f"{station_code}_*.md"))
        if matches:
            return matches[0]
    return None


def _resolve_used_stations(
    workflow: dict[str, Any],
    stations: dict[str, StationSchema],
    aliases: dict[str, str],
) -> list[StationSchema]:
    used: dict[str, StationSchema] = {}
    for step in workflow.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        name = str(step.get("workstation", "")).strip()
        code = aliases.get(name) or aliases.get(name.replace(" ", ""))
        if code and code in stations:
            used[code] = stations[code]
    return [used[key] for key in sorted(used)]


def build_review_prompt(
    *,
    case_id: str,
    package: dict[str, Any],
    workstations: Path,
    stations: dict[str, StationSchema],
    aliases: dict[str, str],
) -> str:
    workflow = package.get("workflow_json")
    if not isinstance(workflow, dict):
        workflow = {}
    used = _resolve_used_stations(workflow, stations, aliases)
    root_skill = workstations / "SKILL.md"
    sections: list[str] = [
        f"CASE_ID: {case_id}",
        "REQUIRED OUTPUT SCHEMA:",
        json.dumps(
            {
                "case_id": case_id,
                "review_status": "completed",
                "verdict": "yes|no|not_evaluable",
                "checked_step_count": "integer",
                "findings": [
                    {
                        "severity": "error|warning",
                        "code": "short_snake_case",
                        "step_numbers": [1],
                        "workstation": "...",
                        "operation": "...",
                        "parameter_path": "...",
                        "actual": "...",
                        "expected": "...",
                        "evidence_path": "absolute supplied path",
                        "evidence_quote": "short exact quote",
                        "explanation": "concise explanation",
                    }
                ],
                "limitations": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "RAW DEVICE EVIDENCE:",
        json.dumps(
            {
                "workflow_json": workflow,
                "container_plan": package.get("container_plan"),
                "reagent_slot_plan": package.get("reagent_slot_plan"),
                "dispatch_formatting": package.get("dispatch_formatting"),
            },
            ensure_ascii=False,
            indent=2,
        ),
    ]
    if root_skill.exists():
        sections.extend(
            [
                f"ROOT WORKSTATION RULES — PATH: {root_skill.resolve()}",
                root_skill.read_text(encoding="utf-8"),
            ]
        )
    for station in used:
        sections.extend(
            [
                f"WORKSTATION SKILL — PATH: {station.skill_path}",
                station.skill_path.read_text(encoding="utf-8"),
            ]
        )
        audit_path = _audit_rule_path(workstations, station.code)
        if audit_path:
            sections.extend(
                [
                    f"WORKSTATION AUDIT RULES — PATH: {audit_path.resolve()}",
                    audit_path.read_text(encoding="utf-8"),
                ]
            )
    sections.append("Return JSON only, following the required schema.")
    return "\n\n".join(sections)


def validate_review(
    review: dict[str, Any], *, case_id: str, expected_steps: int
) -> dict[str, Any]:
    if review.get("case_id") != case_id:
        raise ValueError(f"LLM review case_id mismatch: {review.get('case_id')!r}")
    if review.get("review_status") != "completed":
        raise ValueError("LLM review_status must be completed")
    verdict = review.get("verdict")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"Invalid LLM verdict: {verdict!r}")
    checked = review.get("checked_step_count")
    if checked != expected_steps:
        raise ValueError(
            f"LLM checked_step_count={checked!r}, expected {expected_steps}"
        )
    findings = review.get("findings")
    if not isinstance(findings, list):
        raise ValueError("LLM findings must be a list")
    error_count = 0
    normalized: list[dict[str, Any]] = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ValueError(f"LLM finding {index} must be an object")
        severity = finding.get("severity")
        if severity not in {"error", "warning"}:
            raise ValueError(f"LLM finding {index} has invalid severity")
        if severity == "error":
            error_count += 1
        step_numbers = finding.get("step_numbers", [])
        if not isinstance(step_numbers, list):
            raise ValueError(f"LLM finding {index} step_numbers must be a list")
        for step in step_numbers:
            if not isinstance(step, int) or not 1 <= step <= expected_steps:
                raise ValueError(f"LLM finding {index} has invalid step number {step!r}")
        normalized.append(finding)
    if verdict == "yes" and error_count:
        raise ValueError("LLM verdict yes cannot contain error findings")
    if verdict == "no" and not error_count:
        raise ValueError("LLM verdict no must contain at least one error finding")
    if verdict == "not_evaluable" and expected_steps > 0:
        raise ValueError("LLM cannot use not_evaluable for a supplied workflow")
    review["findings"] = normalized
    review["error_count"] = error_count
    review["warning_count"] = len(normalized) - error_count
    return review


LLMCall = Callable[..., str]


def review_case(
    *,
    case_id: str,
    package: dict[str, Any],
    deterministic_case: dict[str, Any],
    workstations: Path,
    stations: dict[str, StationSchema],
    aliases: dict[str, str],
    keys: list[str],
    key_offset: int,
    endpoint: str,
    model: str,
    wire_api: str,
    reasoning_effort: str,
    timeout: int,
    max_output_tokens: int,
    attempts: int,
    call_fn: LLMCall = call_llm,
) -> dict[str, Any]:
    workflow = package.get("workflow_json")
    if not isinstance(workflow, dict) or not isinstance(workflow.get("steps"), list):
        return {
            "case_id": case_id,
            "review_status": "completed",
            "verdict": "not_evaluable",
            "checked_step_count": 0,
            "findings": [],
            "limitations": ["No workflow_json was generated."],
            "error_count": 0,
            "warning_count": 0,
            "attempts": [],
            "model": model,
            "endpoint": endpoint,
            "wire_api": wire_api,
            "reasoning_effort": reasoning_effort,
        }
    prompt = build_review_prompt(
        case_id=case_id,
        package=package,
        workstations=workstations,
        stations=stations,
        aliases=aliases,
    )
    expected_steps = len(workflow["steps"])
    attempt_records: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            text = call_fn(
                prompt=prompt,
                api_key=keys[(key_offset + attempt - 1) % len(keys)],
                endpoint=endpoint,
                model=model,
                wire_api=wire_api,
                reasoning_effort=reasoning_effort,
                timeout=timeout,
                max_output_tokens=max_output_tokens,
            )
            review = validate_review(
                _parse_json_text(text),
                case_id=case_id,
                expected_steps=expected_steps,
            )
            attempt_records.append(
                {
                    "attempt": attempt,
                    "status": "success",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            )
            review.update(
                {
                    "attempts": attempt_records,
                    "model": model,
                    "endpoint": endpoint,
                    "wire_api": wire_api,
                    "reasoning_effort": reasoning_effort,
                    "prompt_character_count": len(prompt),
                }
            )
            return review
        except Exception as exc:
            attempt_records.append(
                {
                    "attempt": attempt,
                    "status": "failed",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "case_id": case_id,
        "review_status": "failed",
        "verdict": "not_evaluable",
        "checked_step_count": 0,
        "findings": [],
        "limitations": ["Independent LLM schema review failed."],
        "error_count": 0,
        "warning_count": 0,
        "attempts": attempt_records,
        "model": model,
        "endpoint": endpoint,
        "wire_api": wire_api,
        "reasoning_effort": reasoning_effort,
    }


def review_case_workflows(
    *,
    case_id: str,
    deterministic_case: dict[str, Any],
    workstations: Path,
    stations: dict[str, StationSchema],
    aliases: dict[str, str],
    keys: list[str],
    key_offset: int,
    endpoint: str,
    model: str,
    wire_api: str,
    reasoning_effort: str,
    timeout: int,
    max_output_tokens: int,
    attempts: int,
    call_fn: LLMCall,
) -> dict[str, Any]:
    """Review every workflow emitted by one black-box Chem Agent campaign."""
    records = deterministic_case.get("workflows")
    if not isinstance(records, list) or not records:
        return {
            "case_id": case_id,
            "review_status": "completed",
            "verdict": "not_evaluable",
            "checked_step_count": 0,
            "checked_workflow_count": 0,
            "findings": [],
            "limitations": ["No workflow_json was generated by the black-box campaign."],
            "error_count": 0,
            "warning_count": 0,
            "workflow_reviews": [],
            "model": model,
            "endpoint": endpoint,
            "wire_api": wire_api,
            "reasoning_effort": reasoning_effort,
        }

    workflow_reviews: list[dict[str, Any]] = []
    for workflow_index, record in enumerate(records, start=1):
        path_text = str(record.get("workflow_path", "")).strip()
        package_path = Path(path_text).expanduser() if path_text else Path()
        try:
            package = _load_json(package_path) if path_text and package_path.exists() else {}
        except (OSError, json.JSONDecodeError, ValueError):
            package = {}
        review_id = f"{case_id}:workflow_{workflow_index}"
        review = review_case(
            case_id=review_id,
            package=package,
            deterministic_case=record,
            workstations=workstations,
            stations=stations,
            aliases=aliases,
            keys=keys,
            key_offset=key_offset + workflow_index - 1,
            endpoint=endpoint,
            model=model,
            wire_api=wire_api,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
            attempts=attempts,
            call_fn=call_fn,
        )
        review["workflow_path"] = path_text
        workflow_reviews.append(review)

    complete = all(item.get("review_status") == "completed" for item in workflow_reviews)
    workflow_verdicts = [str(item.get("verdict", "not_evaluable")) for item in workflow_reviews]
    if not complete:
        verdict = "not_evaluable"
        status = "failed"
    elif any(value == "no" for value in workflow_verdicts):
        verdict = "no"
        status = "completed"
    elif any(value == "yes" for value in workflow_verdicts):
        verdict = "yes"
        status = "completed"
    else:
        verdict = "not_evaluable"
        status = "completed"

    findings: list[dict[str, Any]] = []
    limitations: list[str] = []
    for item in workflow_reviews:
        for finding in item.get("findings", []) or []:
            if isinstance(finding, dict):
                copied = dict(finding)
                copied.setdefault("workflow_path", item.get("workflow_path", ""))
                findings.append(copied)
        limitations.extend(str(value) for value in item.get("limitations", []) or [])
    return {
        "case_id": case_id,
        "review_status": status,
        "verdict": verdict,
        "checked_step_count": sum(
            int(item.get("checked_step_count", 0)) for item in workflow_reviews
        ),
        "checked_workflow_count": len(workflow_reviews),
        "findings": findings,
        "limitations": limitations,
        "error_count": sum(int(item.get("error_count", 0)) for item in workflow_reviews),
        "warning_count": sum(int(item.get("warning_count", 0)) for item in workflow_reviews),
        "workflow_reviews": workflow_reviews,
        "model": model,
        "endpoint": endpoint,
        "wire_api": wire_api,
        "reasoning_effort": reasoning_effort,
    }


def combine_case_verdicts(
    deterministic_case: dict[str, Any], llm_case: dict[str, Any]
) -> dict[str, Any]:
    deterministic = deterministic_case.get("dispatch_schema_match", "not_evaluable")
    llm_verdict = llm_case.get("verdict", "not_evaluable")
    review_complete = llm_case.get("review_status") == "completed"
    workflow_present = bool(deterministic_case.get("workflow_present"))
    if not workflow_present:
        final = "not_evaluable"
        complete = True
    elif not review_complete:
        final = "no" if deterministic == "no" else "not_evaluable"
        complete = False
    elif deterministic == "no" or llm_verdict == "no":
        final = "no"
        complete = True
    elif deterministic == "yes" and llm_verdict == "yes":
        final = "yes"
        complete = True
    else:
        final = "not_evaluable"
        complete = False
    return {
        "case_id": deterministic_case.get("case_id") or llm_case.get("case_id"),
        "workflow_present": workflow_present,
        "deterministic_verdict": deterministic,
        "llm_verdict": llm_verdict,
        "llm_review_status": llm_case.get("review_status"),
        "dispatch_schema_match": final,
        "evaluation_complete": complete,
        "deterministic_error_count": deterministic_case.get("error_count", 0),
        "llm_error_count": llm_case.get("error_count", 0),
        "deterministic_evidence_paths": deterministic_case.get(
            "evidence_paths",
            [deterministic_case.get("evidence_path", "")]
            if deterministic_case.get("evidence_path")
            else [],
        ),
    }


def review_run(
    *,
    run_dir: Path,
    deterministic_audit: dict[str, Any],
    keys: list[str],
    endpoint: str,
    model: str,
    wire_api: str,
    reasoning_effort: str,
    timeout: int = 7200,
    max_output_tokens: int = 16000,
    attempts: int = 2,
    workers: int = 4,
    repo: Path | None = None,
    workstations: Path | None = None,
    call_fn: LLMCall = call_llm,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_dir = run_dir.expanduser().resolve()
    repo = _infer_repo(run_dir, repo)
    workstations = _infer_workstations(run_dir, repo, workstations)
    stations, aliases = load_workstation_catalog(workstations)
    deterministic_by_case = {
        str(case["case_id"]): case for case in deterministic_audit.get("cases", [])
    }

    reviews: list[dict[str, Any]] = []

    def execute(index: int, case_id: str) -> dict[str, Any]:
        return review_case_workflows(
            case_id=case_id,
            deterministic_case=deterministic_by_case[case_id],
            workstations=workstations,
            stations=stations,
            aliases=aliases,
            keys=keys,
            key_offset=index,
            endpoint=endpoint,
            model=model,
            wire_api=wire_api,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
            attempts=attempts,
            call_fn=call_fn,
        )

    case_ids = list(deterministic_by_case)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(execute, index, case_id): case_id
            for index, case_id in enumerate(case_ids)
        }
        for future in concurrent.futures.as_completed(futures):
            reviews.append(future.result())
    reviews.sort(key=lambda item: item["case_id"])

    combined = [
        combine_case_verdicts(deterministic_by_case[review["case_id"]], review)
        for review in reviews
    ]
    review_result = {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "run_dir": str(run_dir),
        "repository": str(repo),
        "workstations_source": str(workstations),
        "model": model,
        "endpoint": endpoint,
        "wire_api": wire_api,
        "reasoning_effort": reasoning_effort,
        "cases": reviews,
        "review_status_counts": dict(
            sorted(Counter(review["review_status"] for review in reviews).items())
        ),
    }
    combined_result = {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "run_dir": str(run_dir),
        "cases": combined,
        "verdict_counts": dict(
            sorted(Counter(case["dispatch_schema_match"] for case in combined).items())
        ),
        "evaluation_complete": all(case["evaluation_complete"] for case in combined),
    }
    return review_result, combined_result


def write_reviews(
    *,
    review_result: dict[str, Any],
    combined_result: dict[str, Any],
    review_output: Path,
    combined_output: Path,
) -> None:
    review_output.parent.mkdir(parents=True, exist_ok=True)
    review_output.write_text(
        json.dumps(review_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    combined_output.write_text(
        json.dumps(combined_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    run_dir = Path(review_result["run_dir"])
    combined_by_case = {
        case["case_id"]: case for case in combined_result.get("cases", [])
    }
    for review in review_result.get("cases", []):
        case_dir = run_dir / "evaluation" / review["case_id"]
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "workstation_schema_llm_review.json").write_text(
            json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (case_dir / "workstation_schema_verdict.json").write_text(
            json.dumps(combined_by_case[review["case_id"]], ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Independently review workstation workflow schemas with an LLM."
    )
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--workstations", type=Path)
    parser.add_argument("--deterministic-audit", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--endpoint")
    parser.add_argument("--wire-api", choices=["chat", "codex_responses"])
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--api-key-env")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--max-output-tokens", type=int, default=16000)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    run_dir = args.run.expanduser().resolve()
    repo = _infer_repo(run_dir, args.repo)
    deterministic_path = args.deterministic_audit or (
        run_dir / "evaluation" / "workstation_schema_audit.json"
    )
    deterministic = _load_json(deterministic_path)
    env = load_dotenv(repo / ".env")
    env.update(os.environ)
    model = args.model or env.get("REFINER_LLM_MODEL_NAME", "")
    endpoint = args.endpoint or env.get("REFINER_LLM_ENDPOINT_URL", "")
    wire_api = args.wire_api or env.get("REFINER_LLM_WIRE_API", "")
    reasoning_effort = args.reasoning_effort or env.get(
        "REFINER_LLM_REASONING_EFFORT", ""
    )
    keys = discover_keys(env, args.api_key_env)
    workflow_cases = [
        case for case in deterministic.get("cases", []) if case.get("workflow_present")
    ]
    if workflow_cases and not keys:
        raise RuntimeError("No API key found for independent LLM schema review")
    if workflow_cases and (not model or not endpoint or not wire_api):
        raise RuntimeError(
            "Independent LLM schema review requires model, endpoint, and wire API"
        )
    if not keys:
        keys = ["no-call-required"]
    reviews, combined = review_run(
        run_dir=run_dir,
        deterministic_audit=deterministic,
        keys=keys,
        endpoint=endpoint,
        model=model,
        wire_api=wire_api,
        reasoning_effort=reasoning_effort,
        timeout=args.timeout,
        max_output_tokens=args.max_output_tokens,
        attempts=args.attempts,
        workers=args.workers,
        repo=repo,
        workstations=args.workstations,
    )
    review_output = run_dir / "evaluation" / "workstation_schema_llm_review.json"
    combined_output = run_dir / "evaluation" / "workstation_schema_verdict.json"
    write_reviews(
        review_result=reviews,
        combined_result=combined,
        review_output=review_output,
        combined_output=combined_output,
    )
    print(
        json.dumps(
            {
                "review_output": str(review_output),
                "combined_output": str(combined_output),
                "evaluation_complete": combined["evaluation_complete"],
                "cases": combined["cases"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if combined["evaluation_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

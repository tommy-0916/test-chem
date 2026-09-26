"""Recheck and locally repair a saved, blocked Device workflow preview.

This is a diagnostic lane only.  It never accepts a Device Plan, issues a
certificate, saves a dispatch payload, or sends work to a laboratory.  Saved
translation chunks are reused in their original order; a Plan-step regrouping
would change the workflow and is deliberately forbidden.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SOURCE_NAMES = (
    "candidate",
    "research_authority",
    "device_state",
    "relationship_bindings",
    "repair_summary",
)
IMPLEMENTATION_NAMES = (
    "diagnostic_workflow_preview.py",
    "single_agent.py",
    "plan_repair.py",
)


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _sources(manifest: dict[str, Any]) -> dict[str, Path]:
    listed = manifest.get("source_paths")
    digests = manifest.get("source_sha256")
    if not isinstance(listed, dict) or not isinstance(digests, dict):
        raise ValueError("manifest lacks source paths or source hashes")
    if set(listed) != set(SOURCE_NAMES) or set(digests) != set(SOURCE_NAMES):
        raise ValueError("manifest must bind exactly the five source files")
    paths: dict[str, Path] = {}
    for name in SOURCE_NAMES:
        raw_path = listed[name]
        expected = digests[name]
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(f"invalid source path: {name}")
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValueError(f"invalid source SHA-256: {name}")
        path = Path(raw_path).resolve(strict=True)
        if not path.is_file() or _file_digest(path) != expected:
            raise ValueError(f"frozen source file changed: {name}")
        paths[name] = path
    return paths


def _preview_context(manifest: dict[str, Any]) -> None:
    expected = _digest({
        "source_digests": manifest.get("source_sha256"),
        "normalized_candidate_sha256": manifest.get("normalized_candidate_sha256"),
        "workstation_truth_sha256": manifest.get("workstation_truth_sha256"),
        "audit_context_sha256": manifest.get("audit_context_sha256"),
        "model": manifest.get("model"),
        "implementation_sha256": manifest.get("implementation_sha256"),
    })
    if manifest.get("diagnostic_context_sha256") != expected:
        raise ValueError("saved diagnostic context digest is inconsistent")


def _model_descriptor(manifest: dict[str, Any]) -> dict[str, Any]:
    model = manifest.get("model")
    if not isinstance(model, dict):
        raise ValueError("manifest lacks a model descriptor")
    if model.get("wire_api") != "codex_responses":
        raise ValueError("saved diagnostic model uses an unsupported wire API")
    for field in ("model_name", "endpoint", "reasoning_effort"):
        if not isinstance(model.get(field), str) or not model[field].strip():
            raise ValueError(f"invalid diagnostic model {field}")
    endpoint = urlsplit(model["endpoint"])
    # The frozen manifest is integrity-checked, not externally signed.  It
    # must never be allowed to redirect a user's ephemeral API key elsewhere.
    if endpoint.geturl() != "https://api.kimi.com/coding/v1":
        raise ValueError("diagnostic repair requires the approved Kimi endpoint")
    chunk_size = model.get("diagnostic_chunk_size")
    if type(chunk_size) is not int or not 1 <= chunk_size <= 6:
        raise ValueError("invalid saved diagnostic chunk size")
    max_tokens = model.get("max_tokens")
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError("invalid saved model max_tokens")
    return model


def _implementation_drift(manifest: dict[str, Any]) -> dict[str, Any]:
    prior = manifest.get("implementation_sha256")
    if not isinstance(prior, dict):
        raise ValueError("manifest lacks implementation hashes")
    paths = {name: Path(__file__).with_name(name) for name in IMPLEMENTATION_NAMES}
    paths["llm_factory.py"] = Path(__file__).parent / "utils" / "llm_factory.py"
    current = {name: _file_digest(path) for name, path in paths.items()}
    return {
        "saved": copy.deepcopy(prior),
        "current": current,
        "changed_files": [name for name in current if prior.get(name) != current[name]],
        "diagnostic_replay_implementation_sha256": _file_digest(Path(__file__)),
    }


class _ModelRequired:
    """Fail explicitly if a saved-workflow repair needs an unavailable model."""

    def invoke(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("diagnostic_model_required: REFINER_LLM_API_KEY is absent")

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("diagnostic_model_required: REFINER_LLM_API_KEY is absent")


def _load_original_chunks(
    diagnostic_dir: Path,
    *,
    manifest: dict[str, Any],
    saved_package: dict[str, Any],
    agent: Any,
    normalized: dict[str, Any],
    chunk_size: int,
) -> tuple[dict[int, dict[str, Any]], list[list[dict[str, Any]]], int]:
    plan = normalized.get("device_plan")
    if not isinstance(plan, list) or not plan or any(not isinstance(x, dict) for x in plan):
        raise ValueError("normalized Device Plan is missing or malformed")
    chunks = [plan[index:index + chunk_size] for index in range(0, len(plan), chunk_size)]
    if [step for chunk in chunks for step in chunk] != plan:
        raise ValueError("diagnostic chunks do not partition the frozen Device Plan")
    total = len(chunks)
    saved_workflow = saved_package.get("workflow_json")
    if not isinstance(saved_workflow, dict) or not isinstance(saved_workflow.get("steps"), list):
        raise ValueError("saved diagnostic package lacks workflow steps")
    if not saved_workflow["steps"]:
        raise ValueError("saved diagnostic workflow is empty")

    cache: dict[int, dict[str, Any]] = {}
    flattened: list[dict[str, Any]] = []
    carryover: dict[str, Any] = {}
    seen_ids: set[str] = set()
    covered_plan_steps: set[tuple[type, Any]] = set()
    for index, plan_chunk in enumerate(chunks):
        path = diagnostic_dir / "translation_chunks" / f"chunk_{index + 1:03d}_of_{total:03d}.json"
        record = _object(path)
        if record.get("chunk_index") != index or record.get("total_chunks") != total:
            raise ValueError(f"saved chunk {index + 1} has the wrong position")
        binding = _digest({
            "diagnostic_context_sha256": manifest["diagnostic_context_sha256"],
            "chunk_index": index,
            "total_chunks": total,
            "chunk": plan_chunk,
            "carryover": carryover,
        })
        if record.get("chunk_binding_sha256") != binding:
            raise ValueError(f"saved chunk {index + 1} does not bind to frozen inputs")
        result = record.get("result")
        if not isinstance(result, dict) or record.get("result_sha256") != _digest(result):
            raise ValueError(f"saved chunk {index + 1} result digest is invalid")
        steps = result.get("steps")
        txt = result.get("workflow_txt")
        if not isinstance(steps, list) or not steps or not isinstance(txt, str):
            raise ValueError(f"saved chunk {index + 1} has no valid translation")
        allowed = {(type(step.get("plan_step")), step.get("plan_step")) for step in plan_chunk}
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError(f"saved chunk {index + 1} contains a non-object step")
            source = (type(step.get("source_plan_step")), step.get("source_plan_step"))
            if source not in allowed:
                raise ValueError(f"saved chunk {index + 1} contains an out-of-window Plan step")
            covered_plan_steps.add(source)
            step_id = step.get("device_step_id")
            if not isinstance(step_id, str) or not step_id.strip() or step_id in seen_ids:
                raise ValueError("saved workflow has a missing or duplicate device_step_id")
            seen_ids.add(step_id)
        # The frozen preview rendered its text from the assembled JSON.  Seed
        # the repair loop from those same nodes, not a model's independent TXT
        # draft, so a text mismatch cannot manufacture a repair target.
        cache[index] = {
            "steps": copy.deepcopy(steps),
            "txt": agent._workflow_txt_from_json({"steps": steps}),
        }
        flattened.extend(copy.deepcopy(steps))
        carryover = agent._lid_and_container_state_after(flattened)
    expected_plan_steps = {(type(step.get("plan_step")), step.get("plan_step")) for step in plan}
    if covered_plan_steps != expected_plan_steps:
        raise ValueError("saved workflow does not cover every frozen Plan step")
    if len(flattened) != len(saved_workflow["steps"]):
        raise ValueError("saved workflow length differs from its frozen chunks")
    for number, (raw, saved) in enumerate(zip(flattened, saved_workflow["steps"]), start=1):
        if not isinstance(saved, dict) or saved.get("step_number") != number:
            raise ValueError("saved workflow numbering is not sequential")
        for field in ("device_step_id", "source_plan_step", "workstation", "operation"):
            if type(raw.get(field)) is not type(saved.get(field)) or raw.get(field) != saved.get(field):
                raise ValueError(
                    f"saved workflow changed original chunk order or {field} at step {number}"
                )
    return cache, chunks, len(flattened)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    diagnostic_dir = args.diagnostic_dir.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    if not diagnostic_dir.is_dir():
        raise ValueError("--diagnostic-dir must be a directory")
    if output_dir.exists():
        raise ValueError("--output-dir must be a new, separate directory")
    manifest = _object(diagnostic_dir / "run_manifest.json")
    saved_package = _object(diagnostic_dir / "diagnostic_package.json")
    if (
        manifest.get("status") != "diagnostic_blocked"
        or manifest.get("diagnostic_only") is not True
        or saved_package.get("status") != "diagnostic_blocked"
        or saved_package.get("diagnostic_only") is not True
        or manifest.get("diagnostic_context_sha256")
        != saved_package.get("diagnostic_context_sha256")
    ):
        raise ValueError("input is not one frozen, blocked diagnostic preview")
    for record in (manifest, saved_package):
        if (
            record.get("feasibility_accepted") is not False
            or record.get("feasibility_certificate") != {}
            or record.get("dispatchable") is not False
            or record.get("dispatch_payload") != {}
            or record.get("real_dispatch") is not False
        ):
            raise ValueError("input diagnostic preview carries forbidden acceptance authority")
    _preview_context(manifest)
    model_descriptor = _model_descriptor(manifest)
    source_paths = _sources(manifest)
    if (
        output_dir == diagnostic_dir
        or output_dir in diagnostic_dir.parents
        or diagnostic_dir in output_dir.parents
    ):
        raise ValueError("output and diagnostic input directories must not be nested")
    if any(output_dir == path or output_dir in path.parents for path in source_paths.values()):
        raise ValueError("output directory must not contain a frozen source file")
    candidate = _object(source_paths["candidate"])
    research = _object(source_paths["research_authority"])
    bindings = _object(source_paths["relationship_bindings"])
    repair_summary = _object(source_paths["repair_summary"])
    if repair_summary.get("status") != "stopped":
        raise ValueError("frozen repair summary is not stopped")
    blocker = repair_summary.get("material_blocker_summary")
    if not isinstance(blocker, dict) or not blocker.get("issue_count"):
        raise ValueError("frozen repair summary lacks material blockers")
    if saved_package.get("material_blocker_summary") != blocker:
        raise ValueError("saved package changed the original material blockers")
    if repair_summary.get("final_candidate_digest") != _digest(candidate):
        raise ValueError("frozen repair summary does not bind the candidate")
    package = research.get("research_action_package_v2")
    if not isinstance(package, dict) or bindings.get("research_authority_sha256") != _digest(package):
        raise ValueError("relationship authority does not bind the Research package")
    # plan_repair bootstraps both the repository root and device_agent when
    # this standalone CLI is invoked as ``python device_agent/...py``.
    from plan_repair import _offline_full_plan_context
    from material_relationship_compiler import candidate_binding_sha256

    if bindings.get("candidate_sha256") != candidate_binding_sha256(candidate):
        raise ValueError("relationship authority does not bind the Device candidate")

    runtime: dict[str, Any] = {}
    context = _offline_full_plan_context(
        source_paths["device_state"], research, bindings,
        diagnostic_runtime=runtime,
    )
    _, _, workstation_truth_sha256, _, audit_context_sha256, finalize = context
    if (
        workstation_truth_sha256 != manifest.get("workstation_truth_sha256")
        or workstation_truth_sha256 != bindings.get("workstation_truth_sha256")
        or audit_context_sha256 != manifest.get("audit_context_sha256")
    ):
        raise ValueError("current Device truth or Research audit context changed")
    agent = runtime["agent"]
    state = runtime["state"]
    normalized, pre_findings, post_findings = finalize(candidate)
    if _digest(normalized) != manifest.get("normalized_candidate_sha256"):
        raise ValueError("current normalization changed the frozen Device candidate")
    saved_plan_audit = _object(diagnostic_dir / "plan_audit.json")
    if (
        saved_plan_audit.get("status") != "diagnostic_blocked"
        or saved_plan_audit.get("material_blocker_summary") != blocker
        or _digest(saved_plan_audit.get("pre_findings")) != _digest(pre_findings)
        or _digest(saved_plan_audit.get("post_findings")) != _digest(post_findings)
    ):
        raise ValueError("current Plan audit differs from the frozen blocked preview")
    if manifest.get("device_plan_steps") != len(normalized.get("device_plan", [])):
        raise ValueError("saved manifest Plan-step count is inconsistent")
    cache, plan_chunks, source_step_count = _load_original_chunks(
        diagnostic_dir,
        manifest=manifest,
        saved_package=saved_package,
        agent=agent,
        normalized=normalized,
        chunk_size=model_descriptor["diagnostic_chunk_size"],
    )
    if saved_package.get("plan_finding_count") != len(post_findings):
        raise ValueError("current Plan audit no longer matches the saved blocked preview")
    if not post_findings:
        raise ValueError("Plan audit is no longer blocked; use the normal path")

    # A diagnostic shadow is used solely to exercise the workflow checker and
    # its exact-ID repair loop.  Quantity and recipe readiness are independent
    # upstream/downstream gates whose unanchored findings cannot authorize a
    # V2 workflow-node edit.  The original values remain frozen in the input
    # and are copied into the blocked diagnostic report below.
    workflow_only_plan = copy.deepcopy(normalized)
    omitted_gate_fields = [
        name for name in ("quantity_audit", "recipe_materialization")
        if name in workflow_only_plan
    ]
    for name in omitted_gate_fields:
        workflow_only_plan.pop(name)

    state.feasibility_accepted = False
    state.feasibility_certificate = {}
    state.accepted_device_plan_contract = {}
    state.workflow_json = {}
    state.workflow_txt = ""
    state.txt_format_reference = state.txt_format_reference or agent._txt_format_reference
    state.json_format_reference = state.json_format_reference or agent._json_format_reference
    implementation = _implementation_drift(manifest)
    api_key = os.getenv("REFINER_LLM_API_KEY", "").strip()
    model_available = bool(api_key)
    if model_available:
        os.environ["CHEM_LLM_COMPONENT"] = "device"
        os.environ["REFINER_LLM_WIRE_API"] = "codex_responses"
        os.environ["REFINER_LLM_REASONING_EFFORT"] = model_descriptor["reasoning_effort"]
        from utils.llm_factory import LLMFactory

        agent._model = LLMFactory.create(
            provider="openai",
            model_name=model_descriptor["model_name"],
            endpoint_url=model_descriptor["endpoint"],
            api_key=api_key,
            timeout=args.timeout_seconds,
            max_tokens=model_descriptor["max_tokens"],
        )
    else:
        agent._model = _ModelRequired()

    result: dict[str, Any] = {}
    runtime_error = ""
    try:
        result = agent._translate_and_verify(
            state,
            workflow_only_plan,
            initial_chunk_cache=cache,
            plan_chunks_override=plan_chunks,
            materialize_recipes=False,
        )
    except Exception as exc:
        if "diagnostic_model_required" in str(exc):
            runtime_error = "model_required_for_scoped_workflow_repair"
        else:
            # Provider exceptions may contain headers or request fragments.
            # Never persist their raw text alongside diagnostic artifacts.
            runtime_error = f"workflow_repair_internal_error:{type(exc).__name__}"

    workflow_check = result.get("dispatch_validation")
    workflow_check = workflow_check if isinstance(workflow_check, dict) else {}
    repair_cycle = result.get("workflow_repair_cycle")
    repair_cycle = repair_cycle if isinstance(repair_cycle, dict) else {}
    workflow_modifications = repair_cycle.get("modification_count", 0)
    if type(workflow_modifications) is not int:
        workflow_modifications = 0
    repaired_workflow = result.get("workflow_json")
    repaired_workflow = (
        repaired_workflow
        if isinstance(repaired_workflow, dict) and isinstance(repaired_workflow.get("steps"), list)
        else copy.deepcopy(saved_package["workflow_json"])
    )
    repaired_txt = result.get("workflow_txt")
    repaired_txt = repaired_txt if isinstance(repaired_txt, str) else saved_package.get("workflow_txt", "")
    if runtime_error:
        stop_reason = runtime_error
    elif workflow_check.get("repair_stop_reason"):
        stop_reason = str(workflow_check["repair_stop_reason"])
    elif workflow_check.get("status") == "passed":
        stop_reason = "workflow_subcheck_passed_plan_still_blocked"
    else:
        stop_reason = "workflow_subcheck_failed_plan_still_blocked"

    output_dir.mkdir(parents=True, exist_ok=False)
    fixed = {
        "schema_version": 1,
        "status": "diagnostic_blocked",
        "diagnostic_only": True,
        "feasibility_accepted": False,
        "feasibility_certificate": {},
        "dispatchable": False,
        "dispatch_payload": {},
        "real_dispatch": False,
    }
    _save(output_dir / "run_manifest.json", {
        **fixed,
        "source_preview_dir": str(diagnostic_dir),
        "source_preview_manifest_sha256": _file_digest(diagnostic_dir / "run_manifest.json"),
        "source_preview_package_sha256": _file_digest(diagnostic_dir / "diagnostic_package.json"),
        "source_sha256": copy.deepcopy(manifest["source_sha256"]),
        "diagnostic_context_sha256": manifest["diagnostic_context_sha256"],
        "normalized_candidate_sha256": manifest["normalized_candidate_sha256"],
        "workstation_truth_sha256": workstation_truth_sha256,
        "audit_context_sha256": audit_context_sha256,
        "saved_translation_chunks": len(cache),
        "saved_workflow_steps": source_step_count,
        "recipe_materialization_skipped": True,
        "implementation": implementation,
        "model_available": model_available,
        "original_plan_patches_used": repair_summary.get("patches_used", 0),
        "diagnostic_workflow_modifications": workflow_modifications,
    })
    _save(output_dir / "repair_report.json", {
        **fixed,
        "stop_reason": stop_reason,
        "workflow_subcheck_status": workflow_check.get("status", "not_completed"),
        "workflow_subcheck_passed": workflow_check.get("status") == "passed",
        "workflow_repair_cycle": copy.deepcopy(repair_cycle),
        "original_plan_patches_used": repair_summary.get("patches_used", 0),
        "diagnostic_workflow_modifications": workflow_modifications,
        "workflow_check": copy.deepcopy(workflow_check),
        "original_workflow_check": copy.deepcopy(saved_package.get("workflow_check", {})),
        "original_material_blocker_summary": copy.deepcopy(blocker),
        "original_material_issue_count": blocker.get("issue_count"),
        "original_repair_remaining_findings": copy.deepcopy(
            repair_summary.get("remaining_finding_records", [])
        ),
        "original_quantity_audit": copy.deepcopy(candidate.get("quantity_audit", {})),
        "original_quantity_issue_count": len(
            (candidate.get("quantity_audit") or {}).get("issues", [])
        ),
        "workflow_only_omitted_gate_fields": omitted_gate_fields,
        "recipe_materialization_skipped": True,
        "current_plan_pre_findings": pre_findings,
        "current_plan_post_findings": post_findings,
        "implementation_drift": implementation,
        "runtime_error": runtime_error,
    })
    _save(output_dir / "diagnostic_package.json", {
        **fixed,
        "stop_reason": stop_reason,
        "workflow_json": repaired_workflow,
        "workflow_txt": repaired_txt,
        "workflow_check": workflow_check,
        "workflow_repair_cycle": repair_cycle,
        "original_material_blocker_summary": blocker,
        "plan_finding_count": len(post_findings),
        "quantity_audit": copy.deepcopy(candidate.get("quantity_audit", {})),
        "recipe_materialization_skipped": True,
    })
    print(
        "diagnostic workflow repair: "
        f"{stop_reason}; workflow={workflow_check.get('status', 'not_completed')}; "
        f"plan_findings={len(post_findings)}; dispatchable=false",
        flush=True,
    )
    return 0 if not runtime_error else 2


if __name__ == "__main__":
    raise SystemExit(main())

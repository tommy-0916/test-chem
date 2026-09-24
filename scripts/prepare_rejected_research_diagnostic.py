"""Prepare a clearly rejected Research candidate for a V1 Device connectivity test.

This is deliberately *not* a Research V2 adapter.  It copies the unaccepted
LLM candidate into an isolated, V1-compatible diagnostic input so that Device
engineering can be exercised without making the Research quality gate pass.
Neither this command nor the resulting files dispatch a laboratory task.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CANDIDATE_PATH = "raw_llm_outputs.macro_plan_design_retry_1.macro_plan"
BOOTSTRAP_PATH = "raw_llm_outputs.macro_action_design_bootstrap"


class DiagnosticPreparationError(ValueError):
    """The input cannot safely be identified as a rejected V2 candidate."""


def _nonempty_plan(value: Any) -> bool:
    return isinstance(value, list) and bool(value)


def _assert_no_accepted_plan(source: dict[str, Any]) -> None:
    """Do not use this bridge on a state that already has a formal handoff."""

    for key in ("macro_plan", "macro_action_steps"):
        if _nonempty_plan(source.get(key)):
            raise DiagnosticPreparationError(f"source already has accepted {key}")
    for parent_key in (
        "device_adaptation_handoff",
        "persistent_outputs",
        "A. research layer 内部持久化输出",
        "B. 发给下游 device adaptation layer agent 的外部交接输出",
    ):
        parent = source.get(parent_key)
        if not isinstance(parent, dict):
            continue
        for key in ("macro_plan", "macro_action_steps", "待执行 macro plan"):
            if _nonempty_plan(parent.get(key)):
                raise DiagnosticPreparationError(
                    f"source already has accepted {parent_key}.{key}"
                )
        if parent.get("research_action_package_v2"):
            raise DiagnosticPreparationError(
                f"source already has a nonempty {parent_key}.research_action_package_v2"
            )
    if source.get("research_action_package_v2"):
        raise DiagnosticPreparationError(
            "source already has a nonempty Research V2 action package"
        )


def prepare_state(source: dict[str, Any]) -> dict[str, Any]:
    """Return a minimal, visibly diagnostic copy; never mutate ``source``."""

    if source.get("contract_version") != "v2":
        raise DiagnosticPreparationError("source must be a Research V2 state")
    if source.get("status") != "manual_required":
        raise DiagnosticPreparationError(
            "source must have rejected status manual_required"
        )
    _assert_no_accepted_plan(source)

    raw = source.get("raw_llm_outputs")
    retry = raw.get("macro_plan_design_retry_1") if isinstance(raw, dict) else None
    bootstrap = (
        raw.get("macro_action_design_bootstrap") if isinstance(raw, dict) else None
    )
    if not isinstance(retry, dict):
        raise DiagnosticPreparationError(
            "missing raw_llm_outputs.macro_plan_design_retry_1"
        )
    plan = retry.get("macro_plan")
    if not _nonempty_plan(plan) or not all(isinstance(step, dict) for step in plan):
        raise DiagnosticPreparationError(f"{CANDIDATE_PATH} must be nonempty steps")
    if not isinstance(bootstrap, dict) or not bootstrap:
        raise DiagnosticPreparationError(f"{BOOTSTRAP_PATH} must be an object")
    stage_plan = retry.get("current_stage_plan")
    if not isinstance(stage_plan, str) or not stage_plan.strip():
        raise DiagnosticPreparationError(
            "retry current_stage_plan must identify the unresolved constraints"
        )
    event = source.get("event")
    query = event.get("query") if isinstance(event, dict) else None
    if not isinstance(query, str) or not query.strip():
        raise DiagnosticPreparationError("source event.query must be nonempty")

    # Only a small set of Research context fields is passed to the V1 Device
    # compatibility builder.  In particular, neither accepted V2 package nor
    # its empty handoff mirrors is copied or synthesized.
    state = {
        "contract_version": "v1",
        "status": source["status"],
        "campaign_id": source.get("campaign_id", ""),
        "event": {
            "event_type": "rejected_research_connectivity_diagnostic",
            "query": query,
            "constraints": {"real_dispatch": False, "planning_only": True},
        },
        "stage_route": copy.deepcopy(source.get("stage_route", "")),
        "current_stage": copy.deepcopy(source.get("current_stage", "")),
        "current_stage_plan": stage_plan,
        "stage_route_reason": copy.deepcopy(source.get("stage_route_reason", "")),
        "current_stage_reason": copy.deepcopy(source.get("current_stage_reason", "")),
        "macro_plan": copy.deepcopy(plan),
        "macro_action": copy.deepcopy(bootstrap),
        "current_evidence_bundle": copy.deepcopy(
            source.get("current_evidence_bundle") or {}
        ),
        "knowledge_hits": copy.deepcopy(source.get("knowledge_hits") or []),
        "survey_report": copy.deepcopy(source.get("survey_report") or ""),
        "extracted_protocols": copy.deepcopy(source.get("extracted_protocols") or []),
        "observations": copy.deepcopy(source.get("observations") or []),
        "latest_observation": copy.deepcopy(source.get("latest_observation") or ""),
        "diagnostic_forced_continue": {
            "source_contract_version": "v2",
            "source_status": source["status"],
            "source_failure_category": source.get("failure_category", ""),
            "candidate_source_path": CANDIDATE_PATH,
            "runtime_contract": "v1_compat",
            "formal_v2_validated": False,
            "dispatchable": False,
            "real_dispatch": False,
            "warning": (
                "Rejected raw Research candidate; engineering connectivity only. "
                "Missing scientific/equipment facts remain unresolved. "
                "Do not claim Research V2 acceptance, feasibility certification, "
                "or 303 laboratory dispatch."
            ),
        },
    }
    # The Device prompt includes macro_action, so keep the warning visible
    # there as well as in the machine-readable state and manifest.
    state["macro_action"]["diagnostic_forced_continue"] = copy.deepcopy(
        state["diagnostic_forced_continue"]
    )
    return state


def write_diagnostic(source_path: Path, output_dir: Path) -> tuple[Path, Path]:
    source_path = source_path.expanduser().resolve(strict=True)
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes.decode("utf-8"))
    if not isinstance(source, dict):
        raise DiagnosticPreparationError("source JSON must be an object")
    state = prepare_state(source)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    state["diagnostic_forced_continue"]["source_sha256"] = source_sha256
    state["macro_action"]["diagnostic_forced_continue"]["source_sha256"] = (
        source_sha256
    )

    output_dir = output_dir.expanduser().resolve()
    # Requiring a new directory prevents a rerun from overwriting any Device
    # checkpoint, source artifact, or unrelated user file.
    output_dir.mkdir(parents=True, exist_ok=False)
    state_path = output_dir / "diagnostic_research_state.json"
    manifest_path = output_dir / "diagnostic_manifest.json"
    state_bytes = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    state_path.write_bytes(state_bytes)
    manifest = {
        "kind": "rejected_research_v1_compat_device_diagnostic",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_research_state": str(source_path),
        "source_sha256": source_sha256,
        "source_contract_version": source["contract_version"],
        "source_status": source["status"],
        "source_failure_category": source.get("failure_category", ""),
        "candidate_source_path": CANDIDATE_PATH,
        "candidate_step_count": len(state["macro_plan"]),
        "runtime_contract": "v1_compat",
        "diagnostic_state": str(state_path),
        "diagnostic_state_sha256": hashlib.sha256(state_bytes).hexdigest(),
        "formal_v2_validated": False,
        "dispatchable": False,
        "real_dispatch": False,
        "no_official_research_acceptance_or_certificate": True,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return state_path, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-state", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        state_path, manifest_path = write_diagnostic(
            args.source_state, args.output_dir
        )
    except (DiagnosticPreparationError, FileNotFoundError, FileExistsError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(f"diagnostic_state={state_path}")
    print(f"manifest={manifest_path}")
    print("runtime_contract=v1_compat; formal_v2_validated=false; real_dispatch=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

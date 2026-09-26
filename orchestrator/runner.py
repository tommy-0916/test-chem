"""Campaign orchestrator: research -> device -> execution -> research loop.

Automates the previously manual loop: save research state, run the device
agent, wait for the machine result (via an execution adapter), feed the
observation (or device feasibility error) back into research B2, and repeat
until the goal is reached, a human must take over, a feasibility deadlock is
detected, or the iteration budget is exhausted.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reaserch_agent.plan_ledger import (  # noqa: E402
    PlanLedger,
    default_ledger_path,
    generate_campaign_id,
)
from device_agent.human_quantity_approval import (  # noqa: E402
    HumanQuantityApprovalError,
    approval_contract_template,
    validate_human_quantity_approvals,
)
from device_agent.feasibility_certificate import (  # noqa: E402
    FEASIBILITY_CERTIFICATE_VERSION,
    FULL_PLAN_CONTRACT_CERTIFICATE_VERSIONS,
    device_plan_contract_digest,
    device_plan_contract_view,
    feasibility_certificate_id,
    feasibility_certificate_protected_payload,
    stable_digest as certificate_stable_digest,
    strict_feasibility_certificate_version,
)
from device_agent.dispatch_checker import check_dispatch, write_check_report  # noqa: E402
from chem_agent_contracts.adapters import build_observation_event_v2  # noqa: E402
from chem_agent_contracts.v2 import (  # noqa: E402
    DeviceWorkflowPackageV2,
    ResearchActionPackageV2,
)

from .execution_adapters import BaseExecutionAdapter  # noqa: E402
from .human_readable import write_human_readable_result  # noqa: E402

ResearchStepFn = Callable[..., Dict[str, Any]]
DeviceStepFn = Callable[..., Dict[str, Any]]

STOP_GOAL_REACHED = "goal_reached"
STOP_MAX_ITERATIONS = "max_iterations"
STOP_MANUAL_REQUIRED = "manual_required"
STOP_FEASIBILITY_DEADLOCK = "feasibility_deadlock"
STOP_DEVICE_ERROR = "device_error"
STOP_RESEARCH_ERROR = "research_error"
STOP_REVIEW_REQUIRED = "scientific_review_required"
STOP_TERMINAL_UNMAPPABLE = "terminal_unmappable"
STOP_READY_FOR_DISPATCH = "ready_for_dispatch"

APPROVAL_FILENAME = "review_approval.json"
DEVICE_REPAIR_MARKDOWN = "AWAITING_DEVICE_REPAIR.md"
DEVICE_REPAIR_REQUEST = "device_repair_request.json"
DEVICE_PLAN_OVERRIDE_TEMPLATE = "device_plan_override.template.json"
MAX_CAMPAIGN_ITERATIONS = 12

RESEARCH_FEEDBACK_TYPE = "research_replan_required"
RESEARCH_FAILURE_SCOPE = "route_feasibility"
DEVICE_LOCAL_FAILURE_SCOPES = {
    "device_plan",
    "device_quantity",
    "device_local_quantity",
    "device_workflow",
    "device_internal",
}
DEVICE_LOCAL_FEEDBACK_TYPES = {
    "device_workflow_error",
    "device_local_quantity_error",
    "device_internal_error",
    "human_review_required",
}
DEVICE_LOCAL_ERROR_TYPES = {
    "workflow_translation_failed",
    "workflow_skill_review_failed",
    "recipe_materialization_failed",
    "device_workflow_error",
    "device_local_quantity_error",
    "device_internal_error",
    "human_review_required",
}
SCIENTIFIC_QUANTITY_CHANGE_KINDS = {
    "replicate_batch",
    "new_batch",
    "scale_out_for_minimum",
    "concentration_change",
    "molar_ratio_change",
    "amount_change",
    "total_amount_change",
    "single_batch_amount_change",
    "parameter_change",
}


class DeviceRepairResumeError(ValueError):
    """Raised when a manual Device override violates a frozen invariant."""


class DispatchCheckBlockedError(ValueError):
    """A fresh local contract check did not authorize an adapter invocation."""

    def __init__(self, report: Dict[str, Any], report_paths: Dict[str, str]) -> None:
        super().__init__(f"Device dispatch check {report.get('status', 'not_verifiable')}")
        self.report = report
        self.report_paths = report_paths


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _value_signature(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _contract_versions_from_args(args: Sequence[str]) -> List[str]:
    """Read explicit contract flags without letting a later flag win."""

    versions: List[str] = []
    index = 0
    while index < len(args):
        value = str(args[index])
        if value == "--contract-version":
            if index + 1 >= len(args):
                raise ValueError("--contract-version requires v1 or v2")
            versions.append(str(args[index + 1]).strip())
            index += 2
            continue
        if value.startswith("--contract-version="):
            versions.append(value.split("=", 1)[1].strip())
        index += 1
    return versions


def _validated_package_contract_version(
    package: Dict[str, Any],
    expected: str,
    *,
    label: str,
) -> str:
    """Return a package's proven effective contract or fail closed."""

    version = str(package.get("contract_version") or "").strip()
    resolution = _as_dict(package.get("contract_resolution"))
    if version not in {"v1", "v2"}:
        raise DeviceRepairResumeError(
            f"{label} lacks a valid contract_version"
        )
    if (
        str(resolution.get("requested") or "").strip() != version
        or str(resolution.get("effective") or "").strip() != version
        or resolution.get("requested_matches_effective") is not True
    ):
        raise DeviceRepairResumeError(
            f"{label} has an inconsistent contract_resolution"
        )
    if version != expected:
        raise DeviceRepairResumeError(
            f"{label} effective contract_version={version} does not match "
            f"requested contract_version={expected}"
        )
    return version


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return ""


def _nested_dicts(value: Any) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    stack = [value]
    seen: Set[int] = set()
    while stack:
        current = stack.pop()
        if isinstance(current, (dict, list)):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
        if isinstance(current, dict):
            nodes.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return nodes


def package_feasibility_accepted(package: Dict[str, Any]) -> bool:
    """True when any layer of a terminal wrapper records Stage-1 acceptance."""

    if _as_dict(package.get("error_package")).get("type") == "device_external_return_wait_required":
        # Historical certificates may remain in review evidence. None can
        # authorize crossing the current frozen Research observation barrier.
        return False
    for node in _nested_dicts(package):
        if node.get("feasibility_accepted") is True:
            return True
        certificate = node.get("feasibility_certificate")
        if isinstance(certificate, dict) and certificate.get("accepted") is True:
            return True
    return False


def canonical_success_envelope(package: Dict[str, Any]) -> bool:
    """Return true only for an unambiguous, non-dispatched success envelope.

    Raw/legacy Device output uses ``success`` with no route (or ``none``),
    while the V2 wire adapter uses ``ready_for_dispatch`` with route
    ``success``.  Error-routing metadata is not allowed to coexist with either
    pair: a contradictory package must remain at a fail-closed route.
    """

    status = str(package.get("status") or "").strip().lower()
    route = str(package.get("feedback_route") or "").strip().lower()
    feedback_type = str(package.get("feedback_type") or "").strip().lower()
    failure_scope = str(package.get("failure_scope") or "").strip().lower()
    canonical_pair = (
        status == "success" and route in {"", "none"}
    ) or (
        status == "ready_for_dispatch" and route == "success"
    )
    return (
        canonical_pair
        and feedback_type in {"", "none"}
        and failure_scope in {"", "none"}
    )


def feedback_route(package: Dict[str, Any]) -> str:
    """Return the orchestrator route using an explicit, fail-closed whitelist.

    Once Device has accepted route feasibility, legacy labels such as
    ``device_feasibility_error`` are insufficient to enter Research B2.  Only
    the explicit ``research_replan_required`` + ``route_feasibility`` contract
    is allowed to mutate the Research macro plan.
    """

    status = str(package.get("status", "")).strip()
    route = str(package.get("feedback_route", "")).strip().lower()
    scope = str(package.get("failure_scope", "")).strip().lower()
    feedback_type = str(package.get("feedback_type", "")).strip().lower()
    nested = _nested_dicts(package)
    feasibility_accepted = package_feasibility_accepted(package)
    nested_device_scope = any(
        str(node.get("failure_scope") or "").strip().lower().startswith("device_")
        for node in nested
    )
    nested_local_route = any(
        str(node.get("feedback_route") or "").strip().lower()
        in {"device", "human", "stop"}
        for node in nested
    )
    nested_local_feedback = any(
        str(node.get("feedback_type") or "").strip().lower()
        in DEVICE_LOCAL_FEEDBACK_TYPES
        for node in nested
    )
    nested_local_error = any(
        str(node.get("type") or "").strip().lower()
        in DEVICE_LOCAL_ERROR_TYPES
        for node in nested
    )
    failed_device_gate = any(
        (
            isinstance(node.get("dispatch_validation"), dict)
            and str(node["dispatch_validation"].get("status") or "")
            .strip()
            .lower()
            == "failed"
        )
        or (
            isinstance(node.get("quantity_audit"), dict)
            and str(node["quantity_audit"].get("status") or "")
            .strip()
            .lower()
            in {"failed", "human_review_required"}
        )
        or (
            isinstance(node.get("recipe_materialization"), dict)
            and str(node["recipe_materialization"].get("status") or "")
            .strip()
            .lower()
            == "failed"
        )
        for node in nested
    )

    # Human intervention is the safest interpretation of any explicit human
    # marker, including a malformed package that also claims success or a
    # terminal condition.
    if (
        status == "manual_required"
        or route == "human"
        or feedback_type == "human_review_required"
        or scope in {"human", "human_review", "human_review_required"}
    ):
        return "human"
    if (
        status == "terminal_unmappable"
        or route == "terminal"
        or feedback_type == "terminal_unmappable"
        or scope in {"terminal", "terminal_unmappable"}
    ):
        return "terminal"
    if (
        route == "device"
        or scope == "device"
        or scope in DEVICE_LOCAL_FAILURE_SCOPES
        or scope.startswith("device_")
        or feedback_type in DEVICE_LOCAL_FEEDBACK_TYPES
        or feedback_type.startswith("device_")
        or failed_device_gate
    ):
        return "device"
    if canonical_success_envelope(package):
        return "success"
    if (
        not feasibility_accepted
        and not nested_device_scope
        and not nested_local_route
        and not nested_local_feedback
        and not nested_local_error
        and route == "research"
        and feedback_type == RESEARCH_FEEDBACK_TYPE
        and scope == RESEARCH_FAILURE_SCOPE
    ):
        return "research"
    # Unknown or internally contradictory legacy packages stay at Device.
    # Campaign termination is reserved for the explicit V2 terminal contract.
    return "device"


def adjustment_requires_scientific_review(value: Any) -> bool:
    """Infer scientific changes independently of a model-supplied kind label."""

    if not isinstance(value, dict):
        return False
    if value.get("requires_scientific_review") is True:
        return True
    kind = str(
        value.get("kind")
        or value.get("change_type")
        or value.get("adjustment_type")
        or ""
    ).strip().lower()
    if kind in SCIENTIFIC_QUANTITY_CHANGE_KINDS:
        return True

    before = value.get("before")
    after = value.get("after")

    def canonical_quantity(raw: Any) -> tuple[Optional[float], str]:
        if not isinstance(raw, dict):
            return None, ""
        unit = str(raw.get("unit") or raw.get("单位") or "").strip().lower()
        number = _first_nonempty(
            raw.get("total"),
            raw.get("value"),
            raw.get("amount"),
            raw.get("quantity"),
            raw.get("数值"),
            raw.get("数量"),
        )
        if number in (None, "", [], {}):
            aliquots = raw.get("aliquots")
            if isinstance(aliquots, list) and aliquots:
                try:
                    number = sum(float(item) for item in aliquots)
                except (TypeError, ValueError):
                    number = None
        try:
            numeric = float(number)
        except (TypeError, ValueError):
            return None, ""
        unit_table = {
            "mol": ("substance", 1.0),
            "mmol": ("substance", 1e-3),
            "umol": ("substance", 1e-6),
            "µmol": ("substance", 1e-6),
            "μmol": ("substance", 1e-6),
            "g": ("mass", 1.0),
            "mg": ("mass", 1e-3),
            "ug": ("mass", 1e-6),
            "µg": ("mass", 1e-6),
            "μg": ("mass", 1e-6),
            "l": ("volume", 1.0),
            "ml": ("volume", 1e-3),
            "ul": ("volume", 1e-6),
            "µl": ("volume", 1e-6),
            "μl": ("volume", 1e-6),
            "m": ("concentration", 1.0),
            "mm": ("concentration", 1e-3),
            "um": ("concentration", 1e-6),
            "µm": ("concentration", 1e-6),
            "μm": ("concentration", 1e-6),
        }
        dimension_scale = unit_table.get(unit)
        if dimension_scale is None:
            return None, ""
        dimension, scale = dimension_scale
        return numeric * scale, dimension

    before_quantity, before_dimension = canonical_quantity(before)
    after_quantity, after_dimension = canonical_quantity(after)
    if (
        before_quantity is not None
        and after_quantity is not None
        and before_dimension == after_dimension
        and abs(before_quantity - after_quantity)
        > max(1e-12, abs(before_quantity) * 1e-9)
    ):
        # A kind label such as ``device_operational`` or ``split_transfer``
        # cannot hide a changed amount/concentration.  Mechanical splits only
        # remain review-free when their canonical total is conserved.
        return True

    field_text = " ".join(
        str(value.get(key) or "")
        for key in ("field", "parameter", "reason", "description", "calculation")
    )
    sensitive_field = re.search(
        r"浓度|concentration|摩尔比|molar\s*ratio|stoichiometr|"
        r"(?:单批|实验|反应|配方|名义).{0,12}(?:总量|用量|剂量|amount)|"
        r"(?:总量|用量|剂量|amount).{0,12}(?:改变|增加|减少|change|scale)",
        field_text,
        re.I,
    )
    if sensitive_field and before not in (None, "") and after not in (None, ""):
        return _canonical_json(before) != _canonical_json(after)

    for side_before, side_after in (
        (before, after),
        (value.get("previous"), value.get("updated")),
    ):
        if not isinstance(side_before, dict) or not isinstance(side_after, dict):
            continue
        for key in set(side_before) & set(side_after):
            if re.search(
                r"concentration|molar_ratio|stoichiometr|total_amount|"
                r"single_batch_amount|per_batch_quantity|浓度|摩尔比|总量|单批",
                str(key),
                re.I,
            ) and _canonical_json(side_before[key]) != _canonical_json(
                side_after[key]
            ):
                return True

    blob = _canonical_json(value)
    return bool(
        re.search(
            r"新增.{0,12}(?:完整)?批|复制.{0,12}(?:完整)?批|"
            r"replicat(?:e|ed|ion).{0,12}batch|new\s+batch|"
            r"(?:浓度|concentration|摩尔比|molar\s*ratio|stoichiometr).{0,24}"
            r"(?:改变|增加|减少|from|to|->|→)",
            blob,
            re.I,
        )
    )


def package_requires_review(package: Dict[str, Any]) -> bool:
    """A success package that carries approximated/derived adaptations must be
    scientifically reviewed before it may cross the real-lab boundary."""
    if package.get("requires_scientific_review"):
        return True
    workflow_json = package.get("workflow_json")
    adaptations = []
    if isinstance(workflow_json, dict):
        adaptations = workflow_json.get("temporal_adaptations") or []
    if not isinstance(adaptations, list):
        adaptations = []
    top_level = package.get("temporal_adaptations")
    if isinstance(top_level, list):
        adaptations = list(adaptations) + top_level
    quantity_adjustments = package.get("quantity_adjustments") or []
    if not isinstance(quantity_adjustments, list):
        quantity_adjustments = []
    if isinstance(workflow_json, dict):
        nested = workflow_json.get("quantity_adjustments") or []
        if isinstance(nested, list):
            quantity_adjustments = list(quantity_adjustments) + nested
    return any(
        (
            isinstance(item, dict)
            and item.get("requires_scientific_review")
        )
        or adjustment_requires_scientific_review(item)
        for item in list(adaptations) + list(quantity_adjustments)
    )


def load_review_approval(iteration_dir: Path) -> Optional[Dict[str, Any]]:
    """Read ``review_approval.json``; returns the dict only when approved."""
    approval_path = iteration_dir / APPROVAL_FILENAME
    if not approval_path.exists():
        return None
    try:
        data = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("approved") is True:
        return data
    return None


def feasibility_blocker_keys(package: Dict[str, Any]) -> Set[str]:
    """Stable blocker identities used for *repeat* deadlock detection.

    A campaign is still making progress when Device surfaces a different class
    of problem after Research repairs the prior one.  Only overlapping blocker
    identities should advance the deadlock streak.
    """
    error_package = (
        package.get("error_package")
        if isinstance(package.get("error_package"), dict)
        else {}
    )
    keys: Set[str] = set()
    structured = error_package.get("structured_errors")
    for item in structured if isinstance(structured, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("error_code") or item.get("code") or "").strip()
        # Parser placeholders are not blocker identities. Treating every new
        # reviewer finding as ``code:unparsed`` makes unrelated, progressively
        # repaired errors look like the same deadlock and stops a 12-iteration
        # campaign after only three attempts.
        if code and code.lower() not in {"unparsed", "unknown", "unspecified"}:
            keys.add(f"code:{code.lower()}")

    constraints = error_package.get("blocking_constraints")
    for raw in constraints if isinstance(constraints, list) else []:
        text = str(raw).strip()
        if not text:
            continue
        codes = re.findall(r"[（(]([A-Z][A-Z0-9_]{2,})[)）]", text)
        if codes:
            keys.update(f"code:{code.lower()}" for code in codes)
            continue
        normalized = re.sub(r"\d+(?:\.\d+)?", "#", text.lower())
        normalized = re.sub(r"\s+", "", normalized)
        keys.add(f"constraint:{normalized[:240]}")

    if not keys:
        error_type = str(error_package.get("type") or "unspecified").strip().lower()
        keys.add(f"type:{error_type}")
    return keys


def is_transient_device_internal_error(package: Dict[str, Any]) -> bool:
    """True only for retryable provider/runtime transport failures.

    A gateway deadline is not evidence that the chemistry or workstation plan
    is invalid, and it must not be sent to Research as a feasibility blocker.
    Schema, parsing, and implementation errors remain terminal device errors.
    """

    if str(package.get("feedback_type", "")).strip() != "device_internal_error":
        return False
    error_package = (
        package.get("error_package")
        if isinstance(package.get("error_package"), dict)
        else {}
    )
    text = json.dumps(error_package, ensure_ascii=False).lower()
    markers = (
        "gateway_deadline",
        "stream disconnected",
        "connection reset",
        "temporarily unavailable",
        "timed out",
        "timeout",
    )
    return any(marker in text for marker in markers)


@dataclass
class CampaignConfig:
    query: str
    campaign_id: str = ""
    requested_contract_version: str = "v2"
    references: List[str] = field(default_factory=list)
    max_iterations: int = MAX_CAMPAIGN_ITERATIONS
    feasibility_deadlock_limit: int = 3
    # Transport adapters own gateway retries.  Re-running the complete Device
    # mapping here would multiply those requests and repeat already-completed
    # LLM work, so same-layer workflow retries are opt-in.
    transient_device_retry_limit: int = 0
    campaigns_root: Optional[Path] = None
    research_args: List[str] = field(default_factory=list)
    device_args: List[str] = field(default_factory=list)
    resume_device_repair: Optional[Path] = None
    device_plan_override: Optional[Path] = None
    forward_only: bool = False

    def __post_init__(self) -> None:
        if self.requested_contract_version not in {"v1", "v2"}:
            raise ValueError(
                "requested_contract_version must be 'v1' or 'v2'"
            )
        for label, values in (
            ("research_args", self.research_args),
            ("device_args", self.device_args),
        ):
            contract_versions = _contract_versions_from_args(values)
            if any(
                version != self.requested_contract_version
                for version in contract_versions
            ):
                raise ValueError(
                    f"{label} --contract-version conflicts with "
                    "requested_contract_version"
                )
        if not 1 <= self.max_iterations <= MAX_CAMPAIGN_ITERATIONS:
            raise ValueError(
                "max_iterations must be between 1 and "
                f"{MAX_CAMPAIGN_ITERATIONS}, got {self.max_iterations}"
            )
        if self.transient_device_retry_limit < 0:
            raise ValueError("transient_device_retry_limit must be >= 0")
        if bool(self.resume_device_repair) != bool(self.device_plan_override):
            raise ValueError(
                "resume_device_repair and device_plan_override must be provided together"
            )


@dataclass
class CampaignResult:
    campaign_id: str
    stop_reason: str
    iterations_run: int
    goal_reached: bool
    campaign_dir: str
    final_state_path: str
    final_report_path: str


class CampaignRunner:
    """Drive one campaign end to end with pluggable step functions."""

    def __init__(
        self,
        config: CampaignConfig,
        adapter: BaseExecutionAdapter,
        research_step: ResearchStepFn | None = None,
        device_step: DeviceStepFn | None = None,
    ) -> None:
        self.config = config
        if self.config.resume_device_repair and not self.config.campaign_id.strip():
            try:
                request_data = json.loads(
                    Path(self.config.resume_device_repair)
                    .expanduser()
                    .read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                request_data = {}
            if isinstance(request_data, dict):
                self.config.campaign_id = str(
                    request_data.get("campaign_id") or ""
                ).strip()
        if not self.config.campaign_id.strip():
            self.config.campaign_id = generate_campaign_id(config.query)
        self.campaigns_root = (
            Path(config.campaigns_root).expanduser().resolve()
            if config.campaigns_root
            else REPO_ROOT / "campaigns"
        )
        self.campaign_dir = self.campaigns_root / self.config.campaign_id
        self.adapter = adapter
        self._research_step = research_step or self._research_step_subprocess
        self._device_step = device_step or self._device_step_subprocess
        self._trace: List[Dict[str, Any]] = []
        # Issue 4: campaign-level cumulative device constraints for the
        # deadlock report (every constraint the device layer ever returned).
        self._campaign_constraints: List[str] = []

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    def run(self) -> CampaignResult:
        if self.config.resume_device_repair and self.config.device_plan_override:
            return self._run_device_repair_resume(
                Path(self.config.resume_device_repair).expanduser().resolve(),
                Path(self.config.device_plan_override).expanduser().resolve(),
            )
        self.campaign_dir.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now().isoformat(timespec="seconds")
        self._log(
            f"campaign {self.config.campaign_id} started: "
            f"max_iterations={self.config.max_iterations}, "
            f"adapter={self.adapter.name}, "
            f"references={len(self.config.references)}"
        )

        bootstrap_dir = self._iteration_dir(0)
        state = self._research_step(
            "bootstrap",
            query=self.config.query,
            previous_state_path=None,
            payload=None,
            iteration_dir=bootstrap_dir,
            references=list(self.config.references),
        )
        state_path = bootstrap_dir / "research_state.json"
        self._trace.append(
            {
                "iteration": 0,
                "phase": "research_bootstrap",
                "status": state.get("status", ""),
                "macro_steps": len(state.get("macro_plan") or []),
            }
        )

        stop_reason = STOP_MAX_ITERATIONS
        goal_reached = False
        iterations_run = 0
        last_package: Dict[str, Any] = {}

        if state.get("status") == "manual_required":
            stop_reason = STOP_MANUAL_REQUIRED
            self._log("bootstrap requires manual intervention; stopping")
        elif state.get("status") != "completed":
            stop_reason = STOP_RESEARCH_ERROR
            self._log(f"bootstrap ended with status={state.get('status')}; stopping")
        else:
            consecutive_feasibility = 0
            previous_feasibility_keys: Set[str] = set()
            for iteration in range(1, self.config.max_iterations + 1):
                iterations_run = iteration
                iteration_dir = self._iteration_dir(iteration)
                self._log(f"iteration {iteration}: device mapping starts")

                package = self._device_step(state_path, iteration_dir)
                package_status = str(package.get("status", "")).strip()
                last_package = package
                self._write_human_readable(iteration_dir, state, package)
                route = feedback_route(package)

                transient_retry_count = 0
                while (
                    route == "device"
                    and is_transient_device_internal_error(package)
                    and transient_retry_count < self.config.transient_device_retry_limit
                ):
                    transient_retry_count += 1
                    self._trace.append(
                        {
                            "iteration": iteration,
                            "phase": "device",
                            "status": "transient_device_internal_error",
                            "same_layer_retry": transient_retry_count,
                        }
                    )
                    self._log(
                        f"iteration {iteration}: transient Device/API error; "
                        "retrying inside the Device layer without consuming a "
                        "campaign iteration "
                        f"({transient_retry_count}/"
                        f"{self.config.transient_device_retry_limit})"
                    )
                    package = self._device_step(state_path, iteration_dir)
                    package_status = str(package.get("status", "")).strip()
                    last_package = package
                    self._write_human_readable(iteration_dir, state, package)
                    route = feedback_route(package)

                if route == "device" and is_transient_device_internal_error(package):
                    stop_reason = STOP_DEVICE_ERROR
                    self._log(
                        f"iteration {iteration}: transient Device/API retry limit "
                        "exhausted; stopping"
                    )
                    break

                if route == "terminal":
                    stop_reason = STOP_TERMINAL_UNMAPPABLE
                    terminal_ids = _as_dict(
                        package.get("device_workflow_package_v2")
                    ).get("terminal_macro_step_ids", [])
                    self._trace.append(
                        {
                            "iteration": iteration,
                            "phase": "device",
                            "status": "terminal_unmappable",
                            "macro_step_ids": terminal_ids,
                            "research_invoked": False,
                        }
                    )
                    self._log(
                        f"iteration {iteration}: confirmed workstation capability gap "
                        f"for macro_step_ids={terminal_ids}; campaign terminated"
                    )
                    break

                if route == "human":
                    stop_reason = STOP_MANUAL_REQUIRED
                    self._trace.append(
                        {
                            "iteration": iteration,
                            "phase": "device",
                            "status": "human_review_required",
                            "failure_scope": str(package.get("failure_scope", "")),
                        }
                    )
                    if package_feasibility_accepted(package):
                        self._write_device_repair_artifacts(
                            iteration_dir,
                            state_path,
                            package,
                            campaign_iteration=iteration,
                        )
                        self._log(
                            f"iteration {iteration}: Device-local repair exhausted or "
                            "requires scientific judgement; generated a resumable "
                            "human Device repair request without entering Research B2"
                        )
                    else:
                        self._write_unverifiable_review_request(
                            iteration_dir,
                            _as_dict(package.get("error_package")),
                        )
                        self._log(
                            f"iteration {iteration}: Stage-1 feasibility was not "
                            "accepted; requesting condition review without creating "
                            "a Device-plan override"
                        )
                    break

                if route == "research":
                    error_package = (
                        package.get("error_package")
                        if isinstance(package.get("error_package"), dict)
                        else {}
                    )
                    current_feasibility_keys = feasibility_blocker_keys(package)
                    repeated_keys = current_feasibility_keys & previous_feasibility_keys
                    consecutive_feasibility = (
                        consecutive_feasibility + 1 if repeated_keys else 1
                    )
                    previous_feasibility_keys = current_feasibility_keys
                    self._accumulate_campaign_constraints(package)
                    error_type = str(error_package.get("type", "")) or "unspecified"
                    self._trace.append(
                        {
                            "iteration": iteration,
                            "phase": "device",
                            "status": "feasibility_error",
                            "error_type": error_type,
                            "consecutive": consecutive_feasibility,
                            "blocker_keys": sorted(current_feasibility_keys),
                            "repeated_blocker_keys": sorted(repeated_keys),
                        }
                    )
                    self._log(
                        f"iteration {iteration}: device returned feasibility_error "
                        f"[{error_type}] "
                        f"({consecutive_feasibility}/{self.config.feasibility_deadlock_limit})"
                    )
                    if consecutive_feasibility >= self.config.feasibility_deadlock_limit:
                        stop_reason = STOP_FEASIBILITY_DEADLOCK
                        self._log(
                            "feasibility deadlock detected; handing over to manual review. "
                            "cumulative unmet device constraints: "
                            + (
                                "; ".join(self._campaign_constraints)
                                if self._campaign_constraints
                                else "(none captured)"
                            )
                        )
                        break
                    payload = self._feasibility_payload(package)
                elif route == "success":
                    consecutive_feasibility = 0
                    previous_feasibility_keys = set()
                    if self.config.forward_only:
                        stop_reason = self._forward_only_stop_reason(
                            package,
                            iteration_dir,
                            iteration=iteration,
                            phase="device",
                        )
                        break
                    needs_review = package_requires_review(package)
                    if needs_review and self.adapter.real_lab_boundary:
                        approval = load_review_approval(iteration_dir)
                        if approval is None:
                            stop_reason = STOP_REVIEW_REQUIRED
                            self._write_review_request(iteration_dir, package)
                            self._trace.append(
                                {
                                    "iteration": iteration,
                                    "phase": "review_gate",
                                    "status": "blocked_awaiting_scientific_review",
                                }
                            )
                            self._log(
                                f"iteration {iteration}: workflow requires scientific "
                                "review before dispatch; blocking execution "
                                f"(write {APPROVAL_FILENAME} with approved=true to release)"
                            )
                            break
                        self._trace.append(
                            {
                                "iteration": iteration,
                                "phase": "review_gate",
                                "status": "approved",
                                "approver": str(approval.get("approver", "")),
                            }
                        )
                        self._log(
                            f"iteration {iteration}: scientific review approved by "
                            f"{approval.get('approver', 'unknown')}; dispatching"
                        )
                    elif needs_review:
                        self._trace.append(
                            {
                                "iteration": iteration,
                                "phase": "review_gate",
                                "status": "pending_review_simulated_execution",
                            }
                        )
                        self._log(
                            f"iteration {iteration}: workflow flagged for scientific "
                            f"review; proceeding on simulated adapter `{self.adapter.name}` "
                            "(review still required before any real dispatch)"
                        )
                    try:
                        observation = self._execute_checked_package(package, iteration_dir)
                    except DispatchCheckBlockedError:
                        stop_reason = STOP_DEVICE_ERROR
                        break
                    observation_path = iteration_dir / "observation_in.json"
                    observation_path.write_text(
                        json.dumps(observation, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    summary_preview = str(observation.get("summary", ""))[:80]
                    self._trace.append(
                        {
                            "iteration": iteration,
                            "phase": "execution",
                            "status": "observation_received",
                            "summary": summary_preview,
                        }
                    )
                    self._log(
                        f"iteration {iteration}: observation received "
                        f"({summary_preview or 'no summary'})"
                    )
                    payload = {"observation": observation}
                else:
                    stop_reason = STOP_DEVICE_ERROR
                    self._trace.append(
                        {
                            "iteration": iteration,
                            "phase": "device",
                            "status": package_status or "unknown",
                            "feedback_route": route,
                            "failure_scope": str(package.get("failure_scope", "")),
                        }
                    )
                    self._log(
                        f"iteration {iteration}: device ended with "
                        f"status={package_status or 'unknown'}, route={route}; "
                        "stopping without invoking Research"
                    )
                    break

                state = self._research_step(
                    "new_observation",
                    query="",
                    previous_state_path=state_path,
                    payload=payload,
                    iteration_dir=iteration_dir,
                    references=[],
                )
                state_path = iteration_dir / "research_state.json"
                macro_steps = len(state.get("macro_plan") or [])
                self._trace.append(
                    {
                        "iteration": iteration,
                        "phase": "research",
                        "status": state.get("status", ""),
                        "macro_steps": macro_steps,
                        "repair_path": state.get("post_observation_repair_path", ""),
                    }
                )

                if state.get("status") == "manual_required":
                    stop_reason = STOP_MANUAL_REQUIRED
                    self._log(f"iteration {iteration}: research requires manual takeover")
                    break
                if state.get("status") != "completed":
                    stop_reason = STOP_RESEARCH_ERROR
                    self._log(
                        f"iteration {iteration}: research ended with "
                        f"status={state.get('status')}; stopping"
                    )
                    break
                if macro_steps == 0:
                    stop_reason = STOP_GOAL_REACHED
                    goal_reached = True
                    self._log(
                        f"iteration {iteration}: stage route closed with no further "
                        "macro plan; goal reached"
                    )
                    break

        finished_at = datetime.now().isoformat(timespec="seconds")
        self._write_human_readable(
            self.campaign_dir,
            state,
            last_package,
            campaign_meta={
                "campaign_id": self.config.campaign_id,
                "stop_reason": stop_reason,
                "goal_reached": goal_reached,
                "iterations_run": iterations_run,
            },
        )
        report_path = self._write_final_report(
            stop_reason=stop_reason,
            goal_reached=goal_reached,
            iterations_run=iterations_run,
            final_state_path=state_path,
            started_at=started_at,
            finished_at=finished_at,
        )
        self._log(f"campaign finished: stop_reason={stop_reason}")
        return CampaignResult(
            campaign_id=self.config.campaign_id,
            stop_reason=stop_reason,
            iterations_run=iterations_run,
            goal_reached=goal_reached,
            campaign_dir=str(self.campaign_dir),
            final_state_path=str(state_path),
            final_report_path=str(report_path),
        )

    def _run_device_repair_resume(
        self,
        request_path: Path,
        override_path: Path,
    ) -> CampaignResult:
        """Resume one frozen Research plan directly at the Device layer.

        This path deliberately has no Research bootstrap and does not increment
        the stored Research↔Device campaign iteration for the repair attempt.
        """

        self.campaign_dir.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now().isoformat(timespec="seconds")
        summary_path = self.campaign_dir / "campaign_summary.json"
        if summary_path.exists():
            try:
                prior_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prior_summary = {}
            if isinstance(prior_summary, dict):
                prior_trace = prior_summary.get("trace")
                if isinstance(prior_trace, list):
                    self._trace.extend(
                        item for item in prior_trace if isinstance(item, dict)
                    )
                prior_constraints = prior_summary.get(
                    "cumulative_device_constraints"
                )
                if isinstance(prior_constraints, list):
                    for item in prior_constraints:
                        text = str(item).strip()
                        if text and text not in self._campaign_constraints:
                            self._campaign_constraints.append(text)
                started_at = str(prior_summary.get("started_at") or started_at)
        request, override, state_path, state = self._validate_device_repair_resume(
            request_path,
            override_path,
        )
        campaign_iteration = int(request.get("campaign_iteration") or 0)
        resume_dir = self._next_device_repair_resume_dir(campaign_iteration)
        self._log(
            "resuming frozen Device repair without Research bootstrap: "
            f"request_id={request.get('request_id', '')}, "
            f"campaign_iteration={campaign_iteration}"
        )
        self._trace.append(
            {
                "iteration": campaign_iteration,
                "phase": "device_repair_resume",
                "status": "override_validated",
                "request_id": request.get("request_id", ""),
                "campaign_iteration_incremented": False,
            }
        )

        package = self._device_step(
            state_path,
            resume_dir,
            device_plan_override_path=override_path,
            prior_repair_request_path=request_path,
        )
        _validated_package_contract_version(
            package,
            str(request.get("contract_version") or ""),
            label="Device repair resume output",
        )
        if canonical_success_envelope(package):
            self._validate_resumed_success_certificate(
                package,
                request,
                override,
            )
        package_path = resume_dir / "device_package.json"
        if not package_path.exists():
            package_path.write_text(
                json.dumps(package, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        self._write_human_readable(resume_dir, state, package)
        route = feedback_route(package)
        stop_reason = STOP_DEVICE_ERROR
        goal_reached = False
        final_state_path = state_path
        iterations_run = campaign_iteration

        if route == "human":
            stop_reason = STOP_MANUAL_REQUIRED
            if package_feasibility_accepted(package):
                self._write_device_repair_artifacts(
                    resume_dir,
                    state_path,
                    package,
                    campaign_iteration=campaign_iteration,
                    parent_request_id=str(request.get("request_id") or ""),
                    repair_generation=(
                        int(request.get("repair_generation") or 1) + 1
                    ),
                )
            else:
                self._write_unverifiable_review_request(
                    resume_dir,
                    _as_dict(package.get("error_package")),
                )
            self._trace.append(
                {
                    "iteration": campaign_iteration,
                    "phase": "device_repair_resume",
                    "status": "human_review_required_again",
                    "feasibility_accepted": package_feasibility_accepted(
                        package
                    ),
                }
            )
        elif route == "terminal":
            stop_reason = STOP_TERMINAL_UNMAPPABLE
            self._trace.append(
                {
                    "iteration": campaign_iteration,
                    "phase": "device_repair_resume",
                    "status": "terminal_unmappable",
                    "research_invoked": False,
                }
            )
        elif route == "success":
            if self.config.forward_only:
                stop_reason = self._forward_only_stop_reason(
                    package,
                    resume_dir,
                    iteration=campaign_iteration,
                    phase="device_repair_resume",
                    allowed_certificate_scopes={
                        "accepted_device_plan_revision"
                    },
                )
            else:
                needs_review = package_requires_review(package)
                if needs_review and self.adapter.real_lab_boundary:
                    approval = load_review_approval(resume_dir)
                    if approval is None:
                        stop_reason = STOP_REVIEW_REQUIRED
                        self._write_review_request(resume_dir, package)
                    else:
                        stop_reason, goal_reached, state, final_state_path = (
                            self._execute_resumed_package(
                                package,
                                state,
                                state_path,
                                resume_dir,
                                campaign_iteration,
                            )
                        )
                else:
                    stop_reason, goal_reached, state, final_state_path = (
                        self._execute_resumed_package(
                            package,
                            state,
                            state_path,
                            resume_dir,
                            campaign_iteration,
                        )
                    )
        else:
            # A feasibility certificate already exists for every repair
            # request. Even a malformed package claiming route="research" is
            # therefore fail-closed at Device and can never reach Research B2.
            stop_reason = STOP_DEVICE_ERROR
            self._trace.append(
                {
                    "iteration": campaign_iteration,
                    "phase": "device_repair_resume",
                    "status": str(package.get("status") or "failed"),
                    "feedback_route": route,
                    "research_invoked": False,
                }
            )

        if (
            stop_reason == STOP_MAX_ITERATIONS
            and state.get("status") == "completed"
            and state.get("macro_plan")
            and campaign_iteration < self.config.max_iterations
        ):
            (
                stop_reason,
                goal_reached,
                iterations_run,
                state,
                final_state_path,
                continuation_package,
            ) = self._continue_campaign_from_state(
                state,
                final_state_path,
                start_iteration=campaign_iteration + 1,
            )
            if continuation_package:
                package = continuation_package

        finished_at = datetime.now().isoformat(timespec="seconds")
        self._write_human_readable(
            self.campaign_dir,
            state,
            package,
            campaign_meta={
                "campaign_id": self.config.campaign_id,
                "stop_reason": stop_reason,
                "goal_reached": goal_reached,
                "iterations_run": iterations_run,
                "device_repair_resume": True,
            },
        )
        report_path = self._write_final_report(
            stop_reason=stop_reason,
            goal_reached=goal_reached,
            iterations_run=iterations_run,
            final_state_path=final_state_path,
            started_at=started_at,
            finished_at=finished_at,
        )
        self._log(f"Device repair resume finished: stop_reason={stop_reason}")
        return CampaignResult(
            campaign_id=self.config.campaign_id,
            stop_reason=stop_reason,
            iterations_run=iterations_run,
            goal_reached=goal_reached,
            campaign_dir=str(self.campaign_dir),
            final_state_path=str(final_state_path),
            final_report_path=str(report_path),
        )

    def _execute_resumed_package(
        self,
        package: Dict[str, Any],
        state: Dict[str, Any],
        state_path: Path,
        resume_dir: Path,
        campaign_iteration: int,
    ) -> tuple[str, bool, Dict[str, Any], Path]:
        try:
            observation = self._execute_checked_package(
                package,
                resume_dir,
                allowed_certificate_scopes={
                    "accepted_device_plan_revision"
                },
            )
        except DispatchCheckBlockedError:
            return STOP_DEVICE_ERROR, False, state, state_path
        (resume_dir / "observation_in.json").write_text(
            json.dumps(observation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        new_state = self._research_step(
            "new_observation",
            query="",
            previous_state_path=state_path,
            payload={"observation": observation},
            iteration_dir=resume_dir,
            references=[],
        )
        new_state_path = resume_dir / "research_state.json"
        macro_steps = len(new_state.get("macro_plan") or [])
        self._trace.append(
            {
                "iteration": campaign_iteration,
                "phase": "research_after_device_repair",
                "status": new_state.get("status", ""),
                "macro_steps": macro_steps,
                "campaign_iteration_incremented": False,
            }
        )
        if new_state.get("status") == "manual_required":
            return STOP_MANUAL_REQUIRED, False, new_state, new_state_path
        if new_state.get("status") != "completed":
            return STOP_RESEARCH_ERROR, False, new_state, new_state_path
        if macro_steps == 0:
            return STOP_GOAL_REACHED, True, new_state, new_state_path

        # The repaired macro action itself did not consume another campaign
        # iteration. A later macro action emitted by Research is allowed to
        # continue at ``campaign_iteration + 1``.
        return STOP_MAX_ITERATIONS, False, new_state, new_state_path

    def _continue_campaign_from_state(
        self,
        state: Dict[str, Any],
        state_path: Path,
        *,
        start_iteration: int,
    ) -> tuple[str, bool, int, Dict[str, Any], Path, Dict[str, Any]]:
        """Continue later macro actions after a same-iteration Device resume."""

        stop_reason = STOP_MAX_ITERATIONS
        goal_reached = False
        iterations_run = max(0, start_iteration - 1)
        last_package: Dict[str, Any] = {}
        consecutive_feasibility = 0
        previous_feasibility_keys: Set[str] = set()

        for iteration in range(start_iteration, self.config.max_iterations + 1):
            iterations_run = iteration
            iteration_dir = self._iteration_dir(iteration)
            self._log(f"iteration {iteration}: device mapping starts after repair resume")
            package = self._device_step(state_path, iteration_dir)
            last_package = package
            self._write_human_readable(iteration_dir, state, package)
            route = feedback_route(package)

            transient_retry_count = 0
            while (
                route == "device"
                and is_transient_device_internal_error(package)
                and transient_retry_count < self.config.transient_device_retry_limit
            ):
                transient_retry_count += 1
                package = self._device_step(state_path, iteration_dir)
                last_package = package
                self._write_human_readable(iteration_dir, state, package)
                route = feedback_route(package)
            if route == "device" and is_transient_device_internal_error(package):
                stop_reason = STOP_DEVICE_ERROR
                break

            if route == "terminal":
                stop_reason = STOP_TERMINAL_UNMAPPABLE
                self._trace.append(
                    {
                        "iteration": iteration,
                        "phase": "device",
                        "status": "terminal_unmappable",
                        "research_invoked": False,
                    }
                )
                break

            if route == "human":
                stop_reason = STOP_MANUAL_REQUIRED
                if package_feasibility_accepted(package):
                    self._write_device_repair_artifacts(
                        iteration_dir,
                        state_path,
                        package,
                        campaign_iteration=iteration,
                    )
                else:
                    self._write_unverifiable_review_request(
                        iteration_dir,
                        _as_dict(package.get("error_package")),
                    )
                break
            if route == "research":
                current_keys = feasibility_blocker_keys(package)
                repeated = current_keys & previous_feasibility_keys
                consecutive_feasibility = (
                    consecutive_feasibility + 1 if repeated else 1
                )
                previous_feasibility_keys = current_keys
                self._accumulate_campaign_constraints(package)
                if consecutive_feasibility >= self.config.feasibility_deadlock_limit:
                    stop_reason = STOP_FEASIBILITY_DEADLOCK
                    break
                payload = self._feasibility_payload(package)
            elif route == "success":
                consecutive_feasibility = 0
                previous_feasibility_keys = set()
                if self.config.forward_only:
                    stop_reason = self._forward_only_stop_reason(
                        package,
                        iteration_dir,
                        iteration=iteration,
                        phase="device",
                    )
                    break
                if package_requires_review(package) and self.adapter.real_lab_boundary:
                    if load_review_approval(iteration_dir) is None:
                        stop_reason = STOP_REVIEW_REQUIRED
                        self._write_review_request(iteration_dir, package)
                        break
                try:
                    observation = self._execute_checked_package(package, iteration_dir)
                except DispatchCheckBlockedError:
                    stop_reason = STOP_DEVICE_ERROR
                    break
                (iteration_dir / "observation_in.json").write_text(
                    json.dumps(observation, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                payload = {"observation": observation}
            else:
                stop_reason = STOP_DEVICE_ERROR
                break

            state = self._research_step(
                "new_observation",
                query="",
                previous_state_path=state_path,
                payload=payload,
                iteration_dir=iteration_dir,
                references=[],
            )
            state_path = iteration_dir / "research_state.json"
            macro_steps = len(state.get("macro_plan") or [])
            self._trace.append(
                {
                    "iteration": iteration,
                    "phase": "research",
                    "status": state.get("status", ""),
                    "macro_steps": macro_steps,
                }
            )
            if state.get("status") == "manual_required":
                stop_reason = STOP_MANUAL_REQUIRED
                break
            if state.get("status") != "completed":
                stop_reason = STOP_RESEARCH_ERROR
                break
            if macro_steps == 0:
                stop_reason = STOP_GOAL_REACHED
                goal_reached = True
                break

        return (
            stop_reason,
            goal_reached,
            iterations_run,
            state,
            state_path,
            last_package,
        )

    @staticmethod
    def _validate_v2_success_certificate_core(
        package: Dict[str, Any],
        *,
        label: str = "successful V2 Device output",
        allowed_scopes: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """Validate the certificate facts shared by normal and resume success.

        Lineage is deliberately excluded: only a manual repair successor has a
        predecessor/request/authority scope.  Every V2 success, however, must
        carry the same current certificate version and bind the exact accepted
        Device Plan that is about to cross the dispatch boundary.
        """

        if package.get("feasibility_accepted") is not True:
            raise DeviceRepairResumeError(
                f"{label} requires feasibility_accepted=true"
            )
        certificate = _as_dict(package.get("feasibility_certificate"))
        if certificate.get("accepted") is not True:
            raise DeviceRepairResumeError(
                f"{label} requires an accepted feasibility_certificate"
            )
        certificate_version = strict_feasibility_certificate_version(certificate)
        if (
            certificate_version in FULL_PLAN_CONTRACT_CERTIFICATE_VERSIONS
            and certificate_version != FEASIBILITY_CERTIFICATE_VERSION
        ):
            raise DeviceRepairResumeError(
                f"{label} carries obsolete full-contract feasibility_certificate "
                f"version {certificate_version}; version "
                f"{FEASIBILITY_CERTIFICATE_VERSION} expands the signed Device Plan "
                "scope with material execution sidecars, so the plan must be "
                "re-audited and re-issued"
            )
        if certificate_version != FEASIBILITY_CERTIFICATE_VERSION:
            raise DeviceRepairResumeError(
                f"{label} requires feasibility_certificate version "
                f"{FEASIBILITY_CERTIFICATE_VERSION}"
            )
        if str(certificate.get("contract_version") or "").strip() != "v2":
            raise DeviceRepairResumeError(
                f"{label} feasibility_certificate contract_version is invalid"
            )
        effective_scopes = (
            set(allowed_scopes)
            if allowed_scopes is not None
            else {"accepted_device_plan"}
        )
        if certificate.get("acceptance_scope") not in effective_scopes:
            raise DeviceRepairResumeError(
                f"{label} feasibility_certificate acceptance_scope is invalid"
            )

        protected = feasibility_certificate_protected_payload(certificate)
        protected_digest = certificate_stable_digest(protected)
        if certificate.get("protected_digest") != protected_digest:
            raise DeviceRepairResumeError(
                f"{label} feasibility_certificate protected_digest is invalid"
            )
        expected_certificate_id = feasibility_certificate_id(
            protected_digest=protected_digest,
            device_snapshot_id=str(certificate.get("device_snapshot_id") or ""),
            device_truth_sha256=str(certificate.get("device_truth_sha256") or ""),
        )
        if certificate.get("certificate_id") != expected_certificate_id:
            raise DeviceRepairResumeError(
                f"{label} feasibility_certificate certificate_id is invalid"
            )

        route_signature = str(certificate.get("route_signature") or "")
        if not route_signature or route_signature != str(
            certificate.get("research_plan_signature") or ""
        ):
            raise DeviceRepairResumeError(
                f"{label} feasibility_certificate route_signature is invalid"
            )
        expected_matrix_signature = certificate_stable_digest(
            certificate.get("sample_control_matrix", []),
            prefix="sample_matrix",
        )
        if certificate.get("sample_matrix_signature") != expected_matrix_signature:
            raise DeviceRepairResumeError(
                f"{label} feasibility_certificate sample_matrix_signature is invalid"
            )
        snapshot_signature = str(
            certificate.get("device_snapshot_signature") or ""
        )
        if not snapshot_signature or snapshot_signature != str(
            certificate.get("device_truth_sha256") or ""
        ):
            raise DeviceRepairResumeError(
                f"{label} feasibility_certificate device_snapshot_signature is invalid"
            )

        accepted_plan_digest = str(
            certificate.get("accepted_device_plan_contract_sha256") or ""
        )
        if not accepted_plan_digest or accepted_plan_digest != device_plan_contract_digest(
            package
        ):
            raise DeviceRepairResumeError(
                f"{label} does not match its feasibility_certificate plan digest"
            )
        expected_step_digest = certificate_stable_digest(
            package.get("device_plan", []),
            prefix="device_plan",
        )
        if certificate.get("accepted_device_plan_signature") != expected_step_digest:
            raise DeviceRepairResumeError(
                f"{label} does not match its feasibility_certificate device_plan signature"
            )
        return certificate

    @staticmethod
    def _validate_resumed_success_certificate(
        package: Dict[str, Any],
        request: Dict[str, Any],
        override: Dict[str, Any],
    ) -> None:
        """Require a signed current-version successor before resume execution."""

        if str(request.get("contract_version") or "").strip() != "v2":
            return

        predecessor = _as_dict(request.get("feasibility_certificate"))
        certificate = CampaignRunner._validate_v2_success_certificate_core(
            package,
            label="successful V2 Device repair output",
            allowed_scopes={"accepted_device_plan_revision"},
        )
        if certificate.get("acceptance_scope") != "accepted_device_plan_revision":
            raise DeviceRepairResumeError(
                "successor feasibility_certificate acceptance_scope is invalid"
            )

        predecessor_id = str(predecessor.get("certificate_id") or "")
        if not predecessor_id or certificate.get("supersedes_certificate_id") != predecessor_id:
            raise DeviceRepairResumeError(
                "successor feasibility_certificate does not supersede the frozen certificate"
            )
        request_id = str(request.get("request_id") or "")
        if not request_id or certificate.get("repair_request_id") != request_id:
            raise DeviceRepairResumeError(
                "successor feasibility_certificate repair_request_id is invalid"
            )
        if certificate.get("repair_authority") != "human_device_plan_override":
            raise DeviceRepairResumeError(
                "successor feasibility_certificate repair_authority is invalid"
            )

        frozen_fields = (
            "contract_version",
            "research_plan_signature",
            "target_materials",
            "reaction_route",
            "reagent_identity_and_order",
            "observation_points",
            "sample_control_matrix",
            "accepted_device_sample_control_matrix",
            "device_sample_ids",
            "semantic_analysis",
            "external_return_contracts",
            "device_snapshot_id",
            "device_truth_sha256",
        )
        for field_name in frozen_fields:
            if certificate.get(field_name) != predecessor.get(field_name):
                raise DeviceRepairResumeError(
                    "successor feasibility_certificate changes frozen field "
                    f"{field_name}"
                )
        before = device_plan_contract_view(request.get("last_device_plan"))
        after = device_plan_contract_view(package)
        expected_changed_fields = [
            key for key in before if before[key] != after[key]
        ]
        scope = _as_dict(certificate.get("authorized_change_scope"))
        if scope.get("changed_contract_fields") != expected_changed_fields:
            raise DeviceRepairResumeError(
                "successor feasibility_certificate changed_contract_fields is invalid"
            )
        expected_changes = (
            copy.deepcopy(override.get("changes"))
            if isinstance(override.get("changes"), list)
            else []
        )
        if scope.get("declared_changes") != expected_changes:
            raise DeviceRepairResumeError(
                "successor feasibility_certificate declared_changes is invalid"
            )
        expected_declarations = (
            copy.deepcopy(override.get("declarations"))
            if isinstance(override.get("declarations"), dict)
            else {}
        )
        if scope.get("declarations") != expected_declarations:
            raise DeviceRepairResumeError(
                "successor feasibility_certificate declarations scope is invalid"
            )

    def _next_device_repair_resume_dir(self, campaign_iteration: int) -> Path:
        prefix = f"iteration_{campaign_iteration:02d}_device_repair_resume_"
        existing: List[int] = []
        for path in self.campaign_dir.glob(prefix + "*"):
            try:
                existing.append(int(path.name.rsplit("_", 1)[-1]))
            except ValueError:
                continue
        path = self.campaign_dir / f"{prefix}{max(existing, default=0) + 1:02d}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    @staticmethod
    def _load_json_object(path: Path, label: str) -> Dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise DeviceRepairResumeError(f"cannot read {label}: {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise DeviceRepairResumeError(
                f"{label} is not valid JSON: {path}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise DeviceRepairResumeError(f"{label} must be a JSON object: {path}")
        return value

    def _validate_device_repair_resume(
        self,
        request_path: Path,
        override_path: Path,
    ) -> tuple[Dict[str, Any], Dict[str, Any], Path, Dict[str, Any]]:
        request = self._load_json_object(request_path, "device repair request")
        override = self._load_json_object(override_path, "device plan override")
        if request.get("schema_version") != "chem-device-repair-request/v1":
            raise DeviceRepairResumeError("unsupported device repair request schema")
        if override.get("schema_version") != "chem-device-plan-override/v1":
            raise DeviceRepairResumeError("unsupported device plan override schema")
        if str(request.get("campaign_id") or "") != self.config.campaign_id:
            raise DeviceRepairResumeError(
                "repair request campaign_id does not match the selected campaign"
            )
        request_contract = str(
            request.get("contract_version") or ""
        ).strip()
        request_resolution = _as_dict(request.get("contract_resolution"))
        override_contract = str(
            override.get("contract_version") or ""
        ).strip()
        if request_contract not in {"v1", "v2"}:
            raise DeviceRepairResumeError(
                "repair request lacks a valid frozen contract_version"
            )
        if (
            str(request_resolution.get("requested") or "").strip()
            != request_contract
            or str(request_resolution.get("effective") or "").strip()
            != request_contract
            or request_resolution.get("requested_matches_effective") is not True
        ):
            raise DeviceRepairResumeError(
                "repair request contract_resolution is missing or inconsistent"
            )
        if override_contract != request_contract:
            raise DeviceRepairResumeError(
                "override contract_version does not match repair request"
            )
        if request_contract != self.config.requested_contract_version:
            raise DeviceRepairResumeError(
                "repair request contract_version does not match the selected runtime"
            )

        certificate = _as_dict(request.get("feasibility_certificate"))
        if not certificate.get("accepted"):
            raise DeviceRepairResumeError(
                "Device repair resume requires an accepted feasibility_certificate"
            )
        certificate_contract = str(
            certificate.get("contract_version") or ""
        ).strip()
        if request_contract == "v2" and certificate_contract != "v2":
            raise DeviceRepairResumeError(
                "V2 feasibility_certificate lacks its frozen contract_version"
            )
        certificate_version = strict_feasibility_certificate_version(certificate)
        if (
            request_contract == "v2"
            and certificate_version in FULL_PLAN_CONTRACT_CERTIFICATE_VERSIONS
            and certificate_version != FEASIBILITY_CERTIFICATE_VERSION
        ):
            raise DeviceRepairResumeError(
                "V2 feasibility_certificate version 2.4 is obsolete because its "
                "signed Device Plan scope omits material execution sidecars; "
                "re-audit and re-issue a version 2.5 certificate"
            )
        if (
            request_contract == "v2"
            and certificate_version != FEASIBILITY_CERTIFICATE_VERSION
        ):
            raise DeviceRepairResumeError(
                "V2 feasibility_certificate version is unsupported or missing"
            )
        if certificate_contract and certificate_contract != request_contract:
            raise DeviceRepairResumeError(
                "feasibility_certificate contract_version does not match repair request"
            )
        if str(override.get("request_id") or "") != str(
            request.get("request_id") or ""
        ):
            raise DeviceRepairResumeError("override request_id does not match request")

        request_seed = {
            "campaign_id": self.config.campaign_id,
            "campaign_iteration": int(request.get("campaign_iteration") or 0),
            "query": str(request.get("query") or ""),
            "repair_generation": int(request.get("repair_generation") or 1),
            "research_state_sha256": str(
                request.get("research_state_sha256") or ""
            ),
            "route_signature": request.get("frozen_route_signature"),
            "effective_contract_version": request_contract,
            "last_plan": request.get("last_device_plan"),
        }
        expected_request_id = (
            "device-repair-" + _value_signature(request_seed)[:16]
        )
        if str(request.get("request_id") or "") != expected_request_id:
            raise DeviceRepairResumeError(
                "repair request_id does not match its frozen content"
            )

        state_path = Path(str(request.get("research_state_path") or "")).expanduser()
        if not state_path.is_absolute():
            state_path = (request_path.parent / state_path).resolve()
        else:
            state_path = state_path.resolve()
        if not state_path.exists():
            raise DeviceRepairResumeError(
                f"frozen Research state does not exist: {state_path}"
            )
        actual_state_hash = _file_sha256(state_path)
        expected_state_hash = str(request.get("research_state_sha256") or "")
        if not expected_state_hash or actual_state_hash != expected_state_hash:
            raise DeviceRepairResumeError(
                "frozen Research state hash mismatch; re-bootstrap is required"
            )
        if str(override.get("research_state_sha256") or "") != expected_state_hash:
            raise DeviceRepairResumeError(
                "override Research state hash does not match repair request"
            )

        signature_pairs = (
            ("route_signature", "frozen_route_signature"),
            ("sample_matrix_signature", "frozen_sample_matrix_signature"),
            ("device_snapshot_signature", "device_snapshot_signature"),
            ("route_payload_sha256", "frozen_route_payload_sha256"),
            ("sample_matrix_sha256", "frozen_sample_matrix_sha256"),
            ("device_snapshot_sha256", "device_snapshot_sha256"),
        )
        for override_key, request_key in signature_pairs:
            expected = str(request.get(request_key) or "")
            actual = str(override.get(override_key) or "")
            if not expected or actual != expected:
                raise DeviceRepairResumeError(
                    f"override {override_key} does not match frozen request"
                )

        certificate_signature_pairs = (
            (
                certificate.get("route_signature")
                or certificate.get("research_plan_signature"),
                request.get("frozen_route_signature"),
                "route",
            ),
            (
                certificate.get("sample_matrix_signature"),
                request.get("frozen_sample_matrix_signature"),
                "sample matrix",
            ),
            (
                certificate.get("device_snapshot_signature")
                or certificate.get("device_truth_sha256"),
                request.get("device_snapshot_signature"),
                "device snapshot",
            ),
        )
        for certificate_value, request_value, label in certificate_signature_pairs:
            if certificate_value and str(certificate_value) != str(request_value):
                raise DeviceRepairResumeError(
                    f"feasibility_certificate {label} signature does not match request"
                )

        if certificate.get("route_signature") and certificate.get(
            "route_signature"
        ) != certificate.get("research_plan_signature"):
            raise DeviceRepairResumeError(
                "feasibility_certificate route_signature is invalid"
            )
        expected_matrix_signature = certificate_stable_digest(
            certificate.get("sample_control_matrix", []),
            prefix="sample_matrix",
        )
        if certificate.get("sample_matrix_signature") and certificate.get(
            "sample_matrix_signature"
        ) != expected_matrix_signature:
            raise DeviceRepairResumeError(
                "feasibility_certificate sample_matrix_signature is invalid"
            )
        if certificate.get("device_snapshot_signature") and certificate.get(
            "device_snapshot_signature"
        ) != certificate.get("device_truth_sha256"):
            raise DeviceRepairResumeError(
                "feasibility_certificate device_snapshot_signature is invalid"
            )

        if (
            certificate_version == "2.3"
            or certificate_version in FULL_PLAN_CONTRACT_CERTIFICATE_VERSIONS
        ):
            protected = feasibility_certificate_protected_payload(certificate)
            expected_protected_digest = certificate_stable_digest(protected)
            if certificate.get("protected_digest") != expected_protected_digest:
                raise DeviceRepairResumeError(
                    "feasibility_certificate protected_digest is invalid"
                )
            expected_certificate_id = feasibility_certificate_id(
                protected_digest=expected_protected_digest,
                device_snapshot_id=str(
                    certificate.get("device_snapshot_id") or ""
                ),
                device_truth_sha256=str(
                    certificate.get("device_truth_sha256") or ""
                ),
            )
            if certificate.get("certificate_id") != expected_certificate_id:
                raise DeviceRepairResumeError(
                    "feasibility_certificate certificate_id is invalid"
                )
            accepted_plan_digest = str(
                certificate.get("accepted_device_plan_contract_sha256") or ""
            )
            if request_contract == "v2" and not accepted_plan_digest:
                raise DeviceRepairResumeError(
                    "V2 feasibility_certificate lacks its accepted Device Plan digest"
                )
            if (
                request_contract == "v2"
                and accepted_plan_digest
                and accepted_plan_digest != (
                device_plan_contract_digest(request.get("last_device_plan"))
                )
            ):
                raise DeviceRepairResumeError(
                    "repair request last_device_plan does not match its "
                    "feasibility_certificate"
                )

        if _value_signature(request.get("frozen_route_payload")) != str(
            request.get("frozen_route_payload_sha256")
        ):
            raise DeviceRepairResumeError("repair request route payload is corrupted")
        if _value_signature(request.get("frozen_sample_matrix")) != str(
            request.get("frozen_sample_matrix_sha256")
        ):
            raise DeviceRepairResumeError("repair request sample matrix is corrupted")
        if _value_signature(request.get("device_snapshot")) != str(
            request.get("device_snapshot_sha256")
        ):
            raise DeviceRepairResumeError("repair request device snapshot is corrupted")

        declarations = _as_dict(override.get("declarations"))
        forbidden_declarations = (
            "route_changed",
            "sample_matrix_changed",
            "reagent_identity_or_order_changed",
            "observation_points_changed",
        )
        if any(declarations.get(key) is not False for key in forbidden_declarations):
            raise DeviceRepairResumeError(
                "override must explicitly declare every frozen scientific invariant unchanged"
            )
        device_plan = override.get("device_plan")
        if not isinstance(device_plan, (dict, list)) or not device_plan:
            raise DeviceRepairResumeError(
                "override must contain a complete non-empty device_plan"
            )

        frozen_matrix = request.get("frozen_sample_matrix")
        embedded_matrix = (
            _first_nonempty(
                device_plan.get("sample_matrix"),
                device_plan.get("sample_control_matrix"),
                [],
            )
            if isinstance(device_plan, dict)
            else []
        )
        if embedded_matrix not in (None, "", [], {}) and _value_signature(
            embedded_matrix
        ) != _value_signature(frozen_matrix):
            raise DeviceRepairResumeError(
                "override device_plan changes the frozen sample/control matrix"
            )

        changes = override.get("changes") or []
        requires_review = any(
            adjustment_requires_scientific_review(item)
            for item in changes
            if isinstance(changes, list)
        )
        if requires_review and override.get("requires_scientific_review") is not True:
            raise DeviceRepairResumeError(
                "batch/amount/concentration/ratio changes require scientific review"
            )

        state = self._load_json_object(state_path, "frozen Research state")
        route_payload_hash = _value_signature(self._research_route_payload(state))
        if route_payload_hash != str(request.get("frozen_route_payload_sha256")):
            raise DeviceRepairResumeError(
                "current Research route or macro plan differs from the frozen request"
            )
        sample_hash = _value_signature(self._sample_matrix_from_state(state))
        if sample_hash != str(request.get("research_state_sample_matrix_sha256")):
            raise DeviceRepairResumeError(
                "current Research sample/control matrix differs from the frozen request"
            )
        try:
            override, _, _, _ = validate_human_quantity_approvals(
                override,
                request,
                repair_request_sha256=_file_sha256(request_path),
            )
        except HumanQuantityApprovalError as exc:
            raise DeviceRepairResumeError(
                f"invalid human quantity approval: {exc}"
            ) from exc
        return request, override, state_path, state

    # ------------------------------------------------------------------
    # default subprocess step implementations
    # ------------------------------------------------------------------

    def _research_step_subprocess(
        self,
        event_type: str,
        *,
        query: str,
        previous_state_path: Optional[Path],
        payload: Optional[Dict[str, Any]],
        iteration_dir: Path,
        references: List[str],
    ) -> Dict[str, Any]:
        state_path = iteration_dir / "research_state.json"
        ledger_path = default_ledger_path(
            self.config.campaign_id,
            campaigns_root=self.campaigns_root,
        )
        command = [
            sys.executable,
            str(REPO_ROOT / "reaserch_agent" / "run_research_agent.py"),
            "--event-type",
            event_type,
            "--campaign-id",
            self.config.campaign_id,
            "--ledger-path",
            str(ledger_path),
            "--save-state",
            str(state_path),
        ]
        if query:
            command += ["--query", query]
        if previous_state_path:
            command += ["--previous-state", str(previous_state_path)]
        if payload:
            command += ["--payload-json", json.dumps(payload, ensure_ascii=False)]
        for reference in references:
            command += ["--reference", reference]
        command += self.config.research_args
        if not _contract_versions_from_args(self.config.research_args):
            command += [
                "--contract-version",
                self.config.requested_contract_version,
            ]

        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        (iteration_dir / "research_step.log").write_text(
            completed.stdout + "\n--- stderr ---\n" + completed.stderr,
            encoding="utf-8",
        )
        if not state_path.exists():
            raise RuntimeError(
                "research step produced no state file "
                f"(exit={completed.returncode}): {completed.stderr[-800:]}"
            )
        return json.loads(state_path.read_text(encoding="utf-8"))

    def _device_step_subprocess(
        self,
        state_path: Path,
        iteration_dir: Path,
        *,
        device_plan_override_path: Optional[Path] = None,
        prior_repair_request_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        package_path = iteration_dir / "device_package.json"
        device_state_path = iteration_dir / "device_state.json"
        command = [
            sys.executable,
            str(REPO_ROOT / "device_agent" / "run_from_research_state.py"),
            "--research-state",
            str(state_path),
            "--output",
            str(device_state_path),
            "--package-output",
            str(package_path),
        ]
        if device_plan_override_path:
            command += ["--device-plan-override", str(device_plan_override_path)]
        if prior_repair_request_path:
            command += ["--prior-repair-request", str(prior_repair_request_path)]
        command += self.config.device_args
        if not _contract_versions_from_args(self.config.device_args):
            command += [
                "--contract-version",
                self.config.requested_contract_version,
            ]
        # Pin generation and dispatch checking to the same selected contract
        # root. A subprocess .env file must not silently select another source.
        command += ["--workstations-dir", str(self._dispatch_workstation_root())]

        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        (iteration_dir / "device_step.log").write_text(
            completed.stdout + "\n--- stderr ---\n" + completed.stderr,
            encoding="utf-8",
        )
        if not package_path.exists():
            raise RuntimeError(
                "device step produced no package file "
                f"(exit={completed.returncode}): {completed.stderr[-800:]}"
            )
        package = json.loads(package_path.read_text(encoding="utf-8"))
        _validated_package_contract_version(
            package,
            self.config.requested_contract_version,
            label="Device subprocess output",
        )
        return package

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _iteration_dir(self, iteration: int) -> Path:
        path = self.campaign_dir / f"iteration_{iteration:02d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_human_readable(
        self,
        directory: Path,
        research_state: Dict[str, Any],
        device_package: Optional[Dict[str, Any]],
        *,
        campaign_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Issue 2: one user-readable extraction per iteration + per campaign.

        Read-only convenience output — a failure here must never affect the
        campaign loop itself.
        """
        try:
            write_human_readable_result(
                directory,
                research_state,
                device_package or None,
                campaign_meta=campaign_meta,
            )
        except Exception as exc:  # pragma: no cover - must not break the loop
            self._log(f"human readable result generation failed: {exc}")

    def _write_review_request(self, iteration_dir: Path, package: Dict[str, Any]) -> None:
        """Explain what needs review and how to release the gate."""
        workflow_json = package.get("workflow_json")
        adaptations = (
            workflow_json.get("temporal_adaptations", [])
            if isinstance(workflow_json, dict)
            else []
        ) or package.get("temporal_adaptations", [])
        lines = [
            "# 等待科学复核（scientific review required）",
            "",
            "本轮 device workflow 携带近似/派生适配（见同目录 device_package.json），",
            "在进入真实/外部执行边界前必须由人工确认。",
            "",
            "## 待复核内容",
        ]
        if isinstance(adaptations, list) and adaptations:
            for item in adaptations:
                if isinstance(item, dict):
                    lines.append(
                        f"- 原始要求：{item.get('original_requirement', '')} | "
                        f"近似方式：{item.get('adaptation_schedule', '')} | "
                        f"保真等级：{item.get('execution_fidelity', '')}"
                    )
        else:
            lines.append("- （见 device_package.json 中 requires_scientific_review 标记）")
        lines += [
            "",
            "## 放行方式",
            f"确认无误后，在本目录写入 `{APPROVAL_FILENAME}`：",
            "",
            '    {"approved": true, "approver": "你的姓名", "comment": "确认分批近似可接受"}',
            "",
            "然后重新运行 campaign（可复用同一 campaign_id 继续）。",
        ]
        (iteration_dir / "AWAITING_SCIENTIFIC_REVIEW.md").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _attach_actual_execution_parameters(
        observation: Dict[str, Any],
        package: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Carry effective Device parameters into Research result interpretation.

        The adapter owns measured observations.  Device owns the difference
        between nominal Research quantities and the executable parameters.  The
        two are joined here without overwriting adapter-provided fields.
        """

        enriched = dict(observation) if isinstance(observation, dict) else {
            "summary": str(observation)
        }
        workflow = _as_dict(package.get("workflow_json"))
        effective = _first_nonempty(
            package.get("actual_parameter_adjustments"),
            package.get("quantity_adjustments"),
            workflow.get("quantity_adjustments"),
            [],
        )
        batch_plan = _first_nonempty(
            package.get("batch_plan"),
            workflow.get("batch_plan"),
            [],
        )
        material_ledger = _first_nonempty(
            package.get("material_ledger"),
            workflow.get("material_ledger"),
            {},
        )
        plan_level_repair = _as_dict(package.get("plan_level_repair"))
        device_plan_adjustments = _first_nonempty(
            package.get("device_plan_adjustments"),
            package.get("device_plan_changes"),
            plan_level_repair.get("plan_changes"),
            [],
        )
        execution_context = {
            "research_plan_signature": _first_nonempty(
                package.get("research_plan_signature"),
                _as_dict(package.get("feasibility_certificate")).get(
                    "research_plan_signature"
                ),
            ),
            "quantity_adjustments": effective,
            "batch_plan": batch_plan,
            "material_ledger": material_ledger,
            "device_plan_adjustments": device_plan_adjustments,
            "temporal_adaptations": _first_nonempty(
                package.get("temporal_adaptations"),
                workflow.get("temporal_adaptations"),
                [],
            ),
            "requires_scientific_review": bool(
                package.get("requires_scientific_review")
            ),
        }
        existing = enriched.get("actual_execution_parameters")
        if isinstance(existing, dict):
            merged = dict(execution_context)
            merged.update(existing)
            execution_context = merged
        enriched["actual_execution_parameters"] = execution_context
        if str(package.get("contract_version") or "") == "v2":
            raw_device = _as_dict(package.get("device_workflow_package_v2"))
            macro_plan = _as_dict(package.get("macro_plan"))
            raw_research = _as_dict(macro_plan.get("research_action_package_v2"))
            if raw_device and raw_research:
                research_contract = ResearchActionPackageV2.model_validate(raw_research)
                device_contract = DeviceWorkflowPackageV2.model_validate(raw_device)
                observation_event = build_observation_event_v2(
                    enriched, research_contract, device_contract
                ).model_dump(mode="json", exclude_none=True)
                enriched["observation_event_v2"] = observation_event
                enriched["macro_parameter_summary"] = copy.deepcopy(
                    observation_event["macro_parameter_summary"]
                )
                enriched["device_parameter_trace"] = copy.deepcopy(
                    observation_event["device_parameter_trace"]
                )
        return enriched

    @staticmethod
    def _research_route_payload(state: Dict[str, Any]) -> Dict[str, Any]:
        handoff = _as_dict(state.get("device_adaptation_handoff"))
        if not handoff:
            handoff = _as_dict(
                state.get("B. 发给下游 device adaptation layer agent 的外部交接输出")
            )
        return {
            "stage_route": _first_nonempty(
                handoff.get("stage 路线"),
                handoff.get("stage_route"),
                state.get("stage_route"),
            ),
            "current_stage": _first_nonempty(
                handoff.get("当前 stage"),
                state.get("current_stage"),
            ),
            "current_stage_plan": _first_nonempty(
                handoff.get("当前 stage 的完整化学语义实验计划"),
                state.get("current_stage_plan"),
            ),
            "macro_plan": _first_nonempty(
                handoff.get("待执行 macro plan"),
                handoff.get("macro_plan"),
                state.get("macro_plan"),
                [],
            ),
        }

    @staticmethod
    def _sample_matrix_from_state(state: Dict[str, Any]) -> Any:
        handoff = _as_dict(state.get("device_adaptation_handoff"))
        persistent = _as_dict(state.get("persistent_outputs"))
        macro_action = _as_dict(
            _first_nonempty(
                state.get("macro_action"),
                handoff.get("当前 macro action"),
                persistent.get("当前 macro action"),
                {},
            )
        )
        return _first_nonempty(
            handoff.get("sample_matrix"),
            handoff.get("样品/对照矩阵"),
            handoff.get("样品矩阵"),
            macro_action.get("sample_matrix"),
            macro_action.get("样品/对照矩阵"),
            state.get("sample_matrix"),
            [],
        )

    def _write_device_repair_artifacts(
        self,
        iteration_dir: Path,
        research_state_path: Path,
        package: Dict[str, Any],
        *,
        campaign_iteration: int,
        parent_request_id: str = "",
        repair_generation: int = 1,
    ) -> Path:
        """Emit a self-contained, resumable Device-only handoff package."""

        research_state_path = Path(research_state_path).expanduser().resolve()
        try:
            research_state = json.loads(
                research_state_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            research_state = {}
        if not isinstance(research_state, dict):
            research_state = {}

        error_package = _as_dict(package.get("error_package"))
        manual_context = _as_dict(package.get("manual_repair_context"))
        workflow_repair = _as_dict(package.get("workflow_repair"))
        certificate = _as_dict(
            _first_nonempty(
                package.get("feasibility_certificate"),
                error_package.get("feasibility_certificate"),
                {},
            )
        )
        route_payload = self._research_route_payload(research_state)
        sample_matrix = _first_nonempty(
            certificate.get("sample_matrix"),
            certificate.get("sample_control_matrix"),
            package.get("sample_matrix"),
            self._sample_matrix_from_state(research_state),
            [],
        )
        if not isinstance(sample_matrix, (list, dict)):
            sample_matrix = []
        device_snapshot = _first_nonempty(
            certificate.get("device_snapshot"),
            certificate.get("workstation_snapshot"),
            package.get("device_snapshot"),
            {
                "snapshot_id": _first_nonempty(
                    certificate.get("device_snapshot_id"),
                    package.get("device_snapshot_id"),
                    "",
                )
            }
            if _first_nonempty(
                certificate.get("device_snapshot_id"),
                package.get("device_snapshot_id"),
                "",
            )
            else {},
            {},
        )
        route_signature = str(
            _first_nonempty(
                certificate.get("research_plan_signature"),
                certificate.get("route_signature"),
                package.get("research_plan_signature"),
                _value_signature(route_payload),
            )
        )
        sample_matrix_signature = str(
            _first_nonempty(
                certificate.get("sample_matrix_signature"),
                package.get("sample_matrix_signature"),
                _value_signature(sample_matrix),
            )
        )
        device_snapshot_signature = str(
            _first_nonempty(
                certificate.get("device_snapshot_signature"),
                certificate.get("workstation_snapshot_signature"),
                package.get("device_snapshot_signature"),
                _value_signature(device_snapshot),
            )
        )
        research_state_sha256 = (
            _file_sha256(research_state_path) if research_state_path.exists() else ""
        )
        explicit_last_device_plan = _first_nonempty(
            package.get("last_device_plan"),
            manual_context.get("last_device_plan"),
            error_package.get("last_device_plan"),
            {},
        )
        raw_device_plan = package.get("device_plan")
        if isinstance(raw_device_plan, list):
            # Keep repair resumption on the exact same signed projection as
            # certificate issuance.  A hand-maintained field list previously
            # dropped new 2.5 material execution sidecars and made an otherwise
            # valid certificate fail its resume digest check.
            last_device_plan = {
                "status": "device_plan",
                **device_plan_contract_view(package),
            }
            if "quantity_audit" in package:
                last_device_plan["quantity_audit"] = copy.deepcopy(
                    package.get("quantity_audit")
                )
        elif isinstance(raw_device_plan, dict):
            last_device_plan = copy.deepcopy(raw_device_plan)
        elif isinstance(explicit_last_device_plan, dict) and explicit_last_device_plan:
            last_device_plan = copy.deepcopy(explicit_last_device_plan)
        else:
            last_device_plan = {
                "status": "device_plan",
                **device_plan_contract_view(package),
            }
            if "quantity_audit" in package:
                last_device_plan["quantity_audit"] = copy.deepcopy(
                    package.get("quantity_audit")
                )
        effective_contract_version = _validated_package_contract_version(
            package,
            self.config.requested_contract_version,
            label="Device package selected for repair",
        )
        certificate_contract_version = str(
            certificate.get("contract_version") or ""
        ).strip()
        certificate_version = strict_feasibility_certificate_version(certificate)
        if (
            certificate_version in FULL_PLAN_CONTRACT_CERTIFICATE_VERSIONS
            and certificate_version != FEASIBILITY_CERTIFICATE_VERSION
        ):
            raise DeviceRepairResumeError(
                "obsolete full-contract feasibility_certificate requires "
                f"re-audit and version {FEASIBILITY_CERTIFICATE_VERSION} re-issuance"
            )
        if (
            effective_contract_version == "v2"
            and certificate_version != FEASIBILITY_CERTIFICATE_VERSION
        ):
            raise DeviceRepairResumeError(
                "V2 feasibility_certificate version is unsupported or missing"
            )
        if (
            effective_contract_version == "v2"
            and certificate_contract_version != "v2"
        ):
            raise DeviceRepairResumeError(
                "V2 feasibility_certificate lacks its frozen contract_version"
            )
        if (
            certificate_contract_version
            and certificate_contract_version != effective_contract_version
        ):
            raise DeviceRepairResumeError(
                "feasibility_certificate contract_version does not match Device package"
            )
        contract_resolution = copy.deepcopy(
            _as_dict(package.get("contract_resolution"))
        )
        request_seed = {
            "campaign_id": self.config.campaign_id,
            "campaign_iteration": campaign_iteration,
            "query": self.config.query,
            "repair_generation": repair_generation,
            "research_state_sha256": research_state_sha256,
            "route_signature": route_signature,
            "effective_contract_version": effective_contract_version,
            "last_plan": last_device_plan,
        }
        request_id = "device-repair-" + _value_signature(request_seed)[:16]
        request = {
            "schema_version": "chem-device-repair-request/v1",
            "request_id": request_id,
            "parent_request_id": parent_request_id,
            "repair_generation": repair_generation,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": str(
                package.get("failure_scope") or "device_workflow"
            ),
            "campaign_id": self.config.campaign_id,
            "campaign_iteration": campaign_iteration,
            "query": self.config.query,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "contract_version": effective_contract_version,
            "contract_resolution": contract_resolution,
            "research_state_path": str(research_state_path),
            "research_state_sha256": research_state_sha256,
            "feasibility_certificate": certificate,
            "frozen_route_signature": route_signature,
            "frozen_route_payload": route_payload,
            "frozen_route_payload_sha256": _value_signature(route_payload),
            "frozen_sample_matrix": sample_matrix,
            "frozen_sample_matrix_signature": sample_matrix_signature,
            "frozen_sample_matrix_sha256": _value_signature(sample_matrix),
            "research_state_sample_matrix_sha256": _value_signature(
                self._sample_matrix_from_state(research_state)
            ),
            "device_snapshot": device_snapshot,
            "device_snapshot_signature": device_snapshot_signature,
            "device_snapshot_sha256": _value_signature(device_snapshot),
            "last_device_plan": last_device_plan,
            "last_workflow": _first_nonempty(
                package.get("workflow_json"),
                package.get("last_workflow"),
                package.get("last_workflow_json"),
                manual_context.get("last_workflow"),
                error_package.get("last_workflow"),
                {},
            ),
            "repair_history": _first_nonempty(
                package.get("repair_history"),
                package.get("repair_cycles"),
                package.get("workflow_repair_history"),
                manual_context.get("repair_history"),
                manual_context.get("workflow_repair_cycles"),
                workflow_repair.get("deduplicated_error_history"),
                workflow_repair.get("cycles"),
                error_package.get("repair_history"),
                [],
            ),
            "workflow_repair_cycles": _first_nonempty(
                manual_context.get("workflow_repair_cycles"),
                workflow_repair.get("cycles"),
                package.get("repair_cycles"),
                [],
            ),
            "structured_errors": _first_nonempty(
                error_package.get("structured_errors"),
                package.get("structured_errors"),
                [],
            ),
            "allowed_device_plan_changes": [
                "workstation selection",
                "container and slot allocation",
                "operation decomposition",
                "transfer/split/batch execution",
                "total amount, concentration, or molar ratio with scientific-review flag",
            ],
            "forbidden_changes": [
                "target material or reaction route",
                "reagent identity or reagent order",
                "observation point",
                "sample/control/variable matrix",
            ],
            "human_quantity_approval_contract": {
                "schema_version": "chem-human-quantity-approval/v1",
                "allowed_basis": [
                    "observed_quantity",
                    "planning_yield_lower_bound",
                ],
                "transition_quantity_basis_mapping": {
                    "observed_quantity": "measured_observation",
                    "planning_yield_lower_bound": "planning_yield_lower_bound",
                },
                "responsibility": (
                    "observed_quantity is a human-attested measurement, not an "
                    "ordinary model observation; planning_yield_lower_bound is an "
                    "explicit scientific planning assumption. The named approver "
                    "accepts responsibility for the bound transition quantity."
                ),
                "required_target_binding": [
                    "transition_id",
                    "sample_id",
                    "material_id",
                    "batch_id",
                    "approved_quantity",
                ],
            },
        }
        request_path = iteration_dir / DEVICE_REPAIR_REQUEST
        request_path.write_text(
            json.dumps(request, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        repair_request_sha256 = _file_sha256(request_path)

        override_template = {
            "schema_version": "chem-device-plan-override/v1",
            "request_id": request_id,
            "research_state_sha256": research_state_sha256,
            "route_signature": route_signature,
            "route_payload_sha256": request["frozen_route_payload_sha256"],
            "sample_matrix_signature": sample_matrix_signature,
            "sample_matrix_sha256": request["frozen_sample_matrix_sha256"],
            "device_snapshot_signature": device_snapshot_signature,
            "device_snapshot_sha256": request["device_snapshot_sha256"],
            "contract_version": effective_contract_version,
            "declarations": {
                "route_changed": False,
                "sample_matrix_changed": False,
                "reagent_identity_or_order_changed": False,
                "observation_points_changed": False,
            },
            "device_plan": request["last_device_plan"],
            "changes": [],
            "requires_scientific_review": False,
            "human_quantity_approvals": [],
            "human_quantity_approval_template": approval_contract_template(
                request,
                repair_request_sha256=repair_request_sha256,
            ),
        }
        (iteration_dir / DEVICE_PLAN_OVERRIDE_TEMPLATE).write_text(
            json.dumps(override_template, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        lines = [
            "# 等待 Device 层人工修订",
            "",
            "两轮 Device workflow 自修复已耗尽，或剂量/合批条件需要人工判断。",
            "本请求不会返回 Research B2，Research 路线与样品矩阵保持冻结。",
            "",
            "## 文件",
            "",
            f"- `{DEVICE_REPAIR_REQUEST}`：冻结签名、Research state、设备快照与两轮错误历史",
            f"- `{DEVICE_PLAN_OVERRIDE_TEMPLATE}`：可编辑的完整 Device plan 模板",
            "- 若要解除 unknown-yield 数量门，将模板中的 "
            "`human_quantity_approval_template` 复制到 "
            "`human_quantity_approvals`，逐项填写并签认；不要把模板对象本身当批准。",
            "- `observed_quantity` 表示人工对测量值作出的明确证明，并不伪装成机器 "
            "observation；`planning_yield_lower_bound` 表示人工批准的科学规划下界。",
            "",
            "## 续跑",
            "",
            "```bash",
            "python run_campaign.py \\",
            f"  --contract-version {effective_contract_version} \\",
            f"  --resume-device-repair {shlex.quote(str(request_path))} \\",
            "  --device-plan-override /path/to/device_plan_override.json",
            "```",
            "",
            "若需要改目标、化学路线、试剂身份/顺序、observation point 或样品矩阵，",
            "不得使用 Device override，必须重新进入 Research 规划。",
        ]
        (iteration_dir / DEVICE_REPAIR_MARKDOWN).write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        return request_path

    def _write_unverifiable_review_request(
        self,
        iteration_dir: Path,
        error_package: Dict[str, Any],
    ) -> None:
        if error_package.get("type") == "device_external_return_wait_required":
            lines = [
                "# 等待真实 observation / 人工返回",
                "",
                "当前 macro action 跨越了必需的外部读数等待点，尚未批准机器执行。",
                "不能用审核同意、Device override 或预计结果代替真实返回。",
                "请先按 observation 边界拆分计划；取得真实结果后，通过 Research 的",
                "post_observation 交接生成下一段宏动作。原计划的后续科学步骤仍被保留。",
                "",
            ]
            for pending in error_package.get("pending_returns", []) or []:
                if isinstance(pending, dict):
                    lines.append(
                        f"- 步骤 {pending.get('source_macro_step', '?')}："
                        + str(pending.get("wait_for") or pending.get("name") or "需要真实返回")
                    )
            (iteration_dir / "AWAITING_CONDITION_REVIEW.md").write_text(
                "\n".join(lines) + "\n", encoding="utf-8",
            )
            return
        stage1_device_local_exhausted = (
            str(error_package.get("type", "")).strip().lower()
            == "stage1_plan_repair_exhausted"
        )
        classification = (
            error_package.get("constraint_classification")
            if isinstance(error_package.get("constraint_classification"), dict)
            else {}
        )
        unverifiable = classification.get("unverifiable") or error_package.get(
            "blocking_constraints", []
        )
        lines = [
            "# 需要人工判定的条件（无法从设备真源证明）",
            "",
            "device agent 没有发现硬设备能力缺口，但以下条件无法从真源证明满足，",
            "因此没有把方案判为不可执行，而是转交人工审核：",
            "",
        ]
        for item in unverifiable:
            lines.append(f"- {item}")
        if stage1_device_local_exhausted:
            lines += [
                "",
                "这些是 Stage-1 Device-local 计划修复候选耗尽后的人工判定项。",
                "Research 化学路线与样品矩阵保持不变；请只在 Device 层修订工作站、",
                "容器、操作拆分或确定配方，不得把该错误返回 Research。",
            ]
        else:
            lines += [
                "",
                "请确认这些条件在本实验室是否成立；成立则可将该轮 macro plan 视为可执行，",
                "否则请给 research layer 提出化学语义修改意见。",
            ]
        (iteration_dir / "AWAITING_CONDITION_REVIEW.md").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    def _accumulate_campaign_constraints(self, package: Dict[str, Any]) -> None:
        """Collect device blocking constraints across the whole campaign (issue 4)."""
        error_package = package.get("error_package")
        if not isinstance(error_package, dict):
            return
        existing = set(self._campaign_constraints)
        for constraint in error_package.get("blocking_constraints", []) or []:
            text = str(constraint).strip()
            if text and text not in existing:
                self._campaign_constraints.append(text)
                existing.add(text)

    @staticmethod
    def _feasibility_payload(package: Dict[str, Any]) -> Dict[str, Any]:
        """Trim the device error package into a research B2 payload."""
        trimmed = {
            key: value
            for key, value in package.items()
            if key not in {"macro_plan", "feasibility_assessment"}
        }
        trimmed.setdefault(
            "observation",
            {"summary": "设备适应层返回 feasibility_error，需要研究层修订 macro plan。"},
        )
        return trimmed

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(f"[campaign] {message}", flush=True)
        log_path = self.campaign_dir / "campaign_log.txt"
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def _write_final_report(
        self,
        *,
        stop_reason: str,
        goal_reached: bool,
        iterations_run: int,
        final_state_path: Path,
        started_at: str,
        finished_at: str,
    ) -> Path:
        ledger_records = PlanLedger(
            default_ledger_path(
                self.config.campaign_id,
                campaigns_root=self.campaigns_root,
            )
        ).read_all()

        def read_json_object(path: Path) -> Dict[str, Any]:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            return value if isinstance(value, dict) else {}

        final_research_state = read_json_object(final_state_path)
        research_effective = str(
            final_research_state.get("contract_version")
            or _as_dict(
                final_research_state.get("contract_resolution")
            ).get("effective")
            or ""
        ).strip()
        device_effective = ""
        device_contract_path = ""
        for package_path in reversed(
            sorted(
                self.campaign_dir.rglob("device_package.json"),
                key=lambda path: path.as_posix(),
            )
        ):
            device_package = read_json_object(package_path)
            if not device_package:
                continue
            device_effective = str(
                _as_dict(device_package.get("contract_resolution")).get(
                    "effective"
                )
                or device_package.get("contract_version")
                or ""
            ).strip()
            device_contract_path = str(package_path)
            break
        resolved_versions = [
            value for value in (research_effective, device_effective) if value
        ]
        contract_resolution = {
            "requested": self.config.requested_contract_version,
            "research_effective": research_effective,
            "device_effective": device_effective,
            "matched": bool(resolved_versions)
            and all(
                value == self.config.requested_contract_version
                for value in resolved_versions
            ),
            "research_state_path": str(final_state_path),
            "device_package_path": device_contract_path,
        }

        summary = {
            "campaign_id": self.config.campaign_id,
            "query": self.config.query,
            "stop_reason": stop_reason,
            "goal_reached": goal_reached,
            "iterations_run": iterations_run,
            "started_at": started_at,
            "finished_at": finished_at,
            "adapter": self.adapter.name,
            "plan_versions": len(ledger_records),
            "final_state_path": str(final_state_path),
            "contract_resolution": contract_resolution,
            "cumulative_device_constraints": list(self._campaign_constraints),
            "trace": self._trace,
        }
        (self.campaign_dir / "campaign_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        lines: List[str] = [
            f"# Campaign Report: {self.config.campaign_id}",
            "",
            f"- query: {self.config.query}",
            f"- stop_reason: **{stop_reason}**",
            f"- goal_reached: {goal_reached}",
            f"- iterations_run: {iterations_run}",
            f"- execution_adapter: {self.adapter.name}",
            f"- started_at: {started_at}",
            f"- finished_at: {finished_at}",
            f"- final_state: {final_state_path}",
            f"- contract_requested: {self.config.requested_contract_version}",
            f"- research_contract_effective: {research_effective or 'unavailable'}",
            f"- device_contract_effective: {device_effective or 'unavailable'}",
            "",
            "## 计划版本演化（含修改/放弃原因）",
            "",
        ]
        if ledger_records:
            lines.append("| 版本 | 事件 | 范围 | 触发 | 原因 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for record in ledger_records:
                reason = str(record.get("reason", "")).replace("|", "/")
                if len(reason) > 160:
                    reason = reason[:160] + "…"
                lines.append(
                    f"| v{record.get('plan_version')} "
                    f"| {record.get('event')} "
                    f"| {record.get('scope')} "
                    f"| {record.get('trigger')} "
                    f"| {reason} |"
                )
        else:
            lines.append("(台账为空)")

        lines += ["", "## 迭代轨迹", ""]
        if self._trace:
            for entry in self._trace:
                detail = ", ".join(
                    f"{key}={value}"
                    for key, value in entry.items()
                    if key not in {"iteration", "phase"}
                )
                lines.append(
                    f"- iteration {entry.get('iteration')} / "
                    f"{entry.get('phase')}: {detail}"
                )
        else:
            lines.append("(无迭代)")

        report_path = self.campaign_dir / "final_report.md"
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report_path

    def _dispatch_workstation_root(self) -> Path:
        """Resolve one source for both the Device subprocess and execution gate."""
        selected = os.getenv("CHEM_WORKSTATIONS_NEW_DIR", "").strip()
        for index, argument in enumerate(self.config.device_args):
            if argument == "--workstations-dir":
                if index + 1 >= len(self.config.device_args):
                    raise ValueError("--workstations-dir requires a path")
                selected = self.config.device_args[index + 1]
                if not selected.strip() or selected.startswith("--"):
                    raise ValueError("--workstations-dir requires a non-empty path")
            elif argument.startswith("--workstations-dir="):
                selected = argument.split("=", 1)[1]
                if not selected.strip():
                    raise ValueError("--workstations-dir requires a non-empty path")
        if not selected:
            return (
                REPO_ROOT / "chem_resources" / "lab-design-all"
                / "skills" / "chemistry-experiment-workstation"
            )
        source = Path(selected).expanduser()
        return (source if source.is_absolute() else REPO_ROOT / source).resolve()

    def _check_package_for_dispatch(
        self,
        package: Dict[str, Any],
        iteration_dir: Path,
        *,
        allowed_certificate_scopes: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """Recheck a package against the current dispatch contract."""

        resolution = _as_dict(package.get("contract_resolution"))
        certificate = _as_dict(package.get("feasibility_certificate"))
        declared_contracts = {
            self.config.requested_contract_version,
            str(package.get("contract_version") or "").strip(),
            str(resolution.get("requested") or "").strip(),
            str(resolution.get("effective") or "").strip(),
            str(certificate.get("contract_version") or "").strip(),
        }
        certificate_error: DeviceRepairResumeError | None = None
        if canonical_success_envelope(package) and "v2" in declared_contracts:
            try:
                self._validate_v2_success_certificate_core(
                    package,
                    allowed_scopes=allowed_certificate_scopes,
                )
            except DeviceRepairResumeError as exc:
                certificate_error = exc

        if certificate_error is not None:
            report = {
                "status": "failed",
                "dispatchable": False,
                "findings": [{
                    "severity": "error",
                    "code": "invalid_v2_feasibility_certificate",
                    "path": "/feasibility_certificate",
                    "message": str(certificate_error),
                }],
                "summary": {"errors": 1, "warnings": 0},
                "checks": {
                    "feasibility_certificate": {
                        "status": "failed",
                        "certificate_version": certificate.get(
                            "certificate_version"
                        ),
                    }
                },
                "input_sha256": _value_signature(package),
                "source_manifest": {},
                "checked_steps": [],
            }
        else:
            try:
                report = check_dispatch(
                    package,
                    source_path=iteration_dir / "device_package.json",
                    artifact_root=iteration_dir,
                    workstation_root=self._dispatch_workstation_root(),
                    require_payload=True,
                )
            except Exception as exc:
                # Contract-loader or checker faults are Device errors, never a
                # reason to dispatch or to replan the chemistry through Research.
                report = {
                    "status": "not_verifiable",
                    "dispatchable": False,
                    "findings": [{
                        "severity": "error", "code": "checker_internal_error",
                        "path": "", "message": f"{type(exc).__name__}: {exc}",
                    }],
                    "summary": {"errors": 1, "warnings": 0},
                    "checks": {},
                    "input_sha256": _value_signature(package),
                    "source_manifest": {},
                    "checked_steps": [],
                }
        paths: Dict[str, str] = {}
        try:
            paths = write_check_report(report, iteration_dir)
        except (OSError, ValueError, TypeError) as exc:
            self._log(f"Device dispatch check report could not be saved: {exc}")
            report = dict(report, status="not_verifiable", dispatchable=False)
        dispatchable = report.get("status") == "passed" and report.get("dispatchable") is True
        self._trace.append({
            "phase": "dispatch_check",
            "status": report.get("status", "not_verifiable"),
            "dispatchable": dispatchable,
            "failure_scope": "" if dispatchable else "device_workflow",
            "input_sha256": report.get("input_sha256", ""),
            "report_paths": paths,
            "iteration_dir": str(iteration_dir),
            "research_invoked": False,
            "finding_codes": [
                str(item.get("code") or "")
                for item in report.get("findings", [])
                if isinstance(item, dict) and item.get("code")
            ],
        })
        if not dispatchable:
            self._log(
                "Device dispatch check blocked execution; "
                f"status={report.get('status')}, reports={paths}"
            )
            raise DispatchCheckBlockedError(report, paths)
        return report

    def _forward_only_stop_reason(
        self,
        package: Dict[str, Any],
        iteration_dir: Path,
        *,
        iteration: int,
        phase: str,
        allowed_certificate_scopes: Optional[Set[str]] = None,
    ) -> str:
        """Run the fresh dispatch gate, but never invoke an adapter or Research."""

        try:
            self._check_package_for_dispatch(
                package,
                iteration_dir,
                allowed_certificate_scopes=allowed_certificate_scopes,
            )
        except DispatchCheckBlockedError:
            return STOP_DEVICE_ERROR
        self._trace.append(
            {
                "iteration": iteration,
                "phase": phase,
                "status": str(package.get("status") or "ready_for_dispatch"),
                "forward_only": True,
                "workflow_steps": len(
                    _as_dict(package.get("workflow_json")).get("steps", [])
                ),
                "research_invoked": False,
                "adapter_invoked": False,
            }
        )
        self._log(
            f"iteration {iteration}: forward-only workflow is ready for "
            "dispatch; stopping before execution"
        )
        return STOP_READY_FOR_DISPATCH

    def _execute_checked_package(
        self,
        package: Dict[str, Any],
        iteration_dir: Path,
        *,
        allowed_certificate_scopes: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """Recheck the package and execute it only after the check passes."""

        self._check_package_for_dispatch(
            package,
            iteration_dir,
            allowed_certificate_scopes=allowed_certificate_scopes,
        )
        return self._attach_actual_execution_parameters(
            self.adapter.execute(package, iteration_dir), package
        )

"""Task-grounded sample-arm requirements and current-stage coverage.

The proposer is deliberately outside this module.  A model can suggest arms,
but every claimed task requirement must point back to the original query.  A
separate deterministic pass extracts explicit controls and variation demands
so an omitted control cannot silently disappear from the proposed matrix.
Route decisions remain the existing per-experimental-group authority.
"""

from __future__ import annotations

import re
import json
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import ConfigDict, Field, ValidationError, model_validator

from chem_agent_contracts.route_candidate import RouteGoalV1
from chem_agent_contracts.route_decision import RouteDecisionV1
from chem_agent_contracts.v2 import (
    StageV2, StrictModel, canonical_digest, route_material_graph_digest_v1,
)


class _Strict(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StageArmActionIntentV1(_Strict):
    """Semantic action intent; graph-verified IDs are bound only after selection."""

    group_label: str = Field(min_length=1)
    comparison_to_arm_ids: list[str] = Field(default_factory=list)
    hypothesis: str = ""
    variables: dict[str, Any] = Field(default_factory=dict)
    objective: str = Field(min_length=1)
    expected_observation: str = Field(min_length=1)
    completion_condition: str = Field(min_length=1)


class StageArmV1(_Strict):
    arm_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    role: Literal["experimental", "control", "repeat", "calibration"]
    origin: Literal["task_required", "new_design"]
    task_excerpt: str = ""
    task_term: str = ""
    design_rationale: str = ""
    route_goal: RouteGoalV1
    action_intent: StageArmActionIntentV1

    @model_validator(mode="after")
    def validate_origin(self) -> "StageArmV1":
        if self.origin == "task_required" and not (
            self.task_excerpt.strip() and self.task_term.strip()
        ):
            raise ValueError("task-required arm needs task excerpt and exact term")
        if self.origin == "new_design" and not self.design_rationale.strip():
            raise ValueError("new-design arm needs an explicit rationale")
        if self.route_goal.constraint != "open":
            raise ValueError("task decomposition cannot silently lock a route")
        return self


class StageComparisonV1(_Strict):
    comparison_id: str = Field(min_length=1)
    left_arm_id: str = Field(min_length=1)
    right_arm_id: str = Field(min_length=1)
    contrast: str = Field(min_length=1)
    origin: Literal["task_required", "new_design"]
    task_excerpt: str = ""
    design_rationale: str = ""
    # Scientific comparability must be checked using verified, group-scoped
    # facts. An empty list does not assert that two routes are comparable.
    held_constant_field_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_origin(self) -> "StageComparisonV1":
        if self.left_arm_id == self.right_arm_id:
            raise ValueError("comparison requires distinct arms")
        if self.origin == "task_required" and not self.task_excerpt.strip():
            raise ValueError("task-required comparison needs task excerpt")
        if self.origin == "new_design" and not self.design_rationale.strip():
            raise ValueError("new-design comparison needs an explicit rationale")
        if len(set(self.held_constant_field_paths)) != len(self.held_constant_field_paths):
            raise ValueError("held-constant paths must be unique")
        return self


class StageTaskRequirementsV1(_Strict):
    schema_version: Literal["stage-task-requirements/v1"] = (
        "stage-task-requirements/v1"
    )
    query_digest: str = Field(pattern=r"^sha256_[0-9a-f]{64}$")
    observation_point: str = Field(min_length=1)
    stage: StageV2
    arms: list[StageArmV1] = Field(min_length=1)
    comparisons: list[StageComparisonV1] = Field(default_factory=list)
    # A measurement absent from the source paper may be a new experiment. It
    # is kept separate from synthesis-route evidence and must have its own
    # protocol/applicability check before stage completion.
    observation_protocol_status: Literal[
        "not_required", "verified", "runtime_pending", "new_design_pending", "unknown"
    ] = "unknown"
    observation_protocol_id: str = ""
    observation_resolver_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ids(self) -> "StageTaskRequirementsV1":
        arm_ids = [arm.arm_id for arm in self.arms]
        goal_ids = [arm.route_goal.goal_id for arm in self.arms]
        if len(set(arm_ids)) != len(arm_ids) or len(set(goal_ids)) != len(goal_ids):
            raise ValueError("arm IDs and route-goal IDs must be unique")
        if self.stage.observation_point != self.observation_point:
            raise ValueError("stage observation point mismatch")
        for arm in self.arms:
            if len(set(arm.action_intent.comparison_to_arm_ids)) != len(
                arm.action_intent.comparison_to_arm_ids
            ):
                raise ValueError("comparison arm references must be unique")
            if any(
                target not in arm_ids or target == arm.arm_id
                for target in arm.action_intent.comparison_to_arm_ids
            ):
                raise ValueError("action comparison refers to an unknown/self arm")
        if len({item.comparison_id for item in self.comparisons}) != len(self.comparisons):
            raise ValueError("comparison IDs must be unique")
        for item in self.comparisons:
            if item.left_arm_id not in arm_ids or item.right_arm_id not in arm_ids:
                raise ValueError("comparison refers to an unknown arm")
        if self.observation_protocol_status == "verified" and not self.observation_protocol_id:
            raise ValueError("verified observation protocol needs an ID")
        if self.observation_protocol_status == "runtime_pending" and (
            not self.observation_protocol_id
            or not self.observation_resolver_paths
            or any(not path.strip() for path in self.observation_resolver_paths)
            or len(set(self.observation_resolver_paths)) != len(self.observation_resolver_paths)
        ):
            raise ValueError("runtime-pending observation needs protocol ID and resolver paths")
        return self


class StageTaskProductionV1(_Strict):
    status: Literal["ready", "incomplete"]
    requirements: StageTaskRequirementsV1 | None = None
    reason_codes: list[str] = Field(default_factory=list)
    explicit_control_terms: list[str] = Field(default_factory=list)
    variation_excerpts: list[str] = Field(default_factory=list)


class ArmCoverageV1(_Strict):
    arm_id: str
    decision_status: str
    route_id: str = ""
    reason_codes: list[str] = Field(default_factory=list)


class StageCoverageReportV1(_Strict):
    status: Literal["covered", "incomplete"]
    reason_codes: list[str] = Field(default_factory=list)
    arms: list[ArmCoverageV1] = Field(default_factory=list)
    missing_arm_ids: list[str] = Field(default_factory=list)
    unchecked_comparison_ids: list[str] = Field(default_factory=list)
    observation_point: str


class StageQueuedArmV1(_Strict):
    """One selected graph's identity, not a published or executed action."""

    arm_id: str = Field(min_length=1)
    role: Literal["experimental", "control", "repeat", "calibration"]
    route_id: str = Field(min_length=1)
    decision_id: str = Field(pattern=r"^sha256_[0-9a-f]{64}$")
    candidate_digest: str = Field(pattern=r"^sha256_[0-9a-f]{64}$")
    validation_receipt_digest: str = Field(pattern=r"^sha256_[0-9a-f]{64}$")
    source_paper_id: str = ""
    experimental_group_id: str = ""
    source_digest: str = ""
    sample_id: str = Field(min_length=1)
    macro_action_id: str = Field(min_length=1)
    material_graph_digest: str = Field(pattern=r"^sha256_[0-9a-f]{64}$")
    comparison_to_arm_ids: list[str] = Field(default_factory=list)
    publication_status: Literal["planned_only", "research_published"] = "planned_only"
    research_contract_hash: str = ""
    execution_status: Literal["not_executed"] = "not_executed"

    @model_validator(mode="after")
    def validate_publication(self) -> "StageQueuedArmV1":
        if self.publication_status == "research_published" and not self.research_contract_hash:
            raise ValueError("published arm requires Research contract hash")
        if self.publication_status == "planned_only" and self.research_contract_hash:
            raise ValueError("planned arm cannot carry a Research contract hash")
        if bool(self.source_paper_id) != bool(self.experimental_group_id):
            raise ValueError("paper and experimental-group IDs must be paired")
        return self


class StageQueuedComparisonV1(_Strict):
    comparison_id: str = Field(min_length=1)
    left_arm_id: str = Field(min_length=1)
    right_arm_id: str = Field(min_length=1)
    held_constant_field_paths: list[str] = Field(default_factory=list)
    planning_status: Literal["conditions_checked"] = "conditions_checked"
    observation_status: Literal["not_measured"] = "not_measured"


class StageActionQueueReceiptV1(_Strict):
    """Hash-bound stage plan; it never claims that samples were executed."""

    schema_version: Literal["stage-action-queue/v1"] = "stage-action-queue/v1"
    stage_id: str = Field(min_length=1)
    observation_point: str = Field(min_length=1)
    requirements_digest: str = Field(pattern=r"^sha256_[0-9a-f]{64}$")
    coverage_digest: str = Field(pattern=r"^sha256_[0-9a-f]{64}$")
    arms: list[StageQueuedArmV1] = Field(min_length=1)
    comparisons: list[StageQueuedComparisonV1] = Field(default_factory=list)
    planning_status: Literal["coverage_checked"] = "coverage_checked"
    publication_status: Literal[
        "none_published", "partially_published", "all_actions_published"
    ] = "none_published"
    execution_status: Literal["not_executed"] = "not_executed"
    receipt_digest: str = Field(pattern=r"^sha256_[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> "StageActionQueueReceiptV1":
        arm_ids = [item.arm_id for item in self.arms]
        if len(set(arm_ids)) != len(arm_ids):
            raise ValueError("queue arm IDs must be unique")
        published = sum(item.publication_status == "research_published" for item in self.arms)
        expected = (
            "none_published" if published == 0
            else "all_actions_published" if published == len(self.arms)
            else "partially_published"
        )
        if self.publication_status != expected:
            raise ValueError("stage publication status differs from queued arms")
        if self.receipt_digest != canonical_digest(
            self.model_dump(mode="json", exclude={"receipt_digest"})
        ):
            raise ValueError("stage action queue receipt digest mismatch")
        return self


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _explicit_controls(query: str) -> list[str]:
    """Extract coordinated explicit controls, never material-specific names."""

    spans: list[str] = []
    for match in re.finditer(r"(?:作为对照|为对照)", query):
        prefix = re.split(r"[，,。；;.!?]", query[:match.start()])[-1]
        starts = list(re.finditer(r"(?:设置|设有|包括|包含)", prefix))
        if starts:
            spans.append(prefix[starts[-1].end():].strip())
    spans.extend(re.findall(
        r"(?:set(?:ting)? up|include)\s+([^.;!?]{2,100}?)\s+as controls?",
        query, flags=re.IGNORECASE,
    ))
    terms: list[str] = []
    for span in spans:
        span = re.sub(r"^(?:了|一个|一组|the|a)\s*", "", span, flags=re.IGNORECASE)
        for term in re.split(r"\s*(?:、|及|和|以及|与|,|，|\band\b)\s*", span, flags=re.IGNORECASE):
            term = term.strip()
            if term and _compact(term) not in {_compact(item) for item in terms}:
                terms.append(term)
    return terms


def _variation_excerpts(query: str) -> list[str]:
    clauses = re.split(r"[。；;.!?]", query)
    return [
        clause.strip() for clause in clauses
        if re.search(r"(?:不同|差异|different|varying|compare|比较)", clause, re.IGNORECASE)
        and re.search(r"(?:方式|环境|方法|route|method|state|结构|配位)", clause, re.IGNORECASE)
    ]


_MATERIAL_TOKEN = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Za-z0-9]{2,}(?:[-_+/][A-Za-z0-9]+)*)")
_MATERIAL_JOIN = re.compile(
    r"\s*(?:、|及|和|与|,|\band\b|\bvs\.?|\bversus\b)\s*", re.IGNORECASE,
)
_COMPARE_MARKER = re.compile(r"(?:比较|对比|compare|contrast|versus|\bvs\.?\b)", re.I)


def _explicit_coordinated_materials(query: str) -> list[str]:
    """Read only unambiguous formula lists attached to a sample request.

    This is a narrow omission check, not a chemical entity extractor. Other
    wording stays with the task proposer and cannot gain an invented formula.
    """
    terms: list[str] = []
    for clause in re.split(
        r"[。；;.!?]", re.sub(r"\bvs\.", "versus", query, flags=re.I),
    ):
        request = re.search(
            r"(?:制备|合成|准备|prepare|synthesi[sz]e)\s*(.{0,120}?)"
            r"(?:样品|材料|samples?|materials?)",
            clause, re.IGNORECASE,
        )
        spans: list[tuple[str, bool]] = []
        if request is not None:
            spans.append((request.group(1), False))
        if _COMPARE_MARKER.search(clause):
            spans.append((clause[:120], True))
        for span, comparison_only in spans:
            tokens = list(_MATERIAL_TOKEN.finditer(span))
            for left, right in zip(tokens, tokens[1:]):
                if _MATERIAL_JOIN.fullmatch(span[left.end():right.start()]) is None:
                    continue
                pair = (left.group(1), right.group(1))
                # In a bare comparison, all-capitals XRD/XPS/OER are more
                # likely measurements than explicit material identities.
                if comparison_only and not all(
                    any(character.islower() or character.isdigit()
                        for character in token)
                    for token in pair
                ):
                    continue
                for token in pair:
                    if token not in terms:
                        terms.append(token)
    return terms


def _explicit_sample_variants(query: str) -> list[str]:
    """Recognize only explicit short sample labels such as 样品 A 和 B."""
    terms: list[str] = []
    pattern = re.compile(
        r"(?:样品|sample)\s*([A-Z][A-Z0-9]{0,2})\s*"
        r"(?:和|与|、|,|and|/)\s*(?:(?:样品|sample)\s*)?"
        r"([A-Z][A-Z0-9]{0,2})(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    for match in pattern.finditer(query):
        for term in match.groups():
            if term.upper() not in terms:
                terms.append(term.upper())
    return terms


def _explicit_arm_inventory_issues(
    query: str, req: StageTaskRequirementsV1,
) -> list[str]:
    """Abstain when a clearly enumerated material/variant has no distinct arm."""
    issues: list[str] = []
    materials = _explicit_coordinated_materials(query)
    variants = _explicit_sample_variants(query)
    if not materials and not variants:
        return issues

    used: set[str] = set()
    for term in materials:
        matches = [
            arm for arm in req.arms
            if arm.arm_id not in used and arm.origin == "task_required"
            and _compact(term) in {
                _compact(arm.task_term), _compact(arm.route_goal.target.material),
            }
        ]
        if not matches:
            issues.append("explicit_material_arm_missing:" + term)
        else:
            used.add(matches[0].arm_id)
    used.clear()
    for term in variants:
        label = re.compile(
            r"(?:^|[^A-Za-z0-9])" + re.escape(term) + r"(?:$|[^A-Za-z0-9])",
            re.IGNORECASE,
        )
        matches = [
            arm for arm in req.arms
            if arm.arm_id not in used and arm.origin == "task_required"
            and any(label.search(text) for text in (
                arm.arm_id, arm.label, arm.task_term,
            ))
        ]
        if not matches:
            issues.append("explicit_variant_arm_missing:" + term)
        else:
            used.add(matches[0].arm_id)
    if issues:
        issues.insert(0, "task_arm_inventory_unverified")
    return issues


def _explicit_material_comparison_issues(
    query: str, req: StageTaskRequirementsV1,
) -> list[str]:
    """Require a relation when a task asks to compare enumerated materials.

    The task excerpt may name several materials while the model omits every
    comparison. This check uses only the narrow, literal material list above;
    it does not decide which chemical route or measurement is suitable.
    """
    if _COMPARE_MARKER.search(query) is None:
        return []
    materials = _explicit_coordinated_materials(query)
    if len(materials) < 2:
        return []
    arm_ids: list[str] = []
    for material in materials:
        matches = [
            arm.arm_id for arm in req.arms
            if arm.origin == "task_required" and _compact(material) in {
                _compact(arm.task_term), _compact(arm.route_goal.target.material),
            }
        ]
        if len(matches) != 1:
            # The inventory check reports missing or ambiguous arms separately.
            return []
        arm_ids.append(matches[0])
    if len(set(arm_ids)) != len(arm_ids):
        return []
    required = set(arm_ids)
    linked: dict[str, set[str]] = {arm_id: set() for arm_id in arm_ids}
    for comparison in req.comparisons:
        if (
            comparison.origin == "task_required"
            and _COMPARE_MARKER.search(comparison.task_excerpt)
            and comparison.left_arm_id in required
            and comparison.right_arm_id in required
        ):
            linked[comparison.left_arm_id].add(comparison.right_arm_id)
            linked[comparison.right_arm_id].add(comparison.left_arm_id)
    reached = {arm_ids[0]}
    pending = [arm_ids[0]]
    while pending:
        for neighbor in linked[pending.pop()]:
            if neighbor not in reached:
                reached.add(neighbor)
                pending.append(neighbor)
    return [] if reached == required else ["explicit_material_comparison_missing"]


def _grounding_issues(
    query: str, observation_point: str, req: StageTaskRequirementsV1,
    controls: list[str], variation: list[str],
) -> list[str]:
    issues: list[str] = []
    if req.query_digest != canonical_digest(query):
        issues.append("task_query_digest_mismatch")
    if _compact(req.observation_point) != _compact(observation_point):
        issues.append("current_observation_point_mismatch")
    if _compact(req.stage.observation_point) != _compact(observation_point):
        issues.append("stage_observation_point_mismatch")
    for arm in req.arms:
        if arm.origin == "task_required" and (
            arm.task_excerpt not in query or arm.task_term not in arm.task_excerpt
        ):
            issues.append("arm_task_anchor_invalid:" + arm.arm_id)
        if arm.route_goal.target.material not in query and not arm.design_rationale.strip():
            issues.append("arm_target_material_design_unmarked:" + arm.arm_id)
    issues.extend(_explicit_arm_inventory_issues(query, req))
    issues.extend(_explicit_material_comparison_issues(query, req))
    for comparison in req.comparisons:
        if comparison.origin == "task_required" and comparison.task_excerpt not in query:
            issues.append("comparison_task_anchor_invalid:" + comparison.comparison_id)
    for term in controls:
        matching = [
            arm for arm in req.arms
            if arm.role == "control" and arm.origin == "task_required"
            and _compact(term) == _compact(arm.task_term)
        ]
        if not matching:
            issues.append("explicit_control_omitted:" + term)
        for arm in matching:
            if not any(
                arm.arm_id in (item.left_arm_id, item.right_arm_id)
                and item.origin == "task_required"
                for item in req.comparisons
            ):
                issues.append("control_relation_missing:" + arm.arm_id)
    if variation:
        experimentals = [arm for arm in req.arms if arm.role == "experimental"]
        experimental_ids = {arm.arm_id for arm in experimentals}
        if len(experimentals) < 2:
            issues.append("experimental_variation_arms_missing")
        elif not any(
            item.left_arm_id in experimental_ids
            and item.right_arm_id in experimental_ids
            and item.origin == "task_required"
            and any(item.task_excerpt in clause for clause in variation)
            for item in req.comparisons
        ):
            issues.append("experimental_variation_comparison_missing")
    return sorted(set(issues))


def propose_stage_task_requirements(
    query: str,
    observation_point: str,
    proposer: Callable[[str], Mapping[str, Any]] | None,
    *,
    verified_observation_protocol_ids: frozenset[str] = frozenset(),
    trusted_runtime_resolvers: Mapping[str, Sequence[str]] | None = None,
    observation_not_required_verified: bool = False,
) -> StageTaskProductionV1:
    """Produce a typed stage matrix from the original task; abstain on gaps.

    The callback is an isolated proposal mechanism, not an evidence reviewer.
    The production result is only a task decomposition. Scientific route,
    measurement and Device gates retain their own authority.
    """

    controls = _explicit_controls(query)
    variation = _variation_excerpts(query)
    if not query.strip() or not observation_point.strip():
        return StageTaskProductionV1(
            status="incomplete", reason_codes=["task_or_observation_missing"],
            explicit_control_terms=controls, variation_excerpts=variation,
        )
    if proposer is None:
        return StageTaskProductionV1(
            status="incomplete", reason_codes=["task_decomposer_unavailable"],
            explicit_control_terms=controls, variation_excerpts=variation,
        )
    prompt = (
        "Return exactly one JSON object matching the supplied JSON Schema. "
        "Use the schema field names and nesting verbatim; do not invent "
        "parallel keys such as sample_arms, coverage_status or execution_id. "
        "Decompose only the original task up to the CURRENT observation point. "
        "List every required sample arm, explicit control, and comparison. "
        "Do not copy a historical plan or invent numeric synthesis parameters. "
        "For task_required arms, quote an exact task_excerpt and a task_term "
        "contained in it. New arms or measurements must say new_design and "
        "give a rationale. The route_goal.target.material must use the exact "
        "material-family phrase from the task when one exists. Put variant "
        "labels such as A/B in arm_id or label, not in target.material. If "
        "a more specific material target is a new design choice, retain "
        "the task_required origin but provide design_rationale for that "
        "choice. Give each arm an open RouteGoalV1 with a distinct "
        "goal_id. Supply one shared StageV2 and each arm's semantic "
        "action_intent (group_label, comparison_to_arm_ids, objective, expected "
        "observation, completion condition). Do not supply execution IDs, "
        "chemistry operations or numeric parameter values: verified route graphs "
        "provide those only after selection. Comparisons list arm IDs, contrast, task excerpt or design "
        "rationale, and verified held-constant field paths only if known. "
        "Uncertain coverage must remain incomplete. Do not claim an observation "
        "protocol verified or runtime_pending without an independently checked "
        "protocol ID and trusted resolver paths.\n"
        f"Task: {query}\nCurrent observation point: {observation_point}\n"
        f"Explicit control terms to account for: {controls}\n"
        f"Variation clauses to account for: {variation}\n"
        f"query_digest: {canonical_digest(query)}\n"
        "JSON Schema: " + json.dumps(
            StageTaskRequirementsV1.model_json_schema(),
            ensure_ascii=False, separators=(",", ":"),
        )
    )
    try:
        proposed = proposer(prompt)
        req = StageTaskRequirementsV1.model_validate(proposed, strict=True)
    except Exception as exc:
        reasons = ["task_decomposition_invalid:" + type(exc).__name__]
        if isinstance(exc, ValidationError):
            reasons.extend(
                "task_decomposition_schema:" + item["type"] + ":"
                + ".".join(str(part) for part in item["loc"])
                for item in exc.errors()[:12]
            )
        return StageTaskProductionV1(
            status="incomplete", reason_codes=reasons,
            explicit_control_terms=controls, variation_excerpts=variation,
        )
    issues = _grounding_issues(query, observation_point, req, controls, variation)
    if req.observation_protocol_status == "verified" and (
        req.observation_protocol_id not in verified_observation_protocol_ids
    ):
        issues.append("observation_protocol_self_attestation_forbidden")
    if req.observation_protocol_status == "not_required" and not observation_not_required_verified:
        issues.append("observation_not_required_self_attestation_forbidden")
    if req.observation_protocol_status == "runtime_pending" and (
        not trusted_runtime_resolvers
        or tuple(req.observation_resolver_paths)
        != tuple(trusted_runtime_resolvers.get(req.observation_protocol_id, ()))
    ):
        issues.append("observation_runtime_resolver_self_attestation_forbidden")
    return StageTaskProductionV1(
        status="incomplete" if issues else "ready", requirements=req,
        reason_codes=issues, explicit_control_terms=controls,
        variation_excerpts=variation,
    )


def evaluate_stage_coverage(
    requirements: StageTaskRequirementsV1,
    decisions_by_arm: Mapping[str, RouteDecisionV1],
    *,
    verified_observation_protocol_ids: frozenset[str] = frozenset(),
    trusted_runtime_resolvers: Mapping[str, Sequence[str]] | None = None,
    trusted_comparison_fields_by_id: Mapping[str, Sequence[str]] | None = None,
    observation_not_required_verified: bool = False,
) -> StageCoverageReportV1:
    """Assess arms and only independently specified comparison conditions.

    ``held_constant_field_paths`` is proposed task data. A caller must supply
    the independently reviewed required paths; an equal proposed path alone
    cannot certify a comparison. The default therefore leaves comparisons
    pending even when literal values happen to match.
    """

    issues: list[str] = []
    arms: list[ArmCoverageV1] = []
    selected: dict[str, Any] = {}
    missing: list[str] = []
    for arm in requirements.arms:
        decision = decisions_by_arm.get(arm.arm_id)
        if decision is None:
            missing.append(arm.arm_id)
            arms.append(ArmCoverageV1(
                arm_id=arm.arm_id, decision_status="missing",
                reason_codes=["arm_route_decision_missing"],
            ))
            continue
        if decision.goal != arm.route_goal:
            missing.append(arm.arm_id)
            arms.append(ArmCoverageV1(
                arm_id=arm.arm_id, decision_status="mismatched",
                reason_codes=["arm_route_goal_mismatch"],
            ))
            continue
        if decision.status != "selected_for_planning":
            missing.append(arm.arm_id)
            arms.append(ArmCoverageV1(
                arm_id=arm.arm_id, decision_status=decision.status,
                reason_codes=list(decision.decision_reasons),
            ))
            continue
        record = next(
            item for item in decision.candidates
            if item.route_id == decision.selected_route_id
        )
        selected[arm.arm_id] = record
        arms.append(ArmCoverageV1(
            arm_id=arm.arm_id, decision_status=decision.status,
            route_id=record.route_id,
        ))
    if missing:
        issues.append("required_sample_arms_uncovered")
    unchecked: list[str] = []
    for comparison in requirements.comparisons:
        left = selected.get(comparison.left_arm_id)
        right = selected.get(comparison.right_arm_id)
        if left is None or right is None:
            unchecked.append(comparison.comparison_id)
            continue
        if left.route_id == right.route_id or (
            left.candidate.route_signature == right.candidate.route_signature
            and comparison.contrast.strip()
        ):
            issues.append("comparison_contrast_not_established:" + comparison.comparison_id)
            unchecked.append(comparison.comparison_id)
            continue
        if not comparison.held_constant_field_paths:
            issues.append("comparison_conditions_unverified:" + comparison.comparison_id)
            unchecked.append(comparison.comparison_id)
            continue
        trusted_paths = (
            trusted_comparison_fields_by_id.get(comparison.comparison_id)
            if trusted_comparison_fields_by_id is not None else None
        )
        if not (
            isinstance(trusted_paths, (list, tuple))
            and trusted_paths
            and all(isinstance(path, str) and path.strip() for path in trusted_paths)
            and len(set(trusted_paths)) == len(trusted_paths)
            and set(trusted_paths) == set(comparison.held_constant_field_paths)
        ):
            issues.append(
                "comparison_conditions_partially_checked:"
                + comparison.comparison_id
            )
            unchecked.append(comparison.comparison_id)
            continue
        for field_path in comparison.held_constant_field_paths:
            pair = []
            for record in (left, right):
                field = next(
                    (item for item in record.candidate.evidence_matrix
                     if item.field_path == field_path and item.status == "supported"),
                    None,
                )
                if field is None or record.validation is None or (
                    field_path not in record.validation.verified_field_paths
                    and field_path not in record.validation.verified_convention_field_paths
                ):
                    pair = []
                    break
                pair.append((field.value, field.unit))
            if len(pair) != 2 or pair[0] != pair[1]:
                issues.append("comparison_condition_mismatch:" + comparison.comparison_id + ":" + field_path)
                unchecked.append(comparison.comparison_id)
                break
        if (left.candidate.target.desired_state != right.candidate.target.desired_state):
            issues.append("comparison_sample_interface_mismatch:" + comparison.comparison_id)
            unchecked.append(comparison.comparison_id)
    if not (
        requirements.observation_protocol_status == "not_required"
        and observation_not_required_verified
    ) and not (
        requirements.observation_protocol_status == "verified"
        and requirements.observation_protocol_id in verified_observation_protocol_ids
    ) and not (
        requirements.observation_protocol_status == "runtime_pending"
        and bool(trusted_runtime_resolvers)
        and bool(trusted_runtime_resolvers.get(requirements.observation_protocol_id))
        and tuple(requirements.observation_resolver_paths)
        == tuple(trusted_runtime_resolvers[requirements.observation_protocol_id])
    ):
        issues.append("current_observation_protocol_unverified")
    issues = sorted(set(issues))
    return StageCoverageReportV1(
        status="incomplete" if issues else "covered", reason_codes=issues,
        arms=arms, missing_arm_ids=missing,
        unchecked_comparison_ids=sorted(set(unchecked)),
        observation_point=requirements.observation_point,
    )


def _queue_from_payload(payload: dict[str, Any]) -> StageActionQueueReceiptV1:
    payload["receipt_digest"] = canonical_digest(payload)
    return StageActionQueueReceiptV1.model_validate(payload, strict=True)


def compose_stage_action_queue(
    requirements: StageTaskRequirementsV1,
    decisions_by_arm: Mapping[str, RouteDecisionV1],
    coverage: StageCoverageReportV1,
    *,
    verified_observation_protocol_ids: frozenset[str] = frozenset(),
    trusted_runtime_resolvers: Mapping[str, Sequence[str]] | None = None,
    trusted_comparison_fields_by_id: Mapping[str, Sequence[str]] | None = None,
    observation_not_required_verified: bool = False,
) -> StageActionQueueReceiptV1:
    """Freeze a fully covered multi-arm plan without publishing any action.

    This rechecks coverage from the exact decisions and trusted observation
    protocol inputs. Graph IDs and digests are read only from each selected
    candidate. The queue is an index, not a merged chemistry/material graph.
    """

    req = StageTaskRequirementsV1.model_validate(
        requirements.model_dump(mode="json"), strict=True,
    )
    report = StageCoverageReportV1.model_validate(
        coverage.model_dump(mode="json"), strict=True,
    )
    if set(decisions_by_arm) != {arm.arm_id for arm in req.arms}:
        raise ValueError("stage action queue requires exactly one decision per arm")
    decisions = {
        arm_id: RouteDecisionV1.model_validate(
            decision.model_dump(mode="json"), strict=True,
        )
        for arm_id, decision in decisions_by_arm.items()
    }
    expected = evaluate_stage_coverage(
        req, decisions,
        verified_observation_protocol_ids=verified_observation_protocol_ids,
        trusted_runtime_resolvers=trusted_runtime_resolvers,
        trusted_comparison_fields_by_id=trusted_comparison_fields_by_id,
        observation_not_required_verified=observation_not_required_verified,
    )
    if expected.status != "covered" or report != expected:
        raise ValueError("stage action queue requires current complete coverage")
    arm_by_id = {arm.arm_id: arm for arm in req.arms}
    for comparison in req.comparisons:
        left = arm_by_id[comparison.left_arm_id]
        right = arm_by_id[comparison.right_arm_id]
        if (
            right.arm_id not in left.action_intent.comparison_to_arm_ids
            and left.arm_id not in right.action_intent.comparison_to_arm_ids
        ):
            raise ValueError(
                "comparison lacks an action-level arm relation: "
                + comparison.comparison_id
            )

    queued_arms: list[StageQueuedArmV1] = []
    for arm in req.arms:
        decision = decisions[arm.arm_id]
        selected = next(
            item for item in decision.candidates
            if item.route_id == decision.selected_route_id
        )
        receipt = selected.validation
        graph = selected.candidate.material_graph
        if receipt is None or not graph:
            raise ValueError("selected arm has no verified material graph: " + arm.arm_id)
        action_ids = {step.macro_action_id for step in graph}
        sample_ids = {step.sample_id for step in graph}
        graph_step_ids = [step.macro_step_id for step in graph]
        if (
            len(action_ids) != 1 or len(sample_ids) != 1
            or not all(action_ids | sample_ids)
            or graph_step_ids != receipt.verified_graph_step_ids
        ):
            raise ValueError("selected arm graph identity is ambiguous: " + arm.arm_id)
        scope = selected.candidate.source_scope
        queued_arms.append(StageQueuedArmV1(
            arm_id=arm.arm_id, role=arm.role,
            route_id=selected.route_id,
            decision_id=decision.decision_id,
            candidate_digest=selected.candidate_digest,
            validation_receipt_digest=selected.validation_receipt_digest,
            source_paper_id=scope.paper_id if scope else "",
            experimental_group_id=scope.experimental_group_id if scope else "",
            source_digest=scope.source_digest if scope else "",
            sample_id=next(iter(sample_ids)),
            macro_action_id=next(iter(action_ids)),
            material_graph_digest=route_material_graph_digest_v1(graph),
            comparison_to_arm_ids=list(arm.action_intent.comparison_to_arm_ids),
        ))
    if len({arm.sample_id for arm in queued_arms}) != len(queued_arms):
        raise ValueError("stage action queue reuses a sample ID across arms")
    if len({arm.macro_action_id for arm in queued_arms}) != len(queued_arms):
        raise ValueError("stage action queue reuses an action ID across arms")

    comparisons = [StageQueuedComparisonV1(
        comparison_id=item.comparison_id,
        left_arm_id=item.left_arm_id, right_arm_id=item.right_arm_id,
        held_constant_field_paths=list(item.held_constant_field_paths),
    ) for item in req.comparisons]
    return _queue_from_payload({
        "schema_version": "stage-action-queue/v1",
        "stage_id": req.stage.stage_id,
        "observation_point": req.observation_point,
        "requirements_digest": canonical_digest(req),
        "coverage_digest": canonical_digest(report),
        "arms": [item.model_dump(mode="json") for item in queued_arms],
        "comparisons": [item.model_dump(mode="json") for item in comparisons],
        "planning_status": "coverage_checked",
        "publication_status": "none_published",
        "execution_status": "not_executed",
    })


def record_published_stage_action(
    queue: StageActionQueueReceiptV1,
    arm_id: str,
    saved_state: Mapping[str, Any],
) -> StageActionQueueReceiptV1:
    """Record one existing Research publication; never claim lab execution."""

    from chem_agent_contracts.route_saved_state import (
        validate_selected_route_saved_state_v2,
    )

    current = StageActionQueueReceiptV1.model_validate(
        queue.model_dump(mode="json"), strict=True,
    )
    matches = [item for item in current.arms if item.arm_id == arm_id]
    if len(matches) != 1:
        raise ValueError("published arm is not in the stage queue")
    if saved_state.get("status") != "completed" or (
        saved_state.get("route_binding_status_v1") != "publishable"
    ):
        raise ValueError("Research action has not reached published state")
    package = validate_selected_route_saved_state_v2(dict(saved_state))
    binding = package.route_binding
    assert binding is not None
    arm = matches[0]
    if any((left != right) for left, right in (
        (package.stage.stage_id, current.stage_id),
        (package.macro_action.experiment_group.group_id, arm.arm_id),
        (package.macro_action.experiment_group.role, arm.role),
        (package.macro_action.experiment_group.sample_id, arm.sample_id),
        (package.macro_action.macro_action_id, arm.macro_action_id),
        (binding.route_id, arm.route_id),
        (binding.decision_id, arm.decision_id),
        (binding.candidate_digest, arm.candidate_digest),
        (binding.validation_receipt_digest, arm.validation_receipt_digest),
        (binding.source_paper_id, arm.source_paper_id),
        (binding.experimental_group_id, arm.experimental_group_id),
        (binding.source_digest, arm.source_digest),
        (binding.material_graph_digest, arm.material_graph_digest),
    )):
        raise ValueError("published Research action differs from queued arm identity")
    if not package.research_contract_hash:
        raise ValueError("published Research action lacks contract hash")
    if arm.publication_status == "research_published":
        if arm.research_contract_hash != package.research_contract_hash:
            raise ValueError("queued arm has a different published contract hash")
        return current
    payload = current.model_dump(mode="json", exclude={"receipt_digest"})
    for item in payload["arms"]:
        if item["arm_id"] == arm_id:
            item["publication_status"] = "research_published"
            item["research_contract_hash"] = package.research_contract_hash
    published = sum(
        item["publication_status"] == "research_published"
        for item in payload["arms"]
    )
    payload["publication_status"] = (
        "all_actions_published" if published == len(payload["arms"])
        else "partially_published"
    )
    return _queue_from_payload(payload)


__all__ = [
    "StageArmActionIntentV1", "StageArmV1", "StageComparisonV1", "StageTaskRequirementsV1",
    "StageTaskProductionV1", "StageCoverageReportV1",
    "StageQueuedArmV1", "StageQueuedComparisonV1", "StageActionQueueReceiptV1",
    "propose_stage_task_requirements", "evaluate_stage_coverage",
    "compose_stage_action_queue", "record_published_stage_action",
]

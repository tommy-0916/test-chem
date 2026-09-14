"""Tests for the simplified single device agent."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import types
from pathlib import Path

# These tests pin the legacy validation contract (pre Skill-contract gate).
# The evaluation-grade contract audit has its own behaviour and is exercised
# against real workflows offline; here it would reject the minimal canned
# workflows for reasons unrelated to what each test asserts.
os.environ.setdefault("CHEM_DEVICE_CONTRACT_AUDIT", "off")
os.environ.setdefault("CHEM_DEVICE_WORKFLOW_VERIFICATION", "deterministic")
# This large legacy suite pins pre-existing mapper/auditor behaviour.  New
# production campaigns default to the independent full-context semantic LLM;
# its contract is exercised in focused tests that explicitly enable it.
os.environ.setdefault("CHEM_DEVICE_LLM_SEMANTIC_ANALYSIS", "off")
os.environ.setdefault("CHEM_DEVICE_ALLOW_LEGACY_SEMANTICS_FOR_TESTS", "1")

from feasibility_rules import classify_constraint_text
from single_agent import (
    FEASIBILITY_PLAN_TASK_PROMPT,
    SingleDeviceAgent,
    SingleDeviceAgentState,
    _frozen_required_processing_text,
    _required_non_state_workstation_categories,
    _required_state_change_workstation_categories,
    _soft_temporal_mapping_error,
)
from human_quantity_approval import validate_human_quantity_approvals
from utils.workstation_loader import WorkstationLoader
from workflow_validator import WorkflowValidator, structure_validation_errors
from recipe_materializer import (
    RecipeMaterializationError,
    _resolve_spreadsheet_runtime,
    extract_v1_recipe_rows,
    materialize_workflow_recipe_files,
)


class NativeFakeMixin:
    """Legacy payload fixtures explicitly simulate native source discovery."""

    def bind_tools(self, tools, **kwargs):
        from langchain_core.messages import AIMessage

        parent = self

        class Bound:
            loaded = False

            def invoke(self, messages):
                if not self.loaded and kwargs.get("tool_choice") != "none":
                    self.loaded = True
                    calls = [
                        {"name": tool.name, "args": {"station_code": code}, "id": f"fixture-{index}-{code}"}
                        for index, tool in enumerate(tools)
                        for code in (tool.metadata or {}).get("station_codes", [])
                    ]
                    if calls:
                        return AIMessage(content="", tool_calls=calls)
                return parent.invoke(messages)

        return Bound()


class FakeModel(NativeFakeMixin):
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return types.SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


class SequencedFakeModel(NativeFakeMixin):
    def __init__(self, payloads):
        self.payloads = [copy.deepcopy(payload) for payload in payloads]
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.payloads) - 1)
        return types.SimpleNamespace(
            content=json.dumps(self.payloads[index], ensure_ascii=False)
        )


def _semantic_handoff() -> dict:
    return {
        "task": {"query": "完整研究目标：观察样品，不要把状态名误判成操作"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "使用上一阶段的干燥粉末完成观察，本步不得再次干燥",
                "试剂/对象": "样品 A 干燥粉末",
                "参数": "取 20 mg 作为候选观察取样量",
                "quantity_requirements": [
                    {
                        "kind": "semantic_classification_required",
                        "material": "样品 A",
                        "value": 20,
                        "unit": "mg",
                    }
                ],
            }
        ],
    }


def _semantic_contract(*, capabilities: list[str] | None = None, core: bool = False) -> dict:
    categories = capabilities or []
    return {
        "status": "semantic_analysis",
        "macro_step_assessments": [
            {
                "source_macro_step": "1",
                "reason": "结合目标、前序状态与本步用途判断",
                "evidence_refs": [
                    "macro_action_steps[0].操作",
                    "macro_action_steps[0].参数",
                ],
                "required_capabilities": [
                    {
                        "category": category,
                        "reason": "这是本步实际动作",
                        "evidence_refs": ["macro_action_steps[0].操作"],
                    }
                    for category in categories
                ],
                "core_chemistry": {
                    "value": core,
                    "reason": "完整上下文判断",
                    "evidence_refs": ["macro_action_steps[0].操作"],
                },
                "observation_only": {
                    "value": not core,
                    "reason": "完整上下文判断",
                    "evidence_refs": ["macro_action_steps[0].操作"],
                },
                "quantity_semantics": [
                    {
                        "requirement_index": 0,
                        "kind": "target_dose",
                        "material_identity_id": "sample_a",
                        "reason": "观察候选取样量，不是整批实际产量",
                        "evidence_refs": [
                            "macro_action_steps[0].quantity_requirements[0]",
                            "macro_action_steps[0].参数",
                        ],
                    }
                ],
                "material_identities": [
                    {
                        "identity_id": "sample_a",
                        "canonical_name": "样品 A",
                        "role": "sample",
                        "aliases": ["样品 A 干燥粉末"],
                        "reason": "状态词不改变样品身份",
                        "evidence_refs": ["macro_action_steps[0].试剂/对象"],
                    }
                ],
                "joint_requirements": [],
            }
        ],
    }


def test_semantic_analysis_llm_receives_full_research_and_workstation_context() -> None:
    model = FakeModel(_semantic_contract())
    agent = SingleDeviceAgent(model=model)
    state = SingleDeviceAgentState(
        research_handoff=_semantic_handoff(),
        exp_id="semantic-context",
        workstation_descriptions="FULL_WORKSTATION_CONTEXT_MARKER",
    )

    analysis = agent._invoke_semantic_analysis(state)

    assert analysis["analysis_digest"].startswith("semantic_analysis_")
    rendered_prompt = "\n".join(
        str(message.content) for message in model.calls[0]
    )
    assert "完整研究目标" in rendered_prompt
    assert "不得再次干燥" in rendered_prompt
    assert "FULL_WORKSTATION_CONTEXT_MARKER" in rendered_prompt


def test_minimal_semantic_contract_uses_one_step_level_audit_trace() -> None:
    contract = _semantic_contract(capabilities=["drying"], core=True)
    assessment = contract["macro_step_assessments"][0]
    for nested in (
        assessment["core_chemistry"],
        assessment["observation_only"],
        *assessment["required_capabilities"],
        *assessment["quantity_semantics"],
        *assessment["material_identities"],
    ):
        nested.pop("reason", None)
        nested.pop("evidence_refs", None)

    assert SingleDeviceAgent._semantic_analysis_errors(
        _semantic_handoff(), contract
    ) == []

    agent = SingleDeviceAgent(model=FakeModel({}))
    agent._active_semantic_analysis = contract
    updated = agent._apply_semantic_analysis_to_handoff(_semantic_handoff())
    requirement = updated["macro_action_steps"][0]["quantity_requirements"][0]
    assert requirement["semantic_reason"] == assessment["reason"]
    assert requirement["semantic_evidence_refs"] == assessment["evidence_refs"]


def test_material_identity_name_variants_are_audited_not_string_rejected() -> None:
    handoff = _semantic_handoff()
    second_macro = copy.deepcopy(handoff["macro_action_steps"][0])
    second_macro["步骤序号"] = 2
    second_macro["试剂/对象"] = "样品 A 干燥粉末"
    second_macro["quantity_requirements"] = []
    handoff["macro_action_steps"].append(second_macro)

    contract = _semantic_contract()
    first = contract["macro_step_assessments"][0]
    first["material_identities"][0]["canonical_name"] = "样品 A 湿固体"
    second = copy.deepcopy(first)
    second["source_macro_step"] = "2"
    second["evidence_refs"] = ["macro_action_steps[1].操作"]
    second["quantity_semantics"] = []
    second["material_identities"][0]["canonical_name"] = "样品 A 干燥粉末"
    second["material_identities"][0]["evidence_refs"] = [
        "macro_action_steps[1].试剂/对象"
    ]
    contract["macro_step_assessments"].append(second)

    assert SingleDeviceAgent._semantic_analysis_errors(handoff, contract) == []
    normalized = SingleDeviceAgent._normalize_semantic_analysis_contract(
        handoff, contract
    )
    registry = normalized["material_identity_registry"]
    assert registry == [
        {
            "identity_id": "sample_a",
            "canonical_name_variants": ["样品 A 干燥粉末", "样品 A 湿固体"],
            "roles": ["sample"],
            "aliases": ["样品 A 干燥粉末"],
        }
    ]


def test_semantic_evidence_path_is_mechanically_bound_not_llm_required() -> None:
    contract = _semantic_contract()
    assessment = contract["macro_step_assessments"][0]
    assessment["evidence_refs"] = ["not-a-frozen-path"]

    assert SingleDeviceAgent._semantic_analysis_errors(
        _semantic_handoff(), contract
    ) == []
    normalized = SingleDeviceAgent._normalize_semantic_analysis_contract(
        _semantic_handoff(), contract
    )
    normalized_assessment = normalized["macro_step_assessments"][0]
    assert normalized_assessment["evidence_refs"] == ["macro_action_steps[0]"]
    assert normalized_assessment["evidence_binding"] == (
        "mechanically_bound_to_frozen_macro_step"
    )


def test_semantic_contract_keeps_hard_structure_checks() -> None:
    contract = _semantic_contract()
    assessment = contract["macro_step_assessments"][0]
    assessment["core_chemistry"]["value"] = "false"
    assessment["quantity_semantics"][0].pop("material_identity_id")
    assessment["quantity_semantics"][0]["requirement_index"] = 3

    errors = SingleDeviceAgent._semantic_analysis_errors(
        _semantic_handoff(), contract
    )

    assert any("core_chemistry.value must be boolean" in error for error in errors)
    assert any("lacks material_identity_id" in error for error in errors)
    assert any("must cover indices" in error for error in errors)


def test_semantic_contract_repair_receives_previous_candidate_without_memory() -> None:
    invalid_one = _semantic_contract()
    invalid_one["macro_step_assessments"][0]["quantity_semantics"][0].pop(
        "material_identity_id"
    )
    invalid_two = copy.deepcopy(invalid_one)
    valid = _semantic_contract()
    model = SequencedFakeModel([invalid_one, invalid_two, valid])
    agent = SingleDeviceAgent(model=model)
    state = SingleDeviceAgentState(
        research_handoff=_semantic_handoff(),
        exp_id="semantic-memoryless-repair",
        workstation_descriptions="FULL_WORKSTATION_CONTEXT_MARKER",
    )

    analysis = agent._invoke_semantic_analysis(state)

    assert analysis["contract_version"] == "minimal-auditable-v2"
    assert len(model.calls) == 3
    second_prompt = "\n".join(str(item.content) for item in model.calls[1])
    third_prompt = "\n".join(str(item.content) for item in model.calls[2])
    assert "上一份候选 JSON" in second_prompt
    assert "lacks material_identity_id" in second_prompt
    assert "上一份候选 JSON" in third_prompt
    assert "不要依赖此前调用的隐藏记忆" in third_prompt


def test_production_cannot_disable_semantic_llm_with_one_environment_flag() -> None:
    semantic_before = os.environ.get("CHEM_DEVICE_LLM_SEMANTIC_ANALYSIS")
    legacy_before = os.environ.get(
        "CHEM_DEVICE_ALLOW_LEGACY_SEMANTICS_FOR_TESTS"
    )
    try:
        os.environ["CHEM_DEVICE_LLM_SEMANTIC_ANALYSIS"] = "off"
        os.environ.pop(
            "CHEM_DEVICE_ALLOW_LEGACY_SEMANTICS_FOR_TESTS", None
        )
        assert SingleDeviceAgent._llm_semantic_analysis_enabled() is True

        os.environ[
            "CHEM_DEVICE_ALLOW_LEGACY_SEMANTICS_FOR_TESTS"
        ] = "1"
        assert SingleDeviceAgent._llm_semantic_analysis_enabled() is False
    finally:
        if semantic_before is None:
            os.environ.pop("CHEM_DEVICE_LLM_SEMANTIC_ANALYSIS", None)
        else:
            os.environ["CHEM_DEVICE_LLM_SEMANTIC_ANALYSIS"] = semantic_before
        if legacy_before is None:
            os.environ.pop(
                "CHEM_DEVICE_ALLOW_LEGACY_SEMANTICS_FOR_TESTS", None
            )
        else:
            os.environ[
                "CHEM_DEVICE_ALLOW_LEGACY_SEMANTICS_FOR_TESTS"
            ] = legacy_before


def test_active_semantic_contract_replaces_action_keyword_classification() -> None:
    agent = SingleDeviceAgent(model=FakeModel({}))
    handoff = _semantic_handoff()
    # The frozen prose contains “干燥粉末/不得再次干燥”, but the semantic LLM
    # says this is observation-only.  No keyword-derived drying requirement is
    # permitted in the production path.
    agent._active_semantic_analysis = _semantic_contract()
    plan = {
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "General_Material_Station_V1",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_material_identity_ids": ["sample_a"],
            }
        ],
        "material_transitions": [],
    }
    assert agent._frozen_material_transition_coverage_findings(handoff, plan) == []

    # Conversely a generic macro description cannot hide a drying action once
    # the full-context semantic LLM declares it.
    agent._active_semantic_analysis = _semantic_contract(
        capabilities=["drying"], core=True
    )
    findings = agent._frozen_material_transition_coverage_findings(handoff, plan)
    assert findings[0]["missing_processing_categories"] == ["drying"]


def test_semantic_llm_can_reclassify_an_extracted_20mg_without_new_regex() -> None:
    agent = SingleDeviceAgent(model=FakeModel({}))
    analysis = _semantic_contract()
    quantity_semantic = analysis["macro_step_assessments"][0][
        "quantity_semantics"
    ][0]
    quantity_semantic["kind"] = "whole_batch"
    quantity_semantic["reason"] = (
        "下游工作站不需要定量质量；20 mg 只是 Research 提出的候选取样值"
    )
    agent._active_semantic_analysis = analysis

    updated = agent._apply_semantic_analysis_to_handoff(_semantic_handoff())
    requirement = updated["macro_action_steps"][0]["quantity_requirements"][0]

    assert requirement["llm_original_kind"] == "semantic_classification_required"
    assert requirement["kind"] == "whole_batch"
    assert requirement["owner"] == "process_flow"
    assert requirement["required_by"] == "llm_semantic_review"
    # Keep the original number only as provenance; its semantic kind no longer
    # makes it a required batch inventory or a workstation parameter.
    assert requirement["value"] == 20


def test_active_semantic_contract_controls_core_handoff_and_material_identity() -> None:
    agent = SingleDeviceAgent(model=FakeModel({}))
    handoff = _semantic_handoff()
    state = SingleDeviceAgentState(research_handoff=handoff, exp_id="semantic-core")
    agent._active_semantic_analysis = _semantic_contract()
    plan = {
        "offline_handoffs": [
            {
                "name": "文字中提及还原历史，但这里只回传观察数据",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_material_identity_ids": ["sample_a"],
                "semantic_classification": "observation_data_return",
                "semantic_reason": "完整上下文判断为数据回传",
                "semantic_evidence_refs": ["macro_action_steps[0].操作"],
                "material_operation_kind": "none",
            }
        ],
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "General_Material_Station_V1",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                # Deliberately contradictory free text: structured IDs are the
                # only production identity contract.
                "source_reagent_identity": "完全不同的自由文本名称",
                "source_material_identity_ids": ["sample_a"],
            }
        ],
    }
    assert agent._audit_core_chemistry_offline_handoffs(state, plan) == []
    assert agent._device_plan_research_alignment_errors(
        handoff, plan, semantic_analysis=agent._active_semantic_analysis
    ) == []

    plan["offline_handoffs"][0]["semantic_classification"] = (
        "core_chemistry_execution"
    )
    assert agent._audit_core_chemistry_offline_handoffs(state, plan)[0][
        "type"
    ] == "illegal_core_chemistry_offline_handoff"
    plan["device_plan"][0]["source_material_identity_ids"] = ["wrong_id"]
    assert agent._device_plan_research_alignment_errors(
        handoff, plan, semantic_analysis=agent._active_semantic_analysis
    )


def test_plan_rewrite_cannot_change_frozen_llm_material_identity_ids() -> None:
    previous = {
        "device_plan": [
            {
                "plan_step": 1,
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_material_identity_ids": ["sample_a"],
            }
        ]
    }
    repaired = copy.deepcopy(previous)
    repaired["device_plan"][0]["source_material_identity_ids"] = ["sample_b"]

    errors = SingleDeviceAgent._repaired_plan_source_binding_errors(
        previous, repaired
    )

    assert any("material identity IDs" in error for error in errors)


BATCHED_SUCCESS_PAYLOAD = {
    "status": "success",
    "feasibility": {
        "is_feasible": True,
        "blocking_constraints": [],
        "device_layer_adaptations": ["同一进样瓶上采用交替分批节拍"],
    },
    "device_self_check": {
        "container_continuity": "pass",
        "volume_and_capacity": "pass",
        "centrifuge_balancing": "not_applicable",
        "addition_mode_preserved": "pass",
        "workstation_constraints": "pass",
    },
    "workflow_txt": "分批加液与批次间搅拌",
    "workflow_json": {
        "steps": [
            {
                "step_number": 1,
                "workstation": "液体进样站",
                "operation": "加液",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器编号": [1],
                    "原液编号": [1],
                    "原液量": [[1.0]],
                },
                "source_plan_step": 1,
                "source_macro_step": 1,
                "source_macro_steps": [1],
            },
            {
                "step_number": 2,
                "workstation": "磁力搅拌工作站",
                "operation": "磁力搅拌",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器编号": [1],
                    "搅拌速度": 700,
                    "搅拌时间（分钟）": 10,
                },
                "source_plan_step": 2,
                "source_macro_step": 1,
                "source_macro_steps": [1],
            },
            {
                "step_number": 3,
                "workstation": "液体进样站",
                "operation": "加液",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器编号": [1],
                    "原液编号": [1],
                    "原液量": [[1.0]],
                },
                "source_plan_step": 1,
                "source_macro_step": 1,
                "source_macro_steps": [1],
            },
        ],
        "temporal_adaptations": [
            {
                "original_requirement": "边滴入边搅拌",
                "execution_fidelity": "approximated",
                "adaptation_schedule": "加液 -> 搅拌 -> 加液 -> 搅拌",
                "requires_scientific_review": True,
            }
        ],
    },
    # two-stage: stage-1 plan carries temporal_adaptations at top level; the
    # merge sources them from the plan, so the same single payload works as
    # both plan (top-level) and translation (workflow_json.steps).
    "temporal_adaptations": [
        {
            "original_requirement": "边滴入边搅拌",
            "execution_fidelity": "approximated",
            "adaptation_schedule": "加液 -> 搅拌 -> 加液 -> 搅拌",
            "requires_scientific_review": True,
        }
    ],
    "device_plan": [
        {"plan_step": 1, "workstation": "Liquid_Handling_Station_1ml_V2",
         "objective": "分批加液", "operation_intent": "加液",
         "source_macro_step": 1, "source_macro_steps": [1],
         "source_reagent_identity": "B 液"},
        {"plan_step": 2,
         "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
         "objective": "批次间搅拌", "operation_intent": "磁力搅拌",
         "source_macro_step": 1, "source_macro_steps": [1],
         "source_reagent_identity": "B 液"},
    ],
    "quantity_adjustments": [],
    "batch_plan": [
        {
            "batch_id": "B_solution_batch_1",
            "material_id": "B 液",
            "research_material_identity": "B 液",
            "research_source_refs": [
                {
                    "source_path": "macro_action_steps[0].参数",
                    "source_macro_step": 1,
                    "source_field": "参数",
                    "source_context": "B 液 10 mL",
                    "material_identity": "B 液",
                    "quantity": {"value": 10, "unit": "mL"},
                }
            ],
            "sample_id": "sample_1",
            "is_root_batch": True,
            "consumer_ids": ["reaction_1"],
            "allocation": {"reaction_1": {"value": 10, "unit": "mL"}},
            "total_quantity": {"value": 10, "unit": "mL"},
            "per_batch_quantity": {"value": 10, "unit": "mL"},
            "multiplicity": 1,
            "source_kind": "research",
            "source_refs": ["macro_action_steps[0].参数"],
            "calculation": "10 mL × 1 batch = 10 mL",
        }
    ],
    "material_ledger": {
        "entries": [
            {
                "entry_id": "B_solution_draw_1",
                "material_id": "B 液",
                "batch_id": "B_solution_batch_1",
                "sample_id": "sample_1",
                "consumer_id": "reaction_1",
                "produced": {"value": 10, "unit": "mL"},
                "consumed": {"value": 10, "unit": "mL"},
                "reserved": {"value": 0, "unit": "mL"},
                "balance": {"value": 0, "unit": "mL"},
                "source_kind": "research",
                "source_refs": ["macro_action_steps[0].参数"],
                "calculation": "10 mL produced - 10 mL consumed = 0 mL",
            }
        ]
    },
}


class TemporalRetryModel(NativeFakeMixin):
    """First reports the historical false negative, then accepts batching."""

    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        prompt = "\n\n".join(getattr(message, "content", "") for message in messages)
        self.calls.append(prompt)
        if len(self.calls) == 1:
            payload = {
                "feedback_type": "device_feasibility_error",
                "status": "feasibility_error",
                "feasibility": {
                    "is_feasible": False,
                    "blocking_constraints": ["设备无法在同一工作站同时边滴入边搅拌"],
                },
            }
        else:
            payload = BATCHED_SUCCESS_PAYLOAD
        return types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


class GenericComplaintRetryModel(NativeFakeMixin):
    """A NON-temporal wording of a soft complaint, then success on retry.

    Regression for the review finding: any phrasing without hard capability
    evidence must trigger the adaptation retry, not just the temporal
    keyword special case.
    """

    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        prompt = "\n\n".join(getattr(message, "content", "") for message in messages)
        self.calls.append(prompt)
        if len(self.calls) == 1:
            payload = {
                "feedback_type": "device_feasibility_error",
                "status": "feasibility_error",
                "feasibility": {
                    "is_feasible": False,
                    "blocking_constraints": [
                        "宏观计划要求的加料节奏在真源操作列表中没有单一对应操作"
                    ],
                },
            }
        else:
            payload = BATCHED_SUCCESS_PAYLOAD
        return types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


class FakeWorkstationLoader:
    def _native_source_loader(self):
        if not hasattr(self, "_native_source"):
            self._native_source = WorkstationLoader()
        return self._native_source

    def capability_catalog(self):
        return self._native_source_loader().capability_catalog()

    def refresh_truth_snapshot(self):
        return self._native_source_loader().refresh_truth_snapshot()

    def assert_snapshot_current(self):
        return self._native_source_loader().assert_snapshot_current()

    def truth_source_root(self):
        return self._native_source_loader().truth_source_root()

    def global_rules_for_prompt(self):
        return self._native_source_loader().global_rules_for_prompt()

    def format_capability_catalog(self):
        return self._native_source_loader().format_capability_catalog()

    def resolve_station_code(self, name):
        return self._native_source_loader().resolve_station_code(name)

    def load_workstation_skill(self, station_code):
        return self._native_source_loader().load_workstation_skill(station_code)

    def truth_source_digest(self):
        return self._native_source_loader().truth_source_digest()

    def format_for_prompt(self):
        return """
## material-workstation
### USAGE
支持进样瓶。
### AUDIT-RULES
容器类型必须来自支持列表。

## liquid-dispensing-workstation
### USAGE
支持开盖、加液、关盖。

## dryer-workstation
### USAGE
支持静置烘干，温度 26-210 C。
""".strip()


class FullContextTrackingLoader(FakeWorkstationLoader):
    def __init__(self):
        self.full_calls = 0
        self.relevant_calls = 0

    def format_complete_for_workflow_review(self):
        self.full_calls += 1
        return "ROOT_RULE_MARKER\nALL_45_WORKSTATION_SKILLS_MARKER"

    def format_relevant_for_prompt(self, query_text):
        self.relevant_calls += 1
        return "RELEVANT_ONLY_MARKER"


class CaptureHardFailureModel(NativeFakeMixin):
    def __init__(self):
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(
            "\n\n".join(getattr(message, "content", "") for message in messages)
        )
        payload = {
            "feedback_type": "device_feasibility_error",
            "status": "feasibility_error",
            "feasibility": {
                "is_feasible": False,
                "blocking_constraints": ["缺少反应釜"],
            },
        }
        return types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


class RaisingModel(NativeFakeMixin):
    def invoke(self, messages):
        raise TimeoutError("simulated gateway timeout")


class RawSequencedModel(NativeFakeMixin):
    """Return raw response strings so parser/retry behavior is testable."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, messages):
        index = min(len(self.calls), len(self.responses) - 1)
        self.calls.append(messages)
        return types.SimpleNamespace(content=self.responses[index])


RESEARCH_HANDOFF = {
    "task": {"query": "测试 query", "current_stage": "合成并 XRD"},
    "macro_action_steps": [
        {
            "步骤序号": 1,
            "操作": "准备实验容器",
            "试剂/对象": "",
            "参数": "建立可执行容器输入",
        }
    ],
}


def _assert_parse_failure(agent, content, expected_text):
    try:
        agent._parse_json(content)
    except ValueError as exc:
        assert expected_text in str(exc)
        return
    raise AssertionError(f"expected JSON parse failure containing {expected_text!r}")


def test_device_json_parser_accepts_one_exact_fenced_or_prose_wrapped_value():
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )

    assert agent._parse_json('{"status":"ok"}') == {"status": "ok"}
    assert agent._parse_json(
        '```json\n{"status":"ok","note":"brace { inside string }"}\n```'
    ) == {"status": "ok", "note": "brace { inside string }"}
    assert agent._parse_json(
        'Here is the only result:\n{"status":"ok"}\nEnd of result.'
    ) == {"status": "ok"}
    assert agent._parse_json(
        '[source](https://example.invalid) Use {example}:\n{"status":"ok"}'
    ) == {"status": "ok"}


def test_device_json_parser_rejects_ambiguous_or_trailing_structured_output():
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )

    _assert_parse_failure(
        agent,
        '{"status":"first"}{"status":"second"}',
        "multiple complete top-level",
    )
    _assert_parse_failure(
        agent,
        '```json\n{"status":"first"}\n```\n```json\n{"status":"second"}\n```',
        "multiple complete top-level",
    )
    _assert_parse_failure(
        agent,
        '{"status":"ok"}\n{"status":',
        "additional malformed JSON-like fragment",
    )
    _assert_parse_failure(
        agent,
        '{"status":"ok"} trailing garbage',
        "same-line trailing content",
    )
    for trailing_value in ("]", "true", "false", "null", "42", '"second"'):
        _assert_parse_failure(
            agent,
            '{"status":"ok"}\n' + trailing_value,
            (
                "unmatched closing delimiter"
                if trailing_value == "]"
                else "additional complete top-level"
            ),
        )
    for non_object_root in ("[]", "true", "null", "42", '"only string"'):
        _assert_parse_failure(
            agent,
            non_object_root,
            "root must be exactly one JSON object",
        )


def test_feasibility_json_format_retry_discards_ambiguous_response_once():
    raw_marker = "DO_NOT_LOG_RAW_SECRET_MARKER"
    clean = {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": [],
    }
    model = RawSequencedModel(
        [
            json.dumps({"status": "device_plan", "nonce": raw_marker})
            + json.dumps({"status": "feasibility_error"}),
            json.dumps(clean),
        ]
    )
    agent = SingleDeviceAgent(
        model=model, workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="json_format_retry_once",
        workstation_descriptions=FakeWorkstationLoader().format_for_prompt(),
    )

    result = agent._invoke_feasibility_plan(state)

    assert result == clean
    assert len(model.calls) == 2
    retry_prompt = "\n".join(
        getattr(message, "content", "") for message in model.calls[1]
    )
    assert "只输出一个" in retry_prompt
    assert "Device 同层格式重试" in retry_prompt
    assert state.device_plan_rewrite_count == 0
    assert state.workflow_repair_cycles == []
    logs = "\n".join(state.logs)
    assert "response_len=" in logs
    assert "sha256=" in logs
    assert raw_marker not in logs


def test_feasibility_json_format_second_retry_can_recover_without_spending_candidate():
    raw_marker = "DO_NOT_LOG_SECOND_RETRY_RAW_MARKER"
    clean = {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": [],
    }
    ambiguous = (
        json.dumps({"status": "device_plan", "nonce": raw_marker})
        + json.dumps({"status": "feasibility_error"})
    )
    model = RawSequencedModel(
        [ambiguous, "I cannot reconstruct the previous response.", json.dumps(clean)]
    )
    agent = SingleDeviceAgent(
        model=model, workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="json_format_second_retry",
        workstation_descriptions=FakeWorkstationLoader().format_for_prompt(),
    )

    result = agent._invoke_feasibility_plan(state)

    assert result == clean
    assert len(model.calls) == 3
    second_retry_prompt = "\n".join(
        getattr(message, "content", "") for message in model.calls[2]
    )
    assert "最后一次 Device 同层序列化重试" in second_retry_prompt
    assert "不要声称看不到上一响应" in second_retry_prompt
    assert state.device_plan_rewrite_count == 0
    assert state.workflow_repair_cycles == []
    serialized = json.dumps(state.logs, ensure_ascii=False)
    assert raw_marker not in serialized


def test_feasibility_json_format_retry_exhaustion_fails_closed_inside_device():
    raw_marker = "DO_NOT_REPORT_RAW_SECRET_MARKER"
    ambiguous = (
        json.dumps({"status": "device_plan", "nonce": raw_marker})
        + json.dumps({"status": "feasibility_error"})
    )
    model = RawSequencedModel([ambiguous, ambiguous, ambiguous, '{"unused":true}'])
    agent = SingleDeviceAgent(model=model, workstation_loader=FakeWorkstationLoader())

    state = agent.run_state(RESEARCH_HANDOFF, exp_id="json_format_retry_exhausted")
    package = state.terminal_package

    assert len(model.calls) == 3
    assert package["status"] == "failed"
    assert package["feedback_type"] == "device_internal_error"
    assert package["feedback_route"] == "device"
    assert package["failure_scope"] == "device_internal"
    assert package["feasibility_accepted"] is False
    assert package["feasibility_certificate"] == {}
    serialized = json.dumps(
        {"package": package, "logs": state.logs, "raw": state.raw_llm_output},
        ensure_ascii=False,
    )
    assert raw_marker not in serialized
    assert "response_len=" in serialized
    assert "sha256=" in serialized


def _common_device_plan():
    return [
        {
            "plan_step": 1,
            "workstation": "General_Material_Station_V1",
            "objective": "准备实验容器",
            "operation_intent": "物料拿取",
            "containers": {"容器类型": "进样瓶", "容器编号": [1]},
            "source_macro_step": 1,
            "source_reagent_identity": "",
        }
    ]


def _with_common_plan(payload):
    """Upgrade a legacy all-in-one success fixture to the two-stage contract."""
    upgraded = json.loads(json.dumps(payload, ensure_ascii=False))
    if str(upgraded.get("status", "")).lower() in {"success", "device_plan"}:
        upgraded.setdefault("device_plan", _common_device_plan())
        upgraded.setdefault("offline_handoffs", [])
    return upgraded


def test_device_planning_discovers_catalog_and_loads_complete_selected_truth():
    loader = FullContextTrackingLoader()
    model = CaptureHardFailureModel()
    agent = SingleDeviceAgent(model=model, workstation_loader=loader)

    agent.run_state(RESEARCH_HANDOFF, exp_id="full_context_default")

    assert loader.full_calls == 0
    assert loader.relevant_calls == 0
    assert "全部工作站能力目录" in model.prompts[0]
    assert "load_workstation_skill" in model.prompts[0]
    assert len(agent._skill_session.loaded) == 45
    assert "RELEVANT_ONLY_MARKER" not in model.prompts[0]
    assert "用户提供的刚性基底/载体" in model.prompts[0]
    assert "不得先取得空瓶" in model.prompts[0]
    assert "专用容器视为可供给输入" in model.prompts[0]
    assert "自动反应结束后产物仍负载在刚性载体" in model.prompts[0]
    assert "offline_handoff 不是工作站" in model.prompts[0]
    assert "反应管冷却不得虚构不兼容工作站" in model.prompts[0]
    assert "不能用等比例缩小改变名义总量" in model.prompts[0]
    assert "4.0+4.0 mL" in model.prompts[0]
    assert "parent_batch_id" in model.prompts[0]
    assert "无需物理重新合并" in model.prompts[0]
    assert "原液编号是工作站本地槽位" in model.prompts[0]
    assert "reagent_slot_plan 每项必须写明" in model.prompts[0]
    assert "600 rpm 预混20 min后水热" in model.prompts[0]
    assert "全量固体/全量悬浊液合并" in model.prompts[0]
    assert "sample_id -> batch_index -> slot" in model.prompts[0]
    assert "source container -> sample_id" in model.prompts[0]
    assert "sample_container_lineage" in model.prompts[0]
    assert "耐压反应管 -> 进样瓶" in model.prompts[0]
    assert "trace_complete=true" in model.prompts[0]
    assert "禁止生成一个随后会丢字段的 XRD operation" in model.prompts[0]
    assert "后续还存在关盖" in model.prompts[0]


def test_offline_handoff_pseudo_station_is_moved_to_plan_metadata():
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=FakeWorkstationLoader(),
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="normalize_handoff",
    )
    plan = {
        "status": "device_plan",
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "离线输入handoff",
                "objective": "建立已处理刚性载体输入",
                "key_values": {"样品状态": "已洗涤干燥"},
                "containers": {"容器类型": "进样瓶", "容器编号": [1]},
                "notes": "由真实能力缺口边界返回",
            },
            {
                "plan_step": 2,
                "workstation": "X射线衍射仪_V1",
                "objective": "XRD",
            },
        ],
        "offline_handoffs": [],
    }

    normalized = agent._normalize_plan_handoff_steps(state, plan)

    assert [step["workstation"] for step in normalized["device_plan"]] == [
        "X射线衍射仪_V1"
    ]
    assert normalized["offline_handoffs"][0]["name"] == "建立已处理刚性载体输入"
    assert "样品状态=已洗涤干燥" in normalized["offline_handoffs"][0][
        "required_return_data"
    ]


def test_plan_recipe_audit_rejects_runtime_unknown_mass_and_accepts_exact_rows():
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=FakeWorkstationLoader(),
    )
    vague = {
        "device_plan": [
            {
                "plan_step": 29,
                "workstation": "多通道固体称量工作站_V1",
                "operation_intent": "定量称量",
                "key_values": {
                    "Ni-only和Fe-only": "分别按实测干粉量定量",
                    "Ni/Fe-PM": "Ni:Fe=3:1",
                    "料罐号": "1,2",
                },
            }
        ]
    }
    exact = {
        "device_plan": [
            {
                "plan_step": 29,
                "workstation": "多通道固体称量工作站_V1",
                "operation_intent": "定量称量",
                "notes": (
                    "瓶4: 加样量=0.0278 g, 料罐号=1；"
                    "瓶4: 加样量=0.0089 g, 料罐号=2"
                ),
            }
        ]
    }

    assert agent._audit_plan_recipe_evidence(vague)[0]["type"] == (
        "missing_concrete_recipe_evidence"
    )
    assert agent._audit_plan_recipe_evidence(exact) == []


def _a01_stage1_recipe_replay_fixture():
    """The four recipe steps from the 20260826-230413 A01 terminal package."""

    def recipe_file(name, targets, masses, hopper):
        return {
            "文件": name,
            "目标容器顺序": targets,
            "配方行": [
                {
                    "瓶号": index,
                    "加样量(g)": mass,
                    "料罐号": hopper,
                }
                for index, mass in enumerate(masses, 1)
            ],
        }

    step12 = {
        "plan_step": 12,
        "workstation": "多通道固体称量工作站_V1",
        "operation_intent": "固体进样-文件传参-机器人",
        "key_values": {
            "文件配方": [
                recipe_file("NI_R1_to_POST_CTRL", ["V19", "V22"], [0.005, 0.005], 4),
                recipe_file("NI_R2_to_POST_CTRL", ["V20", "V23"], [0.005, 0.005], 5),
                recipe_file("NI_R3_to_POST_CTRL", ["V21", "V24"], [0.005, 0.005], 6),
            ],
            "确定总消耗": "H04、H05、H06各0.010 g",
        },
        "notes": "翻译为三个文件执行；每个文件瓶号从1连续编号且文件内料罐号统一。",
    }
    step24 = {
        "plan_step": 24,
        "workstation": "多通道固体称量工作站_V1",
        "operation_intent": "固体进样-文件传参-机器人",
        "key_values": {
            "文件配方": [
                recipe_file("NI_R1_to_PHYSMIX_R1", ["V25"], [0.015], 4),
                recipe_file("NI_R2_to_PHYSMIX_R2", ["V26"], [0.015], 5),
                recipe_file("NI_R3_to_PHYSMIX_R3", ["V27"], [0.015], 6),
            ],
            "累计母体消耗": "步骤12与本步骤合计，料罐4、5、6各消耗0.025 g",
        },
        "notes": "翻译为三个单行文件执行；每个文件均有确定瓶号、质量和料罐号。",
    }
    step25 = {
        "plan_step": 25,
        "workstation": "多通道固体称量工作站_V1",
        "operation_intent": "固体进样-文件传参-机器人",
        "key_values": {
            "文件配方": [
                recipe_file("FE_R1_to_PHYSMIX_R1", ["V25"], [0.0048], 7),
                recipe_file("FE_R2_to_PHYSMIX_R2", ["V26"], [0.0048], 8),
                recipe_file("FE_R3_to_PHYSMIX_R3", ["V27"], [0.0048], 9),
            ],
            "每个物理混合样品总固体": "0.0198 g",
        },
        "notes": "翻译为三个单行文件执行；每个文件均有确定瓶号、质量和料罐号。",
    }
    xrd_rows = [
        ("XRD_S_NIFE_R1", "V28", 1),
        ("XRD_S_NIFE_R2", "V29", 2),
        ("XRD_S_NIFE_R3", "V30", 3),
        ("XRD_S_POSTFE_R1", "V31", 10),
        ("XRD_S_POSTFE_R2", "V32", 11),
        ("XRD_S_POSTFE_R3", "V33", 12),
        ("XRD_S_NICTRL_R1", "V34", 13),
        ("XRD_S_NICTRL_R2", "V35", 14),
        ("XRD_S_NICTRL_R3", "V36", 15),
        ("XRD_S_PHYSMIX_R1", "V37", 16),
        ("XRD_S_PHYSMIX_R2", "V38", 17),
        ("XRD_S_PHYSMIX_R3", "V39", 18),
        ("XRD_S_FEOXHY_R1", "V40", 7),
        ("XRD_S_FEOXHY_R2", "V41", 8),
        ("XRD_S_FEOXHY_R3", "V42", 9),
    ]
    step36 = {
        "plan_step": 36,
        "workstation": "多通道固体称量工作站_V1",
        "operation_intent": "固体进样-文件传参-机器人",
        "key_values": {
            "文件配方": [
                recipe_file(name, [target], [0.002], hopper)
                for name, target, hopper in xrd_rows
            ],
            "每个目标瓶固体质量": "0.002 g",
        },
        "notes": (
            "翻译为15个单行文件执行；全部目标瓶均有确定瓶号、加样量和数值料罐号，"
            "不使用运行时未知配方。"
        ),
    }
    return {"device_plan": [step12, step24, step25, step36]}


def test_plan_recipe_audit_replays_a01_nested_rows_without_negation_false_positive():
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=FakeWorkstationLoader(),
    )

    assert agent._audit_plan_recipe_evidence(_a01_stage1_recipe_replay_fixture()) == []


def test_plan_recipe_audit_rejects_malformed_structured_rows():
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=FakeWorkstationLoader(),
    )

    invalid_rows = [
        {"瓶号": 1, "加样量(g)": 0.005},
        {"瓶号": 1, "加样量(g)": "0.005", "料罐号": 4},
        {"瓶号": 1, "加样量(g)": "按实测质量", "料罐号": 4},
        {"瓶号": "1", "加样量(g)": 0.005, "料罐号": 4},
        {"瓶号": 1, "加样量(g)": 0.005, "料罐号": "运行时确定"},
        {"瓶号": 1, "加样量(g)": float("nan"), "料罐号": 4},
    ]
    for row in invalid_rows:
        plan = {
            "device_plan": [
                {
                    "plan_step": 12,
                    "workstation": "多通道固体称量工作站_V1",
                    "operation_intent": "固体进样-文件传参-机器人",
                    "key_values": {"文件配方": [{"配方行": [row]}]},
                    "notes": "瓶1: 加样量=0.005 g, 料罐号=4",
                }
            ]
        }
        findings = agent._audit_plan_recipe_evidence(plan)
        assert [item["type"] for item in findings] == [
            "missing_concrete_recipe_evidence"
        ]


def test_plan_semantic_audit_rejects_conditional_premix_omission():
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=FakeWorkstationLoader(),
    )
    invalid = {
        "device_plan": [
            {
                "plan_step": 2,
                "workstation": "常温磁力搅拌工作站_V1",
                "objective": "反应前预混20 min",
                "key_values": {"搅拌时间": "20min"},
                "notes": (
                    "若该站不接受10ml耐压反应管，则省略此非必要混合步骤，"
                    "由反应平台内置搅拌完成"
                ),
            }
        ]
    }
    valid = {
        "device_plan": [
            {
                "plan_step": 2,
                "workstation": "常温磁力搅拌工作站_V1",
                "objective": "在50ml耐热瓶中反应前预混20 min",
                "key_values": {"搅拌时间": "20min"},
                "notes": "预混完成后经最短转移链进入10ml耐压反应管",
            }
        ]
    }

    finding = agent._audit_plan_semantic_omissions(invalid)[0]
    assert finding["type"] == "conditional_macro_semantic_omission"
    assert agent._audit_plan_semantic_omissions(valid) == []


def test_full_checks_treat_string_dispatch_drop_warning_as_hard_error():
    loader = WorkstationLoader(use_new_format=True)
    agent = SingleDeviceAgent(model=RaisingModel(), workstation_loader=loader)
    agent._contract_gate_enabled = lambda: True
    workflow = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "Liquid_Handling_Station_1ml_V2",
                "operation": "关盖",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "关盖编号": [{"瓶号": 1}],
                    "保留瓶盖": 1,
                },
            }
        ]
    }
    result = {"workflow_json": workflow, "workflow_txt": agent._workflow_txt_from_json(workflow)}

    report = agent._run_full_checks(result)

    assert report["status"] == "failed"
    assert any("dispatch_parameter_dropped" in item for item in report["errors"])


def test_full_checks_require_exact_workflow_coverage_of_every_plan_step():
    loader = WorkstationLoader(use_new_format=True)
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=loader
    )
    device_plan = [
        {
            "plan_step": 1,
            "workstation": "General_Material_Station_V1",
            "operation_intent": "物料拿取",
            "source_macro_step": 1,
            "source_macro_steps": [1],
        },
        {
            "plan_step": 2,
            "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
            "operation_intent": "磁力搅拌",
            "source_macro_step": 2,
            "source_macro_steps": [2],
        },
    ]
    first_only = _good_material_step()
    first_only.update(
        {
            "source_plan_step": 1,
            "source_macro_step": 1,
            "source_macro_steps": [1],
        }
    )
    result = {
        "device_plan": device_plan,
        "workflow_json": {"steps": [first_only]},
        "workflow_txt": "第1步 General_Material_Station_V1：物料拿取",
    }
    for contract_enabled in (False, True):
        agent._contract_gate_enabled = lambda enabled=contract_enabled: enabled
        report = agent._run_full_checks(copy.deepcopy(result))
        assert report["status"] == "failed"
        assert any(
            "missing_device_plan_workflow_coverage" in error
            for error in report["errors"]
        )


def test_workflow_plan_trace_rejects_wrong_station_and_auxiliary_only_forgery():
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )
    device_plan = [
        {
            "plan_step": 1,
            "workstation": "General_Material_Station_V1",
            "operation_intent": "物料拿取",
            "source_macro_step": 1,
            "source_macro_steps": [1],
        },
        {
            "plan_step": 2,
            "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
            "operation_intent": "磁力搅拌",
            "source_macro_step": 2,
            "source_macro_steps": [2],
        },
    ]
    wrong_station = _good_material_step()
    wrong_station.update(
        {
            "step_number": 2,
            "source_plan_step": 2,
            "source_macro_step": 2,
            "source_macro_steps": [2],
        }
    )
    first = _good_material_step()
    first.update(
        {
            "source_plan_step": 1,
            "source_macro_step": 1,
            "source_macro_steps": [1],
        }
    )
    wrong_errors = agent._workflow_plan_step_trace_errors(
        {
            "device_plan": device_plan,
            "workflow_json": {"steps": [first, wrong_station]},
        }
    )
    assert any(
        "workflow_plan_step_workstation_mismatch" in error
        for error in wrong_errors
    )

    auxiliary_only = {
        "step_number": 2,
        "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
        "operation": "开盖",
        "parameters": {},
        "source_plan_step": 2,
        "source_macro_step": 2,
        "source_macro_steps": [2],
    }
    auxiliary_errors = agent._workflow_plan_step_trace_errors(
        {
            "device_plan": device_plan,
            "workflow_json": {"steps": [first, auxiliary_only]},
        }
    )
    assert any(
        "workflow_plan_step_operation_missing" in error
        for error in auxiliary_errors
    )


def test_workflow_plan_trace_allows_one_plan_step_to_expand_one_to_many():
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )
    device_plan = [
        {
            "plan_step": 1,
            "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
            "operation_intent": "反应搅拌 60 min",
            "source_macro_step": 1,
            "source_macro_steps": [1],
        }
    ]
    workflow_steps = [
        {
            "step_number": index,
            "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
            "operation": operation,
            "parameters": {},
            "source_plan_step": 1,
            "source_macro_step": 1,
            "source_macro_steps": [1],
        }
        for index, operation in enumerate(
            ("开盖", "开始搅拌", "关盖"), start=1
        )
    ]
    assert agent._workflow_plan_step_trace_errors(
        {
            "device_plan": device_plan,
            "workflow_json": {"steps": workflow_steps},
        }
    ) == []


def test_device_runtime_error_still_returns_end_to_end_terminal_package():
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=FakeWorkstationLoader(),
    )

    state = agent.run_state(RESEARCH_HANDOFF, exp_id="runtime_error_package")

    assert state.status == "failed"
    assert state.terminal_package["status"] == "failed"
    assert state.terminal_package["feedback_type"] == "device_internal_error"
    assert state.terminal_package["failure_stage"] == "device_internal_error"
    assert state.terminal_package["workflow_json"] == {}
    assert state.errors


def build_agent(payload):
    return SingleDeviceAgent(
        model=FakeModel(_with_common_plan(payload)),
        workstation_loader=FakeWorkstationLoader(),
    )


def test_temporal_addition_stirring_soft_error_retries_as_batches():
    model = TemporalRetryModel()
    agent = SingleDeviceAgent(
        model=model, workstation_loader=WorkstationLoader(use_new_format=True)
    )
    handoff = {
        "task": {"query": "共沉淀", "current_stage": "合成"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "边滴入边搅拌加入 B 液",
                "试剂/对象": "B 液",
                "参数": "B 液 10 mL，30 min",
            }
        ],
    }

    state = agent.run_state(handoff, exp_id="temporal_exp")

    assert state.status == "completed"
    assert state.terminal_package["status"] == "success"
    assert state.terminal_package["temporal_adaptations"][0]["execution_fidelity"] == "approximated"
    assert state.terminal_package["requires_scientific_review"] is True
    assert len(model.calls) == 3  # plan -> adaptation retry -> translation
    assert "强制重试要求" in model.calls[1]


def test_generic_soft_complaint_also_retries():
    """Review finding 1: a NON-keyword complaint without hard capability
    evidence must trigger the adaptation retry, not physical_infeasible."""
    model = GenericComplaintRetryModel()
    agent = SingleDeviceAgent(
        model=model, workstation_loader=WorkstationLoader(use_new_format=True)
    )
    handoff = {
        "task": {"query": "共沉淀", "current_stage": "合成"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "边滴入边搅拌加入 B 液",
                "试剂/对象": "B 液",
                "参数": "B 液 10 mL，30 min",
            }
        ],
    }

    state = agent.run_state(handoff, exp_id="generic_exp")

    assert state.status == "completed"
    assert state.terminal_package["status"] == "success"
    assert len(model.calls) == 3  # plan -> adaptation retry -> translation
    assert "强制重试要求" in model.calls[1]


def test_unverifiable_condition_becomes_human_review_not_infeasible():
    """Review finding 1: 'no identically named dispatch field' complaints go
    to human review instead of being reported as physically infeasible."""
    payload = {
        "feedback_type": "device_feasibility_error",
        "status": "feasibility_error",
        "feasibility": {
            "is_feasible": False,
            "blocking_constraints": [
                "XRD_V1 的可下发参数中没有辐射源字段，无法保证 Cu Kα 与 2θ=5-80° 扫描范围"
            ],
        },
    }
    agent = build_agent(payload)

    state = agent.run_state(RESEARCH_HANDOFF, exp_id="unverifiable_exp")

    assert state.status == "manual_required"
    package = state.terminal_package
    assert package["status"] == "manual_required"
    assert package["feedback_type"] == "human_review_required"
    assert package["feedback_route"] == "human"
    assert package["feasibility_accepted"] is False
    assert package["error_package"]["type"] == "device_workflow_error"
    assert package["error_package"]["type"] != "research_replan_required"
    assert package["requires_scientific_review"] is True
    assert package["error_package"]["constraint_classification"]["unverifiable"]
    # the non-hard complaint got one adaptation retry before the verdict
    assert len(agent._model.calls) == 2


def test_hard_capability_gap_stays_physical_infeasible():
    result = {
        "feedback_type": "device_feasibility_error",
        "status": "feasibility_error",
        "feasibility": {
            "blocking_constraints": ["必须以恒定流速连续流滴加，不能分批"],
        },
    }
    handoff = {"macro_action_steps": [{"参数": "边滴入边搅拌"}]}
    assert not _soft_temporal_mapping_error(result, handoff)
    assert classify_constraint_text("必须以恒定流速连续流滴加，不能分批") == "hard"


def test_missing_required_station_is_not_softened():
    result = {
        "feedback_type": "device_feasibility_error",
        "status": "feasibility_error",
        "feasibility": {
            "blocking_constraints": ["磁力搅拌工作站缺失，无法执行边滴入边搅拌"],
        },
    }
    handoff = {"macro_action_steps": [{"参数": "边滴入边搅拌"}]}
    assert not _soft_temporal_mapping_error(result, handoff)


def test_constraint_text_classification_buckets():
    assert classify_constraint_text("缺少反应釜") == "hard"
    assert classify_constraint_text("容器不兼容：进样瓶无法进入马弗炉") == "adaptable"
    assert classify_constraint_text("工作站 1 到 2 没有显式转移路径") == "adaptable"
    assert (
        classify_constraint_text(
            "10ml耐压反应管没有通往进样瓶或留样瓶的受支持悬浊液转移路径"
        )
        == "adaptable"
    )
    assert (
        classify_constraint_text(
            "当前设备没有可合法获取并承载该反应的容器链路"
        )
        == "adaptable"
    )
    assert (
        classify_constraint_text(
            "高温高压微反应平台要求10ml耐压反应管，但真源中没有获取空10ml耐压反应管的物料站"
        )
        == "adaptable"
    )
    assert (
        classify_constraint_text("真源没有接受固体粉末并执行定时均匀混合的工作站操作")
        == "hard"
    )
    assert classify_constraint_text("单容器体积超过离心上限") == "hard"
    assert classify_constraint_text("工作站离线维修") == "hard"
    assert classify_constraint_text("设备无法在同一工作站同时边滴入边搅拌") == "adaptable"
    assert classify_constraint_text("缓慢滴加无法通过现有加液操作实现") == "adaptable"
    assert (
        classify_constraint_text("工作站参数 schema 只包含步长和扫描速度，无法设置扫描范围")
        == "unverifiable"
    )
    assert (
        classify_constraint_text("macro plan 中的参数在设备可下发参数中没有同名字段")
        == "unverifiable"
    )
    # Regression: `设备…参数中不存在字段` must not read as "设备不存在" (the
    # equipment-gap suffix rule once misfired hard on this A02 phrasing).
    assert (
        classify_constraint_text("论文要求的扫描起止角在设备可下发参数中不存在同名字段")
        == "unverifiable"
    )
    assert classify_constraint_text("真源中不存在球磨工作站") == "hard"


def test_connected_platform_guard_keeps_real_operation_gap_only():
    agent = SingleDeviceAgent(
        model=FakeModel({}),
        workstation_loader=FakeWorkstationLoader(),
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="connected_guard",
    )
    result = {
        "status": "feasibility_error",
        "feedback_type": "device_feasibility_error",
        "feasibility": {
            "is_feasible": False,
            "blocking_constraints": [
                "10ml耐压反应管没有通往进样瓶的悬浊液转移路径",
                "当前设备没有可合法获取并承载该反应的容器链路",
                "真源没有接受固体粉末并执行定时均匀混合的工作站操作",
            ],
            "unsupported_items": [{
                "requirement": "六组样品分别在120℃密闭反应12 h",
                "reason": (
                    "高温高压微反应平台要求10ml耐压反应管，但真源中没有获取空"
                    "10ml耐压反应管的物料站"
                ),
                "missing_device_capability": "可获取的10ml耐压反应管及其密闭水热反应链路",
            }],
        },
    }

    filtered = agent._remove_implicit_connectivity_blockers(state, result)

    blocking = filtered["feasibility"]["blocking_constraints"]
    assert len(blocking) == 1
    assert "均匀混合" in blocking[0]
    assert filtered["feasibility"]["unsupported_items"] == []
    assert filtered["feasibility"]["device_layer_adaptations"]
    assert any("connected-platform guard removed" in log for log in state.logs)


def _a02_040935_connectivity_error():
    """Exact connectivity-only evidence emitted by the verified A02 run."""

    automatic_transfer = (
        "水热反应必须在10ml耐压反应管中完成，但后续固定的6000 rpm离心、"
        "3次去离子水洗涤和2次乙醇洗涤只能由离心机_V1或纯化工作站_V1执行，"
        "而这些工作站仅接受有盖进样瓶（纯化站另可接受留样瓶）；当前真源没有"
        "接受10ml耐压反应管悬浊液并自动转入进样瓶的合法设备操作。"
    )
    xrd_input = (
        "XRD_V1仅接受进样瓶或50ml耐热瓶，水热产物仍位于10ml耐压反应管"
        "时无法建立XRD输入；"
    )
    source_container_output = (
        "高温高压微反应平台输出容器固定为10ml耐压反应管；离心机_V1输入必须是有盖"
        "进样瓶且数量为偶数，纯化工作站_V1也不接受10ml耐压反应管。液体处理工作站"
        "没有允许以10ml耐压反应管为源容器并输出进样瓶的操作。该固液分离和洗涤是"
        "研究路线的必要化学步骤，不能用offline_handoff替代。"
    )
    missing_capability = (
        "接受10ml耐压反应管悬浊液并转入可离心进样瓶的自动转移/固液分离链"
    )
    classified_blob = (
        "冷却后的水热悬浊液以6000 rpm离心5 min，固定执行3次水洗和2次乙醇洗涤"
        f"并保留湿沉淀 {source_container_output} {missing_capability}"
    )
    unsupported = {
        "macro_step": 3,
        "requirement": (
            "冷却后的水热悬浊液以6000 rpm离心5 min，固定执行3次水洗和"
            "2次乙醇洗涤并保留湿沉淀"
        ),
        "constraint_category": "hard_capability_gap",
        "reason": source_container_output,
        "missing_device_capability": missing_capability,
        "suggested_research_revision": "改变 Research 路线以补齐转移链。",
    }
    return {
        "feedback_type": "device_feasibility_error",
        "status": "feasibility_error",
        "recommendation_to_research_agent": "补齐反应管到进样瓶的转移接口。",
        "feasibility": {
            "is_feasible": False,
            "blocking_constraints": [automatic_transfer, xrd_input],
            "unsupported_items": [unsupported],
            "constraint_classification": {
                "hard": [automatic_transfer, classified_blob],
                "adaptable": [],
                "unverifiable": [xrd_input],
            },
        },
        "blocking_constraints": [automatic_transfer, xrd_input],
        "constraint_classification": {
            "hard": [classified_blob],
            "adaptable": [],
            "unverifiable": [automatic_transfer, xrd_input],
        },
        "error_package": {
            "blocking_constraints": [automatic_transfer, xrd_input],
            "unsupported_items": [unsupported],
            "constraint_classification": {
                "hard": [classified_blob],
                "adaptable": [],
                "unverifiable": [automatic_transfer, xrd_input],
            },
        },
    }


def test_connected_platform_guard_recursively_scrubs_a02_040935_variants():
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="a02_040935_connected_guard",
    )

    filtered = agent._remove_implicit_connectivity_blockers(
        state, _a02_040935_connectivity_error()
    )
    serialized = json.dumps(filtered, ensure_ascii=False)

    for fragment in (
        "自动转入进样瓶",
        "以10ml耐压反应管为源容器并输出进样瓶",
        "仍位于10ml耐压反应管时无法建立XRD输入",
        "转入可离心进样瓶的自动转移/固液分离链",
    ):
        assert fragment not in serialized
    for container in (filtered, filtered["feasibility"], filtered["error_package"]):
        assert container.get("blocking_constraints", []) == []
        assert container.get("unsupported_items", []) == []
        classification = container.get("constraint_classification", {})
        assert classification.get("hard", []) == []
        assert classification.get("unverifiable", []) == []
    assert agent._canonical_stage1_blockers(filtered) == []
    assert "recommendation_to_research_agent" not in filtered
    assert filtered["feasibility"]["connected_platform_guard"]["status"] == "applied"


def test_connected_platform_guard_enters_normalized_a02_terminal_envelope():
    assessment = _a02_040935_connectivity_error()
    terminal_package = {
        "status": "manual_required",
        "feedback_type": "human_review_required",
        "feedback_route": "human",
        "failure_scope": "device_plan",
        "feasibility_accepted": False,
        "feasibility_assessment": assessment,
        "blocking_constraints": [],
        "unsupported_items": [],
        # The normalizer duplicates the canonical route evidence here for
        # human diagnostics even though the terminal root is no longer a
        # feasibility_error.
        "error_package": json.loads(
            json.dumps(assessment["error_package"], ensure_ascii=False)
        ),
    }
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="a02_040935_terminal_envelope",
    )
    assert len(agent._canonical_stage1_blockers(terminal_package)) >= 2

    filtered = agent._remove_implicit_connectivity_blockers(
        state, terminal_package
    )
    serialized = json.dumps(filtered, ensure_ascii=False)

    assert agent._canonical_stage1_blockers(filtered) == []
    assert filtered["error_package"]["blocking_constraints"] == []
    assert filtered["error_package"]["constraint_classification"]["hard"] == []
    assert (
        filtered["error_package"]["constraint_classification"]["unverifiable"]
        == []
    )
    nested = filtered["feasibility_assessment"]
    assert nested["feasibility"]["blocking_constraints"] == []
    assert nested["feasibility"]["unsupported_items"] == []
    assert "recommendation_to_research_agent" not in nested
    assert filtered["feasibility"]["connected_platform_guard"]["status"] == "applied"
    for fragment in (
        "自动转入进样瓶",
        "仍位于10ml耐压反应管时无法建立XRD输入",
    ):
        assert fragment not in serialized


def test_connected_platform_guard_scrubs_structured_blockers_in_terminal_envelope():
    assessment = _a02_040935_connectivity_error()
    source_item = assessment["feasibility"]["unsupported_items"][0]
    structured_route_blocker = {
        "macro_step": 3,
        "requirement": source_item["requirement"],
        "constraint_category": "hard_capability_gap",
        "reason": source_item["reason"],
        "missing_device_capability": source_item[
            "missing_device_capability"
        ],
    }
    nested_assessment = {
        "status": "feasibility_error",
        "feedback_type": "device_feasibility_error",
        "feasibility": {
            "is_feasible": False,
            "blocking_constraints": [
                json.loads(json.dumps(structured_route_blocker, ensure_ascii=False))
            ],
            "unsupported_items": [],
            # These are non-routing model history and intentionally remain,
            # but the guard must label that status explicitly.
            "device_layer_adaptations": [source_item["reason"]],
        },
        "device_capability_summary": {
            "not_supported": [source_item["missing_device_capability"]]
        },
    }
    terminal_package = {
        "status": "manual_required",
        "feedback_type": "human_review_required",
        "feasibility_assessment": nested_assessment,
        "error_package": {
            "blocking_constraints": [
                json.loads(json.dumps(structured_route_blocker, ensure_ascii=False))
            ],
            "constraint_classification": {
                "hard": [
                    json.loads(
                        json.dumps(structured_route_blocker, ensure_ascii=False)
                    )
                ],
                "adaptable": [],
                "unverifiable": [],
            },
        },
    }
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="a02_structured_terminal_envelope",
    )
    assert agent._canonical_stage1_blockers(terminal_package)

    filtered = agent._remove_implicit_connectivity_blockers(
        state, terminal_package
    )

    assert agent._canonical_stage1_blockers(filtered) == []
    nested = filtered["feasibility_assessment"]
    assert nested["feasibility"]["blocking_constraints"] == []
    assert filtered["error_package"]["blocking_constraints"] == []
    assert filtered["error_package"]["constraint_classification"]["hard"] == []
    # Do not rewrite non-routing historical summaries; clearly label them.
    assert nested["device_capability_summary"]["not_supported"]
    assert nested["feasibility"]["device_layer_adaptations"]
    history_note = filtered["feasibility"]["connected_platform_guard"][
        "non_routing_history_note"
    ]
    assert "不参与当前路由或可行性判定" in history_note


def test_structured_connectivity_blocker_keeps_independent_evidence_fields():
    route_prefix = "没有耐压反应管到进样瓶的转移操作；"
    independent = [
        "pH测量能力未提供，无法验证pH 9.5。",
        "强制惰性安全条件无法满足。",
        "单容器体积超过6 mL上限。",
        "真源没有接受固体粉末并执行定时均匀混合的工作站操作。",
    ]
    result = {
        "status": "feasibility_error",
        "feedback_type": "device_feasibility_error",
        "feasibility": {
            "blocking_constraints": [
                {
                    "macro_step": index,
                    "reason": route_prefix + evidence,
                    "missing_device_capability": (
                        "接受10ml耐压反应管悬浊液并转入可离心进样瓶"
                        "的自动转移/固液分离链"
                    ),
                }
                for index, evidence in enumerate(independent, start=1)
            ]
        },
    }
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="structured_independent_evidence",
    )

    filtered = agent._remove_implicit_connectivity_blockers(state, result)
    blockers = filtered["feasibility"]["blocking_constraints"]
    serialized = json.dumps(blockers, ensure_ascii=False)

    assert len(blockers) == len(independent)
    assert all("missing_device_capability" not in item for item in blockers)
    assert "转移操作" not in serialized
    for evidence in independent:
        assert evidence in serialized
    canonical = " ".join(agent._canonical_stage1_blockers(filtered))
    assert "pH测量能力未提供" in canonical
    assert "强制惰性安全条件无法满足" in canonical
    assert "体积超过" in canonical
    assert "定时均匀混合" in canonical


def test_nested_active_evidence_dicts_and_lists_scrub_in_terminal_envelope():
    route_only = (
        "当前真源没有接受10ml耐压反应管悬浊液并自动转入进样瓶的"
        "合法设备操作。"
    )
    nested_records = [
        {"macro_step": 3, "evidence": {"reason": route_only}},
        {"macro_step": 3, "evidence": [{"reason": route_only}]},
        {
            "macro_step": 3,
            "evidence": {"items": [{"detail": route_only}]},
        },
    ]
    terminal_package = {
        "status": "manual_required",
        "feedback_type": "human_review_required",
        "feasibility_assessment": {
            "status": "feasibility_error",
            "feedback_type": "device_feasibility_error",
            "feasibility": {
                "blocking_constraints": json.loads(
                    json.dumps(nested_records, ensure_ascii=False)
                )
            },
        },
        "error_package": {
            "blocking_constraints": json.loads(
                json.dumps(nested_records, ensure_ascii=False)
            ),
            "constraint_classification": {
                "hard": json.loads(json.dumps(nested_records, ensure_ascii=False)),
                "adaptable": [],
                "unverifiable": [],
            },
        },
    }
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="nested_active_evidence_terminal",
    )
    assert agent._canonical_stage1_blockers(terminal_package)

    filtered = agent._remove_implicit_connectivity_blockers(
        state, terminal_package
    )

    assert agent._canonical_stage1_blockers(filtered) == []
    assert (
        filtered["feasibility_assessment"]["feasibility"][
            "blocking_constraints"
        ]
        == []
    )
    assert filtered["error_package"]["blocking_constraints"] == []
    assert filtered["error_package"]["constraint_classification"]["hard"] == []


def test_nested_active_evidence_keeps_independent_ph_safety_capacity_operation():
    route_prefix = "没有耐压反应管到进样瓶的转移操作；"
    independent = [
        "pH测量能力未提供。",
        "强制惰性安全条件无法满足。",
        "单容器体积超过6 mL上限。",
        "真源没有定时均匀混合化学操作。",
    ]
    result = {
        "status": "feasibility_error",
        "feedback_type": "device_feasibility_error",
        "feasibility": {
            "blocking_constraints": [
                {
                    "evidence": {
                        "items": [{"reason": route_prefix + evidence}]
                    }
                }
                for evidence in independent
            ]
        },
    }
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="nested_independent_evidence",
    )

    filtered = agent._remove_implicit_connectivity_blockers(state, result)
    serialized = json.dumps(
        filtered["feasibility"]["blocking_constraints"], ensure_ascii=False
    )

    assert "转移操作" not in serialized
    for evidence in independent:
        assert evidence in serialized
    canonical = " ".join(agent._canonical_stage1_blockers(filtered))
    for marker in ("pH", "强制惰性", "体积超过", "均匀混合"):
        assert marker in canonical


def test_arbitrary_blocker_wrappers_scrub_route_only_and_ignore_metadata():
    route_only = "工作站1到2没有显式转移路径"
    records = [
        {"wrapper": {"reason": route_only}},
        {"wrapper": [{"reason": route_only}]},
        {"outer": {"inner": [{"detail": route_only}]}},
        {
            "macro_step": 3,
            "constraint_category": "hard_capability_gap",
            "requirement": "后续离心洗涤",
        },
    ]
    terminal_package = {
        "status": "manual_required",
        "feedback_type": "human_review_required",
        "feasibility_assessment": {
            "status": "feasibility_error",
            "feedback_type": "device_feasibility_error",
            "feasibility": {
                "blocking_constraints": json.loads(
                    json.dumps(records, ensure_ascii=False)
                )
            },
        },
        "error_package": {
            "blocking_constraints": json.loads(
                json.dumps(records, ensure_ascii=False)
            )
        },
    }
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="arbitrary_wrapper_route_only",
    )
    assert agent._canonical_stage1_blockers(terminal_package) == [route_only]

    filtered = agent._remove_implicit_connectivity_blockers(
        state, terminal_package
    )

    assert agent._canonical_stage1_blockers(filtered) == []
    assert (
        filtered["feasibility_assessment"]["feasibility"][
            "blocking_constraints"
        ]
        == []
    )
    assert filtered["error_package"]["blocking_constraints"] == []


def test_arbitrary_blocker_wrappers_keep_true_and_mixed_evidence_structured():
    route_prefix = "工作站1到2没有显式转移路径；"
    evidence = [
        route_prefix + "pH测量能力未提供。",
        "设备真源没有聚四氟乙烯内衬高压反应釜。",
        route_prefix + "强制惰性安全条件无法满足。",
        route_prefix + "单容器体积超过6 mL上限。",
        route_prefix + "真源没有接受固体粉末并执行定时均匀混合操作。",
        "设备没有输出氢气并维持5 vol% H2/Ar气氛的操作。",
    ]
    result = {
        "status": "feasibility_error",
        "feedback_type": "device_feasibility_error",
        "feasibility": {
            "blocking_constraints": [
                {"wrapper": {"items": [{"reason": text}]}}
                for text in evidence
            ]
        },
    }
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="arbitrary_wrapper_true_evidence",
    )

    filtered = agent._remove_implicit_connectivity_blockers(state, result)
    blockers = filtered["feasibility"]["blocking_constraints"]
    canonical = agent._canonical_stage1_blockers(filtered)
    serialized = json.dumps(blockers, ensure_ascii=False)

    assert len(blockers) == len(evidence)
    assert all(isinstance(item, dict) and "wrapper" in item for item in blockers)
    assert all(not item.lstrip().startswith("{") for item in canonical)
    assert "显式转移路径" not in serialized
    for marker in (
        "pH测量",
        "高压反应釜",
        "强制惰性",
        "体积超过",
        "均匀混合",
        "输出氢气",
    ):
        assert marker in " ".join(canonical)


def test_a02_input_container_capability_variants_are_local_not_operation_gaps():
    route_reason = "没有耐压反应管到进样瓶的转移操作。"
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )
    for index, missing_capability in enumerate(
        (
            "以10ml耐压反应管为输入的自动离心洗涤",
            "以10ml耐压反应管为输入的XRD_V1制样",
            "以10ml耐压反应管为输入的XRD制样",
        ),
        start=1,
    ):
        result = {
            "status": "feasibility_error",
            "feedback_type": "device_feasibility_error",
            "feasibility": {
                "blocking_constraints": [],
                "unsupported_items": [
                    {
                        "macro_step": index,
                        "requirement": "离心洗涤或XRD制样",
                        "reason": route_reason,
                        "missing_device_capability": missing_capability,
                    }
                ],
            },
        }
        state = SingleDeviceAgentState(
            research_handoff=RESEARCH_HANDOFF,
            exp_id=f"a02_input_container_variant_{index}",
        )

        filtered = agent._remove_implicit_connectivity_blockers(state, result)

        assert filtered["feasibility"]["unsupported_items"] == []
        assert agent._canonical_stage1_blockers(filtered) == []


def test_true_centrifuge_wash_or_xrd_operation_absence_is_not_scrubbed():
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )
    true_gaps = (
        "自动离心洗涤操作完全不存在，所有容器均不支持",
        "XRD制样操作完全不存在",
    )
    result = {
        "status": "feasibility_error",
        "feedback_type": "device_feasibility_error",
        "feasibility": {
            "blocking_constraints": [],
            "unsupported_items": [
                {
                    "macro_step": index,
                    "reason": "没有耐压反应管到进样瓶的转移操作。",
                    "missing_device_capability": gap,
                }
                for index, gap in enumerate(true_gaps, start=1)
            ],
        },
    }
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="true_downstream_operation_gaps",
    )

    filtered = agent._remove_implicit_connectivity_blockers(state, result)
    unsupported = filtered["feasibility"]["unsupported_items"]
    canonical = " ".join(agent._canonical_stage1_blockers(filtered))

    assert len(unsupported) == 2
    for gap in true_gaps:
        assert gap in canonical


def test_a02_connectivity_variants_keep_independent_hard_evidence():
    # The A02 container phrases are local adaptations, but true equipment,
    # chemistry, pH, safety and capacity failures must survive scrubbing.
    assert classify_constraint_text(
        "当前真源没有接受10ml耐压反应管悬浊液并自动转入进样瓶的合法设备操作"
    ) == "adaptable"
    assert classify_constraint_text(
        "XRD_V1仅接受进样瓶，水热产物仍位于10ml耐压反应管时无法建立XRD输入"
    ) == "adaptable"
    assert classify_constraint_text(
        "设备真源没有聚四氟乙烯内衬高压反应釜，也没有从进样瓶向10ml耐压反应管"
        "转移混合样品的受支持操作"
    ) == "hard"
    assert classify_constraint_text(
        "没有耐压反应管到进样瓶的转移操作；单容器体积超过6 mL上限"
    ) == "hard"
    assert classify_constraint_text(
        "没有耐压反应管到进样瓶的转移操作；pH测量能力未提供"
    ) == "unverifiable"
    assert classify_constraint_text(
        "没有耐压反应管到进样瓶的转移操作；强制惰性安全条件无法满足"
    ) == "unverifiable"
    assert classify_constraint_text(
        "没有耐压反应管到进样瓶的转移操作；真源没有接受固体粉末并执行"
        "定时均匀混合的工作站操作"
    ) == "hard"
    assert classify_constraint_text(
        "设备没有输出氢气并维持5 vol% H2/Ar气氛的操作"
    ) == "hard"


def _a01_connectivity_and_quantity_error():
    transfer_after_hydrothermal = (
        "150 °C 密闭水热可由高温高压微反应平台在10 ml耐压反应管中完成，"
        "但当前真源没有将耐压反应管中的反应悬浊液转移到进样瓶/留样瓶的合法设备操作；"
        "因此后续离心、洗涤、干燥和XRD制样链无法闭合。"
    )
    transfer_before_stirring = (
        "表面 Fe 后处理和 Ni-LDH/FeOOH 物理混合要求对 Ni-LDH 悬浊液进行 "
        "pH 9.5、600 rpm、120 min 搅拌，但常温/加热磁力搅拌工作站仅接受进样瓶或"
        "50 ml耐热瓶，耐压反应管没有兼容搅拌操作；现有转移工作站也不接受"
        "耐压反应管作为转移源。"
    )
    transfer_unsupported = {
        "macro_step": "步骤4",
        "requirement": "对五组制备态悬浊液离心、洗涤、干燥",
        "constraint_category": "hard_capability_gap",
        "reason": (
            "离心机_V1和纯化工作站_V1的输入容器均为进样瓶（或留样瓶），且要求有盖；"
            "高温高压微反应平台输出为无盖10 ml耐压反应管。现有液体转移、固体转移"
            "和后处理工作站均没有耐压反应管到进样瓶/留样瓶的合法样品转移操作，"
            "不能建立离心输入状态。"
        ),
        "missing_device_capability": "耐压反应管产物到进样瓶/留样瓶的自动化全量转移接口",
        "suggested_research_revision": "改变化学路线以补齐转移链。",
    }
    quantity_unsupported = {
        "macro_step": "步骤2",
        "requirement": (
            "每批总液体体积固定8.0 mL，同时使用0.10 mol/L Ni/Fe和"
            "0.20 mol/L尿素原液并加入0.625 mmol尿素"
        ),
        "constraint_category": "unverifiable_condition",
        "reason": (
            "按给定浓度计算，Ni-LDH为5.000 mL Ni液+3.125 mL尿素液=8.125 mL；"
            "NiFe-LDH-20和-40同样各为8.125 mL，已超过声明的8.0 mL。"
            "设备可通过拆分反应管规避单管6 mL上限，但不能同时保持全部名义"
            "体积和物质的量而不改变总量。"
        ),
        "missing_device_capability": "无；这是研究配方内部的体积/物质的量矛盾，需要科学确认",
        "suggested_research_revision": "确认采用8.0 mL还是8.125 mL。",
    }
    return {
        "status": "feasibility_error",
        "feedback_type": "device_feasibility_error",
        "recommendation_to_research_agent": "补齐转移链并修改配方。",
        "feasibility": {
            "is_feasible": False,
            "blocking_constraints": [
                transfer_after_hydrothermal,
                transfer_before_stirring,
            ],
            "unsupported_items": [
                transfer_unsupported,
                quantity_unsupported,
            ],
            "constraint_classification": {
                "hard": [transfer_after_hydrothermal],
                "adaptable": [transfer_before_stirring],
                "unverifiable": [],
            },
        },
        "blocking_constraints": [transfer_after_hydrothermal],
        "constraint_classification": {
            "hard": [transfer_after_hydrothermal],
            "adaptable": [],
            "unverifiable": [
                "没有耐压反应管到进样瓶的转移操作；pH测量能力未提供，无法验证pH 9.5。",
                "没有耐压反应管到进样瓶的转移操作；强制惰性安全条件无法满足。",
                (
                    "没有耐压反应管到进样瓶的转移操作；"
                    "真源没有接受固体粉末并执行定时均匀混合的工作站操作。"
                ),
            ],
        },
        "error_package": {
            "blocking_constraints": [
                transfer_before_stirring,
                "没有耐压反应管到进样瓶的转移操作；pH测量能力未提供。",
            ],
            "constraint_classification": {
                "hard": [transfer_after_hydrothermal],
                "adaptable": [],
                "unverifiable": [quantity_unsupported["reason"]],
            },
            "unsupported_items": [transfer_unsupported],
        },
    }


def test_connected_platform_guard_recursively_scrubs_a01_synonyms_only():
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        research_handoff=RESEARCH_HANDOFF,
        exp_id="a01_recursive_connected_guard",
    )

    filtered = agent._remove_implicit_connectivity_blockers(
        state, _a01_connectivity_and_quantity_error()
    )
    serialized = json.dumps(filtered, ensure_ascii=False)

    assert "没有将耐压反应管中的反应悬浊液转移" not in serialized
    assert "不接受耐压反应管作为转移源" not in serialized
    assert "自动化全量转移接口" not in serialized
    assert "制样链无法闭合" not in serialized
    assert "8.125 mL" in serialized
    assert "超过声明的8.0 mL" in serialized
    assert "pH测量能力未提供" in serialized
    assert "强制惰性安全条件无法满足" in serialized
    assert "真源没有接受固体粉末并执行定时均匀混合" in serialized
    assert len(filtered["feasibility"]["unsupported_items"]) == 1
    assert filtered["feasibility"]["unsupported_items"][0]["macro_step"] == "步骤2"
    assert filtered["blocking_constraints"] == []
    assert filtered["error_package"]["unsupported_items"] == []
    assert filtered["feasibility"]["connected_platform_guard"]["status"] == "applied"


def test_a01_quantity_only_error_gets_route_only_certificate_and_stays_device():
    payload = _a01_connectivity_and_quantity_error()
    # Independent pH/safety/operation examples above only test lossless
    # recursive scrubbing; the real A01 terminal candidate contains the two
    # transfer complaints plus the separate 8.0-vs-8.125 mL quantity conflict.
    payload["constraint_classification"] = {}
    payload["error_package"] = {}
    model = FakeModel(payload)
    agent = SingleDeviceAgent(
        model=model, workstation_loader=FakeWorkstationLoader()
    )

    state = agent.run_state(RESEARCH_HANDOFF, exp_id="a01_route_only_quantity")

    package = state.terminal_package
    serialized = json.dumps(package, ensure_ascii=False)
    assert len(model.calls) == 2  # initial Stage-1 candidate + Device-local retry
    assert package["status"] == "manual_required"
    assert package["feedback_type"] == "human_review_required"
    assert package["feedback_route"] == "human"
    assert package["failure_scope"] == "device_quantity"
    assert package["feasibility_accepted"] is True
    assert package["feasibility_certificate"]["accepted"] is True
    assert package["feasibility_certificate"]["acceptance_scope"] == (
        "route_only_pending_device_plan_repair"
    )
    assert package["feasibility"]["is_feasible"] is True
    assert package["feasibility"]["route_feasibility_accepted"] is True
    assert package["feasibility"]["blocking_constraints"] == []
    assert "8.125 mL" in " ".join(package["pending_device_local_constraints"])
    assert package["quantity_audit"]["status"] == "human_review_required"
    assert package["quantity_audit"]["requires_scientific_review"] is True
    assert package["error_package"]["type"] == (
        "device_quantity_stage1_repair_required"
    )
    assert package["error_package"]["structured_errors"][0]["error_code"] == (
        "stage1_quantity_conflict"
    )
    assert package["workflow_json"] == {}
    assert package["workflow_txt"] == ""
    assert "suggested_research_revision" not in serialized
    assert "不得自动返回 Research" in serialized
    assert "没有将耐压反应管中的反应悬浊液转移" not in serialized
    assert "不接受耐压反应管作为转移源" not in serialized


def test_unconfirmable_precondition_station_is_human_review_not_hard():
    """Issue-philosophy regression: 'cannot confirm an existing station is
    equivalent to the required precondition' is unprovable → human review,
    even when phrased as 没有提供X工作站; but a genuinely missing station or a
    no-transfer-path claim stays hard."""
    hedged = (
        "真源提供了谱学磁力搅拌工作站，但没有提供谱学置物工作站。"
        "Container_storaging_Station_V1 仅声明一般容器静置和物料放置，"
        "没有规则证明其等价于谱学置物工作站。"
    )
    assert classify_constraint_text(hedged) == "unverifiable"
    assert (
        classify_constraint_text("未提供可被确认等价于谱学置物工作站的工作站、操作或审核规则")
        == "unverifiable"
    )
    # A missing reactor is still hard even though a transfer complaint follows.
    assert (
        classify_constraint_text(
            "设备真源没有聚四氟乙烯内衬高压反应釜，也没有从进样瓶向10ml耐压反应管"
            "转移混合样品的受支持操作，因此不存在连续合法的容器路径"
        )
        == "hard"
    )
    assert classify_constraint_text("设备清单中没有球磨工作站、球磨操作、球磨罐") == "hard"


def test_liquid_query_keeps_all_ranges_and_stirrer_visible():
    loader = WorkstationLoader(use_new_format=True)
    selected = set(loader._select_relevant_station_codes("边滴入边搅拌 10 mL"))
    assert "Liquid_Handling_Station_5ml_V1" in selected
    assert "Liquid_Handling_Station_5ml_V2" in selected
    assert "Room_Temperture_Magnetic_Stirrer_Workstation_V1" in selected


def test_single_device_agent_success_package():
    payload = _with_common_plan(
        {
            "status": "success",
            "feasibility": {
                "is_feasible": True,
                "blocking_constraints": [],
                "device_layer_adaptations": ["补全容器编号"],
            },
            "macro_plan_summary": "映射成功",
            "reagent_slot_plan": [{"原液编号": 1, "名称": "水"}],
            "container_plan": [{"容器编号": 1, "容器类型": "进样瓶", "用途": "反应"}],
            "workflow_txt": "1. 第1步 物料站：\n   - 操作：物料拿取",
            "workflow_json": {
                "steps": [
                    {
                        "step_number": 1,
                        "workstation": "物料站",
                        "operation": "物料拿取",
                        "parameters": {"容器类型": "进样瓶", "容器编号": [1]},
                        "source_plan_step": 1,
                        "source_macro_step": 1,
                        "source_macro_steps": [1],
                    }
                ],
                "offline_handoffs": [],
            },
        }
    )
    agent = SingleDeviceAgent(
        model=FakeModel(payload),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )

    state = agent.run_state(RESEARCH_HANDOFF, exp_id="test_exp")

    assert state.status == "completed"
    assert state.terminal_package["status"] == "success"
    assert state.terminal_package["agent_mode"] == "single_device_agent"
    assert state.workflow_json["steps"]
    assert len(agent._model.calls) == 2  # plan + translation
    # issue 7: the package carries a platform-form dispatch payload alongside
    # the semantic workflow_json (planning layer stays untouched).
    dispatch = state.terminal_package.get("dispatch_payload") or {}
    dispatch_steps = dispatch.get("experiment_steps", {}).get("steps", [])
    assert dispatch_steps and dispatch_steps[0]["workstation"] == "303物料站"
    formatting = state.terminal_package.get("dispatch_formatting") or {}
    assert formatting.get("mapped_steps") == 1
    assert state.workflow_json["steps"][0]["workstation"] == "物料站"


def test_deterministic_completion_avoids_llm_repair_round():
    """Eval fix: a SKILL-form workflow missing only mechanically-derivable
    required fields (容器数量, 开盖编号, 保留瓶盖) must be completed deterministically
    and pass validation WITHOUT burning an LLM self-repair round (model called once)."""
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)
    payload = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "macro_plan_summary": "映射成功",
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "General_Material_Station_V1",
                "objective": "取得实验容器",
                "operation_intent": "物料拿取",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_reagent_identity": "",
            },
            {
                "plan_step": 2,
                "workstation": "Liquid_Handling_Station_1ml_V2",
                "objective": "打开实验容器",
                "operation_intent": "开盖",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_reagent_identity": "",
            },
        ],
        "workflow_txt": "第1步 General_Material_Station_V1：物料拿取\n第2步 Liquid_Handling_Station_1ml_V2：开盖",
        "workflow_json": {
            "steps": [
                {"step_number": 1, "workstation": "General_Material_Station_V1",
                 "operation": "物料拿取",
                 "parameters": {"容器类型": "进样瓶", "容器编号": [1, 2, 3]},
                 "source_plan_step": 1, "source_macro_step": 1,
                 "source_macro_steps": [1]},
                {"step_number": 2, "workstation": "Liquid_Handling_Station_1ml_V2",
                 "operation": "开盖",
                 "parameters": {"容器类型": "进样瓶", "容器编号": [1, 2, 3]},
                 "source_plan_step": 2, "source_macro_step": 1,
                 "source_macro_steps": [1]},
            ],
            "offline_handoffs": [],
        },
    }
    model = FakeModel(_with_common_plan(payload))
    agent = SingleDeviceAgent(
        model=model, workstation_loader=loader, workflow_validator=validator,
    )
    state = agent.run_state(RESEARCH_HANDOFF, exp_id="complete_exp")

    assert state.status == "completed"
    assert state.terminal_package["status"] == "success"
    # deterministic completion filled the mechanical fields, so NO repair
    # round was needed — exactly plan + translation, no third call.
    assert len(model.calls) == 2
    completion = state.terminal_package.get("dispatch_completion") or {}
    filled_params = {e["param"] for e in completion.get("filled", [])}
    assert {"id", "容器数量", "开盖编号", "保留瓶盖"} <= filled_params
    # the completed workflow_json carries the filled values
    p1 = state.workflow_json["steps"][0]["parameters"]
    assert p1["容器数量"] == 3
    assert state.workflow_json["steps"][0]["id"] == 1427568512205824
    assert state.workflow_json["steps"][1]["id"] == 2002186824385539
    assert state.workflow_json["steps"][1]["parameters"]["保留瓶盖"] == 0


def test_lid_retention_is_one_only_when_same_containers_close_later():
    workflow = {
        "steps": [
            {
                "step_number": 1,
                "operation": "开盖",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器编号": [1, 2],
                    "保留瓶盖": 0,
                },
            },
            {
                "step_number": 2,
                "operation": "关盖",
                "parameters": {"容器类型": "进样瓶", "容器编号": [1, 2]},
            },
            {
                "step_number": 3,
                "operation": "开盖",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器编号": [3],
                    "保留瓶盖": 1,
                },
            },
        ]
    }

    filled = SingleDeviceAgent._normalize_lid_retention(workflow)

    assert workflow["steps"][0]["parameters"]["保留瓶盖"] == 1
    assert workflow["steps"][2]["parameters"]["保留瓶盖"] == 0
    assert [item["value"] for item in filled] == [1, 0]


def test_completion_applies_to_self_repair_output():
    """Regression: deterministic completion must run on the self-repair
    (attempt-2) output too. A bug once left attempt-2 uncompleted, so a fresh
    LLM output could reintroduce mechanical omissions (缺 保留瓶盖) that the
    harness can fill. Here attempt-1 has a structural error forcing a repair;
    the repaired output omits 保留瓶盖 (mechanical) and must be completed."""
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)

    # attempt-1: 加样方案 as string → type_mismatch (structural, forces repair)
    attempt1 = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "workflow_txt": "x",
        "workflow_json": {"steps": [
            {"step_number": 1, "workstation": "Liquid_Handling_Station_1ml_V2",
                 "operation": "加液_物料绑定",
                 "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1],
                                "加样方案": "一段自然语言"},
                 "source_plan_step": 1, "source_macro_step": 1,
                 "source_macro_steps": [1]}],
            "offline_handoffs": []},
    }
    # attempt-2 (repair): fixes 加样方案 structure but OMITS 保留瓶盖 on an 开盖 step
    attempt2 = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "workflow_txt": "x",
        "workflow_json": {"steps": [
            {"step_number": 1, "workstation": "Liquid_Handling_Station_1ml_V2",
                 "operation": "开盖",
                 "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1],
                                "开盖编号": [1]},
                 "source_plan_step": 1, "source_macro_step": 1,
                 "source_macro_steps": [1]}],  # 保留瓶盖 missing → completion fills =1
            "offline_handoffs": []},
    }

    class SequencedModel(NativeFakeMixin):
        def __init__(self, payloads):
            self.payloads = payloads
            self.calls = []

        def invoke(self, messages):
            idx = min(len(self.calls), len(self.payloads) - 1)
            self.calls.append(messages)
            return types.SimpleNamespace(
                content=json.dumps(self.payloads[idx], ensure_ascii=False))

    stage_plan = {
        "status": "device_plan",
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "Liquid_Handling_Station_1ml_V2",
                "objective": "打开实验容器",
                "operation_intent": "开盖",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_reagent_identity": "",
            }
        ],
        "offline_handoffs": [],
    }
    model = SequencedModel([stage_plan, attempt1, attempt2])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=loader, workflow_validator=validator,
    )
    state = agent.run_state(RESEARCH_HANDOFF, exp_id="repair_complete_exp")

    # Stage-1 plan + initial translation + one repair; completion fills
    # 保留瓶盖 on the repaired workflow without another LLM call.
    assert len(model.calls) == 3
    filled = {e["param"] for e in
              (state.terminal_package.get("dispatch_completion") or {}).get("filled", [])}
    assert "保留瓶盖" in filled
    # attempt-2 completed → passes validation → success
    assert state.terminal_package["status"] == "success"


class _SequencedModel(NativeFakeMixin):
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []
        self.prompts = []

    def invoke(self, messages):
        idx = min(len(self.calls), len(self.payloads) - 1)
        self.calls.append(messages)
        self.prompts.append(
            "\n".join(
                m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
                for m in messages
            )
        )
        return types.SimpleNamespace(
            content=json.dumps(self.payloads[idx], ensure_ascii=False))


def _good_material_step():
    return {
        "step_number": 1,
        "workstation": "General_Material_Station_V1",
        "operation": "物料拿取",
        "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1]},
    }


def test_weighing_handoff_triggers_audit_repair_and_route_note():
    """Issue #9 (two-stage): a manual-weighing offline_handoff in the PLAN
    stage must trigger one plan-level repair whose prompt carries the audit
    finding + the canonical solid-weighing route; the clean plan then flows
    through translation to success with capability_audit=clean."""
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)

    plan_with_handoff = {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": [{
            "plan_step": 1, "workstation": "General_Material_Station_V1",
            "objective": "拿取容器", "operation_intent": "物料拿取",
            "containers": {"容器类型": "进样瓶", "容器编号": [1]},
            "source_macro_step": 1,
        }],
        "offline_handoffs": [{
            "name": "XRD定量称样与换瓶",
            "instructions": ["分别称取5.0 mg粉末并对应装入无盖进样瓶21-30"],
        }],
    }
    plan_clean = {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": plan_with_handoff["device_plan"],
        "offline_handoffs": [],
    }
    translation = {
        "workflow_txt": "第1步 General_Material_Station_V1：物料拿取",
        "workflow_json": {"steps": [_good_material_step()], "offline_handoffs": []},
    }
    model = _SequencedModel([plan_with_handoff, plan_clean, translation])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=loader, workflow_validator=validator,
    )
    state = agent.run_state(RESEARCH_HANDOFF, exp_id="audit_repair_exp")

    assert len(model.calls) == 3  # plan -> plan repair -> translation
    repair_prompt = model.prompts[1]
    assert "计划级能力审计" in repair_prompt
    assert "固体样品转移" in repair_prompt  # canonical route note attached
    package = state.terminal_package
    assert package["status"] == "success"
    assert package["capability_audit"]["status"] == "clean"


def test_two_bounded_repair_rounds_then_success():
    """Issue #12: repair → re-validate → repair → re-validate. bad, bad, good
    = 3 mapping calls and a success package."""
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)

    bad = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "workflow_txt": "第1步 General_Material_Station_V1：物料拿取",
        "workflow_json": {"steps": [{
            "step_number": 1,
            "workstation": "General_Material_Station_V1",
            "operation": "物料拿取",
            "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1],
                           "不存在的危险参数": 999},
        }], "offline_handoffs": []},
    }
    good = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "workflow_txt": "第1步 General_Material_Station_V1：物料拿取",
        "workflow_json": {"steps": [_good_material_step()], "offline_handoffs": []},
    }
    model = _SequencedModel([
        {"status": "device_plan",
         "feasibility": {"is_feasible": True, "blocking_constraints": []},
         "device_plan": [{"plan_step": 1,
                          "workstation": "General_Material_Station_V1",
                          "objective": "拿取容器", "operation_intent": "物料拿取",
                          "containers": {"容器类型": "进样瓶", "容器编号": [1]},
                          "source_macro_step": 1}],
         "offline_handoffs": []},
        bad, bad, good,
    ])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=loader, workflow_validator=validator,
    )
    state = agent.run_state(RESEARCH_HANDOFF, exp_id="two_round_exp")
    assert len(model.calls) == 4  # plan + initial translation + 2 repairs
    assert state.terminal_package["status"] == "success"


def test_lid_conflict_and_txt_mismatch_are_deterministic_errors():
    """Issue #12: lid-state continuity and txt↔json agreement are now
    deterministic checks, not LLM self-check prose."""
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)

    # 关盖 then an operation whose SKILL input demands 无盖 → proven conflict;
    # workflow_txt also has fewer 第N步 blocks than steps → txt_json_mismatch.
    bad = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "workflow_txt": "第1步 移液平台1ml_V2：关盖",
        "workflow_json": {"steps": [
            {"step_number": 1, "workstation": "Liquid_Handling_Station_1ml_V2",
             "operation": "关盖",
             "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1],
                            "关盖编号": [1]}},
            {"step_number": 2,
             "workstation": "Multi_Channel_Solid_Weighing_Workstation_V1",
             "operation": "固体进样-文件传参-机器人",
             "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1],
                            "上传文件": "recipe.csv"}},
        ], "offline_handoffs": []},
    }
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=loader,
        workflow_validator=validator,
    )
    report = agent._run_full_checks(bad)
    errors = " ".join(report["errors"])
    assert report["status"] == "failed"
    assert "lid_state_conflict" in errors
    assert "txt_json_mismatch" in errors


def test_single_device_agent_feasibility_error_package():
    agent = build_agent(
        {
            "feedback_type": "device_feasibility_error",
            "status": "feasibility_error",
            "feasibility": {
                "is_feasible": False,
                "blocking_constraints": ["缺少反应釜"],
                "unsupported_items": [
                    {
                        "macro_step": "溶剂热",
                        "requirement": "反应釜",
                        "reason": "真源未支持",
                        "missing_device_capability": "高压釜",
                        "suggested_research_revision": "改为常压路线",
                    }
                ],
            },
            "device_capability_summary": {
                "supported_containers": ["进样瓶"],
                "supported_workstations": ["material-workstation"],
                "not_supported": ["反应釜"],
            },
            "recommendation_to_research_agent": "改为常压路线",
        }
    )

    required_reactor_handoff = {
        "task": {"query": "溶剂热反应", "current_stage": "合成"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "在高压反应釜中完成溶剂热反应",
                "试剂/对象": "前驱体溶液",
                "参数": "必要高压反应条件",
            }
        ],
    }
    state = agent.run_state(required_reactor_handoff, exp_id="test_exp")

    assert state.status == "feasibility_error"
    package = state.terminal_package
    assert package["feedback_type"] == "research_replan_required"
    assert package["legacy_feedback_type"] == "device_feasibility_error"
    assert package["feedback_route"] == "research"
    assert package["failure_scope"] == "route_feasibility"
    assert package["feasibility_accepted"] is False
    assert package["error_package"]["blocking_constraints"] == ["缺少反应釜"]
    assert package["error_package"]["type"] == "research_replan_required"
    assert package["error_package"]["reason_category"] == "physical_infeasible"
    assert package["error_package"]["assessment_source"] == "single_device_agent_llm"
    # hard capability gap: no adaptation retry wasted
    assert len(agent._model.calls) == 1


def test_unrelated_reactor_gap_cannot_replan_container_only_research():
    agent = build_agent(
        {
            "feedback_type": "device_feasibility_error",
            "status": "feasibility_error",
            "feasibility": {
                "is_feasible": False,
                "blocking_constraints": ["缺少反应釜"],
            },
        }
    )

    package = agent.run_state(
        RESEARCH_HANDOFF, exp_id="ungrounded_reactor_gap"
    ).terminal_package

    assert package["status"] == "manual_required"
    assert package["feedback_route"] == "human"
    assert package["failure_scope"] == "device_plan"
    assert package["error_package"]["type"] == "needs_human_review"


def test_false_named_xrd_absence_is_downgraded_by_complete_truth():
    """An LLM cannot erase a workstation present in lab-design-all truth."""
    blocker = "必要 XRD 工作站不存在，且没有任何替代工作站"
    model = FakeModel(
        {
            "feedback_type": "device_feasibility_error",
            "status": "feasibility_error",
            "feasibility": {
                "is_feasible": False,
                "blocking_constraints": [blocker],
            },
            "recommendation_to_research_agent": "删除 XRD 观察点",
        }
    )
    agent = SingleDeviceAgent(
        model=model,
        workstation_loader=WorkstationLoader(use_new_format=True),
    )

    xrd_research_handoff = {
        "task": {"query": "XRD characterization", "current_stage": "XRD"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "采集 XRD 图谱",
                "试剂/对象": "催化剂样品",
                "参数": "获得可判读衍射数据",
            }
        ],
    }
    state = agent.run_state(
        xrd_research_handoff, exp_id="false_xrd_absence"
    )

    package = state.terminal_package
    assert state.status == "manual_required"
    assert package["status"] == "manual_required"
    assert package["feedback_type"] == "human_review_required"
    assert package["feedback_route"] == "human"
    assert package["failure_scope"] == "device_plan"
    assert package["feasibility_accepted"] is False
    assert package["error_package"]["assessment_source"] == (
        "single_device_agent_llm_unverifiable"
    )
    classification = package["error_package"]["constraint_classification"]
    assert classification["hard"] == []
    assert blocker in classification["unverifiable"]
    assert len(model.calls) == 1
    assert any(
        "workstation-truth contradiction guard" in line for line in state.logs
    )


def test_truth_existence_guard_requires_complete_truth_and_bare_claim():
    complete_agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )
    assert complete_agent._constraint_contradicted_by_workstation_truth(
        "X射线衍射仪_V1 未配置，且没有任何替代工作站"
    )
    assert not complete_agent._constraint_contradicted_by_workstation_truth(
        "缺少可在 450 ℃ H2 气氛中原位运行的 XRD 工作站"
    )

    incomplete_agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )
    assert not incomplete_agent._constraint_contradicted_by_workstation_truth(
        "必要 XRD 工作站不存在，且没有任何替代工作站"
    )


def test_fabricated_dispatch_parameter_is_rejected():
    """Review finding 2: 不存在的危险参数=999 must not pass as success."""
    payload = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_self_check": {"workstation_constraints": "pass"},
        "workflow_txt": "1. 第1步 物料站：\n   - 操作：物料拿取",
        "workflow_json": {
            "steps": [
                {
                    "step_number": 1,
                    "workstation": "物料站",
                    "operation": "物料拿取",
                    "parameters": {
                        "容器类型": "进样瓶",
                        "容器编号": [1],
                        "不存在的危险参数": 999,
                    },
                }
            ],
            "offline_handoffs": [],
        },
    }
    plan = _with_common_plan(
        {
            "status": "device_plan",
            "feasibility": {"is_feasible": True, "blocking_constraints": []},
        }
    )
    rewritten_plan = _with_common_plan(
        {
            "status": "device_plan",
            "feasibility": {"is_feasible": True, "blocking_constraints": []},
            "plan_changes": [],
            "change_rationale": "保留冻结路线并最小调整设备计划",
            "expected_resolved_errors": ["unknown_parameter"],
            "route_changed": False,
        }
    )
    model = _SequencedModel(
        [plan] + [payload] * 9 + [rewritten_plan] + [payload] * 9
    )
    agent = SingleDeviceAgent(
        model=model,
        workstation_loader=WorkstationLoader(use_new_format=True),
    )

    state = agent.run_state(RESEARCH_HANDOFF, exp_id="fabricated_exp")

    assert state.status == "manual_required"
    package = state.terminal_package
    assert package["status"] == "manual_required"
    assert package["feedback_type"] == "human_review_required"
    assert package["feedback_route"] == "human"
    assert package["failure_scope"] == "device_workflow"
    assert package["feasibility_accepted"] is True
    assert any(
        "不存在的危险参数" in error
        for error in package["dispatch_validation"]["errors"]
    )
    # Exhaustion stays at Device and produces an auditable manual handoff.
    assert len(agent._model.calls) == 20
    error_package = package["error_package"]
    assert error_package["type"] == "device_workflow_repair_exhausted"
    repair = package["workflow_repair"]
    assert len(repair["cycles"]) == 2
    assert [cycle["initial_candidate_count"] for cycle in repair["cycles"]] == [1, 1]
    assert [cycle["modification_count"] for cycle in repair["cycles"]] == [8, 8]
    assert repair["plan_level_rewrite_count"] == 1
    structured = repair["deduplicated_error_history"]
    assert structured and any(
        "不存在的危险参数" in json.dumps(entry, ensure_ascii=False)
        for entry in structured
    )


def test_workflow_validator_accepts_reference_and_rejects_bad_values():
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)

    from utils.paths import format_reference_path

    reference = json.loads(
        open(format_reference_path("json"), encoding="utf-8").read()
    )
    assert validator.validate(reference)["status"] == "passed"

    unknown_station = {
        "steps": [
            {"step_number": 1, "workstation": "虚构工作站X", "operation": "操作", "parameters": {}}
        ]
    }
    assert validator.validate(unknown_station)["status"] == "failed"

    out_of_range = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "烘干机",
                "operation": "静置烘干",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器编号": [1],
                    "恒温温度（℃）": 999,
                    "烘干时间（分钟）": 60,
                },
            }
        ]
    }
    report = validator.validate(out_of_range)
    assert report["status"] == "failed"
    assert any("value_out_of_range" in error for error in report["errors"])

    missing_required = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "磁力搅拌工作站",
                "operation": "磁力搅拌",
                "parameters": {"容器类型": "进样瓶", "容器编号": [1]},
            }
        ]
    }
    report = validator.validate(missing_required)
    assert report["status"] == "failed"
    assert any("missing_required_parameter" in error for error in report["errors"])

    invalid_enum = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "纯化工作站",
                "operation": "标准离心",
                "parameters": {"进样瓶编号": [1, 2], "纯化方法": "不存在的方法"},
            }
        ]
    }
    report = validator.validate(invalid_enum)
    assert report["status"] == "failed"
    assert any("invalid_enum_value" in error for error in report["errors"])


def test_skill_required_params_enforced_for_skill_form_stations():
    """Issue 7: params the SKILL table marks 是否必填=是 must be present when the
    step names the station in SKILL form (code/display); legacy reference-form
    payloads keep the reference.json contract (previous test). 容器数量 must
    match len(容器编号)."""
    validator = WorkflowValidator(WorkstationLoader(use_new_format=True))

    missing_count = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "General_Material_Station_V1",
                "operation": "物料拿取",
                "parameters": {"容器类型": "进样瓶", "容器编号": [1, 2, 3]},
            }
        ]
    }
    report = validator.validate(missing_count)
    assert report["status"] == "failed"
    assert any("容器数量" in e and "missing_required_parameter" in e for e in report["errors"])

    open_lid_incomplete = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "Liquid_Handling_Station_1ml_V2",
                "operation": "开盖",
                "parameters": {"容器类型": "进样瓶", "容器编号": [1]},
            }
        ]
    }
    report = validator.validate(open_lid_incomplete)
    assert report["status"] == "failed"
    for required in ("容器数量", "开盖编号", "保留瓶盖"):
        assert any(required in e for e in report["errors"]), required

    complete = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "General_Material_Station_V1",
                "operation": "物料拿取",
                "parameters": {"容器类型": "进样瓶", "容器数量": 3, "容器编号": [1, 2, 3]},
            },
            {
                "step_number": 2,
                "workstation": "Liquid_Handling_Station_1ml_V2",
                "operation": "开盖",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "开盖编号": [1],
                    "保留瓶盖": "是",
                },
            },
        ]
    }
    assert validator.validate(complete)["status"] == "passed"

    count_mismatch = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "General_Material_Station_V1",
                "operation": "物料拿取",
                "parameters": {"容器类型": "进样瓶", "容器数量": 2, "容器编号": [1, 2, 3]},
            }
        ]
    }
    report = validator.validate(count_mismatch)
    assert report["status"] == "failed"
    assert any("container_count_mismatch" in e for e in report["errors"])


def test_label_value_enum_param_is_not_a_type_mismatch():
    """Regression: 保留瓶盖 is declared int in SKILL.md but dispatched as the
    label string 是/否; it must not be flagged type_mismatch, and an invalid
    label must still be rejected."""
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)

    valid_label = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "Liquid_Handling_Station_5ml_V2",
                "operation": "开盖",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "开盖瓶号": [1],
                    "保留瓶盖": "是",
                },
            }
        ]
    }
    assert validator.validate(valid_label)["status"] == "passed"

    # numeric form of the same enum also valid
    valid_value = json.loads(json.dumps(valid_label))
    valid_value["steps"][0]["parameters"]["保留瓶盖"] = 1
    assert validator.validate(valid_value)["status"] == "passed"

    invalid_label = json.loads(json.dumps(valid_label))
    invalid_label["steps"][0]["parameters"]["保留瓶盖"] = "也许"
    report = validator.validate(invalid_label)
    assert report["status"] == "failed"
    assert any("保留瓶盖" in error for error in report["errors"])


def test_structure_validation_errors_parses_fields():
    """Issue #4: validator error strings parse into per-error records with
    step/workstation/operation/parameter/error_code, back-filled with
    source_macro_step / macro_action_id from the workflow step."""
    workflow_json = {"steps": [
        {"step_number": 1, "workstation": "物料站", "operation": "物料拿取",
         "source_macro_step": 2, "macro_action_id": "MA_S01_R00",
         "parameters": {}},
        {"step_number": 3, "workstation": "烘干机", "operation": "静置烘干",
         "source_macro_step": 5, "parameters": {}},
    ]}
    errors = [
        "第 1 步（物料站/物料拿取）参数 `不存在的危险参数` 不在该工作站可下发参数中（unknown_parameter）。",
        "第 3 步（烘干机/静置烘干）`恒温温度`=999 超出真源允许范围 [26,210]（value_out_of_range）。",
        "第 1 步的工作站 `虚构站` 不在设备真源中（unknown_workstation）。",
        "workflow_txt 有 1 个『第N步』块，但 workflow_json 有 2 个 steps（txt_json_mismatch）。",
        "一条没有已知模式的新错误。",
    ]
    records = structure_validation_errors(errors, workflow_json)
    assert len(records) == 5

    by_code = {}
    for record in records:
        by_code.setdefault(record["error_code"], []).append(record)

    unknown_param = by_code["unknown_parameter"][0]
    assert unknown_param["step_number"] == 1
    assert unknown_param["workstation"] == "物料站"
    assert unknown_param["operation"] == "物料拿取"
    assert unknown_param["parameter_path"] == "不存在的危险参数"
    assert unknown_param["source_macro_step"] == 2
    assert unknown_param["macro_action_id"] == "MA_S01_R00"

    out_of_range = by_code["value_out_of_range"][0]
    assert out_of_range["step_number"] == 3
    assert out_of_range["parameter_path"] == "恒温温度"
    assert out_of_range["actual"] == "999"
    assert out_of_range["source_macro_step"] == 5

    assert by_code["txt_json_mismatch"][0]["error_code"] == "txt_json_mismatch"
    # unmatched wording degrades gracefully — information never lost
    assert by_code["unparsed"][0]["message"] == "一条没有已知模式的新错误。"


def test_structure_validation_errors_classifies_frozen_reagent_identity_failures():
    errors = [
        (
            "source_macro_step=1 未逐字保留 Research 试剂/对象身份："
            "missing=['nacl']。"
        ),
        (
            "source_macro_step=2 的显式试剂身份发生漂移："
            "before=['nacl'], after=[]。"
        ),
        (
            "source_macro_steps=['1', '2'] 引入了 Research 未授权的"
            "显式试剂身份：extra=['kcl']。"
        ),
    ]

    records = structure_validation_errors(errors, {})

    assert [record["error_code"] for record in records] == [
        "frozen_reagent_identity_missing",
        "frozen_reagent_identity_drift",
        "frozen_reagent_identity_unauthorized",
    ]
    assert records[0]["source_macro_step"] == 1
    assert records[1]["source_macro_step"] == 2
    assert records[2]["source_macro_steps"] == ["1", "2"]


def test_structure_validation_errors_classifies_stable_macro_binding_failures():
    errors = [
        "device_plan 引用了 Research 不存在的 source_macro_step=99。",
        "device_plan 缺少 Research macro step 2 的覆盖。",
        "计划级 Device LLM 删除了 macro step coverage：missing=['2']。",
        "device_plan 的 source_macro_steps 未按 Research macro step 顺序排列。",
        "device_plan 改变了 Research macro step/reagent 执行顺序。",
        (
            "计划级 Device LLM 改变了既存 plan_step 的冻结 source_macro_step "
            "绑定：plan_step=2, before=['1'], after=['2']。"
        ),
        (
            "operation recomposition 改变了冻结 macro source-set union："
            "before_sources=['1'], after_sources=['2']。"
        ),
        (
            "计划级 Device LLM 拆分/合并或新增了 plan step，但缺少逐组绑定的 "
            "operation_split plan_changes 证据。"
        ),
    ]

    records = structure_validation_errors(errors, {})

    assert [record["error_code"] for record in records] == [
        "invalid_source_macro_step",
        "macro_coverage_missing",
        "macro_coverage_missing",
        "frozen_macro_order_drift",
        "frozen_macro_order_drift",
        "frozen_source_macro_binding_drift",
        "frozen_source_macro_union_drift",
        "missing_plan_recomposition_evidence",
    ]
    assert records[0]["source_macro_step"] == 99


def _plan_step(n, ws="General_Material_Station_V1", op="物料拿取"):
    return {"plan_step": n, "workstation": ws, "objective": f"step {n}",
            "operation_intent": op,
            "containers": {"容器类型": "进样瓶", "容器编号": [n]},
            "source_macro_step": 1}


def _material_wf_step(n):
    return {"step_number": 1, "workstation": "General_Material_Station_V1",
            "operation": "物料拿取",
            "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [n]},
            "source_plan_step": n,
            "source_macro_step": 1,
            "source_macro_steps": [1]}


def _large_plan_result(n_steps):
    return {"status": "device_plan",
            "feasibility": {"is_feasible": True, "blocking_constraints": []},
            "device_plan": [_plan_step(i) for i in range(1, n_steps + 1)],
            "reagent_slot_plan": [], "container_plan": [],
            "temporal_adaptations": [], "offline_handoffs": []}


def test_chunked_translation_assembles_and_renumbers():
    """Regression for the D02 failure: a large device_plan (15 steps,
    chunk_size 6 → 3 chunks) is translated chunk-by-chunk and assembled into a
    single workflow with contiguous 1..N step_numbers — no more empty JSON."""
    import os
    os.environ["CHEM_DEVICE_TRANSLATION_CHUNK_SIZE"] = "6"
    try:
        loader = WorkstationLoader(use_new_format=True)
        validator = WorkflowValidator(loader)
        plan = _large_plan_result(15)
        # payload[0] = stage-1 plan; payloads[1..3] = three chunk translations
        chunk_frag = lambda ns: {
            "workflow_txt": "\n".join(f"第{i}步 General_Material_Station_V1：物料拿取"
                                      for i in range(1, len(ns) + 1)),
            "workflow_json": {"steps": [_material_wf_step(n) for n in ns]},
        }
        model = _SequencedModel([
            plan,
            chunk_frag([1, 2, 3, 4, 5, 6]),
            chunk_frag([7, 8, 9, 10, 11, 12]),
            chunk_frag([13, 14, 15]),
        ])
        agent = SingleDeviceAgent(
            model=model, workstation_loader=loader, workflow_validator=validator,
        )
        state = agent.run_state(RESEARCH_HANDOFF, exp_id="chunk_exp")

        assert len(model.calls) == 4  # 1 plan + 3 chunk translations
        package = state.terminal_package
        assert package["status"] == "success"
        steps = package["workflow_json"]["steps"]
        assert len(steps) == 15
        # contiguous, unique, 1..15
        assert [s["step_number"] for s in steps] == list(range(1, 16))
    finally:
        os.environ.pop("CHEM_DEVICE_TRANSLATION_CHUNK_SIZE", None)


def test_chunked_translation_repairs_only_failing_chunk():
    """Targeted repair: when a middle chunk carries a fabricated parameter, only
    that chunk is re-translated — the healthy chunks are reused from cache."""
    import os
    os.environ["CHEM_DEVICE_TRANSLATION_CHUNK_SIZE"] = "6"
    try:
        loader = WorkstationLoader(use_new_format=True)
        validator = WorkflowValidator(loader)
        plan = _large_plan_result(12)  # 2 chunks of 6

        def bad_step(n):
            s = _material_wf_step(n)
            s["parameters"]["不存在的危险参数"] = 999
            return s

        good_c1 = {"workflow_txt": "\n".join(f"第{i}步 General_Material_Station_V1：物料拿取"
                                             for i in range(1, 7)),
                   "workflow_json": {"steps": [_material_wf_step(n) for n in range(1, 7)]}}
        bad_c2 = {"workflow_txt": "\n".join(f"第{i}步 General_Material_Station_V1：物料拿取"
                                            for i in range(1, 7)),
                  "workflow_json": {"steps": [bad_step(n) for n in range(7, 13)]}}
        fixed_c2 = {"workflow_txt": "\n".join(f"第{i}步 General_Material_Station_V1：物料拿取"
                                              for i in range(1, 7)),
                    "workflow_json": {"steps": [_material_wf_step(n) for n in range(7, 13)]}}
        # plan, c1(good), c2(bad) → repair → only c2 re-translated (fixed)
        model = _SequencedModel([plan, good_c1, bad_c2, fixed_c2])
        agent = SingleDeviceAgent(
            model=model, workstation_loader=loader, workflow_validator=validator,
        )
        state = agent.run_state(RESEARCH_HANDOFF, exp_id="chunk_repair_exp")

        package = state.terminal_package
        assert package["status"] == "success"
        # 1 plan + 2 initial chunks + 1 targeted re-translation of chunk 2 = 4
        assert len(model.calls) == 4
        assert len(package["workflow_json"]["steps"]) == 12
    finally:
        os.environ.pop("CHEM_DEVICE_TRANSLATION_CHUNK_SIZE", None)


def test_all_chunks_empty_yields_device_manual_handoff_not_research_reflow():
    """Empty translations exhaust Device repair without reopening Research."""
    import os
    os.environ["CHEM_DEVICE_TRANSLATION_CHUNK_SIZE"] = "6"
    try:
        loader = WorkstationLoader(use_new_format=True)
        validator = WorkflowValidator(loader)
        plan = _large_plan_result(8)
        empty = {"workflow_txt": "", "workflow_json": {"steps": []}}
        model = _SequencedModel([plan, empty])  # every translation empty
        agent = SingleDeviceAgent(
            model=model, workstation_loader=loader, workflow_validator=validator,
        )
        state = agent.run_state(RESEARCH_HANDOFF, exp_id="chunk_empty_exp")

        package = state.terminal_package
        assert package["status"] == "manual_required"
        assert package["feedback_type"] == "human_review_required"
        assert package["feedback_route"] == "human"
        assert package["feasibility_accepted"] is True
        assert package["failure_scope"] == "device_plan"
        assert package["error_package"]["type"] == "device_plan_rewrite_rejected"
        assert package["workflow_repair"]["plan_level_rewrite_count"] == 1
    finally:
        os.environ.pop("CHEM_DEVICE_TRANSLATION_CHUNK_SIZE", None)


class _WorkflowReviewLoader(FakeWorkstationLoader):
    def format_complete_for_workflow_review(self):
        return "ROOT_RULE_MARKER\nALL_WORKSTATION_SKILLS_MARKER"


def _review_plan_and_translation():
    plan = {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_self_check": {"workstation_constraints": "pass"},
        "device_plan": [{
            "plan_step": 1,
            "workstation": "General_Material_Station_V1",
            "objective": "拿取一个进样瓶",
            "operation_intent": "物料拿取",
            "containers": {"容器类型": "进样瓶", "容器编号": [1]},
            "source_macro_step": 1,
        }],
        "reagent_slot_plan": [],
        "container_plan": [{
            "容器编号": 1, "容器类型": "进样瓶", "用途": "反应"
        }],
        "temporal_adaptations": [],
        "offline_handoffs": [],
    }
    translation = {
        "workflow_txt": "第1步 General_Material_Station_V1：物料拿取",
        "workflow_json": {"steps": [_good_material_step()]},
    }
    return plan, translation


def test_llm_skill_review_accepts_executable_workflow():
    plan, translation = _review_plan_and_translation()
    review = {
        "verdict": "executable",
        "summary": "所有输入输出状态均可由前序步骤证明",
        "issues": [],
    }
    model = _SequencedModel([plan, translation, review])
    previous = os.environ.get("CHEM_DEVICE_WORKFLOW_VERIFICATION")
    os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = "llm"
    try:
        agent = SingleDeviceAgent(
            model=model, workstation_loader=_WorkflowReviewLoader()
        )
        state = agent.run_state(RESEARCH_HANDOFF, exp_id="llm_review_pass")
    finally:
        if previous is None:
            os.environ.pop("CHEM_DEVICE_WORKFLOW_VERIFICATION", None)
        else:
            os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = previous

    package = state.terminal_package
    assert package["status"] == "success"
    assert package["dispatch_validation"]["assessment_source"] == (
        "llm_workstation_skill_reviewer"
    )
    assert package["workflow_skill_review"]["final_verdict"] == "executable"
    assert len(model.calls) == 3
    assert "全局工作站规则" in model.prompts[2]
    assert "全部工作站能力目录" in model.prompts[2]
    assert '"station_code": "General_Material_Station_V1"' in model.prompts[2]
    assert "operation_contracts" in model.prompts[2]
    assert "unknown 不能当作满足" in model.prompts[2]


def test_solid_recipe_rows_normalize_duplicate_g_and_mg_evidence():
    step = {
        "workstation": "多通道固体称量工作站_V1",
        "operation": "固体进样-文件传参-机器人",
        "parameters": {
            "容器类型": "进样瓶",
            "容器数量": 1,
            "容器编号": [1],
            "上传文件": "placeholder.xlsx",
        },
        "notes": "文件配方指定1号瓶、0.010 g、料罐号1；输出10 mg粉末。",
    }
    assert extract_v1_recipe_rows(step) == [
        {"bottle_number": 1, "mass_g": 0.01, "hopper_number": 1}
    ]


def test_solid_recipe_rows_handle_uniform_hopper_phrase_and_ignore_range():
    step = {
        "workstation": "多通道固体称量工作站_V1",
        "operation": "固体进样-文件传参-机器人",
        "parameters": {
            "容器类型": "50ml耐热瓶",
            "容器数量": 2,
            "容器编号": [10, 11],
            "上传文件": "placeholder.xlsx",
        },
        "notes": (
            "上传文件共2行，每行加样量为0.005 g，料罐号统一为1，"
            "0.005 g在[0,50] g范围内。"
        ),
    }

    assert extract_v1_recipe_rows(step) == [
        {"bottle_number": 1, "mass_g": 0.005, "hopper_number": 1},
        {"bottle_number": 2, "mass_g": 0.005, "hopper_number": 1},
    ]


def test_solid_recipe_rows_preserve_repeated_masses_and_per_row_hoppers():
    step = {
        "workstation": "多通道固体称量工作站_V1",
        "operation": "固体进样-文件传参-机器人",
        "parameters": {
            "容器类型": "50ml耐热瓶",
            "容器数量": 6,
            "容器编号": [10, 11, 12, 13, 14, 15],
            "上传文件": "placeholder.xlsx",
        },
        "notes": (
            "瓶10: 加样量=0.005 g；料罐号=1；"
            "瓶11: 加样量=0.005 g；料罐号=2；"
            "瓶12: 加样量=0.005 g；料罐号=3；"
            "瓶13: 加样量=0.010 g；料罐号=1；"
            "瓶14: 加样量=0.010 g；料罐号=2；"
            "瓶15: 加样量=0.010 g；料罐号=3"
        ),
    }

    assert extract_v1_recipe_rows(step) == [
        {"bottle_number": 1, "mass_g": 0.005, "hopper_number": 1},
        {"bottle_number": 2, "mass_g": 0.005, "hopper_number": 2},
        {"bottle_number": 3, "mass_g": 0.005, "hopper_number": 3},
        {"bottle_number": 4, "mass_g": 0.01, "hopper_number": 1},
        {"bottle_number": 5, "mass_g": 0.01, "hopper_number": 2},
        {"bottle_number": 6, "mass_g": 0.01, "hopper_number": 3},
    ]


def test_deterministic_completion_restores_recipe_evidence_from_device_plan():
    agent = SingleDeviceAgent(
        model=FakeModel({}), workstation_loader=_WorkflowReviewLoader()
    )
    state = SingleDeviceAgentState(RESEARCH_HANDOFF, exp_id="recipe_restore")
    result = {
        "device_plan": [
            {
                "plan_step": 20,
                "workstation": "多通道固体称量工作站_V1",
                "operation_intent": "定量称量",
                "key_values": {"目标质量": "0.0278g", "料罐号": "1"},
                "containers": {"容器类型": "50ml耐热瓶", "容器编号": [5]},
                "source_macro_step": 5,
            },
            {
                "plan_step": 22,
                "workstation": "多通道固体称量工作站_V1",
                "operation_intent": "定量称量",
                "key_values": {"目标质量": "0.0089g", "料罐号": "2"},
                "containers": {"容器类型": "50ml耐热瓶", "容器编号": [5]},
                "source_macro_step": 5,
            },
        ],
        "workflow_json": {
            "steps": [
                {
                    "step_number": 39,
                    "workstation": "多通道固体称量工作站_V1",
                    "operation": "固体进样-文件传参-机器人",
                    "parameters": {
                        "容器类型": "50ml耐热瓶",
                        "容器数量": 1,
                        "容器编号": [5],
                        "上传文件": "placeholder.xlsx",
                    },
                    "source_macro_step": 5,
                },
                {
                    "step_number": 41,
                    "workstation": "多通道固体称量工作站_V1",
                    "operation": "固体进样-文件传参-机器人",
                    "parameters": {
                        "容器类型": "50ml耐热瓶",
                        "容器数量": 1,
                        "容器编号": [5],
                        "上传文件": "placeholder.xlsx",
                    },
                    "notes": "同一目标瓶连续加入两种粉末。",
                    "source_macro_step": 5,
                },
            ]
        },
    }

    agent._apply_deterministic_completion(state, result)

    first, second = result["workflow_json"]["steps"]
    assert extract_v1_recipe_rows(first) == [
        {"bottle_number": 1, "mass_g": 0.0278, "hopper_number": 1}
    ]
    assert extract_v1_recipe_rows(second) == [
        {"bottle_number": 1, "mass_g": 0.0089, "hopper_number": 2}
    ]
    restored = result["dispatch_completion"]["filled"]
    assert [item["step_number"] for item in restored] == [39, 41]
    assert all(item["field"] == "notes.recipe_evidence" for item in restored)


def test_recipe_materializer_rewrites_to_task_scoped_file():
    workflow = {
        "steps": [{
            "step_number": 28,
            "workstation": "多通道固体称量工作站_V1",
            "operation": "固体进样-文件传参-机器人",
            "parameters": {
                "容器类型": "进样瓶",
                "容器数量": 1,
                "容器编号": [1],
                "上传文件": "missing.xlsx",
            },
            "notes": "加样量0.010 g，料罐号1。",
        }]
    }
    with tempfile.TemporaryDirectory() as temp_text:
        output_root = Path(temp_text) / "recipes"

        def fake_builder(output_path, rows):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake-xlsx")

        records = materialize_workflow_recipe_files(
            workflow,
            exp_id="A02 concurrent run",
            output_root=output_root,
            builder=fake_builder,
        )
        output_path = Path(records[0]["file_path"])
        if not output_path.is_absolute():
            output_path = Path(__file__).resolve().parents[1] / output_path
        assert output_path.is_file()
        assert "A02-concurrent-run" in str(output_path)
        assert workflow["steps"][0]["parameters"]["上传文件"] == records[0]["file_path"]
        assert records[0]["rows"][0]["mass_g"] == 0.01

        rewritten_without_notes = {
            "steps": [{
                "step_number": 28,
                "source_macro_step": records[0].get("source_macro_step"),
                "workstation": "多通道固体称量工作站_V1",
                "operation": "固体进样-文件传参-机器人",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "上传文件": records[0]["file_path"],
                },
            }]
        }
        reused = materialize_workflow_recipe_files(
            rewritten_without_notes,
            exp_id="A02 concurrent run",
            output_root=output_root,
            builder=fake_builder,
            known_artifacts=records,
        )
        assert reused[0]["source"] == "cached_task_scoped"
        assert reused[0]["rows"] == records[0]["rows"]


def test_recipe_materializer_passes_multiple_hoppers_to_workbook_builder():
    workflow = {
        "steps": [{
            "step_number": 29,
            "workstation": "多通道固体称量工作站_V1",
            "operation": "固体进样-文件传参-机器人",
            "parameters": {
                "容器类型": "进样瓶",
                "容器数量": 2,
                "容器编号": [7, 8],
                "上传文件": "missing.xlsx",
            },
            "notes": (
                "瓶7: 加样量=0.0075 g；料罐号=1；"
                "瓶8: 加样量=0.0025 g；料罐号=2"
            ),
        }]
    }
    captured = []
    with tempfile.TemporaryDirectory() as temp_text:
        output_root = Path(temp_text) / "recipes"

        def fake_builder(output_path, rows):
            captured.extend(rows)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake-xlsx")

        records = materialize_workflow_recipe_files(
            workflow,
            exp_id="multi-hopper-recipe",
            output_root=output_root,
            builder=fake_builder,
        )

    expected = [
        {"bottle_number": 1, "mass_g": 0.0075, "hopper_number": 1},
        {"bottle_number": 2, "mass_g": 0.0025, "hopper_number": 2},
    ]
    assert captured == expected
    assert records[0]["rows"] == expected


def test_spreadsheet_runtime_accepts_explicit_valid_paths():
    with tempfile.TemporaryDirectory() as temp_text:
        root = Path(temp_text)
        node = root / "node"
        node.write_bytes(b"runtime")
        modules = root / "node_modules"
        modules.mkdir()

        resolved_node, resolved_modules = _resolve_spreadsheet_runtime(
            node_binary=str(node),
            node_modules=str(modules),
        )

        assert resolved_node == node.resolve()
        assert resolved_modules == modules.resolve()


def test_recipe_file_exists_before_llm_skill_review():
    import single_agent as single_agent_module

    plan, _ = _review_plan_and_translation()
    translation = {
        "workflow_txt": "第1步 多通道固体称量工作站_V1：固体进样-文件传参-机器人",
        "workflow_json": {
            "steps": [{
                "step_number": 1,
                "workstation": "多通道固体称量工作站_V1",
                "operation": "固体进样-文件传参-机器人",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "上传文件": "missing.xlsx",
                },
                "notes": "加样量0.010 g，料罐号1。",
            }]
        },
    }
    review = {"verdict": "executable", "summary": "配方文件存在", "issues": []}
    model = _SequencedModel([plan, translation, review])
    previous_mode = os.environ.get("CHEM_DEVICE_WORKFLOW_VERIFICATION")
    original_materializer = single_agent_module.materialize_workflow_recipe_files
    with tempfile.TemporaryDirectory() as temp_text:
        recipe_path = Path(temp_text) / "task-scoped-recipe.xlsx"

        def fake_materializer(workflow_json, *, exp_id, known_artifacts=None):
            recipe_path.write_bytes(b"fake-xlsx")
            workflow_json["steps"][0]["parameters"]["上传文件"] = str(recipe_path)
            return [{
                "step_number": 1,
                "file_path": str(recipe_path),
                "rows": [{"bottle_number": 1, "mass_g": 0.01, "hopper_number": 1}],
                "source": "generated",
            }]

        os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = "llm"
        single_agent_module.materialize_workflow_recipe_files = fake_materializer
        try:
            agent = SingleDeviceAgent(
                model=model, workstation_loader=_WorkflowReviewLoader()
            )
            state = agent.run_state(RESEARCH_HANDOFF, exp_id="recipe_review_order")
        finally:
            single_agent_module.materialize_workflow_recipe_files = original_materializer
            if previous_mode is None:
                os.environ.pop("CHEM_DEVICE_WORKFLOW_VERIFICATION", None)
            else:
                os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = previous_mode

        assert state.terminal_package["status"] == "success"
        assert recipe_path.is_file()
        assert "每瓶加样量=0.005 g；料罐号=1" in model.prompts[1]
        assert str(recipe_path) in model.prompts[2]
        assert state.terminal_package["recipe_materialization"]["status"] == "passed"


def test_recipe_materializer_failure_is_internal_not_research_feedback():
    import single_agent as single_agent_module

    plan, translation = _review_plan_and_translation()
    translation["workflow_json"]["steps"] = [{
        "step_number": 1,
        "workstation": "多通道固体称量工作站_V1",
        "operation": "固体进样-文件传参-机器人",
        "parameters": {
            "容器类型": "进样瓶",
            "容器数量": 1,
            "容器编号": [1],
            "上传文件": "missing.xlsx",
        },
        "notes": "缺少可解析的称量行。",
    }]
    model = _SequencedModel([plan, translation])
    previous_mode = os.environ.get("CHEM_DEVICE_WORKFLOW_VERIFICATION")
    original_materializer = single_agent_module.materialize_workflow_recipe_files

    def failing_materializer(workflow_json, *, exp_id, known_artifacts=None):
        raise RecipeMaterializationError("cannot derive recipe rows")

    os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = "llm"
    single_agent_module.materialize_workflow_recipe_files = failing_materializer
    try:
        agent = SingleDeviceAgent(
            model=model, workstation_loader=_WorkflowReviewLoader()
        )
        state = agent.run_state(RESEARCH_HANDOFF, exp_id="recipe_failure_routing")
    finally:
        single_agent_module.materialize_workflow_recipe_files = original_materializer
        if previous_mode is None:
            os.environ.pop("CHEM_DEVICE_WORKFLOW_VERIFICATION", None)
        else:
            os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = previous_mode

    package = state.terminal_package
    assert package["status"] == "failed"
    assert package["feedback_type"] == "device_internal_error"
    assert package["failure_stage"] == "recipe_materialization"
    assert package["error_package"]["type"] == "recipe_materialization_failed"


def test_llm_skill_review_rewrites_once_and_rechecks():
    plan, translation = _review_plan_and_translation()
    replacement_step = _good_material_step()
    replacement_step["notes"] = "由 Skill reviewer 完整重写"
    rewritten = {
        "verdict": "rewritten",
        "summary": "原 workflow 状态链不完整，已重写",
        "issues": [{
            "code": "unproven_input_state",
            "severity": "error",
            "step_numbers": [1],
            "reason": "输入状态未由前序输出证明",
        }],
        "workflow_json": {"steps": [replacement_step]},
    }
    accepted = {
        "verdict": "executable",
        "summary": "重写后的状态链完整",
        "issues": [],
    }
    model = _SequencedModel([plan, translation, rewritten, accepted])
    previous = os.environ.get("CHEM_DEVICE_WORKFLOW_VERIFICATION")
    os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = "llm"
    try:
        agent = SingleDeviceAgent(
            model=model, workstation_loader=_WorkflowReviewLoader()
        )
        state = agent.run_state(RESEARCH_HANDOFF, exp_id="llm_review_rewrite")
    finally:
        if previous is None:
            os.environ.pop("CHEM_DEVICE_WORKFLOW_VERIFICATION", None)
        else:
            os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = previous

    package = state.terminal_package
    assert package["status"] == "success"
    assert package["workflow_skill_review"]["rewritten"] is True
    assert [item["verdict"] for item in package["workflow_skill_review"]["rounds"]] == [
        "rewritten", "executable"
    ]
    assert package["workflow_json"]["steps"][0]["notes"] == (
        "由 Skill reviewer 完整重写"
    )
    assert package["workflow_txt"].startswith(
        "第1步 General_Material_Station_V1：物料拿取"
    )
    assert len(model.calls) == 4
    assert "禁止再次重写" in model.prompts[3]
    review_prompt = model.prompts[2]
    assert "设备固定/默认能力" in review_prompt
    assert "不得强制增加当前 workflow 不负责的 XRD" in review_prompt
    assert "小批次交替执行" in review_prompt
    assert "必须优先返回 verdict=rewritten" in review_prompt
    assert "跨工作站等价路径" in review_prompt
    assert "离心 -> 去上清 -> 定量加洗液" in review_prompt
    assert "不要返回 workflow_txt" in review_prompt
    assert "全局已创建且仍活跃的容器库存" in review_prompt
    assert "不同既有编号子集之间的分批切换不是新增容器" in review_prompt
    assert "体积限制默认按目标容器计算" in review_prompt
    assert "issues/reason 已描述出一条" in review_prompt
    assert "实际下发 schema 为准" in review_prompt
    assert "用户提供的刚性载体/基底" in review_prompt
    assert "刚性载体做完自动反应后" in review_prompt
    assert "XRD 输入分支必须一致" in review_prompt
    assert "保留瓶盖" in review_prompt
    assert "不是不可变的化学数值" in review_prompt
    assert "原液瓶编号属于具体工作站的本地命名空间" in review_prompt
    assert "不同工作站可以各自复用 1 号槽位" in review_prompt
    assert "按父批次汇总观察" in review_prompt
    assert "不要求先物理合并" in review_prompt
    assert "反应管编号` 是托盘位置 1–4" in review_prompt
    assert "删除载体字段" in review_prompt
    assert "offline_handoff" in review_prompt


def test_llm_skill_review_repairs_deterministic_error_before_acceptance():
    plan, translation = _review_plan_and_translation()
    material = _good_material_step()
    material["source_macro_step"] = 1
    invalid_lid_rewrite = {
        "verdict": "rewritten",
        "summary": "首次语义重写，但留下重复开盖",
        "issues": [],
        "workflow_json": {
            "steps": [
                material,
                {
                    "step_number": 2,
                    "source_macro_step": 1,
                    "workstation": "Liquid_Handling_Station_1ml_V2",
                    "operation": "开盖",
                    "parameters": {
                        "容器类型": "进样瓶",
                        "容器数量": 1,
                        "容器编号": [1],
                        "开盖编号": [1],
                        "保留瓶盖": 1,
                    },
                },
                {
                    "step_number": 3,
                    "source_macro_step": 1,
                    "workstation": "Liquid_Handling_Station_1ml_V2",
                    "operation": "开盖",
                    "parameters": {
                        "容器类型": "进样瓶",
                        "容器数量": 1,
                        "容器编号": [1],
                        "开盖编号": [1],
                        "保留瓶盖": 1,
                    },
                },
            ]
        },
    }
    repaired_step = _good_material_step()
    repaired_step["source_macro_step"] = 1
    repaired = {
        "verdict": "rewritten",
        "summary": "依据确定性错误删除重复开盖",
        "issues": [],
        "workflow_json": {"steps": [repaired_step]},
    }
    accepted = {
        "verdict": "executable",
        "summary": "确定性校验和 Skill 复审均通过",
        "issues": [],
    }
    model = _SequencedModel(
        [plan, translation, invalid_lid_rewrite, repaired, accepted]
    )
    previous_review = os.environ.get("CHEM_DEVICE_WORKFLOW_VERIFICATION")
    previous_contract = os.environ.get("CHEM_DEVICE_CONTRACT_AUDIT")
    os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = "llm"
    os.environ["CHEM_DEVICE_CONTRACT_AUDIT"] = "on"
    try:
        loader = WorkstationLoader(use_new_format=True)
        agent = SingleDeviceAgent(model=model, workstation_loader=loader)
        state = agent.run_state(
            RESEARCH_HANDOFF, exp_id="llm_review_deterministic_repair"
        )
    finally:
        if previous_review is None:
            os.environ.pop("CHEM_DEVICE_WORKFLOW_VERIFICATION", None)
        else:
            os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = previous_review
        if previous_contract is None:
            os.environ.pop("CHEM_DEVICE_CONTRACT_AUDIT", None)
        else:
            os.environ["CHEM_DEVICE_CONTRACT_AUDIT"] = previous_contract

    package = state.terminal_package
    assert package["status"] == "success", json.dumps(
        package, ensure_ascii=False, indent=2
    )
    skill_review = package["workflow_skill_review"]
    assert skill_review["rewrite_count"] == 2
    assert [item["verdict"] for item in skill_review["rounds"]] == [
        "rewritten",
        "rewritten",
        "executable",
    ]
    assert skill_review["deterministic_rounds"][1]["status"] == "failed"
    assert "deterministic_validation" in model.prompts[3]
    assert "禁止返回 executable" in model.prompts[3]


def test_llm_skill_review_second_failure_blocks_dispatch():
    plan, translation = _review_plan_and_translation()
    rewritten = {
        "verdict": "rewritten",
        "summary": "尝试修复",
        "issues": [],
        "workflow_txt": translation["workflow_txt"],
        "workflow_json": translation["workflow_json"],
    }
    rejected = {
        "verdict": "not_executable",
        "summary": "仍缺少可证明的容器输入状态",
        "issues": [{
            "code": "unproven_input_state",
            "severity": "error",
            "step_numbers": [1],
            "reason": "仍缺少可证明的容器输入状态",
        }],
    }
    model = _SequencedModel([plan, translation, rewritten, rejected])
    previous = os.environ.get("CHEM_DEVICE_WORKFLOW_VERIFICATION")
    os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = "llm"
    try:
        agent = SingleDeviceAgent(
            model=model, workstation_loader=_WorkflowReviewLoader()
        )
        state = agent.run_state(RESEARCH_HANDOFF, exp_id="llm_review_fail")
    finally:
        if previous is None:
            os.environ.pop("CHEM_DEVICE_WORKFLOW_VERIFICATION", None)
        else:
            os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = previous

    package = state.terminal_package
    assert package["status"] == "manual_required"
    assert package["feedback_type"] == "human_review_required"
    assert package["feedback_route"] == "human"
    assert package["failure_scope"] == "device_plan"
    assert package["feasibility_accepted"] is True
    assert package["error_package"]["type"] == "device_plan_rewrite_rejected"
    assert package["workflow_repair"]["cycles"][0]["modification_count"] == 8
    assert package["workflow_repair"]["plan_level_rewrite_count"] == 1
    assert "unproven_input_state" in " ".join(
        package["dispatch_validation"]["errors"]
    )


def _quantity_research_handoff():
    return {
        "task": {"query": "分配前驱体用于 XPS 和 XRD", "current_stage": "表征分配"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "分配外部预制前驱体 A",
                "试剂/对象": "前驱体 A",
                "参数": "总量 0.180 mmol",
            }
        ],
    }


def _valid_quantity_plan():
    """One explicit Research amount with complete, cross-unit sidecars."""
    return {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "General_Material_Station_V1",
                "objective": "制备并分配前驱体 A",
                "operation_intent": "物料拿取",
                "containers": {"容器类型": "进样瓶", "容器编号": [1]},
                "source_macro_step": 1,
                "source_reagent_identity": "前驱体 A",
            }
        ],
        "quantity_adjustments": [],
        "batch_plan": [
            {
                "batch_id": "precursor_A_batch_1",
                "material_id": "前驱体 A",
                "research_material_identity": "前驱体 A",
                "research_source_refs": [
                    {
                        "source_path": "macro_action_steps[0].参数",
                        "source_macro_step": 1,
                        "source_field": "参数",
                        "source_context": "总量 0.180 mmol",
                        "material_identity": "前驱体 A",
                        "quantity": {"value": 0.180, "unit": "mmol"},
                    }
                ],
                "sample_id": "sample_A",
                "is_root_batch": True,
                "consumer_ids": ["XPS", "XRD"],
                "allocation": {
                    "XPS": {"value": 0.060, "unit": "mmol"},
                    "XRD": {"value": 120, "unit": "umol"},
                },
                "total_quantity": {"value": 0.180, "unit": "mmol"},
                "per_batch_quantity": {"value": 180, "unit": "umol"},
                "multiplicity": 1,
                "source_kind": "research",
                "source_refs": ["macro_action_steps[0].参数"],
                "calculation": "0.060 mmol + 120 umol = 0.180 mmol",
            }
        ],
        "material_ledger": {
            "entries": [
                {
                    "entry_id": "precursor_A_allocation",
                    "material_id": "前驱体 A",
                    "batch_id": "precursor_A_batch_1",
                    "sample_id": "sample_A",
                    "produced": {"value": 180, "unit": "umol"},
                    "consumed": {"value": 0.180, "unit": "mmol"},
                    "reserved": {"value": 0, "unit": "mmol"},
                    "balance": {"value": 0, "unit": "mmol"},
                    "consumers": [
                        {
                            "consumer_id": "XPS",
                            "allocation_id": "draw_xps",
                            "quantity": {"value": 60, "unit": "umol"},
                        },
                        {
                            "consumer_id": "XRD",
                            "allocation_id": "draw_xrd",
                            "quantity": {"value": 0.120, "unit": "mmol"},
                        },
                    ],
                    "source_kind": "derived",
                    "source_refs": ["batch_plan[0].allocation"],
                    "calculation": "180 umol - 60 umol - 0.120 mmol = 0 umol",
                }
            ]
        },
        "reagent_slot_plan": [],
        "container_plan": [],
        "temporal_adaptations": [],
        "offline_handoffs": [],
    }


def _quantity_audit(plan):
    return _quantity_audit_with_handoff(plan, _quantity_research_handoff())


def _quantity_audit_with_handoff(plan, handoff):
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )
    return agent._normalize_quantity_contract(
        plan, research_handoff=handoff
    )


def _quantity_audit_with_runtime_approvals(plan, handoff, approvals):
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )
    agent._active_trusted_human_quantity_approvals = copy.deepcopy(approvals)
    return agent._normalize_quantity_contract(plan, research_handoff=handoff)


def test_whole_batch_drying_can_flow_to_one_next_operation_without_fake_mass():
    handoff = {
        "task": {"query": "将湿样整批干燥后直接进入下一操作"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "整批干燥湿态前驱体",
                "试剂/对象": "前驱体 A",
                "参数": "将湿态样品整批干燥，不要求预先知道干粉总质量",
                "quantity_requirements": [
                    {
                        "kind": "whole_batch",
                        "material": "前驱体 A",
                        "source": "process_semantics",
                        "adjustability": "not_applicable",
                    }
                ],
            }
        ],
    }
    plan = {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "General_Material_Station_V1",
                "objective": "取得湿态前驱体 A",
                "operation_intent": "物料拿取",
                "source_macro_step": 1,
                "source_reagent_identity": "前驱体 A",
            },
            {
                "plan_step": 2,
                "workstation": "Drying_Oven_V1",
                "objective": "整批干燥并直接交给下一操作",
                "operation_intent": "烘干主流程",
                "material_event_kind": "state_change",
                "material_transition_ids": ["dry_whole_batch"],
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_reagent_identity": "前驱体 A",
            },
        ],
        "quantity_adjustments": [],
        "batch_plan": [
            {
                "batch_id": "wet_A",
                "material_id": "前驱体 A",
                "research_material_identity": "前驱体 A",
                "research_source_refs": [
                    {
                        "source_path": "macro_action_steps[0].参数",
                        "source_macro_step": 1,
                        "source_field": "参数",
                        "source_context": "将湿态样品整批干燥，不要求预先知道干粉总质量",
                        "material_identity": "前驱体 A",
                    }
                ],
                "sample_id": "sample_A",
                "is_root_batch": True,
                "quantity_mode": "whole_batch",
                "consumer_ids": ["material_transition:dry_whole_batch"],
                "source_kind": "research",
                "source_refs": ["macro_action_steps[0].参数"],
                "calculation": "whole batch; no numeric inventory",
            },
            {
                "batch_id": "dry_A",
                "material_id": "干燥前驱体 A",
                "parent_batch_id": "wet_A",
                "sample_id": "sample_A",
                "is_root_batch": False,
                "transition_kind": "state_change",
                "source_plan_steps": [2],
                "source_macro_steps": [1],
                "quantity_mode": "whole_batch",
                "consumer_ids": ["next_operation"],
                "source_kind": "derived",
                "source_refs": ["material_transition:dry_whole_batch"],
                "calculation": "whole batch continues without weighing",
            },
        ],
        "material_transitions": [
            {
                "transition_id": "dry_whole_batch",
                "transition_kind": "state_change",
                "quantity_basis": "whole_batch",
                "parent_batch_ids": ["wet_A"],
                "child_batch_ids": ["dry_A"],
                "source_plan_steps": [2],
                "source_macro_steps": [1],
                "before_material_state": "湿态前驱体",
                "after_material_state": "干燥前驱体",
            }
        ],
        "material_ledger": {
            "entries": [
                {
                    "entry_id": "wet_A_whole",
                    "material_id": "前驱体 A",
                    "batch_id": "wet_A",
                    "sample_id": "sample_A",
                    "quantity_mode": "whole_batch",
                    "consumer_ids": ["material_transition:dry_whole_batch"],
                    "source_kind": "research",
                    "source_refs": ["macro_action_steps[0].参数"],
                    "calculation": "structural whole-batch lineage",
                },
                {
                    "entry_id": "dry_A_whole",
                    "material_id": "干燥前驱体 A",
                    "batch_id": "dry_A",
                    "sample_id": "sample_A",
                    "quantity_mode": "whole_batch",
                    "consumer_ids": ["next_operation"],
                    "processing_step_refs": [2],
                    "source_kind": "derived",
                    "source_refs": ["material_transition:dry_whole_batch"],
                    "calculation": "structural whole-batch lineage",
                },
            ]
        },
    }
    result = _quantity_audit_with_handoff(plan, handoff)
    assert result["quantity_audit"]["status"] == "passed", result[
        "quantity_audit"
    ]["issues"]
    assert result["batch_plan"][1]["quantity_mode"] == "whole_batch"
    assert "total_quantity" not in result["batch_plan"][1]


def test_whole_batch_cannot_duplicate_inventory_across_consumers():
    plan = _valid_quantity_plan()
    batch = plan["batch_plan"][0]
    batch["quantity_mode"] = "whole_batch"
    batch.pop("total_quantity", None)
    batch.pop("per_batch_quantity", None)
    batch.pop("allocation", None)
    batch["research_source_refs"][0].pop("quantity", None)
    result = _quantity_audit(plan)
    assert "whole_batch_multiple_consumers_require_explicit_split" in (
        _quantity_issue_codes(result)
    )


def test_whole_batch_cannot_erase_fixed_or_unlabelled_research_quantity():
    plan = _valid_quantity_plan()
    batch = plan["batch_plan"][0]
    batch["quantity_mode"] = "whole_batch"
    batch.pop("total_quantity", None)
    batch.pop("per_batch_quantity", None)
    batch.pop("allocation", None)
    batch["consumer_ids"] = ["XPS"]
    batch["research_source_refs"][0].pop("quantity", None)

    result = _quantity_audit(plan)

    assert "whole_batch_not_authorized_by_research" in _quantity_issue_codes(
        result
    )


def _optional_execution_target_handoff():
    return {
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "制备 XRD 悬浊液",
                "试剂/对象": "干燥粉末",
                "参数": "称取 20 mg 干燥粉末用于 XRD",
                "quantity_requirements": [
                    {
                        "kind": "target_dose",
                        "material": "干燥粉末",
                        "value": 20.0,
                        "unit": "mg",
                        "source": "agent_proposed",
                        "adjustability": "scientific_review_required",
                        "owner": "device_execution",
                        "required_by": "agent_proposal",
                        "device_policy": "omit_if_not_skill_required",
                        "scientifically_fixed": False,
                    }
                ],
            }
        ]
    }


def test_optional_execution_target_requires_one_device_disposition():
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )
    findings = agent._quantity_requirement_disposition_findings(
        _optional_execution_target_handoff(),
        {
            "device_plan": [
                {
                    "plan_step": 1,
                    "workstation": "XRD_V1",
                    "source_macro_step": 1,
                    "source_macro_steps": [1],
                    "key_values": {"滴液体积": "50 μL"},
                }
            ]
        },
    )
    assert {item["type"] for item in findings} == {
        "missing_execution_quantity_disposition"
    }


def test_optional_20mg_can_be_omitted_when_xrd_skill_does_not_require_mass():
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )
    plan = {
        "quantity_requirement_dispositions": [
            {
                "source_macro_step": 1,
                "requirement_index": 0,
                "decision": "omit_as_nonessential",
                "reason": "XRD Skill requires suspension/drop volume, not powder mass",
                "evidence_refs": ["XRD_V1/XRD滴液检测全流程"],
                "requires_scientific_review": False,
            }
        ],
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "XRD_V1",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "key_values": {"滴液体积": "50 μL"},
                "notes": "整批干粉加入乙醇后形成悬浊液",
            }
        ],
    }
    assert agent._quantity_requirement_disposition_findings(
        _optional_execution_target_handoff(), plan
    ) == []


def test_omitted_execution_target_must_not_remain_in_device_step():
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )
    plan = {
        "quantity_requirement_dispositions": [
            {
                "source_macro_step": 1,
                "requirement_index": 0,
                "decision": "omit_as_nonessential",
                "reason": "not required",
                "evidence_refs": ["XRD_V1"],
                "requires_scientific_review": False,
            }
        ],
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "XRD_V1",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "key_values": {"粉末质量": "20 mg"},
            }
        ],
    }
    findings = agent._quantity_requirement_disposition_findings(
        _optional_execution_target_handoff(), plan
    )
    assert {item["type"] for item in findings} == {
        "omitted_execution_quantity_still_present"
    }


def test_device_model_may_retain_target_only_through_a_true_skill_binding():
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )
    plan = {
        "quantity_requirement_dispositions": [
            {
                "source_macro_step": 1,
                "requirement_index": 0,
                "decision": "bind_skill_setpoint",
                "plan_step": 1,
                "workstation": "Single_Channel_Solid_Weighing_Workstation_V1",
                "operation": "固体进样",
                "parameter": "进样质量",
                "reason": "the selected weighing operation requires a mass setpoint",
                "evidence_refs": [
                    "Single_Channel_Solid_Weighing_Workstation_V1/固体进样/进样质量"
                ],
                "requires_scientific_review": False,
            }
        ],
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "Single_Channel_Solid_Weighing_Workstation_V1",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "key_values": {"进样质量": "0.020 g"},
            }
        ],
    }
    findings = agent._quantity_requirement_disposition_findings(
        _optional_execution_target_handoff(), plan
    )
    assert findings == []


def test_optional_mass_target_cannot_bind_to_xrd_drop_volume_parameter():
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )
    plan = {
        "quantity_requirement_dispositions": [
            {
                "source_macro_step": 1,
                "requirement_index": 0,
                "decision": "bind_skill_setpoint",
                "plan_step": 1,
                "workstation": "XRD_V1",
                "operation": "XRD滴液检测全流程",
                "parameter": "滴液体积",
                "reason": "invalid attempt to preserve 20 mg",
                "evidence_refs": ["XRD_V1/XRD滴液检测全流程/滴液体积"],
                "requires_scientific_review": False,
            }
        ],
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "XRD_V1",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "key_values": {"粉末质量": "20 mg", "滴液体积": "20 μL"},
            }
        ],
    }
    handoff = _optional_execution_target_handoff()
    handoff["macro_action_steps"][0]["quantity_requirements"][0][
        "required_by"
    ] = "workstation_skill"
    findings = agent._quantity_requirement_disposition_findings(handoff, plan)
    assert {item["type"] for item in findings} == {
        "invalid_execution_quantity_skill_binding"
    }


def test_device_model_can_retain_optional_target_with_explicit_scientific_rationale():
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )
    plan = {
        "quantity_requirement_dispositions": [
            {
                "source_macro_step": 1,
                "requirement_index": 0,
                "decision": "retain_as_scientific_target",
                "reason": "保持跨样品相同上样量以满足冻结的定量比较设计",
                "evidence_refs": ["macro_action_steps[0].scientific_objective"],
                "requires_scientific_review": True,
            }
        ],
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "Single_Channel_Solid_Weighing_Workstation_V1",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "key_values": {"进样质量": "0.020 g"},
            }
        ],
    }
    assert agent._quantity_requirement_disposition_findings(
        _optional_execution_target_handoff(), plan
    ) == []


def test_device_quantity_decision_requires_model_rationale_and_evidence():
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )
    plan = {
        "quantity_requirement_dispositions": [
            {
                "source_macro_step": 1,
                "requirement_index": 0,
                "decision": "retain_as_scientific_target",
            }
        ],
        "device_plan": [],
    }
    findings = agent._quantity_requirement_disposition_findings(
        _optional_execution_target_handoff(), plan
    )
    assert {item["type"] for item in findings} == {
        "execution_quantity_decision_missing_rationale"
    }


def _quantity_issue_codes(result):
    return {
        item.get("code")
        for item in result["quantity_audit"]["issues"]
        if isinstance(item, dict)
    }


def test_mixed_quantity_normalization_is_idempotent_and_keeps_issue_scopes():
    plan = _valid_quantity_plan()
    plan["batch_plan"][0]["total_quantity"] = {
        "value": 0.170,
        "unit": "mmol",
    }
    plan["quantity_audit"] = {
        "status": "human_review_required",
        "issues": [
            {
                "code": "unknown_yield",
                "scope": "human_review_required",
                "message": "actual isolated yield is unknown",
            }
        ],
    }

    first = _quantity_audit(plan)
    second = _quantity_audit(first)

    assert second["quantity_audit"] == first["quantity_audit"]
    assert first["quantity_audit"]["assessment_source"] == (
        "deterministic_quantity_auditor"
    )
    deterministic = [
        item
        for item in second["quantity_audit"]["issues"]
        if item.get("scope") == "device_local_quantity"
    ]
    human = [
        item
        for item in second["quantity_audit"]["issues"]
        if item.get("scope") == "human_review_required"
    ]
    assert deterministic
    assert [item["code"] for item in human] == ["unknown_yield"]


def test_single_macro_explicit_quantity_requires_all_three_sidecars():
    plan = _valid_quantity_plan()
    plan.pop("quantity_adjustments")
    plan.pop("batch_plan")
    plan.pop("material_ledger")

    result = _quantity_audit(plan)

    assert result["quantity_audit"]["status"] == "failed"
    assert {
        "missing_quantity_adjustments_contract",
        "missing_batch_plan_contract",
        "missing_material_ledger_contract",
    } <= _quantity_issue_codes(result)


def test_quantity_nested_allocations_accept_cross_units_and_sum_split_draws():
    plan = _valid_quantity_plan()
    plan["material_ledger"]["entries"] = [
        {
            "entry_id": "production_and_xps_draw_1",
            "material_id": "前驱体 A",
            "batch_id": "precursor_A_batch_1",
            "sample_id": "sample_A",
            "consumer_id": "XPS",
            "allocation_id": "draw_xps_1",
            "produced": {"value": 0.180, "unit": "mmol"},
            "consumed": {"value": 40, "unit": "umol"},
            "source_kind": "derived",
            "source_refs": ["batch_plan[0].allocation.XPS"],
            "calculation": "first XPS aliquot = 40 umol",
        },
        {
            "entry_id": "xps_draw_2",
            "material_id": "前驱体 A",
            "batch_id": "precursor_A_batch_1",
            "sample_id": "sample_A",
            "consumer_id": "XPS",
            "allocation_id": "draw_xps_2",
            "consumed": {"value": 0.020, "unit": "mmol"},
            "source_kind": "device_operational",
            "source_refs": ["split transfer 2"],
            "calculation": "second XPS aliquot = 0.020 mmol",
        },
        {
            "entry_id": "xrd_draw",
            "material_id": "前驱体 A",
            "batch_id": "precursor_A_batch_1",
            "sample_id": "sample_A",
            "consumer_id": "XRD",
            "allocation_id": "draw_xrd",
            "consumed": {"value": 0.120, "unit": "mmol"},
            "source_kind": "derived",
            "source_refs": ["batch_plan[0].allocation.XRD"],
            "calculation": "XRD draw = 0.120 mmol",
        },
    ]

    result = _quantity_audit(plan)

    assert result["quantity_audit"]["status"] == "passed", result[
        "quantity_audit"
    ]["issues"]
    aggregate = result["material_ledger"]["aggregates"][0]
    assert abs(aggregate["consumer_allocations"]["XPS"]["value"] - 0.00006) < 1e-12
    assert abs(aggregate["consumer_allocations"]["XRD"]["value"] - 0.00012) < 1e-12
    assert abs(aggregate["balance"]["value"]) < 1e-12


def test_quantity_audit_rejects_planned_ledger_mismatch_orphan_and_unitless():
    mismatch = _valid_quantity_plan()
    mismatch["batch_plan"][0]["allocation"]["XPS"] = {
        "value": 70,
        "unit": "umol",
    }
    assert "batch_consumer_allocation_quantity_mismatch" in _quantity_issue_codes(
        _quantity_audit(mismatch)
    )

    orphan = _valid_quantity_plan()
    orphan["material_ledger"]["entries"].append(
        {
            "entry_id": "orphan_draw",
            "material_id": "orphan_material",
            "batch_id": "orphan_batch",
            "sample_id": "orphan_sample",
            "consumer_id": "orphan_consumer",
            "produced": {"value": 1, "unit": "mg"},
            "consumed": {"value": 1, "unit": "mg"},
            "source_kind": "device_operational",
            "source_refs": ["orphan fixture"],
            "calculation": "1 mg - 1 mg = 0 mg",
        }
    )
    assert "ledger_batch_missing_from_batch_plan" in _quantity_issue_codes(
        _quantity_audit(orphan)
    )

    unitless = _valid_quantity_plan()
    unitless["batch_plan"][0]["allocation"]["XPS"] = 0.060
    assert "batch_allocation_unit_missing_or_unknown" in _quantity_issue_codes(
        _quantity_audit(unitless)
    )


def test_a01_shaped_multiconsumer_scalar_is_not_guessed_or_silently_rewritten():
    plan = _valid_quantity_plan()
    batch = plan["batch_plan"][0]
    batch["consumer_ids"] = ["purification_1", "xrd_1"]
    batch["allocation"] = {"value": 0.180, "unit": "mmol"}
    plan["material_ledger"]["entries"] = [
        {
            "entry_id": "wet_batch_to_purification",
            "material_id": "前驱体 A",
            "batch_id": "precursor_A_batch_1",
            "sample_id": "sample_A",
            "consumer_id": "purification_1",
            "produced": {"value": 0.180, "unit": "mmol"},
            "consumed": {"value": 0.180, "unit": "mmol"},
            "reserved": {"value": 0, "unit": "mmol"},
            "balance": {"value": 0, "unit": "mmol"},
            "source_kind": "derived",
            "source_refs": ["macro_step:1"],
            "calculation": "0.180-0.180=0 mmol",
        }
    ]

    result = _quantity_audit(plan)
    codes = _quantity_issue_codes(result)

    assert "ambiguous_batch_allocation" in codes
    assert "batch_consumer_lineage_mismatch" in codes
    # The deterministic auditor diagnoses the ambiguity but never invents a
    # purification/XRD split or changes the material-stage semantics.
    assert result["batch_plan"][0]["allocation"] == {
        "value": 0.180,
        "unit": "mmol",
    }
    assert result["batch_plan"][0]["consumer_ids"] == [
        "purification_1",
        "xrd_1",
    ]


def test_batch_allocation_list_requires_explicit_consumer_ids_even_when_lengths_match():
    explicit = _valid_quantity_plan()
    explicit["batch_plan"][0]["allocation"] = [
        {
            "consumer_id": "XPS",
            "quantity": {"value": 0.060, "unit": "mmol"},
        },
        {
            "consumer_id": "XRD",
            "quantity": {"value": 0.120, "unit": "mmol"},
        },
    ]
    explicit_result = _quantity_audit(explicit)
    assert explicit_result["quantity_audit"]["status"] == "passed", (
        explicit_result["quantity_audit"]["issues"]
    )

    unkeyed = _valid_quantity_plan()
    # The order and length happen to align with ["XPS", "XRD"], but neither
    # position nor sorted consumer order is a valid allocation identity.
    unkeyed["batch_plan"][0]["allocation"] = [
        {"value": 0.060, "unit": "mmol"},
        {"value": 0.120, "unit": "mmol"},
    ]
    unkeyed_result = _quantity_audit(unkeyed)
    unkeyed_codes = _quantity_issue_codes(unkeyed_result)
    assert "ambiguous_batch_allocation" in unkeyed_codes
    assert "missing_batch_consumer_allocation" in unkeyed_codes
    declared, inferred_allocations, _ = (
        SingleDeviceAgent._batch_consumer_allocation_contract(
            unkeyed_result["batch_plan"][0]
        )
    )
    assert declared == {"XPS", "XRD"}
    assert inferred_allocations == {}


def test_batch_declared_and_allocated_consumer_sets_must_match_exactly():
    missing = _valid_quantity_plan()
    missing["batch_plan"][0]["allocation"] = {
        "XPS": {"value": 0.060, "unit": "mmol"}
    }
    missing_result = _quantity_audit(missing)
    assert "missing_batch_consumer_allocation" in _quantity_issue_codes(
        missing_result
    )
    assert missing_result["quantity_audit"]["checks"]["all_consumers_funded"] is False

    extra = _valid_quantity_plan()
    extra["batch_plan"][0]["consumer_ids"] = ["XRD"]
    extra["batch_plan"][0]["allocation"] = {
        "XPS": {"value": 0.180, "unit": "mmol"}
    }
    extra["material_ledger"]["entries"] = [
        {
            "entry_id": "xps_only",
            "material_id": "前驱体 A",
            "batch_id": "precursor_A_batch_1",
            "sample_id": "sample_A",
            "consumer_id": "XPS",
            "produced": {"value": 0.180, "unit": "mmol"},
            "consumed": {"value": 0.180, "unit": "mmol"},
            "reserved": {"value": 0, "unit": "mmol"},
            "balance": {"value": 0, "unit": "mmol"},
            "source_kind": "derived",
            "source_refs": ["macro_step:1"],
            "calculation": "0.180-0.180=0 mmol",
        }
    ]
    extra_result = _quantity_audit(extra)
    extra_codes = _quantity_issue_codes(extra_result)
    assert "missing_batch_consumer_allocation" in extra_codes
    assert "undeclared_batch_allocation_consumer" in extra_codes
    assert "batch_consumer_lineage_mismatch" in extra_codes

    exact = _quantity_audit(_valid_quantity_plan())
    exact_codes = _quantity_issue_codes(exact)
    assert "missing_batch_consumer_allocation" not in exact_codes
    assert "undeclared_batch_allocation_consumer" not in exact_codes
    assert exact["quantity_audit"]["status"] == "passed"


def test_batch_allocation_rejects_nested_id_conflict_and_mixed_schema():
    conflicting = _valid_quantity_plan()
    conflicting["batch_plan"][0]["consumer_ids"] = ["XPS"]
    conflicting["batch_plan"][0]["allocation"] = {
        "XPS": {
            "consumer_id": "XRD",
            "value": 0.180,
            "unit": "mmol",
        }
    }
    conflict_codes = _quantity_issue_codes(_quantity_audit(conflicting))
    assert "batch_allocation_consumer_id_conflict" in conflict_codes
    assert "missing_batch_consumer_allocation" in conflict_codes

    mixed = _valid_quantity_plan()
    mixed["batch_plan"][0]["allocation"] = {
        "consumer_id": "XPS",
        "quantity": {"value": 0.060, "unit": "mmol"},
        "XRD": {"value": 0.120, "unit": "mmol"},
    }
    mixed_codes = _quantity_issue_codes(_quantity_audit(mixed))
    assert "mixed_batch_allocation_schema" in mixed_codes


def _valid_material_transition_plan():
    plan = _valid_quantity_plan()
    plan["device_plan"].append(
        {
            "plan_step": 2,
            "workstation": "Drying_Oven_V1",
            "objective": "将湿态前驱体转为干燥前驱体",
            "operation_intent": "干燥",
            "material_event_kind": "process_same_material",
            "material_transition_ids": ["dry_A_transition"],
            "source_macro_step": 1,
            "source_macro_steps": [1],
            "source_reagent_identity": "前驱体 A",
        }
    )
    plan["batch_plan"] = [
        {
            "batch_id": "wet_A",
            "material_id": "前驱体 A",
            "research_material_identity": "前驱体 A",
            "research_source_refs": [
                {
                    "source_path": "macro_action_steps[0].参数",
                    "source_macro_step": 1,
                    "source_field": "参数",
                    "source_context": "总量 0.180 mmol",
                    "material_identity": "前驱体 A",
                    "quantity": {"value": 180, "unit": "umol"},
                }
            ],
            "sample_id": "sample_A",
            "is_root_batch": True,
            "total_quantity": {"value": 180, "unit": "umol"},
            "consumer_ids": ["material_transition:dry_A_transition"],
            "allocation": {
                "material_transition:dry_A_transition": {
                    "value": 0.180,
                    "unit": "mmol",
                }
            },
            "source_kind": "research",
            "source_refs": ["macro_action_steps[0].参数"],
            "calculation": "0.180 mmol wet precursor enters drying",
        },
        {
            "batch_id": "dry_A",
            "material_id": "dry_precursor_A",
            "parent_batch_id": "wet_A",
            "is_root_batch": False,
            "transition_kind": "process_same_material",
            "source_plan_steps": [2],
            "source_macro_steps": [1],
            "sample_id": "sample_A",
            "total_quantity": {"value": 0.180, "unit": "mmol"},
            "consumer_ids": ["XRD"],
            "allocation": {"XRD": {"value": 0.180, "unit": "mmol"}},
            "source_kind": "derived",
            "source_refs": ["material_transition:dry_A_transition"],
            "calculation": "measured dry batch allocated to XRD",
        },
    ]
    plan["material_transitions"] = [
        {
            "transition_id": "dry_A_transition",
            "transition_kind": "process_same_material",
            "quantity_basis": "conserved_inventory",
            "parent_batch_ids": ["wet_A"],
            "child_batch_ids": ["dry_A"],
            "source_plan_steps": [2],
            "source_macro_steps": [1],
            "input_allocations": [
                {
                    "batch_id": "wet_A",
                    "quantity": {"value": 0.180, "unit": "mmol"},
                }
            ],
            "output_allocations": [
                {
                    "batch_id": "dry_A",
                    "quantity": {"value": 180, "unit": "umol"},
                }
            ],
            "before_material_state": "wet precursor",
            "after_material_state": "dry precursor",
            "calculation_or_basis": "plan_step 2 state transition",
        }
    ]
    plan["material_ledger"]["entries"] = [
        {
            "entry_id": "wet_A_to_drying",
            "material_id": "前驱体 A",
            "batch_id": "wet_A",
            "sample_id": "sample_A",
            "consumer_id": "material_transition:dry_A_transition",
            "produced": {"value": 0.180, "unit": "mmol"},
            "consumed": {"value": 0.180, "unit": "mmol"},
            "reserved": {"value": 0, "unit": "mmol"},
            "balance": {"value": 0, "unit": "mmol"},
            "source_kind": "research",
            "source_refs": ["macro_action_steps[0].参数"],
            "calculation": "0.180-0.180=0 mmol",
        },
        {
            "entry_id": "dry_A_to_xrd",
            "material_id": "dry_precursor_A",
            "batch_id": "dry_A",
            "sample_id": "sample_A",
            "consumer_id": "XRD",
            "processing_step_refs": [2],
            "produced": {"value": 0.180, "unit": "mmol"},
            "consumed": {"value": 0.180, "unit": "mmol"},
            "reserved": {"value": 0, "unit": "mmol"},
            "balance": {"value": 0, "unit": "mmol"},
            "source_kind": "derived",
            "source_refs": ["material_transition:dry_A_transition"],
            "calculation": "0.180-0.180=0 mmol",
        },
    ]
    return plan


def test_material_transition_contract_accepts_root_and_explicit_parent_child_lineage():
    root_only = _quantity_audit(_valid_quantity_plan())
    assert root_only["quantity_audit"]["status"] == "passed"
    assert root_only["quantity_audit"]["checks"][
        "material_transition_lineage"
    ] is True

    transitioned = _quantity_audit(_valid_material_transition_plan())
    assert transitioned["quantity_audit"]["status"] == "passed", (
        transitioned["quantity_audit"]["issues"]
    )
    assert transitioned["quantity_audit"]["checks"][
        "material_transition_lineage"
    ] is True


def test_material_transition_contract_rejects_deleted_transition_disguised_as_root():
    disguised = _valid_material_transition_plan()
    child = disguised["batch_plan"][1]
    child["is_root_batch"] = True
    child.pop("parent_batch_id")
    child.pop("transition_kind")
    child.pop("source_plan_steps")
    child.pop("source_macro_steps")
    disguised["material_transitions"] = []

    result = _quantity_audit(disguised)
    codes = _quantity_issue_codes(result)

    assert "derived_batch_cannot_be_root" in codes
    assert result["quantity_audit"]["checks"][
        "material_transition_lineage"
    ] is False


def test_material_transition_contract_rejects_missing_or_invalid_id_refs():
    invalid = _valid_material_transition_plan()
    invalid["batch_plan"][1]["source_plan_steps"] = [999]
    invalid["material_transitions"][0]["parent_batch_ids"] = ["missing_parent"]
    invalid["material_transitions"][0]["source_plan_steps"] = [999]
    invalid["material_ledger"]["entries"][1]["processing_step_refs"] = [999]

    result = _quantity_audit(invalid)
    codes = _quantity_issue_codes(result)

    assert "invalid_batch_source_plan_step_ref" in codes
    assert "invalid_material_transition_batch_ref" in codes
    assert "invalid_material_transition_plan_ref" in codes
    assert "material_transition_parent_mismatch" in codes
    assert result["quantity_audit"]["checks"][
        "material_transition_lineage"
    ] is False

    missing = _valid_material_transition_plan()
    missing["material_transitions"] = []
    missing["material_ledger"]["entries"][1].pop("processing_step_refs")
    missing_codes = _quantity_issue_codes(_quantity_audit(missing))
    assert "missing_or_ambiguous_batch_material_transition" in missing_codes
    assert "missing_ledger_processing_step_refs" in missing_codes


def test_quantity_domain_rejects_negative_nan_and_invalid_multiplicity():
    negative = _valid_quantity_plan()
    negative["batch_plan"][0]["allocation"] = {
        "XPS": {"value": -0.060, "unit": "mmol"},
        "XRD": {"value": 0.240, "unit": "mmol"},
    }
    negative["material_ledger"]["entries"][0]["consumers"] = [
        {
            "consumer_id": "XPS",
            "allocation_id": "draw_xps",
            "quantity": {"value": -0.060, "unit": "mmol"},
        },
        {
            "consumer_id": "XRD",
            "allocation_id": "draw_xrd",
            "quantity": {"value": 0.240, "unit": "mmol"},
        },
    ]
    negative_codes = _quantity_issue_codes(_quantity_audit(negative))
    assert "nonpositive_batch_consumer_allocation" in negative_codes
    assert "nonpositive_ledger_consumer_allocation" in negative_codes

    negative_reserved = _valid_quantity_plan()
    ledger = negative_reserved["material_ledger"]["entries"][0]
    ledger["reserved"] = {"value": -1, "unit": "mmol"}
    ledger["balance"] = {"value": 1, "unit": "mmol"}
    assert "negative_ledger_quantity" in _quantity_issue_codes(
        _quantity_audit(negative_reserved)
    )

    nan_plan = _valid_quantity_plan()
    nan_plan["batch_plan"][0]["allocation"]["XPS"] = {
        "value": float("nan"),
        "unit": "mmol",
    }
    nan_plan["material_ledger"]["entries"][0]["consumers"][0]["quantity"] = {
        "value": float("nan"),
        "unit": "mmol",
    }
    nan_codes = _quantity_issue_codes(_quantity_audit(nan_plan))
    assert "batch_allocation_unit_missing_or_unknown" in nan_codes
    assert "missing_nested_consumer_quantity" in nan_codes

    for invalid_multiplicity in (-1, 1.5, float("nan")):
        invalid = _valid_quantity_plan()
        invalid["batch_plan"][0]["multiplicity"] = invalid_multiplicity
        if invalid_multiplicity == -1:
            invalid["batch_plan"][0]["per_batch_quantity"] = {
                "value": -0.180,
                "unit": "mmol",
            }
        codes = _quantity_issue_codes(_quantity_audit(invalid))
        assert "invalid_batch_multiplicity" in codes


def test_scientific_adjustment_cannot_authorize_negative_inventory():
    plan = _valid_quantity_plan()
    plan["quantity_adjustments"] = [
        {
            "adjustment_id": "negative_after",
            "kind": "amount_change",
            "before": {"value": 0.180, "unit": "mmol"},
            "after": {"value": -0.180, "unit": "mmol"},
            "source_kind": "device_operational",
            "source_refs": ["macro_step:1"],
            "calculation": "invalid negative inventory",
            "requires_scientific_review": True,
        }
    ]
    codes = _quantity_issue_codes(_quantity_audit(plan))
    assert "negative_quantity_adjustment" in codes


def test_transition_edge_flow_prevents_scale_fabrication_and_double_spend():
    fabricated = _valid_material_transition_plan()
    child = fabricated["batch_plan"][1]
    child["total_quantity"] = {"value": 999, "unit": "mmol"}
    child["allocation"] = {"XRD": {"value": 999, "unit": "mmol"}}
    transition = fabricated["material_transitions"][0]
    transition["output_allocations"][0]["quantity"] = {
        "value": 999,
        "unit": "mmol",
    }
    child_ledger = fabricated["material_ledger"]["entries"][1]
    child_ledger["produced"] = {"value": 999, "unit": "mmol"}
    child_ledger["consumed"] = {"value": 999, "unit": "mmol"}
    fabricated_codes = _quantity_issue_codes(_quantity_audit(fabricated))
    assert "conserved_transition_quantity_mismatch" in fabricated_codes

    double_spend = _valid_material_transition_plan()
    parent = double_spend["batch_plan"][0]
    parent["consumer_ids"] = ["XPS"]
    parent["allocation"] = {"XPS": {"value": 0.180, "unit": "mmol"}}
    parent_ledger = double_spend["material_ledger"]["entries"][0]
    parent_ledger["consumer_id"] = "XPS"
    double_spend_codes = _quantity_issue_codes(_quantity_audit(double_spend))
    assert "transition_input_ledger_draw_missing_or_ambiguous" in double_spend_codes

    hidden_child_scale = _valid_material_transition_plan()
    hidden_child_scale["batch_plan"][1]["allocation"] = {
        "XRD": {"value": 999, "unit": "mmol"}
    }
    child_ledger = hidden_child_scale["material_ledger"]["entries"][1]
    child_ledger["produced"] = {"value": 999, "unit": "mmol"}
    child_ledger["consumed"] = {"value": 999, "unit": "mmol"}
    hidden_codes = _quantity_issue_codes(_quantity_audit(hidden_child_scale))
    assert "batch_ledger_produced_quantity_mismatch" in hidden_codes
    assert "transition_output_ledger_quantity_mismatch" in hidden_codes


def _as_state_change(plan):
    transitioned = copy.deepcopy(plan)
    transitioned["device_plan"][1]["material_event_kind"] = "state_change"
    transitioned["batch_plan"][1]["transition_kind"] = "state_change"
    transitioned["material_transitions"][0]["transition_kind"] = "state_change"
    return transitioned


def test_state_change_requires_verified_yield_evidence_and_sample_binding():
    unknown = _as_state_change(_valid_material_transition_plan())
    unknown["material_transitions"][0]["quantity_basis"] = "measured_observation"
    unknown_result = _quantity_audit(unknown)
    assert unknown_result["quantity_audit"]["status"] == "human_review_required"
    assert "unknown_yield" in _quantity_issue_codes(unknown_result)

    observation = {
        "observation_id": "obs_dry_A",
        "sample_id": "sample_A",
        "material_id": "dry_precursor_A",
        "batch_id": "dry_A",
        "measured_quantity": "实测干粉 0.180 mmol",
    }
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )
    measured = _as_state_change(_valid_material_transition_plan())
    measured_transition = measured["material_transitions"][0]
    measured_transition["quantity_basis"] = "measured_observation"
    handoff = _quantity_research_handoff()
    handoff["observations"] = [observation]
    catalog = agent._observation_evidence_catalog(handoff)
    assert len(catalog) == 1
    measured_transition["measurement_artifact"] = copy.deepcopy(
        catalog[0]["measurement_artifact"]
    )
    measured_result = _quantity_audit_with_handoff(measured, handoff)
    assert measured_result["quantity_audit"]["status"] == "passed", (
        measured_result["quantity_audit"]["issues"]
    )

    wrong_sample_handoff = copy.deepcopy(handoff)
    wrong_sample_handoff["observations"][0]["sample_id"] = "OTHER_SAMPLE"
    measured["material_transitions"][0]["measurement_artifact"][
        "artifact_digest"
    ] = agent._stable_digest(
        wrong_sample_handoff["observations"][0], prefix="observation"
    )
    wrong_result = _quantity_audit_with_handoff(measured, wrong_sample_handoff)
    assert wrong_result["quantity_audit"]["status"] == "human_review_required"
    assert "unknown_yield" in _quantity_issue_codes(wrong_result)

    duplicate_handoff = copy.deepcopy(handoff)
    duplicate_handoff["observations"].append(copy.deepcopy(observation))
    assert agent._observation_evidence_catalog(duplicate_handoff) == []
    duplicate_result = _quantity_audit_with_handoff(measured, duplicate_handoff)
    assert duplicate_result["quantity_audit"]["status"] == "human_review_required"


def test_mixed_quantity_issues_rewrite_once_without_forging_unknown_yield():
    initial = _as_state_change(_valid_material_transition_plan())
    initial["material_transitions"][0]["quantity_basis"] = (
        "measured_observation"
    )
    initial["batch_plan"][0]["total_quantity"] = {
        "value": 0.170,
        "unit": "mmol",
    }

    repaired = _as_state_change(_valid_material_transition_plan())
    repaired["material_transitions"][0].update(
        {
            "quantity_basis": "measured_observation",
            "measurement_artifact": {
                "observation_id": "invented_observation",
                "artifact_digest": "invented_digest",
                "sample_id": "sample_A",
                "material_id": "dry_precursor_A",
                "batch_id": "dry_A",
                "quantity_source_field": "measured_quantity",
                "quantity_source_context": "invented 0.180 mmol yield",
            },
        }
    )
    # A rewrite's self-declared audit is not evidence and must be recomputed.
    repaired["quantity_audit"] = {"status": "passed", "issues": []}

    class QuantityRewriteProbe(SingleDeviceAgent):
        def __init__(self, repaired_plan):
            super().__init__(
                model=RaisingModel(),
                workstation_loader=FakeWorkstationLoader(),
            )
            self.repaired_plan = repaired_plan
            self.rewrite_calls = 0
            self.translation_calls = 0

        def _invoke_device_plan_repair(
            self, state, current_plan, failed_workflow
        ):
            self.rewrite_calls += 1
            return copy.deepcopy(self.repaired_plan)

        def _validate_repaired_device_plan(
            self, state, previous_plan, repaired_plan
        ):
            return copy.deepcopy(repaired_plan), []

        def _translate_and_verify(self, state, plan_result):
            self.translation_calls += 1
            raise AssertionError(
                "quantity-blocked plans must not reach workflow translation"
            )

    agent = QuantityRewriteProbe(repaired)
    state = SingleDeviceAgentState(
        research_handoff=_quantity_research_handoff(),
        exp_id="mixed_quantity_rewrite",
        feasibility_accepted=True,
        feasibility_certificate={"accepted": True},
    )

    result = agent._run_accepted_device_plan(
        state,
        initial,
        allow_plan_rewrite=True,
        resumed_from_manual=False,
    )

    assert agent.rewrite_calls == 1
    assert state.device_plan_rewrite_count == 1
    assert agent.translation_calls == 0
    assert result["status"] == "manual_required"
    assert result["error_package"]["type"] == (
        "device_quantity_human_review_required"
    )
    assert _quantity_issue_codes(result) == {"unknown_yield"}
    unknown = result["quantity_audit"]["issues"][0]
    assert unknown["scope"] == "human_review_required"


def _unapproved_planning_yield_plan():
    unapproved = _as_state_change(_valid_material_transition_plan())
    unapproved["batch_plan"][1]["total_quantity"] = {
        "value": 0.090,
        "unit": "mmol",
    }
    unapproved["batch_plan"][1]["allocation"] = {
        "XRD": {"value": 0.090, "unit": "mmol"}
    }
    unapproved["material_transitions"][0].update(
        {
            "quantity_basis": "planning_yield_lower_bound",
            "yield_lower_bound": 0.5,
            "output_allocations": [
                {
                    "batch_id": "dry_A",
                    "quantity": {"value": 0.090, "unit": "mmol"},
                }
            ],
        }
    )
    child_ledger = unapproved["material_ledger"]["entries"][1]
    child_ledger["produced"] = {"value": 0.090, "unit": "mmol"}
    child_ledger["consumed"] = {"value": 0.090, "unit": "mmol"}
    return unapproved


def test_planning_yield_lower_bound_requires_trusted_human_approval():
    unapproved = _unapproved_planning_yield_plan()
    unapproved_result = _quantity_audit(unapproved)
    assert unapproved_result["quantity_audit"]["status"] == "human_review_required"
    assert "unknown_yield" in _quantity_issue_codes(unapproved_result)

    forged = copy.deepcopy(unapproved)
    forged["validated_human_quantity_approvals"] = [
        {
            "transition_id": "dry_A_transition",
            "batch_id": "dry_A",
            "sample_id": "sample_A",
            "material_id": "dry_precursor_A",
            "approval_basis": "planning_yield_lower_bound",
            "approved_quantity": {"value": 0.090, "unit": "mmol"},
        }
    ]
    forged_result = _quantity_audit(forged)
    assert forged_result["quantity_audit"]["status"] == "human_review_required"
    forged["_trusted_human_quantity_approvals"] = copy.deepcopy(
        forged["validated_human_quantity_approvals"]
    )
    private_forge_result = _quantity_audit(forged)
    assert (
        private_forge_result["quantity_audit"]["status"]
        == "human_review_required"
    )

    runtime_approvals = [
        {
            "transition_id": "dry_A_transition",
            "batch_id": "dry_A",
            "sample_id": "sample_A",
            "material_id": "dry_precursor_A",
            "approval_basis": "planning_yield_lower_bound",
            "attestation_kind": "human_approved_planning_bound",
            "approved_quantity": {"value": 0.090, "unit": "mmol"},
            "yield_lower_bound": 0.5,
            "scientific_review": True,
            "acknowledges_scientific_review": True,
        }
    ]
    trusted_result = _quantity_audit_with_runtime_approvals(
        unapproved, _quantity_research_handoff(), runtime_approvals
    )
    assert trusted_result["quantity_audit"]["status"] == "passed", trusted_result[
        "quantity_audit"
    ]["issues"]

    exceeded = copy.deepcopy(unapproved)
    exceeded["material_transitions"][0]["yield_lower_bound"] = 0.4
    runtime_approvals[0]["yield_lower_bound"] = 0.4
    codes = _quantity_issue_codes(
        _quantity_audit_with_runtime_approvals(
            exceeded, _quantity_research_handoff(), runtime_approvals
        )
    )
    assert "planning_yield_allocation_exceeded" in codes


def _single_root_plan(material_id, quantity, unit="mL"):
    plan = _valid_quantity_plan()
    batch = plan["batch_plan"][0]
    batch.update(
        {
            "material_id": material_id,
            "research_material_identity": material_id,
            "total_quantity": {"value": quantity, "unit": unit},
            "per_batch_quantity": {"value": quantity, "unit": unit},
            "allocation": {"XPS": {"value": quantity, "unit": unit}},
            "consumer_ids": ["XPS"],
        }
    )
    ledger = plan["material_ledger"]["entries"][0]
    ledger.update(
        {
            "material_id": material_id,
            "produced": {"value": quantity, "unit": unit},
            "consumed": {"value": quantity, "unit": unit},
            "reserved": {"value": 0, "unit": unit},
            "balance": {"value": 0, "unit": unit},
            "consumers": [
                {
                    "consumer_id": "XPS",
                    "allocation_id": "draw_xps",
                    "quantity": {"value": quantity, "unit": unit},
                }
            ],
        }
    )
    return plan


def test_research_root_provenance_rejects_quantity_and_material_fabrication():
    changed = _valid_quantity_plan()
    batch = changed["batch_plan"][0]
    batch["total_quantity"] = {"value": 999, "unit": "mmol"}
    batch["per_batch_quantity"] = {"value": 999, "unit": "mmol"}
    batch["allocation"] = {"XPS": {"value": 999, "unit": "mmol"}}
    batch["consumer_ids"] = ["XPS"]
    ledger = changed["material_ledger"]["entries"][0]
    ledger["produced"] = {"value": 999, "unit": "mmol"}
    ledger["consumed"] = {"value": 999, "unit": "mmol"}
    ledger["consumers"] = [
        {
            "consumer_id": "XPS",
            "allocation_id": "draw_xps",
            "quantity": {"value": 999, "unit": "mmol"},
        }
    ]
    assert "unauthorized_research_quantity_change" in _quantity_issue_codes(
        _quantity_audit(changed)
    )

    unauthorized = _valid_quantity_plan()
    unauthorized["batch_plan"][0]["material_id"] = "未授权物料 B"
    unauthorized["batch_plan"][0]["research_material_identity"] = "未授权物料 B"
    unauthorized["material_ledger"]["entries"][0]["material_id"] = "未授权物料 B"
    assert "research_root_material_identity_mismatch" in _quantity_issue_codes(
        _quantity_audit(unauthorized)
    )

    # A pure unit conversion remains valid.
    converted = _valid_quantity_plan()
    converted["batch_plan"][0]["total_quantity"] = {
        "value": 180,
        "unit": "umol",
    }
    converted_result = _quantity_audit(converted)
    assert converted_result["quantity_audit"]["status"] == "passed", (
        converted_result["quantity_audit"]["issues"]
    )


def test_same_research_quantity_span_cannot_fund_multiple_identity_variants():
    handoff = {
        "task": {"query": "use stock"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "分配外部预配原液",
                "试剂/对象": "Ni(NO3)2 原液",
                "参数": "取 1.5 mL Ni(NO3)2 原液",
            }
        ],
    }
    plan = _single_root_plan("Ni(NO3)2", 1.5)
    plan["device_plan"][0]["source_reagent_identity"] = "Ni(NO3)2 原液"
    root = plan["batch_plan"][0]
    root["research_source_refs"] = [
        {
            "source_path": "macro_action_steps[0].参数",
            "source_macro_step": 1,
            "source_field": "参数",
            "source_context": "1.5 mL Ni(NO3)2 原液",
            "material_identity": "Ni(NO3)2",
            "quantity": {"value": 1.5, "unit": "mL"},
        }
    ]
    root["source_refs"] = ["macro_action_steps[0].参数"]
    clone = copy.deepcopy(root)
    clone.update(
        {
            "batch_id": "nickel_alias_root",
            "sample_id": "sample_alias",
            "material_id": "Ni(NO3)2 原液",
            "research_material_identity": "Ni(NO3)2 原液",
        }
    )
    clone["research_source_refs"][0]["material_identity"] = "Ni(NO3)2 原液"
    plan["batch_plan"].append(clone)
    clone_ledger = copy.deepcopy(plan["material_ledger"]["entries"][0])
    clone_ledger.update(
        {
            "entry_id": "nickel_alias_draw",
            "batch_id": "nickel_alias_root",
            "sample_id": "sample_alias",
            "material_id": "Ni(NO3)2 原液",
        }
    )
    plan["material_ledger"]["entries"].append(clone_ledger)
    codes = _quantity_issue_codes(_quantity_audit_with_handoff(plan, handoff))
    assert "research_root_source_reused" in codes

    for weak_identity in ("Ni", "原液"):
        weak = copy.deepcopy(plan)
        weak["batch_plan"] = weak["batch_plan"][:1]
        weak["material_ledger"]["entries"] = weak["material_ledger"]["entries"][:1]
        weak["batch_plan"][0]["material_id"] = weak_identity
        weak["batch_plan"][0]["research_material_identity"] = weak_identity
        weak["batch_plan"][0]["research_source_refs"][0][
            "material_identity"
        ] = weak_identity
        weak["material_ledger"]["entries"][0]["material_id"] = weak_identity
        weak_codes = _quantity_issue_codes(
            _quantity_audit_with_handoff(weak, handoff)
        )
        assert "invalid_research_root_material_identity" in weak_codes


def test_research_quantity_parser_does_not_turn_concentration_into_inventory():
    for text in (
        "0.10 mol L−1 Ni(NO3)2",
        "0.30 mol·L−1 NaOH",
        "0.20 mol L-1 Fe(NO3)3",
    ):
        extracted = SingleDeviceAgent._extract_explicit_quantities(text)
        assert extracted
        assert {item["dimension"] for item in extracted} == {"concentration"}

    handoff = {
        "task": {"query": "stock concentration"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "分配外部预配 NaOH 原液",
                "试剂/对象": "NaOH 原液",
                "参数": "0.30 mol L−1 NaOH 原液",
            }
        ],
    }
    plan = _single_root_plan("NaOH", 0.30, unit="M")
    plan["device_plan"][0]["source_reagent_identity"] = "NaOH 原液"
    root = plan["batch_plan"][0]
    root["research_source_refs"] = [
        {
            "source_path": "macro_action_steps[0].参数",
            "source_macro_step": 1,
            "source_field": "参数",
            "source_context": "0.30 mol L−1 NaOH 原液",
            "material_identity": "NaOH",
            "quantity": {"value": 0.30, "unit": "M"},
        }
    ]
    root["source_refs"] = ["macro_action_steps[0].参数"]
    codes = _quantity_issue_codes(_quantity_audit_with_handoff(plan, handoff))
    assert "research_root_quantity_requires_extensive_dimension" in codes
    assert "batch_quantity_requires_extensive_dimension" in codes
    assert "batch_allocation_requires_extensive_quantity" in codes


def test_combined_research_identity_binds_quantity_to_nearest_full_species():
    handoff = {
        "task": {"query": "A01 stock split"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "分配外部预配原液",
                "试剂/对象": (
                    "Ni(NO3)2·6H2O 原液、Fe(NO3)3·9H2O 原液、"
                    "NaOH 原液、去离子水"
                ),
                "参数": (
                    "取 1.5 mL Ni(NO3)2·6H2O 原液、0.5 mL Fe(NO3)3·9H2O "
                    "原液、2.0 mL NaOH 原液、2.0 mL 去离子水"
                ),
            }
        ],
    }
    valid = _single_root_plan("Ni(NO3)2·6H2O", 1.5)
    valid["device_plan"][0]["source_reagent_identity"] = "Ni(NO3)2·6H2O 原液"
    root = valid["batch_plan"][0]
    root["research_source_refs"] = [
        {
            "source_path": "macro_action_steps[0].参数",
            "source_macro_step": 1,
            "source_field": "参数",
            "source_context": "1.5 mL Ni(NO3)2·6H2O 原液",
            "material_identity": "Ni(NO3)2·6H2O",
            "quantity": {"value": 1.5, "unit": "mL"},
        }
    ]
    root["source_refs"] = ["macro_action_steps[0].参数"]
    result = _quantity_audit_with_handoff(valid, handoff)
    assert result["quantity_audit"]["status"] == "passed", result[
        "quantity_audit"
    ]["issues"]

    stolen = copy.deepcopy(valid)
    stolen_root = stolen["batch_plan"][0]
    stolen_root["total_quantity"] = {"value": 0.5, "unit": "mL"}
    stolen_root["per_batch_quantity"] = {"value": 0.5, "unit": "mL"}
    stolen_root["allocation"] = {"XPS": {"value": 0.5, "unit": "mL"}}
    stolen_root["research_source_refs"][0].update(
        {
            "source_context": "0.5 mL Fe(NO3)3·9H2O 原液",
            "quantity": {"value": 0.5, "unit": "mL"},
        }
    )
    stolen_ledger = stolen["material_ledger"]["entries"][0]
    stolen_ledger["produced"] = {"value": 0.5, "unit": "mL"}
    stolen_ledger["consumed"] = {"value": 0.5, "unit": "mL"}
    stolen_ledger["consumers"][0]["quantity"] = {
        "value": 0.5,
        "unit": "mL",
    }
    codes = _quantity_issue_codes(_quantity_audit_with_handoff(stolen, handoff))
    assert "research_root_identity_quantity_context_mismatch" in codes


def test_per_batch_multiplicity_cannot_borrow_other_control_group_count():
    handoff = {
        "task": {"query": "cohort quantities"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "分配外部预配盐",
                "试剂/对象": "Ni(NO3)2、Fe(NO3)3",
                "参数": (
                    "每批 0.180 mmol Ni(NO3)2；Fe 对照组独立制备 999 批"
                ),
            }
        ],
    }
    plan = _single_root_plan("Ni(NO3)2", 179.82, unit="mmol")
    plan["device_plan"][0]["source_reagent_identity"] = "Ni(NO3)2"
    root = plan["batch_plan"][0]
    root.update(
        {
            "per_batch_quantity": {"value": 0.180, "unit": "mmol"},
            "multiplicity": 999,
            "source_refs": ["macro_action_steps[0].参数"],
            "research_source_refs": [
                {
                    "source_path": "macro_action_steps[0].参数",
                    "source_macro_step": 1,
                    "source_field": "参数",
                    "source_context": "每批 0.180 mmol Ni(NO3)2",
                    "material_identity": "Ni(NO3)2",
                    "quantity": {"value": 0.180, "unit": "mmol"},
                    "quantity_scope": "per_batch",
                    "multiplicity_ref": {
                        "source_path": "macro_action_steps[0].参数",
                        "source_context": "Fe 对照组独立制备 999 批",
                        "count": 999,
                    },
                }
            ],
        }
    )
    codes = _quantity_issue_codes(_quantity_audit_with_handoff(plan, handoff))
    assert "invalid_research_root_multiplicity_provenance" in codes


def test_a01_per_batch_and_operation_repeat_provenance_are_expressible():
    batch_handoff = {
        "task": {"query": "A01 independent batches"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "定义样品矩阵",
                "试剂/对象": "所有样品",
                "参数": "每种样品独立制备 3 批",
            },
            {
                "步骤序号": 2,
                "操作": "分配外部预配 NaOH 原液",
                "试剂/对象": "NaOH 原液",
                "参数": "各批约 4 mL NaOH 原液",
            },
        ],
    }
    batch_plan = _single_root_plan("NaOH", 12, unit="mL")
    batch_plan["device_plan"][0].update(
        {"source_macro_step": 2, "source_reagent_identity": "NaOH 原液"}
    )
    root = batch_plan["batch_plan"][0]
    root.update(
        {
            "per_batch_quantity": {"value": 4, "unit": "mL"},
            "multiplicity": 3,
            "source_refs": ["macro_action_steps[1].参数"],
            "research_source_refs": [
                {
                    "source_path": "macro_action_steps[1].参数",
                    "source_macro_step": 2,
                    "source_field": "参数",
                    "source_context": "4 mL NaOH 原液",
                    "material_identity": "NaOH",
                    "quantity": {"value": 4, "unit": "mL"},
                    "quantity_scope": "per_batch",
                    "multiplicity_ref": {
                        "source_path": "macro_action_steps[0].参数",
                        "source_context": "每种样品独立制备 3 批",
                        "count": 3,
                    },
                }
            ],
        }
    )
    batch_result = _quantity_audit_with_handoff(batch_plan, batch_handoff)
    assert batch_result["quantity_audit"]["status"] == "passed", batch_result[
        "quantity_audit"
    ]["issues"]

    wash_handoff = {
        "task": {"query": "A01 repeated wash"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "分配外部预配洗液",
                "试剂/对象": "去离子水",
                "参数": "每次加入 3 mL 去离子水，重复洗涤 3 次",
            }
        ],
    }
    wash_plan = _single_root_plan("去离子水", 9, unit="mL")
    wash_plan["device_plan"][0]["source_reagent_identity"] = "去离子水"
    wash_root = wash_plan["batch_plan"][0]
    wash_root.update(
        {
            "per_batch_quantity": {"value": 9, "unit": "mL"},
            "multiplicity": 1,
            "operation_repeat_count": 3,
            "source_refs": ["macro_action_steps[0].参数"],
            "research_source_refs": [
                {
                    "source_path": "macro_action_steps[0].参数",
                    "source_macro_step": 1,
                    "source_field": "参数",
                    "source_context": "3 mL 去离子水",
                    "material_identity": "去离子水",
                    "quantity": {"value": 3, "unit": "mL"},
                    "quantity_scope": "per_operation",
                    "operation_repeat_ref": {
                        "source_path": "macro_action_steps[0].参数",
                        "source_context": (
                            "每次加入 3 mL 去离子水，重复洗涤 3 次"
                        ),
                        "count": 3,
                    },
                }
            ],
        }
    )
    wash_result = _quantity_audit_with_handoff(wash_plan, wash_handoff)
    assert wash_result["quantity_audit"]["status"] == "passed", wash_result[
        "quantity_audit"
    ]["issues"]

    misuse = copy.deepcopy(wash_plan)
    misuse_root = misuse["batch_plan"][0]
    misuse_root["operation_repeat_count"] = 1
    misuse_root["per_batch_quantity"] = {"value": 3, "unit": "mL"}
    misuse_root["multiplicity"] = 3
    misuse_ref = misuse_root["research_source_refs"][0]
    misuse_ref["quantity_scope"] = "per_batch"
    misuse_ref.pop("operation_repeat_ref")
    misuse_ref["multiplicity_ref"] = {
        "source_path": "macro_action_steps[0].参数",
        "source_context": "重复洗涤 3 次",
        "count": 3,
    }
    misuse_codes = _quantity_issue_codes(
        _quantity_audit_with_handoff(misuse, wash_handoff)
    )
    assert "invalid_research_root_multiplicity_provenance" in misuse_codes


def test_frozen_synthesis_macro_cannot_be_reduced_to_material_pickup():
    handoff = {
        "task": {"query": "synthesize product"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "共沉淀合成 Ni(OH)2 产物",
                "试剂/对象": "Ni(NO3)2",
                "参数": "投料 0.180 mmol Ni(NO3)2",
            }
        ],
    }
    plan = _single_root_plan("Ni(NO3)2", 0.180, unit="mmol")
    plan["device_plan"][0]["source_reagent_identity"] = "Ni(NO3)2"
    root = plan["batch_plan"][0]
    root["research_source_refs"] = [
        {
            "source_path": "macro_action_steps[0].参数",
            "source_macro_step": 1,
            "source_field": "参数",
            "source_context": "0.180 mmol Ni(NO3)2",
            "material_identity": "Ni(NO3)2",
            "quantity": {"value": 0.180, "unit": "mmol"},
        }
    ]
    root["source_refs"] = ["macro_action_steps[0].参数"]
    codes = _quantity_issue_codes(_quantity_audit_with_handoff(plan, handoff))
    assert "frozen_synthesis_macro_missing_material_transition" in codes


def test_precertificate_frozen_reaction_requires_state_change_on_capable_station():
    handoff = {
        "task": {"query": "co-precipitate product"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "共沉淀合成前驱体 A",
                "试剂/对象": "Ni(NO3)2、NaOH、前驱体 A",
                "参数": "反应 60 min",
            }
        ],
    }
    fake_process = {
        "status": "device_plan",
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "General_Material_Station_V1",
                "objective": "物料拿取",
                "operation_intent": "物料拿取",
                "source_macro_step": 1,
                "source_reagent_identity": "Ni(NO3)2、NaOH、前驱体 A",
                "material_event_kind": "process_same_material",
                "material_transition_ids": ["fake_reaction"],
            }
        ],
        "material_transitions": [
            {
                "transition_id": "fake_reaction",
                "transition_kind": "process_same_material",
                "source_plan_steps": [1],
                "source_macro_steps": [1],
            }
        ],
    }
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        exp_id="precert_state_change",
        research_handoff=handoff,
    )

    fake_findings = agent._plan_level_findings(state, fake_process)
    assert "frozen_material_transition_coverage_missing" in {
        finding.get("type") for finding in fake_findings
    }

    valid_reaction = copy.deepcopy(fake_process)
    valid_reaction["device_plan"][0].update(
        {
            "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
            "objective": "执行共沉淀反应",
            "operation_intent": "反应搅拌 60 min",
            "material_event_kind": "state_change",
        }
    )
    valid_reaction["material_transitions"][0][
        "transition_kind"
    ] = "state_change"
    valid_findings = agent._plan_level_findings(state, valid_reaction)
    assert "frozen_material_transition_coverage_missing" not in {
        finding.get("type") for finding in valid_findings
    }


def test_precertificate_composite_processing_requires_every_frozen_category():
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )
    cases = (
        (
            "共沉淀合成并煅烧产物",
            "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
            "calcination",
        ),
        (
            "洗涤并干燥产物",
            "Purification_Workstation_V1",
            "drying",
        ),
        (
            "水热合成并干燥产物",
            "High_Temperature_High_Pressure_Microreaction_Platform_V1",
            "drying",
        ),
    )
    for index, (operation, workstation, missing_category) in enumerate(
        cases, start=1
    ):
        handoff = {
            "task": {"query": operation},
            "macro_action_steps": [
                {
                    "步骤序号": 1,
                    "操作": operation,
                    "试剂/对象": "前驱体 A、产物 A",
                    "参数": "按冻结路线处理",
                }
            ],
        }
        plan = {
            "status": "device_plan",
            "device_plan": [
                {
                    "plan_step": 1,
                    "workstation": workstation,
                    "objective": operation,
                    "operation_intent": operation,
                    "source_macro_step": 1,
                    "source_reagent_identity": "前驱体 A、产物 A",
                    "material_event_kind": "state_change",
                    "material_transition_ids": [f"composite_{index}"],
                }
            ],
            "material_transitions": [
                {
                    "transition_id": f"composite_{index}",
                    "transition_kind": "state_change",
                    "source_plan_steps": [1],
                    "source_macro_steps": [1],
                }
            ],
        }
        state = SingleDeviceAgentState(
            exp_id=f"composite_{index}", research_handoff=handoff
        )
        coverage_findings = [
            finding
            for finding in agent._plan_level_findings(state, plan)
            if finding.get("type")
            == "frozen_material_transition_coverage_missing"
        ]
        assert len(coverage_findings) == 1
        assert missing_category in coverage_findings[0][
            "missing_processing_categories"
        ]

    # A composite reaction+calcination macro needs two independently bound
    # steps; one transition cannot stand in for the omitted furnace stage.
    complete_handoff = {
        "task": {"query": "共沉淀合成并煅烧产物"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "共沉淀合成并煅烧产物",
                "试剂/对象": "前驱体 A、产物 A",
                "参数": "按冻结路线处理",
            }
        ],
    }
    complete_plan = {
        "status": "device_plan",
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
                "objective": "共沉淀",
                "operation_intent": "反应搅拌",
                "source_macro_step": 1,
                "source_reagent_identity": "前驱体 A、产物 A",
                "material_event_kind": "state_change",
                "material_transition_ids": ["reaction_stage"],
            },
            {
                "plan_step": 2,
                "workstation": "Muffle_Furnace_V1",
                "objective": "煅烧",
                "operation_intent": "煅烧",
                "source_macro_step": 1,
                "source_reagent_identity": "前驱体 A、产物 A",
                "material_event_kind": "state_change",
                "material_transition_ids": ["calcination_stage"],
            },
        ],
        "material_transitions": [
            {
                "transition_id": "reaction_stage",
                "transition_kind": "state_change",
                "source_plan_steps": [1],
                "source_macro_steps": [1],
            },
            {
                "transition_id": "calcination_stage",
                "transition_kind": "state_change",
                "source_plan_steps": [2],
                "source_macro_steps": [1],
            },
        ],
    }
    complete_state = SingleDeviceAgentState(
        exp_id="composite_complete", research_handoff=complete_handoff
    )
    assert "frozen_material_transition_coverage_missing" not in {
        finding.get("type")
        for finding in agent._plan_level_findings(
            complete_state, complete_plan
        )
    }


def test_precertificate_reads_positive_parameter_stages_and_ignores_negation():
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )
    a02_handoff = {
        "task": {"query": "A02 XRD deposition"},
        "macro_action_steps": [
            {
                "步骤序号": 4,
                "操作": "制备未反应 NiFe LDH 的 XRD 沉积样品",
                "试剂/对象": "NiFe LDH 悬浊液、XRD 基底",
                "参数": "在 60 ℃干燥 10 min 后重复沉积，共 4 层",
            }
        ],
    }
    reaction_only = {
        "status": "device_plan",
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
                "source_macro_step": 4,
                "source_reagent_identity": "NiFe LDH 悬浊液、XRD 基底",
                "material_event_kind": "state_change",
                "material_transition_ids": ["xrd_deposition"],
            }
        ],
        "material_transitions": [
            {
                "transition_id": "xrd_deposition",
                "transition_kind": "state_change",
                "source_plan_steps": [1],
                "source_macro_steps": [4],
            }
        ],
    }
    a02_state = SingleDeviceAgentState(
        exp_id="a02_parameter_drying", research_handoff=a02_handoff
    )
    a02_findings = [
        finding
        for finding in agent._plan_level_findings(a02_state, reaction_only)
        if finding.get("type")
        == "frozen_material_transition_coverage_missing"
    ]
    assert len(a02_findings) == 1
    assert "drying" in a02_findings[0]["missing_processing_categories"]

    complete = copy.deepcopy(reaction_only)
    complete["device_plan"].append(
        {
            "plan_step": 2,
            "workstation": "Drying_Oven_V1",
            "source_macro_step": 4,
            "source_reagent_identity": "NiFe LDH 悬浊液、XRD 基底",
            "material_event_kind": "state_change",
            "material_transition_ids": ["xrd_layer_drying"],
        }
    )
    complete["material_transitions"].append(
        {
            "transition_id": "xrd_layer_drying",
            "transition_kind": "state_change",
            "source_plan_steps": [2],
            "source_macro_steps": [4],
        }
    )
    assert "frozen_material_transition_coverage_missing" not in {
        finding.get("type")
        for finding in agent._plan_level_findings(a02_state, complete)
    }

    no_calcination_handoff = {
        "task": {"query": "preserve LDH phase"},
        "macro_action_steps": [
            {
                "步骤序号": 3,
                "操作": "制备未反应 NiFe LDH 样品",
                "试剂/对象": "NiFe LDH",
                "参数": "避免高温煅烧导致 LDH 相改变",
            }
        ],
    }
    no_calcination_plan = copy.deepcopy(reaction_only)
    no_calcination_plan["device_plan"][0]["source_macro_step"] = 3
    no_calcination_plan["device_plan"][0][
        "source_reagent_identity"
    ] = "NiFe LDH"
    no_calcination_plan["material_transitions"][0][
        "source_macro_steps"
    ] = [3]
    no_calcination_state = SingleDeviceAgentState(
        exp_id="a02_negated_calcination",
        research_handoff=no_calcination_handoff,
    )
    no_calcination_findings = [
        finding
        for finding in agent._plan_level_findings(
            no_calcination_state, no_calcination_plan
        )
        if finding.get("type")
        == "frozen_material_transition_coverage_missing"
    ]
    assert not any(
        "calcination" in finding.get("missing_processing_categories", [])
        for finding in no_calcination_findings
    )


def test_precertificate_formulation_and_dispensing_cannot_be_material_pickup():
    handoff = {
        "task": {"query": "A01 stocks"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "配制并分装金属盐及碱性前驱体原液",
                "试剂/对象": "Ni 盐、Fe 盐、NaOH、水",
                "参数": "配制后分别分装；搅拌至均匀",
            }
        ],
    }
    pickup_only = {
        "status": "device_plan",
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "General_Material_Station_V1",
                "source_macro_step": 1,
                "source_reagent_identity": "Ni 盐、Fe 盐、NaOH、水",
            }
        ],
        "material_transitions": [],
    }
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )
    state = SingleDeviceAgentState(
        exp_id="a01_stock_formulation", research_handoff=handoff
    )
    pickup_findings = [
        finding
        for finding in agent._plan_level_findings(state, pickup_only)
        if finding.get("type")
        == "frozen_material_transition_coverage_missing"
    ]
    assert len(pickup_findings) == 1
    assert {"liquid_handling", "stirring"}.issubset(
        pickup_findings[0]["missing_processing_categories"]
    )

    complete = copy.deepcopy(pickup_only)
    complete["device_plan"] = [
        {
            "plan_step": 1,
            "workstation": "Liquid_Handling_Station_1ml_V2",
            "source_macro_step": 1,
            "source_reagent_identity": "Ni 盐、Fe 盐、NaOH、水",
        },
        {
            "plan_step": 2,
            "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
            "source_macro_step": 1,
            "source_reagent_identity": "Ni 盐、Fe 盐、NaOH、水",
        },
    ]
    assert "frozen_material_transition_coverage_missing" not in {
        finding.get("type")
        for finding in agent._plan_level_findings(state, complete)
    }


def test_real_040935_a01_a02_processing_categories_are_action_aware():
    """Pin the real terminal Research wording, including misleading nouns."""

    cases = [
        (
            "A01-1",
            {
                "操作": "配制并分装金属盐及碱性前驱体原液",
                "参数": (
                    "使用外部预配并已装载的 0.10 mol L−1 Ni(NO3)2、"
                    "0.10 mol L−1 Fe(NO3)3 和 0.30 mol L−1 NaOH；NiFe "
                    "共沉淀每批预留 1.5 mL Ni 原液和 0.5 mL Fe 原液；"
                    "各批反应总液量控制在约 4 mL，每种样品独立制备 3 批。"
                ),
            },
            {"liquid_handling"},
        ),
        (
            "A01-2",
            {
                "操作": "共沉淀制备 NiFe-LDH，并制备无铁 Ni(OH)2 对照",
                "参数": (
                    "将 Ni、Fe 和水混合，在约 600 rpm 搅拌下将 NaOH 分 6 "
                    "批加入；继续搅拌 60 min，再在室温约 600 rpm 搅拌熟化 "
                    "120 min。"
                ),
            },
            {"reaction", "liquid_handling", "stirring"},
        ),
        (
            "A01-3",
            {
                "操作": "通过 Fe(NO3)3 浸渍引入 Fe",
                "参数": "加入 Fe(NO3)3，再加入 NaOH；室温约 600 rpm 搅拌 120 min。",
            },
            {"reaction", "liquid_handling", "stirring"},
        ),
        (
            "A01-4",
            {
                "操作": "制备 Ni(OH)2/Fe(OH)3 物理混合对照",
                "参数": (
                    "分别制备悬浊液并加入试剂，室温约 600 rpm 搅拌并熟化 "
                    "120 min；混合后约 600 rpm 搅拌 30 min。"
                ),
            },
            {"reaction", "liquid_handling", "stirring"},
        ),
        (
            "A01-5",
            {
                "操作": "统一固液分离、洗涤和干燥",
                "参数": (
                    "各悬浊液以约 6000×g 离心 5 min，弃去上清；加入 3 mL "
                    "去离子水重分散并重复洗涤 3 次，再用乙醇洗涤；将湿固体"
                    "在 70 °C 干燥 8 h。"
                ),
            },
            {"purification", "drying", "liquid_handling", "liquid_pouring"},
        ),
        (
            "A01-6",
            {
                "操作": "制备 XRD 浆液并完成晶相观察",
                "参数": (
                    "将每种干燥粉末按约 5 mg mL−1 的固液比制成 1 mL 水性"
                    "浆液，超声分散 5 min 后取样进行 XRD。"
                ),
            },
            {"liquid_handling", "ultrasonic_dispersion"},
        ),
        (
            "A02-1",
            {
                "操作": "配制 NiFe LDH 微量前驱体反应液",
                "参数": "依次混合各原液；室温下600 rpm搅拌20 min，得到均匀前驱体液。",
            },
            {"liquid_handling", "stirring"},
        ),
        (
            "A02-2",
            {
                "操作": "微量水热合成粉末态 NiFe LDH",
                "参数": (
                    "在120 ℃保持12 h；反应结束后自然冷却并记录沉淀颜色，"
                    "**不加入**泡沫镍或其他刚性基底。"
                ),
            },
            {"reaction"},
        ),
        (
            "A02-3",
            {
                "操作": "分离并固定次数洗涤 NiFe LDH 沉淀",
                "参数": (
                    "以6000 rpm离心5 min，弃去上清液；每次加入4.0 mL水并"
                    "离心，共洗涤3次；末次去除上清液，避免高温煅烧。"
                ),
            },
            {"purification", "liquid_handling", "liquid_pouring"},
        ),
        (
            "A02-4",
            {
                "操作": "制备未反应 NiFe LDH 的 XRD 沉积样品",
                "参数": (
                    "以混合液重悬全部湿沉淀，超声分散10 min；每次取50 μL"
                    "悬浊液滴加，在60 ℃干燥10 min后重复沉积。"
                ),
            },
            {"drying", "liquid_handling", "ultrasonic_dispersion"},
        ),
        (
            "A02-5",
            {
                "操作": "采集并判读未反应样品 XRD 基线",
                "参数": "采集空白背景并扫描，记录为本stage的XRD observation。",
            },
            set(),
        ),
    ]
    for case_id, macro, expected in cases:
        frozen = _frozen_required_processing_text(macro)
        actual = set(
            _required_state_change_workstation_categories(frozen)
        ) | set(_required_non_state_workstation_categories(frozen))
        assert actual == expected, (case_id, frozen, actual)

    for negative_parameters in (
        "**不加入** B 原液",
        "避免超声分散样品",
    ):
        frozen = _frozen_required_processing_text(
            {"操作": "记录样品", "参数": negative_parameters}
        )
        assert _required_non_state_workstation_categories(frozen) == {}


def test_precertificate_rejects_fuzzy_or_nonreaction_station_codes():
    handoff = {
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "共沉淀合成前驱体 A",
                "试剂/对象": "前驱体 A",
                "参数": "反应 60 min",
            }
        ]
    }
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )
    for station in (
        "Magnetic_Stirring_Workstation_V1",
        "Spectroscopy_Magnetic_Stirrer_Workstation_V1",
    ):
        plan = {
            "device_plan": [
                {
                    "plan_step": 1,
                    "workstation": station,
                    "source_macro_step": 1,
                    "source_reagent_identity": "前驱体 A",
                    "material_event_kind": "state_change",
                    "material_transition_ids": ["reaction_A"],
                }
            ],
            "material_transitions": [
                {
                    "transition_id": "reaction_A",
                    "transition_kind": "state_change",
                    "source_plan_steps": [1],
                    "source_macro_steps": [1],
                }
            ],
        }
        state = SingleDeviceAgentState(
            exp_id=f"invalid_reaction_station_{station}",
            research_handoff=handoff,
        )
        findings = agent._plan_level_findings(state, plan)
        assert "frozen_material_transition_coverage_missing" in {
            finding.get("type") for finding in findings
        }


def test_device_plan_rewrite_prompt_requires_staged_material_and_per_consumer_allocations():
    model = FakeModel({"status": "human_review_required"})
    agent = SingleDeviceAgent(model=model, workstation_loader=FakeWorkstationLoader())
    state = SingleDeviceAgentState(
        exp_id="quantity_rewrite_prompt",
        research_handoff=_quantity_research_handoff(),
        feasibility_certificate={"sample_control_matrix": []},
    )

    agent._invoke_device_plan_repair(
        state,
        _valid_quantity_plan(),
        {"workflow_json": {}},
    )

    prompt = "\n".join(message.content for message in model.calls[0])
    assert "consumer_ids 与显式 allocation consumer 集合必须完全相等" in prompt
    assert "逐 consumer" in prompt
    assert "纯化、洗涤、干燥" in prompt
    assert "无 consumer_id 的 list" in prompt
    assert "before_step_ids" in prompt
    assert "after_step_ids" in prompt
    assert "macro_source_coverage" in prompt
    assert "material_transitions" in prompt
    assert "processing_step_refs" in prompt
    assert "derived/device_operational batch 不得 is_root_batch=true" in prompt
    assert "quantity_scope=per_batch" in prompt
    assert "per_batch_quantity" in prompt
    assert "multiplicity_ref" in prompt
    assert "quantity_scope=per_operation" in prompt
    assert "operation_repeat_ref" in prompt
    assert "operation_repeat_count" in prompt
    assert "quantity_basis" in prompt
    assert "input_allocations" in prompt
    assert "output_allocations" in prompt
    assert "consumer_id=material_transition:<id>" in prompt
    assert "source_refs=[material_transition:<id>]" in prompt
    assert "measurement_artifact" in prompt


def test_initial_device_plan_prompt_exposes_complete_quantity_lineage_schema():
    prompt = FEASIBILITY_PLAN_TASK_PROMPT
    for required_field in (
        "quantity_scope",
        "per_batch_quantity",
        "multiplicity_ref",
        "multiplicity",
        "operation_repeat_ref",
        "operation_repeat_count",
        "total_quantity",
        "quantity_basis",
        "input_allocations",
        "output_allocations",
        "material_transition:<id>",
        "measurement_artifact",
        "quantity_source_field",
        "quantity_source_context",
    ):
        assert required_field in prompt


def test_quantity_provenance_is_required_and_scientific_change_is_inferred():
    missing_provenance = _valid_quantity_plan()
    for record in (
        missing_provenance["batch_plan"][0],
        missing_provenance["material_ledger"]["entries"][0],
    ):
        record.pop("source_kind")
        record.pop("source_refs")
        record.pop("calculation")
    codes = _quantity_issue_codes(_quantity_audit(missing_provenance))
    assert {
        "missing_batch_quantity_source",
        "missing_batch_quantity_source_refs",
        "missing_batch_quantity_calculation",
        "missing_ledger_quantity_source",
        "missing_ledger_quantity_source_refs",
        "missing_ledger_quantity_calculation",
    } <= codes

    mislabeled = _valid_quantity_plan()
    mislabeled["quantity_adjustments"] = [
        {
            "adjustment_id": "hidden_scale_change",
            "kind": "device_operational",
            "before": {"value": 0.180, "unit": "mmol"},
            "after": {"value": 0.540, "unit": "mmol"},
            "source_kind": "device_operational",
            "source_refs": ["device capacity analysis"],
            "calculation": "0.180 mmol × 3 = 0.540 mmol",
        }
    ]
    normalized = _quantity_audit(mislabeled)
    adjustment = normalized["quantity_adjustments"][0]
    assert adjustment["kind"] == "amount_change"
    assert adjustment["requires_scientific_review"] is True
    assert normalized["quantity_audit"]["requires_scientific_review"] is True


class _RetryThenTimeoutModel(NativeFakeMixin):
    def __init__(self):
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return types.SimpleNamespace(
                content=json.dumps(
                    {
                        "status": "feasibility_error",
                        "feedback_type": "device_feasibility_error",
                        "feasibility": {
                            "is_feasible": False,
                            "blocking_constraints": [
                                "设备无法在同一工作站同时边滴入边搅拌"
                            ],
                        },
                    },
                    ensure_ascii=False,
                )
            )
        raise TimeoutError("Stage1 adaptation retry timed out")


def test_stage1_retry_timeout_is_device_internal_not_research_feedback():
    model = _RetryThenTimeoutModel()
    agent = SingleDeviceAgent(model=model, workstation_loader=FakeWorkstationLoader())

    state = agent.run_state(RESEARCH_HANDOFF, exp_id="stage1_retry_timeout")

    package = state.terminal_package
    assert model.calls == 2
    assert package["status"] == "failed"
    assert package["feedback_type"] == "device_internal_error"
    assert package["feedback_route"] == "device"
    assert package["failure_scope"] == "device_internal"
    assert package["feasibility_accepted"] is False
    assert "TimeoutError" in " ".join(package["error_package"]["blocking_constraints"])


def test_final_dispatch_formatter_exception_fails_closed_as_internal():
    import single_agent as single_agent_module

    plan, translation = _review_plan_and_translation()
    model = _SequencedModel([plan, translation])
    original_formatter = single_agent_module.format_dispatch_payload
    formatter_calls = []

    def fail_only_final_formatter(*args, **kwargs):
        formatter_calls.append((args, kwargs))
        if len(formatter_calls) == 2:
            raise RuntimeError("simulated final formatter failure")
        return original_formatter(*args, **kwargs)

    single_agent_module.format_dispatch_payload = fail_only_final_formatter
    try:
        agent = SingleDeviceAgent(
            model=model, workstation_loader=WorkstationLoader(use_new_format=True)
        )
        state = agent.run_state(RESEARCH_HANDOFF, exp_id="formatter_internal")
    finally:
        single_agent_module.format_dispatch_payload = original_formatter

    package = state.terminal_package
    assert len(formatter_calls) == 2
    assert package["status"] == "failed"
    assert package["feedback_type"] == "device_internal_error"
    assert package["feedback_route"] == "device"
    assert package["failure_scope"] == "device_internal"
    assert package["dispatch_validation"]["assessment_source"] == (
        "final_dispatch_formatter_internal"
    )
    assert "simulated final formatter failure" in " ".join(
        package["dispatch_validation"]["errors"]
    )


def _reagent_handoff():
    return {
        "task": {"query": "配置氯化钠样品", "current_stage": "配液"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "准备氯化钠样品",
                "试剂/对象": "NaCl",
                "参数": "按既定路线执行",
            }
        ],
    }


def _reagent_plan(identity="NaCl"):
    return {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "General_Material_Station_V1",
                "objective": f"准备 {identity} 样品",
                "operation_intent": "物料拿取",
                "containers": {"容器类型": "进样瓶", "容器编号": [1]},
                "source_macro_step": 1,
                "source_reagent_identity": identity,
            }
        ],
        "quantity_adjustments": [],
        "batch_plan": [],
        "material_ledger": {"entries": []},
        "reagent_slot_plan": [],
        "container_plan": [],
        "offline_handoffs": [],
    }


def test_a01_concentration_identity_and_late_macro_preposition_are_valid():
    handoff = {
        "task": {"query": "A01-shaped route", "current_stage": "synthesis"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "配置前驱体",
                "试剂/对象": (
                    "外部预配并已装载的 0.20 mol/L Ni(NO3)2、"
                    "0.20 mol/L Fe(NO3)3、2.0 mol/L 尿素、"
                    "2.0 mol/L NH4F 原液及去离子水"
                ),
            },
            {"步骤序号": 2, "操作": "水热", "试剂/对象": "前驱体系"},
            {"步骤序号": 3, "操作": "洗涤", "试剂/对象": "水热产物"},
            {"步骤序号": 4, "操作": "后处理", "试剂/对象": "水湿态产物"},
            {
                "步骤序号": 5,
                "操作": "制备 FeOxHy",
                "试剂/对象": "0.20 mol/L Fe(NO3)3 原液、0.30 mol/L KOH 原液",
            },
        ],
    }
    source_1_identity = handoff["macro_action_steps"][0]["试剂/对象"]
    source_5_identity = handoff["macro_action_steps"][4]["试剂/对象"]
    plan = {
        "status": "device_plan",
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "General_Material_Station_V1",
                "objective": "取得 FeOxHy 空瓶并预置稳定 Fe 原液，不加入 KOH",
                "source_macro_step": 5,
                "source_reagent_identity": source_5_identity,
            },
            {
                "plan_step": 2,
                "workstation": "General_Material_Station_V1",
                "objective": "配置前驱体",
                "source_macro_step": 1,
                "source_reagent_identity": source_1_identity,
            },
            {
                "plan_step": 3,
                "workstation": "General_Material_Station_V1",
                "objective": "水热",
                "source_macro_step": 2,
                "source_reagent_identity": "前驱体系",
            },
            {
                "plan_step": 4,
                "workstation": "General_Material_Station_V1",
                "objective": "洗涤",
                "source_macro_step": 3,
                "source_reagent_identity": "水热产物",
            },
            {
                "plan_step": 5,
                "workstation": "General_Material_Station_V1",
                "objective": "后处理",
                "source_macro_step": 4,
                "source_reagent_identity": "水湿态产物",
            },
            {
                "plan_step": 6,
                "workstation": "General_Material_Station_V1",
                "objective": "加入 KOH 并完成 FeOxHy",
                "source_macro_step": 5,
                "source_reagent_identity": source_5_identity,
            },
        ],
    }

    errors = SingleDeviceAgent._device_plan_research_alignment_errors(
        handoff, plan
    )

    assert errors == []
    tokens = SingleDeviceAgent._reagent_identity_tokens(source_1_identity)
    assert any("nino32" in token for token in tokens)
    assert not any("lnino32" in token for token in tokens)


def test_macro_completion_order_still_rejects_wholesale_two_before_one():
    handoff = {
        "macro_action_steps": [
            {"步骤序号": 1, "操作": "first", "试剂/对象": "NaCl"},
            {"步骤序号": 2, "操作": "second", "试剂/对象": "KOH"},
        ]
    }
    reversed_plan = {
        "status": "device_plan",
        "device_plan": [
            {
                "plan_step": 1,
                "source_macro_step": 2,
                "source_reagent_identity": "KOH",
            },
            {
                "plan_step": 2,
                "source_macro_step": 1,
                "source_reagent_identity": "NaCl",
            },
        ],
    }

    errors = SingleDeviceAgent._device_plan_research_alignment_errors(
        handoff, reversed_plan
    )

    assert any("执行顺序" in error for error in errors)


def _b01_multisource_handoff():
    return {
        "task": {"query": "B01-shaped route", "current_stage": "synthesis"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "建立同源对照批次",
                "试剂/对象": (
                    "Ni(NO3)2 原液、铵钼酸盐原液、NaOH 原液、"
                    "HNO3 原液、去离子水、无水乙醇"
                ),
            },
            {
                "步骤序号": 2,
                "操作": "碱性共沉淀制备 NiMo",
                "试剂/对象": "Ni(NO3)2、铵钼酸盐、去离子水、NaOH",
            },
            {
                "步骤序号": 3,
                "操作": "制备 Ni 对照",
                "试剂/对象": "Ni(NO3)2、去离子水、NaOH",
            },
            {
                "步骤序号": 4,
                "操作": "制备 Mo 对照",
                "试剂/对象": "铵钼酸盐、去离子水、HNO3",
            },
            {
                "步骤序号": 5,
                "操作": "分离洗涤三类沉淀",
                "试剂/对象": (
                    "NM-raw、Ni-Ctrl、Mo-Ctrl 悬浊液、去离子水、无水乙醇"
                ),
            },
            {
                "步骤序号": 6,
                "操作": "完成 XRD 观察",
                "试剂/对象": (
                    "干燥后的 NiMo、Ni 和 Mo 对照粉末、无水乙醇、XRD 基底片"
                ),
            },
        ],
    }


def _b01_multisource_plan():
    handoff = _b01_multisource_handoff()
    union_1_to_4 = "、".join(
        step["试剂/对象"] for step in handoff["macro_action_steps"][:4]
    )
    return {
        "status": "device_plan",
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "General_Material_Station_V1",
                "source_macro_step": [1, 2, 3, 4],
                "source_reagent_identity": union_1_to_4,
            },
            {
                "plan_step": 2,
                "workstation": "General_Material_Station_V1",
                "source_macro_step": [2, 3],
                "source_reagent_identity": "、".join(
                    step["试剂/对象"]
                    for step in handoff["macro_action_steps"][1:3]
                ),
                "sample_material_state": "NiMo 和 Ni 碱性共沉淀体系",
            },
            {
                "plan_step": 3,
                "workstation": "General_Material_Station_V1",
                "source_macro_step": [2, 3, 4],
                "source_reagent_identity": "、".join(
                    step["试剂/对象"]
                    for step in handoff["macro_action_steps"][1:4]
                ),
                "sample_material_state": (
                    "70 ℃反应后的 NiMo、Ni 和 Mo 悬浊液、酸化钼酸盐体系"
                ),
            },
            {
                "plan_step": 4,
                "workstation": "General_Material_Station_V1",
                "source_macro_step": 5,
                "source_reagent_identity": handoff["macro_action_steps"][4][
                    "试剂/对象"
                ],
            },
            {
                "plan_step": 5,
                "workstation": "General_Material_Station_V1",
                "source_macro_step": 5,
                "source_reagent_identity": handoff["macro_action_steps"][4][
                    "试剂/对象"
                ],
                "sample_material_state": "最终洗涤离心后的沉淀和乙醇上清",
            },
            {
                "plan_step": 6,
                "workstation": "General_Material_Station_V1",
                "source_macro_step": 6,
                "source_reagent_identity": handoff["macro_action_steps"][5][
                    "试剂/对象"
                ],
            },
        ],
        "offline_handoffs": [],
    }


def test_b01_multisource_trace_normalizes_without_cloning_or_state_drift():
    handoff = _b01_multisource_handoff()
    raw_plan = _b01_multisource_plan()
    state = SingleDeviceAgentState(exp_id="b01_trace", research_handoff=handoff)
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )

    normalized = agent._normalize_plan_handoff_steps(state, raw_plan)

    assert len(normalized["device_plan"]) == len(raw_plan["device_plan"])
    first = normalized["device_plan"][0]
    assert first["source_macro_step"] == 1
    assert first["source_macro_steps"] == [1, 2, 3, 4]
    assert SingleDeviceAgent._device_plan_research_alignment_errors(
        handoff, normalized, reference_plan=normalized
    ) == []

    workflow_steps = [
        {
            "step_number": 1,
            "workstation": "General_Material_Station_V1",
            "source_macro_step": 1,
        }
    ]
    agent._inherit_workflow_source_traces(
        workflow_steps, [first]
    )
    assert workflow_steps[0]["source_plan_step"] == 1
    assert workflow_steps[0]["source_macro_steps"] == [1, 2, 3, 4]


def test_b01_state_changes_are_allowed_but_new_bal_sample_is_rejected():
    handoff = _b01_multisource_handoff()
    plan = _b01_multisource_plan()
    plan["device_plan"].append(
        {
            "plan_step": 7,
            "workstation": "General_Material_Station_V1",
            "source_macro_step": 5,
            "source_reagent_identity": "BAL-Ni-01干燥固体",
        }
    )

    errors = SingleDeviceAgent._device_plan_research_alignment_errors(
        handoff, plan
    )
    error_text = " ".join(errors)

    assert "balni01" in error_text
    assert "ni碱性" not in error_text
    assert "酸化钼酸盐体系" not in error_text
    assert "70反应后的nimo" not in error_text
    assert "洗涤离心后的" not in error_text


def _bad_unknown_parameter_translation():
    return {
        "workflow_txt": "第1步 General_Material_Station_V1：物料拿取",
        "workflow_json": {
            "steps": [
                {
                    "step_number": 1,
                    "workstation": "General_Material_Station_V1",
                    "operation": "物料拿取",
                    "parameters": {
                        "容器类型": "进样瓶",
                        "容器数量": 1,
                        "容器编号": [1],
                        "不存在的危险参数": 999,
                    },
                }
            ]
        },
    }


def test_stage1_reagent_drift_is_rejected_before_certificate():
    drifted = _reagent_plan("KCl")
    model = _SequencedModel([drifted, drifted])
    agent = SingleDeviceAgent(model=model, workstation_loader=FakeWorkstationLoader())

    state = agent.run_state(_reagent_handoff(), exp_id="stage1_reagent_drift")

    package = state.terminal_package
    assert len(model.calls) == 4  # initial plan + at most three local candidates
    assert package["status"] == "manual_required"
    assert package["feedback_route"] == "human"
    assert package["failure_scope"] == "device_plan"
    assert package["feasibility_accepted"] is False
    assert package["feasibility_certificate"] == {}
    assert package["error_package"]["type"] == "stage1_plan_repair_exhausted"
    assert "试剂/对象身份" in " ".join(
        package["error_package"]["blocking_constraints"]
    )


def test_stage1_plan_repair_uses_all_latest_findings_and_stays_device_local():
    handoff = {
        "task": {"query": "eight-step audit", "current_stage": "synthesis"},
        "macro_action_steps": [
            {
                "步骤序号": index,
                "操作": f"macro operation {index}",
                "试剂/对象": "NaCl" if index == 1 else "",
            }
            for index in range(1, 9)
        ],
    }
    incomplete = _reagent_plan("NaCl")
    model = _SequencedModel([incomplete])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=FakeWorkstationLoader()
    )

    state = agent.run_state(handoff, exp_id="stage1_all_latest_findings")

    package = state.terminal_package
    assert len(model.calls) == 4
    assert all(
        "device_plan 缺少 Research macro step 8 的覆盖" in prompt
        for prompt in model.prompts[1:]
    )
    assert package["status"] == "manual_required"
    assert package["feedback_type"] == "human_review_required"
    assert package["feedback_route"] == "human"
    assert package["failure_scope"] == "device_plan"
    assert package["error_package"]["candidate_limit"] == 3
    assert package["error_package"]["type"] == "stage1_plan_repair_exhausted"
    assert package["error_package"]["type"] != "research_replan_required"


def test_stage1_plan_repair_accepts_clean_third_and_final_candidate():
    drifted = _reagent_plan("KCl")
    clean = _reagent_plan("NaCl")
    model = _SequencedModel([drifted, drifted, clean])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=FakeWorkstationLoader()
    )
    state = SingleDeviceAgentState(
        exp_id="stage1_final_candidate",
        research_handoff=_reagent_handoff(),
        workstation_descriptions=FakeWorkstationLoader().format_for_prompt(),
    )

    repaired = agent._repair_plan_level_findings(state, drifted)

    assert len(model.calls) == 3
    assert repaired["status"] == "device_plan"
    assert agent._plan_level_findings(state, repaired) == []


def _high_temperature_hydrogen_handoff():
    return {
        "task": {"query": "B01-shaped reduction gradient", "current_stage": "synthesis"},
        "macro_action_steps": [
            {
                "步骤序号": 4,
                "操作": "构建匹配的受控还原处理梯度",
                "试剂/对象": "母体粉末，5 vol% H2/Ar混合气及Ar",
                "参数": (
                    "先以Ar吹扫30 min，再在100 mL/min的5 vol% H2/Ar中"
                    "以2 ℃/min分别升至250、350或450 ℃，保温120 min。"
                ),
            }
        ],
    }


def _high_temperature_hydrogen_offline_plan():
    reagent = "母体粉末，5 vol% H2/Ar混合气及Ar"
    return {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "General_Material_Station_V1",
                "objective": "预置密封回收容器",
                "operation_intent": "物料拿取",
                "source_macro_step": 4,
                "source_reagent_identity": reagent,
            }
        ],
        "offline_handoffs": [
            {
                "name": "离线受控气氛热处理",
                "source_macro_step": 4,
                "source_reagent_identity": reagent,
                "instructions": [
                    "先Ar吹扫，在5 vol% H2/Ar中分别升至250、350或450 ℃还原"
                ],
            }
        ],
    }


def test_stage1_persistent_core_offline_is_promoted_by_complete_truth_proof():
    """Three self-declared feasible candidates cannot preserve B01's core
    450 C H2/Ar reduction as an offline handoff.  The deterministic complete
    truth audit synthesizes the strict Research route before a certificate."""
    plan = _high_temperature_hydrogen_offline_plan()
    model = _SequencedModel([plan])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=WorkstationLoader(use_new_format=True)
    )

    state = agent.run_state(
        _high_temperature_hydrogen_handoff(), exp_id="stage1_joint_gap"
    )

    package = state.terminal_package
    # The joint-capability gap is proved from the frozen Research step before
    # the mapping LLM can omit, offline, or mis-map it.
    assert len(model.calls) == 0
    assert package["status"] == "feasibility_error"
    assert package["feedback_type"] == "research_replan_required"
    assert package["feedback_route"] == "research"
    assert package["failure_scope"] == "route_feasibility"
    assert package["feasibility_accepted"] is False
    assert package["feasibility_certificate"] == {}
    blocking = " ".join(package["error_package"]["blocking_constraints"])
    assert "450" in blocking
    assert "High_Temperature_High_Pressure_Microreaction_Platform_V1" in blocking
    assert "200.0" in blocking
    proof = package["feasibility_assessment"]["feasibility"][
        "deterministic_route_gap_proof"
    ][0]
    assert proof["truth_workstation_count"] == 45
    assert proof["high_temperature_without_hydrogen_reduction"][0][
        "workstation"
    ] == "Muffle_Furnace_V1"
    assert package["error_package"]["assessment_source"] == (
        "deterministic_complete_workstation_joint_capability_audit"
    )


def test_stage1_matching_repair_hard_gap_promotes_and_filters_local_errors():
    plan = _high_temperature_hydrogen_offline_plan()
    repair_hard = {
        "status": "feasibility_error",
        "feedback_type": "device_feasibility_error",
        "feasibility": {
            "is_feasible": False,
            "blocking_constraints": [
                "必要450 ℃ 5 vol% H2/Ar还原，完整真源不存在兼容工作站",
                "进样瓶容量不足，需要分瓶",
                "累计取液量需重新配平",
            ],
        },
    }
    model = _SequencedModel([plan, repair_hard])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=WorkstationLoader(use_new_format=True)
    )

    package = agent.run_state(
        _high_temperature_hydrogen_handoff(), exp_id="stage1_candidate_hard"
    ).terminal_package

    assert len(model.calls) == 0
    assert package["feedback_route"] == "research"
    blocking = " ".join(package["error_package"]["blocking_constraints"])
    assert "450" in blocking
    assert "容量不足" not in blocking
    assert "取液量" not in blocking


def test_stage1_incomplete_truth_keeps_core_offline_in_human_device_scope():
    plan = _high_temperature_hydrogen_offline_plan()
    model = _SequencedModel([plan])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=FakeWorkstationLoader()
    )

    package = agent.run_state(
        _high_temperature_hydrogen_handoff(), exp_id="stage1_incomplete_truth"
    ).terminal_package

    assert len(model.calls) == 4
    assert package["status"] == "manual_required"
    assert package["feedback_route"] == "human"
    assert package["failure_scope"] == "device_plan"
    assert package["error_package"]["type"] == "stage1_plan_repair_exhausted"
    assert package["error_package"]["type"] != "research_replan_required"


def test_observation_only_xrd_handoff_is_not_core_chemistry():
    handoff = {
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "采集并比较 XRD 图谱",
                "试剂/对象": "12个催化剂 XRD 样品",
                "参数": "返回可判读图谱和物相表",
            }
        ]
    }
    plan = {
        "status": "device_plan",
        "device_plan": [],
        "offline_handoffs": [
            {
                "name": "XRD observation 数据回传",
                "source_macro_step": 1,
                "required_return_data": ["原始 XRD 图谱", "物相对照表"],
            }
        ],
    }
    state = SingleDeviceAgentState(exp_id="xrd_only", research_handoff=handoff)
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=WorkstationLoader(use_new_format=True)
    )

    findings = agent._audit_core_chemistry_offline_handoffs(state, plan)

    assert findings == []
    assert agent._verified_stage1_core_route_gap_result(state, plan) is None


def test_real_b01_xrd_matrix_wording_is_observation_not_reduction():
    handoff = {
        "macro_action_steps": [
            {
                "步骤序号": 6,
                "操作": "采集并比较全系列 XRD 图谱",
                "试剂/对象": "12个催化剂XRD样品及空白基底",
                "参数": (
                    "记录主衍射峰并形成组成×还原温度矩阵；样品来自"
                    "5 vol% H2/Ar还原系列，但本步骤只采集图谱。"
                ),
            }
        ]
    }
    plan = {
        "status": "device_plan",
        "device_plan": [],
        "offline_handoffs": [
            {
                "name": "XRD observation 数据回传",
                "source_macro_steps": [6],
                "required_return_data": ["原始 XRD 图谱", "物相对照表"],
            },
            {
                "name": "XRD observation 数据回传（聚合来源回显）",
                "source_macro_steps": [1, 2, 3, 4, 5, 6],
                "required_return_data": ["原始 XRD 图谱"],
            },
        ],
    }
    state = SingleDeviceAgentState(exp_id="real_b01_xrd", research_handoff=handoff)
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=WorkstationLoader(use_new_format=True)
    )

    assert agent._is_observation_only_macro(handoff["macro_action_steps"][0])
    assert agent._audit_core_chemistry_offline_handoffs(state, plan) == []
    assert agent._verified_stage1_core_route_gap_result(state, plan) is None


def test_post_hydrothermal_container_transfer_is_not_offline_chemistry():
    handoff = {
        "macro_action_steps": [
            {
                "步骤序号": 2,
                "操作": "120 ℃水热反应",
                "试剂/对象": "NiFe水热反应悬浊液",
                "参数": "120 ℃保温12 h后冷却至25 ℃",
            }
        ]
    }
    plan = {
        "status": "device_plan",
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "High_Temperature_High_Pressure_Microreaction_Platform_V1",
                "objective": "完成120 ℃水热反应并冷却",
                "source_macro_step": 2,
            }
        ],
        "offline_handoffs": [
            {
                "name": "10ml耐压反应管到纯化进样瓶的无容器路径转换",
                "handoff_type": "no_supported_container_path",
                "source_macro_step": 2,
                "sample": "已完成水热反应并冷却的反应悬浊液",
                "source_container": "10ml耐压反应管",
                "destination_container": "进样瓶",
                "required_state_on_return": "全部转移且不合并",
            }
        ],
    }
    state = SingleDeviceAgentState(exp_id="post_hydrothermal", research_handoff=handoff)
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=WorkstationLoader(use_new_format=True)
    )

    assert agent._audit_core_chemistry_offline_handoffs(state, plan) == []


def test_frozen_hydrogen_gap_cannot_be_evaded_by_omission_or_muffle_mapping():
    handoff = _high_temperature_hydrogen_handoff()
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=WorkstationLoader(use_new_format=True)
    )
    state = SingleDeviceAgentState(exp_id="b01_gap_bypass", research_handoff=handoff)
    omitted = {"status": "device_plan", "device_plan": [], "offline_handoffs": []}
    mis_mapped = {
        "status": "device_plan",
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "Muffle_Furnace_V1",
                "objective": "450 ℃、5 vol% H2/Ar还原",
                "source_macro_step": 4,
                "source_reagent_identity": "母体粉末，5 vol% H2/Ar混合气及Ar",
            }
        ],
        "offline_handoffs": [],
    }

    for candidate in (omitted, mis_mapped):
        result = agent._verified_stage1_core_route_gap_result(state, candidate)
        assert result is not None
        assert result["status"] == "feasibility_error"
        accepted = agent._accept_feasibility_plan(state, candidate)
        assert accepted["status"] == "feasibility_error"
        assert state.feasibility_accepted is False
        assert state.feasibility_certificate == {}


def test_deterministic_proof_marker_must_bind_to_frozen_research_plan():
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=WorkstationLoader(use_new_format=True)
    )
    true_state = SingleDeviceAgentState(
        exp_id="proof_source", research_handoff=_high_temperature_hydrogen_handoff()
    )
    true_result = agent._verified_stage1_core_route_gap_result(true_state, {})
    assert true_result is not None

    xrd_handoff = {
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "采集 XRD 图谱",
                "试剂/对象": "催化剂样品",
                "参数": "形成样品矩阵",
            }
        ]
    }
    xrd_state = SingleDeviceAgentState(
        exp_id="forged_proof", research_handoff=xrd_handoff
    )
    forged = json.loads(json.dumps(true_result, ensure_ascii=False))
    forged["feasibility"]["blocking_constraints"].insert(
        0, "必要 XRD 工作站不存在且没有替代"
    )

    package = agent._normalize_terminal_package(xrd_state, forged)

    assert package["status"] == "manual_required"
    assert package["feedback_route"] == "human"
    assert package["failure_scope"] == "device_plan"
    assert package["feasibility_accepted"] is False


def test_canonical_route_gap_strips_device_local_blockers_before_research():
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=WorkstationLoader(use_new_format=True)
    )
    state = SingleDeviceAgentState(
        exp_id="route_gap_filter", research_handoff=_high_temperature_hydrogen_handoff()
    )
    result = agent._verified_stage1_core_route_gap_result(state, {})
    assert result is not None
    result["feasibility"]["blocking_constraints"].extend(
        ["进样瓶容量不足，需要分瓶", "累计取液量需要重新配平"]
    )

    package = agent._normalize_terminal_package(state, result)

    assert package["feedback_route"] == "research"
    blocking = " ".join(package["error_package"]["blocking_constraints"])
    assert "450" in blocking
    assert "容量不足" not in blocking
    assert "取液量" not in blocking
    assessment = json.dumps(package["feasibility_assessment"], ensure_ascii=False)
    assert "容量不足" not in assessment
    assert "取液量" not in assessment


def test_manual_required_cannot_overwrite_accepted_certificate_or_route_research():
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=WorkstationLoader(use_new_format=True)
    )
    certificate = {
        "accepted": True,
        "certificate_id": "feasibility_test",
    }
    state = SingleDeviceAgentState(
        exp_id="manual_route_guard",
        research_handoff=_reagent_handoff(),
        feasibility_accepted=True,
        feasibility_certificate=certificate,
    )
    malicious = {
        "status": "manual_required",
        "feedback_type": "research_replan_required",
        "feedback_route": "research",
        "failure_scope": "route_feasibility",
        "feasibility_accepted": False,
        "feasibility_certificate": {},
        "error_package": {
            "type": "workflow_translation_failed",
            "feedback_route": "research",
            "failure_scope": "route_feasibility",
        },
    }

    package = agent._normalize_terminal_package(state, malicious)

    assert package["status"] == "manual_required"
    assert package["feedback_type"] == "human_review_required"
    assert package["feedback_route"] == "human"
    assert package["failure_scope"] == "device_workflow"
    assert package["feasibility_accepted"] is True
    assert package["feasibility_certificate"] == certificate
    assert package["error_package"]["feedback_route"] == "human"
    assert package["error_package"]["failure_scope"] == "device_workflow"


def test_post_certificate_core_offline_never_promotes_to_research():
    plan = _high_temperature_hydrogen_offline_plan()
    state = SingleDeviceAgentState(
        exp_id="post_certificate_guard",
        research_handoff=_high_temperature_hydrogen_handoff(),
        feasibility_accepted=True,
        feasibility_certificate={"accepted": True},
    )
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=WorkstationLoader(use_new_format=True)
    )

    assert agent._verified_stage1_core_route_gap_result(state, plan) is None


def test_plan_level_rewrite_reagent_drift_is_rejected_after_certificate():
    plan = _reagent_plan("NaCl")
    bad = _bad_unknown_parameter_translation()
    drifted_rewrite = _reagent_plan("KCl")
    drifted_rewrite.update(
        {
            "plan_changes": [],
            "change_rationale": "尝试修改设备计划",
            "expected_resolved_errors": ["unknown_parameter"],
            "route_changed": False,
        }
    )
    model = _SequencedModel([plan] + [bad] * 9 + [drifted_rewrite])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=WorkstationLoader(use_new_format=True)
    )

    state = agent.run_state(_reagent_handoff(), exp_id="rewrite_reagent_drift")

    package = state.terminal_package
    assert len(model.calls) == 11
    assert package["status"] == "manual_required"
    assert package["feasibility_accepted"] is True
    assert package["feasibility_certificate"]["accepted"] is True
    assert package["failure_scope"] == "device_plan"
    assert package["error_package"]["type"] == "device_plan_rewrite_rejected"
    assert "试剂" in json.dumps(package["error_package"], ensure_ascii=False)


def _complete_plan_rewrite_contract(plan):
    completed = json.loads(json.dumps(plan, ensure_ascii=False))
    completed.update(
        {
            "plan_changes": [],
            "change_rationale": "仅修复 Device 执行层字段",
            "expected_resolved_errors": ["device_local_quantity"],
            "route_changed": False,
        }
    )
    return completed


def _semantic_reaction_handoff_and_plan():
    handoff = {
        "task": {"query": "co-precipitate precursor"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "共沉淀合成前驱体 A",
                "试剂/对象": "Ni 盐、NaOH、前驱体 A",
                "参数": "室温反应并搅拌 60 min",
            }
        ],
    }
    plan = {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
                "objective": "完成共沉淀反应",
                "operation_intent": "磁力搅拌",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_reagent_identity": "Ni 盐、NaOH、前驱体 A",
                "material_event_kind": "state_change",
                "material_transition_ids": ["reaction_A"],
            }
        ],
        "material_transitions": [
            {
                "transition_id": "reaction_A",
                "transition_kind": "state_change",
                "source_plan_steps": [1],
                "source_macro_steps": [1],
            }
        ],
        "quantity_adjustments": [],
        "batch_plan": [],
        "material_ledger": {"entries": []},
        "reagent_slot_plan": [],
        "container_plan": [],
        "offline_handoffs": [],
    }
    return handoff, plan


def test_post_certificate_rewrite_reruns_full_semantic_plan_gate():
    handoff, previous = _semantic_reaction_handoff_and_plan()
    repaired = _complete_plan_rewrite_contract(previous)
    repaired["device_plan"][0]["workstation"] = "General_Material_Station_V1"
    repaired["plan_changes"] = [
        {
            "change_type": "workstation_substitution",
            "field": "workstation",
            "before": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
            "after": "General_Material_Station_V1",
            "reason": "malicious post-certificate capability drift fixture",
            "preserved_invariants": [
                "route",
                "reagent_identity",
                "macro_source_coverage",
            ],
        }
    ]
    state = SingleDeviceAgentState(
        exp_id="post_cert_semantic_gate",
        research_handoff=handoff,
        feasibility_accepted=True,
        feasibility_certificate={
            "accepted": True,
            "research_plan_signature": "",
            "device_sample_ids": [],
            "sample_control_matrix": [],
        },
    )
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )

    _, errors = agent._validate_repaired_device_plan(
        state, previous, repaired
    )

    assert any(
        "frozen_material_transition_coverage_missing" in error
        for error in errors
    )


def test_manual_override_reruns_full_semantic_plan_gate():
    handoff, accepted_plan = _semantic_reaction_handoff_and_plan()
    agent = SingleDeviceAgent(
        model=RaisingModel(),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )
    seed_state = SingleDeviceAgentState(
        exp_id="manual_semantic_gate_seed", research_handoff=handoff
    )
    accepted_plan = agent._normalize_plan_handoff_steps(
        seed_state, accepted_plan
    )
    certificate = agent._build_feasibility_certificate(
        seed_state, accepted_plan
    )
    repair_request = {
        "feasibility_certificate": copy.deepcopy(certificate),
        "last_device_plan": copy.deepcopy(accepted_plan),
        "frozen_route_signature": certificate["route_signature"],
        "frozen_sample_matrix_signature": certificate[
            "sample_matrix_signature"
        ],
        "device_snapshot_signature": certificate[
            "device_snapshot_signature"
        ],
        "frozen_sample_matrix": copy.deepcopy(
            certificate["sample_control_matrix"]
        ),
    }
    drifted_steps = copy.deepcopy(accepted_plan["device_plan"])
    drifted_steps[0]["workstation"] = "General_Material_Station_V1"
    override = {
        "device_plan": drifted_steps,
        "route_signature": certificate["route_signature"],
        "sample_matrix_signature": certificate["sample_matrix_signature"],
        "device_snapshot_signature": certificate[
            "device_snapshot_signature"
        ],
        "declarations": {
            "route_changed": False,
            "sample_matrix_changed": False,
            "reagent_identity_or_order_changed": False,
            "observation_points_changed": False,
        },
    }
    resume_state = SingleDeviceAgentState(
        exp_id="manual_semantic_gate", research_handoff=handoff
    )

    _, errors = agent._prepare_device_plan_override(
        resume_state, override, repair_request
    )

    assert any(
        "frozen_material_transition_coverage_missing" in error
        for error in errors
    )


def test_plan_level_rewrite_mechanically_inherits_only_blank_reagent_identity():
    previous = _reagent_plan("NaCl")
    omitted = _complete_plan_rewrite_contract(_reagent_plan(""))
    state = SingleDeviceAgentState(
        exp_id="rewrite_identity_inheritance",
        research_handoff=_reagent_handoff(),
        feasibility_accepted=True,
        feasibility_certificate={
            "accepted": True,
            "research_plan_signature": "",
            "device_sample_ids": [],
            "sample_control_matrix": [],
        },
    )
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )

    repaired, errors = agent._validate_repaired_device_plan(
        state, previous, omitted
    )

    assert errors == []
    assert repaired["device_plan"][0]["source_reagent_identity"] == "NaCl"
    assert any("mechanically inherited" in message for message in state.logs)

    explicit_drift = _complete_plan_rewrite_contract(_reagent_plan("KCl"))
    rejected, drift_errors = agent._validate_repaired_device_plan(
        state, previous, explicit_drift
    )
    assert rejected["device_plan"][0]["source_reagent_identity"] == "KCl"
    assert any("试剂" in message for message in drift_errors)


def test_plan_level_rewrite_inherits_multisource_certified_identity_without_guessing():
    handoff = {
        "macro_action_steps": [
            {"步骤序号": 1, "操作": "加入 A", "试剂/对象": "试剂 A"},
            {"步骤序号": 2, "操作": "加入 B", "试剂/对象": "试剂 B"},
        ]
    }
    previous = {
        "device_plan": [
            {
                "plan_step": 1,
                "source_macro_step": 1,
                "source_macro_steps": [1, 2],
                "source_reagent_identity": "试剂 A、试剂 B",
            }
        ]
    }
    repaired = {
        "device_plan": [
            {
                "plan_step": 1,
                "source_macro_step": 1,
                "source_macro_steps": [1, 2],
                "source_reagent_identity": "   ",
            }
        ]
    }

    count = SingleDeviceAgent._inherit_missing_repaired_reagent_identities(
        handoff, previous, repaired
    )

    assert count == 1
    assert repaired["device_plan"][0]["source_reagent_identity"] == "试剂 A、试剂 B"


def _source_binding_rewrite_fixture():
    handoff = {
        "macro_action_steps": [
            {"步骤序号": 1, "操作": "取得 Ni", "试剂/对象": "Ni"},
            {"步骤序号": 2, "操作": "取得 Co", "试剂/对象": "Co"},
        ]
    }
    previous = {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "station_A",
                "objective": "取得 Ni",
                "operation_intent": "取料",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_reagent_identity": "Ni",
            },
            {
                "plan_step": 2,
                "workstation": "station_B",
                "objective": "处理 Ni",
                "operation_intent": "处理",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_reagent_identity": "Ni",
            },
            {
                "plan_step": 3,
                "workstation": "station_C",
                "objective": "取得 Co",
                "operation_intent": "取料",
                "source_macro_step": 2,
                "source_macro_steps": [2],
                "source_reagent_identity": "Co",
            },
        ],
        "quantity_adjustments": [],
        "batch_plan": [],
        "material_ledger": {"entries": []},
        "reagent_slot_plan": [],
        "container_plan": [],
        "offline_handoffs": [],
    }
    state = SingleDeviceAgentState(
        exp_id="source_binding_rewrite",
        research_handoff=handoff,
        feasibility_accepted=True,
        feasibility_certificate={
            "accepted": True,
            "research_plan_signature": "",
            "device_sample_ids": [],
            "sample_control_matrix": [],
        },
    )
    return handoff, previous, state


def test_plan_rewrite_cannot_rebind_persistent_step_even_with_punctuation_or_global_evidence():
    _, previous, state = _source_binding_rewrite_fixture()
    repaired = _complete_plan_rewrite_contract(previous)
    repaired["device_plan"][1]["objective"] += "。"
    repaired["device_plan"][1]["source_macro_step"] = 2
    repaired["device_plan"][1]["source_macro_steps"] = [2]
    repaired["device_plan"][1]["source_reagent_identity"] = ""
    repaired["plan_changes"] = [
        {
            "change_type": "operation_split",
            "before_step_ids": [1],
            "after_step_ids": [1],
            "before": {"plan_step": 1},
            "after": {"plan_step": 1},
            "reason": "unrelated global declaration",
            "preserved_invariants": [
                "route",
                "reagent_identity",
                "macro_source_coverage",
            ],
        }
    ]
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )

    validated, errors = agent._validate_repaired_device_plan(
        state, previous, repaired
    )

    assert any("既存 plan_step" in message for message in errors)
    # Binding rejection runs before inheritance, so blank identity cannot
    # silently become Co and authorize the drift.
    assert validated["device_plan"][1]["source_reagent_identity"] == ""


def test_plan_rewrite_accepts_step_bound_merge_with_equal_source_union():
    handoff, previous, state = _source_binding_rewrite_fixture()
    previous["device_plan"] = [previous["device_plan"][0], previous["device_plan"][2]]
    merged = _complete_plan_rewrite_contract(previous)
    merged["device_plan"] = [
        {
            "plan_step": 10,
            "workstation": "station_merge",
            "objective": "取得 Ni 和 Co",
            "operation_intent": "合并设备操作",
            "source_macro_step": 1,
            "source_macro_steps": [1, 2],
            "source_reagent_identity": "Ni、Co",
        }
    ]
    merged["plan_changes"] = [
        {
            "change_type": "operation_merge",
            "before_step_ids": [1, 3],
            "after_step_ids": [10],
            "before": {"steps": [1, 3], "source_macro_steps": [1, 2]},
            "after": {"steps": [10], "source_macro_steps": [1, 2]},
            "reason": "同一物理工作站合并两个设备操作但不改化学来源",
            "preserved_invariants": [
                "route",
                "reagent_identity",
                "macro_source_coverage",
            ],
        }
    ]
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )

    validated, errors = agent._validate_repaired_device_plan(
        state, previous, merged
    )

    assert errors == [], errors
    assert validated["device_plan"][0]["source_macro_steps"] == [1, 2]
    assert SingleDeviceAgent._device_plan_research_alignment_errors(
        handoff, validated, reference_plan=previous
    ) == []


def test_plan_rewrite_rejects_step_bound_merge_when_source_union_changes():
    _, previous, state = _source_binding_rewrite_fixture()
    previous["device_plan"] = [previous["device_plan"][0], previous["device_plan"][2]]
    bad_merge = _complete_plan_rewrite_contract(previous)
    bad_merge["device_plan"] = [
        {
            "plan_step": 10,
            "workstation": "station_merge",
            "objective": "只取得 Co",
            "operation_intent": "合并设备操作",
            "source_macro_step": 2,
            "source_macro_steps": [2],
            "source_reagent_identity": "Co",
        }
    ]
    bad_merge["plan_changes"] = [
        {
            "change_type": "operation_merge",
            "before_step_ids": [1, 3],
            "after_step_ids": [10],
            "before": {"steps": [1, 3]},
            "after": {"steps": [10]},
            "reason": "invalid union fixture",
            "preserved_invariants": [
                "route",
                "reagent_identity",
                "macro_source_coverage",
            ],
        }
    ]
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )

    _, errors = agent._validate_repaired_device_plan(
        state, previous, bad_merge
    )

    assert any("source-set union" in message for message in errors)
    assert any("macro step 1" in message for message in errors)


def test_plan_rewrite_rejects_many_to_many_source_swap_even_when_union_matches():
    _, previous, state = _source_binding_rewrite_fixture()
    previous["device_plan"] = [previous["device_plan"][0], previous["device_plan"][2]]
    swapped = _complete_plan_rewrite_contract(previous)
    swapped["device_plan"] = [
        {
            "plan_step": 101,
            "workstation": "station_new_co",
            "objective": "取得 Co",
            "operation_intent": "取料",
            "source_macro_step": 1,
            "source_macro_steps": [1],
            "source_reagent_identity": "",
        },
        {
            "plan_step": 102,
            "workstation": "station_new_ni",
            "objective": "取得 Ni",
            "operation_intent": "取料",
            "source_macro_step": 2,
            "source_macro_steps": [2],
            "source_reagent_identity": "",
        },
    ]
    swapped["plan_changes"] = [
        {
            "change_type": "operation_decomposition",
            "before_step_ids": [1, 3],
            "after_step_ids": [101, 102],
            "before": {"steps": [1, 3], "source_macro_steps": [1, 2]},
            "after": {"steps": [101, 102], "source_macro_steps": [1, 2]},
            "reason": "malicious equal-union source swap fixture",
            "preserved_invariants": [
                "route",
                "reagent_identity",
                "macro_source_coverage",
            ],
        }
    ]
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )

    validated, errors = agent._validate_repaired_device_plan(
        state, previous, swapped
    )

    assert any("many-to-many" in message for message in errors)
    assert all(
        step["source_reagent_identity"] == ""
        for step in validated["device_plan"]
    )


def _b01_theoretical_mass_handoff():
    return {
        "task": {"query": "取 MoO3 做 XRD", "current_stage": "表征分配"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "制备并分配 MoO3",
                "试剂/对象": "Mo前驱体、MoO3",
                "参数": (
                    "取 Mo前驱体 0.010 g；XRD 需要实际 MoO3 产物 0.004 g"
                ),
            }
        ],
    }


def _b01_theoretical_mass_plan(*, decorated_unit=True):
    produced = {"value": 0.0043185, "unit": "g"}
    reserved = {"value": 0.0003185, "unit": "g"}
    if decorated_unit:
        produced["unit"] = "g nominal equivalent"
        produced["provenance"] = "theoretical_quantity"
        reserved["unit"] = "g nominal equivalent"
        reserved["provenance"] = "theoretical_quantity"
    plan = {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "General_Material_Station_V1",
                "objective": "取得 Mo前驱体",
                "operation_intent": "物料拿取",
                "containers": {"容器类型": "进样瓶", "容器编号": [1]},
                "source_macro_step": 1,
                "source_reagent_identity": "Mo前驱体",
            },
            {
                "plan_step": 2,
                "workstation": "Drying_Oven_V1",
                "objective": "将 Mo前驱体制备为 MoO3",
                "operation_intent": "干燥制备",
                "containers": {"容器类型": "进样瓶", "容器编号": [1]},
                "material_event_kind": "state_change",
                "material_transition_ids": ["mo_formation"],
                "source_macro_step": 1,
                "source_reagent_identity": "Mo前驱体、MoO3",
            },
        ],
        "quantity_adjustments": [],
        "batch_plan": [
            {
                "batch_id": "mo_precursor_batch_1",
                "material_id": "Mo前驱体",
                "research_material_identity": "Mo前驱体",
                "research_source_refs": [
                    {
                        "source_path": "macro_action_steps[0].参数",
                        "source_macro_step": 1,
                        "source_field": "参数",
                        "source_context": "取 Mo前驱体 0.010 g",
                        "material_identity": "Mo前驱体",
                        "quantity": {"value": 0.010, "unit": "g"},
                    }
                ],
                "sample_id": "Mo-Ctrl",
                "is_root_batch": True,
                "consumer_ids": ["material_transition:mo_formation"],
                "allocation": {
                    "material_transition:mo_formation": {
                        "value": 0.010,
                        "unit": "g",
                    }
                },
                "total_quantity": {"value": 0.010, "unit": "g"},
                "per_batch_quantity": {"value": 0.010, "unit": "g"},
                "multiplicity": 1,
                "source_kind": "research",
                "source_refs": ["macro_action_steps[0].参数"],
                "calculation": "0.010 g Mo precursor enters formation",
            },
            {
                "batch_id": "mo_batch_1",
                "material_id": "MoO3",
                "parent_batch_id": "mo_precursor_batch_1",
                "sample_id": "Mo-Ctrl",
                "is_root_batch": False,
                "transition_kind": "state_change",
                "source_plan_steps": [2],
                "source_macro_steps": [1],
                "consumer_ids": ["XRD"],
                "allocation": {"XRD": {"value": 0.004, "unit": "g"}},
                "total_quantity": {"value": 0.0043185, "unit": "g"},
                "source_kind": "derived",
                "source_refs": ["material_transition:mo_formation"],
                "calculation": "nominal MoO3 equivalent allocated to XRD",
            }
        ],
        "material_transitions": [
            {
                "transition_id": "mo_formation",
                "transition_kind": "state_change",
                "quantity_basis": "planning_yield_lower_bound",
                "yield_lower_bound": 0.5,
                "parent_batch_ids": ["mo_precursor_batch_1"],
                "child_batch_ids": ["mo_batch_1"],
                "source_plan_steps": [2],
                "source_macro_steps": [1],
                "input_allocations": [
                    {
                        "batch_id": "mo_precursor_batch_1",
                        "quantity": {"value": 0.010, "unit": "g"},
                    }
                ],
                "output_allocations": [
                    {
                        "batch_id": "mo_batch_1",
                        "quantity": {"value": 0.0043185, "unit": "g"},
                    }
                ],
                "before_material_state": "Mo precursor",
                "after_material_state": "MoO3 product",
                "calculation_or_basis": "theoretical oxide equivalent; yield unknown",
            }
        ],
        "material_ledger": {
            "entries": [
                {
                    "entry_id": "mo_precursor_to_formation",
                    "material_id": "Mo前驱体",
                    "batch_id": "mo_precursor_batch_1",
                    "sample_id": "Mo-Ctrl",
                    "consumer_id": "material_transition:mo_formation",
                    "produced": {"value": 0.010, "unit": "g"},
                    "consumed": {"value": 0.010, "unit": "g"},
                    "reserved": {"value": 0, "unit": "g"},
                    "balance": {"value": 0, "unit": "g"},
                    "source_kind": "research",
                    "source_refs": ["macro_action_steps[0].参数"],
                    "calculation": "0.010 g - 0.010 g = 0 g",
                },
                {
                    "entry_id": "mo_nominal_equivalent",
                    "material_id": "MoO3",
                    "batch_id": "mo_batch_1",
                    "sample_id": "Mo-Ctrl",
                    "consumer_id": "XRD",
                    "allocation_id": "mo_xrd_draw",
                    "processing_step_refs": [2],
                    "produced": produced,
                    "consumed": {"value": 0.004, "unit": "g"},
                    "reserved": reserved,
                    "balance": {"value": 0, "unit": "g"},
                    "source_kind": "derived",
                    "source_refs": ["material_transition:mo_formation"],
                    "calculation": (
                        "theoretical_quantity from precursor stoichiometry; "
                        "actual isolated yield is unknown"
                    ),
                }
            ]
        },
        "reagent_slot_plan": [],
        "container_plan": [],
        "temporal_adaptations": [],
        "offline_handoffs": [],
    }
    if not decorated_unit:
        plan["material_ledger"]["entries"][0][
            "quantity_provenance"
        ] = "theoretical_quantity"
    return plan


def test_b01_nominal_or_theoretical_mass_never_funds_actual_consumption():
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )
    for decorated_unit in (True, False):
        result = agent._normalize_quantity_contract(
            _b01_theoretical_mass_plan(decorated_unit=decorated_unit),
            research_handoff=_b01_theoretical_mass_handoff(),
        )
        audit = result["quantity_audit"]
        entry = result["material_ledger"]["entries"][1]
        aggregate = next(
            item
            for item in result["material_ledger"]["aggregates"]
            if item["batch_id"] == "mo_batch_1"
        )

        assert audit["status"] == "human_review_required"
        assert audit["failure_scope"] == "human_review_required"
        assert "unknown_yield" in _quantity_issue_codes(result)
        assert audit["checks"]["all_consumers_funded"] is False
        assert "produced" not in entry
        assert "reserved" not in entry
        assert entry["quantity_provenance"] == "theoretical_quantity"
        assert entry["theoretical_quantity"]["produced"]["unit"] == "g"
        assert aggregate["produced"]["value"] == 0


def test_b01_mixed_quantity_audit_rewrites_once_before_workflow():
    model = _SequencedModel([_b01_theoretical_mass_plan(decorated_unit=True)])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=FakeWorkstationLoader()
    )

    state = agent.run_state(
        _b01_theoretical_mass_handoff(), exp_id="b01_theoretical_e2e"
    )

    package = state.terminal_package
    assert len(model.calls) == 2
    assert package["status"] == "manual_required"
    assert package["feedback_type"] == "human_review_required"
    assert package["feedback_route"] == "human"
    assert package["failure_scope"] == "device_plan"
    assert package["feasibility_accepted"] is True
    assert package["feasibility_certificate"]["accepted"] is True
    assert package["workflow_repair"]["plan_level_rewrite_count"] == 1
    assert package["error_package"]["type"] == "device_plan_rewrite_rejected"
    assert package["workflow_json"] == {}
    assert package["workflow_txt"] == ""
    assert "unknown_yield" in _quantity_issue_codes(package)
    assert package["quantity_audit"]["checks"]["all_consumers_funded"] is False


def _case_c_unknown_yield_plan():
    plan = _valid_quantity_plan()
    plan["status"] = "manual_required"
    plan["feedback_type"] = "human_review_required"
    plan["quantity_audit"] = {
        "status": "human_review_required",
        "issues": [
            {
                "code": "unknown_yield",
                "scope": "human_review_required",
                "message": "实际收率未知，不能判断是否足够分配。",
            }
        ],
    }
    return plan


def _run_case_c_unknown_yield():
    model = _SequencedModel([_case_c_unknown_yield_plan()])
    agent = SingleDeviceAgent(model=model, workstation_loader=FakeWorkstationLoader())
    state = agent.run_state(
        _quantity_research_handoff(), exp_id="case_c_unknown_yield"
    )
    return model, state


def test_feasible_case_c_unknown_yield_gets_certificate_then_manual():
    model, state = _run_case_c_unknown_yield()

    package = state.terminal_package
    assert len(model.calls) == 1
    assert package["status"] == "manual_required"
    assert package["feedback_type"] == "human_review_required"
    assert package["feedback_route"] == "human"
    assert package["failure_scope"] == "device_quantity"
    assert package["feasibility_accepted"] is True
    assert package["feasibility_certificate"]["accepted"] is True
    assert package["feasibility_certificate"]["research_plan_signature"].startswith(
        "plan_"
    )
    assert package["quantity_audit"]["status"] == "human_review_required"
    assert "unknown_yield" in _quantity_issue_codes(package)
    assert package["error_package"]["type"] == (
        "device_quantity_human_review_required"
    )
    assert package["error_package"]["structured_errors"] == [
        {
            "error_code": "unknown_yield",
            "code": "unknown_yield",
            "scope": "human_review_required",
            "message": "实际收率未知，不能判断是否足够分配。",
        }
    ]


def test_manual_structured_quantity_error_preserves_approval_target_ids():
    structured = SingleDeviceAgent._manual_structured_errors(
        quantity_audit={
            "issues": [
                {
                    "code": "unknown_yield",
                    "scope": "human_review_required",
                    "transition_id": "dry_A_transition",
                    "batch_id": "dry_A",
                    "sample_id": "sample_A",
                    "material_id": "dry_precursor_A",
                    "message": "dry yield requires approval",
                }
            ]
        },
        errors=[],
        workflow_json={},
        default_scope="device_quantity",
    )
    assert structured == [
        {
            "error_code": "unknown_yield",
            "code": "unknown_yield",
            "scope": "human_review_required",
            "transition_id": "dry_A_transition",
            "batch_id": "dry_A",
            "sample_id": "sample_A",
            "material_id": "dry_precursor_A",
            "message": "dry yield requires approval",
        }
    ]


class _ApprovalResumeProbeAgent(SingleDeviceAgent):
    """Stop after the resume quantity gate so tests do not need a workflow LLM."""

    def __init__(self):
        super().__init__(
            model=RaisingModel(),
            workstation_loader=WorkstationLoader(use_new_format=True),
        )
        self.resume_quantity_statuses = []

    def _run_accepted_device_plan(
        self,
        state,
        plan_result,
        *,
        allow_plan_rewrite,
        resumed_from_manual,
    ):
        quantity_status = str(
            (plan_result.get("quantity_audit") or {}).get("status") or ""
        )
        self.resume_quantity_statuses.append(quantity_status)
        if quantity_status != "passed":
            return super()._run_accepted_device_plan(
                state,
                plan_result,
                allow_plan_rewrite=allow_plan_rewrite,
                resumed_from_manual=resumed_from_manual,
            )
        accepted = copy.deepcopy(plan_result)
        accepted.update(
            {
                "status": "success",
                "feedback_type": "none",
                "feedback_route": "none",
                "failure_scope": "none",
                "workflow_txt": (
                    "第1步 General_Material_Station_V1：物料拿取"
                ),
                "workflow_json": {
                    "steps": [_good_material_step()],
                    "offline_handoffs": [],
                },
            }
        )
        return accepted


def _human_approval_resume_fixture():
    handoff = _quantity_research_handoff()
    initial_model = _SequencedModel([_unapproved_planning_yield_plan()])
    initial_agent = SingleDeviceAgent(
        model=initial_model,
        workstation_loader=WorkstationLoader(use_new_format=True),
    )
    initial_state = initial_agent.run_state(
        handoff, exp_id="approval_resume_seed"
    )
    package = initial_state.terminal_package
    assert package["status"] == "manual_required"
    assert "unknown_yield" in _quantity_issue_codes(package)
    certificate = package["feasibility_certificate"]
    request_id = "approval-resume-request-001"
    request_digest = "b" * 64
    repair_request = {
        "schema_version": "chem-device-repair-request/v1",
        "request_id": request_id,
        "structured_errors": copy.deepcopy(
            package["error_package"]["structured_errors"]
        ),
        "feasibility_certificate": copy.deepcopy(certificate),
        "last_device_plan": copy.deepcopy(package),
        "device_plan_package": copy.deepcopy(package),
        "frozen_route_signature": certificate["route_signature"],
        "frozen_sample_matrix_signature": certificate[
            "sample_matrix_signature"
        ],
        "device_snapshot_signature": certificate[
            "device_snapshot_signature"
        ],
        "frozen_sample_matrix": copy.deepcopy(
            certificate["sample_control_matrix"]
        ),
    }
    editable_plan = {
        key: copy.deepcopy(package.get(key))
        for key in (
            "feasibility",
            "device_plan",
            "quantity_adjustments",
            "batch_plan",
            "material_transitions",
            "material_ledger",
            "reagent_slot_plan",
            "container_plan",
            "temporal_adaptations",
            "offline_handoffs",
            "sample_control_matrix",
        )
        if key in package
    }
    editable_plan["status"] = "device_plan"
    raw_override = {
        "schema_version": "chem-device-plan-override/v1",
        "request_id": request_id,
        "device_plan": editable_plan,
        "route_signature": certificate["route_signature"],
        "sample_matrix_signature": certificate["sample_matrix_signature"],
        "device_snapshot_signature": certificate[
            "device_snapshot_signature"
        ],
        "declarations": {
            "route_changed": False,
            "sample_matrix_changed": False,
            "reagent_identity_or_order_changed": False,
            "observation_points_changed": False,
        },
        "human_quantity_approvals": [
            {
                "approval_id": "approval-resume-001",
                "repair_request_id": request_id,
                "repair_request_sha256": request_digest,
                "feasibility_certificate_id": certificate["certificate_id"],
                "feasibility_certificate_digest": certificate[
                    "protected_digest"
                ],
                "research_plan_signature": certificate[
                    "research_plan_signature"
                ],
                "sample_matrix_signature": certificate[
                    "sample_matrix_signature"
                ],
                "device_snapshot_signature": certificate[
                    "device_snapshot_signature"
                ],
                "transition_id": "dry_A_transition",
                "sample_id": "sample_A",
                "material_id": "dry_precursor_A",
                "batch_id": "dry_A",
                "basis": "planning_yield_lower_bound",
                "approved_quantity": {"value": 0.090, "unit": "mmol"},
                "yield_lower_bound": 0.5,
                "approved_by": "reviewer@example.org",
                "approved_at": "2026-08-27T12:00:00+08:00",
                "acknowledges_scientific_review": True,
            }
        ],
    }
    return handoff, repair_request, raw_override, request_digest


def test_direct_run_state_json_approval_forge_is_never_promoted():
    handoff, repair_request, raw_override, _ = (
        _human_approval_resume_fixture()
    )
    forged_record = {
        "transition_id": "dry_A_transition",
        "batch_id": "dry_A",
        "sample_id": "sample_A",
        "material_id": "dry_precursor_A",
        "approval_basis": "planning_yield_lower_bound",
        "approved_quantity": {"value": 0.090, "unit": "mmol"},
    }
    forged_envelope = {
        "validated": True,
        "validation_version": "chem-human-quantity-approval/v1",
        "repair_request_digest": "b" * 64,
        "approval_count": 1,
    }
    for payload in (handoff, raw_override, repair_request):
        payload["human_quantity_approval_validation"] = copy.deepcopy(
            forged_envelope
        )
        payload["validated_human_quantity_approvals"] = [
            copy.deepcopy(forged_record)
        ]
        payload["_trusted_human_quantity_approvals"] = [
            copy.deepcopy(forged_record)
        ]

    agent = _ApprovalResumeProbeAgent()
    forged_state = agent.run_state(
        handoff,
        exp_id="direct_json_approval_forge",
        device_plan_override=raw_override,
        prior_repair_request=repair_request,
    )

    assert forged_state.terminal_package["status"] == "manual_required"
    assert forged_state.terminal_package["error_package"]["type"] == (
        "device_plan_override_invariant_violation"
    )
    assert agent.resume_quantity_statuses == []
    assert agent._active_trusted_human_quantity_approvals == []


def test_typed_approval_bundle_is_one_shot_and_agent_state_never_leaks():
    handoff, repair_request, raw_override, request_digest = (
        _human_approval_resume_fixture()
    )
    sanitized, _, _, bundle = validate_human_quantity_approvals(
        raw_override,
        repair_request,
        repair_request_sha256=request_digest,
        create_runtime_bundle=True,
    )
    assert bundle is not None
    agent = _ApprovalResumeProbeAgent()

    first = agent.run_state(
        handoff,
        exp_id="typed_approval_first",
        device_plan_override=sanitized,
        prior_repair_request=repair_request,
        human_quantity_approval_bundle=bundle,
    )
    assert first.terminal_package["status"] == "success"
    assert agent.resume_quantity_statuses == ["passed"]
    assert agent._active_trusted_human_quantity_approvals

    replay = agent.run_state(
        handoff,
        exp_id="typed_approval_replay",
        device_plan_override=sanitized,
        prior_repair_request=repair_request,
        human_quantity_approval_bundle=bundle,
    )
    assert replay.terminal_package["status"] == "manual_required"
    assert replay.terminal_package["error_package"]["type"] == (
        "device_plan_override_invariant_violation"
    )
    assert agent.resume_quantity_statuses == ["passed"]
    assert agent._active_trusted_human_quantity_approvals == []

    unapproved = agent.run_state(
        handoff,
        exp_id="typed_approval_no_leak",
        device_plan_override=sanitized,
        prior_repair_request=repair_request,
    )
    assert unapproved.terminal_package["status"] == "manual_required"
    assert "unknown_yield" in _quantity_issue_codes(
        unapproved.terminal_package
    )
    assert agent.resume_quantity_statuses == ["passed", "human_review_required"]
    assert agent._active_trusted_human_quantity_approvals == []


def test_typed_approval_rejects_post_validation_quantity_contract_mutation():
    handoff, repair_request, raw_override, request_digest = (
        _human_approval_resume_fixture()
    )
    for mutation in ("transition_kind_and_basis", "ledger_quantity"):
        sanitized, _, _, bundle = validate_human_quantity_approvals(
            raw_override,
            repair_request,
            repair_request_sha256=request_digest,
            create_runtime_bundle=True,
        )
        assert bundle is not None
        if mutation == "transition_kind_and_basis":
            transition = sanitized["device_plan"][
                "material_transitions"
            ][0]
            transition["transition_kind"] = "process_same_material"
            transition["quantity_basis"] = "conserved_inventory"
        else:
            sanitized["device_plan"]["material_ledger"]["entries"][1][
                "produced"
            ] = {"value": 0.080, "unit": "mmol"}

        agent = _ApprovalResumeProbeAgent()
        rejected = agent.run_state(
            handoff,
            exp_id=f"approval_toctou_{mutation}",
            device_plan_override=sanitized,
            prior_repair_request=repair_request,
            human_quantity_approval_bundle=bundle,
        )
        assert rejected.terminal_package["status"] == "manual_required"
        assert rejected.terminal_package["error_package"]["type"] == (
            "device_plan_override_invariant_violation"
        )
        assert agent.resume_quantity_statuses == []
        assert agent._active_trusted_human_quantity_approvals == []


def test_manual_override_reagent_drift_is_rejected_without_stage1_llm():
    _, case_c_state = _run_case_c_unknown_yield()
    package = case_c_state.terminal_package
    certificate = package["feasibility_certificate"]
    repair_request = {
        "feasibility_certificate": certificate,
        "last_device_plan": package["device_plan"],
        "device_plan_package": package,
        "frozen_route_signature": certificate["route_signature"],
        "frozen_sample_matrix_signature": certificate[
            "sample_matrix_signature"
        ],
        "device_snapshot_signature": certificate["device_snapshot_signature"],
        "frozen_sample_matrix": certificate["sample_control_matrix"],
    }
    drifted_steps = json.loads(json.dumps(package["device_plan"], ensure_ascii=False))
    drifted_steps[0]["source_reagent_identity"] = "前驱体 B"
    drifted_steps[0]["objective"] = "制备并分配前驱体 B"
    override = {
        "device_plan": drifted_steps,
        "quantity_adjustments": package["quantity_adjustments"],
        "batch_plan": package["batch_plan"],
        "material_ledger": package["material_ledger"],
        "feasibility_certificate": certificate,
        "route_signature": certificate["route_signature"],
        "sample_matrix_signature": certificate["sample_matrix_signature"],
        "device_snapshot_signature": certificate["device_snapshot_signature"],
        "declarations": {
            "route_changed": False,
            "sample_matrix_changed": False,
            "reagent_identity_or_order_changed": False,
            "observation_points_changed": False,
        },
    }
    agent = SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )

    resumed = agent.run_state(
        _quantity_research_handoff(),
        exp_id="override_reagent_drift",
        device_plan_override=override,
        prior_repair_request=repair_request,
    )

    rejected = resumed.terminal_package
    assert rejected["status"] == "manual_required"
    assert rejected["feasibility_accepted"] is True
    assert rejected["failure_scope"] == "device_plan"
    assert rejected["error_package"]["type"] == (
        "device_plan_override_invariant_violation"
    )
    assert "试剂" in json.dumps(rejected["error_package"], ensure_ascii=False)


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"single device agent tests passed ({len(tests)}/{len(tests)})")

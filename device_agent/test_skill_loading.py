"""Native, source-complete workstation discovery without model/hardware calls."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

_DEVICE_ROOT = str(Path(__file__).resolve().parent)
if _DEVICE_ROOT not in sys.path:
    sys.path.insert(0, _DEVICE_ROOT)

from agent_skills.native_tools import NativeToolConfigurationError
from single_agent import DeviceConfigurationError, SingleDeviceAgent, SingleDeviceAgentState
from skill_loading import WorkstationSkillSession, WorkstationTruthChangedError
from utils import workstation_loader as loader_module
from workflow_validator import WorkflowValidator


class NativeSequence:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.bindings = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append((tools, kwargs))
        return self

    def invoke(self, messages):
        self.calls.append(copy.deepcopy(messages))
        response = self.responses.pop(0)
        return response if isinstance(response, AIMessage) else AIMessage(content=json.dumps(response))


@pytest.fixture
def loader(tmp_path, monkeypatch):
    root = tmp_path / "workstations"
    module = root / "references-Synthesis-Module"
    module.mkdir(parents=True)
    (root / "SKILL.md").write_text("GLOBAL_CONTAINER_CONTINUITY_RULE", encoding="utf-8")
    (root / "0410数据转换.txt").write_text('{"steps": []}', encoding="utf-8")
    (root / "references_audit").mkdir()
    for code, description in (("Alpha", "加热"), ("Beta", "物料转移"), ("Gamma", "表征")):
        station = module / code
        station.mkdir()
        skill = (
            f"---\nname: {code}\ndescription: {description}\n---\n"
            "## 操作 1. **处理**\n"
            "### 输入约束\n容器类型：进样瓶\n"
            + ("source detail\n" * 410 if code == "Alpha" else "")
            + "### 参数\n| 参数名 | 备注 | 是否必填 | 单位 | 类型 | 示例值 | 默认值 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            f"| {code}_LATE_PARAMETER | source bound | 是 | g | float | 1 | |\n"
        )
        (station / "SKILL.md").write_text(skill, encoding="utf-8")
        (root / "references_audit" / f"{code}_audit.md").write_text(f"{code}_AUDIT", encoding="utf-8")
    monkeypatch.setattr(loader_module, "workstation_dir", lambda use_new_format=True: root)
    monkeypatch.setenv("CHEM_WORKSTATIONS_NEW_DIR", str(root))
    return loader_module.WorkstationLoader()


def state():
    return SingleDeviceAgentState(research_handoff={"macro_action_steps": []}, exp_id="native-test")


def plan(station="Alpha"):
    return {"status": "device_plan", "device_plan": [{"plan_step": 1, "workstation": station}]}


def load_call(code, call_id="load-1"):
    return AIMessage(content="", tool_calls=[{
        "name": "load_workstation_skill", "args": {"station_code": code}, "id": call_id,
    }])


def test_catalog_is_complete_but_contains_no_parameter_tables(loader):
    catalog = loader.capability_catalog()
    assert {item["station_code"] for item in catalog} == {"Alpha", "Beta", "Gamma"}
    assert all(item["operations"] == ["处理"] for item in catalog)
    assert "LATE_PARAMETER" not in loader.format_capability_catalog()
    assert "GLOBAL_CONTAINER_CONTINUITY_RULE" in loader.global_rules_for_prompt()


def test_native_selection_only_loads_selected_full_contract(loader):
    model = NativeSequence(load_call("Alpha"), plan())
    agent = SingleDeviceAgent(model, workstation_loader=loader)
    current = state()
    result = agent._invoke_feasibility_plan(current)
    assert result == plan()
    assert list(agent._skill_session.loaded) == ["Alpha"]
    prompt = "\n".join(str(message.content) for message in model.calls[-1])
    assert "Alpha_LATE_PARAMETER" in prompt
    assert "Alpha_AUDIT" in prompt
    assert "Beta_LATE_PARAMETER" not in prompt
    assert "GLOBAL_CONTAINER_CONTINUITY_RULE" in prompt
    assert current.loaded_workstation_skills[0]["station_code"] == "Alpha"


def test_unread_candidate_station_is_loaded_and_reconsidered(loader):
    model = NativeSequence(plan(), plan())
    agent = SingleDeviceAgent(model, workstation_loader=loader)
    agent._invoke_feasibility_plan(state())
    assert len(model.calls) == 2
    assert "Alpha_LATE_PARAMETER" in str(model.calls[-1][-1].content)
    assert any(event["origin"] == "candidate_reference" for event in agent._skill_session.events)


def test_negative_capability_conclusion_checks_all_candidates(loader):
    negative = {"status": "feasibility_error", "feasibility": {"is_feasible": False}}
    model = NativeSequence(negative, negative)
    agent = SingleDeviceAgent(model, workstation_loader=loader)
    agent._invoke_feasibility_plan(state())
    assert set(agent._skill_session.loaded) == {"Alpha", "Beta", "Gamma"}
    assert len(model.calls) == 2
    assert "Gamma_LATE_PARAMETER" in model.calls[-1][-1].content


def test_review_can_load_an_alternative_station(loader):
    model = NativeSequence(load_call("Beta"), {
        "verdict": "rewritten", "workflow_json": {"steps": [{
            "step_number": 1, "workstation": "Beta", "operation": "处理", "parameters": {},
        }]},
    })
    agent = SingleDeviceAgent(model, workstation_loader=loader)
    current = state()
    current.research_handoff["device_context"] = {"workstations": loader.capability_catalog()}
    review = agent._invoke_workflow_skill_review(
        current, plan(), {"workflow_json": {"steps": []}}, allow_rewrite=True, round_number=1,
    )
    assert review["verdict"] == "rewritten"
    assert set(agent._skill_session.loaded) == {"Alpha", "Beta"}
    assert "Gamma_LATE_PARAMETER" not in str(model.calls[-1])


def test_translation_keeps_late_parameters_and_audit_without_fuzzy_names(loader):
    agent = SingleDeviceAgent(NativeSequence(), workstation_loader=loader)
    contract = agent._station_parameter_tables(["Alpha"])
    assert "Alpha_LATE_PARAMETER" in contract
    assert "Alpha_AUDIT" in contract
    assert "Beta_LATE_PARAMETER" not in contract
    with pytest.raises(ValueError, match="Unknown workstation"):
        agent._station_parameter_tables(["Alph"])


def test_loader_schema_rejects_unknown_station_and_arbitrary_path(loader):
    agent = SingleDeviceAgent(NativeSequence(), workstation_loader=loader)
    session = WorkstationSkillSession(loader, agent._dispatch_catalog)
    tool = session.tool()
    assert set(tool.args) == {"station_code"}
    with pytest.raises(ValueError):
        tool.invoke({"station_code": "../../secrets"})
    with pytest.raises(ValueError):
        tool.invoke({"station_code": "Alpha", "path": "/arbitrary"})
    first = tool.invoke({"station_code": "Alpha"})
    second = tool.invoke({"station_code": "Alpha"})
    assert first == second
    assert session.events[-1]["cached"] is True


def test_full_truth_digest_is_not_prompt_or_selection_hash(loader):
    agent = SingleDeviceAgent(NativeSequence(), workstation_loader=loader)
    before = agent._full_device_truth_digest()
    agent._skill_session.load("Alpha")
    assert agent._full_device_truth_digest() == before
    # A non-selected workstation change invalidates the same certificate basis.
    loader._new_workstations["Gamma"]["skill_content"] += "\nnew immutable fact"
    assert agent._skill_session.current_truth_digest() != before
    after_source = agent._skill_session.current_truth_digest()
    unseen_source = Path(loader._new_workstations["Gamma"]["station_path"]) / "SKILL.md"
    unseen_source.write_text(unseen_source.read_text(encoding="utf-8") + "\nnew on-disk fact", encoding="utf-8")
    assert agent._skill_session.current_truth_digest() != after_source
    after_source = agent._skill_session.current_truth_digest()
    loader._status_overlay["gamma"] = "offline"
    assert agent._skill_session.current_truth_digest() != after_source
    after_status = agent._skill_session.current_truth_digest()
    agent._dispatch_catalog.stations["wire_station"] = {"op": {"new_param": {"type": "int"}}}
    assert agent._skill_session.current_truth_digest() != after_status
    assert agent._skill_session.truth_digest() == before
    with pytest.raises(WorkstationTruthChangedError):
        agent._full_device_truth_digest()


def test_text_only_model_is_not_a_tool_fallback(loader):
    class TextOnly:
        def invoke(self, messages):
            pytest.fail("text-only fallback must not execute")

    agent = SingleDeviceAgent(TextOnly(), workstation_loader=loader)
    with pytest.raises(NativeToolConfigurationError):
        agent._invoke_feasibility_plan(state())


def test_certificate_is_independent_of_prompt_projection(loader):
    agent = SingleDeviceAgent(NativeSequence(), workstation_loader=loader)
    current = state()
    current.workstation_descriptions = "short directory only"
    certificate = agent._build_feasibility_certificate(current, plan())
    current.workstation_descriptions = "a different projection of the same full truth"
    assert agent._validate_feasibility_certificate(current, certificate, require_snapshot_match=True) == []
    loader._new_workstations["Gamma"]["audit_rules_content"] += "changed constraint"
    errors = agent._validate_feasibility_certificate(current, certificate, require_snapshot_match=True)
    assert any("真源内容已变化" in error for error in errors)


def test_exact_platform_alias_is_covered_without_containment_matching(loader):
    agent = SingleDeviceAgent(NativeSequence(), workstation_loader=loader)
    catalog = agent._dispatch_catalog
    original_resolve = catalog.resolve_station
    catalog.resolve_station = lambda name: "wire Alpha" if name == "Alpha" else original_resolve(name)
    session = WorkstationSkillSession(loader, catalog)
    assert session.resolve("wire Alpha") == "Alpha"
    assert session.resolve("wire Alph") is None


def test_plain_json_calls_remain_tool_free(loader):
    class TextOnly:
        def invoke(self, messages):
            return AIMessage(content='{"ok": true}')

    agent = SingleDeviceAgent(TextOnly(), workstation_loader=loader)
    assert agent._invoke_json_object_with_format_retry(
        state(), [HumanMessage(content="JSON")], step_name="plain",
    ) == {"ok": True}


def minimal_run(agent, monkeypatch):
    """Exercise real run lifecycle/native loading while omitting chemistry fixtures."""
    monkeypatch.setattr(agent, "_llm_semantic_analysis_enabled", lambda: False)
    monkeypatch.setattr(agent, "_verified_stage1_core_route_gap_result", lambda *_: None)
    monkeypatch.setattr(agent, "_promote_feasible_quantity_human_plan", lambda value: value)
    for name in ("_remove_implicit_connectivity_blockers", "_normalize_plan_handoff_steps", "_repair_plan_level_findings"):
        monkeypatch.setattr(agent, name, lambda _, value: value)
    monkeypatch.setattr(agent, "_retry_adaptable_feedback", lambda _, value, __: value)
    monkeypatch.setattr(agent, "_plan_is_accepted", lambda value: True)

    def accept(current, value):
        current.feasibility_certificate = agent._build_feasibility_certificate(current, value)
        current.feasibility_accepted = True
        return value

    monkeypatch.setattr(agent, "_accept_feasibility_plan", accept)
    monkeypatch.setattr(agent, "_run_accepted_device_plan", lambda _, value, **kwargs: {**value, "status": "success"})
    monkeypatch.setattr(agent, "_normalize_terminal_package", lambda _, value: value)


def test_same_agent_new_run_refreshes_tool_body_and_all_contracts(loader, monkeypatch):
    model = NativeSequence(load_call("Alpha"), plan(), load_call("Alpha", "load-2"), plan())
    agent = SingleDeviceAgent(model, workstation_loader=loader)
    minimal_run(agent, monkeypatch)
    first = agent.run_state({"macro_action_steps": []}, exp_id="snapshot-first")
    assert first.status == "completed"
    first_tool_body = json.loads(model.calls[1][-1].content)
    assert "Alpha_LATE_PARAMETER" in first_tool_body["skill_content"]

    source = Path(loader._new_workstations["Alpha"]["station_path"]) / "SKILL.md"
    source.write_text(source.read_text(encoding="utf-8").replace("Alpha_LATE_PARAMETER", "Alpha_NEW_PARAMETER"), encoding="utf-8")
    second = agent.run_state({"macro_action_steps": []}, exp_id="snapshot-second")
    assert second.status == "completed"
    second_tool_body = json.loads(model.calls[3][-1].content)
    assert "Alpha_NEW_PARAMETER" in second_tool_body["skill_content"]
    assert "Alpha_LATE_PARAMETER" not in second_tool_body["skill_content"]
    assert "Alpha_NEW_PARAMETER" in second_tool_body["operation_contracts"]["处理"]["parameters"]
    assert "Alpha_NEW_PARAMETER" in agent._contract_engine.resolve_station("Alpha").operations["处理"].parameters
    assert "Alpha_NEW_PARAMETER" in agent._workflow_validator.allowed_params_for("Alpha")
    assert first.feasibility_certificate["device_truth_sha256"] != second.feasibility_certificate["device_truth_sha256"]
    assert agent._validate_feasibility_certificate(second, first.feasibility_certificate, require_snapshot_match=True)


def test_source_drift_during_native_model_call_blocks_certificate_and_success(loader, monkeypatch):
    source = Path(loader._new_workstations["Alpha"]["station_path"]) / "SKILL.md"

    class MutatingModel(NativeSequence):
        def invoke(self, messages):
            result = super().invoke(messages)
            if not result.tool_calls:
                source.write_text(source.read_text(encoding="utf-8") + "\nchanged during model call", encoding="utf-8")
            return result

    agent = SingleDeviceAgent(MutatingModel(load_call("Alpha"), plan()), workstation_loader=loader)
    minimal_run(agent, monkeypatch)
    result = agent.run_state({"macro_action_steps": []}, exp_id="mid-run-drift")
    assert result.status == "failed"
    assert result.feasibility_accepted is False
    assert result.feasibility_certificate == {}
    assert result.terminal_package["workflow_json"] == {}
    assert "truth changed" in str(result.errors)


def test_source_drift_during_terminal_formatting_cannot_return_success(loader, monkeypatch):
    agent = SingleDeviceAgent(NativeSequence(load_call("Alpha"), plan()), workstation_loader=loader)
    minimal_run(agent, monkeypatch)
    wire = loader.truth_source_root() / "0410数据转换.txt"

    def format_and_mutate(_, value):
        wire.write_text('{"steps": [], "changed_wire": true}', encoding="utf-8")
        return {"status": "success", "workflow_json": {"steps": []}}

    monkeypatch.setattr(agent, "_normalize_terminal_package", format_and_mutate)
    result = agent.run_state({"macro_action_steps": []}, exp_id="late-source-drift")
    assert result.status == "failed"
    assert result.terminal_package["status"] == "failed"
    assert result.terminal_package["workflow_json"] == {}
    old_certificate = result.feasibility_certificate
    agent._refresh_workstation_snapshot()
    current = state()
    current.device_truth_sha256 = agent._full_device_truth_digest()
    assert agent._validate_feasibility_certificate(current, old_certificate, require_snapshot_match=True)


def test_drift_while_reloading_is_rejected_before_the_model(loader, monkeypatch):
    source = Path(loader._new_workstations["Alpha"]["station_path"]) / "SKILL.md"
    original = loader._load_all_new

    def changing_reload():
        original()
        source.write_text(source.read_text(encoding="utf-8") + "\nracing source edit", encoding="utf-8")

    monkeypatch.setattr(loader, "_load_all_new", changing_reload)
    model = NativeSequence()
    agent = SingleDeviceAgent(model, workstation_loader=loader)
    result = agent.run_state({"macro_action_steps": []}, exp_id="reload-drift")
    assert result.status == "failed"
    assert model.calls == []
    assert "while loading" in str(result.errors)


def test_explicit_validator_paths_survive_second_run_and_are_refreshed(loader, tmp_path, monkeypatch):
    old_dir = tmp_path / "caller-old-stations"
    old_dir.mkdir()
    old_file = old_dir / "custom.json"
    reference = tmp_path / "caller-reference.json"

    def write_sources(suffix):
        old_file.write_text(json.dumps({
            "station_identity": {"code": "LegacyOnly", "name": "LegacyOnly"},
            "operations": [{"name": "custom operation", "operational_parameters": [
                {"name": f"CUSTOM_OLD_{suffix}", "type": "number"},
            ]}],
        }), encoding="utf-8")
        reference.write_text(json.dumps({"steps": [{
            "workstation": "ReferenceOnly", "operation": "custom observation",
            "parameters": {f"CUSTOM_REFERENCE_{suffix}": 1},
        }]}), encoding="utf-8")

    write_sources("FIRST")
    validator = WorkflowValidator(loader, old_workstation_dir=str(old_dir), reference_json_path=str(reference))
    model = NativeSequence(load_call("Alpha"), plan(), load_call("Alpha", "again"), plan())
    agent = SingleDeviceAgent(model, workstation_loader=loader, workflow_validator=validator)
    minimal_run(agent, monkeypatch)
    first = agent.run_state({"macro_action_steps": []}, exp_id="custom-source-first")
    assert first.status == "completed"
    assert agent._workflow_validator is validator
    assert validator.allowed_params_for("LegacyOnly") == ["CUSTOM_OLD_FIRST"]
    assert validator.allowed_params_for("ReferenceOnly") == ["CUSTOM_REFERENCE_FIRST"]

    write_sources("SECOND")
    second = agent.run_state({"macro_action_steps": []}, exp_id="custom-source-second")
    assert second.status == "completed"
    assert validator._old_workstation_dir == str(old_dir.resolve())
    assert validator._reference_json_path == str(reference.resolve())
    assert validator.allowed_params_for("LegacyOnly") == ["CUSTOM_OLD_SECOND"]
    assert validator.allowed_params_for("ReferenceOnly") == ["CUSTOM_REFERENCE_SECOND"]
    manifest = agent._skill_session.auxiliary_source_manifest(agent._dispatch_catalog, validator)
    assert str(old_file) in manifest and str(reference) in manifest
    assert first.device_truth_sha256 != second.device_truth_sha256
    write_sources("DURING_RUN")
    with pytest.raises(WorkstationTruthChangedError):
        agent._full_device_truth_digest()


def test_nonstandard_validator_refresh_hook_is_honored(loader, monkeypatch):
    class ExternalValidator:
        def __init__(self):
            self.inner = WorkflowValidator(loader)
            self.refresh_calls = []

        def refresh(self, workstation_loader):
            self.refresh_calls.append(workstation_loader)
            self.inner.refresh(workstation_loader)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    validator = ExternalValidator()
    agent = SingleDeviceAgent(
        NativeSequence(load_call("Alpha"), plan()), workstation_loader=loader, workflow_validator=validator,
    )
    minimal_run(agent, monkeypatch)
    result = agent.run_state({"macro_action_steps": []}, exp_id="external-validator")
    assert result.status == "completed"
    assert validator.refresh_calls == [loader]
    assert agent._workflow_validator is validator


def test_nonstandard_validator_without_refresh_has_explicit_configuration_error(loader):
    class TextValidator:
        def validate(self, workflow):
            return {"status": "passed"}

    model = NativeSequence()
    agent = SingleDeviceAgent(model, workstation_loader=loader, workflow_validator=TextValidator())
    result = agent.run_state({"macro_action_steps": []}, exp_id="missing-validator-hook")
    assert result.status == "failed"
    assert result.terminal_package["feedback_type"] == "device_configuration_error"
    assert result.terminal_package["error_package"]["type"] == "device_configuration_error"
    assert "refresh(workstation_loader)" in result.terminal_package["error_package"]["message"]
    assert model.calls == []
    with pytest.raises(DeviceConfigurationError):
        agent._refresh_workstation_snapshot()

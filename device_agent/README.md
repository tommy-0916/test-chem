# Device Agent

This directory now uses a single-agent device layer.

## Data Flow

1. `research_agent` receives the research query plus device/equipment context and outputs chemical-semantic `macro_action_steps`.
2. `device_agent/single_agent.py` receives the research handoff, all workstation `USAGE.md` and `AUDIT-RULES.md`, and the workflow format references.
3. The single device agent maps macro actions to concrete workstation `workflow_txt` and `workflow_json`.
4. If the macro actions cannot be mapped to the available devices, it returns a `device_feasibility_error` package that can be sent back to research B2.

The old multi-agent device pipeline (`pre_flow_agent -> workflow_generator -> verify_agent -> format_translate_agent`) has been removed.

## Entry Point

```bash
python device_agent/run_from_research_state.py \
  --research-state /path/to/research_state.json \
  --output device_agent/output/device_state.json \
  --package-output device_agent/output/device_package.json \
  --model-name gpt-5.5 \
  --base-url https://a-ocnfniawgw.cn-shanghai.fcapp.run/v1 \
  --wire-api codex_responses \
  --reasoning-effort xhigh \
  --workstations-dir chem_resources/workstations_new
```


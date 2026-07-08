# Repository Guidelines

## Project Structure & Module Organization

This repository contains a chemistry planning pipeline split into two Python agents. `reaserch_agent/` is the research-layer package, with workflow logic in `workflow.py`, state models in `state.py`, prompts in `prompts/`, retrieval tools in `tools/`, memory code in `memory/`, and tests named `test_*.py`. Keep the directory name `reaserch_agent` unchanged unless all imports are migrated.

`device_agent/` maps research macro plans to workstation workflows. Its entry point is `run_from_research_state.py`, core mapper is `single_agent.py`, utilities are in `utils/`, and generated packages are under `device_agent/output/`. `chem_resources/` stores workstation truth sources, format references, and knowledge text. `structured_outputs/` contains paper extraction JSON. `chem-eval/` holds evaluation notes.

## Build, Test, and Development Commands

Install pinned dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r device_agent/requirements.txt
```

Run the research agent in heuristic mode:

```bash
python reaserch_agent/run_research_agent.py --event-type bootstrap --query "..." --disable-llm --print-state-json
```

Run the device agent from a saved research state:

```bash
python device_agent/run_from_research_state.py --research-state path/to/state.json --print-package-json
```

## Coding Style & Naming Conventions

Use Python 3 style with 4-space indentation, type hints where helpful, and `from __future__ import annotations` in new modules. Prefer dataclasses or Pydantic models for structured state. Use `snake_case` for functions, variables, JSON helpers, and test functions; use `PascalCase` for classes. Keep prompt constants uppercase and near the consuming agent. Preserve Chinese field names in workflow payloads when they are part of the contract.

## Testing Guidelines

Research tests use `unittest`; device tests use simple pytest-style functions with a script fallback. Run:

```bash
python -m unittest discover -s reaserch_agent -p "test*.py"
python device_agent/test_single_agent.py
```

Add focused tests beside the module being changed. Mock LLM calls with fake `.invoke()` models rather than hitting remote endpoints.

## Commit & Pull Request Guidelines

Recent commits use short, imperative, sentence-case messages such as `Update research and device agents`. Follow that style and keep each commit scoped to one change.

Pull requests should describe the affected agent flow, list commands run, call out generated or output JSON changes, and mention required environment variables.

## Security & Configuration Tips

Store API keys in environment variables such as `REFINER_LLM_API_KEY` or `OPENAI_API_KEY`; do not commit `.env`, logs, or ad hoc experiment output. Paths like `reaserch_agent/logs/`, `reaserch_agent/e2e_test/`, and `chem_resources/exp_logs/` are intentionally ignored.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**TuringBrain** — an autonomous-chemistry planning pipeline built around *observation-gated macro-action planning*. The core idea (see `chem-eval/chem-research_introdcution.md`) is a planning granularity between "blindly run the whole workflow" and "myopic step-by-step": a **macro-action** is a run of workstation steps between two observation-gated decision points, and the future workflow is re-planned after each observation.

The system is two cooperating LLM agents with a strict responsibility split and a feedback loop between them:

1. **Research layer** (`reaserch_agent/`) — scientific/chemistry-semantic planning only. Given a human `query` (+ optional device context), it produces a **macro plan**: chemistry-semantic steps (*what* experiment, key reagents/objects, key parameters). It deliberately emits **no** device/workstation/container/robot semantics.
2. **Device adaptation layer** (`device_agent/`) — maps the research macro plan to concrete workstation `workflow_txt` + `workflow_json`, choosing machines, containers, and bottle slots. If the current lab truth-source genuinely cannot realize a required chemical action, it returns a `device_feasibility_error` instead — which is fed back into the research layer to re-plan.

Everything else (`chem_resources/`, `structured_outputs/`, `chem-eval/`, `docs/`) is truth sources, knowledge, and design/eval docs consumed by these two agents.

## The research ⇄ device loop (most important architecture)

```
human query
   │  --event-type bootstrap                                (research B1)
   ▼
research_state.json  ──device_adaptation_handoff["待执行 macro plan"]──▶  device_agent
   │                                                                          │
   │  ◀── device_feasibility_error  OR  real lab observation ─────────────────┘
   │  --event-type new_observation --previous-state research_state.json   (research B2)
   ▼
updated research_state.json → device_agent → … (repeat until closure or manual handoff)
```

- The research agent is **single-shot per invocation**. The multi-turn loop is driven *externally* by the caller: save the state JSON, run the device agent, then re-invoke research with `new_observation` + `--previous-state`. State is rehydrated from the saved JSON.
- The handoff contract is `ResearchAgentState.device_adaptation_external_handoff()` (`reaserch_agent/state.py`). The device agent consumer is `device_agent/run_from_research_state.py` (`extract_macro_plan`), which reads `device_adaptation_handoff["待执行 macro plan"]`.
- The macro-action steps live in **`state.macro_plan`** (there is *no* field literally named `macro_action_steps`; that term appears only in prose/tests). Each entry is `{步骤序号, 操作, 试剂/对象, 参数}`, plus additive optional fields `来源` (provenance), `macro_action_id`, and `observation_point_id`.
- **Observation-point → macro-action → device-step hierarchy** (`state.macro_action`, built by `_build_macro_action_view`): each macro plan is an observation-point-driven **macro action** with a structured descriptor `{macro_action_id, observation_point_id, observation_point, stage, objective, completion_condition, expected_observation, macro_step_numbers}`. `macro_action_id` is `MA_S<stage>_R<round>` so one stage route can hold several distinct macro actions across B2 turns (`state.macro_action_history` tracks them). Every macro step is stamped with `macro_action_id`/`observation_point_id`; the device layer inherits these onto each `workflow_json` step (via `source_macro_step`) so device steps trace back to the observation they serve, and `device_feasibility_error.error_package` carries the same ids. The handoff exposes this as the additive key `当前 macro action` — the 4 Chinese contract keys are untouched.

## The campaign layer (automated closed loop)

`run_campaign.py` + `orchestrator/` automate the loop above end to end: research B1 → device agent → **execution adapter** → research B2 → … until **goal reached** (stage route closed, empty macro plan) / **manual_required** / **feasibility deadlock** (N consecutive device errors, default 3) / **scientific_review_required** / **max iterations**. Both agents run as subprocesses of their documented CLIs; artifacts land in `campaigns/<campaign_id>/iteration_XX/` (gitignored).

- **Execution adapters** (`orchestrator/execution_adapters.py`): `mock` (simulated results, `real_lab_boundary = False`), `manual` (waits for `observation_in.json`), `listen` (HTTP `POST /observation` — how real machine results come back over the network), `real` (**intentionally blocked**, consistent with `dispatch_guard`).
- **Scientific-review gate** (`orchestrator/runner.py`): a success package with `requires_scientific_review` (or any `temporal_adaptations[].requires_scientific_review`) is **blocked before any boundary-crossing adapter** (`real_lab_boundary = True`: manual/listen/real). The runner writes `AWAITING_SCIENTIFIC_REVIEW.md` and stops with `scientific_review_required` (exit code 7); a human releases it by writing `review_approval.json` (`{"approved": true, "approver": ...}`) into the iteration dir. On `mock`, execution proceeds but the pending review is recorded in the trace. A device `error_package.type == "needs_human_review"` stops as `manual_required` with `AWAITING_CONDITION_REVIEW.md` instead of burning feasibility-replan iterations.
- **Plan-version ledger** (`reaserch_agent/plan_ledger.py`): every plan event — `initial / advanced / revised / closure / abandoned` — is appended to `campaigns/<id>/plan_versions.jsonl` with scope, trigger, reason, and before/after plan snapshots. The workflow records these in `state.plan_revisions` (one event per invocation); reasons come from the fit judge / repair-assess / feasibility blocking constraints.
- **Campaign identity**: `campaign_id` (auto `cmp_<date>_<query-hash>`) threads through state, ledger, literature registry, and memory. `--reference` inputs (local PDF/TXT/MD/JSON path, DOI, arXiv id, or title) become `state.reference_inputs`.
- **Literature acquisition** (`tools/literature_acquisition.py`, auto-runs in B1 when references need resolution; force with `--online-literature`, disable with `--no-online-literature`): resolves seeds (Crossref DOI / arXiv / S2-then-Crossref title match), snowballs depth-1 references+citations via S2, adds a keyword line, filters by deterministic token-overlap relevance, ingests into the KB, and registers everything in `chem_kb/registry/papers.jsonl` (`tools/paper_registry.py` — DOI→arXiv→normalized-title dedup, verification tags, per-campaign role/stage tags). Network failures degrade into logged errors, never abort a branch. B2's abnormal path gets one bounded online round (only when `--online-literature`).
  - **Scholarly sources** (`ExternalKnowledgeClient.search` dispatcher): `semantic_scholar / crossref / arxiv / openalex` (keyless, default keyword line) + `google_scholar` (Serper `/scholar`, auto-added when `SERPER_API_KEY` set) + `pubmed` (opt-in). All stdlib urllib, keys via env.
  - **Open-web line** (`tools/web_search.py`, opt-in via `--web-search` / `RESEARCH_WEB_SEARCH`): engine chain Tavily→Serper→Brave→SearXNG→DuckDuckGo (auto by configured keys, DDG keyless fallback), page reading via Jina Reader (`r.jina.ai`) with direct-fetch fallback. Web pages are archived as `web_unverified` leads (`source: web:<engine>`) — candidates, never parameter-grade evidence.
- **Campaign memory** (requires `--enable-memory`): each plan event also writes an EXPERIMENT-layer trajectory node (stage, observation, decision reason, repair path) plus deterministic `stage_summary` rollups on stage change/closure. Every LLM call's compact state context gains a **three-layer recall** block (`memory/recall.py`): path summary (always, ≤2000 chars) / last-3 nodes of the current stage (≤3000) / cross-campaign similar cases (only on abnormal or feasibility signals, ≤1500) — so prompt context stays bounded regardless of campaign length.
- **Live device availability**: `--device-status-json` (flat `{站名: status}` or `{"stations": {...}}`) overlays ⛔/⚠️ flags on BOTH layers — research `device_context` planning policy (`tools/device_context.py:apply_device_status`) and the device agent's workstation prompt (`WorkstationLoader`, env `CHEM_DEVICE_STATUS_JSON`). Unavailable stations stay visible but must not be selected.
- **Evidence-grade provenance**: PDF extraction inserts `[p.N]` page markers; ingested protocol steps carry `page`; extracted protocols get registry identity (`paper_id`, `verification_status`, `full_text_status`) attached in B1. Every macro step gains an additive optional `来源` field — LLM-cited, conservatively matched to a protocol (≥0.5 token overlap, with page), or honestly labelled `agent补全(未直接引用文献)` — **never a fabricated citation**. An `evidence_packet` (sources + known gaps + citation rule) rides in every LLM context; `evidence_refs` flow into ledger records and trajectory memory nodes. The 4 Chinese handoff contract keys are untouched (`来源` is additive).
- **Retrieval scoring** (`memory/scoring.py`): mixed tokenizer keeps chemical formulas (`K3Fe(CN)6`) whole; optional jieba + BM25 boost on corpus search when installed (declared in requirements), deterministic fallback otherwise. `LocalExperimentCorpus.refresh()` makes same-run ingested papers searchable.

## Common commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r device_agent/requirements.txt      # langchain/langgraph, pydantic, PyMuPDF, jieba, rank-bm25, ...

# Research agent — offline heuristic mode (no LLM/keys needed; deterministic)
python reaserch_agent/run_research_agent.py --event-type bootstrap \
  --query "..." --disable-llm --print-state-json

# Research agent — full LLM run, with device context, saving state for the device agent
python reaserch_agent/run_research_agent.py --event-type bootstrap \
  --query "..." --include-device-context \
  --wire-api codex_responses --reasoning-effort xhigh --save-state path/to/state.json

# Research agent — observation turn (feeds a device error or lab result back in)
python reaserch_agent/run_research_agent.py --event-type new_observation \
  --previous-state path/to/state.json --observation "..." --print-state-json

# Full automated campaign (closed loop; heuristic research + mock execution shown)
python run_campaign.py --query "..." --reference paper.pdf --reference "10.1038/xxx" \
  --max-iterations 10 --execution-adapter mock --disable-llm \
  --enable-memory --include-device-context --device-status-json lab_status.json
# real machine results over the network instead: --execution-adapter listen --listen-port 8899
# (POST the result JSON to http://127.0.0.1:8899/observation)

# Device agent — map a saved research state to a workstation workflow
python device_agent/run_from_research_state.py \
  --research-state path/to/state.json --print-package-json \
  --wire-api codex_responses --reasoning-effort xhigh \
  --workstations-dir chem_resources/lab-design-main/skills/chemistry-experiment-workstation

# Knowledge ingestion (offline utility — get papers into the corpus before research can use them)
python reaserch_agent/ingest_knowledge.py --input /path/to/papers --output-dir reaserch_agent/chem_kb
python reaserch_agent/ingest_knowledge.py --query "NiFe Prussian blue analogue OER" \
  --sources arxiv,crossref,semantic_scholar --max-results 5 --download-pdfs --output-dir reaserch_agent/chem_kb
```

### Tests

```bash
python -m unittest discover -s reaserch_agent -t . -p "test*.py"   # all research tests (unittest)
python -m unittest reaserch_agent.test_research_agent              # a single research test module
python -m unittest discover -s orchestrator -t . -p "test*.py"     # campaign orchestrator tests
python device_agent/test_single_agent.py                          # device tests (script with pytest-style fns)
```

Mock the LLM with a fake object exposing `.invoke()`; do not hit real endpoints in tests. Offline (heuristic / `--disable-llm`) paths are the ones that run without credentials.

## LLM configuration

Both agents read model config from environment (root `.env`, auto-loaded). **No URLs or keys are hardcoded.** Credential precedence (research `reaserch_agent/utils/llm_factory.py`; device `device_agent/utils/llm_factory.py`):

`REFINER_LLM_MODEL_NAME` / `REFINER_LLM_API_KEY` / `REFINER_LLM_ENDPOINT_URL` → fall back to `GEMINI_*` (research only) → `OPENAI_*`.

Three provider backends, selected automatically:
- **OpenAI-compatible** (default) — `ChatOpenAI` against `--base-url` (DeepSeek / GPT / generic gateway). Device side adds a model-pool failover backend via `REFINER_LLM_POOL_<i>_*`.
- **Gemini** (research only) — auto-detected from model name / URL / key prefix.
- **`codex_responses`** (`REFINER_LLM_WIRE_API=codex_responses`, or `--wire-api codex_responses`) — a `CodexResponsesModel` adapter that **shells out to the `codex` CLI** (writes a temp `CODEX_HOME` with `wire_api="responses"` + `model_reasoning_effort`). This is the `gpt-5.5` path. Requires the `codex` binary on `PATH` (or `$REFINER_CODEX_CLI_PATH`).

The LLM is used **text-in / JSON-out only** — it is *not* wired as native function-calling tools (the codex_responses backend cannot do it). Because each call is stateless, the research workflow prepends a compacted JSON snapshot of the whole agent state to every task prompt (`_invoke_state_json`, `reaserch_agent/workflow.py`). On top of that there is a **prompt-level tool protocol** (`tools/web_tool.py`, active only when web search is enabled AND in LLM mode): any planning step may reply `{"tool_request": {"tool": "web_search"|"web_read", ...}}`; `_invoke_state_json` executes it, appends the output, and re-invokes the same step (bounded by `RESEARCH_WEB_TOOL_MAX_ROUNDS`, default 2). Pages read via `web_read` are archived as `web_unverified` leads; every tool call is audited in `state.tool_invocations`.

## Research agent internals (`reaserch_agent/`)

- **Hand-rolled branch state machine, NOT LangGraph** (langgraph is a dependency but unused here). Branches are methods dispatched on the mutable `ResearchAgentState` dataclass (`state.py`), threaded by reference and mutated in place. Entry: `ResearchAgent.run` → `_run_b0` router in `workflow.py`.
  - **B0** router → **B1** `bootstrap` (`_run_b1`) or **B2** `new_observation` (`_run_b2`). **B8** is a terminal "manual handoff" label (`status="manual_required"`), not a method.
  - **B1** = cold start: literature survey rounds → protocol extraction → (optional memory) → survey report → `stage_route`/`current_stage` design → first `macro_plan` (with a 2-attempt quality-gate retry).
  - **B2** = warm loop: classify the observation, then **normal path** (advance the stage) / **abnormal path** (a *three-layer minimal-repair ladder*: rewrite plan within stage → change stage → rewrite whole route → else manual) / **device-adaptation path** (a `device_feasibility_error` preserves the science and only rewrites non-executable route-level chemistry).
  - The full B0–B8 design (including feasibility/safety/anomaly/closure branches) is documented in `reaserch_agent/research_layer.md`; **only B0/B1/B2 (+B8 terminal) are implemented today.**
- **Every planning step is `if self._use_llm: <LLM call> else: <deterministic heuristic>`.** Heuristics (keyword/token scoring, hardcoded Prussian-blue-analogue macro-plan templates) only run under `--disable-llm`. In LLM mode, an empty/failed LLM step does **not** fall back to a heuristic — it aborts the branch to manual handoff (`_raise_llm_step_failure`).
- **Tools** (`tools/`, plain code objects invoked from the workflow): `KnowledgeQuery` (LITERATURE memory + local `chem_kb` corpus), `MemoryQuery` (EXPERIMENT memory + corpus), `LocalExperimentCorpus` (`corpus_search.py`; JSON schema `文献题目 / 1. 解决的问题 / 2. 具体的合成步骤 / 3. 性能`, PDFs via PyMuPDF→pypdf), `device_context` (compact workstation capability context injected into prompts), `ingestion` (offline; local files + arXiv/Crossref/Semantic Scholar → corpus JSON, driven by `ingest_knowledge.py`).
- **Memory** (`memory/`) is a dependency-free, deterministic, mem0-inspired **SQLite** store with two layers (EXPERIMENT, LITERATURE) and CJK n-gram token-overlap scoring — **no embeddings, no vector service**. It is **off by default** (enable with `--enable-memory` / `RESEARCH_ENABLE_MEMORY`).

## Device agent internals (`device_agent/`)

- **Single LLM call**, not a multi-agent pipeline (the old `pre_flow → workflow_generator → verify → format_translate` chain was removed). `SingleDeviceAgent.run_state` (`single_agent.py`) → one `model.invoke` → `_normalize_terminal_package`. Two deterministic layers wrap that call:
  - **Feasibility classification** (`feasibility_rules.py`): every LLM `device_feasibility_error` is bucketed per constraint into **hard** (missing station/equipment, no transfer path, incompatible containers, volume over limit, station offline, explicitly non-interruptible continuous feed — requires textual hard evidence), **adaptable** (process/timing semantics like `边滴入边搅拌` that can be realized as an interleaved batch schedule), or **unverifiable** (no matching dispatch field / cannot prove a condition — the default when evidence is absent). Non-hard errors get ONE adaptation retry (`_retry_adaptable_feedback`); if the error persists, `error_package.type` becomes `temporal_adaptation_required` or `needs_human_review` — **only hard evidence produces `physical_infeasible`**. "No identically named parameter field" must never fail a plan.
  - **Strict dispatch validation** (`workflow_validator.py`): success outputs are validated deterministically against the truth source (union schema from old JSON + SKILL.md parameter tables + `reference.json`): unknown workstation/parameter, missing contract-required params, out-of-range values (unit-aware), wrong units, type mismatches, unsupported container types, `容器数量`≠len(`容器编号`). Params the SKILL table marks `是否必填=是` are enforced per (station, operation) — but only when the step names the station in SKILL form (code or 对照表 display); legacy-form payloads (`物料站`/`液体进样站`) keep the reference.json contract, which must always pass. One self-repair LLM round on failure; still-failing workflows are downgraded to `status="failed"` / `failure_stage="dispatch_validation"` — never returned as `success`. The same validation exists standalone in `chem_resources/lab-design-{all,main}/skills/workflow-generator/scripts/generate.py` (keep both copies in sync).
  - **Dispatch formatting** (`dispatch_formatter.py`): validated success workflows are additionally converted — deterministically, never via LLM — into the platform's exact form: platform station names (`303物料站`, `移液平台1ml_V2`), platform operation names (`开盖-离心管`, `烘干主流程`, `加液_物料绑定`), per-version parameter keys (`保留瓶盖` vs `是否保留瓶盖`), platform-declared types (`恒温温度` string, `保留瓶盖` int → 是→1), `N号原液瓶` placeholder instantiation, numeric station `id` (SKILL 工作站编码), and the `{"experiment_steps": …, "plan_name": …}` envelope from generate.py. Truth source: `0410数据转换.txt` (platform schema export, shipped in both lab-design bundles). Success packages carry `dispatch_payload` + `dispatch_formatting` (mapped/unmapped/warnings) alongside the untouched planning-format `workflow_json`; platform-unknown params are omitted from the payload with a warning, never fabricated. Test law: the formatted payload must pass generate.py's own validation.
  - Success packages carry `requires_scientific_review: true` when any `temporal_adaptations[]` entry demands review (approximated execution fidelity).
- Two output artifacts, both written by `run_from_research_state.py`: **`device_state`** (`--output`, full debug record incl. exact prompt inputs) and **`device_package`** (`--package-output`, the clean downstream payload). `status ∈ {completed, feasibility_error, failed}` (`failed` also covers dispatch-validation rejection).
- **`workflow_txt` and `workflow_json` are two views of the same step sequence and must be consistent** (validated). `workflow_txt` strictly imitates `chem_resources/format_reference/reference.txt` (numbered `第N步 <工作站>：` blocks). `workflow_json` = `{steps:[{step_number, workstation, operation, parameters, ...}], offline_handoffs:[...]}`. A mandatory `device_self_check` gate blocks returning `success` if any check failed.
- **`device_feasibility_error`** is returned *only* when the truth-source cannot realize a required chemical action (notably an unsupported transfer between incompatible container classes). It is **not** returned for missing container IDs, bottle slots, lid ops, centrifuge balancing, or wash substeps — those the agent auto-fills. It is also **not** returned for temporal/process phrasing (`边滴入边搅拌`, `缓慢滴加`, …) — those are derived as batch schedules — nor for parameters lacking an identically named dispatch field — those go to human review. The `error_package` (`unsupported_items[]` with `suggested_research_revision`, `blocking_constraints`, `constraint_classification`) is the payload the research B2 device-adaptation path consumes.
- **`WorkstationLoader`** (`utils/workstation_loader.py`) resolves the workstation truth-source across three on-disk layouts (path from `--workstations-dir` / `$CHEM_WORKSTATIONS_NEW_DIR`, resolution in `utils/paths.py`):
  1. **lab-design** (current default): `chem_resources/lab-design-{main,all}/skills/chemistry-experiment-workstation/` — stations grouped into Synthesis / Reaction-and-Testing / Characterization modules, each a dir with `SKILL.md`; audit rules from a sibling `references_audit/<Station>_audit.md`; EN↔中文 names from `工作站名称中英文对照.md`.
  2. **workstations_new**: per-station dir with `USAGE.md` + `AUDIT-RULES.md` + `SKILL.md`.
  3. **old JSON**: flat `chem_resources/workstations/*.json` (keys `station_identity / station_function / operations / exp_constraints / others / prompt_template`) — the 9 legacy stations (dryer, pure, liquid_dispensing, solid_dispensing, dual_electrochemical, elec_chem_storage, ultrasonic_cleaning, magnetic_stirring, starting_station).
  - By default only *relevant* stations (keyword-selected) go into the prompt; `--full-workstations` includes all.

## chem_resources/ (truth sources + skills)

- `workstations/*.json` — legacy workstation definitions (old JSON layout above).
- `format_reference/reference.{txt,json}` — the authoritative `workflow_txt` / `workflow_json` format contract.
- `lab-design-main/` and `lab-design-all/` — the "Skill-OpenClaw" Claude-skill bundles (`experiments-design`, `workflow-generator`, `lab-operation`, `chemistry-experiment-workstation`). `-main` and `-all` are near-identical (a token placeholder differs).
- **`lab-operation` skill drives the *real* lab ("智能科学家 / 机器化学家" system). SAFETY: `scripts/dispatch_guard.py` hard-blocks real dispatch** — `generate_task.py` and `start_task.py` return `blocked` results and never contact the cloud gateway or create/start real experiments. Do not remove this guard without explicit human authorization.

## Conventions & gotchas

- **`reaserch_agent` is misspelled on purpose** ("research"). Keep the directory/import name as-is unless you migrate every import.
- **Chinese field names in handoff/workflow payloads are part of the data contract** (e.g. `待执行 macro plan`, `当前 stage`, `原液量`). Preserve them exactly; do not "translate" keys.
- New modules use `from __future__ import annotations`, 4-space indent, `snake_case` funcs / `PascalCase` classes, dataclasses/Pydantic for structured state, prompt constants kept uppercase near their consuming agent.
- Ignored/experiment paths (see `.gitignore`): `reaserch_agent/logs/`, `reaserch_agent/{e2e_test,B1_test,B2_test}/`, `chem_resources/{exp_logs,knowledge_base}/`, `campaigns/`, `.env`.
- **`chem-agent-latest/` is an untracked ~78 MB near-snapshot of the whole repo** (older; missing the newer ingestion files). It is not the working tree — edit the top-level packages, not this snapshot.
- **`backend/` and `frontend/` are leftover build/runtime artifacts** of a reverted web console (commit 6106c60) — untracked, no source files. Ignore them; there is no live web app in this repo.
- Commit messages: short, imperative, sentence-case (`Update research and device agents`), one scoped change per commit (see `AGENTS.md`).
- Design/eval references live in `chem-eval/` (`chem-technical-report.md` = full architecture/data-structure spec; `chem-agent-eval-plan.md` = eval tracks A–F + metrics) and `docs/` (`QUICKSTART.md` = Chinese quick-start with flow diagrams; ingestion survey + intervention plan; `代码审查.md` = running code-review log). The full research-layer state-machine spec is `reaserch_agent/research_layer.md`. `README.md` is the Chinese-language project overview.
```

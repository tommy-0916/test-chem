# Field basis: minimal semantic loop (2026-09-27)

This checkpoint corrects a verification category error in the real PDF proposal path. A paper must support chemical claims; it cannot be required to print Chem-Agent's internal `material_id` or `material_instance_id`. The production workflow still calls `propose_pdf_group_unreviewed(..., check_required_graph_facts=True)`.

## Field requirements

The following modes choose a proof method. They do not replace `ProvenanceV2`, `evidence_class`, source scope, or the existing publication gates. `device_sop` remains an evidence source class and can support the appropriate equipment contract; it is not a paper literal or a seventh mode.

| Verification mode | Field examples | Current rule |
| --- | --- | --- |
| `paper_literal` | precursor amount and unit | Same experimental group, literal excerpt, quantity and material attribution; no unsupported number is promoted. |
| `controlled_mapping` | material name, normalized operation or state | A name or term that appears literally can retain its paper fact. A proposed normalized name or alias absent from the excerpt remains `semantic_binding_pending` until an applicable mapping is supplied and verified. A completely missing fact remains a missing fact. |
| `convention` | implicit state or lineage from an operation | Existing Phase 1 convention rules and applicability checks; no numeric generation. No new convention was added here. |
| `derived` | calculated concentration or ratio | Needs source inputs, a formula and dimension checks. This patch adds no automatic derivation or paper-explicit promotion. |
| `generated_id` | `material_id`, instance IDs and references | Program assigns opaque, document and group scoped IDs; graph identity, same-step relation references and earlier-step parent output references are checked. Reusing one instance symbol to define batches in different sample arms is blocked. No paper fact is allowed for an internal ID. Its port name still needs source evidence. |
| `runtime` | measured yield or feedback addition | Needs a resolver and the required temporal dependency; an unresolved value still blocks. |

The compiler, literal receipt, local strict feedback and RouteDecision share the same classification for the paths handled here. The V2 material graph and science audit continue to check lineage, quantity coverage and provenance. A material port's paper provenance anchors its source backed name; a separate amount fact can have separate paper provenance, which the science audit checks against the same port and evidence bundle. The endpoint is selected by its verified output name and state, never by equality between a task target and an internal ID. A required nested `quantity_requirements[].material_id` is checked against its step's port and name receipt. Other nested relation IDs used as goal fields currently stop with `required_generated_id_structure_unsupported`, never a request for a paper quote.

## Frozen A01 source fragment

The regression fixture records the NiFe control experimental group from the locally acquired paper (`sha256_2e9435bccb2e2e281edc0fcbd0ff27ffe48b8c0babcc5e346fbdc0d679df12c8`), blocks `pdf:p2:b58`–`pdf:p2:b59`. It checks `Ni(NO3)2·6H2O`, `37.5 mmol`, a generated internal ID, and the reference from a later Solution A port. The same sentence contains `12.5 mmol` for the Fe precursor; binding that amount to Ni remains blocked. Typed field evidence, the material port and evidence excerpts survive JSON reconstruction with separate document and excerpt digests. This is an unreviewed precursor fragment, not a publishable A01 route or a chemistry review.

## Production strict saved-input replay

The saved A01 proposal was replayed without a model against the eight PDF groups with `check_required_graph_facts=True` and no local repair attempts. The detailed local artifact is `result/semantic-field-baseline-20260927/field-basis-diagnostics.json`.

| Requirement class | Paths | Missing paper fact |
| --- | ---: | ---: |
| `paper_literal` | 19 | 0 |
| `controlled_mapping` | 184 | 0 |
| `generated_id` | 69 | Not applicable; 0 falsely claimed as paper facts |

The replay still produces **0 unreviewed protocols admitted from the batch**. It reports 254 diagnostics: 188 `fact_unit_invalid`, 54 `semantic_binding_pending`, four `fact_quantity_not_in_excerpt`, four `fact_graph_path_missing`, three `fact_quantity_attribution_unresolved`, and one `fact_value_not_in_excerpt`. The previous internal-ID missing-paper-fact signature is gone; the old proposal's invalid units and unresolved semantic mappings still block admission. No complete A01 run or Device dispatch was started.

## Frozen regression (2026-09-28)

The focused checks cover a real A01 Ni precursor, synthetic compiler-to-science association with distinct name and quantity facts, generated ID graph closure, dangling relation and parent references, wrong-material quantity, pending normalized state, Device SOP evidence class, exact endpoint name, and JSON reconstruction. The real A01 fragment was checked through the production strict proposal entry and typed material-port/evidence JSON reconstruction. Compiler-to-science and Research-to-Device save/handoff tests use synthetic evidence; this checkpoint does not establish a complete real-A01 publishable package.

The same `.venv` ran the full suites with source and fixtures frozen throughout. The tracked diff hash was `ad4ff897f21811f9d95e8791129ea34f9bc1387f` before and after the runs. `git diff --check` passed. Local logs are `result/semantic-field-baseline-20260927/{contracts-suite-final.txt,research-suite-final-frozen.txt,device-suite-final-frozen.txt,device-suite-final-frozen.xml}`.

| Suite | Frozen result | Failure-fingerprint comparison |
| --- | --- | --- |
| Contracts `unittest discover` | 113/113 pass | No failures. |
| Research `unittest discover` | 757 tests; 8 failures, 21 errors | 29/29 match `docs/test_failure_fingerprint_20260927.json` `fingerprints.research.current`. |
| Device `pytest device_agent -q --tb=short` | 985 tests; 935 pass, 50 fail | 50/50 match `fingerprints.device.current`. |

The comparison matched test ID, failure kind, exception type, normalized reason, and repository stack-origin path/function for every existing failure; there were zero new, removed, or changed signatures. Line-number shifts and temporary directory names were excluded from semantic comparison. The existing failures remain baseline debt, not suite passes. No full A01 v5 or device dispatch was run.

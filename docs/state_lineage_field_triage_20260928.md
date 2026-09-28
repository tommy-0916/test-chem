# Real NiFe control field triage (2026-09-28)

This is a read-only classification of the 23 required-field issues from the new, locally revised NiFe control proposal at `46b0c9d`. It does not change source facts, approve the route, or report a successful replay. The input is the ignored `result/semantic-unit-mapping-20260928/new-single-group-replay-final-attribution.json` together with its saved model revision and PDF group blocks. The source group is `Synthesis of the Pristine Ni3Fe LDHs (NiFe Control).`, digest `sha256_2e9435bccb2e2e281edc0fcbd0ff27ffe48b8c0babcc5e346fbdc0d679df12c8`.

Each row is unique by experimental group, field path and reason code. Source spans below were recomputed from the saved same-group excerpt and PDF blocks with `bind_pdf_quote` (`route_pdf_quote_binding/v1`), rather than taken from the model's starting-block hint. All 23 excerpts matched one span. There are **21 `semantic_binding_pending`** state fields and **2 `fact_quantity_attribution_unresolved`** output quantity fields.

| Category | Fields | Interpretation |
| --- | ---: | --- |
| Unsupported state claim | 10 | The proposal uses an operation, material name or sample role as a state. The cited text supports the term, not that state assertion. |
| Upstream state or existing convention candidate | 7 | An upstream state may be reusable only after formal instance lineage and rule applicability are established. |
| Missing premise | 4 | A state interpretation needs entity attribution, phase/retention intent or a valid state transition that the present record does not prove. |
| Wrong quantity role | 2 | The cited number is a concentration or split count, not a material output amount. |

## Unique fields

`S` = unsupported state claim; `U` = upstream state / existing convention candidate; `P` = missing premise; `Q` = wrong quantity role. The table keeps the exact field path and computed source span; the short terms are diagnostic labels, not replacement source quotations.

| Fact | Field path | Class | Source span | Finding |
| --- | --- | --- | --- | --- |
| `m1_state` | `material_graph[0].material_inputs[0].state` | S | `pdf:p2:b58-p2:b59` | “mixed and dissolved” is the Ni precursor operation, not its input state. |
| `m2_state` | `material_graph[0].material_inputs[1].state` | S | `pdf:p2:b58-p2:b59` | The same operation is used as the Fe precursor input state. |
| `m3_state` | `material_graph[0].material_inputs[2].state` | S | `pdf:p2:b58-p2:b60` | “water” identifies the material; it is not a controlled state. |
| `m5_state` | `material_graph[1].material_inputs[0].state` | S | `pdf:p2:b60-p2:b60` | “dissolving” describes NaOH preparation, not the incoming NaOH state. |
| `m6_state` | `material_graph[1].material_inputs[1].state` | S | `pdf:p2:b60-p2:b61` | Water identity was placed in the state field. |
| `m7_qty` | `material_graph[1].material_outputs[0].quantity.value` | Q | `pdf:p2:b60-p2:b61` | `1 M` describes Solution B concentration, not its output amount. |
| `m8_state` | `material_graph[2].material_inputs[0].state` | S | `pdf:p2:b61-p2:b62` | “dissolving” describes Na₂CO₃ preparation, not its input state. |
| `m9_state` | `material_graph[2].material_inputs[1].state` | S | `pdf:p2:b61-p2:b62` | Water identity was placed in the state field. |
| `m11_state` | `material_graph[3].material_inputs[0].state` | U | `pdf:p2:b62-p2:b63` | This is named solution A from step 0; inheritance needs a parent output instance reference. |
| `m12_state` | `material_graph[3].material_inputs[1].state` | U | `pdf:p2:b62-p2:b63` | This is named Solution C from step 2; inheritance needs a parent output instance reference. |
| `m13_state` | `material_graph[3].material_outputs[0].state` | P | `pdf:p2:b64-p2:b65` | The pH sentence says “solution” but does not prove the proposed reaction output is that entity or phase. |
| `m14_state` | `material_graph[4].material_inputs[0].state` | U | `pdf:p2:b64-p2:b65` | It reuses step 3's reaction identity; inheritance depends on resolving `m13_state` and linking instances. |
| `m15_state` | `material_graph[4].material_outputs[0].state` | P | `pdf:p2:b64-p2:b65` | Stirring creates a new output instance in the graph, with no proved state-preserving relation. |
| `m22_state` | `material_graph[7].material_outputs[0].state` | U | `pdf:p2:b69-p2:b69` | “divided into 8 parts” is a split operation. `SPLIT_V1` could retain a proved suspension state only with a valid split relation and applicable operation mapping. |
| `m22_qty` | `material_graph[7].material_outputs[0].quantity.value` | Q | `pdf:p2:b68-p2:b70` | `8 parts` counts split portions; it is not the physical amount of the single output port. |
| `m23_state` | `material_graph[8].material_inputs[0].state` | U | `pdf:p2:b69-p2:b70` | It follows step 7's proposed split output; the parent instance and corrected output state are prerequisites. |
| `m24_state` | `material_graph[8].material_inputs[1].state` | S | `pdf:p2:b70-p2:b71` | Deionized water is a material identity, not a controlled state. |
| `m25_state` | `material_graph[8].material_outputs[0].state` | P | `pdf:p2:b71-p2:b72` | The paper names precipitates as LDH seeds; a retained wet-solid state still needs a supported retained-phase interpretation. |
| `m26_state` | `material_graph[9].material_inputs[0].state` | U | `pdf:p2:b71-p2:b72` | LDH seeds reuse step 8's identity; inheritance depends on `m25_state` and a parent instance reference. |
| `m27_state` | `material_graph[9].material_inputs[1].state` | S | `pdf:p2:b72-p2:b73` | Water identity was placed in the state field. |
| `m28_state` | `material_graph[9].material_outputs[0].state` | P | `pdf:p2:b71-p2:b73` | “dispersed” states an operation, not a controlled output phase; a redispersion transition is not proved. |
| `m29_state` | `material_graph[10].material_inputs[0].state` | U | `pdf:p2:b71-p2:b73` | It reuses step 9's LDH seeds identity; inheritance depends on `m28_state` and a parent instance reference. |
| `m30_state` | `material_graph[10].material_outputs[0].state` | S | `pdf:p2:b75-p2:b76` | “samples” is an entity role, not the final material state after the second centrifugation/redispersion. |

## Quantity roles and current blockers

- `m7_qty`: The source explicitly gives Solution B as `1 M`. `MaterialPortV2` has `concentration_value` and `concentration_unit`; the fact belongs to concentration if that path is correctly represented and verified. It does not establish an output batch amount.
- `m22_qty`: The source describes around `200 mL` of suspension divided into `8 parts`; `8` is a split count. It gives no verified equal per-part volume. The nearby approximate mass must not be converted into a definite yield or allocation.
- The revised proposal contains `material_id` symbols but no `material_instance_id`, `material_origin`, `parent_output_refs` or `material_relations`. Matching `material_id` values can suggest identity; they do not by themselves prove the required batch lineage.
- `SPLIT_V1` preserves an established input state and never generates quantities, but the current operation pattern does not match the English “divided into 8 parts”, and the proposal has one output port rather than eight distinct child instances. The present record does not satisfy a verified split proof.
- `CENTRIFUGE_COLLECT_PRECIPITATE_V1` requires retained-precipitate intent and an allowed input state. The proposed centrifugation/redispersion operation does not express that intent in the current rule matcher, so the rule cannot automatically certify `m25_state`.
- No field here has been established as a purely wrong quote that can be safely fixed by replacing its excerpt alone. Independent chemistry review, Research publication, Device preflight and a complete local A01 run remain outside this audit.

The detailed untracked audit record, including the saved fact value and full excerpt per field, is `result/semantic-unit-mapping-20260928/remaining-23-field-triage.json`.

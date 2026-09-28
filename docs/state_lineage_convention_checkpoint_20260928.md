# State and lineage proof checkpoint (2026-09-28)

Baseline: `46b0c9d`. This slice corrects the source role of a state claim and connects existing `TRANSFER_V1` / `SPLIT_V1` conventions to field-level route evidence. It does not approve a route, create a Research publication, or dispatch equipment.

## The 23 real-field issues

The deduplicated [field triage](state_lineage_field_triage_20260928.md) covers the revised NiFe Control proposal: 21 `semantic_binding_pending` states and two output quantity attribution errors. Ten state claims use an operation, material name, or sample role as a state; seven are possible upstream-state or convention candidates without a formal relation; four lack a necessary premise. `1 M` is Solution B concentration, and `8 parts` is a split count. Neither is an output material amount. No field was shown to be fixable solely by replacing a quotation.

## Implemented proof and boundaries

- An inherited output state is accepted only when the same experimental group provides a separately bound parent-state fact and an affirmative operation fact, and the typed graph has an exact parent instance, distinct child instances, operation segment, material relation, and lineage relation. The existing rule must match the operation, input state, and intent. `SPLIT_V1` requires at least two children and an explicit count equal to the declared children. It never allocates a per-child quantity.
- The child-state field remains `agent_inferred` with `evidence_class=chemistry_convention`, rule ID/version, source scope, and resource digest. It is not `paper_explicit`. The production diagnostics, PDF literal receipt, compiler, text/PDF SourceVerifier, scientific audit, RouteDecision, and V2 reconstruction share the checks needed at their respective boundaries. V2 verifies a proof that is present against its graph, evidence excerpt, and current rule bytes; the original PDF is verified upstream.
- A state word describing the material **before** a split or transfer cannot itself prove the state of an output child. This remains true if the proposal declares the wrong input material. A separately stated child state can still pass the literal path. The role check is scoped to the output's own split/transfer step so an earlier reaction output is not rejected merely because a longer excerpt mentions a later split.
- `SPLIT_V1` is version `1.1.0` and recognizes the bounded English phrase `divided into` with word boundaries. An affirmative material subject/object is required; negated split/transfer and a vessel or other object as subject do not generate a state proof. Changing the versioned rule resource changes its digest, so earlier proof snapshots must be re-evaluated rather than silently reused.
- Output concentration units such as `M`/`mM` and count units such as `parts` cannot be treated as output material quantities. The check preserves the distinction from physical `m`/`mm`. A valid quantity still needs its own material-bound evidence or genuine runtime resolution path.

## Real source check and current production result

The locally attested PDF has source digest `sha256_2e9435bccb2e2e281edc0fcbd0ff27ffe48b8c0babcc5e346fbdc0d679df12c8`. In the NiFe Control group, block `pdf:p2:b69-p2:b69` states that the suspension was divided into eight parts. A **separate, manually structured, unsigned diagnostic variant** declares one parent and eight distinct children, without assigning their quantities. It passes compiler and PDF SourceVerifier with the parent state and operation source-verified; scientific audit recognizes all eight output `.state` fields under `chemistry_convention`. It creates zero output quantities. The overall step remains blocked by material-contract status and amount requirements. This demonstrates the real-source field proof, **not** autonomous production of that graph or review of the chemistry. The diagnostic script and report are ignored local artifacts at `result/state-lineage-20260928/b69-state-proof-demo.py` and `.json`.

The saved eight-group raw proposal replays unchanged with zero admitted protocols. The previously revised single-group raw proposal also replays unchanged: 21 state fields remain pending and two output quantities report `fact_quantity_role_mismatch`. Under the frozen code, one new model generation for the real NiFe Control group produced nine steps and 106 proposed facts, but zero admitted protocols. Its diagnostics include one excerpt span error; the field assessment further reports eight parent-state-as-child-evidence errors, 26 semantic pending paths, 33 missing required graph facts, one quantity attribution error, and one material-ID identity conflict. These are diagnostic counts, not a claim of distinct scientific defects. It proposed eight split child ports with the same material ID as the parent, but no lineage relation; its segment effect and quantity-basis representation did not satisfy the typed contract. The model's raw output was not edited to pass. Local artifacts are in `result/state-lineage-20260928/` as `saved-replay-frozen.json`, `revised-real-proposal-replay-frozen.json`, `new-real-proposal-frozen.json`, and `fresh-summary-frozen.json`.

## Frozen verification

| Check | Result |
| --- | --- |
| Focused route/convention suites | 158/158 pass |
| Contracts `unittest discover` | 116/116 pass |
| Research `unittest discover` | 785 run; 8 failures, 21 errors; all 29 existing failure fingerprints match baseline |
| Device `pytest device_agent -q --tb=short` | 935 pass, 50 fail; all 50 existing failure fingerprints match baseline |
| `git diff --check` | Pass |

The failure comparison uses test ID, exception type, normalized reason, and repository stack path/function rather than counts alone. Logs, JUnit XML, and `failure-fingerprint-frozen.json` are retained under ignored `result/state-lineage-20260928/`.

Independent chemistry review, complete A01 sample-arm coverage, Research publication, Device preflight, and 303 execution remain unverified. The complete local A01 forward run was not repeated because the fresh proposal is not yet a valid independent-review input; the existing authorization to run it once the input reaches that boundary remains in force. V2's proof list is optional for historical compatibility: standalone unsigned V2 reconstruction checks included proofs but cannot detect deletion of the entire list with a recomputed package hash. Publication must retain and verify the upstream route/source receipt.

The next engineering task is to make the production proposal provide a contract-valid parent reference, operation segment, lineage relation, and quantity role from the actual source, while preserving the current field-level rejection of unsupported states and amounts. Do not use the hand-built diagnostic graph as a model answer or relax independent review to advance A01.

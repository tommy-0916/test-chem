# Unit representation and controlled state mapping (2026-09-28)

This checkpoint starts from `738ebed`. It changes the production path for unsigned, source-scoped PDF group proposals. It does not approve a chemistry route, change route selection, sign independent review, or dispatch Device tasks.

## Production field handling

- A missing or `null` unit on a textual fact for an existing required graph or route-signature path becomes the contract's empty-string representation. The original model response is retained, and the detached proposal records each conversion. Numeric physical quantities still require a source-supported unit; an object or array is not stringified into a unit.
- An omitted `required` flag becomes `true` only when the fact targets an existing required graph or signature path. An explicit `false` and an unknown path remain invalid. This conversion is recorded and cannot manufacture a missing fact, excerpt, value, or attribution.
- Explicitly recognized pH, repetition count, and ratio parameter paths with no unit report `dimensionless_semantic_pending`. They do not become `paper_explicit` because the current path has no complete labeled-literal verifier for them. Material quantity, concentration and temperature fields with missing physical units keep their blocking errors.
- Material-port state words can use the versioned `material-states/v1` lexical alias resource only when the proposed source expression itself occurs uniquely in the same-group excerpt and is locally associated with that port's literal name. The same attribution test is applied by unsigned production, the formal literal receipt, compiler, scientific audit and RouteDecision, including a state word already in canonical spelling. No absent source word is replaced with another material's word. Contextual phase selection such as `precipitates` to a particular wet or dry state remains pending. Adjacent Chinese prose that the exact word-boundary check cannot prove also remains pending.
- A verified state mapping is stored with the field-level candidate evidence, recomputed by route validation and scientific audit, and projected into an optional hash-bound V2 route-binding snapshot. The snapshot includes field path, evidence ID, source/target terms, rule ID/version and resource digest. Saved state and Device canonical handoff retain it. Historical V2 hashes with no snapshot remain unchanged. The snapshot does not replace independent source or chemical review.
- V2 reconstruction also checks each claimed rule ID and resource digest against the versioned mapping resource. Recomputing a package hash cannot make a forged mapping rule valid.
- The Research Codex CLI transport now explicitly uses UTF-8 for subprocess input/output, allowing Chinese PDF prompts on Windows without a locale-dependent encoding failure.

## Real PDF replay and new proposal

The source is the attested NiFe control experimental group from the locally acquired PDF, source digest `sha256_2e9435bccb2e2e281edc0fcbd0ff27ffe48b8c0babcc5e346fbdc0d679df12c8`. The saved eight-group proposal was replayed without a model under `check_required_graph_facts=True`: 184 valid textual null-unit representations and two narrow `Solution` state normalizations were recorded; the original raw proposal was unchanged. The strict batch still admitted zero protocols. Remaining diagnoses were four invalid facts on non-graph `target` paths, 55 pending semantic bindings, four quantities absent from excerpts, four missing graph paths, three quantity-attribution failures and one value absent from its excerpt. Three additional pending reports reflect the state-to-material attribution check. These are diagnostic counts, not distinct-field counts.

One new model generation was run for the real NiFe control group using the repository's Codex account transport and a bounded local revision. The model supplied 94 facts for all 94 required graph paths but omitted the `required` flag; the unsigned producer recorded 94 flag conversions. Under the final attribution rule, 71 facts passed literal assessment. Twenty-one state claims remain semantically pending, including proposed facts that use process or material expressions such as `mixed and dissolved`, `water`, `divided into 8 parts`, `precipitates` or `samples` as support for a machine state. Two output quantities still lack unambiguous material attribution. The one local revision did not remove these 23 blockers. No protocol entered the independent-review or route-admission path; neither Research publish nor Device preflight was reached.

The ignored local work products are under `result/semantic-unit-mapping-20260928/`: `saved-replay-final-attribution.json`, `new-single-group-proposal-timeout600.json`, `new-single-group-local-revision.json`, and `new-single-group-replay-final-attribution.json`. They contain source excerpts and raw model output and are intentionally not committed.

## Verification boundary

Focused tests cover text and physical units, dimensionless pending, wrong-material amount binding, same-sentence cross-material state terms, controlled mapping tampering, V2 save/reload and Device handoff, old-hash compatibility, and UTF-8 model transport. The new real proposal verifies production input handling but does not prove a complete real A01 route. V2 mapping roundtrip tests use reviewed synthetic candidate evidence.

The full A01 forward-only v5 run remains gated on a new real proposal becoming a valid independent-review input. The actual new proposal still has 23 required-field issues; a full run now would repeat that earlier blocker. No real 303 task was created.

| Suite | Frozen result | Baseline comparison |
| --- | --- | --- |
| Contracts `unittest discover` | 116/116 pass after V2 rule validation | No failures. |
| Research `unittest discover` | 770 tests; 8 failures, 21 errors after V2 rule validation | 29/29 failure fingerprints match `docs/test_failure_fingerprint_20260927.json`. |
| Device `pytest device_agent -q --tb=short` | 935 passed, 50 failed after V2 rule validation | 50/50 failure fingerprints match `docs/test_failure_fingerprint_20260927.json`. |

The existing Research failures remain baseline debt; they are not suite passes. Failure fingerprints compare test ID, exception type, normalized reason, and repository stack-origin path/function, ignoring temporary directory names and line shifts.
The frozen-run logs and post-fix fingerprint audit are retained locally under ignored `result/semantic-unit-mapping-20260928/` as `contracts-postfix.log`, `research-postfix.log`, `device-postfix.log`, `device-postfix.xml` and `failure-fingerprint-postfix-audit.json`.

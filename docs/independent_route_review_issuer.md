# Independent PDF group review issuance

`reaserch_agent.route_review_issuer` is a deployment-side producer for the
existing route trust configuration. It consumes the already signed source
event, reopens and hashes the original PDF, enumerates exact Methods group
scopes, and issues a work order for an independent chemical reviewer. It does
not infer group roles, capabilities, or route signatures. The planner's
proposal is never an authority for these fields.

The source identity key and chemical review key must have distinct issuers and
public key bytes. The chemical review private key and decision file must be
outside both the repository and KB. The route trust configuration may be in
an ignored deployment workspace, but must be outside the KB. Keep all private
keys out of logs and Git.

## Workflow

1. Prepare a signed source identity with `route_receipt_producer`. Save the
   `route-trust-config/v1` file. This authenticates document identity only.
2. List the PDF sections available for review:

   ```powershell
   python -m reaserch_agent.route_review_issuer list-groups `
     --kb-root C:\deployment\kb `
     --route-trust-config C:\deployment\route-trust-config.json `
     --inventory-out C:\deployment\group-review-inventory.json
   ```

   The inventory records exact `paper_id`, `experimental_group_id`,
   `source_digest`, section, locator and parser diagnostics. A Methods
   subsection is not automatically a synthesis route.
3. Take the target from the current trusted task goal (`RouteGoalV1`) and
   produce a work order for one exact group:

   ```powershell
   python -m reaserch_agent.route_review_issuer prepare-work-order `
     --kb-root C:\deployment\kb `
     --route-trust-config C:\deployment\route-trust-config.json `
     --goal-json C:\deployment\current-goal.json `
     --paper-id "paper-id-from-inventory" `
     --experimental-group-id "exact heading from inventory" `
     --source-digest "sha256_<64 lowercase hex>" `
     --work-order-out C:\deployment\review-work-order.json
   ```

   The work order includes the source attestation digest, current target,
   exact group locator and PDF text blocks. It says review is pending.
4. An independent human reviewer examines the original PDF and any SI,
   checks the group boundary, route interpretation, parameter isolation and
   capability completeness, then writes a separate decision JSON **outside
   the repository and KB**. The decision must have exactly these fields:

   ```json
   {
     "schema_version": "independent_group_review_decision_v1",
     "work_order_digest": "sha256_<digest of the exact work order>",
     "reviewer": "Reviewer identity",
     "review_method": "independent_human_pdf_review",
     "review_basis": "Specific original PDF/SI checks performed",
     "group_role": "synthesis",
     "required_capabilities": ["reviewed_capability_id"],
     "route_signature": {
       "route_family": "reviewed family",
       "target_transformation": "reviewed transformation",
       "precursor_roles": [],
       "reagent_roles": [],
       "operations": ["reviewed operation"],
       "control_modes": [],
       "phase_transitions": [],
       "endpoint_state": "reviewed controlled state"
     },
     "chemical_review_completed": true,
     "execution_authorized": false
   }
   ```

   The shown signature values are placeholders, not a chemistry recipe. A
   `synthesis` or `material_processing` decision requires a complete
   `RouteSignatureV1` and a nonempty complete capability list. A group found
   to be nonroute uses `characterization`, `testing`,
   `performance_testing`, or `non_procedural`, with empty capabilities and
   `route_signature: null`. Classification of a nonroute group is still an
   explicit reviewer decision, never a silent skip.
5. The deployment issuer signs and installs the reviewed declaration:

   ```powershell
   python -m reaserch_agent.route_review_issuer issue-review `
     --kb-root C:\deployment\kb `
     --route-trust-config C:\deployment\route-trust-config.json `
     --goal-json C:\deployment\current-goal.json `
     --work-order C:\deployment\review-work-order.json `
     --decision C:\independent-review\decision.json `
     --private-key C:\independent-review\review-ed25519.pem `
     --issuer chemical-review-service `
     --key-id review-key-2026 `
     --receipt-out C:\deployment\issued-review-receipt.json
   ```

The issuer re-enumerates the signed PDF and rejects changed bytes, changed
scope, target, work order, decision file, incomplete capabilities, duplicate
group decisions, and source/review key reuse. It signs the full route
signature with the existing domain-separated Ed25519 format, runs the existing
strict verifier, and checks the complete resulting trust config with the
Research CLI loader before replacing it. The receipt records decision and
work-order digests and explicitly states `execution_authorized: false`.

Digital signatures authenticate declarations by configured issuers; they do
not prove that the reviewer interpreted chemistry correctly. This issuer
currently supports an independently recorded human PDF review. Automatic
source identity and literal-field receipts are produced separately and never
claim human chemical review. Missing independent review remains a named
engineering/review dependency, not a request to invent or auto-approve one.

# Source and field receipt production

`reaserch_agent.route_receipt_producer` is a deployment-side CLI. Run it outside
the planner process with an Ed25519 source issuer key stored outside the
repository and knowledge base. The key is authorized only for **source
identity**, not route chemistry or hardware execution. The route trust config
holds the public key and signed event. It remains outside the knowledge base.

## Remote PDF with independently checkable identity

The `prepare-source` command performs one complete source acquisition. It
fetches DOI metadata or a DOI-resolved publisher page, downloads the linked
PDF, checks the original bytes, writes a controlled `_pdf_sources` PDF and
`SourceDocumentAttestationV1`, rechecks the remote proof, signs the existing
`TrustedAcquisitionEventV1`, and appends it to a v1 route trust config.

Two automatic methods are supported:

- `doi_resolver_publisher_link_exact_pdf_bytes_v1`: the DOI resolver's HTTPS
  publisher page contains the DOI and an exact link to the PDF URL; the PDF
  contains the DOI on its first two pages.
- `configured_source_host_crossref_pdf_match_v1`: a deployment-configured
  HTTPS PDF host supplies the bytes; the PDF contains the DOI, and Crossref's
  title and first-author family name match a prominent article header on the
  first PDF page. An ambiguous header stays pending. The attestation records
  `source_host_doi_metadata_match`, which does not claim a publisher version.

For example, the second method can verify the publicly accessible Zhao 2025
PDF (DOI `10.3390/ma18040911`) using host `mdpi-res.com`:

```powershell
.\.venv\Scripts\python.exe -m reaserch_agent.route_receipt_producer prepare-source `
  --kb-root C:\path\to\knowledge-base `
  --doi 10.3390/ma18040911 --title "Zhao 2025" `
  --campaign-id a01_v5 --document-kind primary_paper `
  --evidence-url https://api.crossref.org/works/10.3390%2Fma18040911 `
  --pdf-url "https://mdpi-res.com/d_attachment/materials/materials-18-00911/article_deploy/materials-18-00911-v2.pdf?version=1740032574" `
  --trusted-source-host mdpi-res.com `
  --issuer automated-source-verifier `
  --receipt-out C:\path\outside-kb\source-receipt.json `
  --private-key C:\path\outside-repo-and-kb\source-key.pem `
  --key-id auto-source-v1 `
  --trust-config-out C:\path\outside-kb\route-trust.json
```

The output config intentionally has empty route review, group-role, and
capability maps. A source identity signature is insufficient to select a
chemical route. The separate `acquire-source` and `sign-source` commands are
available when acquisition and signing run in different processes.

## PDF group proposal and review work orders

During Research, a signed PDF is first enumerated into Methods subsections.
The isolated extractor now records one **unreviewed** proposal for each
subsection before asking for trusted role and capability maps. All such
proposals carry `group_role=unclassified`; a model's `role_hint` is advisory.
`route_group_fact_receipt` checks their group identity, block locators, exact
excerpts, and literal values against the signed inventory and writes an
unsigned work order for every subsection. The saved Research state contains
`route_unreviewed_group_proposals_v1`, `route_group_fact_receipts_v1`, and
specific proposal diagnostics. A subsection with no route facts is recorded as
pending role classification, not as a failed literal check.

The trust config accepts the explicit `non_procedural` role for source context
such as a materials list. That role must be assigned independently to the
exact PDF digest and subsection; the parser or planning model cannot silently
exclude it. Route groups still require a complete, independently checked
capability list and a signed route-signature review before RouteDecision may
select them. The work order is an input to that separate review process; it
does not claim a human chemical review or authorize execution.

## Existing local PDF

`crosscheck-local` compares an existing PDF's DOI, title, and first author
with Crossref metadata and records its byte hash. A supplied historical
acquisition URL is labeled as historical and is **not** fetched or treated as
proof that the local bytes came from it.

```powershell
.\.venv\Scripts\python.exe -m reaserch_agent.route_receipt_producer crosscheck-local `
  --doi 10.1021/acsami.3c11651 `
  --local-pdf C:\path\to\huang.pdf `
  --historical-acquisition-url https://escholarship.org/content/qt64g5s3gx/qt64g5s3gx.pdf `
  --receipt-out C:\path\to\huang-crosscheck.json
```

Even when metadata matches, this receipt remains
`identity_review_pending/remote_acquisition_proof_missing`. It never creates
an acquisition event or a route trust config. This permits offline extraction
diagnosis without representing the local file as a newly verified download.

## Literal field checks and chemical review request

`audit-fields` takes a typed `RouteCandidateV1` JSON and a signed source trust
config. It reopens the registered PDF, checks the exact group and field
locators, quoted excerpts, literal values and units, then writes a
`literal_pdf_field_receipt_v1` with verification method, checked field paths,
reason codes, and an `independent_route_review_request_v1` for that exact
PDF/group/target. The request includes the **proposed** route signature but
is unsigned. Chemical equivalence, route roles, adaptations, and capability
requirements remain independent review work. The existing route signature
verification and Research publish gate are unchanged.

```powershell
.\.venv\Scripts\python.exe -m reaserch_agent.route_receipt_producer audit-fields `
  --kb-root C:\path\to\knowledge-base `
  --candidate-json C:\path\to\candidate.json `
  --route-trust-config C:\path\outside-kb\route-trust.json `
  --receipt-out C:\path\outside-kb\literal-field-receipt.json
```

All automated receipts state their method and scope. None claims a human
chemical review or execution authorization. If remote identity evidence is
unavailable, source acquisition reports `identity_review_pending` with an
explicit reason and creates no signed source event.

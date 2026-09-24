# A02 curated local research records

These five JSON files are **curated excerpts**, not the original full-text
articles and not a complete executable protocol. Use this directory explicitly
with Research's `--knowledge-base-dir` when reproducing the local-only A02
planning input. The files are tracked so a clean Git checkout has the same
retrieval corpus; generated Research states and Device checkpoints remain
excluded from version control.

| Record(s) | Original source and attribution | License |
| --- | --- | --- |
| `a02_primary_2018_nife_ldh_synthesis.json` | Sonia Jaśkaniec et al., “Low-temperature synthesis and investigation into the formation mechanism of high quality Ni-Fe layered double hydroxides hexagonal platelets,” *Scientific Reports* 8, 4179 (2018). [DOI 10.1038/s41598-018-22630-0](https://doi.org/10.1038/s41598-018-22630-0); [open full text](https://pmc.ncbi.nlm.nih.gov/articles/PMC5843585/). | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| `a02_primary_2023_nife_ldh_{activation,electrochem,electrodes,stability}.json` | Daire Tyndall et al., “Demonstrating the source of inherent instability in NiFe LDH-based OER electrocatalysts,” *Journal of Materials Chemistry A* 11, 4067–4077 (2023). [DOI 10.1039/D2TA07261K](https://doi.org/10.1039/D2TA07261K); [open full text](https://pmc.ncbi.nlm.nih.gov/articles/PMC9942694/). | [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/) |

The source URLs and section names are also retained in each record's
`_ingestion_metadata`. This metadata documents attribution; merely carrying a
DOI does **not** promote the runtime `verification_status` from `local_file`
to `verified_doi` or prove that every paraphrase is supported. Several
`描述性总结` and `evidence` strings reproduce source wording; problem descriptions
and the selection of excerpts are our adaptations. Source text was checked
against the open full texts on 2026-09-24.

Important limits: the 2018 synthesis uses 80 mL precursor solution in a
100 mL round-bottom reflux flask and says to wash with water “several times”
without a dose per wash. The 2023 electrode record describes spray coating
with a specialized tool and names characterization-specific substrates, but
does not give a target catalyst loading. The activation and stability excerpts
do not specify all amounts, handling transitions, or equipment needed for an
automated 303 workflow. Combining the papers is a cross-paper adaptation,
not a single validated procedure. Missing quantities and capabilities must
stay unresolved; these records do not authorize real laboratory dispatch.

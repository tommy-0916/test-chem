from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from reaserch_agent.tools.ingestion import ExternalKnowledgeClient


QUERIES = {
    "A01": [
        "NiFe catalyst iron coordination environment alkaline oxygen evolution reaction",
        "iron incorporation mode NiFe oxyhydroxide OER operando spectroscopy reconstruction",
        "NiFe electrocatalyst Fe active site coordination OER mechanism",
    ],
    "A02": [
        "NiFe layered double hydroxide surface reconstruction alkaline oxygen evolution operando",
        "NiFe LDH electrochemical activation time OER phase transformation NiOOH",
        "NiFe LDH before after OER XPS Raman reconstruction stability",
    ],
    "B01": [
        "NiMo catalyst molybdenum oxidation state alkaline hydrogen evolution water dissociation",
        "Ni Mo interface electronic structure alkaline HER Mo valence",
        "NiMo oxide reduction treatment alkaline hydrogen evolution kinetics",
    ],
    "B02": [
        "cobalt molybdenum sulfide electrocatalyst alkaline hydrogen evolution",
        "amorphous cobalt molybdenum sulfide water splitting electrocatalyst",
        "CoMo oxide sulfide phase transformation hydrogen evolution active sites",
    ],
    "C01": [
        "nickel catalyst NiOOH active species alkaline urea oxidation reaction",
        "Ni hydroxide NiO NiFe NiCo urea electrooxidation mechanism NiOOH",
        "operando spectroscopy nickel oxyhydroxide urea oxidation reaction",
    ],
    "C02": [
        "iron doped nickel catalyst urea oxidation oxygen evolution selectivity",
        "NiFe electrocatalyst UOR OER competition alkaline electrolyte",
        "Fe content Ni catalyst urea electrooxidation mechanism selectivity",
    ],
    "D01": [
        "cobalt hydroxide ethanol electrooxidation alkaline",
        "cobalt oxide ethanol electrooxidation electrocatalyst",
        "non noble cobalt catalyst ethanol oxidation alkaline",
    ],
    "D02": [
        "NiCo layered double hydroxide ethanol oxidation reaction",
        "nickel cobalt hydroxide ethanol electrooxidation alkaline",
        "NiCo bimetallic catalyst alkaline ethanol oxidation synergy",
    ],
}


def paper_dict(paper) -> dict:
    return {
        "title": paper.title,
        "abstract": paper.abstract,
        "source": paper.source,
        "source_id": paper.source_id,
        "url": paper.url,
        "doi": paper.doi,
        "year": paper.year,
        "authors": paper.authors,
        "venue": paper.venue,
        "citation_count": paper.citation_count,
    }


def search_case(case: str) -> dict:
    client = ExternalKnowledgeClient(timeout_seconds=45)
    papers = []
    attempts = []
    seen = set()
    for query in QUERIES[case]:
        batch = client.search(
            query,
            sources=("semantic_scholar", "crossref", "openalex", "arxiv"),
            max_results=8,
        )
        attempts.append({
            "query": query,
            "providers": list(client.last_attempts),
            "errors": list(client.last_errors),
        })
        for paper in batch:
            key = (paper.doi or paper.title).strip().lower()
            if key and key not in seen:
                seen.add(key)
                papers.append(paper_dict(paper))
    return {"case": case, "queries": QUERIES[case], "attempts": attempts, "candidates": papers}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output or Path("result") / f"literature-only-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(search_case, case): case for case in QUERIES}
        for future in as_completed(futures):
            case = futures[future]
            results[case] = future.result()
            (output / f"{case}.json").write_text(
                json.dumps(results[case], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(case, len(results[case]["candidates"]), flush=True)
    manifest = {"created_at": stamp, "mode": "literature_only", "cases": results}
    (output / "raw_results.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(output.resolve())


if __name__ == "__main__":
    main()

"""Stricter accuracy/noise audit for the minimal paper knowledge-base MVP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .mvp import DEFAULT_OUTPUT_DIR, KnowledgeBaseBuilder, KnowledgeBaseStore


QUERY_CASES = [
    {
        "query": "ultralong wide-temperature sodium-ion batteries rational design Prussian blue analogue",
        "expected_title": "Rational Design of Prussian Blue Analogues for Ultralong and Wide-Temperature-Range Sodium-Ion Batteries",
        "evidence_terms": ["wide-temperature", "sodium-ion batteries", "prussian blue analogues"],
    },
    {
        "query": "水系钾离子电池 亚铁氰化铁 正极",
        "expected_title": "High-Capacity Aqueous Potassium-Ion Batteries for Large-Scale Energy Storage",
        "evidence_terms": ["aqueous", "potassium-ion", "large-scale energy storage"],
    },
    {
        "query": "Mn 掺杂 亚铁氰化钾 正极",
        "expected_title": "Surface-Substituted Prussian Blue Analogue Cathode for Sustainable Potassium-Ion Batteries",
        "evidence_terms": ["surface-substituted", "prussian blue analogue", "potassium-ion batteries"],
    },
    {
        "query": "K2Mn[Fe(CN)6] defect-free potassium manganese hexacyanoferrate cathode",
        "expected_title": "Defect-Free Potassium Manganese Hexacyanoferrate Cathode Material for High-Performance Potassium-Ion Batteries",
        "evidence_terms": ["defect-free", "potassium manganese hexacyanoferrate", "potassium-ion batteries"],
    },
    {
        "query": "KxMnFe(CN)6 low-cost high-energy potassium cathode",
        "expected_title": "Low-Cost High-Energy Potassium Cathode",
        "evidence_terms": ["kxmnfe(cn)6", "low-cost", "high-energy potassium cathode"],
    },
    {
        "query": "KNiHCF low-strain potassium-rich Prussian blue analogue high power",
        "expected_title": "A Low-Strain Potassium-Rich Prussian Blue Analogue Cathode for High Power Potassium-Ion Batteries",
        "evidence_terms": ["low-strain", "potassium-rich", "high power"],
    },
    {
        "query": "patterned electrodes flexible potassium-ion battery Prussian blue",
        "expected_title": "High-Energy-Density Flexible Potassium-Ion Battery Based on Patterned Electrodes",
        "evidence_terms": ["patterned electrodes", "flexible potassium-ion battery", "prussian blue"],
    },
    {
        "query": "Prussian Blue Analogs as Battery Materials review",
        "expected_title": "Prussian Blue Analogs as Battery Materials",
        "evidence_terms": ["prussian blue analogs", "battery materials"],
    },
    {
        "query": "capacity fading structural degradation wide-temperature sodium-ion cylindrical battery",
        "expected_title": "Understanding Capacity Fading from Structural Degradation in Prussian Blue Analogues for Wide-Temperature Sodium-Ion Cylindrical Battery",
        "evidence_terms": ["capacity fading", "structural degradation", "wide-temperature"],
    },
    {
        "query": "MnHCF 空位 机制 structural vacancies",
        "expected_title": "Unveiling the Role of Structural Vacancies in Mn-Based Prussian Blue Analogues for Energy Storage Applications",
        "evidence_terms": ["structural vacancies", "mn-based prussian blue analogues", "energy storage"],
    },
]


NOISE_MARKERS = [
    "https://doi.org/",
    "wileyonlinelibrary.com",
    "metrics & more",
    "article recommendations",
    "read online",
    "supporting information",
    "downloaded by",
    "view article online",
    "terms and conditions",
    "creative commons license",
    "published on",
    "acknowledges support",
    "office of energy efficiency",
    "vehicle technologies office",
    "energy & environmental science paper",
    "conflicts of interest",
    "author contributions",
    "data availability",
]

NOISY_TITLE_MARKERS = [
    "communication",
    "perspective",
    "article",
    "articles",
    "wiley-vch",
    "energy environ. sci.",
]


def _count_noise_markers(text: str) -> int:
    lowered = str(text or "").lower()
    return sum(lowered.count(marker) for marker in NOISE_MARKERS)


def _looks_like_affiliation_noise(text: str) -> bool:
    lowered = str(text or "").lower()
    affiliation_markers = [
        "school of",
        "department of",
        "college of",
        "faculty of",
        "center for",
        "centre for",
        "key laboratory",
        "state key laboratory",
        "institute of",
        "laboratory of",
        "university",
    ]
    place_markers = [
        "p. r. china",
        "united states",
        "australia",
        "usa",
        "beijing",
        "tianjin",
        "changsha",
        "austin",
    ]
    return any(marker in lowered for marker in affiliation_markers) and any(
        place in lowered for place in place_markers
    )


def run(output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> Dict[str, Any]:
    output_path = Path(output_dir)
    if not output_path.exists():
        KnowledgeBaseBuilder(output_dir=output_path).build()

    store = KnowledgeBaseStore.load(output_path)
    stats = store.stats()
    assert stats["document_count"] == 10, f"Expected 10 documents, got {stats['document_count']}"
    assert stats["chunk_count"] >= 100, f"Expected at least 100 chunks, got {stats['chunk_count']}"

    report: Dict[str, Any] = {
        "stats": stats,
        "documents": [],
        "queries": [],
    }

    for document in store.documents:
        lowered_title = document.title.lower()
        assert document.title.strip(), f"Empty title for {document.filename}"
        assert document.summary.strip(), f"Empty summary for {document.filename}"
        assert document.aliases, f"Empty aliases for {document.filename}"
        assert len(document.summary) <= 220, f"Summary too long for {document.filename}"
        assert not any(marker in lowered_title for marker in NOISY_TITLE_MARKERS), (
            f"Noisy title for {document.filename}: {document.title}"
        )
        assert _count_noise_markers(document.full_text[:1500]) == 0, (
            f"Document leading text still contains obvious boilerplate noise: {document.filename}"
        )
        report["documents"].append(
            {
                "doc_id": document.doc_id,
                "filename": document.filename,
                "title": document.title,
                "summary": document.summary,
                "alias_count": len(document.aliases),
            }
        )

    for case in QUERY_CASES:
        result = store.search(case["query"], top_k=5)
        hits = result["hits"]
        assert hits, f"No hits returned for query: {case['query']}"
        top = hits[0]
        assert top["title"] == case["expected_title"], (
            f"Top hit mismatch for query '{case['query']}'. "
            f"Expected '{case['expected_title']}', got '{top['title']}'"
        )
        merged = f"{top['title']} {top['text']}".lower()
        assert any(term.lower() in merged for term in case["evidence_terms"]), (
            f"Top hit for query '{case['query']}' lacks expected evidence terms "
            f"{case['evidence_terms']}"
        )
        assert _count_noise_markers(top["text"]) == 0, (
            f"Top hit for query '{case['query']}' contains obvious boilerplate noise"
        )
        assert not _looks_like_affiliation_noise(top["text"][:400]), (
            f"Top hit for query '{case['query']}' still looks like affiliation text"
        )
        report["queries"].append(
            {
                "query": case["query"],
                "top_title": top["title"],
                "top_filename": top["filename"],
                "top_source": top["source"],
                "top_score": top["score"],
            }
        )

    report_path = output_path / "audit_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("knowledge_base audit passed")

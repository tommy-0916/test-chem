"""Independent smoke tests for the minimal paper knowledge-base MVP."""

from __future__ import annotations

from pathlib import Path

from .mvp import DEFAULT_OUTPUT_DIR, KnowledgeBaseBuilder, KnowledgeBaseStore


EXPECTED_TITLES = {
    "11_JACS_rational-design-of-prussian-blue-analogues-for-ultralong-and-wide-temperature-range-sodium-ion-batteries.pdf": "Rational Design of Prussian Blue Analogues for Ultralong and Wide-Temperature-Range Sodium-Ion Batteries",
    "15_AM_亚铁氰化铁做为正极材料.pdf": "High-Capacity Aqueous Potassium-Ion Batteries for Large-Scale Energy Storage",
    "16_Nat Sus_Mn掺杂亚铁氰化钾正极.pdf": "Surface-Substituted Prussian Blue Analogue Cathode for Sustainable Potassium-Ion Batteries",
    "22_NC_锰基铁氰化物普鲁士蓝做正极材料.pdf": "Defect-Free Potassium Manganese Hexacyanoferrate Cathode Material for High-Performance Potassium-Ion Batteries",
    "23_JACS_低成本高能量普鲁士蓝钾离子电池正极.pdf": "Low-Cost High-Energy Potassium Cathode",
    "24_Angew_低应变的普鲁士蓝正极.pdf": "A Low-Strain Potassium-Rich Prussian Blue Analogue Cathode for High Power Potassium-Ion Batteries",
    "25_Joule_普鲁士蓝类似物正极材料.pdf": "High-Energy-Density Flexible Potassium-Ion Battery Based on Patterned Electrodes",
    "36_Joule_PBA正极综述.pdf": "Prussian Blue Analogs as Battery Materials",
    "43_NC_PBA宽温钠离子电池正极.pdf": "Understanding Capacity Fading from Structural Degradation in Prussian Blue Analogues for Wide-Temperature Sodium-Ion Cylindrical Battery",
    "50_EES_MnHCF空位机制.pdf": "Unveiling the Role of Structural Vacancies in Mn-Based Prussian Blue Analogues for Energy Storage Applications",
}


SEARCH_CASES = [
    {
        "query": "ultralong wide-temperature sodium-ion batteries rational design Prussian blue analogue",
        "expected_top_filename_contains": ["11_JACS_rational-design-of-prussian-blue-analogues-for-ultralong-and-wide-temperature-range-sodium-ion-batteries"],
    },
    {
        "query": "K2Mn[Fe(CN)6] defect-free potassium manganese hexacyanoferrate cathode",
        "expected_top_filename_contains": ["22_NC_锰基铁氰化物普鲁士蓝做正极材料"],
    },
    {
        "query": "Mn 掺杂 亚铁氰化钾 正极",
        "expected_top_filename_contains": ["16_Nat Sus_Mn掺杂亚铁氰化钾正极"],
    },
    {
        "query": "水系 钾离子电池 亚铁氰化铁 正极",
        "expected_top_filename_contains": ["15_AM_亚铁氰化铁做为正极材料"],
    },
    {
        "query": "KxMnFe(CN)6 low-cost high-energy potassium cathode",
        "expected_top_filename_contains": ["23_JACS_低成本高能量普鲁士蓝钾离子电池正极"],
    },
    {
        "query": "KNiHCF low-strain potassium-rich Prussian blue analogue high power",
        "expected_top_filename_contains": ["24_Angew_低应变的普鲁士蓝正极"],
    },
    {
        "query": "patterned electrodes flexible potassium-ion battery Prussian blue",
        "expected_top_filename_contains": ["25_Joule_普鲁士蓝类似物正极材料"],
    },
    {
        "query": "Prussian Blue Analogs as Battery Materials review",
        "expected_top_filename_contains": ["36_Joule_PBA正极综述"],
    },
    {
        "query": "capacity fading structural degradation wide-temperature sodium-ion cylindrical battery",
        "expected_top_filename_contains": ["43_NC_PBA宽温钠离子电池正极"],
    },
    {
        "query": "MnHCF 空位 机制 structural vacancies",
        "expected_top_filename_contains": ["50_EES_MnHCF空位机制"],
    },
]


def run(output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> None:
    output_path = Path(output_dir)
    if not output_path.exists():
        KnowledgeBaseBuilder(output_dir=output_path).build()

    store = KnowledgeBaseStore.load(output_path)
    stats = store.stats()
    assert stats["document_count"] == 10, f"Expected 10 documents, got {stats['document_count']}"
    assert stats["chunk_count"] >= 100, f"Expected at least 100 chunks, got {stats['chunk_count']}"

    assert len(store.documents) == 10, f"Expected 10 loaded documents, got {len(store.documents)}"
    for document in store.documents:
        expected_title = EXPECTED_TITLES.get(document.filename)
        assert expected_title, f"Unexpected document in knowledge base: {document.filename}"
        assert document.title == expected_title, (
            f"Unexpected title for {document.filename}: {document.title!r}"
        )
        assert document.summary.strip(), f"Document summary is empty for {document.filename}"
        assert document.aliases, f"Document aliases are empty for {document.filename}"

    for case in SEARCH_CASES:
        result = store.search(case["query"], top_k=5)
        hits = result["hits"]
        assert hits, f"No hits returned for query: {case['query']}"
        top_filename = hits[0]["filename"]
        assert any(token in top_filename for token in case["expected_top_filename_contains"]), (
            f"Top hit mismatch for query '{case['query']}'. "
            f"Expected one of {case['expected_top_filename_contains']}, got {top_filename}"
        )

        first_chunk = store.get_chunk(hits[0]["chunk_id"])
        assert first_chunk is not None, f"Missing chunk data for {hits[0]['chunk_id']}"
        linked_doc = store.get_doc(hits[0]["doc_id"])
        assert linked_doc is not None, f"Missing document data for {hits[0]['doc_id']}"
        assert linked_doc["summary"].strip(), f"Linked document summary empty for {hits[0]['doc_id']}"


if __name__ == "__main__":
    run()
    print("knowledge_base smoke tests passed")

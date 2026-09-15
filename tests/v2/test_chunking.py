from __future__ import annotations

from knowledge_runtime.v2.chunking import ChunkSetBuilder
from knowledge_runtime.v2.contracts import DocumentElement, DocumentIR, ParseReport, content_hash


def make_ir() -> DocumentIR:
    elements = tuple(
        DocumentElement(
            element_id=f"el-{index}",
            revision_id="rev-1",
            element_type="paragraph",
            text=text,
            section_path=("Scope",),
            page=1,
            bbox=None,
            payload={},
            provenance={"line_start": index, "line_end": index},
            confidence=1.0,
            content_hash=content_hash(text),
        )
        for index, text in enumerate(("first fact", "second fact"), 1)
    )
    return DocumentIR("doc-1", "rev-1", {"source_name": "policy.md"}, elements, ParseReport.empty("local", "1"), ())


def test_chunkset_preserves_element_provenance() -> None:
    chunks = ChunkSetBuilder(version="hybrid-v1").build(make_ir())
    assert chunks[0].element_ids
    assert chunks[0].revision_id == "rev-1"
    assert chunks[0].section_path == make_ir().elements[0].section_path
    assert chunks[0].next_chunk_id == chunks[1].chunk_id


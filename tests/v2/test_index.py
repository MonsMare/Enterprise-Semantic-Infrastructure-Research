from __future__ import annotations

from knowledge_runtime.v2.chunking import ChunkSetBuilder
from knowledge_runtime.v2.contracts import (
    DocumentElement,
    DocumentIR,
    EvidenceSearchRequest,
    ParseReport,
    RevisionIndexInput,
    content_hash,
)
from knowledge_runtime.v2.index import InMemoryIndexBackend


def make_input() -> RevisionIndexInput:
    text = "The reserve margin is twelve percent for annuity risk."
    element = DocumentElement(
        element_id="el-1",
        revision_id="rev-1",
        element_type="paragraph",
        text=text,
        section_path=("Reserves",),
        page=1,
        bbox=None,
        payload={},
        provenance={},
        confidence=1.0,
        content_hash=content_hash(text),
    )
    ir = DocumentIR("doc-1", "rev-1", {"source_name": "policy.md"}, (element,), ParseReport.empty("local", "1"), ())
    return RevisionIndexInput.from_ir(ir, chunks=ChunkSetBuilder(version="test-v1").build(ir))


def test_search_returns_published_evidence_refs() -> None:
    backend = InMemoryIndexBackend()
    result = backend.publish_revision(make_input())

    page = backend.search_evidence(EvidenceSearchRequest("reserve margin"))

    assert result.indexed_count == 1
    assert page.items
    assert all(item.ref.revision_id == "rev-1" for item in page.items)
    assert page.items[0].ref.element_id == "el-1"


def test_index_remove_revision_removes_candidates() -> None:
    backend = InMemoryIndexBackend()
    backend.publish_revision(make_input())
    backend.remove_revision("doc-1", "rev-1")
    assert backend.search_evidence(EvidenceSearchRequest("reserve")).items == ()


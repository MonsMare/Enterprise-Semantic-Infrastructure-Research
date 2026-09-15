from __future__ import annotations

import pytest

from knowledge_runtime.errors import KRStaleLocator
from knowledge_runtime.v2.canonical import InMemoryCanonicalStore
from knowledge_runtime.v2.chunking import ChunkSetBuilder
from knowledge_runtime.v2.context import ContextRuntime, RetrievalBudget
from knowledge_runtime.v2.contracts import DocumentElement, DocumentIR, EvidenceRef, ParseReport, RevisionIndexInput, content_hash
from knowledge_runtime.v2.index import InMemoryIndexBackend


def make_runtime() -> ContextRuntime:
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
        provenance={"line_start": 1, "line_end": 1},
        confidence=1.0,
        content_hash=content_hash(text),
    )
    ir = DocumentIR("doc-1", "rev-1", {"source_name": "policy.md"}, (element,), ParseReport.empty("local", "1"), ())
    canonical = InMemoryCanonicalStore()
    canonical.put_revision(ir, source_hash="src-1")
    index = InMemoryIndexBackend(canonical=canonical)
    published = index.publish_revision(RevisionIndexInput.from_ir(ir, chunks=ChunkSetBuilder().build(ir)))
    canonical.record_index_run(published.index_version, state="SUCCEEDED")
    canonical.publish_current("doc-1", "rev-1", index_version=published.index_version)
    return ContextRuntime(canonical=canonical, index=index)


def test_get_evidence_reads_canonical_content_after_search() -> None:
    runtime = make_runtime()
    page = runtime.search_evidence("reserve margin", limit=5)
    evidence = runtime.get_evidence(page.items[0].ref, max_bytes=1000)
    assert evidence.ref == page.items[0].ref
    assert evidence.content_hash
    assert "twelve percent" in evidence.content


def test_budget_deduplicates_same_evidence() -> None:
    budget = RetrievalBudget(max_evidence=1, max_bytes=100)
    assert budget.accept("ev-1", "hash-1", 20)
    assert not budget.accept("ev-1", "hash-1", 20)
    assert not budget.accept("ev-2", "hash-2", 20)


def test_get_evidence_rejects_stale_revision() -> None:
    runtime = make_runtime()
    with pytest.raises(KRStaleLocator):
        runtime.get_evidence(EvidenceRef("doc-1", "old-rev", "el-1"), max_bytes=100)

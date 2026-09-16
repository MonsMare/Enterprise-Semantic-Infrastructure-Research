from __future__ import annotations

from knowledge_runtime.v2.chunking import ChunkSetBuilder
from knowledge_runtime.v2.canonical import InMemoryCanonicalStore
from knowledge_runtime.v2.contracts import (
    DocumentElement,
    DocumentIR,
    EvidenceSearchRequest,
    ParseReport,
    IndexRebuildRequest,
    RevisionIndexInput,
    content_hash,
)
from knowledge_runtime.v2.index import InMemoryIndexBackend
from knowledge_runtime.v2.index import OpenSearchIndexBackend


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


def test_rebuild_replaces_prior_derived_rows_with_current_canonical_revision() -> None:
    canonical = InMemoryCanonicalStore()
    current = make_input()
    current_ir = DocumentIR(
        current.document_id,
        current.revision_id,
        {"source_name": "policy.md"},
        current.elements,
        ParseReport.empty("local", "1"),
        (),
    )
    canonical.put_revision(current_ir, source_hash="source-current")
    canonical.record_index_run("canonical-current", state="SUCCEEDED")
    canonical.publish_current("doc-1", "rev-1", index_version="canonical-current")
    backend = InMemoryIndexBackend(canonical=canonical)
    stale = RevisionIndexInput(
        document_id="doc-1",
        revision_id="rev-stale",
        source_name="stale.md",
        elements=current.elements,
    )
    backend.publish_revision(stale, index_version="stale")

    report = backend.rebuild(IndexRebuildRequest("rebuild-v1"))

    assert report.state == "SUCCEEDED"
    assert {key[1] for key in backend._hits} == {"rev-1"}
    assert {item.ref.revision_id for item in backend.search_evidence(EvidenceSearchRequest("reserve margin")).items} == {"rev-1"}


def test_opensearch_backend_filters_stale_revisions_against_canonical_current_pointer() -> None:
    class Canonical:
        def current_revision(self, document_id):
            return "rev-current"

    class Client:
        def search(self, *, index, body):
            return {
                "hits": {
                    "hits": [
                        {
                            "_score": 10,
                            "_source": {
                                "document_id": "doc-1",
                                "revision_id": "rev-stale",
                                "element_id": "el-stale",
                                "source_name": "stale.md",
                                "text": "reserve margin stale text",
                            },
                        },
                        {
                            "_score": 9,
                            "_source": {
                                "document_id": "doc-1",
                                "revision_id": "rev-current",
                                "element_id": "el-current",
                                "source_name": "current.md",
                                "text": "reserve margin current text",
                            },
                        },
                    ]
                }
            }

    backend = OpenSearchIndexBackend(client=Client(), canonical=Canonical())
    page = backend.search_evidence(EvidenceSearchRequest("reserve margin"))

    assert [item.ref.revision_id for item in page.items] == ["rev-current"]


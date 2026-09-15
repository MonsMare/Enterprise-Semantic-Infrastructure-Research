from __future__ import annotations

from knowledge_runtime.v2.canonical import InMemoryCanonicalStore
from knowledge_runtime.v2.contracts import DocumentElement, DocumentIR, ParseReport, content_hash


def make_ir(revision: str, text: str = "reserve margin is 12%") -> DocumentIR:
    element = DocumentElement(
        element_id=f"el-{revision}",
        revision_id=revision,
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
    return DocumentIR(
        document_id="doc-1",
        revision_id=revision,
        metadata={"source_name": "policy.md"},
        elements=(element,),
        parse_report=ParseReport.empty("local", "1"),
        source_artifacts=(),
    )


def test_current_revision_is_not_published_before_index_success() -> None:
    store = InMemoryCanonicalStore()
    ir = make_ir("rev-1")
    store.put_revision(ir, source_hash="src-1")
    store.begin_publication(ir.document_id, ir.revision_id, index_version="idx-1")

    assert store.current_revision(ir.document_id) is None
    store.record_index_run("idx-1", state="SUCCEEDED")
    store.publish_current(ir.document_id, ir.revision_id, index_version="idx-1")

    assert store.current_revision(ir.document_id) == ir.revision_id


def test_failed_revision_keeps_previous_current() -> None:
    store = InMemoryCanonicalStore()
    first = make_ir("rev-1")
    second = make_ir("rev-2", "reserve margin is 14%")
    store.put_revision(first, source_hash="src-1")
    store.record_index_run("idx-1", state="SUCCEEDED")
    store.publish_current("doc-1", "rev-1", index_version="idx-1")
    store.put_revision(second, source_hash="src-2")
    store.record_index_run("idx-2", state="FAILED")

    try:
        store.publish_current("doc-1", "rev-2", index_version="idx-2")
    except ValueError:
        pass

    assert store.current_revision("doc-1") == "rev-1"


def test_store_returns_immutable_ir_and_revision_lookup() -> None:
    store = InMemoryCanonicalStore()
    ir = make_ir("rev-1")
    store.put_revision(ir, source_hash="src-1")

    assert store.find_revision_by_source_hash("src-1").revision_id == "rev-1"
    assert store.get_revision("doc-1", "rev-1").source_name == "policy.md"
    assert store.get_ir("doc-1", "rev-1") == ir
    assert store.get_element("doc-1", "rev-1", "el-rev-1") == ir.elements[0]


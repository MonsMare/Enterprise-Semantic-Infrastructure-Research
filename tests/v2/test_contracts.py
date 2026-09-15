from __future__ import annotations

import pytest

from knowledge_runtime.v2.contracts import (
    DocumentElement,
    DocumentIR,
    Evidence,
    EvidenceRef,
    ParseReport,
    content_hash,
)


def test_evidence_identity_contains_revision_and_full_content_hash():
    ref = EvidenceRef("doc-1", "rev-1", "el-1", {"start": 1, "end": 2})
    evidence = Evidence.from_content(
        ref=ref,
        content="reserve margin is 12%",
        media_type="text/plain",
        representation="structured",
        source_label="policy.md",
    )

    assert evidence.source_revision == "rev-1"
    assert evidence.content_hash == content_hash("reserve margin is 12%")
    assert evidence.as_model_input()["evidence_id"] == evidence.evidence_id


def test_document_element_rejects_cross_revision_reference():
    element = DocumentElement(
        element_id="el-1",
        revision_id="rev-1",
        element_type="paragraph",
        text="text",
        section_path=(),
        page=None,
        bbox=None,
        payload={},
        provenance={},
        confidence=1.0,
        content_hash=content_hash("text"),
    )

    with pytest.raises(ValueError, match="revision"):
        DocumentIR(
            "doc-1",
            "rev-2",
            {},
            (element,),
            ParseReport.empty("local", "1"),
            (),
        )


def test_truncated_evidence_keeps_full_hash():
    ref = EvidenceRef("doc-1", "rev-1", "el-1", None)
    evidence = Evidence.from_content(
        ref=ref,
        content="a long canonical passage",
        media_type="text/plain",
        representation="structured",
        source_label="policy.md",
        max_bytes=4,
    )

    assert evidence.truncated is True
    assert evidence.content == "a lo"
    assert evidence.content_hash == content_hash("a long canonical passage")


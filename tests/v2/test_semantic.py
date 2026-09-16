from __future__ import annotations

import pytest

from knowledge_runtime.v2.contracts import DocumentElement, DocumentIR, EvidenceRef, ParseReport, content_hash
from knowledge_runtime.v2.semantic import InMemoryProposalStore, SemanticEnrichmentWorker, SemanticProjectionService


def evidence_ref() -> EvidenceRef:
    return EvidenceRef("doc-1", "rev-1", "el-1", {"type": "element"})


def test_meaningful_proposal_requires_evidence_from_creation() -> None:
    proposals = InMemoryProposalStore()
    with pytest.raises(ValueError, match="Evidence"):
        proposals.create(
            kind="claim",
            payload={"predicate": "applies_to"},
            evidence_refs=(),
            extractor_version="test",
            confidence=0.8,
        )


def test_conflicted_claim_is_excluded_from_default_context() -> None:
    proposals = InMemoryProposalStore()
    proposal = proposals.create(
        kind="claim",
        payload={},
        evidence_refs=(evidence_ref(),),
        extractor_version="test",
        confidence=0.8,
    )
    proposals.transition(proposal.proposal_id, "CONFLICTED")
    assert proposals.list_eligible(statuses=("VERIFIED", "CERTIFIED")) == []


def test_verified_claim_requires_valid_transition() -> None:
    proposals = InMemoryProposalStore()
    proposal = proposals.create(
        kind="claim",
        payload={"predicate": "applies_to"},
        evidence_refs=(evidence_ref(),),
        extractor_version="test",
        confidence=0.8,
    )
    proposals.transition(proposal.proposal_id, "AUTO_ACCEPTED")
    verified = proposals.transition(proposal.proposal_id, "VERIFIED")
    assert verified.status == "VERIFIED"
    with pytest.raises(ValueError, match="transition"):
        proposals.transition(proposal.proposal_id, "PROPOSED")


def test_deterministic_enrichment_emits_evidence_linked_semantic_types() -> None:
    heading = DocumentElement(
        element_id="heading-1",
        revision_id="rev-1",
        element_type="heading",
        text="Reserve Margin",
        section_path=("Reserve Margin",),
        page=1,
        bbox=None,
        payload={"level": 1},
        provenance={"line_start": 1},
        confidence=1.0,
        content_hash=content_hash("Reserve Margin"),
    )
    paragraph = DocumentElement(
        element_id="paragraph-1",
        revision_id="rev-1",
        element_type="paragraph",
        text="Reserve Margin applies to adverse deviation risk.",
        section_path=("Reserve Margin",),
        page=1,
        bbox=None,
        payload={},
        provenance={"line_start": 3},
        confidence=1.0,
        content_hash=content_hash("Reserve Margin applies to adverse deviation risk."),
    )
    ir = DocumentIR("doc-1", "rev-1", {"source_name": "policy.md"}, (heading, paragraph), ParseReport.empty("local", "1"), ())

    proposals = SemanticEnrichmentWorker(proposals=InMemoryProposalStore()).propose_from_ir(ir)

    assert {proposal.kind for proposal in proposals} >= {"entity", "claim", "term", "metadata"}
    assert all(proposal.evidence_refs for proposal in proposals)


def test_projection_refuses_unverified_proposals() -> None:
    class Graph:
        def __init__(self) -> None:
            self.published = []

        def upsert(self, proposal) -> None:
            self.published.append(proposal.proposal_id)

    proposal = InMemoryProposalStore().create(
        kind="claim",
        payload={"predicate": "applies_to"},
        evidence_refs=(evidence_ref(),),
        extractor_version="test",
        confidence=0.8,
    )
    graph = Graph()
    projection = SemanticProjectionService(neo4j=graph)

    with pytest.raises(ValueError, match="VERIFIED"):
        projection.publish_verified(proposal)
    assert graph.published == []


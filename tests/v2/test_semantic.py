from __future__ import annotations

import pytest

from knowledge_runtime.v2.contracts import EvidenceRef
from knowledge_runtime.v2.semantic import InMemoryProposalStore


def evidence_ref() -> EvidenceRef:
    return EvidenceRef("doc-1", "rev-1", "el-1", {"type": "element"})


def test_claim_without_evidence_cannot_be_verified() -> None:
    proposals = InMemoryProposalStore()
    proposal = proposals.create(
        kind="claim",
        payload={"predicate": "applies_to"},
        evidence_refs=(),
        extractor_version="test",
        confidence=0.8,
    )
    with pytest.raises(ValueError, match="Evidence"):
        proposals.transition(proposal.proposal_id, "VERIFIED")


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


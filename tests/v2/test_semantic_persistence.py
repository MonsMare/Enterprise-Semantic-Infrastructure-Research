from __future__ import annotations

from knowledge_runtime.v2.contracts import EvidenceRef
from knowledge_runtime.v2.semantic import SqliteProposalStore


def _ref() -> EvidenceRef:
    return EvidenceRef("doc-1", "rev-1", "el-1", {"type": "element"})


def test_sqlite_verified_proposal_survives_reopen_and_is_idempotent(tmp_path) -> None:
    database = tmp_path / "semantic.sqlite"
    store = SqliteProposalStore(database)
    proposal = store.create(
        kind="claim",
        payload={"subject_id": "reserve-margin", "predicate": "applies_to", "value": "annuity risk"},
        evidence_refs=(_ref(),),
        extractor_version="deterministic-v1",
        confidence=0.9,
    )
    store.transition(proposal.proposal_id, "AUTO_ACCEPTED")
    verified = store.transition(proposal.proposal_id, "VERIFIED")
    duplicate = store.create(
        kind="claim",
        payload={"subject_id": "reserve-margin", "predicate": "applies_to", "value": "annuity risk"},
        evidence_refs=(_ref(),),
        extractor_version="deterministic-v1",
        confidence=0.9,
    )
    store.close()

    reopened = SqliteProposalStore(database)

    assert duplicate.proposal_id == proposal.proposal_id
    assert reopened.get(proposal.proposal_id) == verified
    assert [item.proposal_id for item in reopened.list_eligible()] == [proposal.proposal_id]

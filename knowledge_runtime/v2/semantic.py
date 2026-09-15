from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, Protocol, Sequence

from .contracts import DocumentIR, EvidenceRef, ProposalStatus, SemanticProposal


class ProposalStore(Protocol):
    def create(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        evidence_refs: Sequence[EvidenceRef],
        extractor_version: str = "unknown",
        confidence: float = 0.0,
        idempotency_key: str | None = None,
    ) -> SemanticProposal: ...

    def get(self, proposal_id: str) -> SemanticProposal: ...

    def transition(self, proposal_id: str, target: ProposalStatus) -> SemanticProposal: ...

    def list_eligible(self, *, statuses: Sequence[ProposalStatus] = ("VERIFIED", "CERTIFIED")) -> list[SemanticProposal]: ...


_TRANSITIONS: dict[str, set[str]] = {
    "PROPOSED": {"AUTO_ACCEPTED", "CONFLICTED", "REJECTED"},
    "AUTO_ACCEPTED": {"VERIFIED", "CONFLICTED"},
    "VERIFIED": {"CERTIFIED", "CONFLICTED"},
    "CERTIFIED": {"CONFLICTED"},
    "CONFLICTED": {"REVIEW", "REJECTED"},
    "REVIEW": {"VERIFIED", "REJECTED"},
    "REJECTED": set(),
}


class InMemoryProposalStore:
    def __init__(self) -> None:
        self._proposals: dict[str, SemanticProposal] = {}
        self._idempotency: dict[str, str] = {}

    def create(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        evidence_refs: Sequence[EvidenceRef],
        extractor_version: str = "unknown",
        confidence: float = 0.0,
        idempotency_key: str | None = None,
    ) -> SemanticProposal:
        key = idempotency_key or hashlib.sha256(
            json.dumps(
                {
                    "kind": kind,
                    "payload": payload,
                    "evidence": [ref.as_dict() for ref in evidence_refs],
                    "extractor_version": extractor_version,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        existing_id = self._idempotency.get(key)
        if existing_id is not None:
            return self._proposals[existing_id]
        proposal_id = "proposal-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        proposal = SemanticProposal(
            proposal_id=proposal_id,
            kind=kind,
            payload=dict(payload),
            evidence_refs=tuple(evidence_refs),
            extractor_version=extractor_version,
            confidence=confidence,
            idempotency_key=key,
        )
        self._proposals[proposal_id] = proposal
        self._idempotency[key] = proposal_id
        return proposal

    def get(self, proposal_id: str) -> SemanticProposal:
        try:
            return self._proposals[proposal_id]
        except KeyError as exc:
            raise KeyError(f"unknown semantic proposal {proposal_id}") from exc

    def transition(self, proposal_id: str, target: ProposalStatus) -> SemanticProposal:
        proposal = self.get(proposal_id)
        if target in {"VERIFIED", "CERTIFIED"} and not proposal.evidence_refs:
            raise ValueError("verified semantic proposals require Evidence")
        if target not in _TRANSITIONS.get(proposal.status, set()):
            raise ValueError(f"invalid proposal transition: {proposal.status} -> {target}")
        updated = replace(proposal, status=target)
        self._proposals[proposal_id] = updated
        return updated

    def list_eligible(self, *, statuses: Sequence[ProposalStatus] = ("VERIFIED", "CERTIFIED")) -> list[SemanticProposal]:
        allowed = set(statuses)
        return [proposal for proposal in self._proposals.values() if proposal.status in allowed]

    def query_hints(self, query: str, *, filters: Any | None = None) -> dict[str, Any]:
        terms = {term.casefold() for term in query.split() if term.strip()}
        matching = [
            proposal
            for proposal in self.list_eligible()
            if terms.intersection(str(proposal.payload).casefold().split())
        ]
        return {"proposal_ids": [proposal.proposal_id for proposal in matching]}


class Neo4jSemanticAdapter:
    def __init__(self, driver: Any) -> None:
        self.driver = driver

    def upsert(self, proposal: SemanticProposal) -> None:
        query = (
            "MERGE (p:KRProposal {proposal_id: $proposal_id}) "
            "SET p.kind=$kind, p.status=$status, p.payload=$payload, p.confidence=$confidence"
        )
        with self.driver.session() as session:
            session.run(
                query,
                proposal_id=proposal.proposal_id,
                kind=proposal.kind,
                status=proposal.status,
                payload=dict(proposal.payload),
                confidence=proposal.confidence,
            )


class OpenMetadataAdapter:
    def __init__(self, client: Any) -> None:
        self.client = client

    def publish(self, proposal: SemanticProposal) -> Any:
        return self.client.publish_proposal(
            proposal_id=proposal.proposal_id,
            kind=proposal.kind,
            payload=dict(proposal.payload),
            status=proposal.status,
        )


class SemanticEnrichmentWorker:
    def __init__(self, *, proposals: ProposalStore, model_gateway: Any | None = None) -> None:
        self.proposals = proposals
        self.model_gateway = model_gateway

    def propose_from_ir(self, ir: DocumentIR, *, evidence_limit: int = 8) -> list[SemanticProposal]:
        refs = tuple(
            EvidenceRef(ir.document_id, ir.revision_id, element.element_id, {"type": "element"})
            for element in ir.elements[:evidence_limit]
        )
        proposals: list[SemanticProposal] = []
        for element, ref in zip(ir.elements[:evidence_limit], refs):
            proposals.append(
                self.proposals.create(
                    kind="metadata",
                    payload={"text": element.text, "section_path": list(element.section_path)},
                    evidence_refs=(ref,),
                    extractor_version="deterministic-v1",
                    confidence=element.confidence or 0.0,
                )
            )
        return proposals

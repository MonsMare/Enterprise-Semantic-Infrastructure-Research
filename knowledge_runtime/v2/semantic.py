from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from .contracts import DocumentIR, EvidenceRef, ProposalStatus, SemanticProposal


_EVIDENCE_REQUIRED_KINDS = {"claim", "entity", "term", "relation"}
_ELIGIBLE_STATUSES: tuple[ProposalStatus, ...] = ("VERIFIED", "CERTIFIED")
_TRANSITIONS: dict[str, set[str]] = {
    "PROPOSED": {"AUTO_ACCEPTED", "CONFLICTED", "REJECTED"},
    "AUTO_ACCEPTED": {"VERIFIED", "CONFLICTED"},
    "VERIFIED": {"CERTIFIED", "CONFLICTED"},
    "CERTIFIED": {"CONFLICTED"},
    "CONFLICTED": {"REVIEW", "REJECTED"},
    "REVIEW": {"VERIFIED", "REJECTED"},
    "REJECTED": set(),
}


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

    def list_eligible(self, *, statuses: Sequence[ProposalStatus] = _ELIGIBLE_STATUSES) -> list[SemanticProposal]: ...

    def query_hints(self, query: str, *, filters: Any | None = None) -> dict[str, Any]: ...


def _canonical_key(
    *,
    kind: str,
    payload: dict[str, Any],
    evidence_refs: Sequence[EvidenceRef],
    extractor_version: str,
    idempotency_key: str | None,
) -> str:
    if idempotency_key:
        return idempotency_key
    material = {
        "kind": kind,
        "payload": payload,
        "evidence": [ref.as_dict() for ref in evidence_refs],
        "extractor_version": extractor_version,
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _new_proposal(
    *,
    kind: str,
    payload: dict[str, Any],
    evidence_refs: Sequence[EvidenceRef],
    extractor_version: str,
    confidence: float,
    idempotency_key: str | None,
) -> SemanticProposal:
    refs = tuple(evidence_refs)
    if kind in _EVIDENCE_REQUIRED_KINDS and not refs:
        raise ValueError(f"{kind} semantic proposals require Evidence")
    key = _canonical_key(
        kind=kind,
        payload=payload,
        evidence_refs=refs,
        extractor_version=extractor_version,
        idempotency_key=idempotency_key,
    )
    return SemanticProposal(
        proposal_id="proposal-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24],
        kind=kind,
        payload=dict(payload),
        evidence_refs=refs,
        extractor_version=extractor_version,
        confidence=confidence,
        idempotency_key=key,
    )


def _transition(proposal: SemanticProposal, target: ProposalStatus) -> SemanticProposal:
    if target in {"VERIFIED", "CERTIFIED"} and not proposal.evidence_refs:
        raise ValueError("verified semantic proposals require Evidence")
    if target not in _TRANSITIONS.get(proposal.status, set()):
        raise ValueError(f"invalid proposal transition: {proposal.status} -> {target}")
    return replace(proposal, status=target)


def _proposal_hints(proposals: Sequence[SemanticProposal], query: str) -> dict[str, Any]:
    terms = {term.casefold() for term in re.findall(r"[\w]+|[^\W\d_]", query, re.UNICODE) if term.strip()}
    matching = [
        proposal
        for proposal in proposals
        if terms.intersection(
            term.casefold()
            for term in re.findall(r"[\w]+|[^\W\d_]", json.dumps(proposal.payload, ensure_ascii=False), re.UNICODE)
        )
    ]
    return {"proposal_ids": [proposal.proposal_id for proposal in matching]}


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
        proposal = _new_proposal(
            kind=kind,
            payload=payload,
            evidence_refs=evidence_refs,
            extractor_version=extractor_version,
            confidence=confidence,
            idempotency_key=idempotency_key,
        )
        existing_id = self._idempotency.get(proposal.idempotency_key)
        if existing_id is not None:
            return self._proposals[existing_id]
        self._proposals[proposal.proposal_id] = proposal
        self._idempotency[proposal.idempotency_key] = proposal.proposal_id
        return proposal

    def get(self, proposal_id: str) -> SemanticProposal:
        try:
            return self._proposals[proposal_id]
        except KeyError as exc:
            raise KeyError(f"unknown semantic proposal {proposal_id}") from exc

    def transition(self, proposal_id: str, target: ProposalStatus) -> SemanticProposal:
        updated = _transition(self.get(proposal_id), target)
        self._proposals[proposal_id] = updated
        return updated

    def list_eligible(self, *, statuses: Sequence[ProposalStatus] = _ELIGIBLE_STATUSES) -> list[SemanticProposal]:
        allowed = set(statuses)
        return sorted(
            (proposal for proposal in self._proposals.values() if proposal.status in allowed),
            key=lambda proposal: proposal.proposal_id,
        )

    def query_hints(self, query: str, *, filters: Any | None = None) -> dict[str, Any]:
        del filters
        return _proposal_hints(self.list_eligible(), query)

    def close(self) -> None:
        return None


class SqliteProposalStore:
    """Persistent L2 proposal store for private local development."""

    def __init__(self, path: str | Path | sqlite3.Connection) -> None:
        self._lock = threading.RLock()
        self._owns_connection = not isinstance(path, sqlite3.Connection)
        if self._owns_connection:
            raw_path = str(path)
            if raw_path != ":memory:":
                Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(raw_path, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
        else:
            self.connection = path
            self.connection.row_factory = sqlite3.Row
        with self._lock:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS semantic_proposals (
                proposal_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                confidence REAL NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                conflict_details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_sqlite_semantic_proposals_status
                ON semantic_proposals(status, proposal_id);
            """
        )
        self.connection.commit()

    def close(self) -> None:
        if self._owns_connection:
            with self._lock:
                self.connection.close()

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
        proposal = _new_proposal(
            kind=kind,
            payload=payload,
            evidence_refs=evidence_refs,
            extractor_version=extractor_version,
            confidence=confidence,
            idempotency_key=idempotency_key,
        )
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO semantic_proposals
                (proposal_id, status, kind, payload_json, evidence_refs_json, extractor_version,
                 confidence, idempotency_key, conflict_details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    proposal.proposal_id,
                    proposal.status,
                    proposal.kind,
                    _json_text(proposal.payload),
                    _json_text([ref.as_dict() for ref in proposal.evidence_refs]),
                    proposal.extractor_version,
                    proposal.confidence,
                    proposal.idempotency_key,
                    _json_text(proposal.conflict_details),
                ),
            )
        return self.get_by_idempotency(proposal.idempotency_key)

    def get_by_idempotency(self, idempotency_key: str) -> SemanticProposal:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT proposal_id, kind, status, payload_json, evidence_refs_json, extractor_version,
                       confidence, idempotency_key, conflict_details_json
                FROM semantic_proposals WHERE idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown semantic proposal idempotency key {idempotency_key}")
        return _proposal_from_row(row)

    def get(self, proposal_id: str) -> SemanticProposal:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT proposal_id, kind, status, payload_json, evidence_refs_json, extractor_version,
                       confidence, idempotency_key, conflict_details_json
                FROM semantic_proposals WHERE proposal_id=?
                """,
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown semantic proposal {proposal_id}")
        return _proposal_from_row(row)

    def transition(self, proposal_id: str, target: ProposalStatus) -> SemanticProposal:
        updated = _transition(self.get(proposal_id), target)
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE semantic_proposals SET status=?, conflict_details_json=? WHERE proposal_id=?",
                (updated.status, _json_text(updated.conflict_details), updated.proposal_id),
            )
        return self.get(proposal_id)

    def list_eligible(self, *, statuses: Sequence[ProposalStatus] = _ELIGIBLE_STATUSES) -> list[SemanticProposal]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT proposal_id, kind, status, payload_json, evidence_refs_json, extractor_version,
                       confidence, idempotency_key, conflict_details_json
                FROM semantic_proposals WHERE status IN ("""
                + placeholders
                + ") ORDER BY proposal_id",
                tuple(statuses),
            ).fetchall()
        return [_proposal_from_row(row) for row in rows]

    def query_hints(self, query: str, *, filters: Any | None = None) -> dict[str, Any]:
        del filters
        return _proposal_hints(self.list_eligible(), query)


class PostgresProposalStore:
    """PostgreSQL proposal store using the same lifecycle as the SQLite POC."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

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
        proposal = _new_proposal(
            kind=kind,
            payload=payload,
            evidence_refs=evidence_refs,
            extractor_version=extractor_version,
            confidence=confidence,
            idempotency_key=idempotency_key,
        )
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO semantic_proposals
                    (proposal_id, status, kind, payload_json, evidence_refs_json, details_json,
                     extractor_version, confidence, idempotency_key, conflict_details_json)
                    VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, '{}'::jsonb, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (
                        proposal.proposal_id,
                        proposal.status,
                        proposal.kind,
                        _json_text(proposal.payload),
                        _json_text([ref.as_dict() for ref in proposal.evidence_refs]),
                        proposal.extractor_version,
                        proposal.confidence,
                        proposal.idempotency_key,
                        _json_text(proposal.conflict_details),
                    ),
                )
        return self.get_by_idempotency(proposal.idempotency_key)

    def get_by_idempotency(self, idempotency_key: str) -> SemanticProposal:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT proposal_id, kind, status, payload_json, evidence_refs_json, extractor_version,
                       confidence, idempotency_key, conflict_details_json
                FROM semantic_proposals WHERE idempotency_key=%s
                """,
                (idempotency_key,),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(f"unknown semantic proposal idempotency key {idempotency_key}")
        return _proposal_from_row(row)

    def get(self, proposal_id: str) -> SemanticProposal:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT proposal_id, kind, status, payload_json, evidence_refs_json, extractor_version,
                       confidence, idempotency_key, conflict_details_json
                FROM semantic_proposals WHERE proposal_id=%s
                """,
                (proposal_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(f"unknown semantic proposal {proposal_id}")
        return _proposal_from_row(row)

    def transition(self, proposal_id: str, target: ProposalStatus) -> SemanticProposal:
        updated = _transition(self.get(proposal_id), target)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE semantic_proposals SET status=%s, conflict_details_json=%s::jsonb WHERE proposal_id=%s",
                    (updated.status, _json_text(updated.conflict_details), updated.proposal_id),
                )
        return self.get(proposal_id)

    def list_eligible(self, *, statuses: Sequence[ProposalStatus] = _ELIGIBLE_STATUSES) -> list[SemanticProposal]:
        if not statuses:
            return []
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT proposal_id, kind, status, payload_json, evidence_refs_json, extractor_version,
                       confidence, idempotency_key, conflict_details_json
                FROM semantic_proposals WHERE status = ANY(%s) ORDER BY proposal_id
                """,
                (list(statuses),),
            )
            rows = cursor.fetchall()
        return [_proposal_from_row(row) for row in rows]

    def query_hints(self, query: str, *, filters: Any | None = None) -> dict[str, Any]:
        del filters
        return _proposal_hints(self.list_eligible(), query)


class Neo4jSemanticAdapter:
    """Fixed-schema projection; it never becomes an Evidence source of truth."""

    def __init__(self, driver: Any) -> None:
        self.driver = driver

    def upsert(self, proposal: SemanticProposal) -> None:
        entity_id = str(proposal.payload.get("entity_id") or proposal.payload.get("subject_id") or proposal.proposal_id)
        term = str(proposal.payload.get("term") or proposal.payload.get("canonical_name") or "")
        query = """
        MERGE (p:KRProposal {proposal_id: $proposal_id})
        SET p.kind=$kind, p.status=$status, p.payload=$payload, p.confidence=$confidence
        WITH p
        FOREACH (_ IN CASE WHEN $kind = 'entity' THEN [1] ELSE [] END |
          MERGE (e:Entity {entity_id: $entity_id}) SET e.canonical_name=$entity_name
          MERGE (p)-[:DESCRIBES]->(e))
        FOREACH (_ IN CASE WHEN $kind = 'term' THEN [1] ELSE [] END |
          MERGE (t:Term {term_id: $entity_id}) SET t.term=$term
          MERGE (p)-[:DEFINES]->(t))
        FOREACH (ref IN $evidence_refs |
          MERGE (d:Document {document_id: ref.document_id})
          MERGE (evidence:Evidence {evidence_id: ref.document_id + ':' + ref.revision_id + ':' + ref.element_id})
          SET evidence.revision_id=ref.revision_id, evidence.element_id=ref.element_id
          MERGE (d)-[:HAS_EVIDENCE]->(evidence)
          MERGE (p)-[:SUPPORTED_BY]->(evidence))
        """
        with self.driver.session() as session:
            session.run(
                query,
                proposal_id=proposal.proposal_id,
                kind=proposal.kind,
                status=proposal.status,
                payload=dict(proposal.payload),
                confidence=proposal.confidence,
                entity_id=entity_id,
                entity_name=str(proposal.payload.get("canonical_name") or entity_id),
                term=term,
                evidence_refs=[ref.as_dict() for ref in proposal.evidence_refs],
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


class SemanticProjectionService:
    def __init__(self, *, neo4j: Any | None = None, openmetadata: Any | None = None) -> None:
        self.neo4j = neo4j
        self.openmetadata = openmetadata

    def publish_verified(self, proposal: SemanticProposal) -> dict[str, Any]:
        if proposal.status not in {"VERIFIED", "CERTIFIED"}:
            raise ValueError("only VERIFIED or CERTIFIED semantic proposals can be projected")
        result: dict[str, Any] = {}
        if self.neo4j is not None:
            self.neo4j.upsert(proposal)
            result["neo4j"] = "published"
        if self.openmetadata is not None and proposal.kind in {"metadata", "term"}:
            result["openmetadata"] = self.openmetadata.publish(proposal)
        return result


class SemanticEnrichmentWorker:
    def __init__(self, *, proposals: ProposalStore, model_gateway: Any | None = None) -> None:
        self.proposals = proposals
        self.model_gateway = model_gateway

    def propose_from_ir(self, ir: DocumentIR, *, evidence_limit: int = 8) -> list[SemanticProposal]:
        proposals: list[SemanticProposal] = []
        for element in ir.elements[:evidence_limit]:
            ref = EvidenceRef(ir.document_id, ir.revision_id, element.element_id, {"type": "element"})
            confidence = element.confidence or 0.0
            proposals.append(
                self.proposals.create(
                    kind="metadata",
                    payload={
                        "document_id": ir.document_id,
                        "revision_id": ir.revision_id,
                        "element_type": element.element_type,
                        "section_path": list(element.section_path),
                        "page": element.page,
                        "content_hash": element.content_hash,
                    },
                    evidence_refs=(ref,),
                    extractor_version="deterministic-v2",
                    confidence=confidence,
                )
            )
            entities = _entity_candidates(element.text)
            for entity in entities[:4]:
                proposals.append(
                    self.proposals.create(
                        kind="entity",
                        payload={
                            "entity_id": _semantic_id("entity", entity),
                            "canonical_name": entity,
                            "entity_class": "document_concept",
                        },
                        evidence_refs=(ref,),
                        extractor_version="deterministic-v2",
                        confidence=confidence,
                    )
                )
            if element.element_type == "heading":
                term = element.text.strip()
                if term:
                    proposals.append(
                        self.proposals.create(
                            kind="term",
                            payload={"term": term, "term_id": _semantic_id("term", term), "definition": None},
                            evidence_refs=(ref,),
                            extractor_version="deterministic-v2",
                            confidence=confidence,
                        )
                    )
            elif element.element_type in {"paragraph", "list", "table"} and element.text.strip():
                subject = entities[0] if entities else (element.section_path[-1] if element.section_path else ir.document_id)
                proposals.append(
                    self.proposals.create(
                        kind="claim",
                        payload={
                            "subject_id": _semantic_id("subject", subject),
                            "predicate": "states",
                            "value": element.text,
                        },
                        evidence_refs=(ref,),
                        extractor_version="deterministic-v2",
                        confidence=confidence,
                    )
                )
        return proposals


def _entity_candidates(text: str) -> list[str]:
    candidates = re.findall(r"\b[A-Z][A-Za-z0-9]*(?:[ -][A-Z][A-Za-z0-9]*){0,3}\b", text)
    ignored = {"A", "An", "And", "For", "Of", "The", "This"}
    unique: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip()
        if cleaned in ignored or cleaned in unique:
            continue
        unique.append(cleaned)
    return unique


def _semantic_id(prefix: str, value: str) -> str:
    return f"{prefix}-" + hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()[:20]


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return value
    return json.loads(value or "{}")


def _proposal_from_row(row: Any) -> SemanticProposal:
    values = list(row)
    refs = tuple(EvidenceRef.from_dict(value) for value in _json_value(values[4]))
    return SemanticProposal(
        proposal_id=str(values[0]),
        kind=str(values[1]),
        status=str(values[2]),  # type: ignore[arg-type]
        payload=_json_value(values[3]),
        evidence_refs=refs,
        extractor_version=str(values[5]),
        confidence=float(values[6]),
        idempotency_key=str(values[7]),
        conflict_details=_json_value(values[8]),
    )

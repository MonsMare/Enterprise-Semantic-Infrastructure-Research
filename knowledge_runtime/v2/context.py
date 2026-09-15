from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from ..errors import KRStaleLocator
from .canonical import CanonicalStore
from .contracts import (
    AssetSearchPage,
    AssetSearchRequest,
    Evidence,
    EvidenceFilters,
    EvidenceRef,
    EvidenceSearchPage,
    EvidenceSearchRequest,
    SearchHitV2,
)
from .index import IndexBackend


@dataclass(frozen=True)
class QueryHints:
    values: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "QueryHints":
        return cls({})


@dataclass
class RetrievalBudget:
    max_evidence: int = 12
    max_bytes: int = 100_000
    evidence_ids: set[str] = field(default_factory=set)
    content_hashes: set[str] = field(default_factory=set)
    bytes_used: int = 0

    def __post_init__(self) -> None:
        if self.max_evidence <= 0 or self.max_bytes <= 0:
            raise ValueError("retrieval budget limits must be positive")

    def accept(self, evidence_id: str, content_hash: str, byte_count: int) -> bool:
        if byte_count < 0:
            raise ValueError("byte_count cannot be negative")
        if evidence_id in self.evidence_ids or content_hash in self.content_hashes:
            return False
        if len(self.evidence_ids) >= self.max_evidence or self.bytes_used + byte_count > self.max_bytes:
            return False
        self.evidence_ids.add(evidence_id)
        self.content_hashes.add(content_hash)
        self.bytes_used += byte_count
        return True


@dataclass(frozen=True)
class ContextRunRecord:
    run_id: str
    primitive: str
    query: str
    evidence_refs: tuple[EvidenceRef, ...]
    budget: dict[str, Any]
    started_at: str
    duration_ms: float
    outcome: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


class ContextRuntime:
    """The only read surface exposed to an Agent.

    Search returns references and previews.  Canonical text enters a model
    context only through ``get_evidence`` after the caller chooses a ref.
    """

    def __init__(self, *, canonical: CanonicalStore, index: IndexBackend, overlay: Any | None = None) -> None:
        self.canonical = canonical
        self.index = index
        self.overlay = overlay

    def search_evidence(
        self,
        query: str,
        *,
        filters: EvidenceFilters | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> EvidenceSearchPage:
        hints = self.overlay.query_hints(query, filters=filters) if self.overlay else {}
        request = EvidenceSearchRequest(query=query, filters=filters, hints=hints, limit=limit, cursor=cursor)
        started = time.perf_counter()
        page = self.index.search_evidence(request)
        self._record(
            primitive="search_evidence",
            query=query,
            refs=tuple(hit.ref for hit in page.items),
            started=started,
            outcome="OK",
            diagnostics=page.diagnostics,
        )
        return page

    def search_assets(
        self,
        query: str,
        *,
        filters: Any | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> AssetSearchPage:
        started = time.perf_counter()
        page = self.index.search_assets(AssetSearchRequest(query=query, filters=filters, limit=limit, cursor=cursor))
        self._record(
            primitive="search_assets",
            query=query,
            refs=(),
            started=started,
            outcome="OK",
            diagnostics=page.diagnostics,
        )
        return page

    def lookup_entity(self, name: str, *, limit: int = 20) -> list[Any]:
        if self.overlay is None:
            return []
        proposals = self.overlay.list_eligible()
        lowered = name.casefold()
        return [
            proposal
            for proposal in proposals
            if proposal.kind == "entity" and lowered in str(proposal.payload).casefold()
        ][:limit]

    def get_claims(self, subject_id: str | None = None, *, limit: int = 20) -> list[Any]:
        if self.overlay is None:
            return []
        proposals = [proposal for proposal in self.overlay.list_eligible() if proposal.kind == "claim"]
        if subject_id:
            proposals = [proposal for proposal in proposals if str(proposal.payload.get("subject_id", "")) == subject_id]
        return proposals[:limit]

    def get_evidence(self, ref: EvidenceRef, *, max_bytes: int = 20_000, representation: str = "structured") -> Evidence:
        current = self.canonical.current_revision(ref.document_id)
        if current is not None and current != ref.revision_id:
            raise KRStaleLocator(
                f"Evidence ref points to {ref.revision_id}, current revision is {current}"
            )
        element = self.canonical.get_element(ref.document_id, ref.revision_id, ref.element_id)
        ir = self.canonical.get_ir(ref.document_id, ref.revision_id)
        source_label = str(ir.metadata.get("source_name", ref.document_id))
        evidence = Evidence.from_content(
            ref=ref,
            content=element.text,
            media_type="text/plain" if element.element_type not in {"table", "code"} else "text/markdown",
            representation=representation,
            source_label=source_label,
            resolved_provenance={
                **dict(element.provenance),
                "section_path": list(element.section_path),
                "page": element.page,
                "bbox": list(element.bbox) if element.bbox else None,
            },
            max_bytes=max_bytes,
        )
        self._record(
            primitive="get_evidence",
            query="",
            refs=(ref,),
            started=time.perf_counter(),
            outcome="OK",
            diagnostics={"truncated": evidence.truncated},
        )
        return evidence

    def _record(
        self,
        *,
        primitive: str,
        query: str,
        refs: tuple[EvidenceRef, ...],
        started: float,
        outcome: str,
        diagnostics: dict[str, Any],
    ) -> None:
        run_id = "ctx-" + hashlib.sha256(
            f"{primitive}\x00{query}\x00{','.join(ref.to_json() for ref in refs)}\x00{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:24]
        record = ContextRunRecord(
            run_id=run_id,
            primitive=primitive,
            query=query,
            evidence_refs=refs,
            budget={},
            started_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=(time.perf_counter() - started) * 1000,
            outcome=outcome,
            diagnostics=diagnostics,
        )
        recorder = getattr(self.canonical, "record_context_run", None)
        if recorder is not None:
            recorder(
                record.run_id,
                primitive=record.primitive,
                query=record.query,
                evidence_refs=[ref.as_dict() for ref in record.evidence_refs],
                budget=record.budget,
                started_at=record.started_at,
                duration_ms=record.duration_ms,
                outcome=record.outcome,
                diagnostics=record.diagnostics,
            )

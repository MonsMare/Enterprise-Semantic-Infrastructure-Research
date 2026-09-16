from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import (
    AssetSearchHit,
    AssetSearchPage,
    AssetSearchRequest,
    EvidenceSearchPage,
    EvidenceSearchRequest,
    EvidenceRef,
    IndexBuildReport,
    IndexPublishResult,
    IndexRebuildRequest,
    RevisionIndexInput,
    SearchHitV2,
)


class IndexBackend(Protocol):
    def publish_revision(self, revision: RevisionIndexInput, *, index_version: str | None = None) -> IndexPublishResult: ...

    def remove_revision(self, document_id: str, revision_id: str) -> None: ...

    def search_evidence(self, request: EvidenceSearchRequest) -> EvidenceSearchPage: ...

    def search_assets(self, request: AssetSearchRequest) -> AssetSearchPage: ...

    def rebuild(self, request: IndexRebuildRequest) -> IndexBuildReport: ...


@dataclass(frozen=True)
class _IndexedHit:
    ref: EvidenceRef
    document_id: str
    revision_id: str
    source_name: str
    text: str
    section_path: tuple[str, ...]
    page: int | None
    metadata: dict[str, Any]


def _terms(value: str) -> list[str]:
    terms = re.findall(r"[\w]+|[^\W\d_]+", value.casefold(), flags=re.UNICODE)
    return terms or [value.casefold()]


def _score(query: str, text: str) -> float:
    lowered = text.casefold()
    tokens = _terms(query)
    matched = sum(1 for token in tokens if token in lowered)
    phrase = 0.25 if query.casefold() in lowered else 0.0
    return matched / max(1, len(tokens)) + phrase


def _snapshot(items: list[Any], query: str) -> str:
    material = json.dumps([query, [str(item) for item in items]], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _page_offset(cursor: str | None, snapshot_id: str) -> int:
    if not cursor:
        return 0
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8"))
        if value["snapshot_id"] != snapshot_id:
            raise ValueError("cursor does not match the current result snapshot")
        return int(value["offset"])
    except Exception as exc:
        raise ValueError("invalid index cursor") from exc


class InMemoryIndexBackend:
    def __init__(self, *, canonical: Any | None = None) -> None:
        self.canonical = canonical
        self._hits: dict[tuple[str, str, str], _IndexedHit] = {}
        self._revisions: dict[tuple[str, str], RevisionIndexInput] = {}
        self._active: dict[str, str] = {}
        self._index_version = "memory-v1"

    def publish_revision(self, revision: RevisionIndexInput, *, index_version: str | None = None) -> IndexPublishResult:
        version = index_version or self._index_version
        self.remove_revision(revision.document_id, revision.revision_id)
        self._revisions[(revision.document_id, revision.revision_id)] = revision
        chunks = tuple(revision.chunks)
        if chunks:
            rows = chunks
        else:
            rows = revision.elements
        for row in rows:
            element_id = row.element_ids[0] if hasattr(row, "element_ids") else row.element_id
            text = row.text
            section_path = tuple(getattr(row, "section_path", ()))
            page = getattr(row, "page", None)
            metadata = dict(getattr(row, "metadata", {}) or {})
            key = (revision.document_id, revision.revision_id, element_id)
            self._hits[key] = _IndexedHit(
                ref=EvidenceRef(revision.document_id, revision.revision_id, element_id, {"type": "element"}),
                document_id=revision.document_id,
                revision_id=revision.revision_id,
                source_name=revision.source_name,
                text=text,
                section_path=section_path,
                page=page,
                metadata={"section_path": section_path, "page": page, **metadata},
            )
        self._active[revision.document_id] = revision.revision_id
        checksum = hashlib.sha256(
            "\n".join(sorted(f"{key}:{value.text}" for key, value in self._hits.items() if key[:2] == (revision.document_id, revision.revision_id))).encode("utf-8")
        ).hexdigest()
        return IndexPublishResult(version, revision.document_id, revision.revision_id, len(rows), checksum)

    def remove_revision(self, document_id: str, revision_id: str) -> None:
        for key in [key for key in self._hits if key[:2] == (document_id, revision_id)]:
            del self._hits[key]
        self._revisions.pop((document_id, revision_id), None)
        if self._active.get(document_id) == revision_id:
            self._active.pop(document_id, None)

    def _filtered_hits(self, filters: Any | None = None) -> list[_IndexedHit]:
        filters = filters or {}
        document_ids = set(getattr(filters, "document_ids", ()) or ())
        result = []
        for hit in self._hits.values():
            if document_ids and hit.document_id not in document_ids:
                continue
            if getattr(filters, "current_only", True):
                current = (
                    self.canonical.current_revision(hit.document_id)
                    if self.canonical is not None
                    else self._active.get(hit.document_id)
                )
                if current != hit.revision_id:
                    continue
            result.append(hit)
        return result

    def search_evidence(self, request: EvidenceSearchRequest) -> EvidenceSearchPage:
        candidates = self._filtered_hits(request.filters)
        ranked = sorted(
            ((hit, _score(request.query, hit.text)) for hit in candidates),
            key=lambda item: (-item[1], item[0].document_id, item[0].revision_id, item[0].ref.element_id),
        )
        ranked = [item for item in ranked if item[1] > 0]
        snapshot_id = _snapshot([item[0].ref.to_json() for item in ranked], request.query)
        offset = _page_offset(request.cursor, snapshot_id)
        selected = ranked[offset : offset + request.limit]
        next_cursor = None
        if offset + request.limit < len(ranked):
            next_cursor = base64.urlsafe_b64encode(
                json.dumps({"snapshot_id": snapshot_id, "offset": offset + request.limit}).encode("utf-8")
            ).decode("ascii").rstrip("=")
        return EvidenceSearchPage(
            items=tuple(
                SearchHitV2(
                    ref=hit.ref,
                    display_name=hit.source_name,
                    preview=hit.text[:240],
                    score=score,
                    rank=offset + rank,
                    metadata=hit.metadata,
                )
                for rank, (hit, score) in enumerate(selected, start=1)
            ),
            next_cursor=next_cursor,
            snapshot_id=snapshot_id,
        )

    def search_assets(self, request: AssetSearchRequest) -> AssetSearchPage:
        candidates = self._filtered_hits(request.filters)
        groups: dict[tuple[str, str], tuple[_IndexedHit, float]] = {}
        for hit in candidates:
            score = _score(request.query, hit.text) + _score(request.query, hit.source_name)
            if score <= 0:
                continue
            key = (hit.document_id, hit.revision_id)
            if key not in groups or score > groups[key][1]:
                groups[key] = (hit, score)
        ranked = sorted(groups.values(), key=lambda item: (-item[1], item[0].document_id, item[0].revision_id))
        snapshot_id = _snapshot([f"{item[0].document_id}:{item[0].revision_id}" for item in ranked], request.query)
        offset = _page_offset(request.cursor, snapshot_id)
        selected = ranked[offset : offset + request.limit]
        return AssetSearchPage(
            items=tuple(
                AssetSearchHit(
                    document_id=hit.document_id,
                    revision_id=hit.revision_id,
                    display_name=hit.source_name,
                    source_label=hit.source_name,
                    metadata=hit.metadata,
                    score=score,
                )
                for hit, score in selected
            ),
            snapshot_id=snapshot_id,
            next_cursor=None,
        )

    def rebuild(self, request: IndexRebuildRequest) -> IndexBuildReport:
        if self.canonical is None:
            return IndexBuildReport(request.index_version, 0, 0, 0, "SKIPPED", {"reason": "canonical store unavailable"})
        revisions = self.canonical.list_current_documents()
        if request.document_ids:
            revisions = [revision for revision in revisions if revision.document_id in request.document_ids]
        elements = chunks = 0
        if request.dry_run:
            for revision in revisions:
                ir = self.canonical.get_ir(revision.document_id, revision.revision_id)
                elements += len(ir.elements)
                chunks += len(ir.elements)
            return IndexBuildReport(request.index_version, len(revisions), elements, chunks, "SUCCEEDED")
        staged = InMemoryIndexBackend(canonical=self.canonical)
        staged._hits = dict(self._hits)
        staged._revisions = dict(self._revisions)
        staged._active = dict(self._active)
        requested = set(request.document_ids)
        if requested:
            for document_id, revision_id in list(staged._revisions):
                if document_id in requested:
                    staged.remove_revision(document_id, revision_id)
        else:
            staged._hits.clear()
            staged._revisions.clear()
            staged._active.clear()
        try:
            for revision in revisions:
                ir = self.canonical.get_ir(revision.document_id, revision.revision_id)
                result = staged.publish_revision(RevisionIndexInput.from_ir(ir), index_version=request.index_version)
                elements += len(ir.elements)
                chunks += result.indexed_count
        except Exception as exc:
            return IndexBuildReport(
                request.index_version,
                len(revisions),
                elements,
                chunks,
                "FAILED",
                {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
            )
        self._hits = staged._hits
        self._revisions = staged._revisions
        self._active = staged._active
        return IndexBuildReport(request.index_version, len(revisions), elements, chunks, "SUCCEEDED")


class OpenSearchIndexBackend:
    """Thin OpenSearch adapter; all canonical reads remain outside the index."""

    def __init__(self, *, client: Any, index_name: str = "kr-v2-evidence", embedding: Any | None = None, canonical: Any | None = None) -> None:
        self.client = client
        self.index_name = index_name
        self.embedding = embedding
        self.canonical = canonical

    def publish_revision(self, revision: RevisionIndexInput, *, index_version: str | None = None) -> IndexPublishResult:
        version = index_version or self.index_name
        rows = tuple(revision.chunks) or revision.elements
        for row in rows:
            element_id = row.element_ids[0] if hasattr(row, "element_ids") else row.element_id
            body = {
                "document_id": revision.document_id,
                "revision_id": revision.revision_id,
                "element_id": element_id,
                "source_name": revision.source_name,
                "text": row.text,
                "section_path": list(getattr(row, "section_path", ())),
                "page": getattr(row, "page", None),
                "metadata": dict(getattr(row, "metadata", {}) or {}),
            }
            if self.embedding is not None:
                body["vector"] = self.embedding.embed_batch([row.text])[0]
            self.client.index(index=self.index_name, id=f"{revision.document_id}:{revision.revision_id}:{element_id}", body=body, refresh=False)
        checksum = hashlib.sha256(f"{revision.document_id}:{revision.revision_id}:{len(rows)}".encode()).hexdigest()
        return IndexPublishResult(version, revision.document_id, revision.revision_id, len(rows), checksum)

    def remove_revision(self, document_id: str, revision_id: str) -> None:
        self.client.delete_by_query(
            index=self.index_name,
            body={"query": {"bool": {"filter": [{"term": {"document_id": document_id}}, {"term": {"revision_id": revision_id}}]}}},
            refresh=True,
        )

    def search_evidence(self, request: EvidenceSearchRequest) -> EvidenceSearchPage:
        body: dict[str, Any] = {
            "size": min(200, max(request.limit, request.limit * 5)),
            "query": {"multi_match": {"query": request.query, "fields": ["text^2", "source_name", "section_path"]}},
        }
        response = self.client.search(index=self.index_name, body=body)
        hits = []
        for row in response.get("hits", {}).get("hits", []):
            source = row.get("_source", {})
            if self.canonical is not None and self.canonical.current_revision(source["document_id"]) != source["revision_id"]:
                continue
            hits.append(
                SearchHitV2(
                    ref=EvidenceRef(source["document_id"], source["revision_id"], source["element_id"], {"type": "element"}),
                    display_name=source.get("source_name", source["document_id"]),
                    preview=source.get("text", "")[:240],
                    score=float(row.get("_score", 0.0)),
                    rank=len(hits) + 1,
                    metadata=source.get("metadata", {}),
                )
            )
            if len(hits) >= request.limit:
                break
        return EvidenceSearchPage(tuple(hits), diagnostics={"backend": "opensearch"})

    def search_assets(self, request: AssetSearchRequest) -> AssetSearchPage:
        response = self.client.search(
            index=self.index_name,
            body={"size": request.limit, "query": {"multi_match": {"query": request.query, "fields": ["text", "source_name"]}}},
        )
        groups: dict[tuple[str, str], AssetSearchHit] = {}
        for row in response.get("hits", {}).get("hits", []):
            source = row.get("_source", {})
            if self.canonical is not None and self.canonical.current_revision(source["document_id"]) != source["revision_id"]:
                continue
            key = (source["document_id"], source["revision_id"])
            groups.setdefault(
                key,
                AssetSearchHit(key[0], key[1], source.get("source_name", key[0]), source.get("source_name", key[0]), source.get("metadata", {}), row.get("_score")),
            )
        return AssetSearchPage(tuple(groups.values()))

    def rebuild(self, request: IndexRebuildRequest) -> IndexBuildReport:
        if self.canonical is None:
            return IndexBuildReport(request.index_version, 0, 0, 0, "SKIPPED", {"reason": "canonical store unavailable"})
        revisions = self.canonical.list_current_documents()
        if request.document_ids:
            revisions = [revision for revision in revisions if revision.document_id in request.document_ids]
        elements = chunks = 0
        try:
            for revision in revisions:
                ir = self.canonical.get_ir(revision.document_id, revision.revision_id)
                if not request.dry_run:
                    result = self.publish_revision(RevisionIndexInput.from_ir(ir), index_version=request.index_version)
                    chunks += result.indexed_count
                else:
                    chunks += len(ir.elements)
                elements += len(ir.elements)
        except Exception as exc:
            return IndexBuildReport(
                request.index_version,
                len(revisions),
                elements,
                chunks,
                "FAILED",
                {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
            )
        return IndexBuildReport(request.index_version, len(revisions), elements, chunks, "SUCCEEDED")

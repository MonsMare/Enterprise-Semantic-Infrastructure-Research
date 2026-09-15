from __future__ import annotations

import json
from typing import Any

from ..errors import KRInvalidLocator, KRNotFound
from ..models import Evidence as LegacyEvidence
from ..models import Locator, Page, ProviderDescriptor, ReadOptions, ResourceEntry, SearchHit, SearchOptions
from .context import ContextRuntime
from .contracts import AssetSearchRequest, EvidenceRef


class LegacyProviderAdapter:
    provider_id = "v2"

    def __init__(self, runtime: ContextRuntime) -> None:
        self.runtime = runtime

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            provider_id=self.provider_id,
            supported_operations=("list", "find", "search", "read", "stat"),
            query_language="hybrid",
            selector_types=("element",),
            revision_model="document-revision",
            max_read_bytes=1_000_000,
        )

    def list(self, *, scope: str | None = None, cursor: str | None = None, limit: int = 20) -> Page[ResourceEntry]:
        page = self.runtime.search_assets(scope or "", limit=limit, cursor=cursor) if scope else self._all_assets(limit=limit, cursor=cursor)
        entries = [
            ResourceEntry(self._locator(item.document_id, item.revision_id), item.display_name)
            for item in page.items
        ]
        return Page(entries, next_cursor=page.next_cursor, snapshot_id=page.snapshot_id, partial=page.partial)

    def _all_assets(self, *, limit: int, cursor: str | None) -> Any:
        documents = self.runtime.canonical.list_current_documents()
        items = [
            type("Asset", (), {"document_id": row.document_id, "revision_id": row.revision_id, "display_name": row.source_name})()
            for row in documents
        ]
        return type("AssetPage", (), {"items": items, "next_cursor": None, "snapshot_id": "legacy", "partial": False})()

    def find(self, pattern: str, *, scope: str | None = None, cursor: str | None = None, limit: int = 20) -> Page[ResourceEntry]:
        page = self.runtime.search_assets(pattern, limit=limit, cursor=cursor)
        entries = [
            ResourceEntry(self._locator(item.document_id, item.revision_id), item.display_name)
            for item in page.items
            if pattern.casefold() in item.display_name.casefold() or not pattern.strip()
        ]
        return Page(entries, next_cursor=page.next_cursor, snapshot_id=page.snapshot_id, partial=page.partial)

    def search(self, query: str, *, scope: str | None = None, options: SearchOptions | None = None) -> Page[SearchHit]:
        options = options or SearchOptions()
        page = self.runtime.search_evidence(query, limit=options.limit, cursor=options.cursor)
        hits = [
            SearchHit(
                locator=self._locator(item.ref.document_id, item.ref.revision_id, item.ref.element_id),
                display_name=item.display_name,
                preview=item.preview,
                ordering_key=f"{item.display_name}:{item.ref.element_id}",
            )
            for item in page.items
        ]
        return Page(hits, next_cursor=page.next_cursor, snapshot_id=page.snapshot_id, partial=page.partial)

    def read(self, locator: Locator, options: ReadOptions | None = None) -> LegacyEvidence:
        if locator.provider != self.provider_id:
            raise KRInvalidLocator("locator belongs to another provider")
        selector = locator.selector or {}
        element_id = selector.get("element_id")
        if not element_id:
            raise KRInvalidLocator("v2 locator requires an element_id selector")
        evidence = self.runtime.get_evidence(
            EvidenceRef(locator.resource_id, locator.revision, str(element_id), selector),
            max_bytes=(options or ReadOptions()).max_bytes,
        )
        return LegacyEvidence(
            evidence_id=evidence.evidence_id,
            locator=Locator(
                1,
                self.provider_id,
                evidence.ref.document_id,
                evidence.ref.revision_id,
                evidence.ref.selector,
            ),
            content=evidence.content,
            content_hash=evidence.content_hash,
            media_type=evidence.media_type,
            encoding=evidence.encoding,
            source_revision=evidence.source_revision,
            resolved_selector=evidence.resolved_provenance,
            retrieved_at=evidence.retrieved_at,
            truncated=evidence.truncated,
            representation=evidence.representation,
            derived_from=evidence.derived_from,
            source_label=evidence.source_label,
        )

    def stat(self, locator: Locator) -> dict[str, Any]:
        if locator.provider != self.provider_id:
            raise KRInvalidLocator("locator belongs to another provider")
        revision = self.runtime.canonical.get_revision(locator.resource_id, locator.revision)
        ir = self.runtime.canonical.get_ir(locator.resource_id, locator.revision)
        return {
            "provider": self.provider_id,
            "resource_id": locator.resource_id,
            "name": revision.source_name,
            "revision": revision.revision_id,
            "media_type": "application/octet-stream",
            "size_bytes": sum(len(element.text.encode("utf-8")) for element in ir.elements),
        }

    def _locator(self, document_id: str, revision_id: str, element_id: str | None = None) -> Locator:
        selector = {"element_id": element_id} if element_id else None
        return Locator(1, self.provider_id, document_id, revision_id, selector)


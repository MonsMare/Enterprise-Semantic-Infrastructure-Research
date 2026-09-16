from __future__ import annotations

from typing import Any

from ..errors import KRInvalidLocator
from ..models import Evidence as LegacyEvidence
from ..models import Locator, Page, ProviderDescriptor, ReadOptions, ResourceEntry, SearchHit, SearchOptions
from .context import ContextRuntime
from .contracts import EvidenceRef


class KnowledgeAccessRuntime:
    """Stable L3 knowledge-access protocol built over canonical Evidence.

    Search operations yield opaque revision-bound locators. Only ``read``
    resolves a locator into source text, through ``ContextRuntime.get_evidence``.
    This keeps Evidence retrieval explicit for human callers and Agent Runtime.
    """

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
        if scope and scope.strip():
            page = self.runtime.search_assets(scope, limit=limit, cursor=cursor)
            entries = [
                ResourceEntry(self._locator(item.document_id, item.revision_id), item.display_name)
                for item in page.items
            ]
            return Page(entries, next_cursor=page.next_cursor, snapshot_id=page.snapshot_id, partial=page.partial)
        documents = self.runtime.canonical.list_current_documents()
        entries = [
            ResourceEntry(self._locator(item.document_id, item.revision_id), item.source_name)
            for item in documents[:limit]
        ]
        return Page(entries, next_cursor=None, snapshot_id="canonical-current", partial=False)

    def find(self, pattern: str, *, scope: str | None = None, cursor: str | None = None, limit: int = 20) -> Page[ResourceEntry]:
        if not pattern.strip():
            return self.list(scope=scope, cursor=cursor, limit=limit)
        page = self.runtime.search_assets(pattern, limit=limit, cursor=cursor)
        entries = [
            ResourceEntry(self._locator(item.document_id, item.revision_id), item.display_name)
            for item in page.items
        ]
        return Page(entries, next_cursor=page.next_cursor, snapshot_id=page.snapshot_id, partial=page.partial)

    def search(self, query: str, *, scope: str | None = None, options: SearchOptions | None = None) -> Page[SearchHit]:
        del scope
        options = options or SearchOptions()
        page = self.runtime.search_evidence(query, limit=options.limit, cursor=options.cursor)
        hits = [
            SearchHit(
                locator=self._locator(item.ref.document_id, item.ref.revision_id, item.ref.element_id, item.ref.selector),
                display_name=item.display_name,
                preview=item.preview,
                ordering_key=f"{item.display_name}:{item.ref.element_id}",
            )
            for item in page.items
        ]
        return Page(hits, next_cursor=page.next_cursor, snapshot_id=page.snapshot_id, partial=page.partial)

    def read(self, locator: Locator, options: ReadOptions | None = None) -> LegacyEvidence:
        ref = self.to_evidence_ref(locator)
        options = options or ReadOptions()
        evidence = self.runtime.get_evidence(
            ref,
            max_bytes=options.max_bytes,
            representation=options.representation,
        )
        return LegacyEvidence(
            evidence_id=evidence.evidence_id,
            locator=self._locator(
                evidence.ref.document_id,
                evidence.ref.revision_id,
                evidence.ref.element_id,
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
        self._validate_locator(locator)
        revision = self.runtime.canonical.get_revision(locator.resource_id, locator.revision)
        ir = self.runtime.canonical.get_ir(locator.resource_id, locator.revision)
        return {
            "provider": self.provider_id,
            "resource_id": locator.resource_id,
            "name": revision.source_name,
            "revision": revision.revision_id,
            "state": revision.state,
            "source_hash": revision.source_hash,
            "parser": {"name": revision.parser_name, "version": revision.parser_version},
            "element_count": len(ir.elements),
            "artifact_count": len(ir.source_artifacts),
        }

    def to_evidence_ref(self, locator: Locator) -> EvidenceRef:
        self._validate_locator(locator)
        selector = dict(locator.selector or {})
        element_id = selector.get("element_id")
        if not element_id:
            raise KRInvalidLocator("v2 locator requires an element_id selector")
        return EvidenceRef(locator.resource_id, locator.revision, str(element_id), selector)

    def _validate_locator(self, locator: Locator) -> None:
        if locator.provider != self.provider_id:
            raise KRInvalidLocator("locator belongs to another provider")

    def _locator(
        self,
        document_id: str,
        revision_id: str,
        element_id: str | None = None,
        selector: Any | None = None,
    ) -> Locator:
        resolved_selector = dict(selector or {})
        if element_id:
            resolved_selector["element_id"] = element_id
        return Locator(1, self.provider_id, document_id, revision_id, resolved_selector or None)

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .errors import KRCursorInvalid, KRInvalidLocator, KRLimitExceeded, KRNotFound, KRQueryInvalid, KRStaleLocator
from .models import (
    Evidence,
    Locator,
    Page,
    ProviderDescriptor,
    ReadOptions,
    ResourceEntry,
    SearchHit,
    SearchOptions,
    MAX_PAGE_SIZE,
    content_hash,
)


@dataclass(frozen=True)
class _Resource:
    name: str
    content: str
    media_type: str


class MemoryProvider:
    provider_id = "memory"

    def __init__(self, documents: dict[str, str], *, provider_id: str = "memory") -> None:
        self.provider_id = provider_id
        self._resources = {
            resource_id: _Resource(name=resource_id, content=content, media_type="text/plain")
            for resource_id, content in documents.items()
        }

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            provider_id=self.provider_id,
            supported_operations=("list", "find", "search", "read", "stat"),
            query_language="lexical",
            selector_types=("line", "section"),
            revision_model="content-hash",
            max_read_bytes=1_000_000,
        )

    def _revision(self, resource: _Resource, resource_id: str | None = None) -> str:
        return content_hash(resource.content)

    def _locator(self, resource_id: str, selector: dict[str, Any] | None = None) -> Locator:
        resource = self._resources.get(resource_id)
        if resource is None:
            raise KRNotFound(resource_id)
        return Locator(1, self.provider_id, resource_id, self._revision(resource, resource_id), selector)

    def _page(self, entries: list[Any], cursor: str | None, limit: int, *, query_key: str) -> Page[Any]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if limit > MAX_PAGE_SIZE:
            raise KRLimitExceeded(f"limit cannot exceed {MAX_PAGE_SIZE}")
        material = json.dumps(
            [query_key, [(item.locator.resource_id, item.locator.revision, item.locator.selector) for item in entries]],
            ensure_ascii=False,
            sort_keys=True,
        )
        snapshot_id = hashlib.sha256(material.encode("utf-8")).hexdigest()
        offset = 0
        if cursor:
            try:
                payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8"))
                if payload.get("snapshot_id") != snapshot_id or payload.get("query_key") != query_key:
                    raise KRCursorInvalid("cursor does not match this query snapshot")
                offset = int(payload["offset"])
                if offset < 0 or offset > len(entries):
                    raise KRCursorInvalid("cursor offset is outside the result set")
            except KRCursorInvalid:
                raise
            except Exception as exc:
                raise KRCursorInvalid("invalid cursor") from exc
        page_items = entries[offset : offset + limit]
        if offset + limit < len(entries):
            next_cursor = base64.urlsafe_b64encode(
                json.dumps({"snapshot_id": snapshot_id, "query_key": query_key, "offset": offset + limit}).encode("utf-8")
            ).decode("ascii").rstrip("=")
        else:
            next_cursor = None
        return Page(page_items, next_cursor=next_cursor, snapshot_id=snapshot_id)

    @staticmethod
    def _within_scope(resource_id: str, scope: str | None) -> bool:
        if not scope:
            return True
        normalized_scope = scope.replace("\\", "/").strip("/").casefold()
        normalized_id = resource_id.replace("\\", "/").strip("/").casefold()
        return normalized_id == normalized_scope or normalized_id.startswith(normalized_scope + "/")

    def list(self, *, scope: str | None = None, cursor: str | None = None, limit: int = 20) -> Page[ResourceEntry]:
        entries = [
            ResourceEntry(self._locator(resource_id), resource.name)
            for resource_id, resource in sorted(self._resources.items(), key=lambda item: item[1].name.lower())
            if self._within_scope(resource_id, scope)
        ]
        return self._page(entries, cursor, limit, query_key=f"list:{scope or ''}")

    def find(self, pattern: str, *, scope: str | None = None, cursor: str | None = None, limit: int = 20) -> Page[ResourceEntry]:
        lowered = pattern.lower()
        entries = [
            ResourceEntry(self._locator(resource_id), resource.name)
            for resource_id, resource in sorted(self._resources.items(), key=lambda item: item[1].name.lower())
            if self._within_scope(resource_id, scope) and (lowered in resource.name.lower() or fnmatch.fnmatch(resource.name.lower(), lowered))
        ]
        return self._page(entries, cursor, limit, query_key=f"find:{scope or ''}:{lowered}")

    def search(self, query: str, *, scope: str | None = None, options: SearchOptions | None = None) -> Page[SearchHit]:
        options = options or SearchOptions()
        if not query.strip():
            raise KRQueryInvalid("search query must not be empty")
        needle = query if options.case_sensitive else query.casefold()
        hits: list[SearchHit] = []
        for resource_id, resource in sorted(self._resources.items(), key=lambda item: item[1].name.lower()):
            if not self._within_scope(resource_id, scope):
                continue
            haystack = resource.content if options.case_sensitive else resource.content.casefold()
            if needle not in haystack:
                continue
            line_number = next(index for index, line in enumerate(resource.content.splitlines(), 1) if needle in (line if options.case_sensitive else line.casefold()))
            line = resource.content.splitlines()[line_number - 1]
            selector = {"type": "line", "start": line_number, "end": line_number}
            hits.append(
                SearchHit(
                    locator=self._locator(resource_id, selector),
                    display_name=resource.name,
                    preview=line[:240],
                    ordering_key=f"{resource.name.lower()}:{line_number:08d}",
                )
            )
        query_key = f"search:{scope or ''}:{query if options.case_sensitive else query.casefold()}:{int(options.case_sensitive)}"
        return self._page(hits, options.cursor, options.limit, query_key=query_key)

    def read(self, locator: Locator, options: ReadOptions | None = None) -> Evidence:
        options = options or ReadOptions()
        if locator.provider != self.provider_id:
            raise KRInvalidLocator("locator belongs to another provider")
        if options.max_bytes > self.descriptor.max_read_bytes:
            raise KRLimitExceeded(f"max_bytes cannot exceed provider limit {self.descriptor.max_read_bytes}")
        resource = self._resources.get(locator.resource_id)
        if resource is None:
            raise KRNotFound(locator.resource_id)
        current_revision = self._revision(resource, locator.resource_id)
        if locator.revision != current_revision:
            raise KRStaleLocator(f"expected {locator.revision}, current {current_revision}")
        selected = self._select(resource.content, locator.selector)
        encoded = selected.encode("utf-8")
        truncated = len(encoded) > options.max_bytes
        if truncated:
            selected = encoded[: options.max_bytes].decode("utf-8", errors="ignore")
        return Evidence.from_content(
            locator=locator,
            content=selected,
            media_type=resource.media_type,
            representation=options.representation,
            resolved_selector=locator.selector,
            truncated=truncated,
            derived_from=None,
            source_label=resource.name,
        )

    def _select(self, content: str, selector: dict[str, Any] | None) -> str:
        if selector is None:
            return content
        selector_type = selector.get("type")
        lines = content.splitlines()
        if selector_type == "line":
            start = int(selector.get("start", 0))
            end = int(selector.get("end", start))
            if start <= 0 or end < start or end > len(lines):
                raise KRInvalidLocator("line selector is out of range")
            return "\n".join(lines[start - 1 : end])
        if selector_type == "section":
            heading = str(selector.get("value", ""))
            marker = re.compile(r"^#{1,6}\s+" + re.escape(heading) + r"\s*$", re.IGNORECASE)
            for index, line in enumerate(lines):
                match = re.match(r"^(#{1,6})\s+", line)
                if marker.fullmatch(line.strip()):
                    level = len(match.group(1)) if match else 1
                    end = next((i for i in range(index + 1, len(lines)) if (m := re.match(r"^(#{1,6})\s+", lines[i])) and len(m.group(1)) <= level), len(lines))
                    return "\n".join(lines[index:end])
            raise KRNotFound(heading)
        raise KRInvalidLocator(f"unsupported selector type: {selector_type}")

    def stat(self, locator: Locator) -> dict[str, Any]:
        resource = self._resources.get(locator.resource_id)
        if resource is None:
            raise KRNotFound(locator.resource_id)
        return {
            "provider": self.provider_id,
            "resource_id": locator.resource_id,
            "name": resource.name,
            "revision": self._revision(resource, locator.resource_id),
            "media_type": resource.media_type,
            "size_bytes": len(resource.content.encode("utf-8")),
        }

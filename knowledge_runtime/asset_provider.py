from __future__ import annotations

import re
import math
from dataclasses import replace
from typing import Any

from .assets import KnowledgeAssetStore
from .errors import KRInvalidLocator, KRNotFound
from .memory_provider import MemoryProvider
from .models import Evidence, SearchHit, SearchOptions


class AssetKnowledgeProvider(MemoryProvider):
    """Queryable view over persisted parsed assets, preserving source revisions."""

    def __init__(self, store: KnowledgeAssetStore) -> None:
        self.store = store
        self._assets = {}
        super().__init__({}, provider_id="knowledge-assets")
        if not hasattr(store, "search_current"):
            self.refresh()

    def refresh(self) -> None:
        """Rebuild the query view from the store's current asset pointers."""
        self._assets = {asset.asset_id: asset for asset in self.store.list_assets()}
        self._resources = {
            asset_id: replace(
                resource,
                name=asset.source_name,
                media_type="text/markdown",
            )
            for asset_id, asset in self._assets.items()
            for resource in [self._resource_from_asset(asset)]
        }

    @staticmethod
    def _resource_from_asset(asset):
        from .memory_provider import _Resource

        return _Resource(name=asset.source_name, content=asset.markdown, media_type="text/markdown")

    def list(self, **kwargs):
        self.refresh()
        return super().list(**kwargs)

    def find(self, *args, **kwargs):
        self.refresh()
        return super().find(*args, **kwargs)

    def search(self, *args, **kwargs):
        if hasattr(self.store, "search_current"):
            query = args[0] if args else kwargs.get("query", "")
            scope = args[1] if len(args) > 1 else kwargs.get("scope")
            options = kwargs.get("options") or (args[2] if len(args) > 2 else None) or SearchOptions()
            if scope:
                self.refresh()
                return super().search(*args, **kwargs)
            if options.case_sensitive:
                self.refresh()
                return super().search(*args, **kwargs)
            assets = self.store.search_current(query)
            hits = []
            for asset in assets:
                self._cache_asset(asset)
                lines = asset.markdown.splitlines()
                start, end = self._best_match_window(lines, query)
                locator = self._locator(asset.asset_id, {"type": "line", "start": start, "end": max(start, end)})
                hits.append(
                    SearchHit(
                        locator=locator,
                        display_name=asset.source_name,
                        preview="\n".join(lines[start - 1 : min(end, start + 2)])[:240] if lines else "",
                        ordering_key=f"{asset.source_name.lower()}:{start:08d}",
                    )
                )
            query_key = f"search::{query.casefold()}:{int(options.case_sensitive)}"
            return self._page(hits, options.cursor, options.limit, query_key=query_key)
        self.refresh()
        return super().search(*args, **kwargs)

    def _revision(self, resource: Any, resource_id: str | None = None) -> str:
        asset = self._assets.get(resource_id or "")
        return (asset.revision_id or asset.source_hash) if asset else super()._revision(resource)

    def read(self, locator, options=None) -> Evidence:
        if locator.provider != self.provider_id:
            return super().read(locator, options)
        if hasattr(self.store, "search_current"):
            try:
                self._cache_asset(self.store.get(locator.resource_id))
            except KRNotFound:
                self._assets.pop(locator.resource_id, None)
                self._resources.pop(locator.resource_id, None)
                raise
        else:
            self.refresh()
        evidence = super().read(locator, options)
        asset = self._assets.get(locator.resource_id)
        return replace(evidence, derived_from=asset.asset_id if asset else None)

    def stat(self, locator) -> dict[str, Any]:
        if locator.provider != self.provider_id:
            raise KRInvalidLocator("locator belongs to another provider")
        if hasattr(self.store, "search_current"):
            try:
                self._cache_asset(self.store.get(locator.resource_id))
            except KRNotFound:
                self._assets.pop(locator.resource_id, None)
                self._resources.pop(locator.resource_id, None)
                raise
        else:
            self.refresh()
        result = super().stat(locator)
        asset = self._assets.get(locator.resource_id)
        if asset:
            result.update({"source_hash": asset.source_hash, "parser": asset.parser_name, "parser_version": asset.parser_version})
        return result

    def _cache_asset(self, asset) -> None:
        self._assets[asset.asset_id] = asset
        self._resources[asset.asset_id] = self._resource_from_asset(asset)

    @staticmethod
    def _normalize_window_token(token: str) -> str:
        if token.endswith("ies") and len(token) > 4:
            token = token[:-3] + "y"
        if token.endswith("al") and len(token) > 5:
            token = token[:-2]
        if token.endswith("y") and len(token) > 3:
            token = token[:-1] + "i"
        if token.endswith("s") and len(token) > 4:
            token = token[:-1]
        return token

    @classmethod
    def _best_match_window(cls, lines: list[str], query: str) -> tuple[int, int]:
        """Choose a compact passage containing the most informative query terms."""
        if not lines:
            return 1, 1
        normalized_query = " ".join(query.casefold().split())
        for index, line in enumerate(lines, 1):
            if normalized_query and normalized_query in " ".join(line.casefold().split()):
                return index, index

        stopwords = {
            "a", "about", "after", "all", "an", "and", "are", "as", "at", "be", "been",
            "before", "being", "between", "by", "can", "could",
            "did", "do", "does", "for", "from", "have", "how", "in", "include", "includes",
            "into", "is", "it", "its", "kind", "kinds", "may", "might", "no", "of", "on",
            "or", "should", "that", "the", "their", "them", "there", "these", "this", "those",
            "to", "type", "types", "use", "used", "uses", "using", "was", "were", "what",
            "when", "where", "which", "who", "whom", "why", "will", "with", "would", "work",
            "actuary", "actuaries", "asop", "eiopa",
        }
        scope_synonyms = {"cover": "scope", "covered": "scope", "covers": "scope"}
        query_tokens = [
            scope_synonyms.get(token, token)
            for token in re.findall(r"\w+", query.casefold())
            if token not in stopwords and not token.isdigit()
        ]
        query_tokens = [token for token in query_tokens if token not in {"asop", "eiopa"}]
        terms = list(dict.fromkeys(cls._normalize_window_token(token) for token in query_tokens))
        if not terms:
            return 1, 1
        line_tokens = [
            [
                cls._normalize_window_token(token)
                for token in re.findall(r"\w+", line.casefold())
            ]
            for line in lines
        ]
        line_terms = [set(tokens) for tokens in line_tokens]
        line_bigrams = [
            set(zip(tokens, tokens[1:])) for tokens in line_tokens
        ]
        frequencies = {
            term: sum(term in current for current in line_terms)
            for term in terms
        }
        weights = {
            term: 1.0 + math.log((len(lines) + 1) / (frequency + 1))
            for term, frequency in frequencies.items()
        }
        query_bigrams = set(zip(terms, terms[1:]))
        best: tuple[int, int] | None = None
        best_score = -1.0
        max_window_lines = min(12, len(lines))
        for left in range(len(lines)):
            covered: set[str] = set()
            window_bigrams: set[tuple[str, str]] = set()
            for right in range(left, min(len(lines), left + max_window_lines)):
                covered.update(line_terms[right])
                window_bigrams.update(line_bigrams[right])
                if right > left and line_tokens[right - 1] and line_tokens[right]:
                    window_bigrams.add((line_tokens[right - 1][-1], line_tokens[right][0]))
                matched = covered.intersection(terms)
                score = sum(weights[term] for term in matched)
                if matched and query_bigrams:
                    score += 2.0 * len(window_bigrams.intersection(query_bigrams))
                if score > best_score or (
                    score == best_score and best is not None and right - left < best[1] - best[0]
                ):
                    best = (left, right)
                    best_score = score
        if best is None or best_score <= 0:
            best_line = max(range(len(lines)), key=lambda index: len(line_terms[index].intersection(terms)))
            best = (best_line, best_line)
        start = max(1, best[0] + 1 - 1)
        end = min(len(lines), best[1] + 1 + 1)
        return start, end

from __future__ import annotations

import re
import math
from collections import OrderedDict
from dataclasses import replace
from typing import Any

from .assets import KnowledgeAssetStore
from .errors import KRInvalidLocator, KRLimitExceeded, KRNotFound, KRStaleLocator
from .memory_provider import MemoryProvider
from .models import Evidence, Locator, ReadOptions, SearchHit, SearchOptions, MAX_PAGE_SIZE
from .retrieval import LocalNgramEncoder, SemanticEncoder, cosine_similarity, reciprocal_rank_fusion


_VECTOR_CACHE_SIZE = 10_000
_LOCAL_NGRAM_MIN_SIMILARITY = 0.12


class AssetKnowledgeProvider(MemoryProvider):
    """Queryable view over persisted parsed assets, preserving source revisions."""

    def __init__(
        self,
        store: KnowledgeAssetStore,
        *,
        semantic_encoder: SemanticEncoder | None = None,
    ) -> None:
        self.store = store
        self.semantic_encoder = semantic_encoder if semantic_encoder is not None else LocalNgramEncoder()
        self._semantic_vector_cache: OrderedDict[str, Any] = OrderedDict()
        self._semantic_vector_cache_size = _VECTOR_CACHE_SIZE
        self._assets = {}
        super().__init__({}, provider_id="knowledge-assets")
        if not hasattr(store, "search_current"):
            self.refresh()

    @property
    def descriptor(self):
        return replace(super().descriptor, selector_types=("line", "section", "chunk"))

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
        if hasattr(self.store, "search_chunks") and hasattr(self.store, "list_current_chunks"):
            query = args[0] if args else kwargs.get("query", "")
            scope = args[1] if len(args) > 1 else kwargs.get("scope")
            options = kwargs.get("options") or (args[2] if len(args) > 2 else None) or SearchOptions()
            lexical_candidates = self.store.search_chunks(
                query,
                scope=scope,
                limit=MAX_PAGE_SIZE,
            )
            def has_case_sensitive_term(chunk) -> bool:
                searchable = " ".join((chunk.source_name, *chunk.heading_path, chunk.text))
                return query in searchable

            if options.case_sensitive:
                lexical_candidates = [
                    item for item in lexical_candidates if has_case_sensitive_term(item[0])
                ]

            lexical_ranked = [
                chunk
                for chunk, _score in sorted(
                    lexical_candidates,
                    key=lambda item: (-item[1], item[0].chunk_id),
                )
            ]
            semantic_ranked = []
            if options.semantic_weight > 0.0:
                query_vector = self.semantic_encoder.encode(query)
            else:
                query_vector = None
            if (
                query_vector is not None
                and isinstance(self.semantic_encoder, LocalNgramEncoder)
                and hasattr(self.store, "has_current_chunk_vector_index")
                and self.store.has_current_chunk_vector_index(
                    self.semantic_encoder.encoder_id,
                    scope=scope,
                )
            ):
                chunks = self.store.list_current_chunks(scope=scope)
                if options.case_sensitive:
                    chunks = [chunk for chunk in chunks if has_case_sensitive_term(chunk)]
                semantic_candidates = []
                if len(chunks) <= self._semantic_vector_cache_size:
                    missing_ids = {
                        chunk.chunk_id
                        for chunk in chunks
                        if chunk.chunk_id not in self._semantic_vector_cache
                    }
                    if missing_ids:
                        for chunk, vector in self.store.list_current_chunks_with_vectors(
                            encoder_id=self.semantic_encoder.encoder_id,
                            scope=scope,
                        ):
                            if chunk.chunk_id in missing_ids:
                                self._remember_chunk_vector(chunk.chunk_id, vector)
                    for chunk in chunks:
                        vector = self._semantic_vector_cache.get(chunk.chunk_id)
                        if vector is None:
                            vector = self._chunk_vector(
                                chunk,
                                "\n".join((*chunk.heading_path, chunk.text)),
                            )
                        semantic_candidates.append(
                            (chunk, cosine_similarity(query_vector, vector))
                        )
                else:
                    # Keep the reusable vector cache bounded for large stores.
                    # This exact ranker still scans and materializes all chunks
                    # and scores, so its temporary working set grows with N.
                    for start in range(0, len(chunks), MAX_PAGE_SIZE):
                        batch = chunks[start : start + MAX_PAGE_SIZE]
                        vectors = {
                            chunk.chunk_id: vector
                            for chunk, vector in self.store.list_current_chunks_with_vectors(
                                encoder_id=self.semantic_encoder.encoder_id,
                                chunk_ids=[chunk.chunk_id for chunk in batch],
                            )
                        }
                        for chunk in batch:
                            vector = (
                                self._semantic_vector_cache.get(chunk.chunk_id)
                                or vectors.get(chunk.chunk_id)
                            )
                            if vector is None:
                                vector = self.semantic_encoder.encode(
                                    "\n".join((*chunk.heading_path, chunk.text))
                                )
                            semantic_candidates.append(
                                (chunk, cosine_similarity(query_vector, vector))
                            )
                semantic_ranked = [
                    chunk
                    for chunk, score in sorted(
                        semantic_candidates,
                        key=lambda item: (-item[1], item[0].chunk_id),
                    )
                    if score >= _LOCAL_NGRAM_MIN_SIMILARITY
                ][:MAX_PAGE_SIZE]
            elif query_vector is not None:
                chunks = self.store.list_current_chunks(scope=scope)
                if options.case_sensitive:
                    chunks = [chunk for chunk in chunks if has_case_sensitive_term(chunk)]
                # Injected or custom-dimension encoders have no matching
                # persistent index. Preserve semantic-only recall by scanning
                # current chunks; their vectors remain bounded by the LRU.
                def searchable_text(chunk) -> str:
                    return "\n".join((*chunk.heading_path, chunk.text))

                semantic_candidates = [
                    (
                        chunk,
                        cosine_similarity(
                            query_vector,
                            self._chunk_vector(chunk, searchable_text(chunk)),
                        ),
                    )
                    for chunk in chunks
                ]
                semantic_ranked = [
                    chunk
                    for chunk, _score in sorted(
                        (item for item in semantic_candidates if item[1] > 0.0),
                        key=lambda item: (-item[1], item[0].chunk_id),
                    )[:MAX_PAGE_SIZE]
                ]
            ranked_chunks = reciprocal_rank_fusion(
                lexical_ranked,
                semantic_ranked,
                key=lambda chunk: chunk.chunk_id,
                semantic_weight=options.semantic_weight,
                rrf_k=options.rrf_k,
            )
            hits = [
                SearchHit(
                    locator=Locator(
                        version=1,
                        provider=self.provider_id,
                        resource_id=chunk.asset_id,
                        revision=chunk.revision_id,
                        selector={
                            "type": "chunk",
                            "id": chunk.chunk_id,
                            "start": chunk.start_line,
                            "end": chunk.end_line,
                        },
                    ),
                    display_name=chunk.source_name,
                    preview=chunk.text[:240],
                    ordering_key=(
                        f"{chunk.source_name.casefold()}:{chunk.start_line:08d}:{chunk.chunk_id}"
                    ),
                )
                for chunk in ranked_chunks
            ]
            query_key = (
                f"search:{scope or ''}:{query.casefold()}:{int(options.case_sensitive)}:"
                f"{options.semantic_weight}:{options.rrf_k}"
            )
            return self._page(hits, options.cursor, options.limit, query_key=query_key)
        if hasattr(self.store, "search_current"):
            self.refresh()
            return super().search(*args, **kwargs)
        self.refresh()
        return super().search(*args, **kwargs)

    def _revision(self, resource: Any, resource_id: str | None = None) -> str:
        asset = self._assets.get(resource_id or "")
        return (asset.revision_id or asset.source_hash) if asset else super()._revision(resource)

    def read(self, locator, options=None) -> Evidence:
        if locator.provider != self.provider_id:
            return super().read(locator, options)
        if (
            locator.selector
            and locator.selector.get("type") == "chunk"
            and hasattr(self.store, "list_current_chunks")
        ):
            options = options or ReadOptions()
            if options.max_bytes > self.descriptor.max_read_bytes:
                raise KRLimitExceeded(
                    f"max_bytes cannot exceed provider limit {self.descriptor.max_read_bytes}"
                )
            asset, chunk = self._resolve_chunk(locator)
            selected = chunk.text
            encoded = selected.encode("utf-8")
            truncated = len(encoded) > options.max_bytes
            if truncated:
                selected = encoded[: options.max_bytes].decode("utf-8", errors="ignore")
            return Evidence.from_content(
                locator=locator,
                content=selected,
                media_type="text/markdown",
                representation=options.representation,
                resolved_selector=locator.selector,
                derived_from=asset.asset_id,
                source_label=asset.source_name,
                truncated=truncated,
            )
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
        if (
            locator.selector
            and locator.selector.get("type") == "chunk"
            and hasattr(self.store, "list_current_chunks")
        ):
            asset, chunk = self._resolve_chunk(locator)
            result = {
                "provider": self.provider_id,
                "resource_id": locator.resource_id,
                "name": asset.source_name,
                "revision": asset.revision_id or asset.source_hash,
                "media_type": "text/markdown",
                "size_bytes": len(chunk.text.encode("utf-8")),
                "chunk_id": chunk.chunk_id,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
            }
            result.update(
                {
                    "source_hash": asset.source_hash,
                    "parser": asset.parser_name,
                    "parser_version": asset.parser_version,
                }
            )
            return result
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

    def _resolve_chunk(self, locator) -> tuple[Any, Any]:
        try:
            asset = self.store.get(locator.resource_id)
        except KRNotFound:
            self._assets.pop(locator.resource_id, None)
            self._resources.pop(locator.resource_id, None)
            raise
        self._cache_asset(asset)
        current_revision = asset.revision_id or asset.source_hash
        if locator.revision != current_revision:
            raise KRStaleLocator(f"expected {locator.revision}, current {current_revision}")

        selector = locator.selector or {}
        chunk_id = selector.get("id")
        chunk = next(
            (
                current
                for current in self.store.list_current_chunks(asset_id=asset.asset_id)
                if current.chunk_id == chunk_id
            ),
            None,
        )
        if chunk is None:
            raise KRInvalidLocator("chunk selector does not identify a current chunk")
        try:
            start = int(selector["start"])
            end = int(selector["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise KRInvalidLocator("chunk selector requires a valid line range") from exc
        if start != chunk.start_line or end != chunk.end_line:
            raise KRInvalidLocator("chunk selector range does not match the current chunk")
        return asset, chunk

    def _cache_asset(self, asset) -> None:
        self._assets[asset.asset_id] = asset
        self._resources[asset.asset_id] = self._resource_from_asset(asset)

    def _chunk_vector(self, chunk, text: str):
        vector = self._semantic_vector_cache.get(chunk.chunk_id)
        if vector is not None:
            self._semantic_vector_cache.move_to_end(chunk.chunk_id)
            return vector
        vector = self.semantic_encoder.encode(text)
        self._remember_chunk_vector(chunk.chunk_id, vector)
        return vector

    def _remember_chunk_vector(self, chunk_id: str, vector: Any) -> None:
        self._semantic_vector_cache[chunk_id] = vector
        self._semantic_vector_cache.move_to_end(chunk_id)
        if len(self._semantic_vector_cache) > self._semantic_vector_cache_size:
            self._semantic_vector_cache.popitem(last=False)

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
